from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from io import BytesIO
from types import SimpleNamespace
from typing import Any, Callable
from zipfile import ZIP_DEFLATED, ZipFile

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from .codex_team_parent_login import parse_outlook_parent_import_data
from .db import acquire_proxy_pool_entry, get_config, get_proxy_pool_summary, parse_config_row_values
from .defaults import DEFAULT_CONFIG
from .external_uploads import generate_cpa_token_json
from .mail_providers import build_mail_provider
from .schemas import CodexAuthBatchExportRequest, StartCodexAuthBatchJobRequest
from platforms.chatgpt.chatgpt_web_auth import ChatGPTWebAuthClient
from platforms.chatgpt.refresh_token_registration_engine import EmailServiceAdapter

router = APIRouter(prefix="/api/codex-auth-batch", tags=["codex-auth-batch"])


@dataclass
class CodexAuthBatchJob:
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
    results: list[dict[str, Any]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


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

def authorize_codex_account_via_outlook(
    account: dict[str, Any],
    *,
    merged_config: dict[str, Any] | None,
    executor_type: str = "protocol",
    log_fn: Callable[[str], None] | None = None,
    interrupt_check: Callable[[], None] | None = None,
) -> dict[str, Any]:
    item = dict(account or {})
    merged = dict(merged_config or {})
    email = str(item.get("email") or "").strip().lower()
    password = str(item.get("password") or "").strip()
    client_id = str(item.get("client_id") or "").strip()
    refresh_token = str(item.get("refresh_token") or "").strip()
    if not (email and password and client_id and refresh_token):
        return {"success": False, "error": "微软邮箱参数不完整"}

    logger = log_fn or (lambda _message: None)
    proxy_url = _configured_proxy_url(merged)
    provider_config = dict(merged)
    provider_config["retry_email_binding"] = {
        "email": email,
        "password": password,
        "client_id": client_id,
        "refresh_token": refresh_token,
    }

    provider = build_mail_provider(
        "outlook_local",
        config=provider_config,
        proxy=proxy_url or None,
        fixed_email=email,
        log_fn=logger,
    )
    provider.create_email(provider_config)
    email_adapter = EmailServiceAdapter(
        provider,
        email,
        logger,
        interrupt_check=interrupt_check,
    )

    client = ChatGPTWebAuthClient(
        config=provider_config,
        proxy=proxy_url or None,
        verbose=False,
        browser_mode=str(executor_type or "protocol"),
    )
    client._log = lambda message: logger(str(message or ""))

    tokens = client.login_and_get_tokens(
        email=email,
        password=password,
        device_id="",
        user_agent=None,
        sec_ch_ua=None,
        impersonate=None,
        skymail_client=email_adapter,
        prefer_passwordless_login=True,
        allow_phone_verification=False,
        force_new_browser=True,
        force_chatgpt_entry=True,
        screen_hint="login",
        force_password_login=False,
        complete_about_you_if_needed=False,
        login_source="codex_auth_batch",
    )
    if not tokens:
        return {"success": False, "error": str(client.last_error or "Codex 授权失败")}

    result_obj = SimpleNamespace(
        email=email,
        access_token=str(tokens.get("access_token") or ""),
        refresh_token=str(tokens.get("refresh_token") or ""),
        id_token=str(tokens.get("id_token") or ""),
        account_id=str(getattr(client, "last_workspace_id", "") or ""),
    )
    cpa_account = generate_cpa_token_json(result_obj)
    return {
        "success": True,
        "email": email,
        "account_id": str(cpa_account.get("account_id") or result_obj.account_id or ""),
        "cpa_account": cpa_account,
        "cpa_json": json.dumps(cpa_account, ensure_ascii=False, indent=2),
    }


class CodexAuthBatchManager:
    def __init__(self) -> None:
        self._jobs: dict[str, CodexAuthBatchJob] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()

    def create_job(self, *, data: str, merged_config: dict[str, Any], executor_type: str = "protocol") -> str:
        job_id = f"codex_auth_batch_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
        job = CodexAuthBatchJob(
            id=job_id,
            raw_data=str(data or ""),
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

        buffer = BytesIO()
        with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as archive:
            for item in items:
                email = str(item.get("email") or "account").strip() or "account"
                safe_name = "".join(ch if ch.isalnum() or ch in {'.', '_', '-'} else '_' for ch in email)
                archive.writestr(f"{safe_name}.json", str(item.get("cpa_json") or ""))
        return {
            "ok": True,
            "filename": f"codex_auth_batch_{job_id}.zip",
            "content": buffer.getvalue(),
            "count": len(items),
        }


    def stop_job(self, job_id: str) -> dict[str, Any]:
        job = self._jobs.get(str(job_id))
        if not job:
            return {"ok": False, "reason": "job_not_found"}
        job.stop_requested = True
        with job.lock:
            if job.status in {"pending", "running"}:
                job.status = "stopped"
        self._append_event(job, "已请求停止批量 Codex 授权", level="error")
        return {"ok": True}

    def _append_event(self, job: CodexAuthBatchJob, message: str, *, level: str = "info", account_email: str = "") -> None:
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

    def _check_stop(self, job: CodexAuthBatchJob) -> None:
        if job.stop_requested:
            raise RuntimeError("批量 Codex 授权已停止")

    def _run_job(self, job: CodexAuthBatchJob) -> None:
        with job.lock:
            job.status = "running"
        try:
            entries = parse_outlook_parent_import_data(job.raw_data)
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
        self._append_event(job, f"开始批量 Codex 授权，共 {len(entries)} 个账号")
        self._append_event(job, f"代理来源：{_describe_proxy_source(runtime_config)}")

        for item in entries:
            email = str(item.get("email") or "").strip().lower()
            try:
                self._check_stop(job)
                self._append_event(job, "开始 Codex 授权登录", account_email=email)
                result = authorize_codex_account_via_outlook(
                    item,
                    merged_config=runtime_config,
                    executor_type=job.executor_type,
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


codex_auth_batch_manager = CodexAuthBatchManager()


@router.post("/jobs")
def create_codex_auth_batch_job(body: StartCodexAuthBatchJobRequest):
    merged_config = dict(DEFAULT_CONFIG)
    merged_config.update(parse_config_row_values(get_config()))
    job_id = codex_auth_batch_manager.create_job(
        data=body.data,
        merged_config=merged_config,
        executor_type=body.executor_type,
    )
    return {"job_id": job_id}


@router.get("/jobs")
def list_codex_auth_batch_jobs(limit: int = 20):
    return {"items": codex_auth_batch_manager.list_jobs(limit=limit)}


@router.get("/jobs/{job_id}")
def get_codex_auth_batch_job(job_id: str):
    snapshot = codex_auth_batch_manager.get_job_snapshot(job_id)
    if not snapshot:
        raise HTTPException(404, "任务不存在")
    return snapshot


@router.post("/jobs/{job_id}/export")
def export_codex_auth_batch_job(job_id: str, body: CodexAuthBatchExportRequest):
    result = codex_auth_batch_manager.export_results(job_id, emails=body.emails)
    if not result.get("ok"):
        reason = str(result.get("reason") or "")
        if reason == "job_not_found":
            raise HTTPException(404, "任务不存在")
        raise HTTPException(400, "没有可导出的成功账号")
    return StreamingResponse(
        iter([result.get("content") or b""]),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{str(result.get("filename") or "codex_auth_batch.zip")}"'},
    )


@router.post("/jobs/{job_id}/stop")
def stop_codex_auth_batch_job(job_id: str):
    result = codex_auth_batch_manager.stop_job(job_id)
    if not result.get("ok"):
        raise HTTPException(404, "任务不存在")
    return result
