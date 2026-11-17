"""CLI: select an allowed project, inspect, cancel, resume and review sessions (interface over app.py)."""
import pytest

from coding_harness import cli
from coding_harness.app import run_task
from coding_harness.llm import ScriptedModel, ai, tool_call
from coding_harness.session import CancelToken
from tests.conftest import COUPON_TASK
from tests.scripted import coupon_replies_with_one_fix


@pytest.fixture
def wide(monkeypatch):
    monkeypatch.setattr(cli, "console", cli.Console(width=200, force_terminal=False, color_system=None))


def _out(capsys):
    return capsys.readouterr().out


def test_projects_lists_only_allowed(tmp_path, capsys, wide):
    reg = tmp_path / "projects.yaml"
    reg.write_text("sessions_dir: runs\nprojects:\n  shop:\n    config: shop.yaml\n    description: sample shop\n")
    assert cli.main(["--projects", str(reg), "projects"]) == 0
    assert "shop" in _out(capsys)
    assert cli.main(["--projects", str(reg), "start", "--project", "other", "--task", "x"]) == 2
    assert "not in the allowed projects" in _out(capsys)


def test_status_review_cancel_resume_flow(feature_settings, capsys, wide):
    token = CancelToken()
    replies = coupon_replies_with_one_fix()

    def cancel_then_reply(messages):
        token.set()
        return replies[3]

    run_task(feature_settings, COUPON_TASK, model=ScriptedModel(replies[:3] + [cancel_then_reply]),
             run_id="cli-1", cancel=token)
    sd = ["--sessions-dir", str(feature_settings.runs_dir)]

    assert cli.main(["sessions", *sd]) == 0
    assert "cli-1" in _out(capsys)

    assert cli.main(["status", "cli-1", *sd]) == 0
    out = _out(capsys)
    assert "cancelled" in out and "actions 4/20" in out and "fix rounds 0/2" in out
    assert "add coupons to discounts.py" in out  # the model's plan is shown as progress

    assert cli.main(["review", "cli-1", *sd]) == 1  # not finished yet
    out = _out(capsys)
    assert "M shop/discounts.py" in out and "+class InvalidCoupon" in out
    assert "cancelled by user - resume to continue" in out and "plan step (todo)" in out

    assert "already cancelled" in (cli.cancel_session(feature_settings.runs_dir / "cli-1"))


def test_review_shows_failed_checks_as_failed(feature_settings, capsys, wide):
    run_task(feature_settings, COUPON_TASK, model=ScriptedModel([ai(tool_call("finish", {"summary": "done"}))]),
             run_id="cli-2")
    assert cli.main(["review", "cli-2", "--sessions-dir", str(feature_settings.runs_dir)]) == 1
    out = _out(capsys)
    assert "FAILED" in out and "check acceptance (feature): FAILED" in out
    assert "PASSED" in out  # regression still passes and is shown
