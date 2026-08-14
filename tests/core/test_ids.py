import pytest

from app.core.ids import IdPrefix, InvalidId, new_id, parse_id


def test_new_id_has_prefix_and_separator():
    value = new_id(IdPrefix.KIOSK)
    assert value.startswith("ksk_")


def test_new_id_body_is_16_chars():
    value = new_id(IdPrefix.ORDER)
    assert len(value.split("_", 1)[1]) == 16


def test_new_id_avoids_ambiguous_characters():
    # Crockford base32 drops i, l, o and u so ids can be read aloud to support.
    for _ in range(200):
        body = new_id(IdPrefix.DOCUMENT).split("_", 1)[1]
        assert not set(body) & {"i", "l", "o", "u"}


def test_new_ids_are_unique():
    values = {new_id(IdPrefix.ORDER) for _ in range(1000)}
    assert len(values) == 1000


def test_parse_id_returns_body_for_matching_prefix():
    value = new_id(IdPrefix.KIOSK)
    assert parse_id(value, IdPrefix.KIOSK) == value.split("_", 1)[1]


def test_parse_id_rejects_wrong_prefix():
    value = new_id(IdPrefix.KIOSK)
    with pytest.raises(InvalidId):
        parse_id(value, IdPrefix.ORDER)


def test_parse_id_rejects_garbage():
    for bad in ["", "ksk", "ksk_", "_abc", "ksk_ABC!", "ksk_" + "a" * 17]:
        with pytest.raises(InvalidId):
            parse_id(bad, IdPrefix.KIOSK)
