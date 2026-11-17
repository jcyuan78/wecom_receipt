"""Classify an incoming image as receipt / homework / other."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from PIL import Image

from .imaging import prepare_for_model
from .ollama import OllamaClient

log = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are a precise document classifier. "
    "You always answer with a single JSON object and never add commentary."
)

CLASSIFY_PROMPT = """\
Look at this image and decide which category it belongs to.

Categories:
- "receipt": invoice, receipt, tax invoice, 領収書, レシート, 发票, 收据, 付款凭证, order slip, expense document.
- "homework": worksheet, exercise sheet, homework, exam paper, math problems, study notes with questions or answers, 作业, 习题, 练习, 解答.
- "other": anything else (photos, screenshots, ID cards, slides, ...).

Return ONE JSON object:
{"category": "receipt" or "homework" or "other",
 "confidence": number between 0 and 1,
 "reason": "short reason in Chinese"}
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


class Classifier:
    def __init__(self, ollama: OllamaClient, max_side: int = 1280) -> None:
        self.ollama = ollama
        self.max_side = max_side

    def classify(self, image: Image.Image) -> Classification:
        payload = prepare_for_model(image, self.max_side)
        data = self.ollama.chat_json(
            CLASSIFY_PROMPT, images=[payload], system=SYSTEM_PROMPT
        )

        raw_category = str(data.get("category", "")).strip().lower()
        category = _ALIASES.get(raw_category, raw_category)
        if category not in {"receipt", "homework", "other"}:
            category = "other"

        try:
            confidence = float(data.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0

        reason = str(data.get("reason", ""))[:200]
        log.info("图片分类: %s (%.2f) %s", category, confidence, reason)
        return Classification(category=category, confidence=max(0.0, min(1.0, confidence)), reason=reason)
