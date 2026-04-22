from __future__ import annotations

import imaplib
import re
import time
import random
import string
import threading
import hashlib
import traceback
from dataclasses import dataclass
from datetime import datetime
from email import message_from_bytes
from email.header import decode_header
from email.policy import default as email_default_policy
from email.utils import parsedate_to_datetime
from typing import Callable, Optional

import requests

from core.proxy_utils import build_requests_proxy_config, tracked_request
from platforms.chatgpt.invite_link import extract_invite_urls, resolve_invite_link

from .db import (
    get_luckmail_token_account_by_email,
    get_outlook_account_by_email,
    take_luckmail_token_account,
    take_outlook_account,
)


class _ServiceType:
    def __init__(self, value: str):
        self.value = value


@dataclass
class ProviderBase:
    proxy: str | None = None
    log_fn: callable | None = None
    fixed_email: str | None = None

    def _ensure_code_tracking_state(self) -> None:
        if not hasattr(self, "_used_codes") or not isinstance(getattr(self, "_used_codes", None), set):
            self._used_codes = set()
        if not hasattr(self, "_last_code"):
            self._last_code = ""
        if not hasattr(self, "_last_code_at"):
            self._last_code_at = 0.0
        if not hasattr(self, "_last_success_code"):
            self._last_success_code = ""
        if not hasattr(self, "_last_success_code_at"):
            self._last_success_code_at = 0.0

    @property
    def last_code(self) -> str:
        self._ensure_code_tracking_state()
        return self._last_success_code or self._last_code

    def _remember_code(self, code: str, *, successful: bool = False) -> None:
        self._ensure_code_tracking_state()
        code = str(code or "").strip()
        if not code:
            return
        now = time.time()
        self._last_code = code
        self._last_code_at = now
        self._used_codes.add(code)
        if successful:
            self._last_success_code = code
            self._last_success_code_at = now

    def remember_successful_code(self, code: str) -> None:
        self._remember_code(code, successful=True)

    def get_recent_code(self, max_age_seconds: int = 180, *, prefer_successful: bool = True) -> str:
        self._ensure_code_tracking_state()
        now = time.time()
        if (
            prefer_successful
            and self._last_success_code
            and now - self._last_success_code_at <= max_age_seconds
        ):
            return self._last_success_code
        if self._last_code and now - self._last_code_at <= max_age_seconds:
            return self._last_code
        return ""

    def extract_verification_code(self, text: str) -> Optional[str]:
        return self._extract_code(text)

    def wait_for_verification_code(
        self,
        email: str,
        timeout: int = 90,
        otp_sent_at: float | None = None,
        exclude_codes=None,
    ):
        exclude_codes = {str(item).strip() for item in (exclude_codes or set()) if str(item or "").strip()}
        code = self.get_verification_code(
            email=email,
            timeout=timeout,
            otp_sent_at=otp_sent_at,
            exclude_codes=exclude_codes,
        )
        code = str(code or "").strip()
        if code:
            self._remember_code(code, successful=False)
        return code

    def _log(self, message: str) -> None:
        if callable(self.log_fn):
            self.log_fn(message)

    def _interrupt(self, interrupt_check: Callable[[], None] | None = None) -> None:
        if callable(interrupt_check):
            interrupt_check()

    def _sleep_interruptibly(
        self,
        seconds: float,
        interrupt_check: Callable[[], None] | None = None,
        *,
        chunk: float = 0.5,
    ) -> None:
        remaining = max(0.0, float(seconds or 0.0))
        while remaining > 0:
            self._interrupt(interrupt_check)
            sleep_for = min(max(chunk, 0.05), remaining)
            time.sleep(sleep_for)
            remaining -= sleep_for
        self._interrupt(interrupt_check)

    @property
    def proxies(self):
        return build_requests_proxy_config(self.proxy)

    def _extract_code(self, text: str) -> Optional[str]:
        text = str(text or "")
        patterns = [
            r"(?is)(?:verification\s+code|one[-\s]*time\s+(?:password|code)|security\s+code|login\s+code|authentication\s+code|auth\s+code|验证码|校验码|动态码|認證碼|驗證碼)[^0-9]{0,40}(\d(?:[\s-]*\d){5})",
            r"(?is)\bcode\b[^0-9]{0,20}(\d(?:[\s-]*\d){5})",
            r"(?<!\d)(\d(?:[\s-]*\d){5})(?!\d)",
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if not match:
                continue
            raw = match.group(1) if match.groups() else match.group(0)
            normalized = re.sub(r"\D", "", raw)
            if len(normalized) == 6:
                return normalized
        return None

    def _decode_raw_content(self, raw: str) -> str:
        import html
        import quopri

        text = str(raw or "")
        if not text:
            return ""
        if "\r\n\r\n" in text:
            text = text.split("\r\n\r\n", 1)[1]
        elif "\n\n" in text:
            text = text.split("\n\n", 1)[1]
        try:
            text = quopri.decodestring(text).decode("utf-8", errors="ignore")
        except Exception:
            pass
        text = html.unescape(text)
        text = re.sub(r"(?im)^content-(?:type|transfer-encoding):.*$", " ", text)
        text = re.sub(r"(?im)^--+[_=\w.-]+$", " ", text)
        text = re.sub(r"(?i)----=_part_[\w.]+", " ", text)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    def _subject_preview(self, subject: str) -> str:
        text = re.sub(r"\s+", " ", str(subject or "")).strip()
        if len(text) <= 80:
            return text or "(无主题)"
        return f"{text[:77]}..."

    def get_invitation_link(self, email=None, timeout: int = 120, invite_sent_at=None, **kwargs):
        raise TimeoutError(f"{self.__class__.__name__} 不支持获取邀请链接")


class TempMailLolProvider(ProviderBase):
    service_type = _ServiceType("tempmail_lol")

    def __init__(self, *, api_base: str = "https://api.tempmail.lol/v2", **kwargs):
        super().__init__(**kwargs)
        self.api_base = api_base.rstrip("/")
        self._token = ""
        self._email = ""
        self._seen_ids: set[str] = set()
        if self.fixed_email:
            raise RuntimeError("tempmail_lol 不支持预置固定邮箱")

    def create_email(self, config=None):
        response = tracked_request(
            requests.request,
            "POST",
            f"{self.api_base}/inbox/create",
            proxies=self.proxies,
            timeout=20,
            json={},
        )
        payload = response.json()
        email = str(payload.get("address") or payload.get("email") or "").strip()
        token = str(payload.get("token") or "").strip()
        if not email or not token:
            raise RuntimeError(f"tempmail.lol 返回异常: {payload}")
        self._email = email
        self._token = token
        self._seen_ids = self._list_ids()
        self._log(f"[TempMail] 已创建邮箱: {email}")
        return {"email": email, "token": token, "service_id": token}

    def _list_ids(self) -> set[str]:
        if not self._token:
            return set()
        response = tracked_request(
            requests.request,
            "GET",
            f"{self.api_base}/inbox",
            params={"token": self._token},
            proxies=self.proxies,
            timeout=15,
        )
        payload = response.json()
        return {str(item.get("id")) for item in (payload.get("emails") or []) if item.get("id") is not None}

    def get_verification_code(self, email=None, timeout: int = 120, otp_sent_at=None, exclude_codes=None, **kwargs):
        interrupt_check = kwargs.get("interrupt_check")
        exclude_codes = {str(item) for item in (exclude_codes or set()) if item}
        deadline = time.monotonic() + max(int(timeout or 0), 1)
        while time.monotonic() < deadline:
            self._interrupt(interrupt_check)
            response = tracked_request(
                requests.request,
                "GET",
                f"{self.api_base}/inbox",
                params={"token": self._token},
                proxies=self.proxies,
                timeout=15,
            )
            payload = response.json()
            emails = sorted(payload.get("emails") or [], key=lambda item: item.get("date", 0), reverse=True)
            for mail in emails:
                message_id = str(mail.get("id") or "")
                if not message_id or message_id in self._seen_ids:
                    continue
                if otp_sent_at and (mail.get("date", 0) / 1000) < otp_sent_at:
                    continue
                self._seen_ids.add(message_id)
                text = " ".join([
                    str(mail.get("subject") or ""),
                    str(mail.get("body") or ""),
                    str(mail.get("html") or ""),
                ])
                code = self._extract_code(text)
                if code and code not in exclude_codes:
                    return code
            self._sleep_interruptibly(3, interrupt_check)
        raise TimeoutError(f"TempMail.lol 等待验证码超时 ({timeout}s)")


class LuckMailProvider(ProviderBase):
    service_type = _ServiceType("luckmail")

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        project_code: str = "openai",
        email_type: str = "",
        domain: str = "",
        fixed_account: dict | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.project_code = project_code or "openai"
        self.email_type = email_type or None
        self.domain = domain or None
        self._fixed_account = dict(fixed_account or {}) if isinstance(fixed_account, dict) else None
        self._email = ""
        self._token = ""
        self._seen_ids: set[str] = set()
        self._seen_message_keys: set[str] = set()
        self._logged_old_message_keys: set[str] = set()
        if not self.base_url or not self.api_key:
            raise RuntimeError("LuckMail 未配置：请填写 luckmail_base_url 和 luckmail_api_key")

    @property
    def headers(self):
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-API-Key": self.api_key,
        }

    def _request(self, method: str, path: str, **kwargs):
        response = tracked_request(
            requests.request,
            method,
            f"{self.base_url}{path}",
            headers={**self.headers, **kwargs.pop("headers", {})},
            proxies=self.proxies,
            timeout=kwargs.pop("timeout", 30),
            **kwargs,
        )
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError(f"LuckMail 返回非 JSON: {response.text[:200]}")
        if payload.get("code") != 0:
            raise RuntimeError(str(payload.get("message") or payload))
        return payload.get("data") or {}

    def _find_existing_purchase(self, email: str) -> tuple[str, str] | None:
        data = self._request(
            "GET",
            "/api/v1/openapi/email/purchases",
            params={"page": 1, "page_size": 100, "keyword": email},
        )
        for item in data.get("list") or []:
            matched = str(item.get("email_address") or "").strip().lower()
            if matched == email.strip().lower() and item.get("token"):
                return str(item["email_address"]), str(item["token"])
        return None

    @staticmethod
    def _normalize_mail_text(subject: str, body: str, html_body: str) -> str:
        return " ".join([str(subject or ""), str(body or ""), str(html_body or "")]).strip()

    def _extract_mail_metadata(self, mail: dict) -> dict[str, object]:
        subject = str(mail.get("subject") or "").strip()
        body = str(mail.get("body") or "")
        html_body = str(mail.get("html_body") or "")
        text = self._normalize_mail_text(subject, body, html_body)
        code = self._extract_code(text)
        lowered = text.lower()
        related = any(
            keyword in lowered
            for keyword in ("openai", "chatgpt", "verification code", "验证码")
        )
        message_id = str(mail.get("message_id") or "").strip()
        return {
            "message_id": message_id,
            "subject": subject,
            "text": text,
            "code": code,
            "related": related,
            "timestamp": self._extract_luckmail_timestamp(mail),
        }

    @staticmethod
    def _build_message_key(message_id: str, *, text: str, code: str = "") -> str:
        message_id = str(message_id or "").strip()
        code = str(code or "").strip()
        if not message_id:
            return ""
        if code:
            return f"{message_id}:code:{code}"
        digest = hashlib.sha1(str(text or "").encode("utf-8", errors="ignore")).hexdigest()[:16]
        return f"{message_id}:body:{digest}"

    @staticmethod
    def _format_luckmail_timestamp(ts: float | None) -> str:
        if not ts:
            return "-"
        try:
            return time.strftime("%H:%M:%S", time.localtime(float(ts)))
        except Exception:
            return "-"

    @staticmethod
    def _parse_luckmail_timestamp(value) -> float | None:
        if value in (None, ""):
            return None
        if isinstance(value, (int, float)):
            numeric = float(value)
            if numeric > 1e12:
                numeric /= 1000.0
            if numeric > 0:
                return numeric
            return None
        text = str(value or "").strip()
        if not text:
            return None
        if re.fullmatch(r"\d{10,16}", text):
            try:
                numeric = float(text)
                if numeric > 1e12:
                    numeric /= 1000.0
                return numeric
            except Exception:
                return None
        for candidate in (
            text,
            text.replace("Z", "+00:00"),
            text.replace("/", "-"),
        ):
            try:
                return datetime.fromisoformat(candidate).timestamp()
            except Exception:
                pass
        return None

    def _extract_luckmail_timestamp(self, mail: dict) -> float | None:
        for key in (
            "received_at",
            "receivedAt",
            "created_at",
            "createdAt",
            "updated_at",
            "updatedAt",
            "date",
            "sent_at",
            "sentAt",
            "timestamp",
            "time",
        ):
            parsed = self._parse_luckmail_timestamp(mail.get(key))
            if parsed:
                return parsed
        return None

    def _list_existing_mail_state(self, token: str) -> tuple[set[str], set[str]]:
        data = self._request("GET", f"/api/v1/openapi/email/token/{token}/mails")
        ids: set[str] = set()
        keys: set[str] = set()
        for mail in data.get("mails") or []:
            meta = self._extract_mail_metadata(mail if isinstance(mail, dict) else {})
            message_id = str(meta.get("message_id") or "").strip()
            if not message_id:
                continue
            ids.add(message_id)
            keys.add(
                self._build_message_key(
                    message_id,
                    text=str(meta.get("text") or ""),
                    code=str(meta.get("code") or ""),
                )
            )
        return ids, keys

    def _initialize_mail_state(self, token: str) -> None:
        self._seen_ids, self._seen_message_keys = self._list_existing_mail_state(token)
        self._logged_old_message_keys = set()

    def create_email(self, config=None):
        if self._fixed_account:
            email = str(self._fixed_account.get("email") or "").strip()
            token = str(self._fixed_account.get("token") or "").strip()
            if not email or not token:
                raise RuntimeError("LuckMail 绑定令牌缺失")
            self._email = email
            self._token = token
            self._initialize_mail_state(token)
            self._log(f"[LuckMail] 使用绑定令牌邮箱: {email}")
            return {
                "email": email,
                "token": token,
                "service_id": token,
                "account": {
                    "id": self._fixed_account.get("id"),
                    "email": email,
                    "token": token,
                },
            }

        if self.fixed_email:
            local_account = get_luckmail_token_account_by_email(self.fixed_email)
            if local_account and local_account.get("token"):
                email = str(local_account.get("email") or "").strip()
                token = str(local_account.get("token") or "").strip()
                self._email = email
                self._token = token
                self._initialize_mail_state(token)
                self._log(f"[LuckMail] 使用本地令牌邮箱: {email}")
                return {
                    "email": email,
                    "token": token,
                    "service_id": token,
                    "account": {
                        "id": local_account.get("id"),
                        "email": email,
                        "token": token,
                    },
                }
            existing = self._find_existing_purchase(self.fixed_email)
            if not existing:
                raise RuntimeError("LuckMail 固定邮箱模式下未找到对应已购邮箱和 token")
            email, token = existing
            self._email = email
            self._token = token
            self._initialize_mail_state(token)
            self._log(f"[LuckMail] 使用已购邮箱: {email}")
            return {"email": email, "token": token, "service_id": token}

        local_account = take_luckmail_token_account(preferred_email=None)
        if local_account and local_account.get("token"):
            email = str(local_account.get("email") or "").strip()
            token = str(local_account.get("token") or "").strip()
            self._email = email
            self._token = token
            self._initialize_mail_state(token)
            self._log(f"[LuckMail] 使用本地令牌邮箱: {email}")
            return {
                "email": email,
                "token": token,
                "service_id": token,
                "account": {
                    "id": local_account.get("id"),
                    "email": email,
                    "token": token,
                },
            }

        body = {"project_code": self.project_code, "quantity": 1}
        if self.email_type:
            body["email_type"] = self.email_type
        if self.domain:
            body["domain"] = self.domain
        data = self._request("POST", "/api/v1/openapi/email/purchase", json=body)
        purchases = data.get("purchases") or []
        if not purchases:
            raise RuntimeError(f"LuckMail 购买邮箱返回为空: {data}")
        item = purchases[0]
        email = str(item.get("email_address") or "").strip()
        token = str(item.get("token") or "").strip()
        if not email or not token:
            raise RuntimeError(f"LuckMail 返回缺少 email/token: {item}")
        self._email = email
        self._token = token
        self._initialize_mail_state(token)
        self._log(f"[LuckMail] 已购邮箱: {email}")
        return {"email": email, "token": token, "service_id": token}

    def _list_message_ids(self, token: str) -> set[str]:
        data = self._request("GET", f"/api/v1/openapi/email/token/{token}/mails")
        return {str(item.get("message_id")) for item in (data.get("mails") or []) if item.get("message_id")}

    def get_verification_code(self, email=None, timeout: int = 120, otp_sent_at=None, exclude_codes=None, **kwargs):
        if not self._token:
            raise RuntimeError("LuckMail token 不存在，无法获取验证码")
        interrupt_check = kwargs.get("interrupt_check")
        exclude_codes = {str(item) for item in (exclude_codes or set()) if item}
        deadline = time.monotonic() + max(int(timeout or 0), 1)
        while time.monotonic() < deadline:
            self._interrupt(interrupt_check)
            data = self._request("GET", f"/api/v1/openapi/email/token/{self._token}/mails")
            mails = list(data.get("mails") or [])
            self._log(f"[LuckMail] 已获取邮件列表: count={len(mails)}")
            for mail in mails:
                meta = self._extract_mail_metadata(mail if isinstance(mail, dict) else {})
                message_id = str(meta.get("message_id") or "").strip()
                subject = str(meta.get("subject") or "").strip()
                text = str(meta.get("text") or "")
                code = str(meta.get("code") or "").strip()
                related = bool(meta.get("related"))
                mail_ts = meta.get("timestamp")
                time_label = self._format_luckmail_timestamp(mail_ts if isinstance(mail_ts, (int, float)) else None)
                message_key = self._build_message_key(message_id, text=text, code=code)
                seen_id = bool(message_id and message_id in self._seen_ids)
                seen_key = bool(message_key and message_key in self._seen_message_keys)
                too_old_for_otp = bool(
                    otp_sent_at
                    and isinstance(mail_ts, (int, float))
                    and (float(mail_ts) + 30) < float(otp_sent_at)
                )
                if not message_id:
                    if code:
                        self._log(
                            f"[LuckMail] 邮件缺少 message_id: subject={self._subject_preview(subject)} code={code}"
                        )
                    elif related:
                        self._log(
                            f"[LuckMail] 邮件缺少 message_id: subject={self._subject_preview(subject)}"
                        )
                    continue
                if too_old_for_otp:
                    self._seen_ids.add(message_id)
                    if message_key:
                        self._seen_message_keys.add(message_key)
                    if message_key and message_key not in self._logged_old_message_keys:
                        if code:
                            self._log(
                                f"[LuckMail] 旧邮件(早于otp_sent_at): id={message_id} time={time_label} subject={self._subject_preview(subject)} code={code}"
                            )
                        else:
                            self._log(
                                f"[LuckMail] 旧邮件(早于otp_sent_at): id={message_id} time={time_label} subject={self._subject_preview(subject)}"
                            )
                        self._logged_old_message_keys.add(message_key)
                    continue
                if seen_key:
                    if message_key not in self._logged_old_message_keys:
                        if code:
                            self._log(
                                f"[LuckMail] 旧邮件: id={message_id} time={time_label} subject={self._subject_preview(subject)} code={code}"
                            )
                        else:
                            self._log(
                                f"[LuckMail] 旧邮件: id={message_id} time={time_label} subject={self._subject_preview(subject)}"
                            )
                        self._logged_old_message_keys.add(message_key)
                    continue
                self._seen_ids.add(message_id)
                if message_key:
                    self._seen_message_keys.add(message_key)
                if seen_id:
                    if code:
                        self._log(
                            f"[LuckMail] 邮件内容更新: id={message_id} time={time_label} subject={self._subject_preview(subject)} code={code}"
                        )
                    else:
                        self._log(
                            f"[LuckMail] 邮件内容更新: id={message_id} time={time_label} subject={self._subject_preview(subject)}"
                        )
                else:
                    if code:
                        self._log(
                            f"[LuckMail] 新邮件: id={message_id} time={time_label} subject={self._subject_preview(subject)} code={code}"
                        )
                    else:
                        self._log(
                            f"[LuckMail] 新邮件: id={message_id} time={time_label} subject={self._subject_preview(subject)}"
                        )
                if code:
                    self._log(
                        f"[LuckMail] 扫描到验证码: id={message_id} time={time_label} subject={self._subject_preview(subject)} code={code}"
                    )
                if code and code not in exclude_codes:
                    self._log(f"[LuckMail] 命中新验证码: {code}")
                    return code
                if code and code in exclude_codes:
                    self._log(f"[LuckMail] 跳过旧验证码: {code}")
                    continue
                if related:
                    self._log(f"[LuckMail] 发现相关邮件但未提取到验证码: {self._subject_preview(subject)}")
            self._sleep_interruptibly(4, interrupt_check)
        raise TimeoutError(f"LuckMail 等待验证码超时 ({timeout}s)")



class CloudMailProvider(ProviderBase):
    service_type = _ServiceType("cloud_mail")
    _shared_tokens: dict[str, tuple[str, float]] = {}
    _token_lock = threading.Lock()
    _shared_seen_ids: dict[str, set[str]] = {}
    _seen_ids_lock = threading.Lock()

    def __init__(
        self,
        *,
        base_url: str,
        admin_email: str,
        admin_password: str,
        domain="",
        subdomain: str = "",
        timeout: int | str = 30,
        fixed_account: dict | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.base_url = str(base_url or "").strip().rstrip("/")
        self.admin_email = str(admin_email or "").strip()
        self.admin_password = str(admin_password or "").strip()
        self.domain = domain
        self.subdomain = str(subdomain or "").strip().strip(".")
        self._fixed_account = dict(fixed_account or {}) if isinstance(fixed_account, dict) else None
        self._created_emails: dict[str, dict] = {}
        try:
            self.timeout = max(1, int(timeout or 30))
        except Exception:
            self.timeout = 30

        if not self.base_url:
            raise RuntimeError("CloudMail 未配置：请填写 cloud_mail_base_url")
        if not self.admin_email or not self.admin_password:
            raise RuntimeError("CloudMail 未配置：请填写 cloud_mail_admin_email 和 cloud_mail_admin_password")

    def _normalize_domains(self, value) -> list[str]:
        if isinstance(value, (list, tuple, set)):
            raw_items = [str(item or "").strip() for item in value]
        else:
            raw_items = re.split(r"[,\n\r]+", str(value or ""))
        domains: list[str] = []
        for item in raw_items:
            current = str(item or "").strip().lstrip("@").strip(".")
            if current:
                domains.append(current)
        return domains

    def _generate_token(self) -> str:
        response = tracked_request(
            requests.request,
            "POST",
            f"{self.base_url}/api/public/genToken",
            json={
                "email": self.admin_email,
                "password": self.admin_password,
            },
            proxies=self.proxies,
            timeout=self.timeout,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"CloudMail 生成 token 失败: HTTP {response.status_code}")
        payload = response.json() or {}
        if not isinstance(payload, dict):
            raise RuntimeError("CloudMail 生成 token 失败: 响应格式异常")
        if int(payload.get("code") or 0) != 200:
            raise RuntimeError(str(payload.get("message") or payload or "CloudMail 生成 token 失败"))
        token = str(((payload.get("data") or {}).get("token") if isinstance(payload.get("data"), dict) else "") or "").strip()
        if not token:
            raise RuntimeError("CloudMail 生成 token 失败: 未返回 token")
        return token

    def _get_token(self, *, force_refresh: bool = False) -> str:
        with self._token_lock:
            cached = self._shared_tokens.get(self.base_url)
            if not force_refresh and cached and time.time() < float(cached[1] or 0):
                return str(cached[0] or "")
            token = self._generate_token()
            self._shared_tokens[self.base_url] = (token, time.time() + 3600)
            return token

    def _request(self, method: str, path: str, *, retry_on_auth_error: bool = True, **kwargs):
        url = f"{self.base_url}{path}"
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            **(kwargs.pop("headers", {}) or {}),
        }
        headers["Authorization"] = self._get_token()
        response = tracked_request(
            requests.request,
            method,
            url,
            headers=headers,
            proxies=self.proxies,
            timeout=kwargs.pop("timeout", self.timeout),
            **kwargs,
        )
        if response.status_code == 401 and retry_on_auth_error:
            headers["Authorization"] = self._get_token(force_refresh=True)
            response = tracked_request(
                requests.request,
                method,
                url,
                headers=headers,
                proxies=self.proxies,
                timeout=kwargs.pop("timeout", self.timeout),
                **kwargs,
            )
        if response.status_code >= 400:
            raise RuntimeError(f"CloudMail 请求失败: HTTP {response.status_code}")
        payload = response.json() or {}
        if not isinstance(payload, dict):
            raise RuntimeError("CloudMail 请求失败: 响应格式异常")
        return payload

    def _pick_domain(self, domain: str = "", subdomain: str = "") -> str:
        selected = str(domain or "").strip().lstrip("@").strip(".")
        if not selected:
            candidates = self._normalize_domains(self.domain)
            if not candidates:
                raise RuntimeError("CloudMail 未配置：请填写 cloud_mail_domain")
            selected = random.choice(candidates)
        selected_subdomain = str(subdomain or self.subdomain or "").strip().strip(".")
        if selected_subdomain:
            return f"{selected_subdomain}.{selected}"
        return selected

    def _generate_email_address(self, prefix: str = "", domain: str = "", subdomain: str = "") -> str:
        local_part = str(prefix or "").strip()
        if not local_part:
            local_part = random.choice(string.ascii_lowercase) + "".join(
                random.choices(string.ascii_lowercase + string.digits, k=9)
            )
        return f"{local_part}@{self._pick_domain(domain=domain, subdomain=subdomain)}"

    @staticmethod
    def _generate_password(length: int = 12) -> str:
        alphabet = string.ascii_letters + string.digits
        return "".join(random.choices(alphabet, k=max(8, int(length or 12))))

    @staticmethod
    def _extract_message_timestamp(mail: dict) -> float | None:
        for key in ("sendTime", "createdAt", "created_at", "timestamp", "time", "date"):
            value = mail.get(key)
            if value in (None, ""):
                continue
            if isinstance(value, (int, float)):
                numeric = float(value)
                if numeric > 1e12:
                    numeric /= 1000.0
                if numeric > 0:
                    return numeric
            text = str(value or "").strip()
            if not text:
                continue
            if text.isdigit():
                try:
                    numeric = float(text)
                    if numeric > 1e12:
                        numeric /= 1000.0
                    if numeric > 0:
                        return numeric
                except Exception:
                    pass
            for candidate in (text, text.replace("Z", "+00:00"), text.replace("/", "-")):
                try:
                    return datetime.fromisoformat(candidate).timestamp()
                except Exception:
                    continue
        return None

    @staticmethod
    def _normalize_visible_mail_text(*parts: str) -> str:
        raw = " ".join(str(part or "") for part in parts if str(part or "").strip())
        raw = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", raw)
        raw = re.sub(r"(?is)<([a-z0-9]+)[^>]*display\s*:\s*none[^>]*>.*?</\1>", " ", raw)
        raw = re.sub(r"(?is)<([a-z0-9]+)[^>]*visibility\s*:\s*hidden[^>]*>.*?</\1>", " ", raw)
        text = ProviderBase._decode_raw_content(ProviderBase, raw)
        text = re.sub(r"\s+", " ", str(text or "")).strip()
        return text

    def _extract_cloudmail_code(self, mail: dict) -> str:
        subject = str(mail.get("subject") or "").strip()
        visible_text = self._normalize_visible_mail_text(
            str(mail.get("content") or ""),
            str(mail.get("text") or ""),
            str(mail.get("html") or ""),
        )

        lowered_meta = " ".join(
            [
                str(mail.get("sendEmail") or ""),
                str(mail.get("sendName") or ""),
                subject,
                visible_text,
            ]
        ).lower()
        if not any(keyword in lowered_meta for keyword in ("openai", "chatgpt", "verification code", "验证码")):
            return ""

        patterns = [
            r"(?is)(?:temporary\s+chatgpt\s+verification\s+code|chatgpt\s+verification\s+code|verification\s+code|login\s+code|security\s+code|authentication\s+code|验证码|驗證碼|認證碼)[^0-9]{0,80}(\d(?:[\s-]*\d){5})",
            r"(?is)\b(?:openai\s+)?code\b[^0-9]{0,80}(\d(?:[\s-]*\d){5})",
        ]
        for source_text in (visible_text, subject):
            if not source_text:
                continue
            for pattern in patterns:
                match = re.search(pattern, source_text)
                if not match:
                    continue
                code = re.sub(r"\D", "", str(match.group(1) or ""))
                if len(code) == 6:
                    return code
        return ""

    @staticmethod
    def _mail_debug_excerpt(mail: dict) -> str:
        subject = str(mail.get("subject") or "").strip()
        content = " ".join(
            [
                str(mail.get("content") or ""),
                str(mail.get("text") or ""),
                str(mail.get("html") or ""),
            ]
        )
        visible = re.sub(r"\s+", " ", CloudMailProvider._normalize_visible_mail_text(content)).strip()
        if len(visible) > 160:
            visible = visible[:157] + "..."
        return f"subject={subject!r} body={visible!r}"

    def create_email(self, config=None):
        request_config = dict(config or {})
        fixed_email = str(
            self.fixed_email
            or request_config.get("email")
            or ((self._fixed_account or {}).get("email") if isinstance(self._fixed_account, dict) else "")
            or ""
        ).strip()
        password = str(
            request_config.get("password")
            or ((self._fixed_account or {}).get("password") if isinstance(self._fixed_account, dict) else "")
            or self._generate_password()
        ).strip()
        email = fixed_email or self._generate_email_address(
            prefix=str(request_config.get("name") or "").strip(),
            domain=str(request_config.get("domain") or "").strip(),
            subdomain=str(request_config.get("subdomain") or "").strip(),
        )
        result = {
            "email": email,
            "service_id": email,
            "password": password,
            "account": {
                "email": email,
                "password": password,
            },
        }
        self._created_emails[email] = result
        self._log(f"[CloudMail] 已生成邮箱: {email}")
        return result

    def get_verification_code(self, email=None, timeout: int = 120, otp_sent_at=None, exclude_codes=None, **kwargs):
        target_email = str(email or "").strip()
        if not target_email:
            raise RuntimeError("CloudMail 邮箱为空")
        interrupt_check = kwargs.get("interrupt_check")
        exclude_codes = {str(item).strip() for item in (exclude_codes or set()) if str(item or "").strip()}

        with self._seen_ids_lock:
            initial_seen_ids = set(self._shared_seen_ids.get(target_email, set()))
            self._shared_seen_ids.setdefault(target_email, set())

        current_seen_ids: set[str] = set()
        deadline = time.monotonic() + max(int(timeout or 0), 1)

        while time.monotonic() < deadline:
            self._interrupt(interrupt_check)
            payload = self._request(
                "POST",
                "/api/public/emailList",
                json={
                    "toEmail": target_email,
                    "timeSort": "desc",
                },
            )
            if int(payload.get("code") or 0) != 200:
                self._sleep_interruptibly(3, interrupt_check)
                continue
            emails = payload.get("data") or []
            if not isinstance(emails, list):
                self._sleep_interruptibly(3, interrupt_check)
                continue

            for mail in emails:
                message_id = str(mail.get("emailId") or mail.get("id") or "").strip()
                if not message_id or message_id in initial_seen_ids or message_id in current_seen_ids:
                    continue

                mail_ts = self._extract_message_timestamp(mail)
                if otp_sent_at and mail_ts and mail_ts + 1 < float(otp_sent_at):
                    continue

                current_seen_ids.add(message_id)
                with self._seen_ids_lock:
                    self._shared_seen_ids.setdefault(target_email, set()).add(message_id)

                to_email = str(mail.get("toEmail") or "").strip().lower()
                if to_email and to_email != target_email.lower():
                    continue

                code = self._extract_cloudmail_code(mail)
                if not code or code in exclude_codes:
                    continue

                self._log(
                    f"[CloudMail] 命中验证码邮件: id={message_id} code={code} {self._mail_debug_excerpt(mail)}"
                )
                return code

            self._sleep_interruptibly(3, interrupt_check)

        raise TimeoutError(f"CloudMail 等待验证码超时 ({timeout}s)")


class OutlookLocalProvider(ProviderBase):
    service_type = _ServiceType("outlook_local")

    def __init__(
        self,
        *,
        imap_server: str = "",
        imap_port: int | str = 993,
        token_endpoint: str = "",
        fixed_account: dict | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._imap_servers: list[str] = []
        if imap_server:
            self._imap_servers.append(str(imap_server).strip())
        else:
            try:
                from platforms.chatgpt.constants import OUTLOOK_IMAP_SERVERS

                self._imap_servers.extend(
                    [
                        str(OUTLOOK_IMAP_SERVERS.get("NEW") or "").strip(),
                        str(OUTLOOK_IMAP_SERVERS.get("OLD") or "").strip(),
                    ]
                )
            except Exception:
                self._imap_servers.extend(["outlook.live.com", "outlook.office365.com"])
        self._imap_servers = [host for host in self._imap_servers if host]
        try:
            self._imap_port = int(imap_port or 993)
        except Exception:
            self._imap_port = 993
        self._token_endpoint = str(token_endpoint or "").strip()
        self._fixed_account = dict(fixed_account or {}) if isinstance(fixed_account, dict) else None
        self._account: dict | None = None
        self._seen_ids: set[str] = set()
        self._oauth_access_tokens: dict[str, str] = {}
        self._oauth_access_token_expires_at: dict[str, float] = {}
        self._last_oauth_error: str = ""
        self._last_provider_errors: dict[str, str] = {}
        self._preferred_provider: str = ""
        self._strict_provider_lock = False
        self._provider_health: dict[str, dict[str, float | int | str]] = {}
        self._provider_failure_threshold = 2
        self._provider_disable_seconds = 120
        self._provider_permanent_disable_seconds = 1800
        self._provider_order_with_oauth = ("graph_api", "imap_old", "imap_new")
        self._provider_order_without_oauth = ("password_imap",)
        self._mailboxes = (
            "INBOX",
            "Junk",
            '"Junk Email"',
            "Junk Email",
            "Spam",
            '"[Gmail]/Spam"',
            '"垃圾邮件"',
            "垃圾邮件",
            "Deleted Items",
            "Trash",
            "Archive",
        )
        self._graph_folders = (
            "inbox",
            "junkemail",
            "deleteditems",
            "archive",
        )

    @staticmethod
    def _is_service_abuse_error(error_text: str) -> bool:
        lowered = str(error_text or "").lower()
        return "service abuse mode" in lowered

    @staticmethod
    def _is_invalid_grant_error(error_text: str) -> bool:
        lowered = str(error_text or "").lower()
        return "invalid_grant" in lowered

    def _is_permanent_oauth_failure(self, provider_name: str, error_text: str) -> bool:
        lowered = str(error_text or "").lower()
        if self._is_service_abuse_error(lowered):
            return True
        if provider_name == "graph_api" and self._is_invalid_grant_error(lowered):
            return True
        return False

    def _lock_provider(self, provider_name: str, *, strict: bool = False) -> None:
        self._preferred_provider = str(provider_name or "").strip()
        if strict:
            self._strict_provider_lock = True

    def _provider_health_entry(self, provider_name: str) -> dict[str, float | int | str]:
        return self._provider_health.setdefault(
            str(provider_name or "").strip(),
            {
                "failures": 0,
                "disabled_until": 0.0,
                "last_error": "",
                "disable_logged_until": 0.0,
            },
        )

    def _is_provider_available(self, provider_name: str) -> bool:
        entry = self._provider_health_entry(provider_name)
        disabled_until = float(entry.get("disabled_until") or 0.0)
        if disabled_until <= time.time():
            if disabled_until > 0:
                entry["disabled_until"] = 0.0
                entry["failures"] = 0
                entry["disable_logged_until"] = 0.0
            return True
        if float(entry.get("disable_logged_until") or 0.0) != disabled_until:
            remaining = max(1, int(disabled_until - time.time()))
            self._log(f"[OutlookLocal] provider 暂时禁用: provider={provider_name} remain={remaining}s")
            entry["disable_logged_until"] = disabled_until
        return False

    def _record_provider_success(self, provider_name: str) -> None:
        entry = self._provider_health_entry(provider_name)
        entry["failures"] = 0
        entry["disabled_until"] = 0.0
        entry["last_error"] = ""
        entry["disable_logged_until"] = 0.0

    def _record_provider_failure(self, provider_name: str, error_text: str) -> None:
        entry = self._provider_health_entry(provider_name)
        entry["last_error"] = str(error_text or "")
        is_permanent = self._is_service_abuse_error(error_text)
        if is_permanent:
            entry["failures"] = self._provider_failure_threshold
            entry["disabled_until"] = time.time() + self._provider_permanent_disable_seconds
            entry["disable_logged_until"] = 0.0
            return
        failures = int(entry.get("failures") or 0) + 1
        entry["failures"] = failures
        if failures >= self._provider_failure_threshold:
            entry["disabled_until"] = time.time() + self._provider_disable_seconds
            entry["disable_logged_until"] = 0.0

    def _preflight_oauth_mailbox(self) -> None:
        if not self._account:
            return
        client_id = str(self._account.get("client_id") or "").strip()
        refresh_token = str(self._account.get("refresh_token") or "").strip()
        if not (client_id and refresh_token):
            self._seen_ids = self._list_current_ids()
            return
        try:
            messages = self._fetch_graph_messages(limit=5)
            self._seen_ids = {
                f"graph_api:{str(item.get('_folder') or 'graph')}:{str(item.get('id') or '').strip()}"
                for item in messages
                if str(item.get("id") or "").strip()
            }
            self._lock_provider("graph_api", strict=False)
            self._log(f"[OutlookLocal] Graph API 预检成功: count={len(messages)}，优先使用 graph_api")
        except Exception as exc:
            message = str(exc or "").strip()
            if self._is_permanent_oauth_failure("graph_api", message):
                raise RuntimeError(f"微软邮箱已失效: {message}")
            self._log(f"[OutlookLocal] Graph API 预检失败，继续尝试其他方式: {message}")
            self._seen_ids = self._list_current_ids()

    def create_email(self, config=None):
        if self._fixed_account:
            account = dict(self._fixed_account)
        elif self.fixed_email:
            account = get_outlook_account_by_email(self.fixed_email)
        else:
            account = take_outlook_account(preferred_email=None)
        if not account:
            if self.fixed_email:
                raise RuntimeError(f"本地微软邮箱池中不存在账号: {self.fixed_email}")
            raise RuntimeError("本地微软邮箱池为空，请先导入 Outlook 账号")

        if not account.get("password") and not (account.get("client_id") and account.get("refresh_token")):
            raise RuntimeError(f"微软邮箱记录缺少密码或 OAuth 令牌: {account.get('email')}")

        self._account = account
        self._seen_ids = set()
        self._last_provider_errors = {}
        self._strict_provider_lock = False
        self._preferred_provider = ""
        auth_desc = "OAuth" if account.get("client_id") and account.get("refresh_token") else "密码登录"
        self._log(f"[OutlookLocal] 已取出账号: {account.get('email')}（{auth_desc}）")
        self._log("[OutlookLocal] 已延迟邮箱预检，首次真实收码/取链接时再建立微软邮箱会话")
        return {
            "email": str(account.get("email") or "").strip(),
            "token": f"local-outlook-{account.get('id')}",
            "service_id": str(account.get("id") or ""),
            "account": {
                "id": account.get("id"),
                "email": str(account.get("email") or "").strip(),
                "password": str(account.get("password") or "").strip(),
                "client_id": str(account.get("client_id") or "").strip(),
                "refresh_token": str(account.get("refresh_token") or "").strip(),
            },
        }

    def _default_token_endpoints(self) -> dict[str, str]:
        if self._token_endpoint:
            endpoint = str(self._token_endpoint).strip()
            return {
                "consumers": endpoint,
                "live": endpoint,
                "common": endpoint,
            }
        try:
            from platforms.chatgpt.constants import MICROSOFT_TOKEN_ENDPOINTS

            return {
                "consumers": str(MICROSOFT_TOKEN_ENDPOINTS.get("CONSUMERS") or "").strip(),
                "live": str(MICROSOFT_TOKEN_ENDPOINTS.get("LIVE") or "").strip(),
                "common": str(MICROSOFT_TOKEN_ENDPOINTS.get("COMMON") or "").strip(),
            }
        except Exception:
            return {
                "consumers": "https://login.microsoftonline.com/consumers/oauth2/v2.0/token",
                "live": "https://login.live.com/oauth20_token.srf",
                "common": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
            }

    def _provider_attempts(self, provider_name: str) -> list[tuple[str, dict[str, str], str]]:
        endpoints = self._default_token_endpoints()
        try:
            from platforms.chatgpt.constants import MICROSOFT_SCOPES

            imap_scope = str(MICROSOFT_SCOPES.get("IMAP_NEW") or "").strip()
            graph_scope = str(MICROSOFT_SCOPES.get("GRAPH_API") or "").strip()
        except Exception:
            imap_scope = "https://outlook.office.com/IMAP.AccessAsUser.All offline_access"
            graph_scope = "https://graph.microsoft.com/.default"

        if provider_name == "imap_old":
            return [
                (endpoints.get("live") or "", {"scope": "wl.imap wl.offline_access"}, "live_wl_imap"),
                (endpoints.get("live") or "", {}, "live_default"),
            ]
        if provider_name == "imap_new":
            return [
                (endpoints.get("consumers") or "", {"scope": imap_scope}, "imap_scope"),
                (endpoints.get("consumers") or "", {}, "default"),
            ]
        if provider_name == "graph_api":
            return [
                (endpoints.get("common") or "", {"scope": graph_scope}, "graph_scope_common"),
                (endpoints.get("consumers") or "", {"scope": graph_scope}, "graph_scope_consumers"),
                (endpoints.get("common") or "", {}, "common_default"),
            ]
        return []

    def _fetch_oauth_token(
        self,
        *,
        provider_name: str,
        email: str,
        client_id: str,
        refresh_token: str,
        force_refresh: bool = False,
    ) -> str:
        if not client_id or not refresh_token:
            self._last_oauth_error = "缺少 client_id 或 refresh_token"
            return ""

        now = time.time()
        if force_refresh:
            self._oauth_access_tokens.pop(provider_name, None)
            self._oauth_access_token_expires_at.pop(provider_name, None)
        cached_token = self._oauth_access_tokens.get(provider_name, "")
        cached_expire_at = float(self._oauth_access_token_expires_at.get(provider_name) or 0.0)
        if cached_token and now < max(0.0, cached_expire_at - 120):
            return cached_token

        self._last_oauth_error = ""
        seen_keys: set[tuple[str, str]] = set()
        for endpoint, extra_payload, label in self._provider_attempts(provider_name):
            if not endpoint:
                continue
            cache_key = (endpoint, label)
            if cache_key in seen_keys:
                continue
            seen_keys.add(cache_key)
            payload = {
                "client_id": client_id,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
                **extra_payload,
            }
            self._log(f"[OutlookLocal] 刷新 access token: provider={provider_name} mode={label} endpoint={endpoint}")
            try:
                response = tracked_request(
                    requests.request,
                    "POST",
                    endpoint,
                    data=payload,
                    proxies=self.proxies,
                    timeout=20,
                )
            except Exception as exc:
                self._last_oauth_error = str(exc) or "请求失败"
                self._log(f"[OutlookLocal] access token 请求失败: provider={provider_name} mode={label} error={self._last_oauth_error}")
                continue

            body_preview = (response.text or "").strip().replace("\n", " ")[:220]
            if response.status_code >= 400:
                self._last_oauth_error = f"HTTP {response.status_code}: {body_preview or 'empty response'}"
                self._log(f"[OutlookLocal] access token 刷新失败: provider={provider_name} mode={label} {self._last_oauth_error}")
                continue

            try:
                data = response.json() if response.content else {}
            except Exception:
                data = {}
            access_token = str(data.get("access_token") or "").strip()
            if access_token:
                try:
                    expires_in = int(data.get("expires_in") or 3600)
                except Exception:
                    expires_in = 3600
                self._oauth_access_tokens[provider_name] = access_token
                self._oauth_access_token_expires_at[provider_name] = time.time() + max(300, expires_in)
                self._last_oauth_error = ""
                self._log(f"[OutlookLocal] 微软 access token 获取成功: {email} provider={provider_name} mode={label}")
                return access_token

            error_desc = str(data.get("error_description") or data.get("error") or body_preview or "empty response").strip()
            self._last_oauth_error = error_desc
            self._log(f"[OutlookLocal] access token 返回为空: provider={provider_name} mode={label} detail={error_desc}")
        return ""

    def _imap_auth_oauth(self, conn: imaplib.IMAP4_SSL, *, email: str, access_token: str) -> None:
        auth_string = f"user={email}\x01auth=Bearer {access_token}\x01\x01"
        conn.authenticate("XOAUTH2", lambda _: auth_string.encode("utf-8"))

    @staticmethod
    def _is_fatal_mail_auth_error(error_text: str) -> bool:
        lowered = str(error_text or "").lower()
        fatal_markers = (
            "basicauthblocked",
            "authenticate failed",
            "logondenied",
            "login failed",
            "oauth 刷新失败",
            "outlook oauth imap 登录失败",
            "invalid_grant",
            "interaction_required",
            "unauthorized_client",
            "aadsts",
            "authfailed",
            "xoauth2",
            "graph api 认证失败",
            "graph api token 获取失败",
        )
        return any(marker in lowered for marker in fatal_markers)

    @staticmethod
    def _should_retry_with_fresh_oauth_token(error_text: str) -> bool:
        lowered = str(error_text or "").lower()
        markers = (
            "authenticate failed",
            "authfailed",
            "invalid token",
            "expired",
            "expiredtoken",
            "oauthbearer",
            "xoauth2",
            "invalid audience",
        )
        return any(marker in lowered for marker in markers)

    def _read_provider_order(self, *, include_disabled: bool = False) -> tuple[str, ...]:
        if not self._account:
            return ()
        if self._account.get("client_id") and self._account.get("refresh_token"):
            order = list(self._provider_order_with_oauth)
        else:
            order = list(self._provider_order_without_oauth)
        if not include_disabled:
            order = [item for item in order if self._is_provider_available(item)]
        preferred = str(self._preferred_provider or "").strip()
        if preferred and preferred in order:
            if self._strict_provider_lock and not include_disabled:
                return (preferred,)
            return tuple([preferred, *[item for item in order if item != preferred]])
        return tuple(order)

    def _imap_host_for_provider(self, provider_name: str) -> str:
        if provider_name == "imap_old":
            return "outlook.office365.com"
        if provider_name == "imap_new":
            return "outlook.live.com"
        if provider_name == "password_imap":
            return self._imap_servers[0] if self._imap_servers else "outlook.office365.com"
        raise RuntimeError(f"未知 IMAP provider: {provider_name}")

    def _open_imap(self, provider_name: str) -> imaplib.IMAP4_SSL:
        if not self._account:
            raise RuntimeError("本地微软邮箱尚未初始化")

        email_addr = str(self._account.get("email") or "").strip()
        password = str(self._account.get("password") or "").strip()
        client_id = str(self._account.get("client_id") or "").strip()
        refresh_token = str(self._account.get("refresh_token") or "").strip()
        has_oauth = bool(client_id and refresh_token)
        host = self._imap_host_for_provider(provider_name)

        last_error: Exception | None = None
        if has_oauth and provider_name in {"imap_old", "imap_new"}:
            access_token = self._fetch_oauth_token(
                provider_name=provider_name,
                email=email_addr,
                client_id=client_id,
                refresh_token=refresh_token,
            )
            if not access_token:
                raise RuntimeError(
                    f"{provider_name} 微软 OAuth 刷新失败: {self._last_oauth_error or '未返回 access token'}"
                )

            refreshed_once = False
            while True:
                conn = None
                try:
                    self._log(f"[OutlookLocal] 尝试 OAuth IMAP 登录: provider={provider_name} host={host} email={email_addr}")
                    conn = imaplib.IMAP4_SSL(host, self._imap_port, timeout=30)
                    self._imap_auth_oauth(conn, email=email_addr, access_token=access_token)
                    self._log(f"[OutlookLocal] OAuth IMAP 登录成功: provider={provider_name} host={host} email={email_addr}")
                    return conn
                except Exception as exc:
                    last_error = exc
                    error_text = str(exc)
                    self._log(f"[OutlookLocal] OAuth IMAP 登录失败: provider={provider_name} host={host} error={error_text}")
                    try:
                        if conn:
                            conn.logout()
                    except Exception:
                        pass
                    if (not refreshed_once) and self._should_retry_with_fresh_oauth_token(error_text):
                        refreshed_once = True
                        access_token = self._fetch_oauth_token(
                            provider_name=provider_name,
                            email=email_addr,
                            client_id=client_id,
                            refresh_token=refresh_token,
                            force_refresh=True,
                        )
                        if access_token:
                            self._log(
                                f"[OutlookLocal] 已刷新 access token，重试 OAuth IMAP 登录: provider={provider_name} host={host}"
                            )
                            continue
                    break
            raise RuntimeError(f"{provider_name} OAuth IMAP 登录失败: {last_error}")

        if password and provider_name == "password_imap":
            conn = None
            try:
                self._log(f"[OutlookLocal] 尝试密码 IMAP 登录: host={host} email={email_addr}")
                conn = imaplib.IMAP4_SSL(host, self._imap_port, timeout=30)
                conn.login(email_addr, password)
                self._log(f"[OutlookLocal] 密码 IMAP 登录成功: host={host} email={email_addr}")
                return conn
            except Exception as exc:
                last_error = exc
                self._log(f"[OutlookLocal] 密码 IMAP 登录失败: host={host} error={exc}")
                try:
                    if conn:
                        conn.logout()
                except Exception:
                    pass

        raise RuntimeError(f"{provider_name} Outlook IMAP 登录失败: {last_error}")

    def _decode_header_value(self, value: str) -> str:
        if not value:
            return ""
        decoded: list[str] = []
        for part, charset in decode_header(value):
            if isinstance(part, bytes):
                try:
                    decoded.append(part.decode(charset or "utf-8", errors="ignore"))
                except Exception:
                    decoded.append(part.decode("utf-8", errors="ignore"))
            else:
                decoded.append(str(part))
        return "".join(decoded)

    def _message_sent_at(self, message) -> float | None:
        try:
            return parsedate_to_datetime(message.get("Date") or "").timestamp()
        except Exception:
            return None

    def _extract_message_text(self, message) -> str:
        subject = self._decode_header_value(message.get("Subject", ""))
        body_chunks: list[str] = []
        if message.is_multipart():
            for part in message.walk():
                if part.get_content_maintype() == "multipart":
                    continue
                if part.get_content_type() not in {"text/plain", "text/html"}:
                    continue
                payload = part.get_payload(decode=True)
                if payload is None:
                    continue
                charset = part.get_content_charset() or "utf-8"
                try:
                    body_chunks.append(payload.decode(charset, errors="ignore"))
                except Exception:
                    body_chunks.append(payload.decode("utf-8", errors="ignore"))
        else:
            payload = message.get_payload(decode=True)
            if payload is None:
                payload = message.get_payload()
            if isinstance(payload, bytes):
                try:
                    body_chunks.append(payload.decode("utf-8", errors="ignore"))
                except Exception:
                    body_chunks.append(payload.decode("latin1", errors="ignore"))
            elif payload:
                body_chunks.append(str(payload))
        return self._decode_raw_content((subject + " " + " ".join(body_chunks)).strip())

    def _extract_message_raw_text(self, message) -> str:
        subject = self._decode_header_value(message.get("Subject", ""))
        body_chunks: list[str] = []
        if message.is_multipart():
            for part in message.walk():
                if part.get_content_maintype() == "multipart":
                    continue
                if part.get_content_type() not in {"text/plain", "text/html"}:
                    continue
                payload = part.get_payload(decode=True)
                if payload is None:
                    continue
                charset = part.get_content_charset() or "utf-8"
                try:
                    body_chunks.append(payload.decode(charset, errors="ignore"))
                except Exception:
                    body_chunks.append(payload.decode("utf-8", errors="ignore"))
        else:
            payload = message.get_payload(decode=True)
            if payload is None:
                payload = message.get_payload()
            if isinstance(payload, bytes):
                try:
                    body_chunks.append(payload.decode("utf-8", errors="ignore"))
                except Exception:
                    body_chunks.append(payload.decode("latin1", errors="ignore"))
            elif payload:
                body_chunks.append(str(payload))
        return f"{subject}\n" + "\n".join(body_chunks)

    def _select_mailbox(self, conn: imaplib.IMAP4_SSL, mailbox: str) -> bool:
        try:
            status, _ = conn.select(mailbox, readonly=True)
            return status == "OK"
        except Exception:
            return False

    def _subject_preview(self, subject: str) -> str:
        text = re.sub(r"\s+", " ", str(subject or "")).strip()
        if len(text) <= 80:
            return text or "(无主题)"
        return f"{text[:77]}..."

    def _is_recent_timestamp(self, sent_at: float | None, otp_sent_at) -> bool:
        if not otp_sent_at:
            return True
        if sent_at is None:
            return True
        return float(sent_at) + 300 >= float(otp_sent_at)

    def _extract_header_from_raw_text(self, raw_text: str, header_name: str) -> str:
        lines = str(raw_text or "").splitlines()
        target = f"{str(header_name or '').strip().lower()}:"
        collecting = False
        parts: list[str] = []
        for line in lines:
            if not collecting:
                if line.lower().startswith(target):
                    parts.append(line.split(":", 1)[1].strip())
                    collecting = True
                continue
            if line.startswith((" ", "\t")):
                parts.append(line.strip())
                continue
            break
        return " ".join(parts).strip()

    def _extract_message_bundle_from_raw(self, raw: bytes) -> tuple[str, str, str, float | None]:
        raw_text = raw.decode("utf-8", errors="ignore") if isinstance(raw, bytes) else str(raw or "")
        subject = self._decode_header_value(self._extract_header_from_raw_text(raw_text, "Subject"))
        from_header = self._decode_header_value(self._extract_header_from_raw_text(raw_text, "From")).lower()
        try:
            sent_at = parsedate_to_datetime(self._extract_header_from_raw_text(raw_text, "Date") or "").timestamp()
        except Exception:
            sent_at = None
        text = self._decode_raw_content(raw_text)

        try:
            msg = message_from_bytes(raw, policy=email_default_policy)
            subject = self._decode_header_value(msg.get("Subject", "")) or subject
            from_header = self._decode_header_value(msg.get("From", "")).lower() or from_header
            sent_at = self._message_sent_at(msg) or sent_at
            parsed_text = self._extract_message_text(msg)
            if parsed_text:
                text = parsed_text
        except Exception as exc:
            self._log(f"[OutlookLocal] MIME 解析回退: {exc}")

        return subject, text, from_header, sent_at

    def _is_recent_message(self, message, otp_sent_at) -> bool:
        sent_at = self._message_sent_at(message)
        return self._is_recent_timestamp(sent_at, otp_sent_at)

    def _is_openai_related(self, subject: str, text: str, sender: str) -> bool:
        lowered = f"{subject} {text} {sender}".lower()
        return any(
            keyword in lowered
            for keyword in (
                "openai",
                "open ai",
                "chatgpt",
                "codex",
                "verification code",
                "your openai code",
                "authentication code",
                "security code",
                "login code",
                "sign in",
                "验证码",
                "校验码",
                "动态码",
            )
        )

    def _parse_graph_timestamp(self, value: str) -> float | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
        except Exception:
            return None

    def _fetch_graph_messages(self, *, otp_sent_at=None, limit: int = 20) -> list[dict]:
        if not self._account:
            raise RuntimeError("本地微软邮箱尚未初始化")
        email_addr = str(self._account.get("email") or "").strip()
        client_id = str(self._account.get("client_id") or "").strip()
        refresh_token = str(self._account.get("refresh_token") or "").strip()
        access_token = self._fetch_oauth_token(
            provider_name="graph_api",
            email=email_addr,
            client_id=client_id,
            refresh_token=refresh_token,
        )
        if not access_token:
            raise RuntimeError(f"Graph API token 获取失败: {self._last_oauth_error or '未返回 access token'}")

        messages: list[dict] = []
        for folder in self._graph_folders:
            params = {
                "$top": max(1, int(limit or 20)),
                "$select": "id,subject,from,toRecipients,receivedDateTime,isRead,bodyPreview,body",
                "$orderby": "receivedDateTime desc",
            }
            try:
                response = tracked_request(
                    requests.request,
                    "GET",
                    f"https://graph.microsoft.com/v1.0/me/mailFolders/{folder}/messages",
                    params=params,
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Accept": "application/json",
                        "Prefer": "outlook.body-content-type='text'",
                    },
                    proxies=self.proxies,
                    timeout=20,
                )
            except Exception as exc:
                self._log(f"[OutlookLocal] Graph API 请求失败: folder={folder} error={exc}")
                continue

            if response.status_code in {401, 403}:
                body_preview = (response.text or "").strip().replace("\n", " ")[:220]
                self._oauth_access_tokens.pop("graph_api", None)
                self._oauth_access_token_expires_at.pop("graph_api", None)
                raise RuntimeError(f"Graph API 认证失败: HTTP {response.status_code}: {body_preview}")
            if response.status_code >= 400:
                self._log(f"[OutlookLocal] Graph API 跳过文件夹: folder={folder} http={response.status_code}")
                continue
            try:
                payload = response.json() if response.content else {}
            except Exception:
                payload = {}
            for item in payload.get("value") or []:
                item = dict(item or {})
                item["_folder"] = folder
                messages.append(item)
        return messages

    def _extract_graph_message_text(self, item: dict) -> str:
        subject = str(item.get("subject") or "")
        body_info = item.get("body") or {}
        body_text = str(body_info.get("content") or "")
        preview = str(item.get("bodyPreview") or "")
        return self._decode_raw_content(f"{subject} {preview} {body_text}".strip())

    def _extract_graph_message_raw_text(self, item: dict) -> str:
        subject = str(item.get("subject") or "")
        body_info = item.get("body") or {}
        body_text = str(body_info.get("content") or "")
        preview = str(item.get("bodyPreview") or "")
        return f"{subject}\n{preview}\n{body_text}"

    def _scan_graph_for_code(self, *, otp_sent_at=None, exclude_codes: set[str], limit: int = 20) -> str:
        messages = self._fetch_graph_messages(otp_sent_at=otp_sent_at, limit=limit)
        messages.sort(key=lambda item: self._parse_graph_timestamp(str(item.get("receivedDateTime") or "")) or 0, reverse=True)
        for item in messages:
            message_id = str(item.get("id") or "").strip()
            folder = str(item.get("_folder") or "graph")
            if not message_id:
                continue
            seen_key = f"graph_api:{folder}:{message_id}"
            if seen_key in self._seen_ids:
                continue
            received_ts = self._parse_graph_timestamp(str(item.get("receivedDateTime") or ""))
            if otp_sent_at and received_ts and received_ts + 300 < float(otp_sent_at):
                continue
            self._seen_ids.add(seen_key)
            from_info = (((item.get("from") or {}).get("emailAddress") or {}).get("address") or "")
            subject = str(item.get("subject") or "")
            text = self._extract_graph_message_text(item)
            code = self._extract_code(text)
            if code:
                self._log(
                    f"[OutlookLocal] 扫描到验证码: provider=graph_api mailbox={folder} "
                    f"subject={self._subject_preview(subject)} code={code}"
                )
            if code and code not in exclude_codes:
                self._lock_provider("graph_api", strict=True)
                self._log("[OutlookLocal] 已锁定收码方式: graph_api")
                self._log(f"[OutlookLocal] 命中新验证码: {code}")
                return code
            if code and code in exclude_codes:
                self._log(f"[OutlookLocal] 跳过旧验证码: {code}")
                continue
            if self._is_openai_related(subject, text, from_info):
                self._log(f"[OutlookLocal] 在 {folder} 发现相关邮件但未提取到验证码: {self._subject_preview(subject)}")
        return ""

    def _scan_imap_for_code(self, provider_name: str, *, otp_sent_at=None, exclude_codes: set[str]) -> str:
        conn = None
        try:
            conn = self._open_imap(provider_name)
            for mailbox in self._mailboxes:
                if not self._select_mailbox(conn, mailbox):
                    continue
                status, data = conn.uid("search", None, "ALL")
                if status != "OK":
                    self._log(f"[OutlookLocal] IMAP 搜索失败: provider={provider_name} mailbox={mailbox} status={status}")
                    continue
                ids = data[0].split() if data and data[0] else []
                total_ids = len(ids)
                if len(ids) > 50:
                    ids = ids[-50:]
                self._log(f"[OutlookLocal] IMAP 扫描邮箱: provider={provider_name} mailbox={mailbox} total={total_ids} scanning={len(ids)}")
                for uid in reversed(ids):
                    uid_text = uid.decode("utf-8", errors="ignore") if isinstance(uid, bytes) else str(uid)
                    seen_key = f"{provider_name}:{mailbox}:{uid_text}"
                    if not uid_text or seen_key in self._seen_ids:
                        continue
                    self._seen_ids.add(seen_key)
                    status, msg_data = conn.uid("fetch", uid, "(RFC822)")
                    if status != "OK":
                        self._log(f"[OutlookLocal] IMAP 拉取邮件失败: provider={provider_name} mailbox={mailbox} uid={uid_text} status={status}")
                        continue
                    raw = None
                    for item in msg_data or []:
                        if isinstance(item, tuple) and item[1]:
                            raw = item[1]
                            break
                    if not raw:
                        self._log(f"[OutlookLocal] IMAP 邮件为空: provider={provider_name} mailbox={mailbox} uid={uid_text}")
                        continue
                    try:
                        subject, text, from_header, sent_at = self._extract_message_bundle_from_raw(raw)
                        self._log(
                            f"[OutlookLocal] IMAP 邮件摘要: provider={provider_name} mailbox={mailbox} uid={uid_text} from={from_header or '-'} subject={self._subject_preview(subject)} sent_at={time.strftime('%H:%M:%S', time.localtime(sent_at)) if sent_at else '-'}"
                        )
                        if not self._is_recent_timestamp(sent_at, otp_sent_at):
                            preview = self._subject_preview(subject)
                            sent_label = (
                                f" ({time.strftime('%H:%M:%S', time.localtime(sent_at))})"
                                if sent_at
                                else ""
                            )
                            self._log(f"[OutlookLocal] 跳过旧邮件: {preview}{sent_label}")
                            continue
                        code = self._extract_code(text)
                        if code:
                            self._log(
                                f"[OutlookLocal] 扫描到验证码: provider={provider_name} mailbox={mailbox} uid={uid_text} subject={self._subject_preview(subject)} code={code}"
                            )
                        if code and code not in exclude_codes:
                            self._lock_provider(provider_name, strict=True)
                            self._log(f"[OutlookLocal] 已锁定收码方式: {provider_name}")
                            self._log(f"[OutlookLocal] 命中新验证码: {code}")
                            return code
                        if code and code in exclude_codes:
                            self._log(f"[OutlookLocal] 跳过旧验证码: {code}")
                            continue
                        if self._is_openai_related(subject, text, from_header):
                            self._log(
                                f"[OutlookLocal] 发现 OpenAI/Codex 相关邮件但未提取到验证码: provider={provider_name} mailbox={mailbox} uid={uid_text} subject={self._subject_preview(subject)}"
                            )
                    except Exception as exc:
                        trace = traceback.format_exc(limit=3).strip().replace("\n", " | ")
                        raw_preview = (raw[:240] if isinstance(raw, (bytes, bytearray)) else str(raw or "")[:240])
                        self._log(
                            f"[OutlookLocal] IMAP 邮件解析异常: provider={provider_name} mailbox={mailbox} uid={uid_text} error={exc!r} trace={trace} raw_preview={raw_preview}"
                        )
                        continue
            return ""
        finally:
            try:
                if conn:
                    conn.logout()
            except Exception:
                pass
    def _list_current_ids(self) -> set[str]:
        seen: set[str] = set()
        for provider_name in self._read_provider_order():
            if provider_name == "graph_api":
                try:
                    for item in self._fetch_graph_messages(limit=30):
                        message_id = str(item.get("id") or "").strip()
                        folder = str(item.get("_folder") or "graph")
                        if message_id:
                            seen.add(f"graph_api:{folder}:{message_id}")
                except Exception:
                    continue
                continue
            conn = None
            try:
                conn = self._open_imap(provider_name)
                for mailbox in self._mailboxes:
                    if not self._select_mailbox(conn, mailbox):
                        continue
                    status, data = conn.uid("search", None, "ALL")
                    if status != "OK":
                        continue
                    ids = data[0].split() if data and data[0] else []
                    for uid in ids:
                        uid_text = uid.decode("utf-8", errors="ignore") if isinstance(uid, bytes) else str(uid)
                        if uid_text:
                            seen.add(f"{provider_name}:{mailbox}:{uid_text}")
            except Exception:
                continue
            finally:
                try:
                    if conn:
                        conn.logout()
                except Exception:
                    pass

    def _scan_graph_for_invitation_link(self, *, invite_sent_at=None, limit: int = 20) -> str:
        messages = self._fetch_graph_messages(otp_sent_at=invite_sent_at, limit=limit)
        messages.sort(key=lambda item: self._parse_graph_timestamp(str(item.get("receivedDateTime") or "")) or 0, reverse=True)
        for item in messages:
            subject = str(item.get("subject") or "")
            raw_text = self._extract_graph_message_raw_text(item)
            urls = extract_invite_urls(raw_text)
            if not urls:
                continue
            for url in urls:
                try:
                    session = requests.Session()
                    session.proxies = self.proxies
                    resolved = resolve_invite_link(url, session=session, timeout=20)
                except Exception:
                    resolved = url
                lowered = str(resolved or "").lower()
                if "chatgpt.com/auth/login" in lowered and ("accept_wid=" in lowered or "inv_ws_name=" in lowered):
                    self._log(f"[OutlookLocal] 命中邀请链接: provider=graph_api subject={self._subject_preview(subject)} url={resolved[:200]}")
                    self._lock_provider("graph_api", strict=True)
                    return resolved
        return ""

    def _scan_imap_for_invitation_link(self, provider_name: str, *, invite_sent_at=None) -> str:
        conn = None
        try:
            conn = self._open_imap(provider_name)
            for mailbox in self._mailboxes:
                if not self._select_mailbox(conn, mailbox):
                    continue
                status, data = conn.uid("search", None, "ALL")
                if status != "OK":
                    continue
                ids = data[0].split() if data and data[0] else []
                if len(ids) > 50:
                    ids = ids[-50:]
                for uid in reversed(ids):
                    status, msg_data = conn.uid("fetch", uid, "(RFC822)")
                    if status != "OK":
                        continue
                    raw = None
                    for item in msg_data or []:
                        if isinstance(item, tuple) and item[1]:
                            raw = item[1]
                            break
                    if not raw:
                        continue
                    subject, raw_text, _from_header, sent_at = self._extract_message_bundle_from_raw(raw)
                    if not self._is_recent_timestamp(sent_at, invite_sent_at):
                        continue
                    urls = extract_invite_urls(raw_text)
                    if not urls:
                        continue
                    for url in urls:
                        try:
                            session = requests.Session()
                            session.proxies = self.proxies
                            resolved = resolve_invite_link(url, session=session, timeout=20)
                        except Exception:
                            resolved = url
                        lowered = str(resolved or "").lower()
                        if "chatgpt.com/auth/login" in lowered and ("accept_wid=" in lowered or "inv_ws_name=" in lowered):
                            self._log(f"[OutlookLocal] 命中邀请链接: provider={provider_name} mailbox={mailbox} subject={self._subject_preview(subject)} url={resolved[:200]}")
                            self._lock_provider(provider_name, strict=True)
                            return resolved
            return ""
        finally:
            try:
                if conn:
                    conn.logout()
            except Exception:
                pass
        return seen

    def _log_recent_code_snapshot(
        self,
        *,
        exclude_codes: set[str],
        otp_sent_at=None,
        limit: int = 8,
    ) -> str:
        seen_logs = 0
        fallback_code = ""
        provider_names = self._read_provider_order(include_disabled=True)
        if not provider_names:
            self._log("[OutlookLocal] 最近验证码回溯：没有可用 provider 定义")
            return ""
        for provider_name in provider_names:
            try:
                if provider_name == "graph_api":
                    messages = self._fetch_graph_messages(otp_sent_at=otp_sent_at, limit=limit)
                    messages.sort(
                        key=lambda item: self._parse_graph_timestamp(str(item.get("receivedDateTime") or "")) or 0,
                        reverse=True,
                    )
                    for item in messages[: max(int(limit or 0), 1)]:
                        subject = str(item.get("subject") or "")
                        text = self._extract_graph_message_text(item)
                        code = self._extract_code(text)
                        if not code:
                            continue
                        status_text = "已尝试" if code in exclude_codes else "未使用"
                        self._log(
                            f"[OutlookLocal] 最近验证码: provider=graph_api mailbox={item.get('_folder') or 'graph'} "
                            f"subject={self._subject_preview(subject)} code={code} status={status_text}"
                        )
                        if not fallback_code and code not in exclude_codes:
                            fallback_code = code
                        seen_logs += 1
                        if seen_logs >= max(int(limit or 0), 1):
                            return fallback_code
                    continue

                conn = self._open_imap(provider_name)
                try:
                    for mailbox in self._mailboxes:
                        if not self._select_mailbox(conn, mailbox):
                            continue
                        status, data = conn.uid("search", None, "ALL")
                        if status != "OK":
                            continue
                        ids = data[0].split() if data and data[0] else []
                        if not ids:
                            continue
                        for uid in reversed(ids[-max(int(limit or 0), 1):]):
                            uid_text = uid.decode("utf-8", errors="ignore") if isinstance(uid, bytes) else str(uid)
                            if not uid_text:
                                continue
                            status, msg_data = conn.uid("fetch", uid, "(RFC822)")
                            if status != "OK":
                                continue
                            raw = None
                            for item in msg_data or []:
                                if isinstance(item, tuple) and item[1]:
                                    raw = item[1]
                                    break
                            if not raw:
                                continue
                            subject, text, _from_header, sent_at = self._extract_message_bundle_from_raw(raw)
                            if not self._is_recent_timestamp(sent_at, otp_sent_at):
                                continue
                            code = self._extract_code(text)
                            if not code:
                                continue
                            status_text = "已尝试" if code in exclude_codes else "未使用"
                            self._log(
                                f"[OutlookLocal] 最近验证码: provider={provider_name} mailbox={mailbox} "
                                f"subject={self._subject_preview(subject)} code={code} status={status_text}"
                            )
                            if not fallback_code and code not in exclude_codes:
                                fallback_code = code
                            seen_logs += 1
                            if seen_logs >= max(int(limit or 0), 1):
                                return fallback_code
                finally:
                    try:
                        conn.logout()
                    except Exception:
                        pass
            except Exception as exc:
                self._log(f"[OutlookLocal] 最近验证码回溯失败: provider={provider_name} error={exc}")
        if seen_logs == 0:
            self._log("[OutlookLocal] 最近未发现可提取的验证码")
        elif fallback_code:
            self._log(f"[OutlookLocal] 最近验证码回溯命中未使用验证码，将直接返回: {fallback_code}")
        return fallback_code

    def get_verification_code(self, email=None, timeout: int = 120, otp_sent_at=None, exclude_codes=None, **kwargs):
        if not self._account:
            raise RuntimeError("本地微软邮箱尚未创建")

        interrupt_check = kwargs.get("interrupt_check")
        exclude_codes = {str(item).strip() for item in (exclude_codes or set()) if str(item or "").strip()}
        deadline = time.monotonic() + max(int(timeout or 0), 1)
        provider_order = self._read_provider_order()
        provider_order_all = self._read_provider_order(include_disabled=True)
        fixed_preferred_provider = str(self._preferred_provider or "").strip()

        try:
            started_at = time.monotonic()
            last_progress_log = 0
            while time.monotonic() < deadline:
                self._interrupt(interrupt_check)
                elapsed_now = int(time.monotonic() - started_at)
                current_order = (fixed_preferred_provider,) if fixed_preferred_provider else provider_order
                if elapsed_now >= last_progress_log + 10:
                    last_progress_log = elapsed_now
                    self._log(
                        f"[OutlookLocal] 等待验证码进度: elapsed={elapsed_now}s timeout={int(timeout or 0)}s providers={','.join(current_order) if current_order else '-'}"
                    )
                poll_had_success = False
                fatal_errors: list[str] = []
                current_order = (fixed_preferred_provider,) if fixed_preferred_provider else provider_order
                if not current_order and provider_order_all:
                    current_order = provider_order_all
                    self._log(
                        f"[OutlookLocal] 所有 provider 目前均处于禁用窗口，强制调试扫描: providers={','.join(current_order)}"
                    )
                for provider_name in current_order:
                    self._interrupt(interrupt_check)
                    try:
                        if provider_name == "graph_api":
                            code = self._scan_graph_for_code(otp_sent_at=otp_sent_at, exclude_codes=exclude_codes)
                        else:
                            code = self._scan_imap_for_code(provider_name, otp_sent_at=otp_sent_at, exclude_codes=exclude_codes)
                        poll_had_success = True
                        self._record_provider_success(provider_name)
                        self._last_provider_errors.pop(provider_name, None)
                        if code:
                            return code
                    except Exception as exc:
                        error_text = str(exc)
                        trace = traceback.format_exc(limit=3).strip().replace("\n", " | ")
                        self._last_provider_errors[provider_name] = error_text
                        self._record_provider_failure(provider_name, error_text)
                        self._log(
                            f"[OutlookLocal] provider 失败: provider={provider_name} error={error_text} trace={trace}"
                        )
                        if self._is_fatal_mail_auth_error(error_text):
                            fatal_errors.append(f"{provider_name}: {error_text}")
                        continue

                if not current_order:
                    self._sleep_interruptibly(1, interrupt_check)
                    continue
                if fatal_errors and len(fatal_errors) == len(current_order) and not poll_had_success:
                    raise RuntimeError(" ; ".join(fatal_errors))
                self._sleep_interruptibly(1, interrupt_check)
        finally:
            pass

        fallback_code = self._log_recent_code_snapshot(exclude_codes=exclude_codes, otp_sent_at=otp_sent_at)
        if fallback_code and fallback_code not in exclude_codes:
            self._log(f"[OutlookLocal] 超时回溯后返回未使用验证码: {fallback_code}")
            return fallback_code
        raise TimeoutError(f"Outlook 本地邮箱等待验证码超时 ({timeout}s)")

    def get_invitation_link(self, email=None, timeout: int = 180, invite_sent_at=None, **kwargs):
        if not self._account:
            raise RuntimeError("本地微软邮箱尚未创建")

        interrupt_check = kwargs.get("interrupt_check")
        deadline = time.monotonic() + max(int(timeout or 0), 1)
        provider_order = self._read_provider_order()
        fixed_preferred_provider = str(self._preferred_provider or "").strip()
        started_at = time.monotonic()
        last_progress_log = 0

        while time.monotonic() < deadline:
            self._interrupt(interrupt_check)
            elapsed_now = int(time.monotonic() - started_at)
            if elapsed_now >= last_progress_log + 10:
                last_progress_log = elapsed_now
                self._log(f"[OutlookLocal] 等待邀请链接进度: elapsed={elapsed_now}s timeout={int(timeout or 0)}s")
            current_order = (fixed_preferred_provider,) if fixed_preferred_provider else provider_order
            for provider_name in current_order:
                self._interrupt(interrupt_check)
                try:
                    if provider_name == "graph_api":
                        invite_link = self._scan_graph_for_invitation_link(invite_sent_at=invite_sent_at)
                    else:
                        invite_link = self._scan_imap_for_invitation_link(provider_name, invite_sent_at=invite_sent_at)
                    if invite_link:
                        return invite_link
                except Exception as exc:
                    self._log(f"[OutlookLocal] 扫描邀请链接失败: provider={provider_name} error={exc}")
                    continue
            self._sleep_interruptibly(1, interrupt_check)

        raise TimeoutError(f"Outlook 本地邮箱等待邀请链接超时 ({timeout}s)")


def build_mail_provider(
    provider: str,
    *,
    config: dict,
    proxy: str | None = None,
    fixed_email: str | None = None,
    log_fn=None,
):
    name = str(provider or "luckmail").strip().lower()
    if name == "tempmail_lol":
        return TempMailLolProvider(
            api_base=str(config.get("tempmail_api_base") or "https://api.tempmail.lol/v2"),
            proxy=proxy,
            log_fn=log_fn,
            fixed_email=fixed_email,
        )
    if name == "luckmail":
        return LuckMailProvider(
            base_url=str(config.get("luckmail_base_url") or "https://mails.luckyous.com/"),
            api_key=str(config.get("luckmail_api_key") or ""),
            project_code="openai",
            email_type=str(config.get("luckmail_email_type") or ""),
            domain=str(config.get("luckmail_domain") or ""),
            fixed_account=(config.get("retry_email_binding") if isinstance(config.get("retry_email_binding"), dict) else None),
            proxy=proxy,
            log_fn=log_fn,
            fixed_email=fixed_email,
        )
    if name == "cloud_mail":
        return CloudMailProvider(
            base_url=str(config.get("cloud_mail_base_url") or ""),
            admin_email=str(config.get("cloud_mail_admin_email") or ""),
            admin_password=str(config.get("cloud_mail_admin_password") or ""),
            domain=config.get("cloud_mail_domain") or "",
            subdomain=str(config.get("cloud_mail_subdomain") or ""),
            timeout=config.get("cloud_mail_timeout") or 30,
            fixed_account=(config.get("retry_email_binding") if isinstance(config.get("retry_email_binding"), dict) else None),
            proxy=proxy,
            log_fn=log_fn,
            fixed_email=fixed_email,
        )
    if name == "outlook_local":
        return OutlookLocalProvider(
            imap_server=str(config.get("outlook_imap_server") or ""),
            imap_port=config.get("outlook_imap_port") or 993,
            token_endpoint=str(config.get("outlook_token_endpoint") or ""),
            fixed_account=(config.get("retry_email_binding") if isinstance(config.get("retry_email_binding"), dict) else None),
            proxy=proxy,
            log_fn=log_fn,
            fixed_email=fixed_email,
        )
    raise RuntimeError(f"不支持的邮箱 provider: {provider}")
