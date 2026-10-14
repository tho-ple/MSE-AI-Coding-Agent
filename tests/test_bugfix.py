"""Bug fix end-to-end with a scripted model: acceptance fails on the starting code, passes after the change,
and the existing tests still pass."""
from coding_harness.app import run_checks_only, run_task
from coding_harness.llm import ScriptedModel, ai, tool_call


def test_acceptance_fails_on_starting_code(settings):
    checks = {c["name"]: c["status"] for c in run_checks_only(settings, run_id="base")}
    assert checks == {"acceptance": "failed", "regression": "passed"}


def test_scripted_bug_fix(settings):
    model = ScriptedModel([
        ai(tool_call("search", {"pattern": "BULK_THRESHOLD"})),
        ai(tool_call("read_file", {"path": "shop/discounts.py"})),
        ai(tool_call("edit_file", {"path": "shop/discounts.py",
                                   "old_text": "if qty > BULK_THRESHOLD:", "new_text": "if qty >= BULK_THRESHOLD:"})),
        ai(tool_call("write_file", {"path": "tests/test_threshold.py", "content":
            "from shop.cart import Cart\n\n\ndef test_threshold():\n    c = Cart()\n    c.add('APPLE', 10)\n    assert c.total() == 450\n"})),
        ai(tool_call("run_command", {"name": "tests"})),
        ai(tool_call("finish", {"summary": "Bulk discount now applies at exactly 10 items."})),
    ])
    result = run_task(settings, "Lines with 10 or more items must get the bulk discount.", model=model, run_id="fix")
    s = result.state
    assert result.outcome == "success", s["checks"]
    assert {c["name"]: c["status"] for c in s["checks"]} == {"acceptance": "passed", "regression": "passed"}
    assert sorted(s["changed_files"]) == ["A tests/test_threshold.py", "M shop/discounts.py"]
    assert "+    if qty >= BULK_THRESHOLD:" in s["diff"]
    assert (result.dirs.root / "diff.patch").read_text() == s["diff"]
