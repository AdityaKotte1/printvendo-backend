from datetime import timedelta

import pytest

from app.core.security import (
    TokenError,
    TokenType,
    create_token,
    decode_token,
    hash_password,
    verify_password,
)

SECRET = "s" * 32


def test_hash_is_not_the_password():
    assert hash_password("hunter2") != "hunter2"


def test_hash_is_salted():
    assert hash_password("hunter2") != hash_password("hunter2")


def test_verify_accepts_correct_password():
    assert verify_password("hunter2", hash_password("hunter2")) is True


def test_verify_rejects_wrong_password():
    assert verify_password("wrong", hash_password("hunter2")) is False


def test_access_token_roundtrip():
    token = create_token("usr_abc", TokenType.ACCESS, SECRET, timedelta(minutes=15))
    claims = decode_token(token, TokenType.ACCESS, SECRET)
    assert claims.subject == "usr_abc"


def test_refresh_token_carries_a_family_id():
    token = create_token(
        "usr_abc", TokenType.REFRESH, SECRET, timedelta(days=30), family="fam_1"
    )
    assert decode_token(token, TokenType.REFRESH, SECRET).family == "fam_1"


def test_refresh_token_is_rejected_where_an_access_token_is_required():
    token = create_token("usr_abc", TokenType.REFRESH, SECRET, timedelta(days=30))
    with pytest.raises(TokenError):
        decode_token(token, TokenType.ACCESS, SECRET)


def test_expired_token_is_rejected():
    token = create_token("usr_abc", TokenType.ACCESS, SECRET, timedelta(seconds=-1))
    with pytest.raises(TokenError):
        decode_token(token, TokenType.ACCESS, SECRET)


def test_token_signed_with_another_secret_is_rejected():
    token = create_token("usr_abc", TokenType.ACCESS, "o" * 32, timedelta(minutes=15))
    with pytest.raises(TokenError):
        decode_token(token, TokenType.ACCESS, SECRET)


def test_every_token_has_a_unique_jti():
    a = create_token("usr_abc", TokenType.ACCESS, SECRET, timedelta(minutes=15))
    b = create_token("usr_abc", TokenType.ACCESS, SECRET, timedelta(minutes=15))
    assert decode_token(a, TokenType.ACCESS, SECRET).jti != decode_token(
        b, TokenType.ACCESS, SECRET
    ).jti
