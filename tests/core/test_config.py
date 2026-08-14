import pytest
from pydantic import ValidationError

from app.core.config import Settings

BASE_ENV = {
    "ENV": "dev",
    "DATABASE_URL": "postgresql+psycopg://u:p@localhost:5432/pv",
    "REDIS_URL": "redis://localhost:6379/0",
    "JWT_SECRET_KEY": "x" * 32,
    "SECRETS_ENCRYPTION_KEY": "k" * 44,
    "RAZORPAY_KEY_ID": "rzp_test_abc",
    "RAZORPAY_KEY_SECRET": "shh",
    "RAZORPAY_WEBHOOK_SECRET": "hook",
    "CORS_ORIGINS": "http://localhost:3000,http://localhost:3002",
}


def test_settings_load_from_env():
    settings = Settings(**BASE_ENV)
    assert settings.ENV == "dev"
    assert settings.DATABASE_URL.startswith("postgresql+psycopg://")


def test_cors_origins_parsed_into_list():
    settings = Settings(**BASE_ENV)
    assert settings.cors_origins == [
        "http://localhost:3000",
        "http://localhost:3002",
    ]


def test_cors_origins_strips_whitespace_and_blanks():
    settings = Settings(**{**BASE_ENV, "CORS_ORIGINS": " http://a.test , ,http://b.test "})
    assert settings.cors_origins == ["http://a.test", "http://b.test"]


def test_short_jwt_secret_is_rejected():
    with pytest.raises(ValidationError):
        Settings(**{**BASE_ENV, "JWT_SECRET_KEY": "tooshort"})


def test_prod_requires_webhook_secret():
    env = {**BASE_ENV, "ENV": "prod", "RAZORPAY_WEBHOOK_SECRET": ""}
    with pytest.raises(ValidationError):
        Settings(**env)


def test_dev_tolerates_missing_webhook_secret():
    settings = Settings(**{**BASE_ENV, "RAZORPAY_WEBHOOK_SECRET": ""})
    assert settings.RAZORPAY_WEBHOOK_SECRET == ""


def test_prod_rejects_wildcard_cors():
    env = {**BASE_ENV, "ENV": "prod", "CORS_ORIGINS": "*"}
    with pytest.raises(ValidationError):
        Settings(**env)


def test_access_token_lifetime_defaults_to_15_minutes():
    assert Settings(**BASE_ENV).ACCESS_TOKEN_MINUTES == 15
