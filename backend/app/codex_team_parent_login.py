from __future__ import annotations

import json
from typing import Any, Callable

from .db import batch_import_codex_team_parent_accounts, update_codex_team_parent_account
from .mail_providers import build_mail_provider
from platforms.chatgpt.chatgpt_client import ChatGPTClient
from platforms.chatgpt.chatgpt_web_auth import ChatGPTWebAuthClient
from platforms.chatgpt.refresh_token_registration_engine import EmailServiceAdapter


def parse_outlook_parent_import_data(data: str) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for index, raw_line in enumerate(str(data or "").splitlines(), start=1):
        line = str(raw_line or "").strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split("----")]
        if len(parts) != 4:
            raise ValueError(f"行 {index}: 格式错误，应为 邮箱----密码----client_id----refresh_token")
        email, password, client_id, refresh_token = parts
        if not email or "@" not in email:
            raise ValueError(f"行 {index}: 邮箱格式无效")
        if not password:
            raise ValueError(f"行 {index}: 密码不能为空")
        if not client_id:
            raise ValueError(f"行 {index}: client_id 不能为空")
        if not refresh_token:
            raise ValueError(f"行 {index}: refresh_token 不能为空")
        items.append(
            {
                "email": email.lower(),
                "password": password,
                "client_id": client_id,
                "refresh_token": refresh_token,
            }
        )
    return items


def login_parent_via_outlook(
    parent: dict[str, Any],
    *,
    merged_config: dict[str, Any] | None,
    executor_type: str = "protocol",
    log_fn: Callable[[str], None] | None = None,
    interrupt_check: Callable[[], None] | None = None,
) -> dict[str, Any]:
    parent = dict(parent or {})
    merged = dict(merged_config or {})
    email = str(parent.get("email") or "").strip().lower()
    password = str(parent.get("password") or "").strip()
    client_id = str(parent.get("client_id") or "").strip()
    refresh_token = str(parent.get("refresh_token") or "").strip()
    if not (email and password and client_id and refresh_token):
        return {"success": False, "error": "母号微软邮箱参数不完整"}

    proxy_url = str(merged.get("proxy") or "").strip() if merged.get("use_proxy") else ""
    logger = log_fn or (lambda _message: None)
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
        complete_about_you_if_needed=True,
        first_name="Codex",
        last_name="Team",
        birthdate="1996-01-01",
        login_source="parent_outlook_import",
    )
    if not tokens:
        return {"success": False, "error": str(client.last_error or "母号登录失败")}

    access_token = str(tokens.get("access_token") or "").strip()
    refresh_token_value = str(tokens.get("refresh_token") or "").strip()
    id_token = str(tokens.get("id_token") or "").strip()

    logger("OAuth 登录成功，开始补取 chatgpt.com Web 会话...")
    session_client = ChatGPTClient(
        proxy=proxy_url or None,
        verbose=False,
        browser_mode=str(executor_type or "protocol"),
        extra_config=provider_config,
    )
    session_client._log = lambda message: logger(str(message or ""))
    session_client.session = client.session
    session_client.device_id = str(getattr(client, "device_id", "") or session_client.device_id)
    session_client.ua = str(getattr(client, "ua", "") or session_client.ua)
    session_client.sec_ch_ua = str(getattr(client, "sec_ch_ua", "") or session_client.sec_ch_ua)
    session_client.chrome_full = str(getattr(client, "chrome_full", "") or session_client.chrome_full)
    session_client.impersonate = str(getattr(client, "impersonate", "") or session_client.impersonate)
    session_client.accept_language = str(getattr(client, "accept_language", "") or session_client.accept_language)

    try:
        session_client.visit_homepage()
    except Exception:
        pass
    account_id = str(getattr(client, "last_workspace_id", "") or "").strip()
    raw_session: dict[str, Any] = {}
    session_token = ""
    ok, session_data = session_client.fetch_chatgpt_session()
    if ok:
        raw_session = dict(session_data or {})
        access_token = str(raw_session.get("accessToken") or access_token or "").strip()
        session_token = str(raw_session.get("sessionToken") or "").strip()
        account_id = str(((raw_session.get("account") or {}).get("id") or account_id or "")).strip()
    else:
        logger("直接请求 /api/auth/session 失败，按 access_token + workspace_id 回退导入")

    if not account_id:
        account_id = str(getattr(client, "last_workspace_id", "") or "").strip()
    if not (account_id and access_token):
        return {"success": False, "error": "母号登录成功但未提取到完整会话"}

    info = dict((raw_session.get("account") or {}))
    session_json = {
        "user": {"email": email},
        "account": {
            "id": account_id,
            "planType": str(info.get("planType") or info.get("plan_type") or ""),
        },
        "accessToken": access_token,
        "sessionToken": session_token,
        "authProvider": "openai",
    }
    import_result = batch_import_codex_team_parent_accounts(
        json.dumps(session_json, ensure_ascii=False),
        enabled=True,
    )
    imported_accounts = list(import_result.get("accounts") or [])
    imported_id = int((imported_accounts[0] or {}).get("id") or 0) if imported_accounts else 0
    if imported_id > 0:
        update_codex_team_parent_account(
            imported_id,
            password=password,
            client_id=client_id,
            refresh_token=refresh_token,
            team_account_id=account_id,
            team_name=str(info.get("planType") or info.get("plan_type") or ""),
        )
    return {
        "success": True,
        "email": email,
        "account_id": account_id,
        "access_token": access_token,
        "session_token": session_token,
        "refresh_token": refresh_token_value,
        "id_token": id_token,
        "session_json": session_json,
        "import_result": import_result,
        "parent_id": imported_id,
        "raw_result": raw_session,
    }
