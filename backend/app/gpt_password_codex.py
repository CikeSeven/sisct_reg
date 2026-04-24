from __future__ import annotations

import html
import json
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from io import BytesIO
from types import SimpleNamespace
from typing import Any, Callable
from zipfile import ZIP_DEFLATED, ZipFile

import requests
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from .db import acquire_proxy_pool_entry, get_config, get_proxy_pool_summary, parse_config_row_values
from .defaults import DEFAULT_CONFIG
from .external_uploads import build_sub2api_export_payload, generate_cpa_token_json
from .mail_providers import ProviderBase
from .schemas import GptPasswordCodexExportRequest, StartGptPasswordCodexJobRequest
from platforms.chatgpt.chatgpt_web_auth import ChatGPTWebAuthClient
from platforms.chatgpt.constants import OPENAI_EMAIL_SENDERS
from platforms.chatgpt.refresh_token_registration_engine import EmailServiceAdapter

router = APIRouter(prefix="/api/gpt-password-codex", tags=["gpt-password-codex"])

DEFAULT_OTP_SITE_URL = "https://nissanserena.my.id/otp"


@dataclass
class GptPasswordCodexJob:
    id: str
    raw_data: str
    delimiter: str = "|"
    otp_site_url: str = DEFAULT_OTP_SITE_URL
    merged_config: dict[str, Any] = field(default_factory=dict)
    executor_type: str = "protocol"
    status: str = "pending"
    total: int = 0
    success: int = 0
    failed: int = 0
    completed: int = 0
    progress: str = "0/0"
    created_at: float = field(default_factory=time.time)
    stop_requested: bool = False
    events: list[dict[str, Any]] = field(default_factory=list)
    results: list[dict[str, Any]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


class NissanSerenaOtpProvider(ProviderBase):
    def __init__(
        self,
        *,
        otp_site_url: str,
        proxy: str | None = None,
        log_fn: Callable[[str], None] | None = None,
        fixed_email: str | None = None,
    ) -> None:
        super().__init__(proxy=proxy, log_fn=log_fn, fixed_email=fixed_email)
        self.service_type = SimpleNamespace(value="nissanserena_otp")
        self.otp_site_url = _normalize_otp_search_url(otp_site_url)
        self._session = requests.Session()
        self._session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:149.0) Gecko/20100101 Firefox/149.0",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            }
        )

    def get_verification_code(
        self,
        *,
        email: str,
        timeout: int = 90,
        otp_sent_at: float | None = None,
        exclude_codes=None,
        interrupt_check: Callable[[], None] | None = None,
    ) -> str:
        deadline = time.time() + max(5, int(timeout or 0))
        excluded = {str(item).strip() for item in (exclude_codes or set()) if str(item or "").strip()}
        email_text = str(email or "").strip().lower()
        last_skip_reason = ""

        while time.time() < deadline:
            self._interrupt(interrupt_check)
            try:
                html_text = self._fetch_search_page(email_text)
                parsed = self._parse_search_result(html_text)
            except Exception as exc:
                last_skip_reason = f"查询接码页异常: {exc}"
                self._log(f"[NissanSerenaOTP] {last_skip_reason}")
                self._sleep_interruptibly(min(3.0, max(0.5, deadline - time.time())), interrupt_check=interrupt_check)
                continue

            code = str(parsed.get("code") or "").strip()
            sender = str(parsed.get("sender") or "").strip()
            sent_at = parsed.get("sent_at")

            if not code:
                last_skip_reason = "页面中尚未解析到 OTP"
            elif code in excluded:
                last_skip_reason = f"命中已尝试验证码: {code}"
            elif sender and not self._is_openai_sender(sender):
                last_skip_reason = f"最新邮件发件人非 OpenAI: {sender}"
            elif otp_sent_at and sent_at and float(sent_at) + 120 < float(otp_sent_at):
                last_skip_reason = "最新 OTP 时间早于本次发送时间"
            else:
                self._log(
                    f"[NissanSerenaOTP] 命中验证码: email={email_text} code={code} "
                    f"sender={sender or '-'} time={parsed.get('sent_at_text') or '-'}"
                )
                self._remember_code(code, successful=False)
                return code

            remaining = deadline - time.time()
            if remaining <= 0:
                break
            self._sleep_interruptibly(min(5.0, max(1.0, remaining)), interrupt_check=interrupt_check)

        if last_skip_reason:
            self._log(f"[NissanSerenaOTP] 等待验证码结束: {last_skip_reason}")
        return ""

    def _fetch_search_page(self, email: str) -> str:
        response = self._session.get(
            self.otp_site_url,
            params={"email": email},
            proxies=self.proxies,
            timeout=20,
        )
        response.raise_for_status()
        return response.text

    @classmethod
    def _parse_search_result(cls, html_text: str) -> dict[str, Any]:
        raw_html = str(html_text or "")
        normalized_html = html.unescape(raw_html)
        text = re.sub(r"<[^>]+>", " ", normalized_html)
        text = re.sub(r"\s+", " ", text).strip()

        sender = ""
        sender_matches = re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+", normalized_html)
        for candidate in sender_matches:
            if cls._is_openai_sender(candidate):
                sender = candidate
                break
        if not sender and sender_matches:
            sender = sender_matches[0]

        code = ""
        highlighted = re.search(r">\s*(\d(?:[\s-]*\d){5})\s*<", normalized_html)
        if highlighted:
            code = re.sub(r"\D", "", highlighted.group(1))
        if len(code) != 6:
            code_match = re.search(r"(?<!\d)(\d(?:[\s-]*\d){5})(?!\d)", text)
            if code_match:
                code = re.sub(r"\D", "", code_match.group(1))
        if len(code) != 6:
            code = ""

        sent_at_text = ""
        sent_at = None
        time_match = re.search(r"(\d{1,2}\s+[A-Za-z]{3}\s+\d{4},\s*\d{2}:\d{2})", text)
        if time_match:
            sent_at_text = time_match.group(1).strip()
            sent_at = cls._parse_result_timestamp(sent_at_text)

        return {
            "code": code,
            "sender": sender,
            "sent_at_text": sent_at_text,
            "sent_at": sent_at,
            "text": text,
        }

    @staticmethod
    def _parse_result_timestamp(value: str) -> float | None:
        text = str(value or "").strip()
        if not text:
            return None
        for pattern in ("%d %b %Y, %H:%M", "%d %B %Y, %H:%M"):
            try:
                return datetime.strptime(text, pattern).timestamp()
            except Exception:
                continue
        return None

    @staticmethod
    def _is_openai_sender(sender: str) -> bool:
        lowered = str(sender or "").strip().lower()
        if not lowered:
            return False
        for candidate in OPENAI_EMAIL_SENDERS:
            marker = str(candidate or "").strip().lower()
            if not marker:
                continue
            if marker.startswith("@") and lowered.endswith(marker):
                return True
            if marker.startswith(".") and lowered.endswith(marker):
                return True
            if lowered == marker:
                return True
        return False


def _normalize_otp_search_url(value: str) -> str:
    raw = str(value or "").strip() or DEFAULT_OTP_SITE_URL
    raw = raw.rstrip("/")
    if raw.endswith("/search"):
        return raw
    if raw.endswith("/otp"):
        return f"{raw}/search"
    if "/otp/" not in raw and not raw.endswith("otp"):
        return f"{raw}/otp/search"
    return f"{raw}/search"


def _configured_proxy_url(merged_config: dict[str, Any] | None) -> str:
    merged = dict(merged_config or {})
    if not merged.get("use_proxy"):
        return ""
    configured_proxy = str(merged.get("proxy") or "").strip()
    if configured_proxy:
        return configured_proxy
    summary = get_proxy_pool_summary(limit=1)
    if int(summary.get("enabled") or 0) <= 0:
        return ""
    entry = acquire_proxy_pool_entry()
    if not entry:
        return ""
    return str(entry.get("proxy_url") or "").strip()


def _describe_proxy_source(merged_config: dict[str, Any] | None) -> str:
    merged = dict(merged_config or {})
    if not merged.get("use_proxy"):
        return "配置页（未启用）"
    configured_proxy = str(merged.get("proxy") or "").strip()
    if configured_proxy:
        return f"配置页直填代理（{configured_proxy}）"
    summary = get_proxy_pool_summary(limit=1)
    if int(summary.get("enabled") or 0) > 0:
        return f"配置页代理池（可用 {int(summary.get('enabled') or 0)} 条）"
    return "配置页（已勾选默认代理，但没有可用代理）"


def parse_gpt_password_accounts(data: str, *, delimiter: str = "|") -> list[dict[str, str]]:
    delimiter_text = str(delimiter or "|")
    if not delimiter_text:
        raise ValueError("分隔符不能为空")

    items: list[dict[str, str]] = []
    for index, raw_line in enumerate(str(data or "").splitlines(), start=1):
        line = str(raw_line or "").strip().lstrip("\ufeff")
        if not line:
            continue
        lowered = line.lower()
        if line.startswith("#") or line.startswith("==="):
            continue
        if lowered.startswith("api接码网站") or lowered.startswith("api otp"):
            continue
        if delimiter_text not in line:
            continue
        email, password = line.split(delimiter_text, 1)
        email = str(email or "").strip().lower()
        password = str(password or "").strip()
        if not email and not password:
            continue
        if not email or "@" not in email:
            raise ValueError(f"行 {index}: 邮箱格式无效")
        if not password:
            raise ValueError(f"行 {index}: 密码不能为空")
        items.append({"email": email, "password": password})

    if not items:
        raise ValueError("未解析到任何账号，请检查分隔符或导入内容")
    return items


def authorize_codex_account_via_password(
    account: dict[str, Any],
    *,
    merged_config: dict[str, Any] | None,
    executor_type: str = "protocol",
    otp_site_url: str = DEFAULT_OTP_SITE_URL,
    log_fn: Callable[[str], None] | None = None,
    interrupt_check: Callable[[], None] | None = None,
) -> dict[str, Any]:
    item = dict(account or {})
    merged = dict(merged_config or {})
    email = str(item.get("email") or "").strip().lower()
    password = str(item.get("password") or "").strip()
    if not (email and password):
        return {"success": False, "error": "GPT 账号参数不完整"}

    logger = log_fn or (lambda _message: None)
    proxy_url = _configured_proxy_url(merged)
    otp_provider = NissanSerenaOtpProvider(
        otp_site_url=otp_site_url,
        proxy=proxy_url or None,
        fixed_email=email,
        log_fn=logger,
    )
    email_adapter = EmailServiceAdapter(
        otp_provider,
        email,
        logger,
        interrupt_check=interrupt_check,
    )

    max_retry_count = 3
    last_error = "Codex 授权失败"
    tokens: dict[str, Any] | None = None
    client: ChatGPTWebAuthClient | None = None
    for attempt in range(1, max_retry_count + 2):
        client = ChatGPTWebAuthClient(
            config=merged,
            proxy=proxy_url or None,
            verbose=False,
            browser_mode=str(executor_type or "protocol"),
        )
        client._log = lambda message: logger(str(message or ""))

        logger(f"开始 GPT 账号 Codex 授权: {attempt}/{max_retry_count + 1}")
        tokens = client.login_and_get_tokens(
            email=email,
            password=password,
            device_id="",
            user_agent=None,
            sec_ch_ua=None,
            impersonate=None,
            skymail_client=email_adapter,
            prefer_passwordless_login=False,
            allow_phone_verification=False,
            force_new_browser=True,
            force_chatgpt_entry=True,
            screen_hint="login",
            force_password_login=True,
            complete_about_you_if_needed=False,
            login_source="gpt_password_codex_batch",
        )
        if tokens:
            break
        last_error = str(client.last_error or "Codex 授权失败")
        if attempt <= max_retry_count:
            logger(f"Codex 授权失败 {attempt}/{max_retry_count + 1}: {last_error}；准备重试")
            time.sleep(min(5, attempt))
            continue
        return {"success": False, "error": last_error}

    if not tokens or client is None:
        return {"success": False, "error": last_error}

    result_obj = SimpleNamespace(
        email=email,
        access_token=str(tokens.get("access_token") or ""),
        refresh_token=str(tokens.get("refresh_token") or ""),
        id_token=str(tokens.get("id_token") or ""),
        session_token=str(tokens.get("session_token") or tokens.get("sessionToken") or ""),
        account_id=str(getattr(client, "last_workspace_id", "") or ""),
    )
    cpa_account = generate_cpa_token_json(result_obj)
    oauth_account = {
        "type": "codex",
        "email": email,
        "account_id": str(cpa_account.get("account_id") or result_obj.account_id or ""),
        "access_token": str(result_obj.access_token or ""),
        "refresh_token": str(result_obj.refresh_token or ""),
        "id_token": str(result_obj.id_token or ""),
        "session_token": str(result_obj.session_token or ""),
        "expired": str(cpa_account.get("expired") or ""),
        "last_refresh": str(cpa_account.get("last_refresh") or ""),
    }
    return {
        "success": True,
        "email": email,
        "account_id": str(cpa_account.get("account_id") or result_obj.account_id or ""),
        "oauth_account": oauth_account,
        "cpa_account": cpa_account,
        "cpa_json": json.dumps(cpa_account, ensure_ascii=False, indent=2),
    }


class GptPasswordCodexManager:
    def __init__(self) -> None:
        self._jobs: dict[str, GptPasswordCodexJob] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()

    def create_job(
        self,
        *,
        data: str,
        delimiter: str,
        otp_site_url: str,
        merged_config: dict[str, Any],
        executor_type: str = "protocol",
    ) -> str:
        job_id = f"gpt_password_codex_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
        job = GptPasswordCodexJob(
            id=job_id,
            raw_data=str(data or ""),
            delimiter=str(delimiter or "|"),
            otp_site_url=str(otp_site_url or DEFAULT_OTP_SITE_URL),
            merged_config=dict(merged_config or {}),
            executor_type=str(executor_type or "protocol"),
        )
        with self._lock:
            self._jobs[job_id] = job
        thread = threading.Thread(target=self._run_job, args=(job,), daemon=True)
        self._threads[job_id] = thread
        thread.start()
        return job_id

    def get_job_snapshot(self, job_id: str) -> dict[str, Any] | None:
        job = self._jobs.get(str(job_id))
        if not job:
            return None
        with job.lock:
            return {
                "id": job.id,
                "status": job.status,
                "delimiter": job.delimiter,
                "otp_site_url": job.otp_site_url,
                "total": job.total,
                "success": job.success,
                "failed": job.failed,
                "completed": job.completed,
                "progress": job.progress,
                "created_at": job.created_at,
                "events": list(job.events),
                "results": list(job.results),
            }

    def list_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        jobs = sorted(self._jobs.values(), key=lambda item: item.created_at, reverse=True)
        result: list[dict[str, Any]] = []
        for job in jobs[: max(1, min(limit, 100))]:
            with job.lock:
                result.append(
                    {
                        "id": job.id,
                        "status": job.status,
                        "delimiter": job.delimiter,
                        "otp_site_url": job.otp_site_url,
                        "total": job.total,
                        "success": job.success,
                        "failed": job.failed,
                        "completed": job.completed,
                        "progress": job.progress,
                        "created_at": job.created_at,
                    }
                )
        return result

    def export_results(self, job_id: str, *, emails: list[str] | None = None) -> dict[str, Any]:
        job = self._jobs.get(str(job_id))
        if not job:
            return {"ok": False, "reason": "job_not_found"}

        selected = {str(item or "").strip().lower() for item in (emails or []) if str(item or "").strip()}
        with job.lock:
            items = [
                dict(item)
                for item in (job.results or [])
                if str(item.get("status") or "") == "success"
                and str(item.get("cpa_json") or "").strip()
                and (not selected or str(item.get("email") or "").strip().lower() in selected)
            ]
        if not items:
            return {"ok": False, "reason": "empty_export"}

        payloads = [self._build_export_payload(item) for item in items]
        buffer = BytesIO()
        with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as archive:
            for item, payload in zip(items, payloads):
                email = str(item.get("email") or "account").strip() or "account"
                safe_name = "".join(ch if ch.isalnum() or ch in {".", "_", "-"} else "_" for ch in email)
                archive.writestr(
                    f"CPA/{safe_name}.json",
                    json.dumps(generate_cpa_token_json(payload), ensure_ascii=False, indent=2),
                )
            archive.writestr(
                "sub2api/sub2api_accounts.json",
                json.dumps(build_sub2api_export_payload(payloads), ensure_ascii=False, indent=2),
            )
        return {
            "ok": True,
            "filename": f"gpt_password_codex_{job_id}.zip",
            "content": buffer.getvalue(),
            "count": len(items),
        }

    @staticmethod
    def _build_export_payload(item: dict[str, Any]) -> Any:
        oauth_account = item.get("oauth_account") if isinstance(item.get("oauth_account"), dict) else {}
        cpa_account = item.get("cpa_account") if isinstance(item.get("cpa_account"), dict) else {}
        return SimpleNamespace(
            email=str(oauth_account.get("email") or item.get("email") or cpa_account.get("email") or "").strip(),
            access_token=str(oauth_account.get("access_token") or cpa_account.get("access_token") or "").strip(),
            refresh_token=str(oauth_account.get("refresh_token") or cpa_account.get("refresh_token") or "").strip(),
            id_token=str(oauth_account.get("id_token") or cpa_account.get("id_token") or "").strip(),
            session_token=str(oauth_account.get("session_token") or cpa_account.get("sessionToken") or "").strip(),
            account_id=str(oauth_account.get("account_id") or cpa_account.get("account_id") or item.get("account_id") or "").strip(),
        )

    def delete_results(self, job_id: str, *, emails: list[str] | None = None) -> dict[str, Any]:
        job = self._jobs.get(str(job_id))
        if not job:
            return {"ok": False, "reason": "job_not_found"}
        selected = {str(item or "").strip().lower() for item in (emails or []) if str(item or "").strip()}
        if not selected:
            return {"ok": False, "reason": "empty_selection"}
        with job.lock:
            before = len(job.results or [])
            job.results = [
                item
                for item in (job.results or [])
                if str(item.get("email") or "").strip().lower() not in selected
            ]
            deleted = before - len(job.results or [])
        if deleted > 0:
            self._append_event(job, f"已删除转换结果 {deleted} 条: {', '.join(sorted(selected))}")
        return {"ok": True, "deleted": deleted}


    def stop_job(self, job_id: str) -> dict[str, Any]:
        job = self._jobs.get(str(job_id))
        if not job:
            return {"ok": False, "reason": "job_not_found"}
        job.stop_requested = True
        with job.lock:
            if job.status in {"pending", "running"}:
                job.status = "stopped"
        self._append_event(job, "已请求停止 GPT 账号 Codex 转换", level="error")
        return {"ok": True}

    def _append_event(self, job: GptPasswordCodexJob, message: str, *, level: str = "info", account_email: str = "") -> None:
        with job.lock:
            seq = len(job.events) + 1
            job.events.append(
                {
                    "id": seq,
                    "seq": seq,
                    "level": str(level or "info"),
                    "account_email": str(account_email or ""),
                    "message": str(message or ""),
                    "created_at": time.time(),
                }
            )

    def _check_stop(self, job: GptPasswordCodexJob) -> None:
        if job.stop_requested:
            raise RuntimeError("批量 GPT 账号 Codex 转换已停止")

    def _run_job(self, job: GptPasswordCodexJob) -> None:
        with job.lock:
            job.status = "running"
        try:
            entries = parse_gpt_password_accounts(job.raw_data, delimiter=job.delimiter)
        except Exception as exc:
            with job.lock:
                job.status = "failed"
                job.progress = "0/0"
            self._append_event(job, str(exc or "导入格式错误"), level="error")
            return

        with job.lock:
            job.total = len(entries)
            job.progress = f"0/{len(entries)}"

        runtime_config = dict(job.merged_config or {})
        self._append_event(job, f"开始 GPT 账号 Codex 转换，共 {len(entries)} 个账号")
        self._append_event(job, f"接码查询地址：{_normalize_otp_search_url(job.otp_site_url)}")
        self._append_event(job, f"代理来源：{_describe_proxy_source(runtime_config)}")

        for item in entries:
            email = str(item.get("email") or "").strip().lower()
            try:
                self._check_stop(job)
                self._append_event(job, "开始 Codex OAuth 登录", account_email=email)
                result = authorize_codex_account_via_password(
                    item,
                    merged_config=runtime_config,
                    executor_type=job.executor_type,
                    otp_site_url=job.otp_site_url,
                    log_fn=lambda message, current_email=email: self._append_event(job, str(message or ""), account_email=current_email),
                    interrupt_check=lambda current_job=job: self._check_stop(current_job),
                )
                with job.lock:
                    if result.get("success"):
                        job.success += 1
                    else:
                        job.failed += 1
                    job.results.insert(
                        0,
                        {
                            "email": email,
                            "status": "success" if result.get("success") else "failed",
                            "account_id": str(result.get("account_id") or ""),
                            "error": str(result.get("error") or ""),
                            "cpa_json": str(result.get("cpa_json") or ""),
                            "cpa_account": dict(result.get("cpa_account") or {}),
                            "oauth_account": dict(result.get("oauth_account") or {}),
                            "created_at": time.time(),
                        },
                    )
                if result.get("success"):
                    self._append_event(job, f"Codex 授权成功: account_id={str(result.get('account_id') or '-')}", account_email=email)
                else:
                    self._append_event(job, str(result.get("error") or "Codex 授权失败"), level="error", account_email=email)
            except Exception as exc:
                with job.lock:
                    job.failed += 1
                    job.results.insert(
                        0,
                        {
                            "email": email,
                            "status": "failed",
                            "account_id": "",
                            "error": str(exc or "Codex 授权异常"),
                            "cpa_json": "",
                            "cpa_account": {},
                            "oauth_account": {},
                            "created_at": time.time(),
                        },
                    )
                self._append_event(job, str(exc or "Codex 授权异常"), level="error", account_email=email)
            finally:
                with job.lock:
                    job.completed += 1
                    job.progress = f"{job.completed}/{job.total}"

        with job.lock:
            if job.stop_requested:
                job.status = "stopped"
            elif job.failed > 0 and job.success == 0:
                job.status = "failed"
            else:
                job.status = "done"


gpt_password_codex_manager = GptPasswordCodexManager()


@router.post("/jobs")
def create_gpt_password_codex_job(body: StartGptPasswordCodexJobRequest):
    merged_config = dict(DEFAULT_CONFIG)
    merged_config.update(parse_config_row_values(get_config()))
    job_id = gpt_password_codex_manager.create_job(
        data=body.data,
        delimiter=body.delimiter,
        otp_site_url=body.otp_site_url or DEFAULT_OTP_SITE_URL,
        merged_config=merged_config,
        executor_type=body.executor_type,
    )
    return {"job_id": job_id}


@router.get("/jobs")
def list_gpt_password_codex_jobs(limit: int = 20):
    return {"items": gpt_password_codex_manager.list_jobs(limit=limit)}


@router.get("/jobs/{job_id}")
def get_gpt_password_codex_job(job_id: str):
    snapshot = gpt_password_codex_manager.get_job_snapshot(job_id)
    if not snapshot:
        raise HTTPException(404, "任务不存在")
    return snapshot


@router.post("/jobs/{job_id}/export")
def export_gpt_password_codex_job(job_id: str, body: GptPasswordCodexExportRequest):
    result = gpt_password_codex_manager.export_results(job_id, emails=body.emails)
    if not result.get("ok"):
        reason = str(result.get("reason") or "")
        if reason == "job_not_found":
            raise HTTPException(404, "任务不存在")
        raise HTTPException(400, "没有可导出的成功账号")
    return StreamingResponse(
        iter([result.get("content") or b""]),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{str(result.get("filename") or "gpt_password_codex.zip")}"'},
    )


@router.post("/jobs/{job_id}/results/delete")
def delete_gpt_password_codex_results(job_id: str, body: GptPasswordCodexExportRequest):
    result = gpt_password_codex_manager.delete_results(job_id, emails=body.emails)
    if not result.get("ok"):
        reason = str(result.get("reason") or "")
        if reason == "job_not_found":
            raise HTTPException(404, "任务不存在")
        raise HTTPException(400, "请选择要删除的账号")
    return result


@router.post("/jobs/{job_id}/stop")
def stop_gpt_password_codex_job(job_id: str):
    result = gpt_password_codex_manager.stop_job(job_id)
    if not result.get("ok"):
        raise HTTPException(404, "任务不存在")
    return result
