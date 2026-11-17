"""Session storage: task details, steps, history, errors and check results survive an application restart."""
import json
import subprocess
import sys
from pathlib import Path

from langchain_core.messages import AIMessage, ToolMessage

from coding_harness.app import load_state, run_task, session_review
from coding_harness.llm import ScriptedModel
from coding_harness.session import Session, SessionStore
from tests.conftest import COUPON_TASK
from tests.scripted import coupon_replies_with_one_fix

ROOT = Path(__file__).resolve().parents[1]


def _run(feature_settings, run_id="store"):
    replies = [ConnectionError("ollama not reachable")] + coupon_replies_with_one_fix()
    return run_task(feature_settings, COUPON_TASK, model=ScriptedModel(replies), run_id=run_id)


def test_session_reopens_after_restart(feature_settings):
    result = _run(feature_settings)
    assert result.outcome == "success"
    sdir = result.session.root

    # A new Python process (= application restart) reads the saved session.
    code = ("import json, sys; from coding_harness.app import session_review; "
            "print(json.dumps(session_review(sys.argv[1]), default=str))")
    out = subprocess.run([sys.executable, "-c", code, str(sdir)], capture_output=True, text=True, cwd=ROOT, check=True)
    r = json.loads(out.stdout)

    # Task details
    assert r["task"]["request"] == COUPON_TASK
    assert r["task"]["allowed_changes"] == ["shop/*.py", "tests/*.py"]
    assert [c["name"] for c in r["task"]["checks"]] == ["acceptance (feature)", "regression"]
    assert r["task"]["acceptance_criteria"] and r["task"]["workspace_baseline"]
    assert r["status"] == "completed" and r["outcome"] == "success"
    # Steps
    done = [s for s in r["steps"] if s["type"] == "action_done"]
    assert [s["tool"] for s in done][:3] == ["find_symbol", "module_deps", "update_plan"]
    assert any(s["type"] == "fix_round" for s in r["steps"])
    # Errors (the first model request failed)
    assert any("ollama not reachable" in e for e in r["errors"])
    # Check results + history
    assert {c["name"]: c["status"] for c in r["checks"]} == {"acceptance (feature)": "passed", "regression": "passed"}
    assert [h["round"] for h in r["check_history"]] == [0, 1]
    assert r["progress"]["fix_round"] == 1 and r["progress"]["retries"] == 1
    assert "M shop/cart.py" in r["changed_files"] and "+class InvalidCoupon" in r["diff"]
    assert r["unfinished"] == []


def test_history_round_trips(feature_settings):
    result = _run(feature_settings, "hist")
    state = load_state(Session.load(result.session.root))
    assert len(state["messages"]) == len(result.state["messages"])
    ai_msgs = [m for m in state["messages"] if isinstance(m, AIMessage)]
    assert ai_msgs[0].tool_calls[0]["name"] == "find_symbol"
    tools = [m for m in state["messages"] if isinstance(m, ToolMessage)]
    assert tools[0].tool_call_id == ai_msgs[0].tool_calls[0]["id"]
    assert any((m.additional_kwargs or {}).get("harness_kind") == "fix_feedback" for m in state["messages"])
    assert state["actions"] == result.state["actions"] and state["last_node"] == "report"


def test_store_lists_and_finds_sessions(feature_settings):
    _run(feature_settings, "listme-1")
    store = SessionStore(feature_settings.runs_dir)
    rows = store.list()
    assert [r["id"] for r in rows] == ["listme-1"] and rows[0]["status"] == "completed"
    assert store.find("listme").name == "listme-1"  # unique prefix
    journal = Session.load(store.find("listme-1")).read_journal()
    assert journal[0]["type"] == "task_saved" and journal[-1]["type"] == "run_end"
    assert [r["seq"] for r in journal] == list(range(1, len(journal) + 1))


def test_journal_seq_unique_across_writers(tmp_path):
    """The running process and `harness cancel` (another process) both append to the journal."""
    a = Session.create(tmp_path / "s", {"id": "s"})
    b = Session.load(tmp_path / "s")  # e.g. the cancel command's view of the same session
    a.journal("x")
    b.journal("cancel_requested")
    a.journal("cancelled", payload="y" * 100_000)  # large record: tail read must cope
    b.journal("z")
    assert [r["seq"] for r in a.read_journal()] == [1, 2, 3, 4]
