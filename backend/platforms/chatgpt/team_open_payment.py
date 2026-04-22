from __future__ import annotations

import time
from urllib.parse import urlparse
from dataclasses import dataclass
from typing import Any, Callable

import requests
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from core.browser_runtime import ensure_browser_display_available, resolve_browser_headless
from core.proxy_utils import build_playwright_proxy_config, build_requests_proxy_config, instrument_session_proxy_requests


def _iter_session_cookies(session, *, target_host: str = "") -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    jar = getattr(session, "cookies", None)
    if jar is None:
        return items
    try:
        iterable = list(jar)
    except Exception:
        iterable = []
    normalized_target = str(target_host or "").strip().lower()
    for cookie in iterable:
        name = str(getattr(cookie, "name", "") or "").strip()
        value = str(getattr(cookie, "value", "") or "")
        domain = str(getattr(cookie, "domain", "") or "").strip()
        path = str(getattr(cookie, "path", "") or "/") or "/"
        if not name:
            continue
        normalized_domain = domain.lstrip(".").lower()
        if normalized_target and normalized_domain and normalized_target not in normalized_domain and normalized_domain not in normalized_target:
            continue
        host = normalized_domain or normalized_target
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
        expires = getattr(cookie, "expires", None)
        if expires not in (None, ""):
            try:
                item["expires"] = float(expires)
            except Exception:
                pass
        items.append(item)
    return items


@dataclass
class TeamOpenCardProfile:
    card_number: str
    exp_month: str
    exp_year: str
    cvc: str
    holder_name: str = ""
    billing_email: str = ""
    country: str = ""
    state: str = ""
    city: str = ""
    line1: str = ""
    postal_code: str = ""
    label: str = ""
    id: int = 0


class ChatGPTPaymentClient:
    BASE_URL = "https://chatgpt.com"

    def __init__(self, auth_client, session_summary: dict[str, Any], *, log_fn: Callable[[str], None] | None = None):
        self.auth_client = auth_client
        self.session_summary = dict(session_summary or {})
        self.log_fn = log_fn or (lambda _message: None)

    def _headers(self, *, referer: str = "https://chatgpt.com/") -> dict[str, str]:
        headers = {
            "Accept": "*/*",
            "Content-Type": "application/json",
            "oai-language": "zh-CN",
            "Origin": self.BASE_URL,
            "Referer": referer,
        }
        access_token = str(self.session_summary.get("access_token") or "").strip()
        account_id = str(
            self.session_summary.get("selected_workspace_id")
            or self.session_summary.get("account_id")
            or ""
        ).strip()
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        if account_id:
            headers["chatgpt-account-id"] = account_id
        return headers

    def _post_json(self, path: str, payload: dict[str, Any], *, referer: str) -> dict[str, Any]:
        url = f"{self.BASE_URL}{path}"
        last_status = 0
        last_text = ""
        for attempt in range(1, 4):
            response = self.auth_client.session.post(
                url,
                headers=self._headers(referer=referer),
                json=payload,
                timeout=30,
            )
            last_status = int(getattr(response, "status_code", 0) or 0)
            last_text = str(getattr(response, "text", "") or "")[:300]
            if 200 <= last_status < 300:
                try:
                    return response.json() or {}
                except Exception as exc:
                    raise RuntimeError(f"支付接口返回非 JSON: {path}: {exc}") from exc
            if last_status >= 500 and attempt < 3:
                self.log_fn(f"[Payments] {path} 返回 HTTP {last_status}，第 {attempt}/3 次失败，准备重试")
                time.sleep(1.5 * attempt)
                continue
            raise RuntimeError(f"支付接口请求失败: {path} HTTP {last_status}: {last_text}")
        raise RuntimeError(f"支付接口请求失败: {path} HTTP {last_status}: {last_text}")

    def create_pro_checkout(
        self,
        *,
        country: str = "US",
        currency: str = "USD",
        plan_name: str = "chatgptpro",
        price_interval: str = "month",
        seat_quantity: int = 1,
    ) -> dict[str, Any]:
        initial_payload = self._post_json(
            "/backend-api/payments/checkout",
            {
                "entry_point": "all_plans_pricing_modal",
                "plan_name": "chatgptprolite",
                "billing_details": {
                    "country": str(country or "US").strip().upper(),
                    "currency": str(currency or "USD").strip().upper(),
                },
                "checkout_ui_mode": "custom",
            },
            referer="https://chatgpt.com/",
        )
        checkout_session_id = str(initial_payload.get("checkout_session_id") or "").strip()
        processor_entity = str(initial_payload.get("processor_entity") or "openai_llc").strip() or "openai_llc"
        if not checkout_session_id:
            raise RuntimeError(f"创建 Pro checkout 失败: {initial_payload}")
        self.log_fn(f"[Payments] 已创建 Pro checkout: {checkout_session_id}")
        update_payload = self._post_json(
            "/backend-api/payments/checkout/update",
            {
                "checkout_session_id": checkout_session_id,
                "processor_entity": processor_entity,
                "plan_name": str(plan_name or "chatgptpro").strip() or "chatgptpro",
                "price_interval": str(price_interval or "month").strip() or "month",
                "seat_quantity": max(1, int(seat_quantity or 1)),
            },
            referer=f"https://chatgpt.com/checkout/{processor_entity}/{checkout_session_id}",
        )
        if not bool(update_payload.get("success") is True or update_payload == {"success": True}):
            raise RuntimeError(f"更新 Pro checkout 失败: {update_payload}")
        return {
            "checkout_session_id": checkout_session_id,
            "processor_entity": processor_entity,
            "checkout_url": f"https://chatgpt.com/checkout/{processor_entity}/{checkout_session_id}",
        }

    def approve_checkout(self, *, checkout_session_id: str, processor_entity: str) -> dict[str, Any]:
        payload = self._post_json(
            "/backend-api/payments/checkout/approve",
            {
                "checkout_session_id": str(checkout_session_id or "").strip(),
                "processor_entity": str(processor_entity or "openai_llc").strip() or "openai_llc",
            },
            referer=f"https://chatgpt.com/checkout/{processor_entity}/{checkout_session_id}",
        )
        return payload if isinstance(payload, dict) else {"result": payload}


class TeamPaymentLinkClient:
    def __init__(self, *, base_url: str, proxy_url: str | None = None, log_fn: Callable[[str], None] | None = None):
        self.base_url = str(base_url or "").strip().rstrip("/")
        self.proxy_url = str(proxy_url or "").strip() or None
        self.log_fn = log_fn or (lambda _message: None)
        if not self.base_url:
            raise RuntimeError("未配置 Team 支付链接服务地址")
        self.session = requests.Session()
        self.session.proxies = build_requests_proxy_config(self.proxy_url) or {}
        instrument_session_proxy_requests(self.session, proxy_url=self.proxy_url)
        self.session.headers.update({
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
        })

    def generate_payment_link(
        self,
        *,
        access_token: str,
        plan_name: str,
        country: str,
        currency: str,
        seat_quantity: int,
        price_interval: str,
        promo_campaign_id: str,
        check_card_proxy: bool = False,
        is_short_link: bool = False,
        is_coupon_from_query_param: bool = True,
    ) -> dict[str, Any]:
        if not str(access_token or "").strip():
            raise RuntimeError("生成 Team 支付链接失败：access_token 为空")
        url = f"{self.base_url}/api/public/generate-payment-link"
        payload_data = {
            "access_token": str(access_token or "").strip(),
            "plan_name": str(plan_name or "chatgptteamplan").strip() or "chatgptteamplan",
            "country": str(country or "SG").strip().upper() or "SG",
            "currency": str(currency or "SGD").strip().upper() or "SGD",
            "promo_campaign_id": str(promo_campaign_id or "").strip(),
            "is_coupon_from_query_param": bool(is_coupon_from_query_param),
            "seat_quantity": max(1, int(seat_quantity or 1)),
            "price_interval": str(price_interval or "month").strip() or "month",
            "check_card_proxy": bool(check_card_proxy),
            "is_short_link": bool(is_short_link),
        }

        retry_statuses = {408, 409, 425, 429, 500, 502, 503, 504, 520, 522, 524}
        max_attempts = 4
        response: requests.Response | None = None
        last_exc: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                response = self.session.post(url, json=payload_data, timeout=90)
                status_code = int(response.status_code or 0)
                if status_code in retry_statuses and attempt < max_attempts:
                    preview = str(response.text or "").strip().replace("\n", " ")[:180]
                    delay_seconds = min(20, 2 * attempt + 1)
                    self.log_fn(
                        f"[TeamLink] 支付链接服务 HTTP {status_code}，{delay_seconds}s 后重试 {attempt + 1}/{max_attempts}: {preview or '<empty>'}"
                    )
                    time.sleep(delay_seconds)
                    continue
                break
            except (
                requests.exceptions.ReadTimeout,
                requests.exceptions.Timeout,
                requests.exceptions.SSLError,
                requests.exceptions.ProxyError,
                requests.exceptions.ConnectionError,
            ) as exc:
                last_exc = exc
                if self.proxy_url and isinstance(exc, (requests.exceptions.SSLError, requests.exceptions.ProxyError, requests.exceptions.ConnectionError)):
                    self.log_fn(f"[TeamLink] 代理请求支付链接服务失败，改用直连重试: {exc}")
                    direct_session = requests.Session()
                    direct_session.headers.update(dict(self.session.headers))
                    try:
                        response = direct_session.post(url, json=payload_data, timeout=90)
                        if int(response.status_code or 0) not in retry_statuses:
                            break
                    except Exception as direct_exc:
                        last_exc = direct_exc
                if attempt >= max_attempts:
                    break
                delay_seconds = min(20, 2 * attempt + 1)
                self.log_fn(f"[TeamLink] 支付链接服务请求异常，{delay_seconds}s 后重试 {attempt + 1}/{max_attempts}: {last_exc}")
                time.sleep(delay_seconds)

        if response is None:
            raise RuntimeError(f"生成 Team 支付链接失败：多次请求无响应: {last_exc}")
        try:
            response.raise_for_status()
        except requests.exceptions.HTTPError as exc:
            preview = str(response.text or "").strip().replace("\n", " ")[:300]
            raise RuntimeError(f"生成 Team 支付链接失败: HTTP {response.status_code} {preview or '<empty>'}") from exc
        try:
            payload = response.json() or {}
        except Exception as exc:
            preview = str(response.text or "").strip().replace("\n", " ")[:300]
            raise RuntimeError(f"生成 Team 支付链接失败：响应不是 JSON: {preview or '<empty>'}") from exc
        if not isinstance(payload, dict) or not payload.get("success"):
            raise RuntimeError(f"生成 Team 支付链接失败: {payload}")
        payment_url = str(payload.get("url") or "").strip()
        checkout_session_id = str(payload.get("checkout_session_id") or "").strip()
        if not payment_url:
            raise RuntimeError(f"生成 Team 支付链接失败: {payload}")
        self.log_fn(f"[TeamLink] 已生成支付链接: {checkout_session_id or payment_url[:80]}")
        return {
            "payment_url": payment_url,
            "checkout_session_id": checkout_session_id,
            "raw": payload,
        }


def _fill_input(locator, value: str) -> bool:
    try:
        if locator.count() <= 0:
            return False
        target = locator.first
        if not target.is_visible(timeout=500):
            return False
        target.click(timeout=500)
        try:
            target.fill("")
        except Exception:
            pass
        target.fill(str(value or ""), timeout=1500)
        return True
    except Exception:
        return False


def _locator_is_visible(locator, *, timeout: int = 300) -> bool:
    try:
        return locator.count() > 0 and locator.first.is_visible(timeout=timeout)
    except Exception:
        return False


def _select_or_fill(locator, value: str) -> bool:
    clean_value = str(value or "").strip()
    if not clean_value:
        return True
    try:
        if locator.count() <= 0:
            return False
        target = locator.first
        if not target.is_visible(timeout=300):
            return False
        try:
            target.select_option(value=clean_value, timeout=1000)
            return True
        except Exception:
            pass
        try:
            target.select_option(label=clean_value, timeout=1000)
            return True
        except Exception:
            pass
    except Exception:
        return False
    return _fill_input(locator, clean_value)


def _fill_by_labels(scope, labels: list[str], value: str, *, allow_select: bool = False) -> bool:
    clean_value = str(value or "").strip()
    if not clean_value:
        return True
    for label in labels:
        try:
            locator = scope.get_by_label(label, exact=False)
            if allow_select:
                if _select_or_fill(locator, clean_value):
                    return True
            elif _fill_input(locator, clean_value):
                return True
        except Exception:
            continue
    return False


def _fill_first_match(scopes, selectors: list[str], labels: list[str], value: str) -> bool:
    clean_value = str(value or "").strip()
    if not clean_value:
        return True
    for scope in scopes:
        if labels and _fill_by_labels(scope, labels, clean_value):
            return True
        for selector in selectors:
            try:
                if _fill_input(scope.locator(selector), clean_value):
                    return True
            except Exception:
                continue
    return False


def _dedupe_candidates(values: list[str]) -> list[str]:
    result: list[str] = []
    for item in values:
        clean = str(item or "").strip()
        if clean and clean not in result:
            result.append(clean)
    return result


def _country_candidates(country: str) -> list[str]:
    clean_value = str(country or "").strip()
    if not clean_value:
        return []
    lowered = clean_value.lower()
    aliases = {
        "us": ["US", "USA", "United States", "United States of America", "美国"],
        "usa": ["US", "USA", "United States", "United States of America", "美国"],
        "united states": ["US", "USA", "United States", "United States of America", "美国"],
        "united states of america": ["US", "USA", "United States", "United States of America", "美国"],
        "美国": ["US", "USA", "United States", "United States of America", "美国"],
        "sg": ["SG", "Singapore", "新加坡"],
        "singapore": ["SG", "Singapore", "新加坡"],
        "新加坡": ["SG", "Singapore", "新加坡"],
    }
    return _dedupe_candidates([clean_value, clean_value.upper(), *aliases.get(lowered, [])])


def _state_candidates(state: str) -> list[str]:
    clean_value = str(state or "").strip()
    if not clean_value:
        return []
    aliases = {
        "CA": ["California", "加利福尼亚州"],
        "CO": ["Colorado", "科罗拉多州"],
    }
    return _dedupe_candidates([clean_value, clean_value.upper(), *aliases.get(clean_value.upper(), [])])


def _select_or_fill_first_match(scopes, selectors: list[str], labels: list[str], values: list[str]) -> bool:
    candidates = _dedupe_candidates(values)
    if not candidates:
        return True
    for scope in scopes:
        for selector in selectors:
            try:
                locator = scope.locator(selector)
                if not _locator_is_visible(locator):
                    continue
                for candidate in candidates:
                    if _select_or_fill(locator, candidate):
                        return True
            except Exception:
                continue
        for candidate in candidates:
            if labels and _fill_by_labels(scope, labels, candidate, allow_select=True):
                return True
    return False
def _billing_line1_selectors() -> list[str]:
    return [
        "input[autocomplete='address-line1']",
        "input[autocomplete='street-address']",
        "input[autocomplete*='address-line1']",
        "input[autocomplete*='street-address']",
        "input[name*='line1']",
        "input[name*='addressLine1']",
        "input[name*='address']",
        "input[id*='line1']",
        "input[id*='addressLine1']",
        "input[id*='address']",
        "input[placeholder*='Address line 1']",
        "input[placeholder*='Street address']",
        "input[placeholder*='Address']",
        "input[placeholder*='地址第 1 行']",
        "input[placeholder*='地址第一行']",
        "input[placeholder*='地址']",
        "input[aria-label*='Address line 1']",
        "input[aria-label*='Street address']",
        "input[aria-label*='Address']",
        "input[aria-label*='地址第 1 行']",
        "input[aria-label*='地址第一行']",
        "input[aria-label*='地址']",
        "input[aria-autocomplete='list']",
        "input[role='combobox']",
        "[data-testid*='address'] input",
    ]


def _billing_line1_labels() -> list[str]:
    return ["Address line 1", "Street address", "Address", "地址第 1 行", "地址第一行", "地址", "账单地址"]


def _accept_address_autocomplete_suggestion(page, scopes) -> bool:
    option_selectors = [
        "[role='listbox'] [role='option']",
        "li[role='option']",
        "button[role='option']",
        "[data-testid*='address'] [role='option']",
        "[data-testid*='autocomplete'] [role='option']",
    ]
    for scope in scopes:
        for selector in option_selectors:
            try:
                locator = scope.locator(selector)
                if _locator_is_visible(locator):
                    locator.first.click(timeout=1200)
                    return True
            except Exception:
                continue
    try:
        page.keyboard.press("ArrowDown")
        page.wait_for_timeout(200)
        page.keyboard.press("Enter")
        return True
    except Exception:
        return False


def _fill_line1_with_autocomplete(page, scopes, value: str) -> bool:
    clean_value = str(value or "").strip()
    if not clean_value:
        return True
    if not _fill_first_match(scopes, _billing_line1_selectors(), _billing_line1_labels(), clean_value):
        return False
    page.wait_for_timeout(350)
    _accept_address_autocomplete_suggestion(page, scopes)
    return True




def _select_country(page, country: str) -> None:
    scopes = [page] + [frame for frame in page.frames]
    selectors = [
        "select[name*='country']",
        "select[autocomplete='country']",
        "select[data-testid*='country']",
        "input[autocomplete='country']",
        "input[name*='country']",
        "input[placeholder*='Country']",
        "input[placeholder*='国家']",
        "input[aria-label*='Country']",
        "input[aria-label*='国家']",
    ]
    _select_or_fill_first_match(
        scopes,
        selectors,
        ["Country", "Country or region", "Country/Region", "国家或地区", "国家", "地区"],
        _country_candidates(country),
    )


def _fill_main_form(page, card: TeamOpenCardProfile) -> None:
    _select_country(page, card.country)
    scopes = [page] + [frame for frame in page.frames]
    _fill_first_match(
        scopes,
        [
            "input[type='email']",
            "input[name='email']",
            "input[autocomplete='email']",
            "input[placeholder*='Email']",
            "input[placeholder*='邮箱']",
            "input[aria-label*='Email']",
            "input[aria-label*='邮箱']",
        ],
        ["Email", "电子邮件", "邮箱"],
        card.billing_email,
    )
    _fill_first_match(
        scopes,
        [
            "input[name='name']",
            "input[name*='name']",
            "input[autocomplete='name']",
            "input[autocomplete='cc-name']",
            "input[placeholder*='Full name']",
            "input[placeholder*='Name']",
            "input[placeholder*='全名']",
            "input[aria-label*='Full name']",
            "input[aria-label*='Name']",
            "input[aria-label*='全名']",
        ],
        ["Full name", "Name", "Cardholder name", "持卡人姓名", "全名", "姓名"],
        card.holder_name,
    )
    _fill_line1_with_autocomplete(page, scopes, card.line1)
    _fill_first_match(
        scopes,
        [
            "input[autocomplete='address-level2']",
            "input[name*='city']",
            "input[placeholder*='City']",
            "input[placeholder*='城市']",
            "input[aria-label*='City']",
            "input[aria-label*='城市']",
        ],
        ["City", "Town / City", "城市"],
        card.city,
    )
    _select_or_fill_first_match(
        scopes,
        [
            "select[autocomplete='address-level1']",
            "select[name*='state']",
            "select[name*='province']",
            "input[autocomplete='address-level1']",
            "input[name*='state']",
            "input[name*='province']",
            "input[placeholder*='State']",
            "input[placeholder*='Province']",
            "input[placeholder*='州']",
            "input[placeholder*='省']",
            "input[aria-label*='State']",
            "input[aria-label*='Province']",
            "input[aria-label*='州']",
            "input[aria-label*='省']",
        ],
        ["State", "Province", "州", "省/自治区/直辖市", "省"],
        _state_candidates(card.state),
    )
    _fill_first_match(
        scopes,
        [
            "input[autocomplete='postal-code']",
            "input[name*='postal']",
            "input[name*='zip']",
            "input[placeholder*='Postal']",
            "input[placeholder*='ZIP']",
            "input[placeholder*='邮政编码']",
            "input[placeholder*='邮编']",
            "input[aria-label*='Postal']",
            "input[aria-label*='ZIP']",
            "input[aria-label*='邮政编码']",
            "input[aria-label*='邮编']",
        ],
        ["Postal code", "ZIP", "ZIP code", "邮政编码", "邮编"],
        card.postal_code,
    )


def _visible_billing_form_groups(page) -> list[str]:
    scopes = [page] + [frame for frame in page.frames]
    groups = [
        (
            "name",
            [
                "input[name='name']",
                "input[name*='name']",
                "input[autocomplete='name']",
                "input[autocomplete='cc-name']",
                "input[placeholder*='Full name']",
                "input[placeholder*='Name']",
                "input[placeholder*='全名']",
                "input[aria-label*='Full name']",
                "input[aria-label*='Name']",
                "input[aria-label*='全名']",
            ],
        ),
        (
            "line1",
            _billing_line1_selectors(),
        ),
        (
            "city",
            [
                "input[autocomplete='address-level2']",
                "input[name*='city']",
                "input[placeholder*='City']",
                "input[placeholder*='城市']",
                "input[aria-label*='City']",
                "input[aria-label*='城市']",
            ],
        ),
        (
            "state",
            [
                "select[autocomplete='address-level1']",
                "select[name*='state']",
                "select[name*='province']",
                "input[autocomplete='address-level1']",
                "input[name*='state']",
                "input[name*='province']",
                "input[placeholder*='State']",
                "input[placeholder*='Province']",
                "input[placeholder*='州']",
                "input[placeholder*='省']",
                "input[aria-label*='State']",
                "input[aria-label*='Province']",
                "input[aria-label*='州']",
                "input[aria-label*='省']",
            ],
        ),
        (
            "postal",
            [
                "input[autocomplete='postal-code']",
                "input[name*='postal']",
                "input[name*='zip']",
                "input[placeholder*='Postal']",
                "input[placeholder*='ZIP']",
                "input[placeholder*='邮政编码']",
                "input[placeholder*='邮编']",
                "input[aria-label*='Postal']",
                "input[aria-label*='ZIP']",
                "input[aria-label*='邮政编码']",
                "input[aria-label*='邮编']",
            ],
        ),
    ]
    visible: list[str] = []
    for field_name, selectors in groups:
        matched = False
        for scope in scopes:
            for selector in selectors:
                try:
                    if _locator_is_visible(scope.locator(selector)):
                        visible.append(field_name)
                        matched = True
                        break
                except Exception:
                    continue
            if matched:
                break
    return visible


def _wait_and_fill_main_form(page, card: TeamOpenCardProfile, *, wait_ms: int = 15000, log_fn: Callable[[str], None] | None = None) -> None:
    logger = log_fn or (lambda _message: None)
    start_time = time.time()
    deadline = start_time + max(3.0, wait_ms / 1000.0)
    last_progress_bucket = -1
    filled_once = False

    while time.time() < deadline:
        visible_groups = _visible_billing_form_groups(page)
        if visible_groups:
            _fill_main_form(page, card)
            filled_once = True
        if len(visible_groups) >= 3:
            page.wait_for_timeout(350)
            _fill_main_form(page, card)
            logger(f"[Stripe] 账单地址字段已就绪，完成补填: visible={','.join(visible_groups)}")
            return
        elapsed_seconds = int(time.time() - start_time)
        progress_bucket = elapsed_seconds // 5
        if elapsed_seconds >= 5 and progress_bucket > last_progress_bucket:
            last_progress_bucket = progress_bucket
            logger(
                f"[Stripe] 等待账单地址区域渲染: elapsed={elapsed_seconds}s visible={','.join(visible_groups) or 'none'}"
            )
        page.wait_for_timeout(500)

    if filled_once:
        _fill_main_form(page, card)
        logger("[Stripe] 账单地址区域未完整出现，但已按当前布局重复填写")
        return

    logger("[Stripe] 未检测到可填写的账单地址输入区域，继续后续流程")

def _fill_card_fields(page, card: TeamOpenCardProfile, *, wait_ms: int = 20000, log_fn: Callable[[str], None] | None = None) -> None:
    logger = log_fn or (lambda _message: None)
    expiry_value = f"{str(card.exp_month or '').zfill(2)} / {str(card.exp_year or '')[-2:]}"
    card_selectors = [
        "input[name='cardnumber']",
        "input[name*='cardnumber']",
        "input[name='number']",
        "input[autocomplete='cc-number']",
        "input[placeholder*='Card number']",
        "input[placeholder*='1234']",
        "input[aria-label*='Card number']",
        "[data-elements-stable-field-name='cardNumber'] input",
        "input[data-elements-stable-field-name='cardNumber']",
    ]
    exp_selectors = [
        "input[name='exp-date']",
        "input[name='expDate']",
        "input[name*='exp']",
        "input[autocomplete='cc-exp']",
        "input[placeholder*='MM / YY']",
        "input[placeholder*='MM']",
        "input[aria-label*='Expiration']",
        "[data-elements-stable-field-name='cardExpiry'] input",
        "input[data-elements-stable-field-name='cardExpiry']",
    ]
    cvc_selectors = [
        "input[name='cvc']",
        "input[name*='cvc']",
        "input[name='security-code']",
        "input[name*='security']",
        "input[autocomplete='cc-csc']",
        "input[placeholder*='CVC']",
        "input[aria-label*='Security code']",
        "input[aria-label*='CVC']",
        "[data-elements-stable-field-name='cardCvc'] input",
        "input[data-elements-stable-field-name='cardCvc']",
    ]

    filled_number = False
    filled_expiry = False
    filled_cvc = False
    seen_frame_urls: list[str] = []
    start_time = time.time()
    deadline = start_time + max(5.0, wait_ms / 1000.0)
    last_progress_bucket = -1

    while time.time() < deadline:
        frames = [page.main_frame] + [frame for frame in page.frames if frame != page.main_frame]
        preferred_frames = []
        fallback_frames = []
        for frame in frames:
            frame_url = str(getattr(frame, 'url', '') or '').strip()
            frame_name = str(getattr(frame, 'name', '') or '').strip().lower()
            if frame_url and frame_url not in seen_frame_urls:
                seen_frame_urls.append(frame_url)
            lowered_url = frame_url.lower()
            if frame is page.main_frame:
                fallback_frames.append(frame)
                continue
            if any(token in lowered_url for token in ['js.stripe.com', 'stripecdn.com', 'stripe.com']) or '__privatestripeframe' in frame_name:
                preferred_frames.append(frame)
            else:
                fallback_frames.append(frame)
        scan_frames = preferred_frames + fallback_frames

        elapsed_seconds = int(time.time() - start_time)
        progress_bucket = elapsed_seconds // 10
        if elapsed_seconds >= 10 and progress_bucket > last_progress_bucket:
            last_progress_bucket = progress_bucket
            preview_urls = ', '.join(seen_frame_urls[:6])
            logger(
                f"[Stripe] Payment Element 仍在加载: elapsed={elapsed_seconds}s stripe_frames={len(preferred_frames)} "
                f"all_frames={len(frames)} number={filled_number} exp={filled_expiry} cvc={filled_cvc} "
                f"seen={preview_urls[:220]}"
            )

        for frame in scan_frames:
            if not filled_number:
                for selector in card_selectors:
                    if _fill_input(frame.locator(selector), card.card_number):
                        filled_number = True
                        break
            if not filled_expiry:
                for selector in exp_selectors:
                    if _fill_input(frame.locator(selector), expiry_value):
                        filled_expiry = True
                        break
            if not filled_cvc:
                for selector in cvc_selectors:
                    if _fill_input(frame.locator(selector), card.cvc):
                        filled_cvc = True
                        break
            if filled_number and filled_expiry and filled_cvc:
                logger(f"[Stripe] 卡片输入框已就绪: stripe_frames={len(preferred_frames)} all_frames={len(frames)}")
                return

        page.wait_for_timeout(500)

    preview_urls = ', '.join(seen_frame_urls[:6])
    raise RuntimeError(
        "未找到完整的 Stripe 卡片输入框"
        f" (number={filled_number}, exp={filled_expiry}, cvc={filled_cvc}, frames={len(page.frames) + 1}, waited={int(time.time() - start_time)}s, seen={preview_urls[:300]})"
    )


def _click_submit_button(page) -> None:
    keywords = [
        "Subscribe",
        "Pay",
        "Start trial",
        "Complete order",
        "Submit",
        "订阅",
        "支付",
        "提交",
        "开始试用",
    ]
    for keyword in keywords:
        try:
            locator = page.get_by_role("button", name=keyword)
            if locator.count() > 0 and locator.first.is_visible(timeout=800):
                locator.first.click(timeout=1500)
                return
        except Exception:
            continue
    buttons = page.locator("button")
    for index in range(min(buttons.count(), 12)):
        try:
            target = buttons.nth(index)
            text = (target.inner_text(timeout=300) or "").strip().lower()
            if any(token in text for token in ["subscribe", "pay", "trial", "submit", "订阅", "支付", "提交"]):
                target.click(timeout=1500)
                return
        except Exception:
            continue
    raise RuntimeError("未找到支付提交按钮")


def submit_stripe_checkout(
    url: str,
    *,
    card: TeamOpenCardProfile,
    browser_mode: str = "headless",
    timeout_ms: int = 120000,
    expect_decline: bool = False,
    proxy_url: str | None = None,
    session = None,
    chatgpt_session_token: str | None = None,
    chatgpt_account_id: str | None = None,
    log_fn: Callable[[str], None] | None = None,
    user_agent: str | None = None,
 ) -> dict[str, Any]:
    logger = log_fn or (lambda _message: None)
    target_url = str(url or "").strip()
    if not target_url:
        raise RuntimeError("支付页面 URL 为空")

    requested_headless = str(browser_mode or "headless").strip().lower() != "headed"
    if expect_decline and requested_headless:
        requested_headless = False
        logger("[Stripe] Pro 预检强制切换为 headed，降低 checkout 风控")
    effective_headless, reason = resolve_browser_headless(requested_headless)
    ensure_browser_display_available(effective_headless)
    logger(f"[Stripe] 启动浏览器: {'headless' if effective_headless else 'headed'} ({reason})")

    network_events: list[tuple[str, str]] = []
    success_markers = ["approved", "payment successful", "thanks", "complete", "success"]
    decline_markers = [
        "declined",
        "card was declined",
        "payment failed",
        "too many",
        "try again",
        "unable",
        "problem",
        "rejected",
        "拒",
        "失败",
        "频繁",
        "无法",
        "错误",
    ]

    launch_args: dict[str, Any] = {
        "headless": effective_headless,
        "args": [
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
        ],
    }
    proxy_config = build_playwright_proxy_config(proxy_url)
    if proxy_config:
        launch_args["proxy"] = proxy_config

    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_args)
        try:
            session_headers = getattr(session, "headers", {}) if session is not None else {}
            context = browser.new_context(
                viewport={"width": 1440, "height": 900},
                user_agent=str(user_agent or getattr(session_headers, "get", lambda *_: "")("User-Agent") or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"),
            )
            target_host = str(urlparse(target_url).hostname or "").strip().lower()
            seed_cookies = _iter_session_cookies(session, target_host=target_host) if session is not None else []
            if seed_cookies:
                try:
                    context.add_cookies(seed_cookies)
                    logger(f"[Stripe] 已注入 {len(seed_cookies)} 个会话 cookies 到 {target_host or 'checkout'}")
                except Exception as exc:
                    logger(f"[Stripe] 注入会话 cookies 失败: {exc}")
            explicit_cookies: list[dict[str, Any]] = []
            explicit_session_token = str(chatgpt_session_token or "").strip()
            if explicit_session_token:
                for cookie_name in ("__Secure-next-auth.session-token", "__Secure-authjs.session-token"):
                    explicit_cookies.append(
                        {
                            "name": cookie_name,
                            "value": explicit_session_token,
                            "url": "https://chatgpt.com/",
                            "secure": True,
                            "httpOnly": True,
                            "sameSite": "Lax",
                        }
                    )
            explicit_account_id = str(chatgpt_account_id or "").strip()
            if explicit_account_id:
                explicit_cookies.append(
                    {
                        "name": "_account",
                        "value": explicit_account_id,
                        "url": "https://chatgpt.com/",
                        "secure": True,
                        "sameSite": "Lax",
                    }
                )
            if explicit_cookies:
                try:
                    context.add_cookies(explicit_cookies)
                    logger(f"[Stripe] 已显式注入 ChatGPT session cookies: count={len(explicit_cookies)}")
                except Exception as exc:
                    logger(f"[Stripe] 显式注入 ChatGPT session cookies 失败: {exc}")
            page = context.new_page()

            def _probe_auth_session() -> dict[str, Any]:
                try:
                    result = page.evaluate(
                        """
                        async () => {
                          try {
                            const res = await fetch('/api/auth/session', { credentials: 'include' });
                            const text = await res.text();
                            let data = {};
                            try { data = text ? JSON.parse(text) : {}; } catch (e) { data = {}; }
                            return {
                              ok: res.ok,
                              status: res.status,
                              hasAccessToken: !!(data && data.accessToken),
                              hasUser: !!(data && data.user),
                              hasError: !!(data && data.error),
                            };
                          } catch (e) {
                            return { ok: false, status: 0, hasAccessToken: false, hasUser: false, hasError: true, error: String(e) };
                          }
                        }
                        """
                    )
                    return dict(result or {}) if isinstance(result, dict) else {}
                except Exception as exc:
                    return {"ok": False, "status": 0, "hasAccessToken": False, "hasUser": False, "hasError": True, "error": str(exc)}

            def on_response(response):
                url_text = str(response.url or "")
                if not any(token in url_text for token in ["payment_pages", "payment_methods", "checkout/approve"]):
                    return
                try:
                    text = response.text()[:2000]
                except Exception:
                    text = ""
                network_events.append((url_text, text))

            page.on("response", on_response)
            def _wait_for_web_session(timeout_seconds: int = 12) -> dict[str, Any]:
                deadline = time.time() + max(3, timeout_seconds)
                last_state: dict[str, Any] = {}
                while time.time() < deadline:
                    state = _probe_auth_session()
                    last_state = dict(state or {})
                    if state.get("hasAccessToken") or state.get("hasUser"):
                        logger(
                            f"[Stripe] ChatGPT Web 会话已建立: status={state.get('status')} access={state.get('hasAccessToken')} user={state.get('hasUser')}"
                        )
                        return last_state
                    page.wait_for_timeout(1000)
                logger(
                    f"[Stripe] ChatGPT Web 会话未建立: status={last_state.get('status')} access={last_state.get('hasAccessToken')} user={last_state.get('hasUser')} err={last_state.get('error') or ''}"
                )
                return last_state

            def _detect_cloudflare_challenge() -> tuple[bool, str]:
                current_url = str(page.url or "").strip()
                frame_urls = [str(getattr(frame, "url", "") or "").strip() for frame in page.frames]
                body_text = ""
                try:
                    body_text = (page.locator("body").inner_text(timeout=500) or "").lower()
                except Exception:
                    body_text = ""
                challenge_hit = (
                    "__cf_chl_rt_tk=" in current_url
                    or any("challenges.cloudflare.com" in url.lower() for url in frame_urls if url)
                    or any(token in body_text for token in ["just a moment", "checking your browser", "verify you are human", "cloudflare"])
                )
                detail = current_url or ", ".join(url for url in frame_urls if url)[:160]
                return challenge_hit, detail

            def _wait_cloudflare_pass(timeout_seconds: int = 25) -> str:
                deadline = time.time() + max(5, timeout_seconds)
                last_detail = ""
                while time.time() < deadline:
                    hit, detail = _detect_cloudflare_challenge()
                    last_detail = detail or last_detail
                    if not hit:
                        current_url = str(page.url or "").strip()
                        logger(f"[Stripe] Cloudflare challenge 已放行: {current_url[:160]}")
                        return current_url
                    page.wait_for_timeout(1000)
                raise RuntimeError(f"Cloudflare challenge 未放行: {last_detail[:200]}")
            def _goto_with_log(navigate_url: str, label: str) -> str:
                page.goto(navigate_url, wait_until="domcontentloaded", timeout=timeout_ms)
                page.wait_for_timeout(1200)
                current = str(page.url or "").strip()
                logger(f"[Stripe] {label}: {current[:160]}")
                return current

            checkout_mode = target_host == "chatgpt.com" and "/checkout/" in str(urlparse(target_url).path or "").lower()
            if checkout_mode:
                current_url = _goto_with_log("https://chatgpt.com/?refresh_account=true", "预热 ChatGPT 会话")
                _wait_for_web_session(10)
                hit, detail = _detect_cloudflare_challenge()
                if hit:
                    logger(f"[Stripe] 预热阶段命中 Cloudflare challenge: {detail[:160]}")
                    current_url = _wait_cloudflare_pass(25)
                    logger(f"[Stripe] 预热后状态: {current_url[:160]}")
                current_url = _goto_with_log(target_url, "进入 checkout")
                hit, detail = _detect_cloudflare_challenge()
                if hit:
                    logger(f"[Stripe] checkout 命中 Cloudflare challenge，等待放行: {detail[:160]}")
                    current_url = _wait_cloudflare_pass(30)
                    logger(f"[Stripe] challenge 放行后 checkout 状态: {current_url[:160]}")
                if "/checkout/" not in current_url.lower():
                    logger(f"[Stripe] 首次未进入 checkout，重试 refresh_account 后再次进入: {current_url[:160]}")
                    _goto_with_log("https://chatgpt.com/?refresh_account=true", "二次预热 ChatGPT 会话")
                    _wait_for_web_session(10)
                    current_url = _goto_with_log(target_url, "二次进入 checkout")
                hit, detail = _detect_cloudflare_challenge()
                if hit:
                    logger(f"[Stripe] 二次进入 checkout 仍命中 Cloudflare challenge，等待放行: {detail[:160]}")
                    current_url = _wait_cloudflare_pass(30)
                    logger(f"[Stripe] 二次 challenge 放行后 checkout 状态: {current_url[:160]}")
                if "/checkout/" not in current_url.lower():
                    state = _probe_auth_session()
                    raise RuntimeError(
                        f"未进入 ChatGPT checkout 页面，当前 URL={current_url[:200]} | web_session=status={state.get('status')} access={state.get('hasAccessToken')} user={state.get('hasUser')} err={state.get('error') or ''}"
                    )
            else:
                _goto_with_log(target_url, "进入支付页")
                hit, detail = _detect_cloudflare_challenge()
                if hit:
                    logger(f"[Stripe] 通用支付页命中 Cloudflare challenge，等待放行: {detail[:160]}")
                    _wait_cloudflare_pass(30)
            _fill_main_form(page, card)
            payment_element_wait_ms = min(timeout_ms, 90000 if proxy_url else 45000)
            payment_element_wait_ms = max(30000, int(payment_element_wait_ms))
            logger(
                f"[Stripe] 等待 Payment Element/iframe 加载: timeout={int(payment_element_wait_ms / 1000)}s proxy={'on' if proxy_url else 'off'}"
            )
            try:
                _fill_card_fields(page, card, wait_ms=payment_element_wait_ms, log_fn=logger)
            except RuntimeError as exc:
                if "未找到完整的 Stripe 卡片输入框" not in str(exc):
                    raise
                extra_wait_ms = min(30000 if proxy_url else 15000, max(10000, int(timeout_ms / 4)))
                logger(
                    f"[Stripe] 首轮等待后卡片输入框仍未就绪，追加等待 {int(extra_wait_ms / 1000)}s 后重试: {exc}"
                )
                page.wait_for_timeout(extra_wait_ms)
                _fill_card_fields(page, card, wait_ms=extra_wait_ms + 5000, log_fn=logger)
            billing_wait_ms = min(timeout_ms, 30000 if proxy_url else 15000)
            billing_wait_ms = max(5000, int(billing_wait_ms))
            logger(
                f"[Stripe] 等待账单地址区域加载并补填: timeout={int(billing_wait_ms / 1000)}s"
            )
            _wait_and_fill_main_form(page, card, wait_ms=billing_wait_ms, log_fn=logger)
            page.wait_for_timeout(300)
            _click_submit_button(page)
            logger("[Stripe] 已提交支付表单")

            deadline = time.time() + max(15, int(timeout_ms / 1000))
            while time.time() < deadline:
                page.wait_for_timeout(1000)
                current_url = str(page.url or "")
                body_text = (page.locator("body").inner_text(timeout=500) or "").lower()
                network_text = "\n".join(text.lower() for _url, text in network_events if text)

                if any(marker in current_url.lower() or marker in body_text or marker in network_text for marker in decline_markers):
                    return {
                        "success": False,
                        "declined": True,
                        "submitted": True,
                        "url": current_url,
                        "error": "检测到拒付/失败提示",
                        "network_events": network_events,
                    }
                if (not expect_decline) and any(marker in current_url.lower() or marker in body_text or marker in network_text for marker in success_markers):
                    return {
                        "success": True,
                        "declined": False,
                        "submitted": True,
                        "url": current_url,
                        "network_events": network_events,
                    }
                if (not expect_decline) and "checkout/approve" in network_text and "approved" in network_text:
                    return {
                        "success": True,
                        "declined": False,
                        "submitted": True,
                        "url": current_url,
                        "network_events": network_events,
                    }

            if expect_decline:
                return {
                    "success": False,
                    "declined": True,
                    "submitted": True,
                    "url": str(page.url or ""),
                    "error": "预检已提交，未检测到最终成功状态，按预检错误继续",
                    "network_events": network_events,
                }

            return {
                "success": False,
                "declined": False,
                "submitted": True,
                "url": str(page.url or ""),
                "error": "支付结果未在预期时间内明确返回",
                "network_events": network_events,
            }
        except PlaywrightTimeoutError as exc:
            return {
                "success": False,
                "declined": False,
                "submitted": False,
                "url": target_url,
                "error": f"支付页面超时: {exc}",
                "network_events": network_events,
            }
        finally:
            browser.close()
