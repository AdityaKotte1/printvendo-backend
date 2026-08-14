"""Application settings.

Two rules the old backend learned the hard way and this keeps:
  * the app refuses to boot with a weak JWT secret or, in production, without a
    Razorpay webhook secret;
  * the CORS allowlist comes from the environment, never from source, so adding
    a frontend is a deploy variable rather than a code change.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    ENV: Literal["dev", "staging", "prod"] = "dev"

    DATABASE_URL: str
    REDIS_URL: str

    JWT_SECRET_KEY: str = Field(min_length=32)
    ACCESS_TOKEN_MINUTES: int = 15
    REFRESH_TOKEN_DAYS: int = 30

    # Fernet key (urlsafe base64, 44 chars) used to encrypt stored third-party
    # secrets such as an owner's Razorpay key secret. See app/core/crypto.py.
    SECRETS_ENCRYPTION_KEY: str = Field(min_length=44)

    RAZORPAY_KEY_ID: str = ""
    RAZORPAY_KEY_SECRET: str = ""
    RAZORPAY_WEBHOOK_SECRET: str = ""

    GOOGLE_CLIENT_ID: str = ""
    VAPID_PUBLIC_KEY: str = ""
    VAPID_PRIVATE_KEY: str = ""
    VAPID_SUBJECT: str = ""
    BREVO_API_KEY: str = ""

    GHOSTSCRIPT_PATH: str = "gs"
    STORAGE_ROOT: str = "./storage"

    CORS_ORIGINS: str = ""

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    @model_validator(mode="after")
    def _production_requires_real_secrets(self) -> "Settings":
        if self.ENV != "prod":
            return self
        if not self.RAZORPAY_WEBHOOK_SECRET:
            raise ValueError("RAZORPAY_WEBHOOK_SECRET is required in production")
        if "*" in self.cors_origins:
            raise ValueError("Wildcard CORS origin is not allowed in production")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
