from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from .codex_team_manager import codex_team_manager
from .db import (
    batch_import_codex_team_parent_accounts,
    delete_codex_team_parent_account,
    get_codex_team_parent_pool_summary,
    get_config,
    list_codex_team_job_events,
    parse_config_row_values,
)
from .defaults import DEFAULT_CONFIG
from .schemas import CreateCodexTeamJobRequest, ImportCodexTeamParentsRequest

router = APIRouter(prefix="/api/codex-team", tags=["codex-team"])


@router.post("/jobs")
def create_codex_team_job(body: CreateCodexTeamJobRequest):
    payload = body.model_dump()
    merged_config = dict(DEFAULT_CONFIG)
    merged_config.update(parse_config_row_values(get_config()))
    job_id = codex_team_manager.create_job(payload, merged_config=merged_config)
    return {"job_id": job_id}


@router.get("/jobs/{job_id}")
def get_codex_team_job(job_id: str):
    snapshot = codex_team_manager.get_job_snapshot(job_id)
    if not snapshot:
        raise HTTPException(404, "任务不存在")
    return snapshot


@router.post("/jobs/{job_id}/stop")
def stop_codex_team_job(job_id: str):
    result = codex_team_manager.stop_job(job_id)
    if not result.get("ok"):
        raise HTTPException(404, "任务不存在")
    return result


@router.get("/jobs/{job_id}/events")
async def stream_codex_team_events(job_id: str, since: int = 0):
    snapshot = codex_team_manager.get_job_snapshot(job_id)
    if not snapshot:
        raise HTTPException(404, "任务不存在")

    async def event_generator():
        sent = int(since or 0)
        while True:
            events = list_codex_team_job_events(job_id, after_seq=sent)
            for event in events:
                sent = int(event.get("seq") or sent)
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

            snapshot_inner = codex_team_manager.get_job_snapshot(job_id) or {}
            if snapshot_inner.get("status") in {"done", "failed", "stopped"}:
                yield f"data: {json.dumps({'done': True, 'status': snapshot_inner.get('status')}, ensure_ascii=False)}\n\n"
                break

            import asyncio

            await asyncio.sleep(0.75)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/sessions")
def list_codex_team_sessions(job_id: str | None = None, limit: int = 100):
    return {"items": codex_team_manager.list_sessions(job_id=job_id, limit=max(1, min(limit, 200)))}


@router.get("/sessions/export-cpa")
def export_codex_team_sessions_cpa(job_id: str | None = None, limit: int = 200):
    result = codex_team_manager.export_cpa_bundle(job_id=job_id, limit=max(1, min(limit, 500)))
    if not result.get("ok"):
        raise HTTPException(400, str(result.get("message") or "导出失败"))
    from fastapi.responses import StreamingResponse

    return StreamingResponse(
        iter([result.get("content") or b""]),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{str(result.get("filename") or "codex_team_cpa_export.zip")}"'},
    )


@router.delete("/sessions/{session_id}")
def delete_codex_team_session(session_id: int):
    result = codex_team_manager.delete_session(session_id)
    if not result.get("ok"):
        raise HTTPException(404, "子号结果不存在")
    return result


@router.get("/parents")
def get_codex_team_parents():
    return get_codex_team_parent_pool_summary()


@router.post("/parents/import")
def import_codex_team_parents(body: ImportCodexTeamParentsRequest):
    return batch_import_codex_team_parent_accounts(body.data, enabled=body.enabled)


@router.delete("/parents/{parent_id}")
def delete_codex_team_parent(parent_id: int):
    if not delete_codex_team_parent_account(parent_id):
        raise HTTPException(404, "母号池账号不存在")
    return {"ok": True, "summary": get_codex_team_parent_pool_summary()}
