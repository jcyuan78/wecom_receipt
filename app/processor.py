"""Downstream processors.

Three built-in modes:

``noop``     only write the job JSON (the next stage can watch ``data/jobs``)
``command``  run a local command with the job details in environment variables
``http``     POST the job JSON to a webhook

A processor is a callable that raises on failure; the worker then retries with
back-off (see :mod:`app.store`).
"""

from __future__ import annotations

from loguru import logger as log
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import Settings



class ProcessorError(RuntimeError):
    pass


@dataclass(slots=True)
class Job:
    data: dict
    path: Path

    @property
    def msg_id(self) -> str:
        return str(self.data["msg_id"])

    @property
    def file_path(self) -> str:
        return str(self.data["file"]["path"])

    def env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.update(
            {
                "WECOM_MSG_ID": str(self.data["msg_id"]),
                "WECOM_MSG_TYPE": str(self.data["msg_type"]),
                "WECOM_FROM_USER": str(self.data["from_user"]),
                "WECOM_RECEIVED_AT": str(self.data["received_at"]),
                "WECOM_FILE_PATH": self.file_path,
                "WECOM_FILE_NAME": str(self.data["file"]["name"]),
                "WECOM_FILE_SIZE": str(self.data["file"]["size"]),
                "WECOM_FILE_SHA256": str(self.data["file"]["sha256"]),
                "WECOM_JOB_PATH": str(self.path),
            }
        )
        return env


class NoopProcessor:
    def __call__(self, job: Job) -> None:
        log.info("noop 处理器：文件已就绪 {}（job: {}）", job.file_path, job.path)


class CommandProcessor:
    def __init__(self, command: str, timeout: float = 300.0) -> None:
        self.command = command
        self.timeout = timeout

    def __call__(self, job: Job) -> None:
        log.info("执行处理器命令: {}", self.command)
        try:
            result = subprocess.run(
                self.command,
                shell=True,
                cwd=str(Path(job.file_path).parent),
                env=job.env(),
                capture_output=True,
                timeout=self.timeout,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except subprocess.TimeoutExpired as exc:
            raise ProcessorError(f"处理器命令超时（{self.timeout}s）") from exc
        except OSError as exc:
            raise ProcessorError(f"无法启动处理器命令: {exc}") from exc

        if result.stdout.strip():
            log.info("处理器输出: {}", result.stdout.strip()[:4000])
        if result.stderr.strip():
            log.warning("处理器 stderr: {}", result.stderr.strip()[:4000])
        if result.returncode != 0:
            raise ProcessorError(f"处理器命令退出码 {result.returncode}")


class HttpProcessor:
    def __init__(self, url: str, timeout: float = 300.0) -> None:
        self.url = url
        self.timeout = timeout

    def __call__(self, job: Job) -> None:
        log.info("POST job 到 {}", self.url)
        try:
            response = httpx.post(self.url, json={**job.data, "job_path": str(job.path)}, timeout=self.timeout)
        except httpx.HTTPError as exc:
            raise ProcessorError(f"webhook 调用失败: {exc}") from exc
        if response.status_code >= 300:
            raise ProcessorError(f"webhook 返回 HTTP {response.status_code}: {response.text[:500]}")


def build_processor(
    settings: Settings,
    *,
    ollama_transport: httpx.BaseTransport | None = None,
    wecom_client=None,
):
    if settings.processor == "command":
        return CommandProcessor(settings.processor_command, settings.processor_timeout)
    if settings.processor == "http":
        return HttpProcessor(settings.processor_webhook_url, settings.processor_timeout)
    if settings.processor == "auto":
        from .pipeline import AutoProcessor

        return AutoProcessor(
            settings,
            ollama_transport=ollama_transport,
            wecom_client=wecom_client,
        )
    return NoopProcessor()
