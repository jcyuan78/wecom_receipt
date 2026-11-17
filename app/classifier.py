"""One-shot document analysis: classify + extract invoice fields.

The image is sent to the local vision model **before** any cropping.  Cropping
is only used for archiving, so a bad segmentation can no longer destroy the
recognition result.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

from PIL import Image

from .imaging import prepare_for_model
from .invoices import parsing
from .invoices.extract import InvoiceFields, fields_from_dict
from .ollama import OllamaClient

log = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are a precise document router and OCR assistant. "
    "You always answer with a single JSON object and never add commentary."
)

ANALYZE_PROMPT = """\
You are looking at a photo that may contain one or more documents.

The message was received on {received}.

Step 1 - Categories:
- "receipt": invoice, receipt, tax invoice, 領収書, レシート, 发票, 收据, 付款凭证, order slip, meal ticket, expense document.
- "homework": worksheet, exercise sheet, homework, exam paper, math problems, study notes with questions or answers, 作业, 习题, 练习, 解答.
- "other": anything else (ID cards, slides, screenshots, scenery, ...).

Step 2 - If the category is "receipt", also read the purchase data from the image.

Return ONE JSON object with exactly these keys:
{{
  "category": "receipt" or "homework" or "other",
  "confidence": number between 0 and 1,
  "reason": "short reason in Chinese",
  "field_confidence": number between 0 and 1,
  "document_type": "receipt" or "invoice" or null,
  "invoice_no": string or null,
  "date": "YYYY-MM-DD" or null,
  "vendor": string or null,
  "currency": "JPY" or "CNY" or "USD" or ...,
  "total": number or null,
  "tax": number or null,
  "items": [{{"name": string, "qty": number or null, "amount": number or null}}],
  "rotation": 0 or 90 or 180 or 270
}}

Rules:
- invoice_no: 发票号码 / 发票号 / 領収書番号 / 請求書番号 / receipt number / invoice number.
- date: purchase or issue date, normalized to YYYY-MM-DD.
  It should normally be on or before {received}. Use this context to infer a
  missing year, a Japanese era (令和/平成/昭和), or an ambiguous month/day order.
- vendor: the store, shop, supplier or company name.
- total: the final amount actually paid, tax included. Look near 合計 / お会計 /
  总计 / 合计 / TOTAL / 金額.
- items: line items with name, quantity and amount. Use [] if not readable.
- currency: infer from symbols (¥/円 => JPY, 元/￥ => CNY, $ => USD).
- rotation: clockwise degrees needed to make the text upright (0 if already upright).
- Use null when a value is not visible. Never invent values.
- Numbers must be plain JSON numbers: no currency symbols, no thousands separators.
- If the image is rotated or cropped, still read it carefully.
"""

_ALIASES = {
    "invoice": "receipt",
    "receipts": "receipt",
    "发票": "receipt",
    "作业": "homework",
    "exercise": "homework",
    "worksheet": "homework",
    "study": "homework",
}


@dataclass(slots=True)
class Classification:
    category: str
    confidence: float
    reason: str
    fields: InvoiceFields | None = None


class Classifier:
    def __init__(self, ollama: OllamaClient, max_side: int = 1600) -> None:
        self.ollama = ollama
        self.max_side = max_side

    def analyze(
        self,
        image: Image.Image,
        *,
        received_date: date | None = None,
        default_currency: str = "JPY",
    ) -> Classification:
        payload = prepare_for_model(image, self.max_side)
        prompt = ANALYZE_PROMPT.format(
            received=(received_date or date.today()).isoformat()
        )
        data = self.ollama.chat_json(prompt, images=[payload], system=SYSTEM_PROMPT)

        raw_category = str(data.get("category", "")).strip().lower()
        category = _ALIASES.get(raw_category, raw_category)
        if category not in {"receipt", "homework", "other"}:
            category = "other"

        fields: InvoiceFields | None = None
        if category == "receipt":
            fields = fields_from_dict(
                data,
                default_currency,
                received_date,
                confidence_key="field_confidence",
            )

        confidence = parsing.parse_confidence(data.get("confidence"), default=0.0)
        reason = str(data.get("reason", ""))[:200]

        log.info(
            "图片分析: %s (%.2f) %s | 发票=%s 日期=%s 金额=%s",
            category,
            confidence,
            reason,
            getattr(fields, "invoice_no", None),
            getattr(fields, "invoice_date", None),
            getattr(fields, "amount_minor", None),
        )
        return Classification(
            category=category,
            confidence=confidence,
            reason=reason,
            fields=fields,
        )

    def classify(self, image: Image.Image) -> Classification:
        """Backwards-compatible wrapper without date context."""
        return self.analyze(image)
