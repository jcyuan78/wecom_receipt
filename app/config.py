"""Application configuration.

All settings are read from environment variables prefixed with ``WECOM_``
(or from a local ``.env`` file).  See ``.env.example`` for the full list.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigError(RuntimeError):
    """Raised when the service is started without a usable configuration."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="WECOM_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- WeCom credentials -------------------------------------------------
    corp_id: str = ""
    agent_id: str = ""
    app_secret: str = ""
    callback_token: str = ""
    callback_aes_key: str = ""

    # --- HTTP server -------------------------------------------------------
    host: str = "0.0.0.0"
    port: int = Field(default=8080, ge=1, le=65535)
    public_base_url: str = ""

    # --- Storage -----------------------------------------------------------
    data_dir: Path = Path("data")
    max_file_mb: int = Field(default=100, ge=1, le=2048)

    # --- WeCom API ---------------------------------------------------------
    wecom_api_base: str = "https://qyapi.weixin.qq.com"
    http_timeout: float = Field(default=30.0, gt=0)

    # --- Background worker -------------------------------------------------
    worker_enabled: bool = True
    worker_poll_seconds: float = Field(default=1.0, gt=0)
    max_attempts: int = Field(default=3, ge=1, le=20)

    # --- Behaviour ---------------------------------------------------------
    auto_reply: str = "文件已收到，正在处理中。"
    notify_results: bool = True
    notify_failure: bool = True

    # --- Downstream processor ---------------------------------------------
    processor: Literal["noop", "command", "http", "auto"] = "noop"
    processor_command: str = ""
    processor_webhook_url: str = ""
    processor_timeout: float = Field(default=300.0, gt=0)

    # --- Local AI (Ollama) -------------------------------------------------
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen2.5vl:7b"
    ollama_timeout: float = Field(default=300.0, gt=0)
    ollama_keep_alive: str = "10m"
    ollama_num_ctx: int = Field(default=8192, ge=2048, le=131072)

    # --- Auto routing / invoice pipeline ----------------------------------
    receipt_dir: Path = Path("data/receipt")
    homework_dir: Path = Path("data/homework")
    other_dir: Path = Path("data/other")
    invoice_db: Path | None = None
    default_currency: str = "JPY"
    invoice_min_confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    receipt_max_side: int = Field(default=2200, ge=800, le=6000)
    invoice_max_regions: int = Field(default=6, ge=1, le=20)
    keep_original: bool = True

    # --- Debug proxy -------------------------------------------------------
    # mode=proxy: this server only forwards WeCom traffic to a dev machine.
    mode: Literal["normal", "proxy"] = "normal"
    debug_proxy_target: str = ""
    debug_proxy_token: str = ""
    debug_proxy_timeout: float = Field(default=120.0, gt=0)

    # --- Operations --------------------------------------------------------
    admin_token: str = ""
    log_level: str = "INFO"

    # ------------------------------------------------------------------ paths
    @property
    def inbox_dir(self) -> Path:
        return self.data_dir / "inbox"

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "receiver.sqlite3"

    @property
    def invoice_db_path(self) -> Path:
        return self.invoice_db or (self.data_dir / "invoices.sqlite3")

    # -------------------------------------------------------------- lifecycle
    def ensure_dirs(self) -> None:
        paths = (
            self.data_dir,
            self.inbox_dir,
            self.jobs_dir,
            self.logs_dir,
            self.receipt_dir,
            self.homework_dir,
            self.other_dir,
        )
        for path in paths:
            path.mkdir(parents=True, exist_ok=True)

    def validate_for_runtime(self) -> None:
        """Validate the fields that are required to talk to WeCom."""
        if self.mode == "proxy":
            self.validate_proxy()
            return

        missing = [
            name
            for name in ("corp_id", "agent_id", "app_secret", "callback_token", "callback_aes_key")
            if not getattr(self, name).strip()
        ]
        if missing:
            raise ConfigError(
                "缺少必要配置: "
                + ", ".join(f"WECOM_{name.upper()}" for name in missing)
                + "。请复制 .env.example 为 .env 并填写。"
            )

        key = self.callback_aes_key.strip()
        if len(key) != 43:
            raise ConfigError("WECOM_CALLBACK_AES_KEY 必须是 43 位字符（企业微信后台生成的 EncodingAESKey）。")

        if self.processor == "command" and not self.processor_command.strip():
            raise ConfigError("WECOM_PROCESSOR=command 时必须配置 WECOM_PROCESSOR_COMMAND。")
        if self.processor == "http" and not self.processor_webhook_url.strip():
            raise ConfigError("WECOM_PROCESSOR=http 时必须配置 WECOM_PROCESSOR_WEBHOOK_URL。")
        if self.processor == "auto" and not self.ollama_model.strip():
            raise ConfigError("WECOM_PROCESSOR=auto 时必须配置 WECOM_OLLAMA_MODEL（需支持视觉的模型）。")

    def masked_corp_id(self) -> str:
        if len(self.corp_id) <= 6:
            return self.corp_id
        return f"{self.corp_id[:4]}***{self.corp_id[-2:]}"

    def validate_proxy(self) -> None:
        if not self.debug_proxy_target.strip():
            raise ConfigError(
                "WECOM_MODE=proxy 时必须配置 WECOM_DEBUG_PROXY_TARGET（开发机地址，"
                "例如 http://192.168.1.23:8081）。"
            )
        if not self.debug_proxy_token.strip():
            raise ConfigError(
                "WECOM_MODE=proxy 时必须配置 WECOM_DEBUG_PROXY_TOKEN（服务器与开发机共享的调试令牌）。"
            )

    @field_validator("default_currency")
    @classmethod
    def _upper_currency(cls, value: str) -> str:
        return (value or "JPY").strip().upper()
