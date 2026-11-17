"""Invoice field extraction with a local vision model (Ollama)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from PIL import Image

from ..imaging import prepare_for_model
from ..ollama import OllamaClient
from . import parsing
from .dates import resolve_invoice_date

log = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are a precise OCR assistant for financial documents. "
    "You always answer with a single JSON object and never add commentary."
)

def build_extraction_prompt(received_date: date | None = None) -> str:
    context = ""
    if received_date is not None:
        context = (
            f"\nContext: this photo was received on {received_date.isoformat()}. "
            "The purchase date should normally be on or before that date and close to it. "
            "Use this context to infer a missing year, a Japanese era (令和/平成/昭和), "
            "or an ambiguous month/day order.\n"
        )

    return f"""\
Read this receipt or invoice image and extract the purchase information.

The document may be written in Japanese, Chinese or English.
{context}
Return ONE JSON object with exactly these keys:
{{
  "document_type": "receipt" or "invoice",
  "invoice_no": string or null,
  "date": "YYYY-MM-DD" or null,
  "vendor": string or null,
  "currency": "JPY" or "CNY" or "USD" or ...,
  "total": number or null,
  "tax": number or null,
  "items": [{{"name": string, "qty": number or null, "amount": number or null}}],
  "rotation": 0 or 90 or 180 or 270,
  "confidence": number between 0 and 1
}}

Rules:
- invoice_no: 发票号码 / 发票号 / 領収書番号 / 請求書番号 / receipt number / invoice number.
- date: the purchase or issue date printed on the document, normalized to YYYY-MM-DD.
  If the year is missing or printed as a Japanese era, infer it from the received date.
- vendor: the store, shop, supplier or company name.
- total: the final amount actually paid, tax included. Look near 合計 / お会計 / 总计 / 合计 / TOTAL / 金額.
- items: line items with name, quantity and amount. Use [] if not readable.
- currency: infer from symbols (¥/円 => JPY, 元/￥ => CNY, $ => USD).
- rotation: clockwise degrees needed to make the text upright (0 if already upright).
- Use null when a value is not visible. Never invent values.
- Numbers must be plain JSON numbers: no currency symbols, no thousands separators.
- If the image is slightly rotated, still read it carefully.
"""


@dataclass(slots=True)
class InvoiceFields:
    invoice_no: str | None
    invoice_date: date | None
    amount_minor: int | None
    currency: str
    vendor: str | None
    items: list[dict[str, Any]] = field(default_factory=list)
    tax_minor: int | None = None
    confidence: float = 0.0
    rotation: int = 0
    date_adjusted: bool = False
    document_type: str = "receipt"
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_complete(self) -> bool:
        return bool(
            self.invoice_no
            and self.invoice_date
            and self.amount_minor is not None
            and self.vendor
        )

    def score(self) -> int:
        score = 0
        score += 2 if self.invoice_no else 0
        score += 2 if self.invoice_date else 0
        score += 2 if self.amount_minor is not None else 0
        score += 2 if self.vendor else 0
        score += 1 if self.items else 0
        score += 1 if self.tax_minor is not None else 0
        return score

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "invoice_no": self.invoice_no,
            "invoice_date": self.invoice_date.isoformat() if self.invoice_date else None,
            "amount_minor": self.amount_minor,
            "currency": self.currency,
            "vendor": self.vendor,
            "items": self.items,
            "tax_minor": self.tax_minor,
            "confidence": self.confidence,
            "rotation": self.rotation,
            "date_adjusted": self.date_adjusted,
            "document_type": self.document_type,
        }


def fields_from_dict(
    data: dict[str, Any],
    default_currency: str = "JPY",
    received_date: date | None = None,
    *,
    confidence_key: str = "confidence",
) -> InvoiceFields:
    currency = parsing.normalize_currency(data.get("currency"), default_currency)
    total = parsing.parse_amount(data.get("total"))
    tax = parsing.parse_amount(data.get("tax"))
    rotation = parsing.parse_amount(data.get("rotation")) or 0
    rotation = int(rotation) if int(rotation) in {0, 90, 180, 270} else 0
    invoice_date, date_adjusted = resolve_invoice_date(data.get("date"), received_date)

    return InvoiceFields(
        invoice_no=parsing.clean_invoice_no(data.get("invoice_no")),
        invoice_date=invoice_date,
        amount_minor=parsing.to_minor(total, currency) if total is not None else None,
        currency=currency,
        vendor=(str(data.get("vendor")).strip()[:200] if data.get("vendor") else None),
        items=parsing.parse_items(data.get("items")),
        tax_minor=parsing.to_minor(tax, currency) if tax is not None else None,
        confidence=parsing.parse_confidence(data.get(confidence_key, data.get("confidence"))),
        rotation=rotation,
        document_type=str(data.get("document_type") or "receipt"),
        date_adjusted=date_adjusted,
        raw=data,
    )


def _rotate_clockwise(image: Image.Image, degrees: int) -> Image.Image:
    if degrees == 0:
        return image
    if degrees == 90:
        return image.transpose(Image.Transpose.ROTATE_270)  # 90° clockwise
    if degrees == 180:
        return image.transpose(Image.Transpose.ROTATE_180)
    if degrees == 270:
        return image.transpose(Image.Transpose.ROTATE_90)  # 270° clockwise
    return image


def extract_invoice(
    ollama: OllamaClient,
    image: Image.Image,
    *,
    default_currency: str = "JPY",
    max_side: int = 1600,
    received_date: date | None = None,
) -> tuple[InvoiceFields, int]:
    """Extract invoice fields, retrying with rotations when the model asks for it."""
    prompt = build_extraction_prompt(received_date)
    best: InvoiceFields | None = None
    best_rotation = 0
    attempts = 0

    def run(candidate: Image.Image, rotation: int) -> InvoiceFields:
        nonlocal attempts
        attempts += 1
        payload = prepare_for_model(candidate, max_side)
        data = ollama.chat_json(prompt, images=[payload], system=SYSTEM_PROMPT)
        fields = fields_from_dict(data, default_currency, received_date)
        fields.rotation = rotation
        return fields

    fields = run(image, 0)
    best, best_rotation = fields, 0

    if fields.rotation in {90, 180, 270}:
        rotated = _rotate_clockwise(image, fields.rotation)
        retry = run(rotated, fields.rotation)
        if retry.score() >= best.score():
            best, best_rotation = retry, fields.rotation

    # If key fields are still missing, try the common upside-down/landscape cases.
    for rotation in (180, 90):
        if best.is_complete or attempts >= 3:
            break
        if rotation == best_rotation:
            continue
        retry = run(_rotate_clockwise(image, rotation), rotation)
        if retry.score() > best.score():
            best, best_rotation = retry, rotation

    log.info(
        "发票识别: no=%s date=%s(adjusted=%s) amount=%s vendor=%s confidence=%.2f rotation=%s",
        best.invoice_no,
        best.invoice_date,
        best.date_adjusted,
        best.amount_minor,
        best.vendor,
        best.confidence,
        best_rotation,
    )
    return best, best_rotation
