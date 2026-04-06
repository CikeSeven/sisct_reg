from __future__ import annotations

import json
import threading
import time
import re
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

import requests

from core.proxy_utils import build_requests_proxy_config, isolate_proxy_session, proxy_usage_context, tracked_request
from core.task_runtime import (
    AttemptOutcome,
    AttemptResult,
    DeferAttemptRequested,
    RegisterTaskStore,
    SkipCurrentAttemptRequested,
    StopCurrentAttemptRequested,
    StopTaskRequested,
)
from platforms.chatgpt.refresh_token_registration_engine import RefreshTokenRegistrationEngine

from .db import (
    acquire_proxy_pool_entry,
    append_task_event,
    get_proxy_pool_summary,
    count_task_runs,
    create_task_run,
    delete_task_result,
    delete_task_account,
    get_config,
    get_task_events,
    get_task_result,
    get_task_results,
    get_task_run,
    insert_task_result,
    list_task_runs,
    list_enabled_proxy_pool,
    parse_config_row_values,
    update_proxy_check_result,
    update_task_run,
)
from .defaults import DEFAULT_CONFIG
from .external_uploads import (
    build_sub2api_export_payload,
    generate_cpa_token_json,
    sync_chatgpt_result,
    upload_to_cpa,
    upload_to_sub2api,
)
from .mail_providers import build_mail_provider
from .schemas import CreateRegisterTaskRequest


@dataclass
class QueuedAttempt:
    attempt_index: int
    req_overrides: dict[str, Any] = field(default_factory=dict)
    merged_config_overrides: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)
    not_before: float = 0.0


@dataclass
class TaskExecutionState:
    total: int
    completed: int = 0
    success: int = 0
    failed: int = 0
    skipped: int = 0
    pending_attempts: list[QueuedAttempt] = field(default_factory=list)
    queued_indexes: set[int] = field(default_factory=set)
    active_indexes: set[int] = field(default_factory=set)
    initial_enqueued: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    condition: threading.Condition = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.condition = threading.Condition(self.lock)

    def enqueue(self, item: QueuedAttempt) -> bool:
        with self.condition:
            if item.attempt_index in self.queued_indexes or item.attempt_index in self.active_indexes:
                return False
            self.pending_attempts.append(item)
            self.queued_indexes.add(item.attempt_index)
            self.condition.notify_all()
            return True

    def mark_initial_enqueued(self) -> None:
        with self.condition:
            self.initial_enqueued = True
            self.condition.notify_all()

    def get_next(self, *, stop_requested: bool) -> QueuedAttempt | None:
        with self.condition:
            while True:
                if self.pending_attempts:
                    now = time.time()
                    ready_index = next(
                        (idx for idx, item in enumerate(self.pending_attempts) if float(getattr(item, "not_before", 0.0) or 0.0) <= now),
                        None,
                    )
                    if ready_index is not None:
                        item = self.pending_attempts.pop(ready_index)
                        self.queued_indexes.discard(item.attempt_index)
                        self.active_indexes.add(item.attempt_index)
                        return item
                if stop_requested or (self.initial_enqueued and not self.active_indexes):
                    if not self.pending_attempts:
                        return None
                next_ready_in = None
                if self.pending_attempts:
                    now = time.time()
                    next_ready_at = min(float(getattr(item, "not_before", 0.0) or 0.0) for item in self.pending_attempts)
                    next_ready_in = max(0.0, next_ready_at - now)
                self.condition.wait(timeout=min(0.25, next_ready_in) if next_ready_in is not None else 0.25)

    def finish_attempt(self, attempt_index: int) -> None:
        with self.condition:
            self.active_indexes.discard(int(attempt_index))
            self.condition.notify_all()

    def snapshot_counts(self) -> tuple[int, int, int, int]:
        with self.lock:
            return self.completed, self.success, self.failed, self.skipped

    def apply_outcome(self, outcome: AttemptOutcome) -> tuple[int, int, int, int]:
        with self.lock:
            if outcome == AttemptOutcome.SUCCESS:
                self.success += 1
                self.completed += 1
            elif outcome == AttemptOutcome.FAILED:
                self.failed += 1
                self.completed += 1
            elif outcome in {AttemptOutcome.SKIPPED, AttemptOutcome.STOPPED}:
                self.skipped += 1
                self.completed += 1
            return self.completed, self.success, self.failed, self.skipped

    def rewind_for_retry(self, previous_status: str) -> tuple[int, int, int, int]:
        status = str(previous_status or "").strip().lower()
        with self.lock:
            if status == "success" and self.success > 0:
                self.success -= 1
                self.completed = max(0, self.completed - 1)
            elif status == "failed" and self.failed > 0:
                self.failed -= 1
                self.completed = max(0, self.completed - 1)
            elif status in {"stopped", "skipped"} and self.skipped > 0:
                self.skipped -= 1
                self.completed = max(0, self.completed - 1)
            return self.completed, self.success, self.failed, self.skipped


class RegistrationManager:
    PROXY_TEST_TIMEOUT_SECONDS = 10

    def __init__(self):
        self._task_store = RegisterTaskStore(max_finished_tasks=200, cleanup_threshold=240)
        self._log_sequences: dict[str, int] = {}
        self._task_accounts: dict[str, dict[int, dict[str, Any]]] = {}
        self._task_execution_states: dict[str, TaskExecutionState] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _stage_label(stage: str) -> str:
        mapping = {
            "create_email": "创建邮箱",
            "authorize_continue": "进入授权",
            "otp": "邮箱验证码",
            "about_you": "资料提交",
            "workspace_select": "工作区选择",
            "token_exchange": "令牌获取",
            "oauth_login": "OAuth 登录",
            "register_flow": "注册流程",
        }
        return mapping.get(str(stage or "").strip(), str(stage or "").strip() or "-")

    @staticmethod
    def _supports_retry(mail_provider: str, email: str) -> bool:
        return str(mail_provider or "").strip().lower() in {"luckmail", "outlook_local"}

    @staticmethod
    def _get_current_runtime_defaults() -> dict[str, Any]:
        stored = parse_config_row_values(get_config())
        merged = dict(DEFAULT_CONFIG)
        merged.update(stored)
        return merged

    @staticmethod
    def _query_egress_info(proxy_url: str | None = None, *, proxy_id: int | None = None) -> tuple[str, str]:
        proxies = build_requests_proxy_config(proxy_url)
        timeout = RegistrationManager.PROXY_TEST_TIMEOUT_SECONDS

        # 先做一次快速连通性检测
        try:
            response = tracked_request(
                requests.request,
                "GET",
                "http://httpbin.org/ip",
                proxies=proxies,
                timeout=timeout,
                proxy_id=proxy_id,
                proxy_url=proxy_url,
            )
            response.raise_for_status()
            payload = response.json() or {}
            origin = str(payload.get("origin") or "").strip()
            ip = origin.split(",")[0].strip() if origin else ""
            if not ip:
                raise RuntimeError("未获取到出口 IP")
        except Exception as exc:
            raise RuntimeError(str(exc) or "代理连通性检测失败")

        # 国家码查询尽力而为，不阻塞主流程
        try:
            geo_resp = tracked_request(
                requests.request,
                "GET",
                "http://ip-api.com/json/?fields=status,message,query,countryCode",
                proxies=proxies,
                timeout=min(5, timeout),
                proxy_id=proxy_id,
                proxy_url=proxy_url,
            )
            geo_resp.raise_for_status()
            geo_payload = geo_resp.json() or {}
            status = str(geo_payload.get("status") or "").strip().lower()
            if status != "fail":
                geo_ip = str(geo_payload.get("query") or "").strip()
                country = str(geo_payload.get("countryCode") or "").strip().upper()
                if geo_ip:
                    ip = geo_ip
                return ip, country
        except Exception:
            pass

        return ip, ""

    def _preflight_network(self, task_id: str, req: CreateRegisterTaskRequest) -> None:
        if not bool(getattr(req, "use_proxy", True)):
            return

        proxy_value = str(req.proxy or "").strip()
        if proxy_value:
            self._log(task_id, "正在检测代理连通性...")
            proxy_ip, proxy_country = self._query_egress_info(proxy_value)
            if proxy_country:
                self._log(task_id, f"代理出口: {proxy_ip} ({proxy_country})")
            else:
                self._log(task_id, f"代理出口 IP: {proxy_ip}")
            return

        if self._enabled_proxy_pool_exists():
            return

        try:
            current_ip, current_country = self._query_egress_info(None)
            if current_country:
                self._log(task_id, f"当前出口: {current_ip} ({current_country})")
            else:
                self._log(task_id, f"当前出口 IP: {current_ip}")
        except Exception as exc:
            self._log(task_id, f"出口 IP 查询失败: {exc}", level="warning")

    @staticmethod
    def _guess_attempt_index_from_message(message: str) -> int | None:
        import re

        text = str(message or "")
        match = re.search(r"开始注册第\s+(\d+)\s*/", text)
        if not match:
            return None
        try:
            value = int(match.group(1))
        except Exception:
            return None
        return value if value > 0 else None

    @staticmethod
    def _extract_stage_from_logs(logs: list[str]) -> str:
        import re

        for line in reversed(logs):
            match = re.search(r"\[stage=([^\]]+)\]", str(line or ""))
            if match:
                return str(match.group(1) or "").strip()
        return ""

    def create_task(
        self,
        req: CreateRegisterTaskRequest,
        merged_config: dict[str, Any],
        *,
        source: str = "manual",
        meta: dict[str, Any] | None = None,
    ) -> str:
        task_id = f"task_{int(time.time() * 1000)}"
        task_meta = {"mail_provider": req.mail_provider, "mode": "refresh_token"}
        task_meta.update(meta or {})
        self._task_store.create(
            task_id,
            platform="chatgpt",
            total=req.count,
            source=source,
            meta=task_meta,
        )
        request_payload = req.model_dump()
        request_payload["merged_config"] = {k: merged_config.get(k) for k in sorted(merged_config.keys())}
        request_payload["source"] = source
        request_payload["meta"] = task_meta
        create_task_run(task_id, total=req.count, request_payload=request_payload)
        threading.Thread(target=self._run_task, args=(task_id, req, merged_config), daemon=True).start()
        return task_id

    def _next_log_seq(self, task_id: str) -> int:
        with self._lock:
            seq = self._log_sequences.get(task_id, 0) + 1
            self._log_sequences[task_id] = seq
            return seq

    def _upsert_task_account(
        self,
        task_id: str,
        attempt_index: int,
        *,
        email: str | None = None,
        status: str | None = None,
        error: str | None = None,
    ) -> None:
        now = time.time()
        with self._lock:
            task_accounts = self._task_accounts.setdefault(task_id, {})
            item = task_accounts.setdefault(
                attempt_index,
                {
                    "attempt_index": attempt_index,
                    "email": "",
                    "label": f"第 {attempt_index} 个账号",
                    "status": "registering",
                    "error": "",
                    "logs": [],
                    "created_at": now,
                    "updated_at": now,
                },
            )
            if email:
                item["email"] = str(email).strip()
                item["label"] = item["email"] or item["label"]
            if status:
                item["status"] = status
            if error is not None:
                item["error"] = str(error)
            item["updated_at"] = now

    def _append_task_account_log(
        self,
        task_id: str,
        attempt_index: int,
        message: str,
        *,
        discovered_email: str | None = None,
    ) -> None:
        self._upsert_task_account(task_id, attempt_index, email=discovered_email)
        with self._lock:
            item = self._task_accounts.setdefault(task_id, {}).setdefault(
                attempt_index,
                {
                    "attempt_index": attempt_index,
                    "email": "",
                    "label": f"第 {attempt_index} 个账号",
                    "status": "registering",
                    "error": "",
                    "logs": [],
                    "created_at": time.time(),
                    "updated_at": time.time(),
                },
            )
            item["logs"].append(message)
            item["logs"] = item["logs"][-120:]
            item["updated_at"] = time.time()

    def _set_task_execution_state(self, task_id: str, state: TaskExecutionState | None) -> None:
        with self._lock:
            if state is None:
                self._task_execution_states.pop(task_id, None)
            else:
                self._task_execution_states[task_id] = state

    def _get_task_execution_state(self, task_id: str) -> TaskExecutionState | None:
        with self._lock:
            return self._task_execution_states.get(task_id)

    def _sync_task_progress(
        self,
        task_id: str,
        state: TaskExecutionState,
        *,
        status: str | None = None,
        error: str | None = None,
        summary_total: int | None = None,
    ) -> tuple[int, int, int, int]:
        completed, success, failed, skipped = state.snapshot_counts()
        progress = f"{completed}/{state.total}"
        self._task_store.set_progress(task_id, progress)
        payload: dict[str, Any] = {
            "progress": progress,
            "success": success,
            "failed": failed,
            "skipped": skipped,
        }
        if status is not None:
            payload["status"] = status
        if error is not None:
            payload["error"] = error
        if summary_total is not None:
            payload["summary_json"] = {
                "success": success,
                "failed": failed,
                "skipped": skipped,
                "total": summary_total,
            }
        update_task_run(task_id, **payload)
        return completed, success, failed, skipped

    @staticmethod
    def _enabled_proxy_pool_exists() -> bool:
        summary = get_proxy_pool_summary(limit=1)
        return int(summary.get("enabled") or 0) > 0

    def _acquire_checked_proxy_for_attempt(
        self,
        *,
        task_id: str,
        attempt_index: int,
        attempt_log,
        max_candidates: int = 3,
    ) -> tuple[str | None, int | None]:
        tried_proxy_ids: list[int] = []
        enabled_count = len(list_enabled_proxy_pool())
        candidate_limit = max(1, enabled_count or 1)
        last_error = ""

        for _ in range(candidate_limit):
            entry = acquire_proxy_pool_entry(exclude_ids=tried_proxy_ids)
            if not entry:
                break
            proxy_id = int(entry.get("id") or 0)
            proxy_url = str(entry.get("proxy_url") or "").strip()
            tried_proxy_ids.append(proxy_id)
            if not proxy_url:
                continue
            try:
                attempt_log("正在检测代理连通性...")
                proxy_ip, proxy_country = self._query_egress_info(proxy_url, proxy_id=proxy_id)
                update_proxy_check_result(proxy_id, ok=True, message="ok", ip=proxy_ip, country=proxy_country)
                if proxy_country:
                    attempt_log(f"代理出口: {proxy_ip} ({proxy_country})")
                else:
                    attempt_log(f"代理出口 IP: {proxy_ip}")
                return proxy_url, proxy_id
            except Exception as exc:
                last_error = str(exc)
                update_proxy_check_result(proxy_id, ok=False, message=last_error)
                attempt_log(f"代理检测失败: {last_error}", level="error")
                if len(tried_proxy_ids) < candidate_limit:
                    attempt_log("切换下一个代理继续尝试")

        if last_error:
            raise RuntimeError(f"代理检测失败: {last_error}")
        return None, None

    def _queue_retry_into_active_task(
        self,
        task_id: str,
        *,
        attempt_index: int,
        current_status: str,
        email: str,
        retry_stage: str,
        retry_origin: str,
        current_proxy: str | None,
        retry_email_binding: dict[str, Any] | None = None,
        use_proxy: bool = True,
    ) -> bool:
        state = self._get_task_execution_state(task_id)
        if state is None:
            return False

        delete_task_result(task_id, int(attempt_index))
        state.rewind_for_retry(current_status)
        self._sync_task_progress(task_id, state, status="running")
        self._upsert_task_account(
            task_id,
            int(attempt_index),
            email=email or "",
            status="pending",
            error="",
        )
        self._log(task_id, "已加入等待队列", attempt_index=int(attempt_index))
        queued = state.enqueue(
            QueuedAttempt(
                attempt_index=int(attempt_index),
                req_overrides={
                    "email": email or None,
                    "password": None,
                    "proxy": current_proxy,
                    "use_proxy": bool(use_proxy),
                },
                merged_config_overrides={
                    "retry_resume_stage": retry_stage,
                    "retry_resume_origin": retry_origin,
                    "retry_from_task_id": task_id,
                    "retry_from_attempt_index": int(attempt_index),
                    "retry_email_binding": dict(retry_email_binding or {}),
                },
            )
        )
        if queued:
            return True

        self._upsert_task_account(
            task_id,
            int(attempt_index),
            email=email or "",
            status=current_status,
        )
        return False

    @staticmethod
    def _guess_email_from_message(message: str) -> str:
        import re

        text = str(message or "")
        match = re.search(r"([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})", text)
        return str(match.group(1) if match else "").strip()

    def _get_live_accounts(self, task_id: str) -> list[dict[str, Any]]:
        with self._lock:
            task_accounts = self._task_accounts.get(task_id, {})
            items = [deepcopy(item) for item in task_accounts.values()]
        active_statuses = {"pending", "registering", "running"}
        items.sort(key=lambda item: (0 if item.get("status") in active_statuses else 1, item.get("attempt_index", 0)))
        return items

    def _build_db_accounts(self, task_id: str, *, request_payload: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        request_payload = request_payload or {}
        task_row = get_task_run(task_id) or {}
        task_status = str(task_row.get("status") or "")
        task_total = int(task_row.get("total") or request_payload.get("count") or 0)
        mail_provider = str(
            request_payload.get("mail_provider")
            or ((request_payload.get("merged_config") or {}).get("mail_provider") if isinstance(request_payload.get("merged_config"), dict) else "")
            or ""
        ).strip()
        logs_by_attempt: dict[int, list[str]] = {}
        email_by_attempt: dict[int, str] = {}
        current_attempt_index: int | None = None
        all_events = get_task_events(task_id, after_seq=0)
        for event in all_events:
            attempt_index = event.get("attempt_index")
            message = str(event.get("message") or "")
            if attempt_index is None:
                attempt_index = self._guess_attempt_index_from_message(message)
                if attempt_index is not None:
                    current_attempt_index = int(attempt_index)
                else:
                    attempt_index = current_attempt_index
            else:
                try:
                    attempt_index = int(attempt_index)
                    current_attempt_index = int(attempt_index)
                except Exception:
                    attempt_index = current_attempt_index
            if attempt_index is None:
                if task_total == 1 and all_events:
                    attempt_index = 1
                    current_attempt_index = 1
                else:
                    continue
            idx = int(attempt_index)
            logs_by_attempt.setdefault(idx, []).append(str(event.get("message") or ""))
            guessed_email = self._guess_email_from_message(message)
            if guessed_email and idx not in email_by_attempt:
                email_by_attempt[idx] = guessed_email

        items: list[dict[str, Any]] = []
        result_attempts: set[int] = set()
        for result in get_task_results(task_id):
            extra = result.get("extra_json") or {}
            metadata = extra.get("metadata") if isinstance(extra.get("metadata"), dict) else {}
            failure_stage = str(extra.get("failure_stage") or metadata.get("failure_stage") or "").strip()
            failure_origin = str(extra.get("failure_origin") or metadata.get("failure_origin") or "").strip()
            failure_detail = str(extra.get("failure_detail") or metadata.get("failure_detail") or result.get("error") or "").strip()
            retry_email_binding = (
                dict(metadata.get("email_binding") or {})
                if isinstance(metadata.get("email_binding"), dict)
                else {}
            )
            retry_supported = bool(
                extra.get("retry_supported")
                if "retry_supported" in extra
                else metadata.get("resume_supported")
            )
            if not retry_supported and str(result.get("status") or "") == "stopped":
                retry_supported = True
            if not retry_supported and str(result.get("status") or "") == "failed" and (
                failure_stage == "network_precheck" or bool(retry_email_binding)
            ):
                retry_supported = True
            attempt_index = int(result.get("attempt_index") or 0)
            result_attempts.add(attempt_index)
            email = str(result.get("email") or retry_email_binding.get("email") or email_by_attempt.get(attempt_index) or "").strip()
            items.append(
                {
                    "id": result.get("id"),
                    "attempt_index": attempt_index,
                    "email": email,
                    "label": email or f"第 {attempt_index} 个账号",
                    "status": str(result.get("status") or ""),
                    "error": str(result.get("error") or ""),
                    "logs": logs_by_attempt.get(attempt_index, []),
                    "created_at": result.get("created_at") or 0,
                    "updated_at": result.get("created_at") or 0,
                    "flow_status": "success" if result.get("status") == "success" else "failed",
                    "failure_stage": failure_stage,
                    "failure_stage_label": self._stage_label(failure_stage),
                    "failure_origin": failure_origin,
                    "failure_detail": failure_detail,
                    "retry_supported": bool(retry_supported) and self._supports_retry(mail_provider, email),
                    "retry_email_binding": retry_email_binding,
                }
            )

        fallback_status = "failed" if task_status == "failed" else ("stopped" if task_status == "stopped" else "registering")
        for attempt_index, logs in sorted(logs_by_attempt.items()):
            if attempt_index in result_attempts:
                continue
            email = str(email_by_attempt.get(attempt_index) or "").strip()
            failure_stage = self._extract_stage_from_logs(logs)
            failure_detail = ""
            for line in reversed(logs):
                text = str(line or "").strip()
                if "失败" in text or "异常" in text or "超时" in text or "错误" in text:
                    failure_detail = text
                    break
            items.append(
                {
                    "id": None,
                    "attempt_index": int(attempt_index),
                    "email": email,
                    "label": email or f"第 {attempt_index} 个账号",
                    "status": fallback_status,
                    "error": failure_detail if fallback_status in {"failed", "stopped"} else "",
                    "logs": logs,
                    "created_at": 0,
                    "updated_at": 0,
                    "flow_status": "failed" if fallback_status in {"failed", "stopped"} else "running",
                    "failure_stage": failure_stage,
                    "failure_stage_label": self._stage_label(failure_stage),
                    "failure_origin": "",
                    "failure_detail": failure_detail,
                    "retry_supported": fallback_status in {"failed", "stopped"} and self._supports_retry(mail_provider, email),
                }
            )

        if not items and task_total == 1 and all_events:
            items.append(
                {
                    "id": None,
                    "attempt_index": 1,
                    "email": "",
                    "label": "第 1 个账号",
                    "status": fallback_status,
                    "error": "",
                    "logs": [str(event.get("message") or "") for event in all_events],
                    "created_at": 0,
                    "updated_at": 0,
                    "flow_status": "failed" if fallback_status in {"failed", "stopped"} else "running",
                    "failure_stage": "",
                    "failure_stage_label": "-",
                    "failure_origin": "",
                    "failure_detail": "",
                    "retry_supported": fallback_status in {"failed", "stopped"} and self._supports_retry(mail_provider, ""),
                }
            )
        active_statuses = {"pending", "registering", "running"}
        items.sort(key=lambda item: (0 if item.get("status") in active_statuses else 1, item.get("attempt_index", 0)))
        return items

    def _merged_accounts_snapshot(self, task_id: str, *, request_payload: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        db_items = self._build_db_accounts(task_id, request_payload=request_payload)
        live_items = self._get_live_accounts(task_id)
        if not live_items:
            return db_items
        db_map = {int(item.get("attempt_index") or 0): item for item in db_items}
        merged: list[dict[str, Any]] = []
        seen: set[int] = set()
        for item in live_items:
            idx = int(item.get("attempt_index") or 0)
            merged_item = deepcopy(db_map.get(idx) or {})
            merged_item.update(item)
            if idx in db_map:
                merged_item.setdefault("id", db_map[idx].get("id"))
                merged_item.setdefault("flow_status", db_map[idx].get("flow_status"))
                merged_item.setdefault("failure_stage", db_map[idx].get("failure_stage"))
                merged_item.setdefault("failure_stage_label", db_map[idx].get("failure_stage_label"))
                merged_item.setdefault("failure_origin", db_map[idx].get("failure_origin"))
                merged_item.setdefault("failure_detail", db_map[idx].get("failure_detail"))
                merged_item.setdefault("retry_supported", db_map[idx].get("retry_supported"))
            seen.add(idx)
            merged.append(merged_item)
        for item in db_items:
            idx = int(item.get("attempt_index") or 0)
            if idx not in seen:
                merged.append(item)
        active_statuses = {"pending", "registering", "running"}
        merged.sort(key=lambda item: (0 if item.get("status") in active_statuses else 1, item.get("attempt_index", 0)))
        return merged

    def _clear_live_accounts(self, task_id: str) -> None:
        with self._lock:
            self._task_accounts.pop(task_id, None)

    def _log(self, task_id: str, message: str, *, level: str = "info", attempt_index: int | None = None) -> None:
        import re

        text = str(message or "").strip()
        if re.match(r"^\[\d{2}:\d{2}:\d{2}\]\s+", text):
            entry = text
        else:
            ts = datetime.now().strftime("%H:%M:%S")
            entry = f"[{ts}] {text}"
        self._task_store.append_log(task_id, entry)
        append_task_event(task_id, seq=self._next_log_seq(task_id), message=entry, level=level, attempt_index=attempt_index)
        if attempt_index is not None:
            self._append_task_account_log(
                task_id,
                attempt_index,
                entry,
                discovered_email=self._guess_email_from_message(text),
            )

    def _ensure_task_exists(self, task_id: str) -> None:
        if self._task_store.exists(task_id):
            return
        if get_task_run(task_id) is None:
            raise KeyError(task_id)

    def get_task_snapshot(self, task_id: str) -> dict[str, Any] | None:
        db_task = get_task_run(task_id)
        request_payload = db_task.get("request_json") or {} if db_task is not None else {}
        source = str(request_payload.get("source") or "manual")
        meta = request_payload.get("meta") if isinstance(request_payload.get("meta"), dict) else {}
        if self._task_store.exists(task_id):
            snapshot = self._task_store.snapshot(task_id)
            if db_task is not None:
                snapshot['progress'] = db_task.get('progress') or snapshot.get('progress')
                snapshot['success'] = db_task.get('success', snapshot.get('success', 0))
                snapshot['failed'] = db_task.get('failed', snapshot.get('failed', 0))
                snapshot['skipped'] = db_task.get('skipped', snapshot.get('skipped', 0))
                snapshot['summary'] = db_task.get('summary_json') or {}
                snapshot['source'] = source
                snapshot['meta'] = meta
                snapshot['request'] = request_payload
            snapshot['accounts'] = self._merged_accounts_snapshot(task_id, request_payload=request_payload)
            snapshot['is_active'] = True
            return snapshot
        
        if db_task is None:
            return None
        events = get_task_events(task_id, after_seq=0)
        results = get_task_results(task_id)
        return {
            "id": db_task["id"],
            "status": db_task["status"],
            "is_active": False,
            "platform": "chatgpt",
            "source": source,
            "meta": meta,
            "request": request_payload,
            "progress": db_task["progress"],
            "success": db_task["success"],
            "failed": db_task["failed"],
            "skipped": db_task["skipped"],
            "errors": [item["error"] for item in results if item.get("status") == "failed" and item.get("error")],
            "logs": [item["message"] for item in events],
            "control": {"stop_requested": db_task["status"] == "stopped"},
            "summary": db_task.get("summary_json") or {},
            "accounts": self._build_db_accounts(task_id, request_payload=db_task.get("request_json") or {}),
        }

    def list_history(self, *, page: int, page_size: int) -> dict[str, Any]:
        total = count_task_runs()
        offset = max(page - 1, 0) * page_size
        rows = list_task_runs(limit=page_size, offset=offset)
        items = []
        for row in rows:
            items.append(
                {
                    "id": row["id"],
                    "status": row["status"],
                    "progress": row["progress"],
                    "total": row["total"],
                    "success": row["success"],
                    "failed": row["failed"],
                    "skipped": row["skipped"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                    "request": row.get("request_json") or {},
                    "summary": row.get("summary_json") or {},
                    "error": row.get("error") or "",
                }
            )
        return {"total": total, "items": items}

    def get_history_detail(self, task_id: str) -> dict[str, Any] | None:
        row = get_task_run(task_id)
        if row is None:
            return None
        return {"task": row, "events": get_task_events(task_id, after_seq=0), "results": get_task_results(task_id)}

    def export_results(self, task_id: str) -> list[dict[str, Any]]:
        exported = []
        for item in get_task_results(task_id):
            if item.get("status") != "success":
                continue
            exported.append(
                {
                    "email": item.get("email") or "",
                    "password": item.get("password") or "",
                    "access_token": item.get("access_token") or "",
                    "refresh_token": item.get("refresh_token") or "",
                    "session_token": item.get("session_token") or "",
                    "workspace_id": item.get("workspace_id") or "",
                    "extra": item.get("extra_json") or {},
                }
            )
        return exported

    def retry_result(self, result_id: int) -> dict[str, Any]:
        result = get_task_result(result_id)
        if result is None:
            return {"ok": False, "reason": "result_not_found"}
        if str(result.get("status") or "") not in {"failed", "stopped"}:
            return {"ok": False, "reason": "result_not_failed"}

        task = get_task_run(str(result.get("task_id") or ""))
        if task is None:
            return {"ok": False, "reason": "task_not_found"}

        request_payload = task.get("request_json") or {}
        merged_config = deepcopy(request_payload.get("merged_config") or {})
        extra = result.get("extra_json") or {}
        metadata = extra.get("metadata") if isinstance(extra.get("metadata"), dict) else {}
        failure_stage = str(extra.get("failure_stage") or metadata.get("failure_stage") or "").strip()
        failure_origin = str(extra.get("failure_origin") or metadata.get("failure_origin") or "").strip()
        retry_email_binding = dict(metadata.get("email_binding") or {}) if isinstance(metadata.get("email_binding"), dict) else {}
        mail_provider = str(
            request_payload.get("mail_provider")
            or extra.get("mail_provider")
            or merged_config.get("mail_provider")
            or "luckmail"
        ).strip()
        email = str(result.get("email") or "").strip()
        if not self._supports_retry(mail_provider, email):
            return {"ok": False, "reason": "retry_not_supported"}

        current_defaults = self._get_current_runtime_defaults()
        current_use_proxy = bool(current_defaults.get("use_proxy", True))
        current_proxy = None
        if current_use_proxy and not self._enabled_proxy_pool_exists():
            current_proxy = str(current_defaults.get("proxy") or "").strip() or None
        source_task_id = str(result.get("task_id") or "").strip()
        source_task_row = get_task_run(source_task_id) if source_task_id else None

        if (
            source_task_id
            and self._task_store.exists(source_task_id)
            and source_task_row is not None
            and str(source_task_row.get("status") or "").strip().lower() in {"pending", "running"}
        ):
            queued = self._queue_retry_into_active_task(
                source_task_id,
                attempt_index=int(result.get("attempt_index") or 0),
                current_status=str(result.get("status") or ""),
                email=email,
                retry_stage=failure_stage,
                retry_origin=failure_origin,
                current_proxy=current_proxy,
                retry_email_binding=retry_email_binding,
                use_proxy=current_use_proxy,
            )
            if queued:
                return {"ok": True, "task_id": source_task_id, "queued": True}

        req = CreateRegisterTaskRequest(
            count=1,
            concurrency=1,
            register_delay_seconds=0,
            email=email,
            password=str(result.get("password") or "") or None,
            proxy=current_proxy,
            use_proxy=current_use_proxy,
            executor_type=request_payload.get("executor_type") or merged_config.get("executor_type") or "protocol",
            mail_provider=mail_provider,
            provider_config=request_payload.get("provider_config") or {},
            phone_config=request_payload.get("phone_config") or {},
        )
        retry_from_attempt_index = int(result.get("attempt_index") or 0)
        merged_config["retry_resume_stage"] = failure_stage
        merged_config["retry_resume_origin"] = failure_origin
        merged_config["retry_from_result_id"] = int(result_id)
        merged_config["retry_from_task_id"] = str(result.get("task_id") or "")
        merged_config["retry_from_attempt_index"] = retry_from_attempt_index
        merged_config["retry_email_binding"] = retry_email_binding
        task_id = self.create_task(
            req,
            merged_config,
            source="retry",
            meta={
                "retry_from_result_id": int(result_id),
                "retry_from_task_id": str(result.get("task_id") or ""),
                "retry_from_attempt_index": retry_from_attempt_index,
                "retry_stage": failure_stage,
            },
        )
        return {"ok": True, "task_id": task_id}

    def retry_attempt(self, task_id: str, attempt_index: int) -> dict[str, Any]:
        task = get_task_run(task_id)
        if task is None:
            return {"ok": False, "reason": "task_not_found"}

        request_payload = task.get("request_json") or {}
        merged_config = deepcopy(request_payload.get("merged_config") or {})
        mail_provider = str(
            request_payload.get("mail_provider")
            or merged_config.get("mail_provider")
            or "luckmail"
        ).strip()
        accounts = self._build_db_accounts(task_id, request_payload=request_payload)
        target = next((item for item in accounts if int(item.get("attempt_index") or 0) == int(attempt_index)), None)
        if target is None:
            return {"ok": False, "reason": "account_not_found"}
        if str(target.get("status") or "") not in {"failed", "stopped"}:
            return {"ok": False, "reason": "result_not_failed"}

        email = str(target.get("email") or "").strip()
        if not self._supports_retry(mail_provider, email):
            return {"ok": False, "reason": "retry_not_supported"}

        current_defaults = self._get_current_runtime_defaults()
        current_use_proxy = bool(current_defaults.get("use_proxy", True))
        current_proxy = None
        if current_use_proxy and not self._enabled_proxy_pool_exists():
            current_proxy = str(current_defaults.get("proxy") or "").strip() or None

        if (
            self._task_store.exists(task_id)
            and str(task.get("status") or "").strip().lower() in {"pending", "running"}
        ):
            queued = self._queue_retry_into_active_task(
                task_id,
                attempt_index=int(attempt_index),
                current_status=str(target.get("status") or ""),
                email=email,
                retry_stage=str(target.get("failure_stage") or "").strip(),
                retry_origin=str(target.get("failure_origin") or "").strip(),
                current_proxy=current_proxy,
                use_proxy=current_use_proxy,
                retry_email_binding=(
                    dict(target.get("retry_email_binding") or {})
                    if isinstance(target.get("retry_email_binding"), dict)
                    else {}
                ),
            )
            if queued:
                return {"ok": True, "task_id": task_id, "queued": True}

        req = CreateRegisterTaskRequest(
            count=1,
            concurrency=1,
            register_delay_seconds=0,
            email=email or None,
            password=None,
            proxy=current_proxy,
            use_proxy=current_use_proxy,
            executor_type=request_payload.get("executor_type") or merged_config.get("executor_type") or "protocol",
            mail_provider=mail_provider,
            provider_config=request_payload.get("provider_config") or {},
            phone_config=request_payload.get("phone_config") or {},
        )
        merged_config["retry_resume_stage"] = str(target.get("failure_stage") or "").strip()
        merged_config["retry_resume_origin"] = str(target.get("failure_origin") or "").strip()
        merged_config["retry_from_task_id"] = task_id
        merged_config["retry_from_attempt_index"] = int(attempt_index)
        merged_config["retry_email_binding"] = (
            dict(target.get("retry_email_binding") or {})
            if isinstance(target.get("retry_email_binding"), dict)
            else {}
        )
        new_task_id = self.create_task(
            req,
            merged_config,
            source="retry",
            meta={
                "retry_from_task_id": task_id,
                "retry_from_attempt_index": int(attempt_index),
                "retry_stage": str(target.get("failure_stage") or "").strip(),
            },
        )
        return {"ok": True, "task_id": new_task_id}

    def delete_account(
        self,
        task_id: str,
        attempt_index: int,
        task_ids: list[str] | None = None,
        refs: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        candidate_refs: list[tuple[str, int]] = []
        seen_refs: set[tuple[str, int]] = set()

        for ref in refs or []:
            ref_payload = ref.model_dump() if hasattr(ref, "model_dump") else (dict(ref) if isinstance(ref, dict) else {})
            ref_task_id = str((ref_payload or {}).get("task_id") or "").strip()
            try:
                ref_attempt_index = int((ref_payload or {}).get("attempt_index") or 0)
            except Exception:
                ref_attempt_index = 0
            if ref_task_id and ref_attempt_index > 0 and (ref_task_id, ref_attempt_index) not in seen_refs:
                seen_refs.add((ref_task_id, ref_attempt_index))
                candidate_refs.append((ref_task_id, ref_attempt_index))

        for value in [task_id, *(task_ids or [])]:
            task_value = str(value or "").strip()
            if task_value and int(attempt_index) > 0 and (task_value, int(attempt_index)) not in seen_refs:
                seen_refs.add((task_value, int(attempt_index)))
                candidate_refs.append((task_value, int(attempt_index)))

        if not candidate_refs:
            return {"ok": False, "reason": "task_not_found"}

        found_any_task = False
        matched_refs: list[tuple[str, int]] = []
        for candidate_task_id, candidate_attempt_index in candidate_refs:
            snapshot = self.get_task_snapshot(candidate_task_id)
            if snapshot is None:
                continue
            found_any_task = True
            accounts = snapshot.get("accounts") or []
            target = next(
                (
                    item
                    for item in accounts
                    if int(item.get("attempt_index") or 0) == int(candidate_attempt_index)
                ),
                None,
            )
            if target is None:
                continue
            if str(target.get("status") or "") in {"pending", "registering", "running"}:
                return {"ok": False, "reason": "account_running"}
            matched_refs.append((candidate_task_id, int(candidate_attempt_index)))

        if not found_any_task:
            return {"ok": False, "reason": "task_not_found"}
        if not matched_refs:
            return {"ok": False, "reason": "account_not_found"}

        deleted_results = 0
        deleted_events = 0
        for candidate_task_id, candidate_attempt_index in matched_refs:
            result = delete_task_account(candidate_task_id, candidate_attempt_index)
            deleted_results += int(result.get("deleted_results") or 0)
            deleted_events += int(result.get("deleted_events") or 0)
        with self._lock:
            for candidate_task_id, candidate_attempt_index in matched_refs:
                task_accounts = self._task_accounts.get(candidate_task_id)
                if task_accounts is not None:
                    task_accounts.pop(int(candidate_attempt_index), None)
                    if not task_accounts:
                        self._task_accounts.pop(candidate_task_id, None)
        return {"ok": True, "deleted_results": deleted_results, "deleted_events": deleted_events}

    @staticmethod
    def _collect_account_candidate_refs(
        task_id: str,
        attempt_index: int,
        *,
        task_ids: list[str] | None = None,
        refs: list[dict[str, Any]] | None = None,
    ) -> list[tuple[str, int]]:
        candidate_refs: list[tuple[str, int]] = []
        seen_refs: set[tuple[str, int]] = set()

        for ref in refs or []:
            ref_payload = ref.model_dump() if hasattr(ref, "model_dump") else (dict(ref) if isinstance(ref, dict) else {})
            ref_task_id = str((ref_payload or {}).get("task_id") or "").strip()
            try:
                ref_attempt_index = int((ref_payload or {}).get("attempt_index") or 0)
            except Exception:
                ref_attempt_index = 0
            if ref_task_id and ref_attempt_index > 0 and (ref_task_id, ref_attempt_index) not in seen_refs:
                seen_refs.add((ref_task_id, ref_attempt_index))
                candidate_refs.append((ref_task_id, ref_attempt_index))

        for value in [task_id, *(task_ids or [])]:
            task_value = str(value or "").strip()
            if task_value and int(attempt_index) > 0 and (task_value, int(attempt_index)) not in seen_refs:
                seen_refs.add((task_value, int(attempt_index)))
                candidate_refs.append((task_value, int(attempt_index)))

        return candidate_refs

    @staticmethod
    def _result_to_upload_payload(row: dict[str, Any]) -> Any:
        extra = row.get("extra_json") if isinstance(row.get("extra_json"), dict) else {}
        metadata = extra.get("metadata") if isinstance(extra.get("metadata"), dict) else {}
        return SimpleNamespace(
            email=str(row.get("email") or "").strip(),
            password=str(row.get("password") or "").strip(),
            access_token=str(row.get("access_token") or "").strip(),
            refresh_token=str(row.get("refresh_token") or "").strip(),
            session_token=str(row.get("session_token") or "").strip(),
            workspace_id=str(row.get("workspace_id") or "").strip(),
            account_id=str(extra.get("account_id") or "").strip(),
            id_token=str(extra.get("id_token") or "").strip(),
            metadata=metadata,
        )

    def upload_accounts(
        self,
        target: str,
        items: list[Any],
    ) -> dict[str, Any]:
        target_name = str(target or "").strip().lower()
        if target_name not in {"cpa", "sub2api"}:
            return {"ok": False, "reason": "invalid_target"}

        config = self._get_current_runtime_defaults()
        if target_name == "cpa":
            api_url = str(config.get("cpa_api_url") or "").strip()
            api_key = str(config.get("cpa_api_key") or "").strip()
            if not api_url:
                return {"ok": False, "reason": "target_not_configured", "message": "CPA 上传未配置"}
        else:
            api_url = str(config.get("sub2api_api_url") or "").strip()
            api_key = str(config.get("sub2api_api_key") or "").strip()
            group_ids = config.get("sub2api_group_ids")
            if not api_url or not api_key:
                return {"ok": False, "reason": "target_not_configured", "message": "Sub2API 上传未配置"}

        uploaded = 0
        failed = 0
        skipped = 0
        results: list[dict[str, Any]] = []
        seen_result_ids: set[int] = set()

        for raw_item in items or []:
            item = raw_item.model_dump() if hasattr(raw_item, "model_dump") else (dict(raw_item) if isinstance(raw_item, dict) else {})
            task_id = str(item.get("task_id") or "").strip()
            try:
                attempt_index = int(item.get("attempt_index") or 0)
            except Exception:
                attempt_index = 0
            candidate_refs = self._collect_account_candidate_refs(
                task_id,
                attempt_index,
                task_ids=item.get("task_ids") if isinstance(item.get("task_ids"), list) else [],
                refs=item.get("refs") if isinstance(item.get("refs"), list) else [],
            )
            if not candidate_refs:
                skipped += 1
                results.append(
                    {
                        "task_id": task_id,
                        "attempt_index": attempt_index,
                        "ok": False,
                        "reason": "account_not_found",
                        "message": "账号不存在",
                    }
                )
                continue

            candidates: list[dict[str, Any]] = []
            for candidate_task_id, candidate_attempt_index in candidate_refs:
                task = get_task_run(candidate_task_id)
                if task is None:
                    continue
                for row in get_task_results(candidate_task_id):
                    if int(row.get("attempt_index") or 0) != int(candidate_attempt_index):
                        continue
                    if str(row.get("status") or "").strip().lower() != "success":
                        continue
                    candidates.append(row)

            if not candidates:
                skipped += 1
                results.append(
                    {
                        "task_id": task_id,
                        "attempt_index": attempt_index,
                        "ok": False,
                        "reason": "account_not_success",
                        "message": "账号未注册成功",
                    }
                )
                continue

            row = max(
                candidates,
                key=lambda value: (
                    float(value.get("created_at") or 0),
                    int(value.get("id") or 0),
                ),
            )
            row_id = int(row.get("id") or 0)
            if row_id > 0 and row_id in seen_result_ids:
                skipped += 1
                results.append(
                    {
                        "task_id": task_id,
                        "attempt_index": attempt_index,
                        "ok": False,
                        "reason": "duplicate_result",
                        "message": "已跳过重复账号",
                    }
                )
                continue
            if row_id > 0:
                seen_result_ids.add(row_id)

            payload = self._result_to_upload_payload(row)
            if target_name == "cpa":
                ok, message = upload_to_cpa(payload, api_url=api_url, api_key=api_key)
            else:
                ok, message = upload_to_sub2api(
                    payload,
                    api_url=api_url,
                    api_key=api_key,
                    group_ids=group_ids,
                )
            if ok:
                uploaded += 1
            else:
                failed += 1
            results.append(
                {
                    "task_id": str(row.get("task_id") or task_id),
                    "attempt_index": int(row.get("attempt_index") or attempt_index),
                    "result_id": row_id or None,
                    "email": str(row.get("email") or "").strip(),
                    "ok": ok,
                    "message": str(message or "").strip(),
                }
            )

        return {
            "ok": True,
            "target": target_name,
            "uploaded": uploaded,
            "failed": failed,
            "skipped": skipped,
            "items": results,
        }

    @staticmethod
    def _build_export_file_stem(email: str, created_at: float | int | None = None) -> str:
        email_text = str(email or "").strip().lower()
        sanitized_email = re.sub(r"[^a-z0-9._-]+", "_", email_text.replace("@", "_"))
        timestamp = int(float(created_at or 0) or time.time())
        return f"token_{sanitized_email}_{timestamp}"

    def export_accounts_bundle(self, items: list[Any]) -> dict[str, Any]:
        selected_rows: list[dict[str, Any]] = []
        seen_result_ids: set[int] = set()
        skipped = 0

        for raw_item in items or []:
            item = raw_item.model_dump() if hasattr(raw_item, "model_dump") else (dict(raw_item) if isinstance(raw_item, dict) else {})
            task_id = str(item.get("task_id") or "").strip()
            try:
                attempt_index = int(item.get("attempt_index") or 0)
            except Exception:
                attempt_index = 0
            candidate_refs = self._collect_account_candidate_refs(
                task_id,
                attempt_index,
                task_ids=item.get("task_ids") if isinstance(item.get("task_ids"), list) else [],
                refs=item.get("refs") if isinstance(item.get("refs"), list) else [],
            )
            candidates: list[dict[str, Any]] = []
            for candidate_task_id, candidate_attempt_index in candidate_refs:
                if get_task_run(candidate_task_id) is None:
                    continue
                for row in get_task_results(candidate_task_id):
                    if int(row.get("attempt_index") or 0) != int(candidate_attempt_index):
                        continue
                    if str(row.get("status") or "").strip().lower() != "success":
                        continue
                    candidates.append(row)
            if not candidates:
                skipped += 1
                continue

            row = max(
                candidates,
                key=lambda value: (
                    float(value.get("created_at") or 0),
                    int(value.get("id") or 0),
                ),
            )
            row_id = int(row.get("id") or 0)
            if row_id > 0 and row_id in seen_result_ids:
                skipped += 1
                continue
            if row_id > 0:
                seen_result_ids.add(row_id)
            selected_rows.append(row)

        if not selected_rows:
            return {"ok": False, "reason": "no_success_accounts", "message": "没有可导出的成功账号"}

        bundle_results = [self._result_to_upload_payload(row) for row in selected_rows]
        zip_buffer = BytesIO()
        with ZipFile(zip_buffer, "w", compression=ZIP_DEFLATED) as archive:
            for row, payload in zip(selected_rows, bundle_results):
                file_stem = self._build_export_file_stem(
                    str(row.get("email") or ""),
                    row.get("created_at"),
                )
                archive.writestr(
                    f"CPA/{file_stem}.json",
                    json.dumps(generate_cpa_token_json(payload), ensure_ascii=False),
                )
            archive.writestr(
                "sub2api/sub2api_accounts.json",
                json.dumps(
                    build_sub2api_export_payload(bundle_results),
                    ensure_ascii=False,
                    indent=2,
                ),
            )

        zip_buffer.seek(0)
        return {
            "ok": True,
            "filename": f"accounts_export_{int(time.time())}.zip",
            "content": zip_buffer.getvalue(),
            "exported": len(selected_rows),
            "skipped": skipped,
        }

    def stop_task(self, task_id: str) -> dict[str, Any]:
        self._ensure_task_exists(task_id)
        if not self._task_store.exists(task_id):
            return {"ok": False, "reason": "task_not_active"}
        control = self._task_store.request_stop(task_id)
        self._log(task_id, "收到手动停止任务请求")
        return {"ok": True, "control": control}

    def skip_current(self, task_id: str) -> dict[str, Any]:
        self._ensure_task_exists(task_id)
        if not self._task_store.exists(task_id):
            return {"ok": False, "reason": "task_not_active"}
        control = self._task_store.request_skip_current(task_id)
        self._log(task_id, "收到手动跳过当前账号请求")
        return {"ok": True, "control": control}

    def stop_attempt(self, task_id: str, attempt_index: int) -> dict[str, Any]:
        self._ensure_task_exists(task_id)
        if not self._task_store.exists(task_id):
            return {"ok": False, "reason": "task_not_active"}
        snapshot = self.get_task_snapshot(task_id)
        if snapshot is None:
            return {"ok": False, "reason": "task_not_found"}
        target = next(
            (
                item
                for item in (snapshot.get("accounts") or [])
                if int(item.get("attempt_index") or 0) == int(attempt_index)
            ),
            None,
        )
        if target is None:
            return {"ok": False, "reason": "account_not_found"}
        if str(target.get("status") or "") not in {"registering", "running"}:
            return {"ok": False, "reason": "account_not_running"}
        control = self._task_store.request_stop_attempt(task_id, int(attempt_index))
        if control is None:
            return {"ok": False, "reason": "account_not_running"}
        self._log(task_id, f"收到手动停止第 {attempt_index} 个账号请求", attempt_index=int(attempt_index))
        return {"ok": True, "control": control}

    def _merge_runtime_config(self, merged_config: dict[str, Any], req: CreateRegisterTaskRequest) -> dict[str, Any]:
        config = deepcopy(DEFAULT_CONFIG)
        config.update(merged_config or {})
        config.update(req.provider_config or {})
        config.update(req.phone_config or {})
        config["mail_provider"] = req.mail_provider
        config["executor_type"] = req.executor_type
        config["chatgpt_registration_mode"] = "refresh_token"
        config["captcha_solver"] = "yescaptcha"
        config["use_proxy"] = bool(getattr(req, "use_proxy", True))
        if req.proxy and bool(getattr(req, "use_proxy", True)):
            config["proxy"] = req.proxy
        elif not bool(getattr(req, "use_proxy", True)):
            config["proxy"] = ""
        return config

    def _make_engine(
        self,
        req: CreateRegisterTaskRequest,
        merged_config: dict[str, Any],
        email_provider,
        log_fn,
        *,
        interrupt_check=None,
    ):
        extra_config = self._merge_runtime_config(merged_config, req)
        kwargs = {
            "email_service": email_provider,
            "proxy_url": req.proxy,
            "browser_mode": req.executor_type,
            "callback_logger": log_fn,
            "max_retries": 3,
            "extra_config": extra_config,
            "interrupt_check": interrupt_check,
        }
        engine = RefreshTokenRegistrationEngine(**kwargs)
        if req.email:
            engine.email = req.email
        if req.password:
            engine.password = req.password
        return engine

    def _run_task(self, task_id: str, req: CreateRegisterTaskRequest, merged_config: dict[str, Any]) -> None:
        control = self._task_store.control_for(task_id)
        state = TaskExecutionState(total=req.count)
        self._set_task_execution_state(task_id, state)
        self._task_store.mark_running(task_id)
        update_task_run(task_id, status="running", progress=f"0/{req.count}", success=0, failed=0, skipped=0)
        self._log(task_id, f"任务开始：共 {req.count} 个账号，并发 {req.concurrency}")
        errors: list[str] = []
        start_gate_lock = threading.Lock()
        next_start_time = time.time()

        try:
            control.checkpoint()
            self._preflight_network(task_id, req)
        except StopTaskRequested as exc:
            completed, success, failed, skipped = state.snapshot_counts()
            self._log(task_id, f"[STOP] {exc}")
            update_task_run(
                task_id,
                status="stopped",
                progress=f"{completed}/{req.count}",
                success=success,
                failed=failed,
                skipped=skipped,
                error=str(exc),
                summary_json={"success": success, "failed": failed, "skipped": skipped, "total": req.count},
            )
            self._task_store.finish(task_id, status="stopped", success=success, skipped=skipped, errors=[str(exc)], error=str(exc))
            self._set_task_execution_state(task_id, None)
            self._task_store.cleanup()
            return
        except Exception as exc:
            message = f"代理检测失败: {exc}" if str(req.proxy or "").strip() else f"网络预检失败: {exc}"
            self._log(task_id, message, level="error")
            retry_supported = self._supports_retry(req.mail_provider, req.email or "")
            insert_task_result(
                task_id,
                attempt_index=1,
                status="failed",
                email=req.email or "",
                password=req.password or "",
                error=message,
                extra={
                    "failure_stage": "network_precheck",
                    "failure_origin": "network_precheck",
                    "failure_detail": message,
                    "retry_supported": retry_supported,
                    "metadata": {
                        "failure_stage": "network_precheck",
                        "failure_origin": "network_precheck",
                        "failure_detail": message,
                        "resume_supported": retry_supported,
                    },
                    "mail_provider": req.mail_provider,
                },
            )
            self._upsert_task_account(task_id, 1, email=req.email or "", status="failed", error=message)
            state.apply_outcome(AttemptOutcome.FAILED)
            completed, success, failed, skipped = state.snapshot_counts()
            update_task_run(
                task_id,
                status="failed",
                progress=f"{completed}/{req.count}",
                success=success,
                failed=failed,
                skipped=skipped,
                error=message,
                summary_json={"success": success, "failed": failed, "skipped": skipped, "total": req.count},
            )
            self._task_store.finish(task_id, status="failed", success=success, skipped=skipped, errors=[message], error=message)
            self._set_task_execution_state(task_id, None)
            self._task_store.cleanup()
            return

        for index in range(req.count):
            state.enqueue(QueuedAttempt(attempt_index=index + 1))
        state.mark_initial_enqueued()

        def do_one(queued_attempt: QueuedAttempt):
            nonlocal next_start_time
            attempt_id: int | None = None
            proxy_entry_id: int | None = None
            deferred_queued_attempt: QueuedAttempt | None = None
            attempt_index = int(queued_attempt.attempt_index)
            proxy_retry_count = max(0, int((queued_attempt.meta or {}).get("proxy_retry_count") or 0))
            attempt_req = req.model_copy(deep=True, update=queued_attempt.req_overrides or {})
            attempt_merged_config = deepcopy(merged_config)
            attempt_merged_config.update(queued_attempt.merged_config_overrides or {})
            try:
                control.checkpoint()
                attempt_id = control.start_attempt(attempt_index)
                control.checkpoint(attempt_id=attempt_id)
                self._upsert_task_account(task_id, attempt_index, email=attempt_req.email or "", status="registering", error="")
                self._log(task_id, f"开始注册第 {attempt_index}/{req.count} 个账号", attempt_index=attempt_index)
                with start_gate_lock:
                    now = time.time()
                    wait_seconds = max(0.0, next_start_time - now)
                    if req.register_delay_seconds > 0 and wait_seconds > 0:
                        self._log(task_id, f"第 {attempt_index} 个账号启动前延迟 {wait_seconds:g} 秒", attempt_index=attempt_index)
                        while wait_seconds > 0:
                            control.checkpoint(attempt_id=attempt_id)
                            chunk = min(0.25, wait_seconds)
                            time.sleep(chunk)
                            wait_seconds -= chunk
                    next_start_time = time.time() + float(req.register_delay_seconds or 0)

                def attempt_log(message: str, *, level: str = "info") -> None:
                    control.checkpoint(attempt_id=attempt_id)
                    self._log(task_id, message, level=level, attempt_index=attempt_index)
                    control.checkpoint(attempt_id=attempt_id)

                def build_network_precheck_failure(message: str) -> RuntimeError:
                    failure_detail = str(message or "").strip()
                    retry_email_binding = (
                        dict(attempt_merged_config.get("retry_email_binding") or {})
                        if isinstance(attempt_merged_config.get("retry_email_binding"), dict)
                        else {}
                    )
                    metadata = {
                        "failure_stage": "network_precheck",
                        "failure_origin": "network_precheck",
                        "failure_detail": failure_detail,
                        "resume_supported": self._supports_retry(attempt_req.mail_provider, attempt_req.email or ""),
                        "email_binding": retry_email_binding,
                    }
                    error = RuntimeError(failure_detail)
                    error.__cause__ = Exception(str(metadata))
                    return error

                def requeue_attempt_for_proxy_retry(message: str) -> AttemptResult | None:
                    if str(attempt_req.proxy or "").strip():
                        return None
                    if not self._enabled_proxy_pool_exists():
                        return None
                    max_proxy_retry_rounds = 5
                    if proxy_retry_count >= max_proxy_retry_rounds:
                        return None
                    retry_meta = dict(queued_attempt.meta or {})
                    retry_meta["proxy_retry_count"] = proxy_retry_count + 1
                    queued = state.enqueue(
                        QueuedAttempt(
                            attempt_index=attempt_index,
                            req_overrides=dict(queued_attempt.req_overrides or {}),
                            merged_config_overrides=dict(queued_attempt.merged_config_overrides or {}),
                            meta=retry_meta,
                        )
                    )
                    if not queued:
                        return None
                    self._upsert_task_account(
                        task_id,
                        attempt_index,
                        email=attempt_req.email or "",
                        status="pending",
                        error="",
                    )
                    attempt_log(
                        f"{message}，已重新排队 ({retry_meta['proxy_retry_count']}/{max_proxy_retry_rounds})",
                        level="warning",
                    )
                    return AttemptResult.stopped(message)

                if bool(getattr(attempt_req, "use_proxy", True)) and not str(attempt_req.proxy or "").strip() and self._enabled_proxy_pool_exists():
                    try:
                        selected_proxy, selected_proxy_id = self._acquire_checked_proxy_for_attempt(
                            task_id=task_id,
                            attempt_index=attempt_index,
                            attempt_log=attempt_log,
                        )
                    except Exception as exc:
                        retry_result = requeue_attempt_for_proxy_retry(str(exc))
                        if retry_result is not None:
                            return retry_result
                        raise build_network_precheck_failure(str(exc))
                    if selected_proxy:
                        attempt_req = attempt_req.model_copy(update={"proxy": selected_proxy})
                        proxy_entry_id = int(selected_proxy_id or 0) or None
                elif bool(getattr(attempt_req, "use_proxy", True)) and str(attempt_req.proxy or "").strip():
                    attempt_log("正在检测代理连通性...")
                    try:
                        proxy_ip, proxy_country = self._query_egress_info(str(attempt_req.proxy or "").strip())
                    except Exception as exc:
                        raise build_network_precheck_failure(f"代理检测失败: {exc}")
                    if proxy_country:
                        attempt_log(f"代理出口: {proxy_ip} ({proxy_country})")
                    else:
                        attempt_log(f"代理出口 IP: {proxy_ip}")

                accounting_proxy = str(attempt_req.proxy or "").strip() or None
                attempt_proxy = isolate_proxy_session(
                    attempt_req.proxy,
                    scope=f"{task_id}:{attempt_index}:{time.time_ns()}",
                )
                if attempt_proxy != attempt_req.proxy:
                    attempt_req = attempt_req.model_copy(update={"proxy": attempt_proxy})

                with proxy_usage_context(proxy_id=proxy_entry_id, proxy_url=accounting_proxy):
                    runtime_config = self._merge_runtime_config(attempt_merged_config, attempt_req)
                    provider = build_mail_provider(
                        attempt_req.mail_provider,
                        config=runtime_config,
                        proxy=attempt_req.proxy,
                        fixed_email=attempt_req.email,
                        log_fn=attempt_log,
                    )
                    engine = self._make_engine(
                        attempt_req,
                        attempt_merged_config,
                        provider,
                        attempt_log,
                        interrupt_check=lambda: control.checkpoint(attempt_id=attempt_id),
                    )
                    result = engine.run()
                if not result or not getattr(result, "success", False):
                    failure_message = getattr(result, "error_message", "") or "注册失败"
                    failure_metadata = getattr(result, "metadata", None) or {}
                    raise RuntimeError(failure_message) from Exception(str(failure_metadata))

                upload_results = sync_chatgpt_result(result, runtime_config)
                for upload_item in upload_results:
                    name = str(upload_item.get("name") or "上传")
                    ok = bool(upload_item.get("ok"))
                    msg = str(upload_item.get("msg") or "").strip()
                    if ok:
                        self._log(task_id, f"{name} 上传成功：{msg or '完成'}", attempt_index=attempt_index)
                    else:
                        self._log(task_id, f"{name} 上传失败：{msg or '未知错误'}", level="error", attempt_index=attempt_index)

                insert_task_result(
                    task_id,
                    attempt_index=attempt_index,
                    status="success",
                    email=str(getattr(result, "email", "") or ""),
                    password=str(getattr(result, "password", "") or ""),
                    access_token=str(getattr(result, "access_token", "") or ""),
                    refresh_token=str(getattr(result, "refresh_token", "") or ""),
                    session_token=str(getattr(result, "session_token", "") or ""),
                    workspace_id=str(getattr(result, "workspace_id", "") or ""),
                    extra={
                        "account_id": str(getattr(result, "account_id", "") or ""),
                        "id_token": str(getattr(result, "id_token", "") or ""),
                        "metadata": getattr(result, "metadata", None) or {},
                        "mode": "refresh_token",
                        "mail_provider": attempt_req.mail_provider,
                        "upload_results": upload_results,
                    },
                )
                self._upsert_task_account(
                    task_id,
                    attempt_index,
                    email=str(getattr(result, "email", "") or ""),
                    status="success",
                )
                self._log(task_id, f"[OK] 注册成功: {getattr(result, 'email', '')}", attempt_index=attempt_index)
                state.apply_outcome(AttemptOutcome.SUCCESS)
                self._sync_task_progress(task_id, state)
                return AttemptResult.success()
            except SkipCurrentAttemptRequested as exc:
                insert_task_result(task_id, attempt_index=attempt_index, status="skipped", email=attempt_req.email or "", error=str(exc))
                self._upsert_task_account(task_id, attempt_index, email=attempt_req.email or "", status="skipped", error=str(exc))
                self._log(task_id, f"[SKIP] 已跳过当前账号: {exc}", attempt_index=attempt_index)
                state.apply_outcome(AttemptOutcome.SKIPPED)
                self._sync_task_progress(task_id, state)
                return AttemptResult.skipped(str(exc))
            except StopTaskRequested as exc:
                message = str(exc)
                insert_task_result(task_id, attempt_index=attempt_index, status="stopped", email=attempt_req.email or "", error=message)
                self._upsert_task_account(task_id, attempt_index, email=attempt_req.email or "", status="stopped", error=message)
                self._log(task_id, f"[STOP] {message}", attempt_index=attempt_index)
                state.apply_outcome(AttemptOutcome.STOPPED)
                self._sync_task_progress(task_id, state)
                return AttemptResult.stopped(message)
            except StopCurrentAttemptRequested as exc:
                message = str(exc)
                insert_task_result(task_id, attempt_index=attempt_index, status="stopped", email=attempt_req.email or "", error=message)
                self._upsert_task_account(task_id, attempt_index, email=attempt_req.email or "", status="stopped", error=message)
                self._log(task_id, f"[STOP] {message}", attempt_index=attempt_index)
                state.apply_outcome(AttemptOutcome.STOPPED)
                self._sync_task_progress(task_id, state)
                return AttemptResult.stopped(message)
            except DeferAttemptRequested as exc:
                suspend_message = str(exc) or "等待验证码超时，已挂起"
                suspend_meta = dict(getattr(exc, "metadata", None) or {})
                delay_seconds = max(1, int(getattr(exc, "delay_seconds", 120) or 120))
                merged_overrides = dict(queued_attempt.merged_config_overrides or {})
                merged_overrides.update(
                    dict(suspend_meta.get("config_overrides") or {})
                    if isinstance(suspend_meta.get("config_overrides"), dict)
                    else {}
                )
                retry_email_binding = (
                    dict(merged_overrides.get("retry_email_binding") or {})
                    if isinstance(merged_overrides.get("retry_email_binding"), dict)
                    else {}
                )
                if not retry_email_binding and isinstance(suspend_meta.get("email_binding"), dict):
                    retry_email_binding = dict(suspend_meta.get("email_binding") or {})
                if retry_email_binding:
                    merged_overrides["retry_email_binding"] = retry_email_binding
                if suspend_meta.get("retry_resume_stage"):
                    merged_overrides["retry_resume_stage"] = str(suspend_meta.get("retry_resume_stage") or "")
                if suspend_meta.get("retry_resume_origin"):
                    merged_overrides["retry_resume_origin"] = str(suspend_meta.get("retry_resume_origin") or "")
                deferred_queued_attempt = QueuedAttempt(
                    attempt_index=attempt_index,
                    req_overrides=dict(queued_attempt.req_overrides or {}),
                    merged_config_overrides=merged_overrides,
                    meta=dict(queued_attempt.meta or {}),
                    not_before=time.time() + delay_seconds,
                )
                self._upsert_task_account(task_id, attempt_index, email=attempt_req.email or "", status="pending", error="")
                self._log(
                    task_id,
                    f"[WAIT] {suspend_message}，已挂起 {delay_seconds}s 后继续",
                    level="warning",
                    attempt_index=attempt_index,
                )
                return AttemptResult.stopped(suspend_message)
            except Exception as exc:
                message = str(exc)
                failure_meta: dict[str, Any] = {}
                if hasattr(exc, "__cause__") and exc.__cause__ is not None:
                    cause = str(exc.__cause__ or "").strip()
                    if cause.startswith("{") and cause.endswith("}"):
                        import ast

                        try:
                            parsed = ast.literal_eval(cause)
                            if isinstance(parsed, dict):
                                failure_meta = parsed
                        except Exception:
                            failure_meta = {}
                insert_task_result(
                    task_id,
                    attempt_index=attempt_index,
                    status="failed",
                    email=str(getattr(locals().get("result", None), "email", "") or attempt_req.email or ""),
                    password=str(getattr(locals().get("result", None), "password", "") or attempt_req.password or ""),
                    error=message,
                    extra={
                        "failure_stage": str(failure_meta.get("failure_stage") or ""),
                        "failure_origin": str(failure_meta.get("failure_origin") or ""),
                        "failure_detail": str(failure_meta.get("failure_detail") or message),
                        "retry_supported": bool(failure_meta.get("resume_supported")),
                        "metadata": failure_meta,
                        "mail_provider": attempt_req.mail_provider,
                    },
                )
                self._upsert_task_account(task_id, attempt_index, email=attempt_req.email or "", status="failed", error=message)
                self._log(task_id, f"[FAIL] 注册失败: {message}", level="error", attempt_index=attempt_index)
                state.apply_outcome(AttemptOutcome.FAILED)
                errors.append(message)
                self._sync_task_progress(task_id, state)
                return AttemptResult.failed(message)
            finally:
                control.finish_attempt(attempt_id)
                state.finish_attempt(attempt_index)
                if deferred_queued_attempt is not None:
                    state.enqueue(deferred_queued_attempt)

        try:
            worker_count = max(1, min(req.concurrency, req.count, 100))

            def worker_loop() -> None:
                while True:
                    try:
                        control.checkpoint()
                    except StopTaskRequested:
                        return
                    queued_attempt = state.get_next(stop_requested=control.is_stop_requested())
                    if queued_attempt is None:
                        return
                    try:
                        do_one(queued_attempt)
                    except Exception as exc:
                        errors.append(str(exc))
                        self._log(task_id, f"[ERROR] 任务线程异常: {exc}", level="error")

            with ThreadPoolExecutor(max_workers=worker_count) as pool:
                futures = [pool.submit(worker_loop) for _ in range(worker_count)]
                for future in futures:
                    future.result()
        except Exception as exc:
            completed, success, failed, skipped = state.snapshot_counts()
            self._log(task_id, f"致命错误: {exc}", level="error")
            update_task_run(
                task_id,
                status="failed",
                progress=f"{completed}/{req.count}",
                success=success,
                failed=failed or len(errors),
                skipped=skipped,
                error=str(exc),
                summary_json={"success": success, "failed": failed or len(errors), "skipped": skipped},
            )
            self._task_store.finish(task_id, status="failed", success=success, skipped=skipped, errors=errors, error=str(exc))
            self._set_task_execution_state(task_id, None)
            self._task_store.cleanup()
            return

        completed, success, failed, skipped = state.snapshot_counts()
        final_status = "stopped" if control.is_stop_requested() else "done"
        summary = {"success": success, "failed": failed, "skipped": skipped, "total": req.count}
        summary_text = (
            f"任务已停止: 成功 {success} 个, 跳过 {skipped} 个, 失败 {failed} 个"
            if final_status == "stopped"
            else f"完成: 成功 {success} 个, 跳过 {skipped} 个, 失败 {failed} 个"
        )
        self._log(task_id, summary_text)
        update_task_run(
            task_id,
            status=final_status,
            progress=f"{completed}/{req.count}",
            success=success,
            failed=failed,
            skipped=skipped,
            error="; ".join(errors[-3:]) if errors and final_status == "failed" else "",
            summary_json=summary,
        )
        self._task_store.finish(task_id, status=final_status, success=success, skipped=skipped, errors=errors)
        self._set_task_execution_state(task_id, None)
        self._task_store.cleanup()


manager = RegistrationManager()
