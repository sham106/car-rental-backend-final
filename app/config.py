from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[1] / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )
    app_env: Literal["development", "production", "test"] = "development"
    supabase_url: str = ""
    supabase_publishable_key: SecretStr = SecretStr("")
    supabase_secret_key: SecretStr = SecretStr("")
    storage_bucket: str = "fleet-files"
    notification_webhook_url: str = ""
    notification_webhook_secret: SecretStr = SecretStr("")
    frontend_origin: str = "http://localhost:3000"
    allowed_origins: list[str] = ["http://localhost:3000", "http://127.0.0.1:3000"]
    allowed_hosts: list[str] = ["localhost", "127.0.0.1"]
    cookie_secure: bool = False
    session_max_age: int = Field(default=604800, ge=3600, le=2592000)
    auth_rate_limit: int = Field(default=10, ge=1, le=100)

    @model_validator(mode="after")
    def validate_deployment(self):
        if self.notification_webhook_url:
            webhook = urlsplit(self.notification_webhook_url)
            if webhook.scheme != "https" or not webhook.netloc or webhook.username:
                raise ValueError("NOTIFICATION_WEBHOOK_URL must be an HTTPS URL")
        if not self.storage_bucket or "/" in self.storage_bucket:
            raise ValueError("STORAGE_BUCKET must be a bucket name")
        self.supabase_url = self.supabase_url.rstrip("/")
        if self.supabase_url:
            parsed = urlsplit(self.supabase_url)
            if not parsed.netloc or parsed.username or parsed.query or parsed.fragment:
                raise ValueError("SUPABASE_URL must be a project origin")
            if parsed.path or (
                parsed.scheme != "https"
                and not (
                    self.app_env != "production"
                    and parsed.scheme == "http"
                    and parsed.hostname in {"localhost", "127.0.0.1"}
                )
            ):
                raise ValueError("SUPABASE_URL must use HTTPS (except local development)")
        for origin in [self.frontend_origin, *self.allowed_origins]:
            parsed = urlsplit(origin)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or (parsed.path or parsed.query or parsed.fragment or parsed.username)
            ):
                raise ValueError("Frontend origins must be exact HTTP(S) origins without a path")
        if self.frontend_origin not in self.allowed_origins:
            raise ValueError("FRONTEND_ORIGIN must be in ALLOWED_ORIGINS")
        if self.app_env == "production":
            if not self.cookie_secure or not self.auth_configured:
                raise ValueError("Production requires configured Supabase and secure cookies")
            if any(not origin.startswith("https://") for origin in self.allowed_origins):
                raise ValueError("Production origins must use HTTPS")
            if "*" in self.allowed_hosts:
                raise ValueError("Production requires explicit ALLOWED_HOSTS")
        return self

    @property
    def auth_configured(self) -> bool:
        key = self.supabase_publishable_key.get_secret_value()
        return bool(self.supabase_url and key and "YOUR_" not in self.supabase_url + key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
