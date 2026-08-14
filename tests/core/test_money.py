from decimal import Decimal

import pytest

from app.core.money import as_money, from_paise, sum_money, to_paise


def test_as_money_quantizes_to_two_places():
    assert as_money(Decimal("1.005")) == Decimal("1.01")
    assert as_money(Decimal("1.004")) == Decimal("1.00")


def test_as_money_rounds_half_up_not_bankers():
    # Decimal's default is ROUND_HALF_EVEN, which would give 2.02 here.
    assert as_money(Decimal("2.025")) == Decimal("2.03")


def test_as_money_accepts_int_and_str():
    assert as_money(5) == Decimal("5.00")
    assert as_money("5.5") == Decimal("5.50")


def test_as_money_rejects_float():
    with pytest.raises(TypeError):
        as_money(1.1)


def test_to_paise():
    assert to_paise(Decimal("12.34")) == 1234
    assert to_paise(Decimal("0.05")) == 5


def test_from_paise():
    assert from_paise(1234) == Decimal("12.34")


def test_paise_roundtrip():
    amount = Decimal("199.99")
    assert from_paise(to_paise(amount)) == amount


def test_sum_money_of_empty_is_zero():
    assert sum_money([]) == Decimal("0.00")


def test_sum_money_quantizes_result():
    assert sum_money([Decimal("1.005"), Decimal("1.005")]) == Decimal("2.01")
