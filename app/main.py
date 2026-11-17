"""Application factory."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from .callback import AppContext, router
from .client import WeComClient
from .config import ConfigError, Settings
from .crypto import WeComCrypto
from .logging_setup import configure_logging
from .processor import build_processor
from .storage import FileStorage
from .store import MessageStore
from .worker import Worker

log = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
    ollama_transport: httpx.BaseTransport | None = None,
) -> FastAPI:
    """Build the ASGI app.

    ``transport`` / ``ollama_transport`` are only used by tests to stub the
    WeCom and Ollama HTTP APIs.
    """
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        configure_logging(settings)
        try:
            settings.ensure_dirs()
            settings.validate_for_runtime()
        except (ConfigError, OSError):
            log.exception("启动配置校验失败")
            raise

        crypto = WeComCrypto(
            settings.callback_token, settings.callback_aes_key, settings.corp_id
        )
        store = MessageStore(settings.db_path, max_attempts=settings.max_attempts)
        store.init()
        recovered = store.reset_stuck()
        if recovered:
            log.warning("恢复了 %d 条上次未完成的任务", recovered)

        client = WeComClient(
            corp_id=settings.corp_id,
            secret=settings.app_secret,
            api_base=settings.wecom_api_base,
            timeout=settings.http_timeout,
            max_file_mb=settings.max_file_mb,
            transport=transport,
        )
        storage = FileStorage(settings.inbox_dir)
        processor = build_processor(
            settings, ollama_transport=ollama_transport, wecom_client=client
        )
        worker = Worker(settings, store, storage, client, processor)

        app.state.ctx = AppContext(
            settings=settings,
            crypto=crypto,
            store=store,
            storage=storage,
            client=client,
            worker=worker,
        )

        if settings.worker_enabled:
            worker.start()
        log.info(
            "服务已启动 corp=%s processor=%s worker=%s",
            settings.masked_corp_id(),
            settings.processor,
            "on" if settings.worker_enabled else "off",
        )
        try:
            yield
        finally:
            worker.stop()
            client.close()
            store.close()
            close_processor = getattr(processor, "close", None)
            if callable(close_processor):
                close_processor()
            log.info("服务已停止")

    app = FastAPI(
        title="WeCom File Receiver",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    app.include_router(router)
    return app


app = create_app()
