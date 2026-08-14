import pytest
from cryptography.fernet import Fernet

from app.core.crypto import SecretBox, mask_secret

KEY = Fernet.generate_key().decode()


def test_roundtrip():
    box = SecretBox(KEY)
    assert box.decrypt(box.encrypt("rzp_live_supersecret")) == "rzp_live_supersecret"


def test_ciphertext_is_not_the_plaintext():
    box = SecretBox(KEY)
    assert "supersecret" not in box.encrypt("rzp_live_supersecret")


def test_same_plaintext_encrypts_differently_each_time():
    box = SecretBox(KEY)
    assert box.encrypt("same") != box.encrypt("same")


def test_wrong_key_cannot_decrypt():
    ciphertext = SecretBox(KEY).encrypt("secret")
    other = SecretBox(Fernet.generate_key().decode())
    with pytest.raises(ValueError):
        other.decrypt(ciphertext)


def test_tampered_ciphertext_is_rejected():
    box = SecretBox(KEY)
    ciphertext = box.encrypt("secret")
    tampered = ciphertext[:-2] + ("aa" if not ciphertext.endswith("aa") else "bb")
    with pytest.raises(ValueError):
        box.decrypt(tampered)


def test_tampering_the_middle_of_the_token_is_rejected():
    """The tail-swap above can fail base64 decoding rather than HMAC checking.

    Tampering in the middle lands inside the ciphertext body, so this is the
    test that actually proves authentication rather than parsing.
    """
    box = SecretBox(KEY)
    ciphertext = box.encrypt("a secret long enough to have a middle")
    middle = len(ciphertext) // 2
    swapped = "b" if ciphertext[middle] != "b" else "c"
    tampered = ciphertext[:middle] + swapped + ciphertext[middle + 1 :]

    assert tampered != ciphertext
    with pytest.raises(ValueError):
        box.decrypt(tampered)


def test_mask_shows_only_the_last_four_characters():
    assert mask_secret("rzp_live_abcd1234") == "••••1234"


def test_mask_of_short_value_reveals_nothing():
    assert mask_secret("abc") == "••••"


def test_mask_of_empty_value_is_empty():
    assert mask_secret("") == ""
