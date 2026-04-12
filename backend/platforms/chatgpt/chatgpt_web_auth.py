from __future__ import annotations

from typing import Any, Iterable

from .invite_link import extract_invite_workspace_ids
from .oauth_client import OAuthClient
from .utils import decode_jwt_payload


def choose_team_workspace(workspaces: Iterable[dict[str, Any]] | None) -> dict[str, Any] | None:
    items = [dict(item or {}) for item in (workspaces or []) if isinstance(item, dict)]
    if not items:
        return None
    for item in items:
        if str(item.get("kind") or "").strip().lower() == "organization" and str(item.get("id") or "").strip():
            return item
    return None


def extract_web_session_from_cookies(cookies: Iterable[Any] | None) -> dict[str, Any]:
    cookie_jar: list[dict[str, str]] = []
    chunked_next_auth: dict[int, str] = {}
    account_id = ""
    device_id = ""
    residency_region = ""

    for cookie in cookies or []:
        name = str(getattr(cookie, "name", "") or "").strip()
        value = str(getattr(cookie, "value", "") or "")
        domain = str(getattr(cookie, "domain", "") or "")
        path = str(getattr(cookie, "path", "") or "/")
        if not name:
            continue
        cookie_jar.append({"name": name, "value": value, "domain": domain, "path": path})
        lowered = name.lower()
        if name == "_account":
            account_id = value
        elif name == "oai-did":
            device_id = value
        elif name == "_account_residency_region":
            residency_region = value
        elif lowered.startswith("__secure-next-auth.session-token."):
            try:
                idx = int(name.rsplit(".", 1)[-1])
            except Exception:
                continue
            chunked_next_auth[idx] = value

    next_auth_session_token = ""
    if chunked_next_auth:
        next_auth_session_token = "".join(value for _, value in sorted(chunked_next_auth.items()))

    return {
        "account_id": account_id,
        "device_id": device_id,
        "account_residency_region": residency_region,
        "next_auth_session_token": next_auth_session_token,
        "cookie_jar": cookie_jar,
    }


def build_child_session_summary(
    *,
    session_snapshot: dict[str, Any],
    access_token: str,
    refresh_token: str,
    id_token: str,
    account_info: dict[str, Any] | None,
    me_payload: dict[str, Any] | None,
    accounts_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    session_data = dict(session_snapshot or {})
    account_info = dict(account_info or {})
    me_payload = dict(me_payload or {})
    accounts_payload = dict(accounts_payload or {})
    selected_workspace_id = str(session_data.get("selected_workspace_id") or session_data.get("account_id") or "").strip()
    workspace_entry = {}
    accounts_map = accounts_payload.get("accounts") or {}
    if isinstance(accounts_map, dict):
        workspace_entry = dict((accounts_map.get(selected_workspace_id) or {}).get("account") or {})
    session_data.update(
        {
            "access_token": str(access_token or ""),
            "refresh_token": str(refresh_token or ""),
            "id_token": str(id_token or ""),
            "user_id": str(me_payload.get("id") or account_info.get("user_id") or "").strip(),
            "display_name": str(me_payload.get("name") or workspace_entry.get("name") or "").strip(),
            "plan_type": str(workspace_entry.get("plan_type") or "").strip(),
            "account_role": str(workspace_entry.get("account_user_role") or "").strip(),
            "profile_email": str(me_payload.get("email") or account_info.get("email") or "").strip(),
            "info": {
                "account_info": account_info,
                "me": me_payload,
                "accounts": accounts_payload,
                "plan_type": str(workspace_entry.get("plan_type") or "").strip(),
                "account_role": str(workspace_entry.get("account_user_role") or "").strip(),
            },
        }
    )
    return session_data


class ChatGPTWebAuthClient(OAuthClient):
    def __init__(self, config: dict[str, Any] | None = None, **kwargs):
        super().__init__(config=config or {}, **kwargs)

    def login_and_get_session(
        self,
        *,
        email: str,
        password: str,
        skymail_client: Any = None,
        executor_type: str = "protocol",
        invite_link: str = "",
    ) -> dict[str, Any]:
        invite_workspace = extract_invite_workspace_ids(invite_link) if invite_link else {}
        tokens = self.login_and_get_tokens(
            email=email,
            password=password,
            device_id="",
            user_agent=None,
            sec_ch_ua=None,
            impersonate=None,
            skymail_client=skymail_client,
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
            login_source="child_invite_register",
        )
        if not tokens:
            return self._failed_result(email, self.last_error or "子号 OAuth 登录失败")

        session_snapshot = extract_web_session_from_cookies(self.session.cookies)
        session_snapshot.update(
            {
                "email": email,
                "selected_workspace_id": str(getattr(self, "last_workspace_id", "") or session_snapshot.get("account_id") or ""),
                "selected_workspace_kind": "organization",
                "invite_workspace_id": str(invite_workspace.get("accept_workspace_id") or invite_workspace.get("workspace_id") or ""),
                "invite_link": str(invite_link or ""),
            }
        )
        enriched = self._enrich_child_session(session_snapshot, tokens=tokens)
        enriched["invite_workspace_id"] = str(invite_workspace.get("accept_workspace_id") or invite_workspace.get("workspace_id") or "")
        enriched["invite_link"] = str(invite_link or "")
        enriched["email"] = email
        return enriched

    def _enrich_child_session(self, session_snapshot: dict[str, Any], *, tokens: dict[str, Any]) -> dict[str, Any]:
        access_token = str(tokens.get("access_token") or "")
        refresh_token = str(tokens.get("refresh_token") or "")
        id_token = str(tokens.get("id_token") or "")
        access_payload = decode_jwt_payload(access_token)
        auth_claims = access_payload.get("https://api.openai.com/auth") or {}
        profile_claims = access_payload.get("https://api.openai.com/profile") or {}
        account_info = {
            "email": str(profile_claims.get("email") or "").strip(),
            "account_id": str(auth_claims.get("chatgpt_account_id") or session_snapshot.get("account_id") or "").strip(),
            "user_id": str(auth_claims.get("chatgpt_user_id") or auth_claims.get("user_id") or "").strip(),
            "claims": access_payload,
        }
        me_payload = self._get_backend_json(
            "https://chatgpt.com/backend-api/me",
            access_token=access_token,
            account_id=str(session_snapshot.get("selected_workspace_id") or session_snapshot.get("account_id") or ""),
        )
        accounts_payload = self._get_backend_json(
            "https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27?timezone_offset_min=-480",
            access_token=access_token,
            account_id=str(session_snapshot.get("selected_workspace_id") or session_snapshot.get("account_id") or ""),
        )
        result = build_child_session_summary(
            session_snapshot=session_snapshot,
            access_token=access_token,
            refresh_token=refresh_token,
            id_token=id_token,
            account_info=account_info,
            me_payload=me_payload,
            accounts_payload=accounts_payload,
        )
        result["success"] = bool(result.get("account_id") and result.get("next_auth_session_token"))
        if not result["success"] and not result.get("error"):
            result["error"] = "缺少 chatgpt.com 会话 Cookie"
        return result

    def _get_backend_json(self, url: str, *, access_token: str, account_id: str) -> dict[str, Any]:
        if not access_token:
            return {}
        try:
            headers = {
                "Authorization": f"Bearer {access_token}",
                "chatgpt-account-id": str(account_id or ""),
                "oai-language": "zh-CN",
                "Referer": "https://chatgpt.com/",
                "Origin": "https://chatgpt.com",
            }
            response = self.session.get(url, headers=headers, timeout=30)
            if response.status_code != 200:
                return {}
            if not response.content:
                return {}
            return response.json()
        except Exception:
            return {}

    @staticmethod
    def _failed_result(email: str, error: str) -> dict[str, Any]:
        return {
            "success": False,
            "email": str(email or ""),
            "selected_workspace_id": "",
            "selected_workspace_kind": "",
            "account_id": "",
            "next_auth_session_token": "",
            "cookie_jar": [],
            "error": str(error or ""),
        }
