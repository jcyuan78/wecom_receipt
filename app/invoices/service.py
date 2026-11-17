"""Rectify, split, extract and persist invoices from one image.

A photo may contain one receipt or several.  ``segment_documents`` returns one
rectified image per detected paper region; each region becomes its own file and
database row.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from PIL import Image

from ..config import Settings
from ..imaging import (
    RectifyResult,
    encode_jpeg,
    load_image,
    rectify_document,
    segment_documents,
)
from ..ollama import OllamaClient
from ..processor import Job
from ..storage import safe_filename
from .db import DuplicateInvoiceError, InvoiceDB
from .extract import InvoiceFields, extract_invoice
from .parsing import format_money

log = logging.getLogger(__name__)


class InvoiceProcessingError(RuntimeError):
    pass


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(1, 1000):
        candidate = path.with_name(f"{path.stem} ({index}){path.suffix}")
        if not candidate.exists():
            return candidate
    raise InvoiceProcessingError(f"无法生成唯一文件名: {path}")


def _write_bytes(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.part")
    tmp.write_bytes(data)
    os.replace(tmp, path)


class InvoiceService:
    def __init__(self, settings: Settings, ollama: OllamaClient, db: InvoiceDB) -> None:
        self.settings = settings
        self.ollama = ollama
        self.db = db

    # ------------------------------------------------------------------ paths
    @property
    def root(self) -> Path:
        return Path(self.settings.receipt_dir)

    @property
    def originals_dir(self) -> Path:
        return self.root / "_originals"

    @property
    def duplicates_dir(self) -> Path:
        return self.root / "_duplicates"

    # ---------------------------------------------------------------- process
    def process(self, job: Job, source: Path, image: Image.Image | None = None) -> dict[str, Any]:
        raw_bytes = source.read_bytes()
        source_sha = hashlib.sha256(raw_bytes).hexdigest()
        image = image or load_image(raw_bytes)

        original_path = self._keep_original(job, source)
        segments = segment_documents(
            image,
            max_side=self.settings.receipt_max_side,
            max_regions=self.settings.invoice_max_regions,
        )
        multi = len(segments) > 1

        invoices: list[dict[str, Any]] = []
        duplicates: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []

        for index, segment in enumerate(segments):
            try:
                outcome = self._process_segment(
                    job,
                    segment,
                    index=index,
                    multi=multi,
                    source_sha=source_sha,
                    original_path=original_path,
                )
            except InvoiceProcessingError as exc:
                failures.append({"segment": index, "error": str(exc)})
                log.warning("第 %d 张发票识别失败: %s", index + 1, exc)
                continue

            if outcome["duplicate"]:
                duplicates.append(outcome)
            else:
                invoices.append(outcome)

        # A false-positive split can leave every segment unusable; fall back to
        # the old single-document behaviour so the file is not lost.
        if not invoices and not duplicates and len(segments) > 1:
            log.warning("分段后未得到有效发票，回退为整图识别")
            try:
                outcome = self._process_segment(
                    job,
                    rectify_document(image, self.settings.receipt_max_side),
                    index=0,
                    multi=False,
                    source_sha=source_sha,
                    original_path=original_path,
                )
            except InvoiceProcessingError as exc:
                failures.append({"segment": -1, "error": str(exc)})
            else:
                if outcome["duplicate"]:
                    duplicates.append(outcome)
                else:
                    invoices.append(outcome)

        if not invoices and not duplicates:
            details = "；".join(item["error"] for item in failures) or "未识别到发票"
            raise InvoiceProcessingError(details)

        self._remove_source(source, original_path)

        primary = invoices[0] if invoices else duplicates[0]
        log.info(
            "消息 %s: 检测到 %d 张，新增 %d 张，重复 %d 张，失败 %d 张",
            job.msg_id,
            len(segments),
            len(invoices),
            len(duplicates),
            len(failures),
        )
        return {
            "category": "receipt",
            "segments": len(segments),
            "invoice_count": len(invoices),
            "invoices": invoices,
            "duplicates": duplicates,
            "failed_segments": failures,
            "image_path": primary.get("image_path") or primary.get("moved_to"),
            "original_path": original_path,
        }

    # ----------------------------------------------------------------- segment
    def _process_segment(
        self,
        job: Job,
        segment: RectifyResult,
        *,
        index: int,
        multi: bool,
        source_sha: str,
        original_path: str,
    ) -> dict[str, Any]:
        jpeg = encode_jpeg(segment.image, 92)
        segment_sha = hashlib.sha256(jpeg).hexdigest()

        existing = self.db.find_duplicate(None, segment_sha)
        if existing is not None:
            return self._duplicate_segment(
                job, jpeg, index, multi, existing, reason="相同切片（sha256）"
            )

        fields, rotation = extract_invoice(
            self.ollama,
            segment.image,
            default_currency=self.settings.default_currency,
        )

        if fields.invoice_no:
            existing = self.db.find_duplicate(fields.invoice_no, None)
            if existing is not None:
                return self._duplicate_segment(
                    job, jpeg, index, multi, existing, reason="相同发票号"
                )

        if fields.invoice_date is None:
            raise InvoiceProcessingError("未能识别发票日期（date）")
        if fields.amount_minor is None:
            raise InvoiceProcessingError("未能识别发票金额（total）")

        needs_review = (
            not fields.is_complete
            or fields.confidence < self.settings.invoice_min_confidence
        )

        today = date.today()
        if not (date(1990, 1, 1) <= fields.invoice_date <= today + timedelta(days=366)):
            log.warning("发票日期异常: %s（可能识别错误）", fields.invoice_date)
            needs_review = True
        if fields.amount_minor <= 0:
            log.warning("发票金额异常: %s", fields.amount_minor)
            needs_review = True

        target = self._save_image(fields, jpeg, index=index, multi=multi, segment_sha=segment_sha)

        try:
            record_id = self.db.add(
                invoice_no=fields.invoice_no,
                invoice_date=fields.invoice_date,
                amount_minor=fields.amount_minor,
                currency=fields.currency,
                vendor=fields.vendor,
                items=fields.items,
                tax_minor=fields.tax_minor,
                image_path=str(target),
                source_msg_id=job.msg_id,
                source_path=original_path,
                source_sha256=source_sha,
                segment_index=index,
                segment_sha256=segment_sha,
                ai_model=self.ollama.model,
                ai_confidence=fields.confidence,
                needs_review=needs_review,
                raw=fields.raw,
            )
        except DuplicateInvoiceError as exc:
            target.unlink(missing_ok=True)
            return self._duplicate_segment(
                job, jpeg, index, multi, exc.existing, reason="并发写入时发现重复"
            )

        self._write_sidecar(
            target, job, fields, record_id, needs_review, rotation, original_path, index
        )

        summary = {
            "segment_index": index,
            "duplicate": False,
            "invoice_id": record_id,
            "invoice_no": fields.invoice_no,
            "invoice_date": fields.invoice_date.isoformat(),
            "amount_minor": fields.amount_minor,
            "amount_display": format_money(fields.amount_minor, fields.currency),
            "currency": fields.currency,
            "vendor": fields.vendor,
            "needs_review": needs_review,
            "confidence": fields.confidence,
            "rotation": rotation,
            "deskew_angle": round(segment.deskew_angle, 2),
            "warped": segment.warped,
            "segment_sha256": segment_sha,
            "image_path": str(target),
            "original_path": original_path,
        }
        log.info(
            "发票已入库 #%s %s %s %s（第 %d 张，%s）",
            record_id,
            fields.invoice_date,
            fields.vendor or "-",
            format_money(fields.amount_minor, fields.currency),
            index + 1,
            "需复核" if needs_review else "已完整",
        )
        return summary

    # ------------------------------------------------------------------ files
    def _save_image(
        self, fields: InvoiceFields, jpeg: bytes, *, index: int, multi: bool, segment_sha: str
    ) -> Path:
        folder = self.root / fields.invoice_date.strftime("%Y-%m")
        folder.mkdir(parents=True, exist_ok=True)

        parts = [
            fields.invoice_date.isoformat(),
            fields.vendor or "unknown",
            fields.invoice_no or f"auto-{segment_sha[:8]}",
        ]
        if multi:
            parts.append(f"p{index + 1}")
        stem = safe_filename("_".join(parts))[:90]
        target = _unique_path(folder / f"{stem}.jpg")
        _write_bytes(target, jpeg)
        return target

    def _keep_original(self, job: Job, source: Path) -> str:
        if not self.settings.keep_original:
            return ""
        self.originals_dir.mkdir(parents=True, exist_ok=True)
        target = self.originals_dir / safe_filename(f"{job.msg_id}_{source.name}")
        if not target.exists():
            shutil.copy2(source, target)
        return str(target)

    def _remove_source(self, source: Path, original_path: str) -> None:
        if original_path:
            try:
                if source.resolve() == Path(original_path).resolve():
                    return
            except OSError:
                pass
        try:
            source.unlink(missing_ok=True)
        except OSError:
            log.warning("删除 inbox 原文件失败: %s", source, exc_info=True)

    def _duplicate_target(self, job: Job, index: int, multi: bool) -> Path:
        self.duplicates_dir.mkdir(parents=True, exist_ok=True)
        suffix = f"_p{index + 1}" if multi else ""
        return _unique_path(self.duplicates_dir / safe_filename(f"{job.msg_id}{suffix}.jpg"))

    def _duplicate_segment(
        self,
        job: Job,
        jpeg: bytes,
        index: int,
        multi: bool,
        existing: dict[str, Any],
        *,
        reason: str,
    ) -> dict[str, Any]:
        target = self._duplicate_target(job, index, multi)
        _write_bytes(target, jpeg)
        log.warning(
            "重复发票，已移入 %s（原因: %s，原记录 #%s %s）",
            target,
            reason,
            existing.get("id"),
            existing.get("invoice_no"),
        )
        return {
            "segment_index": index,
            "duplicate": True,
            "duplicate_reason": reason,
            "duplicate_of": existing.get("id"),
            "invoice_no": existing.get("invoice_no"),
            "invoice_date": existing.get("invoice_date"),
            "amount_display": format_money(
                int(existing.get("amount_minor") or 0), str(existing.get("currency") or "JPY")
            ),
            "moved_to": str(target),
        }

    def _write_sidecar(
        self,
        target: Path,
        job: Job,
        fields: InvoiceFields,
        record_id: int,
        needs_review: bool,
        rotation: int,
        original_path: str,
        segment_index: int,
    ) -> None:
        document = {
            "schema": "wecom.receiver.invoice/v1",
            "invoice_id": record_id,
            "msg_id": job.msg_id,
            "from_user": job.data.get("from_user"),
            "received_at": job.data.get("received_at"),
            "segment_index": segment_index,
            "fields": fields.to_public_dict(),
            "needs_review": needs_review,
            "rotation_applied": rotation,
            "image_path": str(target),
            "original_path": original_path,
            "model": self.ollama.model,
        }
        sidecar = target.with_suffix(".json")
        sidecar.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
