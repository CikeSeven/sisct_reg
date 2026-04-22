from __future__ import annotations

from fastapi import APIRouter, HTTPException

from .db import get_config, parse_config_row_values, set_config
from .defaults import DEFAULT_CONFIG
from .schemas import CreateTeamOpenJobRequest, ImportTeamOpenCardsRequest, UpdateConfigRequest
from .team_open_manager import team_open_manager
from .team_open_store import (
    delete_team_open_card,
    get_team_open_card_summary,
    import_team_open_cards,
)

router = APIRouter(prefix="/api/team-open", tags=["team-open"])


@router.get("/config")
def get_team_open_config():
    merged = dict(DEFAULT_CONFIG)
    merged.update(parse_config_row_values(get_config()))
    return {key: merged.get(key) for key in sorted(merged.keys()) if key.startswith("team_open_")}


@router.put("/config")
def save_team_open_config(body: UpdateConfigRequest):
    values = {key: value for key, value in dict(body.values or {}).items() if str(key).startswith("team_open_")}
    if values:
        set_config(values)
    merged = dict(DEFAULT_CONFIG)
    merged.update(parse_config_row_values(get_config()))
    return {
        "ok": True,
        "config": {key: merged.get(key) for key in sorted(merged.keys()) if key.startswith("team_open_")},
    }


@router.get("/cards")
def list_team_open_cards():
    return get_team_open_card_summary(limit=200)


@router.post("/cards/import")
def import_team_open_cards_api(body: ImportTeamOpenCardsRequest):
    return import_team_open_cards(
        body.data,
        enabled=body.enabled,
        default_holder_name=body.default_holder_name,
        default_billing_email=body.default_billing_email,
        default_country=body.default_country,
        default_state=body.default_state,
        default_city=body.default_city,
        default_line1=body.default_line1,
        default_postal_code=body.default_postal_code,
    )


@router.delete("/cards/{card_id}")
def delete_team_open_card_api(card_id: int):
    if not delete_team_open_card(card_id):
        raise HTTPException(404, "银行卡不存在")
    return {"ok": True, "summary": get_team_open_card_summary(limit=200)}


@router.post("/jobs")
def create_team_open_job(body: CreateTeamOpenJobRequest):
    merged_config = dict(DEFAULT_CONFIG)
    merged_config.update(parse_config_row_values(get_config()))
    try:
        job_id = team_open_manager.create_job(body.model_dump(), merged_config=merged_config)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"job_id": job_id}


@router.get("/jobs")
def list_team_open_jobs(limit: int = 20):
    return {"items": team_open_manager.list_jobs(limit=max(1, min(limit, 100)))}


@router.get("/jobs/{job_id}")
def get_team_open_job(job_id: str):
    snapshot = team_open_manager.get_job_snapshot(job_id)
    if not snapshot:
        raise HTTPException(404, "任务不存在")
    return snapshot


@router.post("/jobs/{job_id}/stop")
def stop_team_open_job(job_id: str):
    result = team_open_manager.stop_job(job_id)
    if not result.get("ok"):
        raise HTTPException(404, "任务不存在")
    return result
