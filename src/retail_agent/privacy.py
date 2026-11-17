import hashlib
import hmac
import math
import re
from decimal import Decimal
from typing import Any

from retail_agent.errors import AgentError, ErrorCode

FORBIDDEN_FIELDS = frozenset(
    {
        "first_name",
        "last_name",
        "email",
        "street_address",
        "postal_code",
        "latitude",
        "longitude",
        "ip_address",
        "user_id",
        "customer_id",
    }
)
EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
SECRET = re.compile(r"\b(?:AIza[\w-]{20,}|AQ\.[\w.-]{20,}|eyJ[\w-]+\.[\w-]+\.[\w-]+)\b")
PHONE = re.compile(r"(?<!\w)(?:\+\d{1,3}[ .-]?)?(?:\(\d{3}\)|\d{3})[ .-]\d{3}[ .-]\d{4}\b")
ADDRESS = re.compile(
    r"\b\d{1,6}\s+(?:[A-Za-z]+\s+){1,5}"
    r"(?:street|st|avenue|ave|road|rd|lane|ln|drive|dr|boulevard|blvd)\b",
    re.I,
)
SENSITIVE_REQUEST = re.compile(
    r"\b(?:first[ _-]?name|last[ _-]?name|e-?mail|street address|postal[ _-]?code|"
    r"zip[ _-]?code|exact coordinates|latitude|longitude|phone number)\b",
    re.I,
)


def sanitize_text(text: str, *, user_input: bool = False) -> str:
    if len(text) > 12000:
        raise AgentError(ErrorCode.PRIVACY)
    if user_input and SENSITIVE_REQUEST.search(text):
        raise AgentError(ErrorCode.PRIVACY)
    for pattern in (EMAIL, SECRET, PHONE, ADDRESS):
        text = pattern.sub("[REDACTED]", text)
    return "".join(c for c in text if c in "\n\t" or (ord(c) >= 32 and ord(c) != 127))


def release_rows(
    rows: list[dict[str, Any]],
    *,
    columns: frozenset[str],
    minimum_group: int,
) -> list[dict[str, str | int | float | None]]:
    """Suppress the whole result if a hidden cell could be reconstructed from totals.

    `_unsafe_groups` is computed across all groups before LIMIT in trusted SQL.
    Raw counts below threshold are never returned to the model.
    """
    released: list[dict[str, str | int | float | None]] = []
    for row in rows:
        if set(row) != columns | {"_privacy_n", "_unsafe_groups"}:
            raise AgentError(ErrorCode.PRIVACY)
        n, unsafe = row["_privacy_n"], row["_unsafe_groups"]
        if type(n) is not int or type(unsafe) is not int or n < minimum_group or unsafe != 0:
            raise AgentError(ErrorCode.PRIVACY)
        result: dict[str, str | int | float | None] = {}
        for key in columns:
            if key.lower() in FORBIDDEN_FIELDS:
                raise AgentError(ErrorCode.PRIVACY)
            value = row[key]
            if isinstance(value, Decimal):
                value = float(value)
            if isinstance(value, str):
                result[key] = sanitize_text(value)
            elif (
                value is None or type(value) is int or type(value) is float and math.isfinite(value)
            ):
                result[key] = value
            else:
                raise AgentError(ErrorCode.PRIVACY)
        released.append(result)
    return released


def pseudonymize_customer_rows(rows: list[dict[str, Any]], salt: bytes) -> list[dict[str, Any]]:
    """Remove technical customer IDs inside the trusted data adapter, before any LLM call."""
    result = []
    for row in rows:
        raw = row.get("_customer_key")
        if not isinstance(raw, str) or not raw.isdecimal() or len(raw) > 20:
            raise AgentError(ErrorCode.PRIVACY)
        copy = {k: v for k, v in row.items() if k != "_customer_key"}
        copy["customer"] = (
            "customer_" + hmac.new(salt, raw.encode(), hashlib.sha256).hexdigest()[:16]
        )
        result.append(copy)
    return result
