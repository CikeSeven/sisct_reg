"""Playwright 版 Sentinel SDK token 获取辅助。"""

from __future__ import annotations

import json
import threading
from typing import Any, Callable, Optional

from core.browser_runtime import (
    ensure_browser_display_available,
    resolve_browser_headless,
)
from core.proxy_utils import build_playwright_proxy_config


_SENTINEL_BROWSER_SEMAPHORE = threading.BoundedSemaphore(2)


def _flow_page_url(flow: str) -> str:
    flow_name = str(flow or "").strip().lower()
    mapping = {
        "authorize_continue": "https://auth.openai.com/create-account",
        "username_password_create": "https://auth.openai.com/create-account/password",
        "password_verify": "https://auth.openai.com/log-in/password",
        "email_otp_validate": "https://auth.openai.com/email-verification",
        "oauth_create_account": "https://auth.openai.com/about-you",
    }
    return mapping.get(flow_name, "https://auth.openai.com/about-you")


def get_sentinel_token_via_browser(
    *,
    flow: str,
    proxy: Optional[str] = None,
    timeout_ms: int = 90000,
    page_url: Optional[str] = None,
    headless: bool = True,
    device_id: Optional[str] = None,
    session=None,
    log_fn: Optional[Callable[[str], None]] = None,
) -> Optional[str]:
    """通过浏览器直接调用 SentinelSDK.token(flow) 获取完整 token。"""
    logger = log_fn or (lambda _msg: None)

    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        logger(f"Sentinel Browser 不可用: {e}")
        return None

    target_url = str(page_url or _flow_page_url(flow)).strip() or _flow_page_url(flow)
    effective_headless, reason = resolve_browser_headless(headless)
    ensure_browser_display_available(effective_headless)
    logger(
        f"Sentinel Browser 模式: {'headless' if effective_headless else 'headed'} ({reason}), timeout={int(timeout_ms / 1000)}s"
    )

    launch_args: dict[str, Any] = {
        "headless": effective_headless,
        "args": [
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
        ],
    }
    proxy_config = build_playwright_proxy_config(proxy)
    if proxy_config:
        launch_args["proxy"] = proxy_config

    logger(f"Sentinel Browser 启动: flow={flow}, url={target_url}")

    if not _SENTINEL_BROWSER_SEMAPHORE.acquire(blocking=False):
        logger("Sentinel Browser 忙，直接回退 HTTP PoW")
        return None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(**launch_args)
            try:
                context = browser.new_context(
                    viewport={"width": 1440, "height": 900},
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/136.0.7103.92 Safari/537.36"
                    ),
                    ignore_https_errors=True,
                )
                seed_cookies = _iter_session_cookies(
                    session,
                    fallback_host=target_url.split('/')[2] if '://' in target_url else 'auth.openai.com',
                ) if session is not None else []
                if device_id:
                    seed_cookies.append(
                        {
                            "name": "oai-did",
                            "value": str(device_id),
                            "url": "https://auth.openai.com/",
                            "secure": True,
                            "sameSite": "Lax",
                        }
                    )
                if seed_cookies:
                    try:
                        context.add_cookies(seed_cookies)
                    except Exception as e:
                        logger(f"Sentinel Browser 注入 cookies 失败: {e}")

                page = context.new_page()
                page.goto(target_url, wait_until="domcontentloaded", timeout=timeout_ms)
                page.wait_for_function(
                    "() => typeof window.SentinelSDK !== 'undefined' && typeof window.SentinelSDK.token === 'function'",
                    timeout=min(timeout_ms, 60000),
                )

                result = page.evaluate(
                    """
                    async ({ flow }) => {
                        try {
                            const token = await window.SentinelSDK.token(flow);
                            return { success: true, token };
                        } catch (e) {
                            return {
                                success: false,
                                error: (e && (e.message || String(e))) || "unknown",
                            };
                        }
                    }
                    """,
                    {"flow": flow},
                )

                if not result or not result.get("success") or not result.get("token"):
                    logger(
                        "Sentinel Browser 未取到 token，回退 HTTP PoW: "
                        + str((result or {}).get("error") or "no result")
                    )
                    return None

                token = str(result["token"] or "").strip()
                if not token:
                    logger("Sentinel Browser 返回空 token")
                    return None

                try:
                    parsed = json.loads(token)
                    logger(
                        "Sentinel Browser 成功: "
                        f"p={'✓' if parsed.get('p') else '✗'} "
                        f"t={'✓' if parsed.get('t') else '✗'} "
                        f"c={'✓' if parsed.get('c') else '✗'}"
                    )
                except Exception:
                    logger(f"Sentinel Browser 成功: len={len(token)}")

                return token
            except Exception as e:
                logger(f"Sentinel Browser 超时，回退 HTTP PoW: {e}")
                return None
            finally:
                browser.close()
    finally:
        _SENTINEL_BROWSER_SEMAPHORE.release()

def _iter_session_cookies(session, *, fallback_host: str = "auth.openai.com") -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    jar = getattr(session, "cookies", None)
    if jar is None:
        return items
    try:
        iterable = list(jar)
    except Exception:
        iterable = []
    for cookie in iterable:
        name = str(getattr(cookie, "name", "") or "").strip()
        value = str(getattr(cookie, "value", "") or "")
        domain = str(getattr(cookie, "domain", "") or "").strip()
        path = str(getattr(cookie, "path", "") or "/") or "/"
        if not name:
            continue
        host = domain.lstrip(".") or str(fallback_host or "auth.openai.com").strip()
        if not host:
            continue
        item: dict[str, Any] = {
            "name": name,
            "value": value,
            "url": f"https://{host}{path}",
        }
        secure = getattr(cookie, "secure", None)
        if secure is not None:
            item["secure"] = bool(secure)
        items.append(item)
    return items


def sync_auth_session_via_browser(
    *,
    session,
    target_url: str,
    proxy: Optional[str] = None,
    timeout_ms: int = 45000,
    headless: bool = True,
    device_id: Optional[str] = None,
    log_fn: Optional[Callable[[str], None]] = None,
) -> bool:
    """用 Playwright 打开 auth 页面并回灌浏览器 cookies，尽量补齐 Cloudflare/JSD 挑战状态。"""
    logger = log_fn or (lambda _msg: None)
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        logger(f"Auth Browser 不可用: {e}")
        return False

    effective_headless, reason = resolve_browser_headless(headless)
    ensure_browser_display_available(effective_headless)
    launch_args: dict[str, Any] = {
        "headless": effective_headless,
        "args": ["--no-sandbox", "--disable-blink-features=AutomationControlled"],
    }
    proxy_config = build_playwright_proxy_config(proxy)
    if proxy_config:
        launch_args["proxy"] = proxy_config

    if not _SENTINEL_BROWSER_SEMAPHORE.acquire(blocking=False):
        logger("Auth Browser 忙，跳过 challenge 预热")
        return False

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(**launch_args)
            try:
                context = browser.new_context(
                    viewport={"width": 1440, "height": 900},
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/136.0.7103.92 Safari/537.36"
                    ),
                    ignore_https_errors=True,
                )
                seed_cookies = _iter_session_cookies(
                    session,
                    fallback_host=target_url.split('/')[2] if '://' in target_url else 'auth.openai.com',
                )
                if device_id:
                    seed_cookies.append(
                        {
                            "name": "oai-did",
                            "value": str(device_id or ""),
                            "url": "https://auth.openai.com/",
                            "secure": True,
                            "sameSite": "Lax",
                        }
                    )
                if seed_cookies:
                    try:
                        context.add_cookies(seed_cookies)
                    except Exception as e:
                        logger(f"Auth Browser 注入 cookies 失败: {e}")

                page = context.new_page()
                logger(
                    f"Auth Browser 预热: {'headless' if effective_headless else 'headed'} ({reason}), timeout={int(timeout_ms / 1000)}s -> {target_url}"
                )
                page.goto(str(target_url or "").strip(), wait_until="domcontentloaded", timeout=timeout_ms)
                page.wait_for_timeout(3500)
                browser_cookies = context.cookies()
                applied = 0
                for cookie in browser_cookies:
                    name = str(cookie.get("name") or "").strip()
                    value = str(cookie.get("value") or "")
                    domain = str(cookie.get("domain") or "").strip()
                    path = str(cookie.get("path") or "/")
                    if not name:
                        continue
                    try:
                        session.cookies.set(name, value, domain=domain or None, path=path or "/")
                        applied += 1
                    except Exception:
                        try:
                            session.cookies.set(name, value)
                            applied += 1
                        except Exception:
                            continue
                logger(f"Auth Browser 预热完成，已同步 {applied} 个 cookies")
                return applied > 0
            except Exception as e:
                logger(f"Auth Browser 预热失败: {e}")
                return False
            finally:
                browser.close()
    finally:
        _SENTINEL_BROWSER_SEMAPHORE.release()


def auth_browser_post_json(
    *,
    session,
    page_url: str,
    post_url: str,
    payload: dict[str, Any],
    headers: dict[str, str] | None = None,
    proxy: Optional[str] = None,
    timeout_ms: int = 20000,
    headless: bool = True,
    device_id: Optional[str] = None,
    log_fn: Optional[Callable[[str], None]] = None,
) -> dict[str, Any]:
    """在真实浏览器上下文内执行 auth.openai.com JSON POST，并回灌 cookies。"""
    logger = log_fn or (lambda _msg: None)
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        return {"success": False, "status_code": 0, "error": f"Playwright 不可用: {e}", "data": {}, "text": ""}

    effective_headless, reason = resolve_browser_headless(headless)
    ensure_browser_display_available(effective_headless)
    launch_args: dict[str, Any] = {
        "headless": effective_headless,
        "args": ["--no-sandbox", "--disable-blink-features=AutomationControlled"],
    }
    proxy_config = build_playwright_proxy_config(proxy)
    if proxy_config:
        launch_args["proxy"] = proxy_config

    if not _SENTINEL_BROWSER_SEMAPHORE.acquire(blocking=False):
        return {"success": False, "status_code": 0, "error": "Auth Browser 忙", "data": {}, "text": ""}

    forbidden = {
        "host",
        "cookie",
        "content-length",
        "origin",
        "referer",
        "connection",
    }

    def _filter_headers(source: dict[str, str] | None) -> dict[str, str]:
        result: dict[str, str] = {}
        for key, value in dict(source or {}).items():
            lowered = str(key or "").strip().lower()
            if not lowered or lowered in forbidden or lowered.startswith("sec-fetch-"):
                continue
            result[str(key)] = str(value)
        result.setdefault("Accept", "application/json")
        result.setdefault("Content-Type", "application/json")
        return result

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(**launch_args)
            try:
                context = browser.new_context(
                    viewport={"width": 1440, "height": 900},
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/136.0.7103.92 Safari/537.36"
                    ),
                    ignore_https_errors=True,
                )
                host = page_url.split('/')[2] if '://' in str(page_url or '') else 'auth.openai.com'
                seed_cookies = _iter_session_cookies(session, fallback_host=host)
                if device_id:
                    seed_cookies.append(
                        {
                            "name": "oai-did",
                            "value": str(device_id or ""),
                            "url": "https://auth.openai.com/",
                            "secure": True,
                            "sameSite": "Lax",
                        }
                    )
                if seed_cookies:
                    try:
                        context.add_cookies(seed_cookies)
                    except Exception as e:
                        logger(f"Auth Browser POST 注入 cookies 失败: {e}")

                page = context.new_page()
                logger(
                    f"Auth Browser POST: {'headless' if effective_headless else 'headed'} ({reason}) -> {post_url}"
                )
                page.goto(str(page_url or "").strip(), wait_until="domcontentloaded", timeout=timeout_ms)
                page.wait_for_timeout(1200)
                result = page.evaluate(
                    """
                    async ({ postUrl, payload, headers, referrer }) => {
                        try {
                            const response = await fetch(postUrl, {
                                method: 'POST',
                                credentials: 'include',
                                headers,
                                body: JSON.stringify(payload),
                                referrer,
                                referrerPolicy: 'strict-origin-when-cross-origin',
                            });
                            const text = await response.text();
                            let data = {};
                            try { data = text ? JSON.parse(text) : {}; } catch (e) { data = {}; }
                            return {
                                ok: response.ok,
                                status: response.status,
                                url: response.url,
                                text,
                                data,
                            };
                        } catch (e) {
                            return {
                                ok: false,
                                status: 0,
                                url: postUrl,
                                text: '',
                                data: {},
                                error: (e && (e.message || String(e))) || 'unknown',
                            };
                        }
                    }
                    """,
                    {
                        "postUrl": str(post_url or ""),
                        "payload": dict(payload or {}),
                        "headers": _filter_headers(headers),
                        "referrer": str(page_url or ""),
                    },
                )
                browser_cookies = context.cookies()
                applied = 0
                for cookie in browser_cookies:
                    name = str(cookie.get("name") or "").strip()
                    value = str(cookie.get("value") or "")
                    domain = str(cookie.get("domain") or "").strip()
                    path = str(cookie.get("path") or "/")
                    if not name:
                        continue
                    try:
                        session.cookies.set(name, value, domain=domain or None, path=path or "/")
                        applied += 1
                    except Exception:
                        try:
                            session.cookies.set(name, value)
                            applied += 1
                        except Exception:
                            continue
                logger(f"Auth Browser POST 完成: status={int((result or {}).get('status') or 0)} cookies={applied}")
                return {
                    "success": bool((result or {}).get("ok")),
                    "status_code": int((result or {}).get("status") or 0),
                    "data": (result or {}).get("data") or {},
                    "text": str((result or {}).get("text") or ""),
                    "url": str((result or {}).get("url") or post_url or ""),
                    "error": str((result or {}).get("error") or ""),
                }
            finally:
                browser.close()
    except Exception as e:
        return {"success": False, "status_code": 0, "error": str(e), "data": {}, "text": ""}
    finally:
        _SENTINEL_BROWSER_SEMAPHORE.release()