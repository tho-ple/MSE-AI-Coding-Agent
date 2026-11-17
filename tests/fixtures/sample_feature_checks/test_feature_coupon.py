"""Protected feature check for the sample shop: coupon codes (cart + discounts modules)."""
import pytest

from shop import discounts
from shop.cart import Cart


def test_coupon_reduces_total():
    cart = Cart()
    cart.add("APPLE", 2)
    cart.apply_coupon("SAVE10")
    assert cart.total() == 90


def test_coupon_applies_after_bulk_discount():
    cart = Cart()
    cart.add("APPLE", 20)  # 1000 - 10% bulk = 900
    cart.apply_coupon("HALF")
    assert cart.total() == 450


def test_unknown_coupon_rejected_with_clear_error():
    cart = Cart()
    cart.add("PEAR")
    with pytest.raises(discounts.InvalidCoupon, match="BOGUS"):
        cart.apply_coupon("BOGUS")
    assert cart.total() == 80


def test_existing_callers_without_coupon_unchanged():
    cart = Cart()
    cart.add("APPLE", 3)
    cart.add("PEAR")
    assert cart.total() == 230
