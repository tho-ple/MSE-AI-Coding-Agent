from shop.cart import Cart


def test_exactly_threshold_gets_discount():
    cart = Cart()
    cart.add("PEAR", 10)
    assert cart.total() == 720
