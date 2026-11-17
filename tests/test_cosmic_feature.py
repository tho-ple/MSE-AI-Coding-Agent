"""Stage 2 feature on the real Cosmic Python target, with a scripted reference implementation.

Skipped unless the target was set up (scripts/setup.*). Proves: the protected feature check fails on the
starting commit for the expected reason; after a change across commands.py, model.py and handlers.py it
passes, and the regression, integration, lint and build checks still pass. A first attempt with a bug
is fixed in the edit-test-fix loop.
"""
import shutil
from pathlib import Path

import pytest

from coding_harness.app import run_checks_only, run_task
from coding_harness.config import load_settings
from coding_harness import demo
from coding_harness.llm import ScriptedModel
from tests.conftest import _force_remove

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "examples" / "cosmic-feature" / "harness.yaml"
TASK = (ROOT / "examples" / "cosmic-feature" / "task.md").read_text(encoding="utf-8")

pytestmark = pytest.mark.skipif(
    not (ROOT / "targets" / "cosmicpython" / ".git").exists() or not (ROOT / "targets" / ".venv-cosmic").exists(),
    reason="Cosmic Python target not set up (run scripts/setup.ps1 or scripts/setup.sh)",
)


@pytest.fixture
def settings(tmp_path):
    s = load_settings(CONFIG)
    s.sandbox.backend = "local"
    s.runs_dir = tmp_path / "runs"
    yield s
    shutil.rmtree(s.runs_dir, onerror=_force_remove)


COMMANDS, MODEL, HANDLERS = demo.COMMANDS, demo.MODEL, demo.HANDLERS
REFERENCE = demo.REFERENCE_REPLIES  # the same reference solution `harness demo` replays


def test_feature_check_fails_on_starting_commit_for_expected_reason(settings):
    checks = {c["name"]: c for c in run_checks_only(settings, run_id="fbase")}
    acc = checks["acceptance (feature)"]
    assert acc["status"] == "failed" and acc["expectation"].startswith("met"), acc.get("expectation")
    for name, c in checks.items():
        if name.startswith("regression") or name.startswith("build"):
            assert c["status"] == "passed", (name, c["output"])
    assert checks["lint (new pyflakes warnings)"]["status"] in ("skipped", "unavailable")


def test_reference_feature_with_one_fix_round(settings):
    result = run_task(settings, TASK, model=ScriptedModel(REFERENCE), run_id="feat")
    s = result.state
    assert result.outcome == "success", [(c["name"], c["status"], c["output"][-800:]) for c in s["checks"]]
    assert s["fix_round"] == 1
    assert sorted(s["changed_files"]) == [f"M {COMMANDS}", f"M {MODEL}", f"M {HANDLERS}"]
    statuses = {c["name"]: c["status"] for c in s["checks"]}
    lint = statuses.pop("lint (new pyflakes warnings)")
    assert lint == "passed", "pyflakes missing in the target env? run scripts/setup.*"
    assert all(v == "passed" for v in statuses.values()), statuses
    round0 = {c["name"]: c["status"] for c in s["check_history"][0]["checks"]}
    assert round0["acceptance (feature)"] == "failed"


def test_demo_cancel_then_resume_continues_the_script(settings):
    """`harness demo`: Ctrl+C mid-run, then `harness resume` with a NEW DemoModel continues at the right turn."""
    from coding_harness.app import resume_session
    from coding_harness.session import CancelToken

    token = CancelToken()

    class CancelAtTurn4(demo.DemoModel):
        def invoke(self, messages):
            reply = super().invoke(messages)
            if sum(1 for m in messages if type(m).__name__ == "AIMessage") == 3:
                token.set()  # user presses Ctrl+C while the 4th model request is running
            return reply

    first = run_task(settings, TASK, model=CancelAtTurn4(), run_id="demo-cancel", cancel=token)
    assert first.status == "cancelled"
    assert "Deallocate" in (first.dirs.workspace / COMMANDS).read_text()   # earlier edit saved
    assert "NotAllocated" not in (first.dirs.workspace / MODEL).read_text()  # discarded reply never ran

    result = resume_session(first.session.root, settings, model=demo.DemoModel())
    assert result.status == "completed" and result.outcome == "success", result.state["checks"]
    assert result.state["fix_round"] == 1
