from __future__ import annotations

import json
import threading
import time
import uuid
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any
from core.proxy_utils import build_requests_proxy_config

from .codex_team_manager import codex_team_manager
from .db import acquire_proxy_pool_entry, batch_import_codex_team_parent_accounts
from .mail_providers import build_mail_provider
from .team_open_store import (
    append_team_open_job_event,
    create_team_open_job,
    get_team_open_card_summary,
    get_team_open_job,
    list_team_open_cards,
    list_team_open_job_events,
    list_team_open_jobs,
    list_team_open_results,
    mark_team_open_card_usage,
    update_team_open_job,
    upsert_team_open_result,
)
from platforms.chatgpt.chatgpt_web_auth import ChatGPTWebAuthClient, choose_team_workspace
from platforms.chatgpt.refresh_token_registration_engine import RefreshTokenRegistrationEngine
from platforms.chatgpt.team_open_payment import (
    ChatGPTPaymentClient,
    TeamOpenCardProfile,
    TeamPaymentLinkClient,
    submit_stripe_checkout,
)
from platforms.chatgpt.utils import decode_jwt_payload


@dataclass
class TeamOpenJob:
    id: str
    request_payload: dict[str, Any]
    merged_config: dict[str, Any]
    status: str = "pending"
    total: int = 0
    success: int = 0
    failed: int = 0
    completed: int = 0
    progress: str = "0/0"
    stop_requested: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


TEAM_OPEN_OPTION_KEYS = {
    "team_open_precheck_enabled",
    "team_open_precheck_attempts",
    "team_open_auto_submit_payment",
    "team_open_reuse_cards",
    "team_open_use_proxy",
    "team_open_proxy",
    "team_open_payment_service_base_url",
    "team_open_payment_service_plan_name",
    "team_open_payment_service_country",
    "team_open_payment_service_currency",
    "team_open_payment_service_promo_campaign_id",
    "team_open_payment_service_price_interval",
    "team_open_payment_service_seat_quantity",
    "team_open_payment_service_check_card_proxy",
    "team_open_payment_service_is_short_link",
    "team_open_pro_checkout_country",
    "team_open_pro_checkout_currency",
    "team_open_default_holder_name",
    "team_open_default_billing_email",
    "team_open_default_country",
    "team_open_default_state",
    "team_open_default_city",
    "team_open_default_line1",
    "team_open_default_postal_code",
    "team_open_post_payment_wait_seconds",
    "team_open_verification_retries",
}


class TeamOpenManager:
    def __init__(self) -> None:
        self._jobs: dict[str, TeamOpenJob] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _extract_access_token_item(item: Any) -> str:
        if isinstance(item, dict):
            for key in ("access_token", "accessToken", "token", "bearer", "Authorization", "authorization"):
                value = str(item.get(key) or "").strip()
                if value:
                    return value.removeprefix("Bearer ").strip()
            nested = item.get("https://api.openai.com/auth")
            if isinstance(nested, dict):
                value = str(nested.get("access_token") or nested.get("token") or "").strip()
                if value:
                    return value.removeprefix("Bearer ").strip()
            return ""
        text = str(item or "").strip().strip(",")
        if not text:
            return ""
        if text.lower().startswith("bearer "):
            return text[7:].strip()
        if text.startswith("{") and text.endswith("}"):
            try:
                return TeamOpenManager._extract_access_token_item(json.loads(text))
            except Exception:
                return text
        for marker in ("access_token=", "accessToken=", "token="):
            if marker in text:
                return text.split(marker, 1)[1].split("&", 1)[0].strip().strip('"\'')
        return text

    @staticmethod
    def _parse_access_tokens(value: Any) -> list[str]:
        raw_items: list[Any] = []
        if isinstance(value, (list, tuple)):
            raw_items.extend(value)
        elif isinstance(value, dict):
            raw_items.append(value)
        else:
            text = str(value or "").strip()
            if text:
                try:
                    parsed = json.loads(text)
                    if isinstance(parsed, list):
                        raw_items.extend(parsed)
                    elif isinstance(parsed, dict):
                        raw_items.append(parsed)
                    else:
                        raw_items.extend(text.replace("\r", "\n").split("\n"))
                except Exception:
                    raw_items.extend(text.replace("\r", "\n").split("\n"))
        tokens: list[str] = []
        seen: set[str] = set()
        for item in raw_items:
            token = TeamOpenManager._extract_access_token_item(item)
            if not token or token in seen:
                continue
            seen.add(token)
            tokens.append(token)
        return tokens

    @staticmethod
    def _email_from_access_token(access_token: str) -> str:
        try:
            payload = decode_jwt_payload(str(access_token or ""))
        except Exception:
            payload = {}
        profile = payload.get("https://api.openai.com/profile") if isinstance(payload, dict) else {}
        if isinstance(profile, dict):
            email = str(profile.get("email") or "").strip().lower()
            if email:
                return email
        return str(payload.get("email") or "").strip().lower() if isinstance(payload, dict) else ""


    @staticmethod
    def normalize_options(merged_config: dict[str, Any], options: dict[str, Any] | None = None) -> dict[str, Any]:
        runtime = dict(merged_config or {})
        for key, value in dict(options or {}).items():
            if key in TEAM_OPEN_OPTION_KEYS:
                runtime[key] = value
        runtime["team_open_precheck_enabled"] = bool(runtime.get("team_open_precheck_enabled", True))
        runtime["team_open_precheck_attempts"] = max(1, int(runtime.get("team_open_precheck_attempts") or 2))
        runtime["team_open_auto_submit_payment"] = bool(runtime.get("team_open_auto_submit_payment", True))
        runtime["team_open_reuse_cards"] = bool(runtime.get("team_open_reuse_cards", False))
        runtime["team_open_use_proxy"] = bool(runtime.get("team_open_use_proxy", True))
        runtime["team_open_proxy"] = str(runtime.get("team_open_proxy") or "").strip()
        runtime["team_open_payment_service_base_url"] = str(runtime.get("team_open_payment_service_base_url") or "").strip()
        runtime["team_open_payment_service_plan_name"] = str(runtime.get("team_open_payment_service_plan_name") or "chatgptteamplan").strip() or "chatgptteamplan"
        runtime["team_open_payment_service_country"] = str(runtime.get("team_open_payment_service_country") or "SG").strip().upper() or "SG"
        runtime["team_open_payment_service_currency"] = str(runtime.get("team_open_payment_service_currency") or "SGD").strip().upper() or "SGD"
        runtime["team_open_payment_service_promo_campaign_id"] = str(runtime.get("team_open_payment_service_promo_campaign_id") or "team-1-month-free").strip()
        runtime["team_open_payment_service_price_interval"] = str(runtime.get("team_open_payment_service_price_interval") or "month").strip() or "month"
        runtime["team_open_payment_service_seat_quantity"] = max(1, int(runtime.get("team_open_payment_service_seat_quantity") or 5))
        runtime["team_open_payment_service_check_card_proxy"] = bool(runtime.get("team_open_payment_service_check_card_proxy", False))
        runtime["team_open_payment_service_is_short_link"] = bool(runtime.get("team_open_payment_service_is_short_link", False))
        runtime["team_open_pro_checkout_country"] = str(runtime.get("team_open_pro_checkout_country") or "US").strip().upper() or "US"
        runtime["team_open_pro_checkout_currency"] = str(runtime.get("team_open_pro_checkout_currency") or "USD").strip().upper() or "USD"
        runtime["team_open_default_holder_name"] = str(runtime.get("team_open_default_holder_name") or "lu").strip()
        runtime["team_open_default_billing_email"] = str(runtime.get("team_open_default_billing_email") or "redwood@mail.yaoke.vip").strip()
        runtime["team_open_default_country"] = str(runtime.get("team_open_default_country") or "US").strip()
        runtime["team_open_default_state"] = str(runtime.get("team_open_default_state") or "CA").strip()
        runtime["team_open_default_city"] = str(runtime.get("team_open_default_city") or "Woodland").strip()
        runtime["team_open_default_line1"] = str(runtime.get("team_open_default_line1") or "120 Main Street").strip()
        runtime["team_open_default_postal_code"] = str(runtime.get("team_open_default_postal_code") or "95695").strip()
        runtime["team_open_post_payment_wait_seconds"] = max(0, int(runtime.get("team_open_post_payment_wait_seconds") or 30))
        runtime["team_open_verification_retries"] = max(1, int(runtime.get("team_open_verification_retries") or 3))
        return runtime

    def create_job(self, request_payload: dict[str, Any], *, merged_config: dict[str, Any]) -> str:
        payload = dict(request_payload or {})
        payload.setdefault("count", 1)
        payload.setdefault("concurrency", 1)
        payload.setdefault("executor_type", "protocol")
        payload.setdefault("options", {})
        runtime = self.normalize_options(merged_config, payload.get("options") or {})
        total = max(1, int(payload.get("count") or 1))
        cards = list_team_open_cards(enabled_only=True, include_sensitive=True) if runtime.get("team_open_auto_submit_payment") else []
        if runtime.get("team_open_auto_submit_payment"):
            if not cards:
                raise ValueError("已开启自动提交 Team 绑卡，请先导入至少一张启用中的银行卡；如只生成支付链接请关闭自动提交。")
            if total > len(cards) and not runtime.get("team_open_reuse_cards"):
                raise ValueError(f"当前仅有 {len(cards)} 张可用银行卡，未开启重复使用，无法处理 {total} 个任务")
        if not str(runtime.get("cloud_mail_base_url") or "").strip():
            raise ValueError("请先在邮箱设置中配置 Cloud Mail，用于自动注册 Team 母号")

        job_id = f"team_open_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
        job = TeamOpenJob(
            id=job_id,
            request_payload=payload,
            merged_config=runtime,
            total=total,
            progress=f"0/{total}",
        )
        with self._lock:
            self._jobs[job_id] = job
        create_team_open_job(job_id, request_payload=payload)
        update_team_open_job(job_id, total=total, completed=0, success=0, failed=0, progress=job.progress)
        append_team_open_job_event(job_id, f"创建 Team 母号开通任务成功，计划处理 {total} 个账号")
        thread = threading.Thread(target=self._run_job, args=(job,), daemon=True)
        self._threads[job_id] = thread
        thread.start()
        return job_id

    def _set_job_state(self, job: TeamOpenJob, **fields: Any) -> None:
        with job.lock:
            for key, value in fields.items():
                setattr(job, key, value)
            job.progress = f"{job.completed}/{job.total}"
            update_team_open_job(
                job.id,
                status=job.status,
                total=job.total,
                success=job.success,
                failed=job.failed,
                completed=job.completed,
                progress=job.progress,
                error=str(fields.get("error") or "") if "error" in fields else None,
            )

    @staticmethod
    def _check_stop(job: TeamOpenJob) -> None:
        if job.stop_requested:
            raise RuntimeError("任务已手动停止")

    def _log(self, job: TeamOpenJob, message: str, *, level: str = "info", email: str = "") -> None:
        append_team_open_job_event(job.id, str(message or ""), level=level, account_email=email)

    def _resolve_proxy(self, runtime: dict[str, Any]) -> tuple[str, int]:
        use_proxy = bool(runtime.get("team_open_use_proxy", runtime.get("use_proxy", True)))
        if not use_proxy:
            return "", 0
        fixed_proxy = str(runtime.get("team_open_proxy") or runtime.get("proxy") or "").strip()
        if fixed_proxy:
            return fixed_proxy, 0
        entry = acquire_proxy_pool_entry()
        if not entry:
            return "", 0
        return str(entry.get("proxy_url") or "").strip(), int(entry.get("id") or 0)

    @staticmethod
    def _normalize_country(value: Any) -> str:
        text = str(value or "").strip()
        lowered = text.lower()
        aliases = {
            "us": "US",
            "usa": "US",
            "united states": "US",
            "united states of america": "US",
            "sg": "SG",
            "singapore": "SG",
        }
        if lowered in aliases:
            return aliases[lowered]
        if len(text) == 2 and text.isalpha():
            return text.upper()
        return text

    @staticmethod
    def _build_card_profile(card: dict[str, Any], runtime: dict[str, Any]) -> TeamOpenCardProfile:
        return TeamOpenCardProfile(
            id=int(card.get("id") or 0),
            label=str(card.get("label") or ""),
            card_number=str(card.get("card_number") or ""),
            exp_month=str(card.get("exp_month") or ""),
            exp_year=str(card.get("exp_year") or ""),
            cvc=str(card.get("cvc") or ""),
            holder_name=str(card.get("holder_name") or runtime.get("team_open_default_holder_name") or "").strip(),
            billing_email=str(card.get("billing_email") or runtime.get("team_open_default_billing_email") or "").strip(),
            country=TeamOpenManager._normalize_country(card.get("country") or runtime.get("team_open_default_country") or ""),
            state=str(card.get("state") or runtime.get("team_open_default_state") or "").strip(),
            city=str(card.get("city") or runtime.get("team_open_default_city") or "").strip(),
            line1=str(card.get("line1") or runtime.get("team_open_default_line1") or "").strip(),
            postal_code=str(card.get("postal_code") or runtime.get("team_open_default_postal_code") or "").strip(),
        )

    @staticmethod
    def _extract_team_workspace(session_payload: dict[str, Any]) -> dict[str, Any]:
        data = dict(session_payload or {})
        if str(data.get("selected_workspace_kind") or "").strip().lower() == "organization":
            return {
                "id": str(data.get("selected_workspace_id") or data.get("account_id") or "").strip(),
                "name": str(data.get("display_name") or "").strip(),
                "plan_type": str(data.get("plan_type") or "").strip(),
            }
        accounts_root = dict((data.get("info") or {}).get("accounts") or {})
        accounts_map = accounts_root.get("accounts") or {}
        items: list[dict[str, Any]] = []
        if isinstance(accounts_map, dict):
            for workspace_id, entry in accounts_map.items():
                account = dict((entry or {}).get("account") or {})
                if not account:
                    continue
                account.setdefault("id", str(account.get("id") or workspace_id or "").strip())
                items.append(account)
        selected = choose_team_workspace(items)
        if not selected:
            return {}
        return {
            "id": str(selected.get("id") or "").strip(),
            "name": str(selected.get("name") or "").strip(),
            "plan_type": str(selected.get("plan_type") or "").strip(),
        }

    def _register_account(
        self,
        job: TeamOpenJob,
        *,
        runtime: dict[str, Any],
        proxy_url: str,
        card: TeamOpenCardProfile,
        attempt_index: int,
    ) -> tuple[Any, Any]:
        email_service = build_mail_provider(
            "cloud_mail",
            config=runtime,
            proxy=proxy_url or None,
            log_fn=lambda message, current_job=job: self._log(current_job, str(message or ""), email=""),
        )
        engine = RefreshTokenRegistrationEngine(
            email_service=email_service,
            proxy_url=proxy_url or None,
            callback_logger=lambda message, current_job=job: self._log(current_job, str(message or ""), email=""),
            task_uuid=f"{job.id}:{attempt_index}",
            browser_mode=str(job.request_payload.get("executor_type") or "protocol"),
            extra_config={
                **runtime,
                "team_open_reuse_registration_session": True,
                "register_only_before_invite": False,
            },
            interrupt_check=lambda current_job=job: self._check_stop(current_job),
        )
        result = engine.run()
        if not result.success:
            raise RuntimeError(str(result.error_message or "注册失败"))
        if not str(getattr(result, "access_token", "") or "").strip():
            raise RuntimeError("注册成功但未获取到 Team 支付链接所需 accessToken")
        upsert_team_open_result(
            job.id,
            attempt_index,
            email=str(result.email or ""),
            password=str(result.password or ""),
            status="registered_token_ready",
            card_id=card.id,
            card_label=card.label,
            extra_json=dict(result.metadata or {}),
        )
        return email_service, result

    def _login_web_session(
        self,
        *,
        email: str,
        password: str,
        email_service,
        runtime: dict[str, Any],
        proxy_url: str,
        login_source: str,
    ) -> tuple[ChatGPTWebAuthClient, dict[str, Any]]:
        auth_client = ChatGPTWebAuthClient(
            config=runtime,
            proxy=proxy_url or None,
            verbose=False,
            browser_mode=str(runtime.get("executor_type") or self._normalize_executor(runtime.get("executor_type"))),
        )
        auth_client._log = lambda _message: None
        session_payload = auth_client.login_and_get_session(
            email=email,
            password=password,
            skymail_client=email_service,
            login_source=login_source,
            require_web_session=True,
        )
        if not session_payload.get("success"):
            raise RuntimeError(str(session_payload.get("error") or "网页登录失败"))
        return auth_client, session_payload

    @staticmethod
    def _normalize_executor(value: Any) -> str:
        text = str(value or "protocol").strip().lower()
        return text if text in {"protocol", "headless", "headed"} else "protocol"

    def _run_precheck(
        self,
        job: TeamOpenJob,
        *,
        auth_client: ChatGPTWebAuthClient,
        session_payload: dict[str, Any],
        card: TeamOpenCardProfile,
        runtime: dict[str, Any],
        email: str,
    ) -> None:
        attempts = int(runtime.get("team_open_precheck_attempts") or 2)
        payment_client = ChatGPTPaymentClient(auth_client, session_payload, log_fn=lambda message: self._log(job, message, email=email))
        for current in range(1, attempts + 1):
            self._check_stop(job)
            checkout = payment_client.create_pro_checkout(
                country=str(runtime.get("team_open_pro_checkout_country") or "US"),
                currency=str(runtime.get("team_open_pro_checkout_currency") or "USD"),
            )
            self._log(job, f"预检订阅第 {current}/{attempts} 次，checkout={checkout['checkout_session_id']}", email=email)
            self._log(job, "[Stripe] checkout 浏览器默认直连（不使用注册代理）", email=email)
            pay_result = submit_stripe_checkout(
                checkout["checkout_url"],
                card=card,
                browser_mode="headed" if self._normalize_executor(job.request_payload.get("executor_type")) == "headed" else "headless",
                proxy_url=None,
                expect_decline=True,
                log_fn=lambda message: self._log(job, message, email=email),
            )
            upsert_team_open_result(
                job.id,
                int(current if current else 1),
            )
            if bool(pay_result.get("declined")):
                upsert_team_open_result(job.id, int(session_payload.get("attempt_index") or 1), precheck_status=f"declined:{current}")
                self._log(job, f"预检订阅第 {current} 次已拒付，继续下一次", level="warning", email=email)
                continue
            raise RuntimeError(str(pay_result.get("error") or "预检订阅未出现预期拒付，已停止后续流程"))

    def _generate_payment_link(
        self,
        job: TeamOpenJob,
        *,
        runtime: dict[str, Any],
        session_payload: dict[str, Any],
        proxy_url: str,
        email: str,
    ) -> dict[str, Any]:
        self._log(job, "[TeamLink] 支付链接服务默认直连（不使用注册代理）", email=email)
        client = TeamPaymentLinkClient(
            base_url=str(runtime.get("team_open_payment_service_base_url") or ""),
            proxy_url=None,
            log_fn=lambda message: self._log(job, message, email=email),
        )
        return client.generate_payment_link(
            access_token=str(session_payload.get("access_token") or ""),
            plan_name=str(runtime.get("team_open_payment_service_plan_name") or "chatgptteamplan"),
            country=str(runtime.get("team_open_payment_service_country") or "SG"),
            currency=str(runtime.get("team_open_payment_service_currency") or "SGD"),
            seat_quantity=int(runtime.get("team_open_payment_service_seat_quantity") or 5),
            price_interval=str(runtime.get("team_open_payment_service_price_interval") or "month"),
            promo_campaign_id=str(runtime.get("team_open_payment_service_promo_campaign_id") or "team-1-month-free"),
            check_card_proxy=bool(runtime.get("team_open_payment_service_check_card_proxy", False)),
            is_short_link=bool(runtime.get("team_open_payment_service_is_short_link", False)),
        )

    def _verify_team_workspace_by_access_token(
        self,
        job: TeamOpenJob,
        *,
        access_token: str,
        runtime: dict[str, Any],
        proxy_url: str,
        email: str,
    ) -> dict[str, Any]:
        retries = int(runtime.get("team_open_verification_retries") or 3)
        wait_seconds = int(runtime.get("team_open_post_payment_wait_seconds") or 30)
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {str(access_token or '').strip()}",
            "oai-language": "zh-CN",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
        }
        proxies = build_requests_proxy_config(proxy_url) or {}
        url = "https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27?timezone_offset_min=-480"
        for current in range(1, retries + 1):
            self._check_stop(job)
            if current == 1 and wait_seconds > 0:
                self._log(job, f"支付提交后等待 {wait_seconds} 秒再用 Access Token 验证 Team 工作区", email=email)
                for _ in range(wait_seconds):
                    self._check_stop(job)
                    time.sleep(1)
            elif current > 1:
                time.sleep(5)
            try:
                response = requests.get(url, headers=headers, proxies=proxies, timeout=30)
                if response.status_code >= 400:
                    self._log(job, f"第 {current} 次 token 验证失败: HTTP {response.status_code} {response.text[:120]}", level="warning", email=email)
                    continue
                payload = response.json() or {}
            except Exception as exc:
                self._log(job, f"第 {current} 次 token 验证异常: {exc}", level="warning", email=email)
                continue

            accounts_map = payload.get("accounts") or {}
            workspaces: list[dict[str, Any]] = []
            if isinstance(accounts_map, dict):
                for workspace_id, entry in accounts_map.items():
                    account = dict((entry or {}).get("account") or {})
                    if not account:
                        continue
                    account.setdefault("id", str(account.get("id") or workspace_id or "").strip())
                    workspaces.append(account)
            selected = choose_team_workspace(workspaces)
            if selected and str(selected.get("id") or "").strip():
                return {
                    "success": True,
                    "team_workspace": {
                        "id": str(selected.get("id") or "").strip(),
                        "name": str(selected.get("name") or "").strip(),
                        "plan_type": str(selected.get("plan_type") or "").strip(),
                    },
                    "raw": payload,
                }
            self._log(job, f"第 {current} 次 token 验证未发现组织工作区", level="warning", email=email)
        return {"success": False, "error": "支付后未通过 Access Token 检测到 Team 组织工作区"}

    def _verify_team_workspace(
        self,
        job: TeamOpenJob,
        *,
        email: str,
        password: str,
        email_service,
        runtime: dict[str, Any],
        proxy_url: str,
    ) -> dict[str, Any]:
        retries = int(runtime.get("team_open_verification_retries") or 3)
        wait_seconds = int(runtime.get("team_open_post_payment_wait_seconds") or 30)
        for current in range(1, retries + 1):
            self._check_stop(job)
            if current == 1 and wait_seconds > 0:
                self._log(job, f"支付提交后等待 {wait_seconds} 秒再验证 Team 工作区", email=email)
                for _ in range(wait_seconds):
                    self._check_stop(job)
                    time.sleep(1)
            elif current > 1:
                time.sleep(5)
            auth_client = ChatGPTWebAuthClient(
                config=runtime,
                proxy=proxy_url or None,
                verbose=False,
                browser_mode=self._normalize_executor(job.request_payload.get("executor_type")),
            )
            auth_client._log = lambda _message: None
            session_payload = auth_client.login_and_get_session(
                email=email,
                password=password,
                skymail_client=email_service,
                login_source="team_open_verify",
                require_web_session=False,
            )
            if not session_payload.get("success"):
                self._log(job, f"验证 Team 工作区失败（第 {current} 次）: {session_payload.get('error')}", level="warning", email=email)
                continue
            team_workspace = self._extract_team_workspace(session_payload)
            if not team_workspace.get("id"):
                self._log(job, f"第 {current} 次验证未发现组织工作区", level="warning", email=email)
                continue
            payload = {
                "user": {"email": email},
                "account": {"id": str(team_workspace.get("id") or "")},
                "accessToken": str(session_payload.get("access_token") or ""),
                "sessionToken": str(session_payload.get("next_auth_session_token") or ""),
                "refreshToken": str(session_payload.get("refresh_token") or ""),
            }
            import_result = batch_import_codex_team_parent_accounts(json.dumps(payload, ensure_ascii=False), enabled=True)
            imported_accounts = list(import_result.get("accounts") or [])
            imported_parent_id = int((imported_accounts[0] or {}).get("id") or 0) if imported_accounts else 0
            if imported_parent_id > 0:
                try:
                    codex_team_manager.sync_parent_account_child_count(imported_parent_id, merged_config=runtime, executor_type=self._normalize_executor(job.request_payload.get("executor_type")))
                except Exception:
                    pass
            return {
                "success": True,
                "session": session_payload,
                "team_workspace": team_workspace,
                "imported_parent_id": imported_parent_id,
            }
        return {"success": False, "error": "支付后未检测到 Team 组织工作区"}

    def _process_attempt(self, job: TeamOpenJob, attempt_index: int, card_data: dict[str, Any]) -> None:
        runtime = dict(job.merged_config or {})
        runtime["executor_type"] = self._normalize_executor(job.request_payload.get("executor_type"))
        card = self._build_card_profile(card_data, runtime)
        email = ""
        proxy_url, _proxy_id = self._resolve_proxy(runtime)
        if proxy_url:
            runtime["proxy"] = proxy_url
            runtime["team_open_proxy"] = proxy_url
        runtime["use_proxy"] = bool(proxy_url)
        try:
            self._check_stop(job)
            self._log(job, f"开始第 {attempt_index}/{job.total} 个 Team 母号开通任务" + (f"，银行卡={card.label}" if card.id else ""), email=email)

            email_service, reg_result = self._register_account(
                job,
                runtime=runtime,
                proxy_url=proxy_url,
                card=card,
                attempt_index=attempt_index,
            )
            email = str(reg_result.email or "")
            access_token = str(reg_result.access_token or "").strip()
            session_payload = {
                "access_token": access_token,
                "session_token": str(reg_result.session_token or ""),
                "account_id": str(reg_result.account_id or ""),
                "selected_workspace_id": str(reg_result.workspace_id or reg_result.account_id or ""),
                "email": email,
            }
            self._log(job, f"母号注册完成并已取得 ChatGPT accessToken: {email}", email=email)
            upsert_team_open_result(
                job.id,
                attempt_index,
                email=email,
                password=str(reg_result.password or ""),
                status="registered_token_ready",
                card_id=card.id,
                card_label=card.label,
                precheck_status="pending" if bool(runtime.get("team_open_precheck_enabled", True)) else "skipped",
                extra_json={"flow": "register_then_team_payment_link", "registration": dict(reg_result.metadata or {})},
            )



            if bool(runtime.get("team_open_precheck_enabled", True)):
                register_session = getattr(reg_result, "session", None)
                if register_session is None:
                    raise RuntimeError("无法执行 Pro 预检：注册会话为空")

                class _SessionAuthClient:
                    def __init__(self, session):
                        self.session = session

                payment_client = ChatGPTPaymentClient(
                    _SessionAuthClient(register_session),
                    session_payload,
                    log_fn=lambda message: self._log(job, message, email=email),
                )
                precheck_attempts = int(runtime.get("team_open_precheck_attempts") or 2)
                for current in range(1, precheck_attempts + 1):
                    self._check_stop(job)
                    checkout = payment_client.create_pro_checkout(
                        country=str(runtime.get("team_open_pro_checkout_country") or "US"),
                        currency=str(runtime.get("team_open_pro_checkout_currency") or "USD"),
                    )
                    self._log(job, f"Pro 预检第 {current}/{precheck_attempts} 次，checkout={checkout['checkout_session_id']}", email=email)
                    self._log(job, "[Stripe] Pro 预检 checkout 浏览器默认直连（不使用注册代理）", email=email)
                    pay_result = submit_stripe_checkout(
                        checkout["checkout_url"],
                        card=card,
                        browser_mode="headed" if runtime["executor_type"] == "headed" else "headless",
                        proxy_url=None,
                        session=register_session,
                        user_agent=str((getattr(register_session, 'headers', {}) or {}).get('User-Agent') or ''),
                        chatgpt_session_token=str(reg_result.session_token or ""),
                        chatgpt_account_id=str(reg_result.account_id or reg_result.workspace_id or ""),
                        expect_decline=True,
                        log_fn=lambda message: self._log(job, message, email=email),
                    )
                    if not bool(pay_result.get("declined")):
                        raise RuntimeError(str(pay_result.get("error") or "Pro 预检未出现预期错误"))
                    upsert_team_open_result(job.id, attempt_index, precheck_status=f"declined:{current}")
                    self._log(job, f"Pro 预检第 {current} 次已出现预期错误，继续", level="warning", email=email)
            payment_link = self._generate_payment_link(job, runtime=runtime, session_payload=session_payload, proxy_url=proxy_url, email=email)
            payment_url = str(payment_link.get("payment_url") or "").strip()
            upsert_team_open_result(
                job.id,
                attempt_index,
                status="payment_link_ready",
                payment_status="link_generated",
                payment_service=str(runtime.get("team_open_payment_service_base_url") or ""),
                payment_url=payment_url,
                extra_json={"checkout_session_id": payment_link.get("checkout_session_id"), "raw": payment_link.get("raw")},
            )

            if not bool(runtime.get("team_open_auto_submit_payment", True)):
                upsert_team_open_result(job.id, attempt_index, status="manual_review", error="已生成支付链接，自动绑卡已关闭")
                with job.lock:
                    job.success += 1
                    job.completed += 1
                self._log(job, f"已生成 Team 支付链接，因关闭自动绑卡而停止: {payment_url[:120]}", email=email)
                return

            self._log(job, "[Stripe] Team 绑卡 checkout 浏览器使用注册代理，并强制 headed", email=email)
            pay_result = submit_stripe_checkout(
                payment_url,
                card=card,
                browser_mode="headed",
                proxy_url=proxy_url or None,
                expect_decline=False,
                chatgpt_session_token=str(reg_result.session_token or ""),
                chatgpt_account_id=str(reg_result.account_id or reg_result.workspace_id or ""),
                log_fn=lambda message: self._log(job, message, email=email),
            )
            if bool(pay_result.get("declined")):
                raise RuntimeError(str(pay_result.get("error") or "Team 支付链接绑卡失败"))
            upsert_team_open_result(job.id, attempt_index, status="payment_submitted", payment_status="submitted")

            verification = self._verify_team_workspace_by_access_token(
                job,
                access_token=access_token,
                runtime=runtime,
                proxy_url=proxy_url,
                email=email,
            )
            if not verification.get("success"):
                raise RuntimeError(str(verification.get("error") or "支付后验证 Team 工作区失败"))
            team_workspace = dict(verification.get("team_workspace") or {})
            upsert_team_open_result(
                job.id,
                attempt_index,
                status="success",
                payment_status="verified",
                team_account_id=str(team_workspace.get("id") or ""),
                team_name=str(team_workspace.get("name") or ""),
                imported_parent_id=0,
                error="",
            )
            if card.id:
                mark_team_open_card_usage(card.id, success=True)
            with job.lock:
                job.success += 1
                job.completed += 1
            self._log(job, f"Team 母号开通成功，workspace={team_workspace.get('id') or '-'}；未执行 Codex 授权/导入", email=email)
        except Exception as exc:
            message = str(exc or "Team 母号开通失败")
            upsert_team_open_result(
                job.id,
                attempt_index,
                status="failed",
                error=message,
                card_id=card.id,
                card_label=card.label,
                email=email,
            )
            if card.id:
                mark_team_open_card_usage(card.id, success=False, error=message)
            with job.lock:
                job.failed += 1
                job.completed += 1
            self._log(job, message, level="error", email=email)
        finally:
            with job.lock:
                job.progress = f"{job.completed}/{job.total}"
                update_team_open_job(job.id, success=job.success, failed=job.failed, completed=job.completed, progress=job.progress)

    def _run_job(self, job: TeamOpenJob) -> None:
        self._set_job_state(job, status="running")
        cards = list_team_open_cards(enabled_only=True, include_sensitive=True) if job.merged_config.get("team_open_auto_submit_payment") else []
        total = max(1, int(job.request_payload.get("count") or 1))
        max_workers = max(1, min(int(job.request_payload.get("concurrency") or 1), 20))
        self._log(job, f"开始执行 Team 母号开通任务：总数 {total}，并发 {max_workers}")
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = []
            for attempt_index in range(1, total + 1):
                if job.stop_requested:
                    break
                card_data = cards[(attempt_index - 1) % len(cards)] if cards else {}
                futures.append(executor.submit(self._process_attempt, job, attempt_index, card_data))
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as exc:
                    self._log(job, f"worker 异常: {exc}", level="error")
        final_status = "stopped" if job.stop_requested else ("done" if job.success > 0 or job.failed == 0 else "failed")
        self._set_job_state(job, status=final_status)

    def stop_job(self, job_id: str) -> dict[str, Any]:
        job = self._jobs.get(str(job_id))
        if job:
            job.stop_requested = True
            self._set_job_state(job, status="stopped")
            return {"ok": True}
        snapshot = get_team_open_job(str(job_id))
        if not snapshot:
            return {"ok": False, "reason": "job_not_found"}
        update_team_open_job(str(job_id), status="stopped")
        return {"ok": True, "detached": True}

    def get_job_snapshot(self, job_id: str) -> dict[str, Any] | None:
        snapshot = get_team_open_job(str(job_id))
        if not snapshot:
            return None
        snapshot["events"] = list_team_open_job_events(str(job_id), limit=500)
        snapshot["results"] = list_team_open_results(str(job_id))
        snapshot["card_summary"] = get_team_open_card_summary(limit=100)
        return snapshot

    def list_jobs(self, *, limit: int = 20) -> list[dict[str, Any]]:
        return list_team_open_jobs(limit=limit)


team_open_manager = TeamOpenManager()
