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

    # Ghostscript is named `gs` on Linux and `gswin64c` on Windows, and this
    # value is only a hint: app.modules.printing.pdfs falls back to the known
    # names when what is configured is not on PATH, so one .env works on the
    # developer's machine and on the VPS.
    GHOSTSCRIPT_PATH: str = "gs"
    GHOSTSCRIPT_TIMEOUT_SECONDS: int = 120
    STORAGE_ROOT: str = "./storage"

    # Upload caps. Both are abuse guards rather than product limits -- what a
    # student can actually print is bounded by the kiosk's paper and by what
    # they are willing to pay. The size cap matches the old backend's 64MB so
    # no existing upload becomes newly impossible at cutover.
    MAX_UPLOAD_MB: int = 64
    MAX_DOCUMENT_PAGES: int = 2000

    # Normalising a small PDF costs more than it saves, so it is skipped below
    # this size. 300dpi is print quality; going lower shows on paper.
    PDF_NORMALISE_MIN_BYTES: int = 8 * 1024 * 1024
    PDF_NORMALISE_DPI: int = 300

    # How long an uploaded file is kept. The row outlives the file so a
    # student's order history does not develop holes -- see DocumentState.
    FILE_RETENTION_DAYS: int = 7

    @property
    def max_upload_bytes(self) -> int:
        return self.MAX_UPLOAD_MB * 1024 * 1024

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
