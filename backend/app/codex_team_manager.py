from __future__ import annotations

import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from io import BytesIO
from types import SimpleNamespace
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

from core.task_runtime import StopTaskRequested

from .db import (
    acquire_proxy_pool_entry,
    append_codex_team_job_event,
    batch_import_codex_team_parent_accounts,
    get_codex_team_parent_pool_summary,
    create_codex_team_job,
    get_codex_team_job,
    insert_codex_team_web_session,
    list_codex_team_jobs,
    list_enabled_codex_team_parent_accounts,
    list_codex_team_job_events,
    list_codex_team_web_sessions,
    update_codex_team_parent_account,
    update_codex_team_job,
)
from .manager import RegistrationManager
from .mail_providers import build_mail_provider
from .codex_team_parent_login import login_parent_via_outlook, parse_outlook_parent_import_data
from platforms.chatgpt.refresh_token_registration_engine import RefreshTokenRegistrationEngine
from platforms.chatgpt.team_manage_style_client import TeamManageStyleClient
from .external_uploads import generate_cpa_token_json
from platforms.chatgpt.team_invite import (
    accept_team_invite,
    resolve_parent_invite_context as resolve_parent_invite_context_from_token,
)


@dataclass
class CodexTeamJob:
    id: str
    request_payload: dict[str, Any]
    merged_config: dict[str, Any]
    status: str = "pending"
    total: int = 0
    success: int = 0
    failed: int = 0
    progress: str = "0/0"
    completed: int = 0
    created_at: float = field(default_factory=time.time)
    stop_requested: bool = False
    parent_invite_context: dict[str, Any] | None = None
    proxy_url: str = ""
    proxy_id: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


@dataclass
class CodexTeamParentImportJob:
    id: str
    raw_data: str
    merged_config: dict[str, Any]
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
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


class CodexTeamManager:
    def __init__(self) -> None:
        self._jobs: dict[str, CodexTeamJob] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()
        self._team_clients: dict[str, TeamManageStyleClient] = {}
        self._parent_import_jobs: dict[str, CodexTeamParentImportJob] = {}
        self._parent_import_threads: dict[str, threading.Thread] = {}

    def create_parent_import_job(
        self,
        *,
        data: str,
        merged_config: dict[str, Any],
        executor_type: str = "protocol",
    ) -> str:
        job_id = f"codex_parent_import_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
        job = CodexTeamParentImportJob(
            id=job_id,
            raw_data=str(data or ""),
            merged_config=dict(merged_config or {}),
            executor_type=str(executor_type or "protocol"),
        )
        with self._lock:
            self._parent_import_jobs[job_id] = job
        thread = threading.Thread(target=self._run_parent_import_job, args=(job,), daemon=True)
        self._parent_import_threads[job_id] = thread
        thread.start()
        return job_id

    def _append_parent_import_event(
        self,
        job: CodexTeamParentImportJob,
        message: str,
        *,
        level: str = "info",
        account_email: str = "",
    ) -> None:
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

    def _check_parent_import_stop(self, job: CodexTeamParentImportJob) -> None:
        if job.stop_requested:
            raise RuntimeError("母号导入已取消")

    def _run_parent_import_job(self, job: CodexTeamParentImportJob) -> None:
        with job.lock:
            job.status = "running"
        try:
            entries = parse_outlook_parent_import_data(job.raw_data)
        except Exception as exc:
            with job.lock:
                job.status = "failed"
                job.progress = "0/0"
            self._append_parent_import_event(job, str(exc or "母号导入格式错误"), level="error")
            return

        with job.lock:
            job.total = len(entries)
            job.progress = f"0/{len(entries)}"
        self._append_parent_import_event(job, f"开始导入 {len(entries)} 个母号并执行登录转换")

        runtime_config = dict(job.merged_config)
        if runtime_config.get("use_proxy") and not str(runtime_config.get("proxy") or "").strip():
            proxy_entry = acquire_proxy_pool_entry()
            if proxy_entry and str(proxy_entry.get("proxy_url") or "").strip():
                runtime_config["proxy"] = str(proxy_entry.get("proxy_url") or "").strip()

        for item in entries:
            self._check_parent_import_stop(job)
            email = str(item.get("email") or "").strip().lower()
            self._append_parent_import_event(job, "开始母号登录转换", account_email=email)
            try:
                result = login_parent_via_outlook(
                    item,
                    merged_config=runtime_config,
                    executor_type=job.executor_type,
                    log_fn=lambda message, current_email=email: self._append_parent_import_event(
                        job,
                        str(message or ""),
                        account_email=current_email,
                    ),
                    interrupt_check=lambda current_job=job: self._check_parent_import_stop(current_job),
                )
                if result.get("success"):
                    sync_result = self.sync_parent_account_child_count(
                        int(result.get("parent_id") or 0),
                        merged_config=runtime_config,
                        executor_type=job.executor_type,
                    )
                    with job.lock:
                        job.success += 1
                    if sync_result.get("success"):
                        self._append_parent_import_event(
                            job,
                            f"母号转换成功，已导入 session: account_id={str(result.get('account_id') or '-')} · 当前子号数={int(sync_result.get('child_member_count') or 0)}",
                            account_email=email,
                        )
                    else:
                        self._append_parent_import_event(
                            job,
                            f"母号转换成功，但自动获取子号数失败: {str(sync_result.get('error') or 'unknown')}",
                            level="error",
                            account_email=email,
                        )
                else:
                    with job.lock:
                        job.failed += 1
                    self._append_parent_import_event(
                        job,
                        str(result.get("error") or "母号转换失败"),
                        level="error",
                        account_email=email,
                    )
            except Exception as exc:
                with job.lock:
                    job.failed += 1
                self._append_parent_import_event(job, str(exc or "母号转换异常"), level="error", account_email=email)
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

    def get_parent_import_job_snapshot(self, job_id: str) -> dict[str, Any] | None:
        job = self._parent_import_jobs.get(str(job_id))
        if not job:
            return None
        with job.lock:
            return {
                "id": job.id,
                "status": job.status,
                "total": job.total,
                "success": job.success,
                "failed": job.failed,
                "completed": job.completed,
                "progress": job.progress,
                "created_at": job.created_at,
                "events": list(job.events),
            }

    def stop_parent_import_job(self, job_id: str) -> dict[str, Any]:
        job = self._parent_import_jobs.get(str(job_id))
        if not job:
            return {"ok": False, "reason": "job_not_found"}
        job.stop_requested = True
        with job.lock:
            if job.status in {"pending", "running"}:
                job.status = "stopped"
        self._append_parent_import_event(job, "已请求取消母号导入", level="error")
        return {"ok": True}

    def import_parent_accounts(
        self,
        data: str,
        *,
        enabled: bool = True,
        merged_config: dict[str, Any] | None = None,
        executor_type: str = "protocol",
    ) -> dict[str, Any]:
        result = batch_import_codex_team_parent_accounts(data, enabled=enabled)
        accounts = list(result.get("accounts") or [])
        merged = dict(merged_config or {})
        sync_errors: list[str] = []
        for account in accounts:
            parent_id = int(account.get("id") or 0)
            if parent_id <= 0:
                continue
            sync_result = self.sync_parent_account_child_count(
                parent_id,
                merged_config=merged,
                executor_type=executor_type,
            )
            if sync_result.get("success"):
                account["child_member_count"] = int(sync_result.get("child_member_count") or 0)
                account["team_account_id"] = str(sync_result.get("team_account_id") or account.get("team_account_id") or "")
            else:
                error = str(sync_result.get("error") or "自动获取母号子号数失败")
                sync_errors.append(f"{str(account.get('email') or parent_id)}: {error}")
                account["sync_error"] = error
        errors = list(result.get("errors") or [])
        errors.extend(sync_errors)
        result["accounts"] = accounts
        result["errors"] = errors
        result["summary"] = get_codex_team_parent_pool_summary()
        return result

    def sync_parent_account_child_count(
        self,
        parent_id: int,
        *,
        merged_config: dict[str, Any] | None = None,
        executor_type: str = "protocol",
    ) -> dict[str, Any]:
        parents = list_enabled_codex_team_parent_accounts()
        parent = next((item for item in parents if int(item.get("id") or 0) == int(parent_id)), None)
        if not parent:
            return {"success": False, "error": "母号不存在"}

        merged = dict(merged_config or {})
        proxy_url = str(merged.get("proxy") or "").strip() if merged.get("use_proxy") else ""
        try:
            parent_context = resolve_parent_invite_context(
                parent,
                merged_config=merged,
                executor_type=executor_type,
                proxy_url_override=proxy_url or None,
            )
            if not parent_context.get("success") and not (
                str(parent_context.get("access_token") or "").strip()
                and str(parent_context.get("account_id") or "").strip()
            ):
                error = str(parent_context.get("error") or "母号上下文初始化失败")
                update_codex_team_parent_account(int(parent_id), last_error=error, last_sync_at=time.time())
                return {"success": False, "error": error}

            parent_account_id = str(parent_context.get("account_id") or parent.get("team_account_id") or "").strip()
            if not parent_account_id:
                error = "母号缺少 team account_id"
                update_codex_team_parent_account(int(parent_id), last_error=error, last_sync_at=time.time())
                return {"success": False, "error": error}

            parent_identifier = str(parent_context.get("email") or parent.get("email") or parent_account_id or "default")
            client = self._get_team_client(parent_identifier, proxy_url or None)
            count_result = client.get_members(
                access_token=str(parent_context.get("access_token") or ""),
                account_id=parent_account_id,
                identifier=parent_identifier,
            )
            if not count_result.get("success"):
                error = str(count_result.get("error") or "读取 Team 子号人数失败")
                update_codex_team_parent_account(int(parent_id), last_error=error, last_sync_at=time.time())
                return {"success": False, "error": error}

            child_count = self._count_child_members(count_result)
            update_codex_team_parent_account(
                int(parent_id),
                team_account_id=parent_account_id,
                team_name=str(parent_context.get("team_name") or parent.get("team_name") or ""),
                child_member_count=child_count,
                last_sync_at=time.time(),
                last_error="",
                last_used=time.time(),
            )
            return {
                "success": True,
                "child_member_count": child_count,
                "team_account_id": parent_account_id,
                "email": str(parent.get("email") or ""),
            }
        except Exception as exc:
            error = str(exc or "读取 Team 子号人数失败")
            update_codex_team_parent_account(int(parent_id), last_error=error, last_sync_at=time.time())
            return {"success": False, "error": error}

    def create_job(
        self,
        request_payload: dict[str, Any],
        *,
        merged_config: dict[str, Any],
        start_immediately: bool = True,
    ) -> str:
        job_id = f"codex_team_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
        total = max(1, int(request_payload.get("child_count") or 1))
        job = CodexTeamJob(
            id=job_id,
            request_payload=dict(request_payload or {}),
            merged_config=dict(merged_config or {}),
            total=total,
            progress=f"0/{total}",
        )
        with self._lock:
            self._jobs[job_id] = job

        create_codex_team_job(job_id, request_payload=request_payload)
        update_codex_team_job(job_id, total=total, progress=job.progress)
        append_codex_team_job_event(job_id, f"创建任务成功，计划处理 {total} 个子号")

        if start_immediately:
            thread = threading.Thread(target=self._run_job, args=(job,), daemon=True)
            self._threads[job_id] = thread
            thread.start()
        return job_id

    def _run_job(self, job: CodexTeamJob) -> None:
        self._set_job_state(job, status="running")
        if str(job.request_payload.get("parent_source") or "").strip().lower() == "pool":
            self._run_parent_pool_job(job)
            final_status = "failed" if job.status == "failed" else ("stopped" if job.stop_requested else "done")
            self._set_job_state(job, status=final_status)
            return
        max_workers = max(1, int(job.request_payload.get("concurrency") or 1))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = []
            for attempt_index in range(1, job.total + 1):
                if job.stop_requested:
                    append_codex_team_job_event(job.id, "任务已停止")
                    break
                futures.append(executor.submit(self._process_account, job, attempt_index))

            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as exc:
                    append_codex_team_job_event(job.id, f"任务 worker 异常: {str(exc or '')}", level="error")

        final_status = "failed" if job.status == "failed" else ("stopped" if job.stop_requested else "done")
        self._set_job_state(job, status=final_status)

    @staticmethod
    def _check_job_stop(job: CodexTeamJob) -> None:
        if job.stop_requested:
            raise StopTaskRequested("任务已手动停止")

    @staticmethod
    def _is_fatal_child_failure_message(message: str) -> bool:
        text = str(message or "").strip()
        if not text:
            return False
        nonfatal_markers = (
            "微软邮箱已失效",
            "service abuse mode",
            "invalid_grant",
            "aadsts70000",
        )
        if any(marker.lower() in text.lower() for marker in nonfatal_markers):
            return False
        fatal_markers = (
            "本地微软邮箱池为空",
            "本地微软邮箱池中不存在账号",
            "本地令牌池为空",
            "令牌池为空",
            "空邮箱",
        )
        return any(marker in text for marker in fatal_markers)

    @staticmethod
    def _should_halt_parent_on_failure(message: str) -> bool:
        text = str(message or "").strip().lower()
        if not text:
            return True
        nonfatal_markers = (
            "微软邮箱已失效",
            "service abuse mode",
            "invalid_grant",
            "aadsts70000",
            "graph api token 获取失败",
        )
        if any(marker.lower() in text for marker in nonfatal_markers):
            return False
        return True

    @staticmethod
    def _should_retry_registration_failure(error_text: str) -> bool:
        text = str(error_text or "").strip().lower()
        if not text:
            return False
        retry_markers = (
            "http 429",
            " 429:",
            "too many requests",
            "just a moment",
            "rate limit",
        )
        return any(marker in text for marker in retry_markers)

    def _stop_job_on_fatal_child_failure(self, job: CodexTeamJob, reason: str, *, account_email: str = "") -> None:
        if job.stop_requested and job.status == "failed":
            return
        job.stop_requested = True
        append_codex_team_job_event(
            job.id,
            f"检测到致命错误，停止任务: {str(reason or '').strip()}",
            level="error",
            account_email=account_email,
        )
        self._set_job_state(job, status="failed")

    def _resolve_job_proxy(self, job: CodexTeamJob) -> tuple[str | None, int | None]:
        if not bool(job.merged_config.get("use_proxy")):
            return None, None
        with job.lock:
            if str(job.proxy_url or "").strip():
                return str(job.proxy_url or "").strip(), int(job.proxy_id or 0) or None

        proxy_url = ""
        proxy_id = 0
        entry = acquire_proxy_pool_entry()
        if entry and str(entry.get("proxy_url") or "").strip():
            proxy_url = str(entry.get("proxy_url") or "").strip()
            proxy_id = int(entry.get("id") or 0)
        if not proxy_url:
            proxy_url = str(job.merged_config.get("proxy") or "").strip()

        with job.lock:
            job.proxy_url = proxy_url
            job.proxy_id = proxy_id
        return proxy_url or None, proxy_id or None

    def _run_parent_pool_job(self, job: CodexTeamJob) -> None:
        proxy_url, _proxy_id = self._resolve_job_proxy(job)
        proxy_url = str(proxy_url or "").strip()
        if proxy_url:
            try:
                proxy_ip, proxy_country = RegistrationManager._query_egress_info(proxy_url)
                if proxy_country:
                    append_codex_team_job_event(job.id, f"母号代理出口确认: {proxy_ip} ({proxy_country})")
                else:
                    append_codex_team_job_event(job.id, f"母号代理出口确认: {proxy_ip}")
            except Exception as exc:
                append_codex_team_job_event(job.id, f"母号代理出口检测失败: {exc}", level="error")
        target_children = max(1, int(job.request_payload.get("target_children_per_parent") or 5))
        max_parent_accounts = max(1, int(job.request_payload.get("max_parent_accounts") or 9999))
        parents = list_enabled_codex_team_parent_accounts()[:max_parent_accounts]
        if not parents:
            append_codex_team_job_event(job.id, "母号池为空，无法启动任务", level="error")
            self._set_job_state(job, status="failed")
            return

        total_target = len(parents) * target_children
        job.total = total_target
        job.progress = f"{job.completed}/{job.total}"
        update_codex_team_job(job.id, total=job.total, progress=job.progress)

        attempt_index = 0
        for parent in parents:
            if job.stop_requested:
                break
            parent_email = str(parent.get("email") or "").strip()
            append_codex_team_job_event(job.id, f"开始处理母号: {parent_email}")
            parent_context = resolve_parent_invite_context(
                parent,
                merged_config=job.merged_config,
                executor_type=str(job.request_payload.get("executor_type") or "protocol"),
                proxy_url_override=proxy_url or None,
            )
            if not parent_context.get("success") and not (
                str(parent_context.get("access_token") or "").strip()
                and str(parent_context.get("account_id") or "").strip()
            ):
                error = str(parent_context.get("error") or "母号上下文初始化失败")
                append_codex_team_job_event(job.id, f"母号跳过: {parent_email} {error}", level="error")
                update_codex_team_parent_account(int(parent.get("id") or 0), last_error=error, last_sync_at=time.time())
                continue

            parent_account_id = str(parent_context.get("account_id") or "").strip()
            parent_identifier = str(parent_context.get("email") or parent_email or parent_account_id or "default")
            client = self._get_team_client(parent_identifier, proxy_url or None)
            count_result = client.get_members(
                access_token=str(parent_context.get("access_token") or ""),
                account_id=parent_account_id,
                identifier=parent_identifier,
            )
            if not count_result.get("success"):
                error = str(count_result.get("error") or "读取 Team 子号人数失败")
                append_codex_team_job_event(job.id, f"母号跳过: {parent_email} {error}", level="error")
                update_codex_team_parent_account(int(parent.get("id") or 0), last_error=error, last_sync_at=time.time())
                continue

            child_count = self._count_child_members(count_result)
            update_codex_team_parent_account(
                int(parent.get("id") or 0),
                team_account_id=parent_account_id,
                team_name=str(parent_context.get("team_name") or parent.get("team_name") or ""),
                child_member_count=child_count,
                last_sync_at=time.time(),
                last_error="",
                last_used=time.time(),
            )
            append_codex_team_job_event(job.id, f"母号当前子号数: {parent_email} {child_count}/{target_children}")

            while child_count < target_children and not job.stop_requested:
                attempt_index += 1
                success = self._process_account(job, attempt_index, parent_context=dict(parent_context))
                if not success:
                    halt_parent = bool(getattr(job, "_last_account_halt_parent", True))
                    if halt_parent:
                        append_codex_team_job_event(job.id, f"母号停止继续邀请: {parent_email} 子号流程失败", level="error")
                        break
                    append_codex_team_job_event(job.id, f"子号失败但继续当前母号: {parent_email}", level="error")
                    continue
                count_result = client.get_members(
                    access_token=str(parent_context.get("access_token") or ""),
                    account_id=parent_account_id,
                    identifier=parent_identifier,
                )
                if count_result.get("success"):
                    child_count = self._count_child_members(count_result)
                    update_codex_team_parent_account(
                        int(parent.get("id") or 0),
                        child_member_count=child_count,
                        last_sync_at=time.time(),
                        last_used=time.time(),
                    )
                    append_codex_team_job_event(job.id, f"母号刷新子号数: {parent_email} {child_count}/{target_children}")

    def _process_account(self, job: CodexTeamJob, attempt_index: int, *, parent_context: dict[str, Any] | None = None) -> bool:
        child_email = ""
        try:
            setattr(job, "_last_account_halt_parent", True)
            self._check_job_stop(job)
            proxy_url, _proxy_id = self._resolve_job_proxy(job)
            proxy_url = str(proxy_url or "").strip()
            provider = build_mail_provider(
                "outlook_local",
                config=job.merged_config,
                proxy=proxy_url or None,
                log_fn=lambda message: append_codex_team_job_event(job.id, str(message or ""), account_email=child_email),
            )
            self._check_job_stop(job)
            append_codex_team_job_event(job.id, f"开始从邮箱池取子号 #{attempt_index}")
            mail_info = provider.create_email(job.merged_config)
            self._check_job_stop(job)
            child_email = str(mail_info.get("email") or "").strip()
            if not child_email:
                raise RuntimeError("本地微软邮箱池返回空邮箱，停止任务")
            account = dict(mail_info.get("account") or {})
            child_password = str(account.get("password") or "").strip()
            append_codex_team_job_event(job.id, f"子号邮箱取出成功: {child_email}", account_email=child_email)
            append_codex_team_job_event(job.id, f"开始处理子号 #{attempt_index}", account_email=child_email)

            active_parent_context = dict(parent_context or self._ensure_parent_invite_context(job))
            parent_identifier = str(active_parent_context.get("email") or active_parent_context.get("account_id") or "default")
            client = self._get_team_client(parent_identifier, proxy_url or None)
            append_codex_team_job_event(job.id, "检查是否存在待接受邀请", account_email=child_email)
            invites_before = client.get_invites(
                access_token=str(active_parent_context.get("access_token") or ""),
                account_id=str(active_parent_context.get("account_id") or ""),
                identifier=parent_identifier,
            )
            pending_before = self._find_pending_invite(invites_before, child_email) if invites_before.get("success") else None
            if pending_before:
                append_codex_team_job_event(
                    job.id,
                    f"复用已有待接受邀请: invite_id={str(pending_before.get('id') or '-')}",
                    account_email=child_email,
                )
                invite_result = {
                    "success": True,
                    "invite_id": str(pending_before.get("id") or "").strip(),
                    "error": "",
                    "data": {"reused_existing_invite": True, "invite": pending_before},
                }
            else:
                invite_result = client.send_invite(
                    access_token=str(active_parent_context.get("access_token") or ""),
                    account_id=str(active_parent_context.get("account_id") or ""),
                    email=child_email,
                    identifier=parent_identifier,
                )
            self._check_job_stop(job)
            if not invite_result.get("success"):
                append_codex_team_job_event(job.id, "邀请失败后复查待接受邀请列表", account_email=child_email)
                invites_after = client.get_invites(
                    access_token=str(active_parent_context.get("access_token") or ""),
                    account_id=str(active_parent_context.get("account_id") or ""),
                    identifier=parent_identifier,
                )
                pending_after = self._find_pending_invite(invites_after, child_email) if invites_after.get("success") else None
                if pending_after:
                    invite_result = {
                        "success": True,
                        "invite_id": str(pending_after.get("id") or "").strip(),
                        "error": "",
                        "data": {"reused_existing_invite": True, "invite": pending_after},
                    }
                    append_codex_team_job_event(
                        job.id,
                        f"邀请接口失败，但检测到已存在待接受邀请，继续复用: invite_id={str(invite_result.get('invite_id') or '-')}",
                        account_email=child_email,
                    )
                else:
                    error_message = str(invite_result.get("error") or "发送邀请失败")
                    insert_codex_team_web_session(
                        job_id=job.id,
                        email=child_email,
                        status="failed",
                        selected_workspace_id="",
                        selected_workspace_kind="",
                        account_id="",
                        next_auth_session_token="",
                        cookie_jar=[],
                        error=error_message,
                    )
                    append_codex_team_job_event(
                        job.id,
                        f"母号邀请失败: {error_message}",
                        level="error",
                        account_email=child_email,
                    )
                    setattr(job, "_last_account_halt_parent", True)
                    self._increment(job, failed=1)
                    return False
            append_codex_team_job_event(
                job.id,
                f"母号邀请已发送: invite_id={str(invite_result.get('invite_id') or '-')}",
                account_email=child_email,
            )
            append_codex_team_job_event(job.id, "开始等待邀请链接邮件", account_email=child_email)
            invite_link = provider.get_invitation_link(
                email=child_email,
                timeout=int(job.merged_config.get("chatgpt_invite_link_wait_seconds") or 300),
                invite_sent_at=time.time(),
                interrupt_check=lambda current_job=job: self._check_job_stop(current_job),
            )
            self._check_job_stop(job)
            append_codex_team_job_event(
                job.id,
                f"已获取邀请链接: {invite_link[:180]}",
                account_email=child_email,
            )

            engine = RefreshTokenRegistrationEngine(
                email_service=provider,
                proxy_url=proxy_url or None,
                callback_logger=lambda message: append_codex_team_job_event(job.id, str(message or ""), account_email=child_email),
                task_uuid=f"{job.id}:{attempt_index}",
                browser_mode=str(job.request_payload.get("executor_type") or "protocol"),
                extra_config=dict(job.merged_config),
                interrupt_check=lambda current_job=job: self._check_job_stop(current_job),
            )
            engine.email = child_email
            engine.email_info = mail_info
            max_register_retries = max(1, int(job.merged_config.get("codex_team_register_retry_attempts") or 3))
            result_obj = None
            for register_try in range(1, max_register_retries + 1):
                append_codex_team_job_event(job.id, f"开始子号注册流程: attempt={register_try}/{max_register_retries}", account_email=child_email)
                result_obj = engine.run()
                current_error = str(getattr(result_obj, "error_message", "") or "")
                if bool(getattr(result_obj, "success", False)):
                    break
                if register_try >= max_register_retries:
                    break
                if not self._should_retry_registration_failure(current_error):
                    break
                retry_delay = min(30, 5 * register_try)
                append_codex_team_job_event(
                    job.id,
                    f"子号注册触发重试 {register_try}/{max_register_retries - 1}: {current_error[:180]}，{retry_delay}s 后重试",
                    level="error",
                    account_email=child_email,
                )
                self._check_job_stop(job)
                time.sleep(retry_delay)
                self._check_job_stop(job)
            result = {
                "success": bool(getattr(result_obj, "success", False)),
                "email": str(getattr(result_obj, "email", "") or child_email),
                "selected_workspace_id": str(getattr(result_obj, "workspace_id", "") or ""),
                "selected_workspace_kind": "organization" if getattr(result_obj, "workspace_id", "") else "",
                "invite_workspace_id": str(active_parent_context.get("account_id") or ""),
                "account_id": str(getattr(result_obj, "account_id", "") or ""),
                "next_auth_session_token": str(getattr(result_obj, "session_token", "") or ""),
                "access_token": str(getattr(result_obj, "access_token", "") or ""),
                "refresh_token": str(getattr(result_obj, "refresh_token", "") or ""),
                "id_token": str(getattr(result_obj, "id_token", "") or ""),
                "user_id": str((getattr(result_obj, "metadata", {}) or {}).get("user_id") or ""),
                "display_name": str((getattr(result_obj, "metadata", {}) or {}).get("display_name") or ""),
                "cookie_jar": [],
                "info": dict(getattr(result_obj, "metadata", {}) or {}),
                "error": str(getattr(result_obj, "error_message", "") or ""),
            }
            if result.get("success") and result.get("access_token") and result.get("invite_workspace_id"):
                self._check_job_stop(job)
                accept_result = client.accept_invite(
                    access_token=str(result.get("access_token") or ""),
                    account_id=str(result.get("invite_workspace_id") or ""),
                    identifier=parent_identifier,
                )
                if not accept_result.get("success"):
                    result["success"] = False
                    result["error"] = str(accept_result.get("error") or "接受邀请失败")
            success = bool(result.get("success"))
            failure_info = dict(result.get("info") or {})
            if (not success) and bool(failure_info.get("stop_task_on_failure")):
                self._stop_job_on_fatal_child_failure(
                    job,
                    str(failure_info.get("stop_task_reason") or result.get("error") or "子号流程致命失败"),
                    account_email=child_email,
                )
            setattr(job, "_last_account_halt_parent", self._should_halt_parent_on_failure(str(result.get("error") or "")))
            insert_codex_team_web_session(
                job_id=job.id,
                email=child_email or str(result.get("email") or ""),
                status="success" if success else "failed",
                selected_workspace_id=str(result.get("selected_workspace_id") or ""),
                selected_workspace_kind=str(result.get("selected_workspace_kind") or ""),
                account_id=str(result.get("account_id") or ""),
                next_auth_session_token=str(result.get("next_auth_session_token") or ""),
                access_token=str(result.get("access_token") or ""),
                refresh_token=str(result.get("refresh_token") or ""),
                id_token=str(result.get("id_token") or ""),
                user_id=str(result.get("user_id") or ""),
                display_name=str(result.get("display_name") or ""),
                info=dict(result.get("info") or {}),
                cookie_jar=list(result.get("cookie_jar") or []),
                error=str(result.get("error") or ""),
            )
            if success:
                append_codex_team_job_event(
                    job.id,
                    f"子号处理成功，已进入 workspace {str(result.get('selected_workspace_id') or '-')}",
                    account_email=child_email,
                )
                self._increment(job, success=1)
                return True
            else:
                append_codex_team_job_event(
                    job.id,
                    f"子号处理失败: {str(result.get('error') or 'unknown')}",
                    level="error",
                    account_email=child_email,
                )
                self._increment(job, failed=1)
                return False
        except StopTaskRequested as exc:
            insert_codex_team_web_session(
                job_id=job.id,
                email=child_email,
                status="failed",
                selected_workspace_id="",
                selected_workspace_kind="",
                account_id="",
                next_auth_session_token="",
                cookie_jar=[],
                error=str(exc or ""),
            )
            append_codex_team_job_event(
                job.id,
                f"子号处理已停止: {str(exc or '')}",
                level="error",
                account_email=child_email,
            )
            setattr(job, "_last_account_halt_parent", True)
            return False
        except Exception as exc:
            if self._is_fatal_child_failure_message(str(exc or "")):
                self._stop_job_on_fatal_child_failure(job, str(exc or ""), account_email=child_email)
            setattr(job, "_last_account_halt_parent", self._should_halt_parent_on_failure(str(exc or "")))
            insert_codex_team_web_session(
                job_id=job.id,
                email=child_email,
                status="failed",
                selected_workspace_id="",
                selected_workspace_kind="",
                account_id="",
                next_auth_session_token="",
                cookie_jar=[],
                error=str(exc or ""),
            )
            append_codex_team_job_event(
                job.id,
                f"子号处理异常: {str(exc or '')}",
                level="error",
                account_email=child_email,
            )
            self._increment(job, failed=1)
            return False

    def _ensure_parent_invite_context(self, job: CodexTeamJob) -> dict[str, Any]:
        with job.lock:
            if isinstance(job.parent_invite_context, dict) and job.parent_invite_context.get("access_token"):
                return dict(job.parent_invite_context)

        parent_credentials = dict((job.request_payload or {}).get("parent_credentials") or {})
        resolved = resolve_parent_invite_context(
            parent_credentials,
            merged_config=job.merged_config,
            executor_type=str(job.request_payload.get("executor_type") or "protocol"),
        )
        if not resolved.get("success") and not (
            str(resolved.get("access_token") or "").strip()
            and str(resolved.get("account_id") or "").strip()
        ):
            raise RuntimeError(str(resolved.get("error") or "母号邀请上下文初始化失败"))
        resolved["success"] = True

        with job.lock:
            job.parent_invite_context = dict(resolved)
        append_codex_team_job_event(
            job.id,
            f"母号邀请上下文已就绪: account_id={str(resolved.get('account_id') or '-')}",
        )
        return dict(resolved)

    def _get_team_client(self, identifier: str, proxy_url: str | None) -> TeamManageStyleClient:
        key = f"{identifier}|{proxy_url or ''}"
        client = self._team_clients.get(key)
        if client is None:
            client = TeamManageStyleClient(proxy_url=proxy_url)
            self._team_clients[key] = client
        return client

    @staticmethod
    def _count_child_members(result: dict[str, Any]) -> int:
        members = list(result.get("members") or [])
        count = 0
        for item in members:
            role = str(item.get("role") or item.get("account_user_role") or "").strip().lower()
            if role == "account-owner":
                continue
            count += 1
        return count

    @staticmethod
    def _find_pending_invite(invites_result: dict[str, Any], email: str) -> dict[str, Any] | None:
        target = str(email or "").strip().lower()
        for item in list(invites_result.get("items") or []):
            invite_email = str(item.get("email_address") or item.get("email") or "").strip().lower()
            if invite_email == target:
                return dict(item)
        return None

    def _increment(self, job: CodexTeamJob, *, success: int = 0, failed: int = 0) -> None:
        with job.lock:
            job.success += int(success or 0)
            job.failed += int(failed or 0)
            job.completed += int(success or 0) + int(failed or 0)
            job.progress = f"{job.completed}/{job.total}"
            status = "done" if job.completed >= job.total and not job.stop_requested else job.status
            if status == "done":
                job.status = status
        update_codex_team_job(
            job.id,
            status=job.status,
            total=job.total,
            success=job.success,
            failed=job.failed,
            progress=job.progress,
        )

    def _set_job_state(self, job: CodexTeamJob, *, status: str) -> None:
        with job.lock:
            job.status = str(status or job.status)
        update_codex_team_job(
            job.id,
            status=job.status,
            total=job.total,
            success=job.success,
            failed=job.failed,
            progress=job.progress,
        )

    def stop_job(self, job_id: str) -> dict[str, Any]:
        job = self._jobs.get(str(job_id))
        if job:
            job.stop_requested = True
            self._set_job_state(job, status="stopped")
            return {"ok": True}

        snapshot = get_codex_team_job(str(job_id))
        if not snapshot:
            return {"ok": False, "reason": "job_not_found"}
        update_codex_team_job(str(job_id), status="stopped")
        return {"ok": True, "detached": True}

    def get_job_snapshot(self, job_id: str) -> dict[str, Any] | None:
        snapshot = get_codex_team_job(str(job_id))
        if not snapshot:
            return None
        snapshot["events"] = list_codex_team_job_events(str(job_id), limit=500, newest_first_window=True)
        snapshot["sessions"] = list_codex_team_web_sessions(job_id=str(job_id))
        return snapshot

    def list_jobs(self, *, limit: int = 20) -> list[dict[str, Any]]:
        return list_codex_team_jobs(limit=limit)

    def list_sessions(self, *, job_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        return list_codex_team_web_sessions(job_id=job_id, limit=limit)

    def _list_sessions_by_ids(self, session_ids: list[int]) -> list[dict[str, Any]]:
        from .db import connection, row_to_dict

        normalized_ids = [int(item) for item in session_ids if int(item or 0) > 0]
        normalized_ids = list(dict.fromkeys(normalized_ids))
        if not normalized_ids:
            return []

        placeholders = ",".join("?" for _ in normalized_ids)
        with connection() as conn:
            rows = conn.execute(
                f"SELECT * FROM codex_team_web_sessions WHERE id IN ({placeholders}) ORDER BY id DESC",
                tuple(normalized_ids),
            ).fetchall()

        items: list[dict[str, Any]] = []
        for row in rows:
            data = row_to_dict(row) or {}
            try:
                data["cookie_jar"] = json.loads(str(data.get("cookie_jar_json") or "[]"))
            except Exception:
                data["cookie_jar"] = []
            try:
                data["info"] = json.loads(str(data.get("info_json") or "{}"))
            except Exception:
                data["info"] = {}
            items.append(data)
        return items

    def export_cpa_bundle(self, *, job_id: str | None = None, limit: int = 200, session_ids: list[int] | None = None) -> dict[str, Any]:
        rows = self._list_sessions_by_ids(session_ids or []) if session_ids else list_codex_team_web_sessions(job_id=job_id, limit=limit)
        selected = [row for row in rows if str(row.get("status") or "").strip().lower() == "success"]
        if not selected:
            return {"ok": False, "reason": "no_success_accounts", "message": "没有可导出的成功子号"}

        zip_buffer = BytesIO()
        with ZipFile(zip_buffer, "w", compression=ZIP_DEFLATED) as archive:
            export_ts = time.strftime("%Y-%m-%dT%H-%M-%S", time.localtime())
            for index, row in enumerate(selected, start=1):
                payload = SimpleNamespace(
                    email=str(row.get("email") or ""),
                    access_token=str(row.get("access_token") or ""),
                    refresh_token=str(row.get("refresh_token") or ""),
                    id_token=str(row.get("id_token") or ""),
                    account_id=str(row.get("account_id") or ""),
                )
                filename = f"cpa-{export_ts}-{index}.json"
                archive.writestr(
                    f"CPA/{filename}",
                    json.dumps([generate_cpa_token_json(payload)], ensure_ascii=False, indent=2),
                )

        zip_buffer.seek(0)
        return {
            "ok": True,
            "filename": f"codex_team_cpa_export_{int(time.time())}.zip",
            "content": zip_buffer.getvalue(),
            "exported": len(selected),
        }

    def delete_session(self, session_id: int) -> dict[str, Any]:
        from .db import connection

        with connection(write=True) as conn:
            row = conn.execute(
                "SELECT id, job_id FROM codex_team_web_sessions WHERE id = ? LIMIT 1",
                (int(session_id),),
            ).fetchone()
            if row is None:
                return {"ok": False, "reason": "session_not_found"}
            job_id = str(row["job_id"] or "")
            conn.execute("DELETE FROM codex_team_web_sessions WHERE id = ?", (int(session_id),))
        return {"ok": True, "job_id": job_id}

    def delete_sessions(self, session_ids: list[int]) -> dict[str, Any]:
        from .db import connection

        normalized_ids = [int(item) for item in session_ids if int(item or 0) > 0]
        normalized_ids = list(dict.fromkeys(normalized_ids))
        if not normalized_ids:
            return {"ok": True, "deleted": 0, "job_ids": []}

        placeholders = ",".join("?" for _ in normalized_ids)
        with connection(write=True) as conn:
            rows = conn.execute(
                f"SELECT id, job_id FROM codex_team_web_sessions WHERE id IN ({placeholders})",
                tuple(normalized_ids),
            ).fetchall()
            if not rows:
                return {"ok": False, "reason": "session_not_found"}
            job_ids = sorted({str(row["job_id"] or "") for row in rows if str(row["job_id"] or "").strip()})
            conn.execute(
                f"DELETE FROM codex_team_web_sessions WHERE id IN ({placeholders})",
                tuple(normalized_ids),
            )
        return {"ok": True, "deleted": len(rows), "job_ids": job_ids}


codex_team_manager = CodexTeamManager()


def resolve_parent_invite_context(
    parent_credentials: dict[str, Any] | None,
    *,
    merged_config: dict[str, Any] | None = None,
    executor_type: str = "protocol",
    force_refresh: bool = False,
    proxy_url_override: str | None = None,
) -> dict[str, Any]:
    parent = dict(parent_credentials or {})
    merged = dict(merged_config or {})
    proxy_url = str(proxy_url_override or "").strip()
    if not proxy_url:
        proxy_url = str(merged.get("proxy") or "").strip() if merged.get("use_proxy") else ""
    resolved = resolve_parent_invite_context_from_token(
        parent,
        proxy_url=proxy_url or None,
        force_refresh=force_refresh,
    )
    if resolved.get("success"):
        resolved.setdefault("source", "team_manage_style_tokens")
        return resolved
    if (
        str(parent.get("email") or "").strip()
        and str(parent.get("password") or "").strip()
        and str(parent.get("client_id") or "").strip()
        and str(parent.get("refresh_token") or "").strip()
    ):
        login_result = login_parent_via_outlook(
            parent,
            merged_config=merged,
            executor_type=executor_type,
        )
        if login_result.get("success"):
            return {
                "success": True,
                "source": "outlook_mailbox_login",
                "email": str(login_result.get("email") or parent.get("email") or "").strip(),
                "access_token": str(login_result.get("access_token") or ""),
                "session_token": str(login_result.get("session_token") or ""),
                "account_id": str(login_result.get("account_id") or ""),
            }
        return {"success": False, "error": str(login_result.get("error") or "母号邮箱登录失败")}
    return {"success": False, "error": str(resolved.get("error") or "母号参数不可用")}
