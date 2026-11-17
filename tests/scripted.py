"""Scripted model replies shared by the Stage 2 tests (no model, no network, no API key)."""
from coding_harness.llm import ai, tool_call

DISCOUNTS_EDIT = tool_call("edit_file", {
    "path": "shop/discounts.py", "old_text": "BULK_PERCENT = 10\n",
    "new_text": ("BULK_PERCENT = 10\nCOUPONS = {\"SAVE10\": 10, \"HALF\": 50}\n\n\n"
                 "class InvalidCoupon(Exception):\n    pass\n\n\n"
                 "def coupon_percent(code: str) -> int:\n    try:\n        return COUPONS[code]\n"
                 "    except KeyError:\n        raise InvalidCoupon(f\"unknown coupon {code}\") from None\n")})
CART_INIT_EDIT = tool_call("edit_file", {
    "path": "shop/cart.py", "old_text": "        self.lines: dict[str, int] = {}\n",
    "new_text": "        self.lines: dict[str, int] = {}\n        self.coupon = None\n"})
CART_APPLY_EDIT = tool_call("edit_file", {
    "path": "shop/cart.py", "old_text": "    def total(self) -> int:\n",
    "new_text": ("    def apply_coupon(self, code: str) -> None:\n        discounts.coupon_percent(code)  # validates\n"
                 "        self.coupon = code\n\n    def total(self) -> int:\n")})
# First attempt has a bug: it returns the coupon *amount* instead of the discounted total.
CART_TOTAL_BUGGY = tool_call("edit_file", {
    "path": "shop/cart.py", "old_text": "        return total\n",
    "new_text": ("        if self.coupon:\n            total = total * discounts.coupon_percent(self.coupon) // 100\n"
                 "        return total\n")})
CART_TOTAL_FIX = tool_call("edit_file", {
    "path": "shop/cart.py",
    "old_text": "            total = total * discounts.coupon_percent(self.coupon) // 100\n",
    "new_text": "            total -= total * discounts.coupon_percent(self.coupon) // 100\n"})


def coupon_replies_with_one_fix() -> list:
    """Navigate, implement with a bug, finish -> failed check -> fix -> finish."""
    return [
        ai(tool_call("find_symbol", {"name": "total"}), tool_call("module_deps", {"path": "shop/cart.py"})),
        ai(tool_call("update_plan", {"items": [{"step": "add coupons to discounts.py", "status": "doing"},
                                               {"step": "apply coupon in Cart.total", "status": "todo"}]})),
        ai(DISCOUNTS_EDIT),
        ai(CART_INIT_EDIT, CART_APPLY_EDIT, CART_TOTAL_BUGGY),
        ai(tool_call("run_command", {"name": "tests"})),
        ai(tool_call("finish", {"summary": "coupons added"})),
        # -- harness feeds back the failed acceptance check --
        ai(CART_TOTAL_FIX),
        ai(tool_call("update_plan", {"items": [{"step": "add coupons to discounts.py", "status": "done"},
                                               {"step": "apply coupon in Cart.total", "status": "done"}]})),
        ai(tool_call("finish", {"summary": "fixed: subtract the coupon amount"})),
    ]
