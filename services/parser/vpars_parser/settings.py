import os

from pydantic import Field

from .models import Model


class Settings(Model):
    gateway_target: str = "127.0.0.1:50051"
    max_inflight: int = Field(default=6, ge=1, le=6)
    browser_timeout: int = Field(default=60, ge=1, le=300)
    rpc_grace: int = Field(default=15, ge=15, le=60)
    control_timeout: float = Field(default=3, ge=0.1, le=30)
    parse_timeout: float = Field(default=10, ge=0.1, le=60)
    max_html_bytes: int = Field(default=32 * 1024 * 1024, ge=1, le=32 * 1024 * 1024)

    @classmethod
    def from_env(cls):
        return cls(
            **{
                key: os.environ[f"PARSER_{key.upper()}"]
                for key in cls.model_fields
                if f"PARSER_{key.upper()}" in os.environ
            }
        )
