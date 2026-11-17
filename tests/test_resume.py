"""Safe resume: continue from the saved working copy (incl. the harness's edits), recheck permissions and
limits, and pause on mismatches or uncertain interrupted actions. Uncertain actions are never replayed."""
import os

import pytest
from langchain_core.messages import ToolMessage

from coding_harness.app import resume_session, run_task, settings_for_session
from coding_harness.config import load_projects
from coding_harness.llm import ScriptedModel, ai, tool_call
from coding_harness.session import CancelToken, Session
from coding_harness.tools import ToolRegistry
from tests.conftest import COUPON_TASK
from tests.scripted import DISCOUNTS_EDIT, coupon_replies_with_one_fix

FULL = coupon_replies_with_one_fix()
CUT = 3  # replies 0..2 (navigation, plan, discounts edit) run before the cancel


def _cancel_after_discounts_edit(feature_settings, run_id="res"):
    """Run the coupon feature; the user cancels right after the 4th model reply (before its edits run)."""
    token = CancelToken()

    def cancel_then_reply(messages):
        token.set()
        return FULL[CUT]

    replies = FULL[:CUT] + [cancel_then_reply]
    return run_task(feature_settings, COUPON_TASK, model=ScriptedModel(replies), run_id=run_id, cancel=token)


def test_cancel_then_resume_completes_with_preserved_counters(feature_settings):
    first = _cancel_after_discounts_edit(feature_settings)
    assert first.status == "cancelled"
    s = Session.load(first.session.root)
    assert s.data["state"]["actions"] == 4 and s.data["state"]["last_node"] == "agent"
    assert "COUPONS" in (first.dirs.workspace / "shop" / "discounts.py").read_text()  # harness edit is saved

    model = ScriptedModel(FULL[CUT + 1:])
    events = []
    result = resume_session(first.session.root, feature_settings, model=model, on_event=events.append)
    assert result.status == "completed" and result.outcome == "success", result.state["checks"]
    # The pending reply's three edits ran on resume (they had never started); nothing was redone twice.
    assert result.state["actions"] == 12  # identical to an uninterrupted run
    assert result.state["model_calls"] == 9
    text = (result.dirs.workspace / "shop" / "discounts.py").read_text()
    assert text.count("COUPONS = ") == 1
    assert any(e["kind"] == "resumed" for e in events)
    journal = Session.load(first.session.root).read_journal()
    assert [r["type"] for r in journal].count("resume") == 1


def test_consumed_limits_are_preserved_on_resume(feature_settings):
    feature_settings.limits.max_actions = 6
    first = _cancel_after_discounts_edit(feature_settings, "lim")
    assert first.state["actions"] == 4
    feature_settings.limits.max_actions = 100  # config changed after the cancel: the saved limit still applies
    result = resume_session(first.session.root, feature_settings, model=ScriptedModel(FULL[CUT + 1:]))
    assert result.state["stop_reason"] == "action limit reached (6 actions)"
    assert result.state["actions"] == 6 and result.outcome == "stopped"


def test_workspace_mismatch_pauses_until_accepted(feature_settings):
    first = _cancel_after_discounts_edit(feature_settings, "mm")
    cart = first.dirs.workspace / "shop" / "cart.py"
    cart.write_text(cart.read_text() + "\n# edited by hand\n")
    paused = resume_session(first.session.root, feature_settings, model=ScriptedModel(FULL[CUT + 1:]))
    assert paused.status == "paused"
    assert "working copy differs from the saved state: modified shop/cart.py" in paused.pause_reason
    assert Session.load(first.session.root).data["status"] == "paused"
    # The hand edit is appended, so the scripted edits still apply once the user accepts the working copy.
    result = resume_session(first.session.root, feature_settings, model=ScriptedModel(FULL[CUT + 1:]),
                            accept_workspace=True)
    assert result.status == "completed" and result.outcome == "success"
    journal = Session.load(first.session.root).read_journal()
    assert any(r["type"] == "workspace_accepted" and "modified shop/cart.py" in r["changes"] for r in journal)


class SimulatedCrash(BaseException):
    """The process dies in the middle of an action (not an Exception: nothing catches it)."""


def test_uncertain_interrupted_action_is_never_replayed(feature_settings, monkeypatch):
    real_edit = ToolRegistry._edit_file

    def crash_after_write(self, a):
        real_edit(self, a)  # the write happens ...
        raise SimulatedCrash()  # ... but the process dies before the harness records it

    monkeypatch.setattr(ToolRegistry, "_edit_file", crash_after_write)
    with pytest.raises(SimulatedCrash):
        run_task(feature_settings, COUPON_TASK, model=ScriptedModel(FULL[:CUT]), run_id="crash")
    monkeypatch.undo()
    sdir = feature_settings.runs_dir / "crash"
    session = Session.load(sdir)
    assert session.data["status"] == "running"  # no final status was saved
    (sdir / "lock").write_text("999999")  # stale lock of the dead process
    [uncertain] = session.uncertain_actions()
    assert uncertain["tool"] == "edit_file"

    calls = []
    monkeypatch.setattr(ToolRegistry, "_edit_file", lambda self, a: calls.append(a) or real_edit(self, a))
    paused = resume_session(sdir, feature_settings, model=ScriptedModel(FULL[CUT:]))
    assert paused.status == "paused" and calls == []
    assert "uncertain interrupted action #4 edit_file" in paused.pause_reason
    assert "modified shop/discounts.py" in paused.pause_reason  # the uncertain write is visible as a mismatch

    still = resume_session(sdir, feature_settings, model=ScriptedModel(FULL[CUT:]), skip_uncertain=True)
    assert still.status == "paused" and "uncertain" not in still.pause_reason  # decision recorded, mismatch remains

    model = ScriptedModel([ai(tool_call("git_diff", {"path": "shop/discounts.py"}))] + FULL[CUT:])
    result = resume_session(sdir, feature_settings, model=model, accept_workspace=True)
    assert result.status == "completed" and result.outcome == "success"
    first_view = model.calls[0]
    interrupted = [m for m in first_view if isinstance(m, ToolMessage) and "INTERRUPTED" in m.content]
    assert interrupted and "NOT replayed" in interrupted[0].content
    assert not any(c.path == "shop/discounts.py" and "COUPONS" in c.new_text for c in calls)  # never replayed
    assert (sdir / "workspace" / "shop" / "discounts.py").read_text().count("COUPONS = ") == 1


def test_permission_changes_pause_the_session(feature_settings):
    first = _cancel_after_discounts_edit(feature_settings, "perm")
    feature_settings.target.writable = ["tests/*.py"]
    paused = resume_session(first.session.root, feature_settings, model=ScriptedModel(FULL[CUT + 1:]),
                            accept_workspace=True, skip_uncertain=True)  # cannot be overridden
    assert paused.status == "paused"
    assert "permissions/config changed since the session was saved: allowed_changes" in paused.pause_reason
    assert "changed files outside the allowed changes: shop/discounts.py" in paused.pause_reason

    feature_settings.target.writable = ["shop/*.py", "tests/*.py"]
    feature_settings.commands["extra"] = "{python} -c \"print(1)\""
    paused = resume_session(first.session.root, feature_settings, model=ScriptedModel(FULL[CUT + 1:]))
    assert paused.status == "paused" and "commands" in paused.pause_reason


def test_project_must_still_be_allowed(feature_settings, tmp_path):
    first = _cancel_after_discounts_edit(feature_settings, "proj")
    s = Session.load(first.session.root)
    s.save(project="shop-demo")
    reg = tmp_path / "projects.yaml"
    reg.write_text("sessions_dir: runs\nprojects: {}\n")
    with pytest.raises(KeyError, match="not in the allowed projects"):
        settings_for_session(s, load_projects(reg))


def test_live_or_completed_sessions_are_not_resumed(feature_settings):
    first = _cancel_after_discounts_edit(feature_settings, "live")
    (first.session.root / "lock").write_text(str(os.getppid()))  # another live process owns it
    r = resume_session(first.session.root, feature_settings, model=ScriptedModel(FULL[CUT + 1:]))
    assert r.status == "paused" and "still being run by process" in r.pause_reason
    (first.session.root / "lock").unlink()
    done = resume_session(first.session.root, feature_settings, model=ScriptedModel(FULL[CUT + 1:]))
    assert done.outcome == "success"
    again = resume_session(first.session.root, feature_settings, model=ScriptedModel([]))
    assert "already completed" in again.pause_reason
