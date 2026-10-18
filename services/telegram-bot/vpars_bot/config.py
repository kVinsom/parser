from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    telegram_bot_token: SecretStr = SecretStr("")
    wallet_session_encryption_key: SecretStr = SecretStr("")
    database_url: SecretStr = SecretStr(
        "postgresql+asyncpg://vpars:vpars_local@127.0.0.1:15432/vpars_bot"
    )
    redis_url: SecretStr = SecretStr("redis://127.0.0.1:16379/0")
    update_mode: Literal["polling", "webhook"] = "polling"
    app_env: Literal["development", "production"] = "development"
    search_adapter: Literal["unavailable", "demo"] = "unavailable"
    menu_mode: Literal["menu", "preview", "full"] = "menu"
    source_config: Path = Path("config/sources.json")
    webhook_base_url: str = ""
    webhook_secret: SecretStr = SecretStr("")
    webhook_path: str = "/telegram"
    http_host: str = "127.0.0.1"
    http_port: int = 8085
    wallet_handoff_url: str = ""
    wallet_checkout_mode: Literal["automatic", "manual", "balance_first", "bot"] = "automatic"
    wallet_cache_capacity: int = Field(default=4, ge=1, le=32)
    wallet_cache_idle_seconds: int = Field(default=300, ge=30, le=3600)
    wallet_handoff_port: int = Field(default=8086, ge=1024, le=65535)
    inbox_concurrency: int = Field(default=8, ge=1, le=32)
    handler_timeout: float = Field(default=20, ge=10, le=60)
    telegram_request_timeout: float = Field(default=7, ge=1, le=15)
    telegram_connection_limit: int = Field(default=40, ge=8, le=100)
    telegram_messages_per_second: float = Field(default=25, gt=0, le=30)
    telegram_chat_interval: float = Field(default=1, ge=1, le=10)
    webhook_max_connections: int = Field(default=40, ge=1, le=100)

    @property
    def checkout_window_url(self):
        from . import autobuy_availability as autobuy

        if not autobuy.AVAILABLE:
            return ""
        return "" if self.wallet_checkout_mode == "bot" else self.wallet_handoff_url

    @model_validator(mode="after")
    def validate_modes(self):
        if self.wallet_handoff_url:
            from urllib.parse import urlsplit

            target = urlsplit(self.wallet_handoff_url)
            if (target.scheme != "https" or not target.hostname or target.username
                    or target.password or target.port not in {None, 443}
                    or target.path not in {"", "/"} or target.query or target.fragment):
                raise ValueError("Wallet handoff requires an HTTPS origin")
            self.wallet_handoff_url = self.wallet_handoff_url.rstrip("/")
            if self.wallet_handoff_port == self.http_port:
                raise ValueError("Wallet handoff must use its own loopback port")
        if self.telegram_request_timeout >= self.handler_timeout:
            raise ValueError("Telegram request timeout must be less than handler timeout")
        if self.telegram_connection_limit < self.inbox_concurrency + 6:
            raise ValueError("Reserve HTTP connections for polling and callback acknowledgements")
        if self.search_adapter == "demo" and self.app_env != "development":
            raise ValueError("Demo search adapter requires APP_ENV=development")
        if not self.database_url.get_secret_value().startswith("postgresql+asyncpg://"):
            raise ValueError("DATABASE_URL must use PostgreSQL with asyncpg")
        if self.update_mode == "webhook":
            import re

            if not self.webhook_base_url.startswith("https://"):
                raise ValueError("Webhook requires an HTTPS base URL")
            if not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", self.webhook_secret.get_secret_value()):
                raise ValueError("WEBHOOK_SECRET must contain 32–256 safe characters")
            if not self.webhook_path.startswith("/") or "?" in self.webhook_path:
                raise ValueError("Invalid webhook path")
        return self
