from datetime import timedelta

import pytest
from passlib.hash import pbkdf2_sha256

from app.core.security import (
    TokenError,
    TokenType,
    create_token,
    decode_token,
    hash_password,
    needs_rehash,
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


def test_legacy_pbkdf2_hashes_still_verify():
    """Every user migrated from the old backend has a pbkdf2_sha256 hash.

    If this fails, nobody who existed before the migration can log in.
    """
    legacy = pbkdf2_sha256.hash("hunter2")
    assert verify_password("hunter2", legacy) is True
    assert verify_password("wrong", legacy) is False


def test_legacy_hashes_are_flagged_for_upgrade():
    assert needs_rehash(pbkdf2_sha256.hash("hunter2")) is True


def test_current_scheme_hashes_are_not_flagged_for_upgrade():
    assert needs_rehash(hash_password("hunter2")) is False


def test_new_hashes_use_bcrypt():
    assert hash_password("hunter2").startswith("$2")


def test_needs_rehash_on_garbage_does_not_raise():
    assert needs_rehash("not-a-hash") is False


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
