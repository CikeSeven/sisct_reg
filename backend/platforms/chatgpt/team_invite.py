from __future__ import annotations

from typing import Any

import requests

from core.proxy_utils import build_requests_proxy_config
from .chatgpt_client import ChatGPTClient
from .utils import build_browser_headers
from .utils import decode_jwt_payload


INVITE_API_BASE = "https://chatgpt.com/backend-api"
OPENAI_OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
OPENAI_MOBILE_REDIRECT_URI = "com.openai.sora://auth.openai.com/android/com.openai.sora/callback"
CHATGPT_SESSION_URL = "https://chatgpt.com/api/auth/session"


def _build_parent_browser_client(
    *,
    session_token: str,
    account_id: str | None,
    proxy_url: str | None,
) -> ChatGPTClient:
    client = ChatGPTClient(proxy=proxy_url, verbose=False, browser_mode="protocol")
    try:
        client.session.cookies.set(
            "__Secure-next-auth.session-token",
            str(session_token or "").strip(),
            domain=".chatgpt.com",
            path="/",
            secure=True,
        )
    except Exception:
        pass
    if str(account_id or "").strip():
        try:
            client.session.cookies.set(
                "_account",
                str(account_id).strip(),
                domain="chatgpt.com",
                path="/",
            )
            client.session.cookies.set(
                "_account_residency_region",
                "no_constraint",
                domain="chatgpt.com",
                path="/",
            )
        except Exception:
            pass
    setattr(client.session, "_browser_user_agent", client.ua)
    setattr(client.session, "_browser_sec_ch_ua", client.sec_ch_ua)
    setattr(client.session, "_browser_chrome_full", client.chrome_full)
    setattr(client.session, "_browser_accept_language", client.accept_language)
    try:
        client.visit_homepage()
    except Exception:
        pass
    return client


def _build_chatgpt_headers(
    session: Any,
    *,
    url: str,
    access_token: str = "",
    account_id: str = "",
    content_type: str | None = None,
) -> dict[str, str]:
    headers = build_browser_headers(
        url=url,
        user_agent=str(getattr(session, "_browser_user_agent", "") or "Mozilla/5.0"),
        sec_ch_ua=str(getattr(session, "_browser_sec_ch_ua", "") or None),
        chrome_full_version=str(getattr(session, "_browser_chrome_full", "") or None),
        accept="application/json",
        accept_language=str(getattr(session, "_browser_accept_language", "") or "en-US,en;q=0.9"),
        referer="https://chatgpt.com/",
        origin="https://chatgpt.com",
        content_type=content_type,
        fetch_site="same-origin",
        extra_headers={
            "oai-language": "zh-CN",
            "chatgpt-account-id": str(account_id or "").strip() or None,
            "Authorization": f"Bearer {str(access_token or '').strip()}" if access_token else None,
        },
    )
    return headers


def extract_account_id_from_access_token(access_token: str) -> str:
    payload = decode_jwt_payload(str(access_token or "").strip())
    auth_claims = payload.get("https://api.openai.com/auth") or {}
    if isinstance(auth_claims, dict):
        for key in (
            "chatgpt_account_id",
            "account_id",
            "workspace_id",
        ):
            value = str(auth_claims.get(key) or "").strip()
            if value:
                return value
    return ""


def refresh_parent_access_token(
    *,
    client_id: str,
    refresh_token: str,
    timeout: int = 30,
    proxy_url: str | None = None,
) -> dict[str, Any]:
    response = requests.post(
        OPENAI_OAUTH_TOKEN_URL,
        json={
            "client_id": str(client_id or "").strip(),
            "grant_type": "refresh_token",
            "redirect_uri": OPENAI_MOBILE_REDIRECT_URI,
            "refresh_token": str(refresh_token or "").strip(),
        },
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        timeout=timeout,
        proxies=build_requests_proxy_config(proxy_url),
    )
    if response.status_code != 200:
        return {"success": False, "error": f"refresh failed: {response.status_code} - {response.text[:180]}"}
    data = response.json() if response.content else {}
    access_token = str(data.get("access_token") or "").strip()
    if not access_token:
        return {"success": False, "error": "refresh succeeded but access_token missing"}
    return {
        "success": True,
        "access_token": access_token,
        "refresh_token": str(data.get("refresh_token") or refresh_token or "").strip(),
    }


def refresh_parent_access_token_with_session_token(
    *,
    session_token: str,
    account_id: str | None = None,
    timeout: int = 30,
    proxy_url: str | None = None,
) -> dict[str, Any]:
    params = {}
    if str(account_id or "").strip():
        params = {
            "exchange_workspace_token": "true",
            "workspace_id": str(account_id).strip(),
            "reason": "setCurrentAccount",
        }
    browser_client = _build_parent_browser_client(
        session_token=session_token,
        account_id=account_id,
        proxy_url=proxy_url,
    )
    response = browser_client.session.get(
        CHATGPT_SESSION_URL,
        params=params or None,
        headers=_build_chatgpt_headers(browser_client.session, url=CHATGPT_SESSION_URL),
        timeout=timeout,
    )
    if response.status_code != 200:
        return {"success": False, "error": f"session refresh failed: {response.status_code} - {response.text[:180]}"}
    data = response.json() if response.content else {}
    access_token = str(data.get("accessToken") or "").strip()
    if not access_token:
        return {"success": False, "error": "session refresh succeeded but accessToken missing"}
    return {
        "success": True,
        "access_token": access_token,
        "session_token": str(data.get("sessionToken") or session_token or "").strip(),
        "account_id": str(((data.get("account") or {}).get("id") or account_id or "")).strip(),
        "raw_session": data,
        "session": browser_client.session,
    }


def resolve_parent_invite_context(
    parent_credentials: dict[str, Any] | None,
    *,
    proxy_url: str | None = None,
    force_refresh: bool = False,
) -> dict[str, Any]:
    creds = dict(parent_credentials or {})
    access_token = str(creds.get("access_token") or "").strip()
    session_token = str(creds.get("session_token") or "").strip()
    client_id = str(creds.get("client_id") or "").strip()
    refresh_token = str(creds.get("refresh_token") or "").strip()
    provided_account_id = str(creds.get("team_account_id") or creds.get("account_id") or "").strip()

    if session_token and (force_refresh or not access_token):
        refreshed = refresh_parent_access_token_with_session_token(
            session_token=session_token,
            account_id=provided_account_id or None,
            proxy_url=proxy_url,
        )
        if not refreshed.get("success"):
            return refreshed
        account_id = str(refreshed.get("account_id") or provided_account_id or "").strip()
        if not account_id:
            account_id = extract_account_id_from_access_token(str(refreshed.get("access_token") or ""))
        if not account_id:
            return {"success": False, "error": "刷新母号 session token 后仍无法解析 account_id"}
        return {
            "success": True,
            "access_token": str(refreshed.get("access_token") or ""),
            "session_token": str(refreshed.get("session_token") or session_token),
            "account_id": account_id,
            "session": refreshed.get("session"),
        }

    if client_id and refresh_token and (force_refresh or not access_token):
        refreshed = refresh_parent_access_token(client_id=client_id, refresh_token=refresh_token, proxy_url=proxy_url)
        if not refreshed.get("success"):
            return refreshed
        access_token = str(refreshed.get("access_token") or "").strip()
        account_id = provided_account_id or extract_account_id_from_access_token(access_token)
        if not account_id:
            return {"success": False, "error": "刷新母号 access token 后仍无法解析 account_id"}
        return {
            "success": True,
            "access_token": access_token,
            "account_id": account_id,
            "refresh_token": str(refreshed.get("refresh_token") or refresh_token),
            "client_id": client_id,
        }

    if access_token.count(".") >= 2:
        account_id = provided_account_id or extract_account_id_from_access_token(access_token)
        if not account_id:
            return {"success": False, "error": "无法从母号 access token 中解析 account_id"}
        return {
            "success": True,
            "access_token": access_token,
            "account_id": account_id,
            "session_token": session_token,
            "refresh_token": refresh_token,
            "client_id": client_id,
        }

    return {"success": False, "error": "母号邀请缺少有效凭据：请提供 access token，或 client_id + refresh_token"}


def send_team_invite(
    *,
    access_token: str,
    account_id: str,
    email: str,
    session: requests.Session | Any | None = None,
    timeout: int = 30,
    proxy_url: str | None = None,
) -> dict[str, Any]:
    http = session or requests.Session()
    if proxy_url:
        try:
            http.proxies = build_requests_proxy_config(proxy_url)
        except Exception:
            pass
    response = http.post(
        f"{INVITE_API_BASE}/accounts/{str(account_id or '').strip()}/invites",
        headers=_build_chatgpt_headers(
            http,
            url=f"{INVITE_API_BASE}/accounts/{str(account_id or '').strip()}/invites",
            access_token=access_token,
            account_id=account_id,
            content_type="application/json",
        ),
        json={
            "email_addresses": [str(email or "").strip()],
            "role": "standard-user",
            "seat_type": "default",
            "resend_emails": True,
        },
        timeout=timeout,
    )
    if response.status_code != 200:
        return {"success": False, "error": f"invite failed: {response.status_code} - {response.text[:180]}", "invite_id": ""}
    data = response.json() if hasattr(response, "json") else {}
    invites = data.get("account_invites") or []
    errored_emails = data.get("errored_emails") or []
    if errored_emails:
        return {"success": False, "error": f"invite errored: {errored_emails}", "invite_id": ""}
    invite_id = str((invites[0] or {}).get("id") or "").strip() if invites else ""
    return {"success": True, "invite_id": invite_id, "data": data, "error": ""}


def accept_team_invite(
    *,
    access_token: str,
    account_id: str,
    session: requests.Session | Any | None = None,
    timeout: int = 30,
    proxy_url: str | None = None,
) -> dict[str, Any]:
    http = session or requests.Session()
    if proxy_url:
        try:
            http.proxies = build_requests_proxy_config(proxy_url)
        except Exception:
            pass
    response = http.post(
        f"{INVITE_API_BASE}/accounts/{str(account_id or '').strip()}/invites/accept",
        headers=_build_chatgpt_headers(
            http,
            url=f"{INVITE_API_BASE}/accounts/{str(account_id or '').strip()}/invites/accept",
            access_token=access_token,
            account_id=account_id,
            content_type="application/json",
        ),
        timeout=timeout,
    )
    if response.status_code != 200:
        return {"success": False, "error": f"accept failed: {response.status_code} - {response.text[:180]}"}
    data = response.json() if response.content else {}
    return {"success": bool(data.get("success") is True or data == {}), "data": data, "error": "" if data.get("success", True) else str(data)}


def get_team_child_member_count(
    *,
    access_token: str,
    account_id: str,
    session: requests.Session | Any | None = None,
    timeout: int = 30,
    limit: int = 100,
    proxy_url: str | None = None,
) -> dict[str, Any]:
    http = session or requests.Session()
    if proxy_url:
        try:
            http.proxies = build_requests_proxy_config(proxy_url)
        except Exception:
            pass
    offset = 0
    members: list[dict[str, Any]] = []
    total = None
    while True:
        response = http.get(
            f"{INVITE_API_BASE}/accounts/{str(account_id or '').strip()}/users?limit={int(limit)}&offset={int(offset)}",
            headers=_build_chatgpt_headers(
                http,
                url=f"{INVITE_API_BASE}/accounts/{str(account_id or '').strip()}/users",
                access_token=access_token,
                account_id=account_id,
            ),
            timeout=timeout,
        )
        if response.status_code != 200:
            return {"success": False, "error": f"member list failed: {response.status_code} - {response.text[:180]}", "child_count": 0, "members": []}
        data = response.json() if response.content else {}
        items = list(data.get("items") or [])
        members.extend(items)
        total = int(data.get("total") or len(items))
        if len(members) >= total or not items:
            break
        offset += len(items)

    child_members = []
    for item in members:
        role = str(item.get("role") or item.get("account_user_role") or "").strip().lower()
        if role == "account-owner":
            continue
        child_members.append(item)
    return {"success": True, "child_count": len(child_members), "members": child_members, "total": len(members), "error": ""}
