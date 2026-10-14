BULK_THRESHOLD = 10  # lines with at least this many items get the bulk discount
BULK_PERCENT = 10


def bulk_discount(qty: int, line_total: int) -> int:
    """Discount in cents for one cart line."""
    if qty > BULK_THRESHOLD:
        return line_total * BULK_PERCENT // 100
    return 0
