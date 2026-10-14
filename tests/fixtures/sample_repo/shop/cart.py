from . import catalog, discounts


class Cart:
    def __init__(self):
        self.lines: dict[str, int] = {}

    def add(self, sku: str, qty: int = 1) -> None:
        catalog.price_of(sku)  # validates the sku
        self.lines[sku] = self.lines.get(sku, 0) + qty

    def total(self) -> int:
        total = 0
        for sku, qty in self.lines.items():
            line_total = catalog.price_of(sku) * qty
            total += line_total - discounts.bulk_discount(qty, line_total)
        return total
