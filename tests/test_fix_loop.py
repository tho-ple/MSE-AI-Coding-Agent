"""Edit-test-fix loop: failed final checks guide fixes; failed or unavailable checks never appear as passed."""
from langchain_core.messages import HumanMessage

from coding_harness.app import run_checks_only, run_task
from coding_harness.config import CheckSpec
from coding_harness.context import UNTRUSTED_OPEN
from coding_harness.llm import ScriptedModel, ai, tool_call
from tests.conftest import COUPON_TASK
from tests.scripted import coupon_replies_with_one_fix


def test_feature_check_fails_on_starting_code_for_expected_reason(feature_settings):
    checks = {c["name"]: c for c in run_checks_only(feature_settings, run_id="fbase")}
    assert checks["acceptance (feature)"]["status"] == "failed"
    assert checks["acceptance (feature)"]["expectation"].startswith("met")
    assert "apply_coupon" in checks["acceptance (feature)"]["output"]
    assert checks["regression"]["status"] == "passed"


def test_failed_check_is_fed_back_and_fixed(feature_settings):
    model = ScriptedModel(coupon_replies_with_one_fix())
    events = []
    result = run_task(feature_settings, COUPON_TASK, model=model, run_id="fix1", on_event=events.append)
    s = result.state
    assert result.outcome == "success", s["checks"]
    assert s["fix_round"] == 1
    assert s["end_reason"] == "all checks passed"
    # Round 0 failed (and was recorded as failed), round 1 passed.
    hist = s["check_history"]
    assert [h["round"] for h in hist] == [0, 1]
    assert {c["name"]: c["status"] for c in hist[0]["checks"]}["acceptance (feature)"] == "failed"
    assert all(c["status"] == "passed" for c in hist[1]["checks"])
    # The model saw the failure output, marked as untrusted, in a trusted harness message.
    feedback = [m for m in model.calls[6] if isinstance(m, HumanMessage) and "fix round 1" in m.content]
    assert feedback and "acceptance (feature)" in feedback[0].content
    assert UNTRUSTED_OPEN.split("{")[0] in feedback[0].content and "assert" in feedback[0].content
    assert any(e["kind"] == "fix_round" for e in events)
    # Both interacting modules changed; existing callers still work (regression passed).
    assert sorted(s["changed_files"]) == ["M shop/cart.py", "M shop/discounts.py"]
    assert all(p["status"] == "done" for p in s["plan"])


def test_fix_rounds_exhausted_reports_failure(feature_settings):
    feature_settings.limits.max_fix_rounds = 1
    replies = coupon_replies_with_one_fix()[:6] + [
        ai(tool_call("write_file", {"path": "shop/notes.py", "content": "# still wrong\n"})),
        ai(tool_call("finish", {"summary": "I am sure it works now"})),
    ]
    result = run_task(feature_settings, COUPON_TASK, model=ScriptedModel(replies), run_id="fix2")
    s = result.state
    assert result.outcome == "checks_failed"
    assert "fix rounds exhausted (1)" in s["end_reason"]
    assert {c["name"]: c["status"] for c in s["checks"]}["acceptance (feature)"] == "failed"
    assert len(s["check_history"]) == 2


def test_no_progress_stops_the_loop(feature_settings):
    feature_settings.limits.max_fix_rounds = 3
    replies = [ai(tool_call("finish", {"summary": "done"})), ai(tool_call("finish", {"summary": "done!"}))]
    result = run_task(feature_settings, COUPON_TASK, model=ScriptedModel(replies), run_id="noprog")
    s = result.state
    assert result.outcome == "checks_failed"
    assert s["fix_round"] == 1 and s["end_reason"].startswith("no progress")


def test_unavailable_check_is_never_passed_and_not_looped(feature_settings):
    feature_settings.checks.append(CheckSpec("lint", "{python} -c \"print('lint')\"", kind="lint",
                                             probe="{python} -c \"import no_such_linter_xyz\""))
    replies = coupon_replies_with_one_fix()[:6]
    result = run_task(feature_settings, COUPON_TASK, model=ScriptedModel(replies), run_id="unav")
    s = result.state
    lint = next(c for c in s["checks"] if c["name"] == "lint")
    assert lint["status"] == "unavailable" and "probe failed" in lint["output"]
    assert result.outcome == "checks_failed"  # never success while a check is unavailable
    # Unavailable checks cannot be fixed by editing code: no fix round is started.
    assert s["fix_round"] == 0 and s["end_reason"].startswith("check unavailable (lint)")


def test_skipped_lint_without_changes_is_not_success(feature_settings):
    feature_settings.checks = [CheckSpec("lint", "{python} -m pyflakes {changed_py}", kind="lint")]
    result = run_task(feature_settings, "x", model=ScriptedModel([ai(tool_call("finish", {"summary": "s"}))]),
                      run_id="skip")
    assert result.state["checks"][0]["status"] == "skipped"
    assert result.outcome == "unverified"
