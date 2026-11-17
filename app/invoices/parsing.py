"""Normalization helpers for amounts, currencies, dates and invoice numbers."""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

# ISO 4217 minor-unit exponents (only the ones we are likely to meet).
CURRENCY_EXPONENT: dict[str, int] = {
    "JPY": 0,
    "KRW": 0,
    "CNY": 2,
    "USD": 2,
    "EUR": 2,
    "GBP": 2,
    "HKD": 2,
    "TWD": 2,
    "SGD": 2,
    "AUD": 2,
    "CAD": 2,
    "CHF": 2,
    "THB": 2,
}

_SYMBOL_TO_CODE = {
    "¥": "JPY",
    "￥": "JPY",
    "円": "JPY",
    "元": "CNY",
    "人民币": "CNY",
    "RMB": "CNY",
    "$": "USD",
    "US$": "USD",
    "€": "EUR",
    "£": "GBP",
    "₩": "KRW",
}
_KNOWN_CODES = set(CURRENCY_EXPONENT) | {"NZD", "MOP", "MYR", "PHP", "VND"}

_DATE_PATTERNS = (
    re.compile(r"(?P<y>\d{4})\s*[-/.年]\s*(?P<m>\d{1,2})\s*[-/.月]\s*(?P<d>\d{1,2})\s*日?"),
    re.compile(r"(?P<y>\d{4})(?P<m>\d{2})(?P<d>\d{2})"),
    re.compile(r"(?P<m>\d{1,2})\s*[-/]\s*(?P<d>\d{1,2})\s*[-/]\s*(?P<y>\d{4})"),
)

_INVOICE_PREFIX = re.compile(
    r"^(?:invoice\s*(?:no\.?|number)?|receipt\s*(?:no\.?|number)?|no\.?|#|"
    r"发票号码|发票号|請求書番号|領収書番号|伝票番号)\s*[:：#]?\s*",
    re.IGNORECASE,
)


def normalize_currency(value: Any, default: str = "JPY") -> str:
    if value is None:
        return default
    text = str(value).strip()
    if not text:
        return default

    upper = text.upper()
    if upper in _KNOWN_CODES:
        return upper
    for symbol, code in _SYMBOL_TO_CODE.items():
        if symbol in text:
            return code

    match = re.search(r"\b([A-Z]{3})\b", upper)
    if match and match.group(1) in _KNOWN_CODES:
        return match.group(1)
    return default


def parse_amount(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip()
    if not text:
        return None

    text = text.replace(",", "").replace("，", "").replace(" ", "").replace("\u00a0", "")
    text = re.sub(r"[^\d.\-]", "", text)
    if text in {"", "-", ".", "-."} or text.count(".") > 1:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def to_minor(amount: float, currency: str) -> int:
    exponent = CURRENCY_EXPONENT.get(currency.upper(), 2)
    try:
        value = Decimal(str(amount)) * (Decimal(10) ** exponent)
        return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError):
        return 0


def from_minor(amount_minor: int, currency: str) -> Decimal:
    exponent = CURRENCY_EXPONENT.get(currency.upper(), 2)
    return Decimal(amount_minor) / (Decimal(10) ** exponent)


def format_money(amount_minor: int, currency: str) -> str:
    exponent = CURRENCY_EXPONENT.get(currency.upper(), 2)
    value = from_minor(amount_minor, currency)
    return f"{value:,.{exponent}f} {currency}"


def parse_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value

    text = str(value).strip()
    if not text:
        return None

    for index, pattern in enumerate(_DATE_PATTERNS):
        match = pattern.search(text)
        if not match:
            continue
        year, month, day = int(match.group("y")), int(match.group("m")), int(match.group("d"))
        if index == 2 and month > 12 and day <= 12:  # only the M/D/Y pattern is ambiguous
            month, day = day, month
        try:
            return date(year, month, day)
        except ValueError:
            continue
    return None


def clean_invoice_no(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().strip('"').strip("'")
    if not text:
        return None
    text = _INVOICE_PREFIX.sub("", text)
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"[^0-9A-Za-z\-_/.]", "", text)
    text = text.strip("-/.")
    if not text or text.lower() in {"null", "none", "n/a", "unknown"}:
        return None
    return text[:64]


def parse_items(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, str):
        lines = [line.strip() for line in value.splitlines() if line.strip()]
        return [{"name": line[:200]} for line in lines[:50]]
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return []

    items: list[dict[str, Any]] = []
    for entry in value[:50]:
        if isinstance(entry, str):
            items.append({"name": entry[:200]})
            continue
        if not isinstance(entry, dict):
            continue
        name = entry.get("name") or entry.get("description") or entry.get("item")
        if not name:
            continue
        item: dict[str, Any] = {"name": str(name).strip()[:200]}
        quantity = parse_amount(entry.get("qty", entry.get("quantity")))
        amount = parse_amount(entry.get("amount", entry.get("price", entry.get("total"))))
        if quantity is not None:
            item["qty"] = quantity
        if amount is not None:
            item["amount"] = amount
        items.append(item)
    return items


def parse_confidence(value: Any, default: float = 0.5) -> float:
    number = parse_amount(value)
    if number is None:
        return default
    return max(0.0, min(1.0, float(number)))
