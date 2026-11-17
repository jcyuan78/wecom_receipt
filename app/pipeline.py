"""Auto routing processor: analyze -> receipt/homework/other directories."""

from __future__ import annotations

import logging
import shutil
from datetime import date, datetime
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

from .classifier import Classifier
from .config import Settings
from .imaging import load_image
from .invoices.db import InvoiceDB
from .invoices.service import InvoiceService
from .jobs import write_job
from .notify import ResultNotifier
from .ollama import OllamaClient
from .processor import Job
from .storage import safe_filename

log = logging.getLogger(__name__)

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".gif"}


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(1, 1000):
        candidate = path.with_name(f"{path.stem} ({index}){path.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"无法生成唯一文件名: {path}")


def _received_date(job: Job) -> date:
    raw = str(job.data.get("received_at") or "")
    try:
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone()
        return parsed.date()
    except ValueError:
        return date.today()


class AutoProcessor:
    """Used with ``WECOM_PROCESSOR=auto``."""

    def __init__(
        self,
        settings: Settings,
        *,
        ollama_transport=None,
        wecom_client=None,
    ) -> None:
        self.settings = settings
        settings.ensure_dirs()
        self.ollama = OllamaClient(
            base_url=settings.ollama_base_url,
            model=settings.ollama_model,
            timeout=settings.ollama_timeout,
            keep_alive=settings.ollama_keep_alive,
            num_ctx=settings.ollama_num_ctx,
            transport=ollama_transport,
        )
        self.db = InvoiceDB(settings.invoice_db_path)
        self.db.init()
        self.classifier = Classifier(self.ollama)
        self.invoice_service = InvoiceService(settings, self.ollama, self.db)
        self.notifier = (
            ResultNotifier(wecom_client, settings) if wecom_client is not None else None
        )

    def __call__(self, job: Job) -> None:
        source = Path(job.file_path)
        if not source.exists():
            raise FileNotFoundError(f"源文件不存在: {source}")

        if source.suffix.lower() in IMAGE_SUFFIXES:
            try:
                image = load_image(source)
            except (UnidentifiedImageError, OSError):
                log.warning("无法读取图片，归入 other: %s", source)
                outcome = self._move(job, source, self.settings.other_dir, "other")
            else:
                analysis = self.classifier.analyze(
                    image,
                    received_date=_received_date(job),
                    default_currency=self.settings.default_currency,
                )
                if analysis.category == "receipt":
                    outcome = self.invoice_service.process(
                        job, source, image, analysis=analysis
                    )
                    outcome["classifier"] = {
                        "category": analysis.category,
                        "confidence": analysis.confidence,
                        "reason": analysis.reason,
                    }
                elif analysis.category == "homework":
                    outcome = self._move(job, source, self.settings.homework_dir, "homework")
                else:
                    outcome = self._move(job, source, self.settings.other_dir, "other")
        else:
            outcome = self._move(job, source, self.settings.other_dir, "other")

        self._update_job(job, outcome)
        if outcome.get("category") == "receipt" and self.notifier is not None:
            self.notifier.invoice_result(job.data, outcome)

    def notify_failure(self, row: dict[str, Any], error: str) -> None:
        """Called by the worker after the last retry failed."""
        if self.notifier is None:
            return
        self.notifier.invoice_failure(str(row.get("from_user") or ""), error)

    # ------------------------------------------------------------------ files
    def _move(self, job: Job, source: Path, base: Path, category: str) -> dict[str, Any]:
        folder = Path(base) / datetime.now().strftime("%Y-%m")
        folder.mkdir(parents=True, exist_ok=True)
        name = safe_filename(f"{job.msg_id}_{source.name}")
        target = _unique_path(folder / name)
        shutil.move(str(source), target)
        log.info("已归类 %s -> %s", category, target)
        return {
            "category": category,
            "duplicate": False,
            "path": str(target),
            "image_path": str(target) if category != "other" else None,
        }

    @staticmethod
    def _update_job(job: Job, outcome: dict[str, Any]) -> None:
        job.data["processing"] = outcome
        new_path = outcome.get("image_path") or outcome.get("path") or outcome.get("moved_to")
        if new_path:
            job.data.setdefault("file", {})["path"] = str(new_path)
            job.data["file"]["name"] = Path(new_path).name
        write_job(job.path.parent, job.data)
        log.info("任务 %s 已更新: %s", job.msg_id, new_path or "-")

    def close(self) -> None:
        self.db.close()
        self.ollama.close()
