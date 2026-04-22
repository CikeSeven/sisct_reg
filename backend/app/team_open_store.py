from __future__ import annotations

import json
import re
import time
from typing import Any

from .db import connection, row_to_dict

CARD_IMPORT_PATTERN = re.compile(r"[^0-9]")
COMMENT_PREFIXES = ("#", "//", ";")


def init_team_open_db() -> None:
    with connection(write=True) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS team_open_cards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                card_number TEXT NOT NULL,
                exp_month TEXT NOT NULL DEFAULT '',
                exp_year TEXT NOT NULL DEFAULT '',
                cvc TEXT NOT NULL DEFAULT '',
                holder_name TEXT NOT NULL DEFAULT '',
                billing_email TEXT NOT NULL DEFAULT '',
                country TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL DEFAULT '',
                city TEXT NOT NULL DEFAULT '',
                line1 TEXT NOT NULL DEFAULT '',
                postal_code TEXT NOT NULL DEFAULT '',
                label TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                success_count INTEGER NOT NULL DEFAULT 0,
                failure_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                last_used REAL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                UNIQUE(card_number, exp_month, exp_year, cvc)
            );
            CREATE INDEX IF NOT EXISTS idx_team_open_cards_enabled_id ON team_open_cards(enabled, id);

            CREATE TABLE IF NOT EXISTS team_open_jobs (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                total INTEGER NOT NULL DEFAULT 0,
                success INTEGER NOT NULL DEFAULT 0,
                failed INTEGER NOT NULL DEFAULT 0,
                completed INTEGER NOT NULL DEFAULT 0,
                progress TEXT NOT NULL DEFAULT '0/0',
                request_json TEXT NOT NULL DEFAULT '{}',
                error TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS team_open_job_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                level TEXT NOT NULL DEFAULT 'info',
                account_email TEXT NOT NULL DEFAULT '',
                message TEXT NOT NULL,
                created_at REAL NOT NULL,
                FOREIGN KEY(job_id) REFERENCES team_open_jobs(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_team_open_job_events_job_id_id ON team_open_job_events(job_id, id);

            CREATE TABLE IF NOT EXISTS team_open_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                attempt_index INTEGER NOT NULL,
                email TEXT NOT NULL DEFAULT '',
                password TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                card_id INTEGER,
                card_label TEXT NOT NULL DEFAULT '',
                precheck_status TEXT NOT NULL DEFAULT '',
                payment_status TEXT NOT NULL DEFAULT '',
                payment_url TEXT NOT NULL DEFAULT '',
                payment_service TEXT NOT NULL DEFAULT '',
                team_account_id TEXT NOT NULL DEFAULT '',
                team_name TEXT NOT NULL DEFAULT '',
                imported_parent_id INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '',
                extra_json TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                UNIQUE(job_id, attempt_index),
                FOREIGN KEY(job_id) REFERENCES team_open_jobs(id) ON DELETE CASCADE,
                FOREIGN KEY(card_id) REFERENCES team_open_cards(id) ON DELETE SET NULL
            );
            CREATE INDEX IF NOT EXISTS idx_team_open_results_job_id_id ON team_open_results(job_id, id);
            """
        )


def finalize_orphaned_team_open_jobs() -> int:
    now = time.time()
    with connection(write=True) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS total FROM team_open_jobs WHERE status IN ('pending', 'running')"
        ).fetchone()
        total = int((row_to_dict(row) or {}).get("total") or 0)
        if total <= 0:
            return 0
        conn.execute(
            "UPDATE team_open_jobs SET status = 'stopped', updated_at = ? WHERE status IN ('pending', 'running')",
            (now,),
        )
        return total


def _normalize_country(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    aliases = {
        "us": "US",
        "usa": "US",
        "united states": "US",
        "united states of america": "US",
        "美国": "US",
        "美國": "US",
        "sg": "SG",
        "singapore": "SG",
        "新加坡": "SG",
    }
    if lowered in aliases:
        return aliases[lowered]
    if len(text) == 2 and text.isalpha():
        return text.upper()
    return text


def _clean_card_number(value: str) -> str:
    return CARD_IMPORT_PATTERN.sub("", str(value or "")).strip()


def _normalize_year(value: str) -> str:
    digits = _clean_card_number(value)
    if len(digits) >= 4:
        return digits[-2:]
    if len(digits) == 1:
        return f"0{digits}"
    return digits[-2:]


def _normalize_month(value: str) -> str:
    digits = _clean_card_number(value)
    if not digits:
        return ""
    try:
        month = max(1, min(int(digits), 12))
        return f"{month:02d}"
    except Exception:
        return digits[-2:].zfill(2)


LABELED_CARD_FIELD_PREFIXES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("card_number", ("card number", "card_number", "卡号", "卡號")),
    ("expiry", ("有效期", "过期时间", "過期時間", "expiry", "exp")),
    ("exp_month", ("有效期月份", "月份", "exp_month")),
    ("exp_year", ("有效期年份", "年份", "exp_year")),
    ("cvc", ("安全码", "安全碼", "验证码", "驗證碼", "cvv", "cvc")),
    ("billing_email", ("账单邮箱", "帳單郵箱", "邮箱", "郵箱", "email")),
    ("holder_name", ("持卡人姓名", "持卡人", "姓名", "name", "holder")),
    ("line1", ("账单地址", "帳單地址", "地址第 1 行", "地址第一行", "地址", "address line 1", "address", "line1")),
    ("city", ("城市", "city")),
    ("state", ("州/省", "州", "省", "state", "province")),
    ("postal_code", ("邮政编码", "郵政編碼", "邮编", "郵編", "postal code", "postcode", "zip code", "zip")),
    ("country", ("国家或地区", "國家或地區", "国家", "國家", "地区", "地區", "country", "region")),
    ("label", ("标签", "標籤", "备注", "備註", "label")),
)


def _strip_labeled_card_line_prefix(line: str) -> str:
    return re.sub(r"^[\s\-\*•·▶▷▪▫🕐⏰⌛]+", "", str(line or "")).strip()


def _extract_labeled_card_field(line: str) -> tuple[str, str] | None:
    text = _strip_labeled_card_line_prefix(line)
    if not text:
        return None
    lowered = text.lower()
    for key, labels in LABELED_CARD_FIELD_PREFIXES:
        for label in labels:
            label_text = str(label or "").strip()
            if not label_text:
                continue
            if lowered.startswith(label_text.lower()):
                value = text[len(label_text):].strip()
                value = value.lstrip("：:=-—– \\t").strip()
                if value:
                    return key, value
    return None


def _extract_labeled_card_fields(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for raw_line in str(text or "").splitlines():
        line = str(raw_line or "").strip()
        if not line or line.startswith(COMMENT_PREFIXES):
            continue
        extracted = _extract_labeled_card_field(line)
        if not extracted:
            continue
        key, value = extracted
        fields[key] = value
    return fields


def _split_team_open_card_import_records(data: str) -> list[tuple[int, str]]:
    records: list[tuple[int, str]] = []
    buffer: list[str] = []
    buffer_start = 0
    for index, raw_line in enumerate(str(data or "").splitlines(), start=1):
        line = str(raw_line or "").strip()
        if not line or line.startswith(COMMENT_PREFIXES):
            if buffer:
                continue
            continue
        extracted = _extract_labeled_card_field(line)
        if extracted and extracted[0] == "card_number" and buffer:
            records.append((buffer_start, "\n".join(buffer)))
            buffer = []
            buffer_start = 0
        if extracted or buffer:
            if not buffer:
                buffer_start = index
            buffer.append(line)
            continue
        records.append((index, line))
    if buffer:
        records.append((buffer_start, "\n".join(buffer)))
    return records


def _parse_labeled_team_open_card_text(
    text: str,
    *,
    default_holder_name: str = "",
    default_billing_email: str = "",
    default_country: str = "",
    default_state: str = "",
    default_city: str = "",
    default_line1: str = "",
    default_postal_code: str = "",
) -> dict[str, str] | None:
    fields = _extract_labeled_card_fields(text)
    if not fields:
        return None

    card_number = _clean_card_number(fields.get("card_number", ""))
    if len(card_number) < 13:
        raise ValueError("中文格式缺少有效卡号")

    expiry = str(fields.get("expiry") or "").strip()
    exp_month = ""
    exp_year = ""
    if expiry:
        if "/" in expiry:
            left, _, right = expiry.partition("/")
            exp_month = _normalize_month(left)
            exp_year = _normalize_year(right)
        else:
            digits = _clean_card_number(expiry)
            if len(digits) == 4:
                exp_month = _normalize_month(digits[:2])
                exp_year = _normalize_year(digits[2:])
            elif len(digits) >= 6:
                exp_month = _normalize_month(digits[:2])
                exp_year = _normalize_year(digits[-2:])
    if not exp_month and fields.get("exp_month"):
        exp_month = _normalize_month(fields.get("exp_month", ""))
    if not exp_year and fields.get("exp_year"):
        exp_year = _normalize_year(fields.get("exp_year", ""))
    if not exp_month or not exp_year:
        raise ValueError("中文格式缺少有效期")

    cvc = _clean_card_number(fields.get("cvc", ""))
    if len(cvc) < 3:
        raise ValueError("中文格式缺少有效 CVV/CVC")

    holder_name = str(fields.get("holder_name") or default_holder_name or "").strip()
    billing_email = str(fields.get("billing_email") or default_billing_email or "").strip()
    country = _normalize_country(str(fields.get("country") or default_country or ""))
    state = str(fields.get("state") or default_state or "").strip()
    city = str(fields.get("city") or default_city or "").strip()
    line1 = str(fields.get("line1") or default_line1 or "").strip()
    postal_code = str(fields.get("postal_code") or default_postal_code or "").strip()
    label = str(fields.get("label") or "").strip()

    result = {
        "card_number": card_number,
        "exp_month": exp_month,
        "exp_year": exp_year,
        "cvc": cvc,
        "holder_name": holder_name,
        "billing_email": billing_email,
        "country": country,
        "state": state,
        "city": city,
        "line1": line1,
        "postal_code": postal_code,
        "label": label,
    }
    result["label"] = _build_card_label(result)
    return result

def _mask_card_number(number: str) -> str:
    digits = _clean_card_number(number)
    if len(digits) <= 8:
        return digits
    return f"{digits[:4]} **** **** {digits[-4:]}"


def _build_card_label(record: dict[str, Any]) -> str:
    explicit = str(record.get("label") or "").strip()
    if explicit:
        return explicit
    holder = str(record.get("holder_name") or "").strip()
    last4 = str(record.get("card_number") or "")[-4:]
    city = str(record.get("city") or "").strip()
    base = " / ".join(part for part in [holder or "card", city or "", last4 and f"尾号 {last4}"] if part)
    return base or (last4 and f"尾号 {last4}") or "未命名银行卡"


def parse_team_open_card_line(
    line: str,
    *,
    default_holder_name: str = "",
    default_billing_email: str = "",
    default_country: str = "",
    default_state: str = "",
    default_city: str = "",
    default_line1: str = "",
    default_postal_code: str = "",
) -> dict[str, str]:
    raw = str(line or "").strip()
    if not raw:
        raise ValueError("空行")
    if raw.startswith(COMMENT_PREFIXES):
        raise ValueError("注释行")

    labeled = _parse_labeled_team_open_card_text(
        raw,
        default_holder_name=default_holder_name,
        default_billing_email=default_billing_email,
        default_country=default_country,
        default_state=default_state,
        default_city=default_city,
        default_line1=default_line1,
        default_postal_code=default_postal_code,
    )
    if labeled is not None:
        return labeled

    parts = [part.strip() for part in re.split(r"----|\||\t|\s{2,}", raw) if part.strip()]
    if len(parts) < 3:
        parts = [part.strip() for part in raw.split() if part.strip()]
    if len(parts) < 3:
        raise ValueError("字段不足，至少需要卡号、有效期、CVC")

    card_number = _clean_card_number(parts[0])
    if len(card_number) < 13:
        raise ValueError("卡号格式无效")

    offset = 1
    exp_month = ""
    exp_year = ""
    expiry_token = str(parts[1] or "").strip()
    if "/" in expiry_token:
        left, _, right = expiry_token.partition("/")
        exp_month = _normalize_month(left)
        exp_year = _normalize_year(right)
        offset = 2
    elif len(_clean_card_number(expiry_token)) == 4 and len(parts) >= 3:
        digits = _clean_card_number(expiry_token)
        exp_month = _normalize_month(digits[:2])
        exp_year = _normalize_year(digits[2:])
        offset = 2
    else:
        if len(parts) < 4:
            raise ValueError("缺少月份/年份/CVC")
        exp_month = _normalize_month(parts[1])
        exp_year = _normalize_year(parts[2])
        offset = 3

    cvc = _clean_card_number(parts[offset])
    offset += 1
    if len(cvc) < 3:
        raise ValueError("CVC 格式无效")

    rest = parts[offset:]
    holder_name = str(default_holder_name or "").strip()
    billing_email = str(default_billing_email or "").strip()
    country = _normalize_country(default_country)
    state = str(default_state or "").strip()
    city = str(default_city or "").strip()
    line1 = str(default_line1 or "").strip()
    postal_code = str(default_postal_code or "").strip()
    label = ""

    if rest:
        looks_like_country = len(rest[0]) <= 3 and rest[0].upper() == rest[0] and rest[0].isalpha()
        if "@" in rest[0] and not holder_name:
            billing_email = rest[0]
            rest = rest[1:]
        elif rest[0] and not looks_like_country:
            holder_name = rest[0]
            rest = rest[1:]

    if rest:
        if "@" in rest[0]:
            billing_email = rest[0]
            rest = rest[1:]

    if rest:
        country = _normalize_country(rest[0]) or country
    if len(rest) > 1:
        state = str(rest[1] or "").strip() or state
    if len(rest) > 2:
        city = str(rest[2] or "").strip() or city
    if len(rest) > 3:
        line1 = str(rest[3] or "").strip() or line1
    if len(rest) > 4:
        postal_code = str(rest[4] or "").strip() or postal_code
    if len(rest) > 5:
        label = str(rest[5] or "").strip()

    result = {
        "card_number": card_number,
        "exp_month": exp_month,
        "exp_year": exp_year,
        "cvc": cvc,
        "holder_name": holder_name,
        "billing_email": billing_email,
        "country": country,
        "state": state,
        "city": city,
        "line1": line1,
        "postal_code": postal_code,
        "label": label,
    }
    if not result["holder_name"]:
        result["holder_name"] = str(default_holder_name or "").strip()
    if not result["billing_email"]:
        result["billing_email"] = str(default_billing_email or "").strip()
    result["country"] = _normalize_country(result["country"])
    result["label"] = _build_card_label(result)
    return result


def _team_open_card_row_to_item(row, *, include_sensitive: bool = False) -> dict[str, Any]:
    data = row_to_dict(row) or {}
    item = {
        "id": int(data.get("id") or 0),
        "last4": str(data.get("card_number") or "")[-4:],
        "masked_number": _mask_card_number(str(data.get("card_number") or "")),
        "exp_month": str(data.get("exp_month") or ""),
        "exp_year": str(data.get("exp_year") or ""),
        "holder_name": str(data.get("holder_name") or ""),
        "billing_email": str(data.get("billing_email") or ""),
        "country": str(data.get("country") or ""),
        "state": str(data.get("state") or ""),
        "city": str(data.get("city") or ""),
        "line1": str(data.get("line1") or ""),
        "postal_code": str(data.get("postal_code") or ""),
        "label": str(data.get("label") or ""),
        "enabled": bool(data.get("enabled")),
        "success_count": int(data.get("success_count") or 0),
        "failure_count": int(data.get("failure_count") or 0),
        "last_error": str(data.get("last_error") or ""),
        "last_used": float(data.get("last_used") or 0),
        "created_at": float(data.get("created_at") or 0),
        "updated_at": float(data.get("updated_at") or 0),
    }
    if include_sensitive:
        item["card_number"] = str(data.get("card_number") or "")
        item["cvc"] = str(data.get("cvc") or "")
    return item


def import_team_open_cards(
    data: str,
    *,
    enabled: bool = True,
    default_holder_name: str = "",
    default_billing_email: str = "",
    default_country: str = "",
    default_state: str = "",
    default_city: str = "",
    default_line1: str = "",
    default_postal_code: str = "",
) -> dict[str, Any]:
    parsed_total = 0
    success = 0
    updated = 0
    failed = 0
    errors: list[str] = []
    now = time.time()
    imported_card: dict[str, str] | None = None

    with connection(write=True) as conn:
        for index, record_text in _split_team_open_card_import_records(data):
            parsed_total += 1
            try:
                card = parse_team_open_card_line(
                    record_text,
                    default_holder_name=default_holder_name,
                    default_billing_email=default_billing_email,
                    default_country=default_country,
                    default_state=default_state,
                    default_city=default_city,
                    default_line1=default_line1,
                    default_postal_code=default_postal_code,
                )
            except Exception as exc:
                failed += 1
                errors.append(f"行 {index}: {exc}")
                continue

            existing = conn.execute(
                """
                SELECT id
                FROM team_open_cards
                WHERE card_number = ? AND exp_month = ? AND exp_year = ? AND cvc = ?
                LIMIT 1
                """,
                (
                    card["card_number"],
                    card["exp_month"],
                    card["exp_year"],
                    card["cvc"],
                ),
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE team_open_cards
                    SET holder_name = ?, billing_email = ?, country = ?, state = ?, city = ?, line1 = ?, postal_code = ?,
                        label = ?, enabled = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        card["holder_name"],
                        card["billing_email"],
                        card["country"],
                        card["state"],
                        card["city"],
                        card["line1"],
                        card["postal_code"],
                        card["label"],
                        1 if enabled else 0,
                        now,
                        int(existing["id"] or 0),
                    ),
                )
                updated += 1
                imported_card = dict(card)
            else:
                conn.execute(
                    """
                    INSERT INTO team_open_cards(
                        card_number, exp_month, exp_year, cvc, holder_name, billing_email,
                        country, state, city, line1, postal_code, label, enabled,
                        success_count, failure_count, last_error, last_used, created_at, updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, '', NULL, ?, ?)
                    """,
                    (
                        card["card_number"],
                        card["exp_month"],
                        card["exp_year"],
                        card["cvc"],
                        card["holder_name"],
                        card["billing_email"],
                        card["country"],
                        card["state"],
                        card["city"],
                        card["line1"],
                        card["postal_code"],
                        card["label"],
                        1 if enabled else 0,
                        now,
                        now,
                    ),
                )
                success += 1
                imported_card = dict(card)

    return {
        "total": parsed_total,
        "success": success,
        "updated": updated,
        "failed": failed,
        "errors": errors,
        "summary": get_team_open_card_summary(),
        "imported_card": imported_card or {},
    }


def list_team_open_cards(*, limit: int = 200, enabled_only: bool = False, include_sensitive: bool = False) -> list[dict[str, Any]]:
    query = "SELECT * FROM team_open_cards"
    params: list[Any] = []
    if enabled_only:
        query += " WHERE enabled = 1"
    query += " ORDER BY enabled DESC, id ASC LIMIT ?"
    params.append(max(1, min(int(limit or 200), 1000)))
    with connection() as conn:
        rows = conn.execute(query, params).fetchall()
    return [_team_open_card_row_to_item(row, include_sensitive=include_sensitive) for row in rows]


def get_team_open_card_summary(*, limit: int = 200) -> dict[str, Any]:
    with connection() as conn:
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN enabled = 1 THEN 1 ELSE 0 END) AS enabled,
                SUM(CASE WHEN enabled = 0 THEN 1 ELSE 0 END) AS disabled,
                SUM(success_count) AS success_count,
                SUM(failure_count) AS failure_count
            FROM team_open_cards
            """
        ).fetchone()
    data = row_to_dict(row) or {}
    return {
        "total": int(data.get("total") or 0),
        "enabled": int(data.get("enabled") or 0),
        "disabled": int(data.get("disabled") or 0),
        "success_count": int(data.get("success_count") or 0),
        "failure_count": int(data.get("failure_count") or 0),
        "items": list_team_open_cards(limit=limit),
    }


def delete_team_open_card(card_id: int) -> bool:
    with connection(write=True) as conn:
        cur = conn.execute("DELETE FROM team_open_cards WHERE id = ?", (int(card_id or 0),))
        return int(cur.rowcount or 0) > 0


def mark_team_open_card_usage(card_id: int, *, success: bool, error: str = "") -> None:
    if int(card_id or 0) <= 0:
        return
    with connection(write=True) as conn:
        if success:
            conn.execute(
                """
                UPDATE team_open_cards
                SET success_count = success_count + 1,
                    last_error = '',
                    last_used = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (time.time(), time.time(), int(card_id)),
            )
        else:
            conn.execute(
                """
                UPDATE team_open_cards
                SET failure_count = failure_count + 1,
                    last_error = ?,
                    last_used = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (str(error or ""), time.time(), time.time(), int(card_id)),
            )


def create_team_open_job(job_id: str, *, request_payload: dict[str, Any]) -> None:
    now = time.time()
    with connection(write=True) as conn:
        conn.execute(
            """
            INSERT INTO team_open_jobs(id, status, total, success, failed, completed, progress, request_json, error, created_at, updated_at)
            VALUES(?, 'pending', 0, 0, 0, 0, '0/0', ?, '', ?, ?)
            """,
            (str(job_id), json.dumps(request_payload or {}, ensure_ascii=False), now, now),
        )


def update_team_open_job(job_id: str, **fields: Any) -> None:
    if not fields:
        return
    updates = {key: value for key, value in dict(fields).items() if value is not None}
    if not updates:
        return
    updates["updated_at"] = time.time()
    columns = ", ".join(f"{key} = ?" for key in updates)
    values: list[Any] = []
    for key, value in updates.items():
        if key == "request_json" and not isinstance(value, str):
            values.append(json.dumps(value or {}, ensure_ascii=False))
        else:
            values.append(value)
    values.append(str(job_id))
    with connection(write=True) as conn:
        conn.execute(f"UPDATE team_open_jobs SET {columns} WHERE id = ?", values)


def get_team_open_job(job_id: str) -> dict[str, Any] | None:
    with connection() as conn:
        row = conn.execute("SELECT * FROM team_open_jobs WHERE id = ? LIMIT 1", (str(job_id),)).fetchone()
    data = row_to_dict(row)
    if not data:
        return None
    try:
        data["request_json"] = json.loads(str(data.get("request_json") or "{}"))
    except Exception:
        data["request_json"] = {}
    return data


def list_team_open_jobs(*, limit: int = 20) -> list[dict[str, Any]]:
    with connection() as conn:
        rows = conn.execute(
            "SELECT * FROM team_open_jobs ORDER BY created_at DESC, id DESC LIMIT ?",
            (max(1, min(int(limit or 20), 100)),),
        ).fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        data = row_to_dict(row) or {}
        try:
            data["request_json"] = json.loads(str(data.get("request_json") or "{}"))
        except Exception:
            data["request_json"] = {}
        items.append(data)
    return items


def append_team_open_job_event(job_id: str, message: str, *, level: str = "info", account_email: str = "") -> int:
    with connection(write=True) as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) AS seq FROM team_open_job_events WHERE job_id = ?",
            (str(job_id),),
        ).fetchone()
        seq = int((row_to_dict(row) or {}).get("seq") or 0) + 1
        cur = conn.execute(
            """
            INSERT INTO team_open_job_events(job_id, seq, level, account_email, message, created_at)
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            (str(job_id), seq, str(level or "info"), str(account_email or ""), str(message or ""), time.time()),
        )
        return int(cur.lastrowid or 0)


def list_team_open_job_events(job_id: str, *, limit: int = 500) -> list[dict[str, Any]]:
    with connection() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM team_open_job_events
            WHERE job_id = ?
            ORDER BY id ASC
            LIMIT ?
            """,
            (str(job_id), max(1, min(int(limit or 500), 2000))),
        ).fetchall()
    return [row_to_dict(row) or {} for row in rows]


def upsert_team_open_result(job_id: str, attempt_index: int, **fields: Any) -> None:
    normalized_job_id = str(job_id)
    normalized_attempt_index = int(attempt_index or 0)
    if not normalized_job_id or normalized_attempt_index <= 0:
        return

    allowed_keys = {
        "email",
        "password",
        "status",
        "card_id",
        "card_label",
        "precheck_status",
        "payment_status",
        "payment_url",
        "payment_service",
        "team_account_id",
        "team_name",
        "imported_parent_id",
        "error",
        "extra_json",
    }
    updates = {key: value for key, value in fields.items() if key in allowed_keys}
    if "card_id" in updates:
        try:
            normalized_card_id = int(updates.get("card_id") or 0)
        except Exception:
            normalized_card_id = 0
        updates["card_id"] = normalized_card_id if normalized_card_id > 0 else None
    now = time.time()
    with connection(write=True) as conn:
        row = conn.execute(
            "SELECT id FROM team_open_results WHERE job_id = ? AND attempt_index = ? LIMIT 1",
            (normalized_job_id, normalized_attempt_index),
        ).fetchone()
        if row:
            if not updates:
                conn.execute(
                    "UPDATE team_open_results SET updated_at = ? WHERE id = ?",
                    (now, int(row["id"] or 0)),
                )
                return
            updates["updated_at"] = now
            columns = ", ".join(f"{key} = ?" for key in updates)
            values: list[Any] = []
            for key, value in updates.items():
                if key == "extra_json" and not isinstance(value, str):
                    values.append(json.dumps(value or {}, ensure_ascii=False))
                else:
                    values.append(value)
            values.append(int(row["id"] or 0))
            conn.execute(f"UPDATE team_open_results SET {columns} WHERE id = ?", values)
            return

        payload = {
            "email": "",
            "password": "",
            "status": "pending",
            "card_id": None,
            "card_label": "",
            "precheck_status": "",
            "payment_status": "",
            "payment_url": "",
            "payment_service": "",
            "team_account_id": "",
            "team_name": "",
            "imported_parent_id": 0,
            "error": "",
            "extra_json": {},
        }
        payload.update(updates)
        extra_json = payload.get("extra_json")
        conn.execute(
            """
            INSERT INTO team_open_results(
                job_id, attempt_index, email, password, status, card_id, card_label, precheck_status,
                payment_status, payment_url, payment_service, team_account_id, team_name,
                imported_parent_id, error, extra_json, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                normalized_job_id,
                normalized_attempt_index,
                str(payload.get("email") or ""),
                str(payload.get("password") or ""),
                str(payload.get("status") or "pending"),
                payload.get("card_id"),
                str(payload.get("card_label") or ""),
                str(payload.get("precheck_status") or ""),
                str(payload.get("payment_status") or ""),
                str(payload.get("payment_url") or ""),
                str(payload.get("payment_service") or ""),
                str(payload.get("team_account_id") or ""),
                str(payload.get("team_name") or ""),
                int(payload.get("imported_parent_id") or 0),
                str(payload.get("error") or ""),
                json.dumps(extra_json or {}, ensure_ascii=False) if not isinstance(extra_json, str) else extra_json,
                now,
                now,
            ),
        )


def list_team_open_results(job_id: str) -> list[dict[str, Any]]:
    with connection() as conn:
        rows = conn.execute(
            "SELECT * FROM team_open_results WHERE job_id = ? ORDER BY attempt_index ASC",
            (str(job_id),),
        ).fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        data = row_to_dict(row) or {}
        try:
            data["extra_json"] = json.loads(str(data.get("extra_json") or "{}"))
        except Exception:
            data["extra_json"] = {}
        items.append(data)
    return items
