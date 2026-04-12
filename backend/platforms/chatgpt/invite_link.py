from __future__ import annotations

import re
from html import unescape
from urllib.parse import parse_qs, urlparse

import requests


def extract_invite_urls(content: str) -> list[str]:
    text = str(content or "")
    if not text:
        return []

    urls: list[str] = []
    seen: set[str] = set()

    href_matches = re.findall(r'href=["\\\']([^"\\\']+)["\\\']', text, flags=re.I)
    raw_matches = re.findall(r'https?://[^\s<>"\\\']+', text, flags=re.I)

    for candidate in [*href_matches, *raw_matches]:
        url = unescape(str(candidate or "").strip())
        if not url or url in seen:
            continue
        seen.add(url)
        urls.append(url)

    def _rank(url: str) -> tuple[int, int]:
        lowered = url.lower()
        if "chatgpt.com/auth/login" in lowered and ("accept_wid=" in lowered or "inv_ws_name=" in lowered):
            return (0, 0)
        if "sendgrid.net/ls/click" in lowered:
            return (1, 0)
        return (9, len(url))

    urls.sort(key=_rank)
    return urls


def resolve_invite_link(url: str, *, session: requests.Session | None = None, timeout: int = 30) -> str:
    candidate = str(url or "").strip()
    if not candidate:
        return ""
    lowered = candidate.lower()
    if "chatgpt.com/auth/login" in lowered and ("accept_wid=" in lowered or "inv_ws_name=" in lowered):
        return candidate
    if "sendgrid.net/ls/click" not in lowered:
        return candidate

    http = session or requests.Session()
    response = http.get(candidate, allow_redirects=True, timeout=timeout)
    return str(getattr(response, "url", "") or "").strip()


def extract_invite_workspace_ids(url: str) -> dict[str, str]:
    parsed = urlparse(str(url or "").strip())
    query = parse_qs(parsed.query)
    return {
        "workspace_id": str((query.get("wId") or [""])[0] or "").strip(),
        "accept_workspace_id": str((query.get("accept_wId") or [""])[0] or "").strip(),
        "invite_email": str((query.get("inv_email") or [""])[0] or "").strip(),
        "workspace_name": str((query.get("inv_ws_name") or [""])[0] or "").strip(),
    }
