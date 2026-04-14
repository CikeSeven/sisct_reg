from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from fastapi import APIRouter, HTTPException

from .db import get_config, parse_config_row_values
from .defaults import DEFAULT_CONFIG
from .external_uploads import generate_cpa_token_json
from .schemas import OAuthCpaCallbackRequest
from platforms.chatgpt.oauth import generate_oauth_url, submit_callback_url

router = APIRouter(prefix="/api/oauth-cpa", tags=["oauth-cpa"])


def _configured_proxy_url() -> str:
    merged = dict(DEFAULT_CONFIG)
    merged.update(parse_config_row_values(get_config()))
    if not merged.get("use_proxy"):
        return ""
    return str(merged.get("proxy") or "").strip()


def create_oauth_cpa_link() -> dict[str, Any]:
    start = generate_oauth_url()
    return {
        "auth_url": start.auth_url,
        "state": start.state,
        "code_verifier": start.code_verifier,
        "redirect_uri": start.redirect_uri,
    }


def exchange_oauth_callback_for_cpa(
    *,
    callback_url: str,
    state: str,
    code_verifier: str,
    proxy_url: str | None = None,
) -> dict[str, Any]:
    result_json = submit_callback_url(
        callback_url=callback_url,
        expected_state=state,
        code_verifier=code_verifier,
        proxy_url=proxy_url or None,
    )
    oauth_account = json.loads(result_json)
    if not isinstance(oauth_account, dict):
        raise ValueError("OAuth 返回格式异常")

    cpa_account = generate_cpa_token_json(SimpleNamespace(**oauth_account))
    cpa_json = json.dumps(cpa_account, ensure_ascii=False, indent=2)
    return {
        "email": str(cpa_account.get("email") or oauth_account.get("email") or ""),
        "account_id": str(cpa_account.get("account_id") or oauth_account.get("account_id") or ""),
        "oauth_account": oauth_account,
        "cpa_account": cpa_account,
        "cpa_json": cpa_json,
    }


@router.get("/link")
def get_oauth_cpa_link():
    return create_oauth_cpa_link()


@router.post("/exchange")
def exchange_oauth_cpa_callback(body: OAuthCpaCallbackRequest):
    callback_url = str(body.callback_url or "").strip()
    state = str(body.state or "").strip()
    code_verifier = str(body.code_verifier or "").strip()
    if not callback_url:
        raise HTTPException(400, "回调链接不能为空")
    if not state or not code_verifier:
        raise HTTPException(400, "OAuth 会话参数缺失，请重新生成授权链接")

    try:
        return exchange_oauth_callback_for_cpa(
            callback_url=callback_url,
            state=state,
            code_verifier=code_verifier,
            proxy_url=_configured_proxy_url(),
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(400, str(exc or "OAuth 回调处理失败")) from exc
