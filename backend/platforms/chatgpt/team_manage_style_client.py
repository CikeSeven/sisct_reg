from __future__ import annotations

import random
import time
from typing import Any

from curl_cffi.requests import Session

from core.proxy_utils import build_requests_proxy_config, instrument_session_proxy_requests


class TeamManageStyleClient:
    BASE_URL = "https://chatgpt.com/backend-api"
    MAX_RETRIES = 3
    RETRY_DELAYS = [1, 2, 4]

    def __init__(self, *, proxy_url: str | None = None):
        self.proxy_url = proxy_url
        self._sessions: dict[str, Session] = {}

    def _get_session(self, identifier: str) -> Session:
        key = str(identifier or "default")
        if key not in self._sessions:
            session = Session(
                impersonate="chrome110",
                proxies=build_requests_proxy_config(self.proxy_url),
                timeout=30,
                verify=False,
            )
            instrument_session_proxy_requests(session, proxy_url=self.proxy_url)
            self._sessions[key] = session
        return self._sessions[key]

    def _make_request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        json_data: dict[str, Any] | None = None,
        identifier: str,
    ) -> dict[str, Any]:
        session = self._get_session(identifier)
        base_headers = {
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://chatgpt.com/",
            "Origin": "https://chatgpt.com",
            "Connection": "keep-alive",
        }
        merged_headers = {**base_headers, **headers}

        for attempt in range(self.MAX_RETRIES):
            try:
                if attempt > 0:
                    time.sleep(self.RETRY_DELAYS[attempt - 1] + random.uniform(0.2, 0.8))
                if method == "GET":
                    response = session.get(url, headers=merged_headers)
                elif method == "POST":
                    response = session.post(url, headers=merged_headers, json=json_data)
                else:
                    raise ValueError(f"unsupported method: {method}")
            except Exception as exc:
                if attempt < self.MAX_RETRIES - 1:
                    continue
                return {"success": False, "error": str(exc), "status_code": 0}

            if 200 <= response.status_code < 300:
                try:
                    payload = response.json()
                except Exception:
                    payload = {}
                return {"success": True, "data": payload, "status_code": response.status_code}

            if attempt < self.MAX_RETRIES - 1 and response.status_code >= 500:
                continue
            return {"success": False, "error": response.text, "status_code": response.status_code}

        return {"success": False, "error": "unknown error", "status_code": 0}

    def get_members(self, *, access_token: str, account_id: str, identifier: str) -> dict[str, Any]:
        all_members: list[dict[str, Any]] = []
        offset = 0
        limit = 50
        while True:
            result = self._make_request(
                "GET",
                f"{self.BASE_URL}/accounts/{account_id}/users?limit={limit}&offset={offset}",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "chatgpt-account-id": account_id,
                },
                identifier=identifier,
            )
            if not result.get("success"):
                return {"success": False, "members": [], "total": 0, "error": result.get("error")}
            data = result.get("data") or {}
            items = list(data.get("items") or [])
            total = int(data.get("total") or len(items))
            all_members.extend(items)
            if len(all_members) >= total or not items:
                break
            offset += limit
        return {"success": True, "members": all_members, "total": len(all_members), "error": None}

    def send_invite(self, *, access_token: str, account_id: str, email: str, identifier: str) -> dict[str, Any]:
        result = self._make_request(
            "POST",
            f"{self.BASE_URL}/accounts/{account_id}/invites",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {access_token}",
                "chatgpt-account-id": account_id,
            },
            json_data={
                "email_addresses": [email],
                "role": "standard-user",
                "resend_emails": True,
            },
            identifier=identifier,
        )
        if not result.get("success"):
            return {"success": False, "invite_id": "", "error": result.get("error")}
        data = result.get("data") or {}
        invites = data.get("account_invites") or []
        invite_id = str((invites[0] or {}).get("id") or "").strip() if invites else ""
        if invite_id:
            return {"success": True, "invite_id": invite_id, "error": "", "data": data}
        errored_emails = list(data.get("errored_emails") or [])
        summary = {
            "keys": sorted(list(data.keys()))[:8] if isinstance(data, dict) else [],
            "errored_emails": errored_emails[:3],
            "account_invites_count": len(invites),
            "status_code": int(result.get("status_code") or 0),
        }
        return {
            "success": False,
            "invite_id": "",
            "error": f"invite response missing id: {summary}",
            "data": data,
        }

    def get_invites(self, *, access_token: str, account_id: str, identifier: str) -> dict[str, Any]:
        result = self._make_request(
            "GET",
            f"{self.BASE_URL}/accounts/{account_id}/invites",
            headers={
                "Authorization": f"Bearer {access_token}",
                "chatgpt-account-id": account_id,
            },
            identifier=identifier,
        )
        if not result.get("success"):
            return {"success": False, "items": [], "total": 0, "error": result.get("error")}
        data = result.get("data") or {}
        items = list(data.get("items") or [])
        return {"success": True, "items": items, "total": len(items), "error": "", "data": data}

    def accept_invite(self, *, access_token: str, account_id: str, identifier: str) -> dict[str, Any]:
        result = self._make_request(
            "POST",
            f"{self.BASE_URL}/accounts/{account_id}/invites/accept",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {access_token}",
                "chatgpt-account-id": account_id,
            },
            identifier=identifier,
        )
        if not result.get("success"):
            return {"success": False, "error": result.get("error")}
        data = result.get("data") or {}
        return {"success": bool(data.get("success") is True or data == {}), "error": "" if data.get("success", True) else str(data), "data": data}
