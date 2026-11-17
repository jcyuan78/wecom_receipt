"""Background worker: download media, persist it, trigger the processor."""

from __future__ import annotations

import logging
import threading
from datetime import datetime

from .config import Settings
from .client import MediaTooLarge, WeComClient
from .jobs import build_job, write_job
from .models import Message
from .processor import Job
from .storage import FileStorage
from .store import MessageStore

log = logging.getLogger(__name__)


class Worker(threading.Thread):
    def __init__(
        self,
        settings: Settings,
        store: MessageStore,
        storage: FileStorage,
        client: WeComClient,
        processor,
    ) -> None:
        super().__init__(name="wecom-worker", daemon=True)
        self.settings = settings
        self.store = store
        self.storage = storage
        self.client = client
        self.processor = processor
        self._stop_event = threading.Event()

    # ------------------------------------------------------------- lifecycle
    def run(self) -> None:  # pragma: no cover - exercised indirectly
        log.info("后台下载线程已启动")
        while not self._stop_event.is_set():
            try:
                processed = self.process_once()
            except Exception:  # noqa: BLE001 - keep the worker alive
                log.exception("后台任务异常")
                processed = 0
            if processed == 0:
                self._stop_event.wait(self.settings.worker_poll_seconds)

    def stop(self, timeout: float = 10.0) -> None:
        self._stop_event.set()
        if self.is_alive():
            self.join(timeout)

    # ---------------------------------------------------------------- work
    def process_once(self) -> int:
        rows = self.store.claim_due(limit=5)
        for row in rows:
            self._handle(row)
        return len(rows)

    def _handle(self, row: dict) -> None:
        msg_id = row["msg_id"]
        try:
            if not row.get("file_path"):
                saved = self._download(row)
                self.store.mark_downloaded(
                    msg_id,
                    filename=saved.name,
                    file_path=saved.path,
                    file_size=saved.size,
                    content_type=saved.content_type,
                    sha256=saved.sha256,
                )
                row = self.store.get(msg_id) or row

            job_data = build_job(row)
            job_path = write_job(self.settings.jobs_dir, job_data)
            self.processor(Job(job_data, job_path))
            self.store.mark_done(msg_id)
            log.info("消息 %s 处理完成（%s）", msg_id, row.get("filename"))
        except MediaTooLarge as exc:
            self.store.mark_permanent_failure(msg_id, str(exc))
        except Exception as exc:  # noqa: BLE001 - retry policy lives in the store
            self.store.mark_retry(msg_id, f"{type(exc).__name__}: {exc}")

    def _download(self, row: dict):
        media_id = row.get("media_id")
        if not media_id:
            raise MediaTooLarge("消息没有 media_id，无法下载")

        log.info("下载媒体 msg=%s media_id=%s", row["msg_id"], media_id)
        media = self.client.download_media(media_id)

        message = Message(
            msg_id=row["msg_id"],
            msg_type=row["msg_type"],
            from_user=row["from_user"],
            to_user=row["to_user"],
            agent_id=row["agent_id"],
            create_time=row["create_time"],
            media_id=media_id,
            file_name=row.get("filename"),
        )
        return self.storage.save(message, media, datetime.now().astimezone())
