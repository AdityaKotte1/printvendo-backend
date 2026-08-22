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
    # A student is waiting on this request, so the gateway does not get to hang
    # for the default socket timeout. A refused checkout they can retry beats a
    # spinner that never resolves.
    RAZORPAY_TIMEOUT_SECONDS: float = 20.0

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

    # Where this API answers from, used to build the webhook URL an owner
    # registers in their own Razorpay dashboard. Handed to them rather than
    # assembled by hand: a typo there is silent -- deliveries hit a URL naming
    # a different account, fail the signature check, and the owner's payments
    # never settle with no error anyone sees.
    PUBLIC_BASE_URL: str = "https://api.printvendo.com"

    # Where a person lands when they click a link in an email. The *app*, never
    # this API: a verification URL on api.printvendo.com puts somebody on a page
    # of JSON. The accept-invite and reset flows all live on the student app,
    # which is why one setting covers all three.
    APP_BASE_URL: str = "https://printvendo.com"

    # Who the email is from. A no-reply address that bounces is worse than none:
    # somebody who replies to an invitation deserves to reach a person.
    MAIL_FROM_EMAIL: str = "hello@printvendo.com"
    MAIL_FROM_NAME: str = "Printvendo"

    # ── the edge ────────────────────────────────────────────────────────────
    # Off is never the default. The switch exists for a load test and for an
    # incident where the limiter itself is the problem.
    RATE_LIMIT_ENABLED: bool = True

    # Whether `X-Forwarded-For` is evidence. True only when something we run
    # sits in front and appends the peer it saw: with no proxy there, the header
    # is whatever the caller typed, and every attacker gets a fresh bucket per
    # request. Without it behind a proxy the opposite happens -- the whole
    # internet arrives from one address and shares one bucket.
    TRUST_PROXY_HEADERS: bool = False

    @property
    def rate_limit_store_url(self) -> str:
        """Where hits are counted. Redis anywhere that runs more than one worker.

        An in-process counter under `--workers 4` enforces four times the
        configured number, silently. Production already requires Redis for the
        device socket, so there is nothing new to install -- and deriving this
        rather than adding a second setting means there is no way to configure
        a multi-worker deployment into counting locally.
        """
        return "" if self.ENV == "dev" else self.REDIS_URL

    CORS_ORIGINS: str = ""

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    @model_validator(mode="after")
    def _encryption_key_must_actually_work(self) -> "Settings":
        """A key of the right length that Fernet cannot use is not a key.

        Length alone was the only check, so 44 characters of anything booted
        cleanly and then raised the first time an owner's Razorpay secret was
        read or written -- which is the first checkout at an owner-collecting
        kiosk, in production, with a student waiting. Failing here instead
        turns a silent misconfiguration into a refusal to start.
        """
        from app.core.crypto import SecretBox

        try:
            SecretBox(self.SECRETS_ENCRYPTION_KEY)
        except Exception as exc:
            raise ValueError(
                "SECRETS_ENCRYPTION_KEY is not a valid Fernet key. "
                "Generate one with: "
                "python -c \"from cryptography.fernet import Fernet; "
                "print(Fernet.generate_key().decode())\""
            ) from exc
        return self

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
