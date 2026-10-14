import pytest

from shop.cart import Cart
from shop.catalog import UnknownSku


def test_total_without_discount():
    cart = Cart()
    cart.add("APPLE", 3)
    cart.add("PEAR")
    assert cart.total() == 3 * 50 + 80


def test_large_order_gets_discount():
    cart = Cart()
    cart.add("APPLE", 20)
    assert cart.total() == 900


def test_unknown_sku():
    with pytest.raises(UnknownSku):
        Cart().add("KIWI")
