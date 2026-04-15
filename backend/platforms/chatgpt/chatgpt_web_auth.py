from __future__ import annotations

from typing import Any, Iterable

from .chatgpt_client import ChatGPTClient
from .invite_link import extract_invite_workspace_ids
from .oauth_client import FlowState, OAuthClient
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

    def _build_chatgpt_web_login_client(self) -> ChatGPTClient:
        client = ChatGPTClient(
            proxy=self.proxy,
            verbose=False,
            browser_mode=self.browser_mode,
            extra_config=dict(self.config or {}),
        )
        client._log = lambda message: self._log(f"[Web Login] {str(message or '')}")
        return client

    def _complete_chatgpt_web_callback(
        self,
        callback_url: str,
        *,
        referer: str = "https://auth.openai.com/workspace",
    ) -> dict[str, Any]:
        try:
            response = self.session.get(
                callback_url,
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Referer": referer,
                    "Origin": "https://chatgpt.com",
                },
                allow_redirects=True,
                timeout=30,
            )
            self._log(f"普通登录 callback -> {response.status_code} {str(response.url)[:120]}")
        except Exception as exc:
            return self._failed_result("", f"普通登录 callback 失败: {exc}")

        session_snapshot = extract_web_session_from_cookies(self.session.cookies)
        session_snapshot.update(
            {
                "selected_workspace_id": str(session_snapshot.get("account_id") or ""),
                "selected_workspace_kind": "organization" if session_snapshot.get("account_id") else "",
                "callback_completed": bool(200 <= int(getattr(response, "status_code", 0) or 0) < 400),
                "workspace_selected": bool(session_snapshot.get("account_id")),
            }
        )
        session_snapshot["success"] = bool(
            session_snapshot.get("callback_completed")
            and str(response.url or "").startswith("https://chatgpt.com/")
        )
        if not session_snapshot["success"] and not session_snapshot.get("error"):
            session_snapshot["error"] = "普通登录 callback 未成功回到 chatgpt.com"
        return session_snapshot

    def login_and_select_workspace(
        self,
        *,
        email: str,
        password: str,
        skymail_client: Any = None,
        preferred_workspace_id: str = "",
    ) -> dict[str, Any]:
        login_client = self._build_chatgpt_web_login_client()
        if not login_client.visit_homepage():
            return self._failed_result(email, "普通登录访问首页失败")
        csrf_token = login_client.get_csrf_token()
        if not csrf_token:
            return self._failed_result(email, "普通登录获取 CSRF token 失败")
        auth_url = login_client.signin(email, csrf_token)
        if not auth_url:
            return self._failed_result(email, "普通登录提交邮箱失败")
        final_url = login_client.authorize(auth_url)
        if not final_url:
            return self._failed_result(email, "普通登录 authorize 失败")

        self.adopt_browser_context(
            login_client.session,
            device_id=getattr(login_client, "device_id", "") or "",
            user_agent=getattr(login_client, "ua", None),
            sec_ch_ua=getattr(login_client, "sec_ch_ua", None),
            accept_language=(
                getattr(login_client.session, "headers", {}).get("Accept-Language", "")
                if getattr(login_client, "session", None) is not None
                else ""
            ),
        )
        self.impersonate = str(getattr(login_client, "impersonate", "") or self.impersonate or "").strip()

        state = self._state_from_url(final_url)
        self._log(f"普通登录状态起点: {state.page_type or '-'} {str(state.current_url or state.continue_url or final_url)[:120]}")

        for hop in range(3):
            state_url = str(state.current_url or state.continue_url or final_url).lower()
            is_authorize_entry = "/api/accounts/authorize" in state_url
            if self._state_is_login_password(state) or self._state_is_email_otp(state) or (
                (not is_authorize_entry) and self._state_supports_workspace_resolution(state)
            ):
                break
            should_follow = self._state_requires_navigation(state) or is_authorize_entry
            if not should_follow:
                break
            callback_code, next_state = self._follow_flow_state(
                state,
                referer=final_url,
                user_agent=self.ua or None,
                impersonate=self.impersonate or None,
            )
            if callback_code:
                callback_result = self._complete_chatgpt_web_callback(
                    next_state.current_url or next_state.continue_url or final_url,
                    referer=final_url,
                )
                callback_result["email"] = email
                callback_result["preferred_workspace_id"] = str(preferred_workspace_id or "")
                return callback_result
            state = next_state
            self._log(
                f"普通登录 follow[{hop + 1}] -> {state.page_type or '-'} {str(state.current_url or state.continue_url or '-')[:120]}"
            )

        if self._state_is_login_password(state):
            state = self._submit_password_verify(
                password,
                self.device_id,
                user_agent=self.ua or None,
                sec_ch_ua=self.sec_ch_ua or None,
                impersonate=self.impersonate or None,
                referer=state.current_url or state.continue_url or final_url,
            )
            if not state:
                return self._failed_result(email, self.last_error or "普通登录密码验证失败")
        elif self._state_is_email_otp(state):
            if not skymail_client:
                return self._failed_result(email, "普通登录需要邮箱 OTP，但缺少接码客户端")
            next_state = self._handle_otp_verification(
                email,
                self.device_id,
                self.ua or None,
                self.sec_ch_ua or None,
                self.impersonate or None,
                skymail_client,
                state,
                prefer_passwordless_login=False,
                allow_cached_code_retry=False,
            )
            if not next_state:
                return self._failed_result(email, self.last_error or "普通登录 OTP 验证失败")
            state = next_state

        if self._state_is_about_you(state):
            self._log("普通登录命中 about_you，先提交姓名和生日")
            state = self._submit_about_you_create_account(
                "Codex",
                "Team",
                "1996-01-01",
                self.device_id,
                user_agent=self.ua or None,
                sec_ch_ua=self.sec_ch_ua or None,
                impersonate=self.impersonate or None,
                referer=state.current_url or state.continue_url or final_url,
            )
            if not state:
                return self._failed_result(email, self.last_error or "普通登录提交 about_you 失败")

        callback_url = str(state.current_url or state.continue_url or "").strip()
        callback_code = self._extract_code_from_url(callback_url)
        if callback_code and "chatgpt.com/api/auth/callback/openai" in callback_url:
            self._log("普通登录已直接进入 chatgpt callback，跳过工作区选择，继续建立 Web 会话")
            callback_result = self._complete_chatgpt_web_callback(
                callback_url,
                referer=state.current_url or state.continue_url or final_url,
            )
            callback_result["email"] = email
            callback_result["preferred_workspace_id"] = str(preferred_workspace_id or "")
            callback_result.setdefault("workspace_selected", False)
            return callback_result

        if not self._state_supports_workspace_resolution(state):
            return self._failed_result(
                email,
                f"普通登录后未进入可选工作区状态: {state.page_type or state.current_url or state.continue_url or '-'}",
            )

        previous_preferred = str(self.config.get("preferred_workspace_id") or "")
        if str(preferred_workspace_id or "").strip():
            self.config["preferred_workspace_id"] = str(preferred_workspace_id).strip()
        try:
            callback_code, workspace_state = self._oauth_submit_workspace_and_org(
                state.continue_url or state.current_url or final_url,
                self.device_id,
                self.ua or None,
                self.impersonate or None,
            )
        finally:
            if previous_preferred:
                self.config["preferred_workspace_id"] = previous_preferred
            else:
                self.config.pop("preferred_workspace_id", None)

        callback_url = str(
            (workspace_state or FlowState()).current_url
            or (workspace_state or FlowState()).continue_url
            or ""
        ).strip()
        if callback_code and "chatgpt.com/api/auth/callback/openai" in callback_url:
            callback_result = self._complete_chatgpt_web_callback(
                callback_url,
                referer=state.current_url or state.continue_url or final_url,
            )
            callback_result["email"] = email
            callback_result["preferred_workspace_id"] = str(preferred_workspace_id or "")
            return callback_result

        return self._failed_result(
            email,
            self.last_error or "普通登录完成工作区选择后，未进入 chatgpt.com callback",
        )

    def login_and_get_session(
        self,
        *,
        email: str,
        password: str,
        skymail_client: Any = None,
        executor_type: str = "protocol",
        invite_link: str = "",
        force_new_browser: bool = True,
        force_chatgpt_entry: bool = True,
        login_source: str = "child_invite_register",
        require_web_session: bool = True,
    ) -> dict[str, Any]:
        invite_workspace = extract_invite_workspace_ids(invite_link) if invite_link else {}
        tokens = self.login_and_get_tokens(
            email=email,
            password=password,
            device_id=str(getattr(self, "device_id", "") or ""),
            user_agent=getattr(self, "ua", None),
            sec_ch_ua=getattr(self, "sec_ch_ua", None),
            impersonate=getattr(self, "impersonate", None),
            skymail_client=skymail_client,
            prefer_passwordless_login=True,
            allow_phone_verification=False,
            force_new_browser=force_new_browser,
            force_chatgpt_entry=force_chatgpt_entry,
            screen_hint="login",
            force_password_login=False,
            complete_about_you_if_needed=True,
            first_name="Codex",
            last_name="Team",
            birthdate="1996-01-01",
            login_source=login_source,
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
        if (not require_web_session) and (not enriched.get("success")):
            selected_workspace_id = str(
                enriched.get("selected_workspace_id")
                or getattr(self, "last_workspace_id", "")
                or session_snapshot.get("account_id")
                or ""
            ).strip()
            if selected_workspace_id:
                enriched["selected_workspace_id"] = selected_workspace_id
                enriched.setdefault("selected_workspace_kind", "organization")
            if not str(enriched.get("account_id") or "").strip() and selected_workspace_id:
                enriched["account_id"] = selected_workspace_id
            enriched["success"] = bool(
                str(enriched.get("access_token") or "").strip()
                and str(enriched.get("selected_workspace_id") or enriched.get("account_id") or "").strip()
            )
            if enriched.get("success") and str(enriched.get("error") or "") == "缺少 chatgpt.com 会话 Cookie":
                enriched["error"] = ""
            info = dict(enriched.get("info") or {})
            info["web_session_optional"] = True
            info["web_session_present"] = bool(enriched.get("next_auth_session_token"))
            enriched["info"] = info
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
