PRICES = {"APPLE": 50, "PEAR": 80, "MELON": 300}  # cents


class UnknownSku(Exception):
    pass


def price_of(sku: str) -> int:
    try:
        return PRICES[sku]
    except KeyError:
        raise UnknownSku(sku) from None
