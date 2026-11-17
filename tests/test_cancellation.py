"""Cancellation during a command and during a model request: no further actions or writes, no child processes."""
import subprocess
import threading
import time

import psutil

from coding_harness.app import cancel_session, run_task
from coding_harness.llm import ScriptedModel, ai, tool_call
from coding_harness.session import CancelToken, Session


def _children_alive() -> list[psutil.Process]:
    return [p for p in psutil.Process().children(recursive=True) if p.is_running()]


def test_cancel_during_command_kills_process_tree(settings, tmp_path):
    marker = tmp_path / "late_write.txt"
    child = f"import time; time.sleep(3); open({str(marker)!r}, 'w').write('late')"
    script = tmp_path / "stuck.py"
    script.write_text(f"import subprocess, sys, time\nsubprocess.Popen([sys.executable, '-c', {child!r}])\ntime.sleep(60)\n")
    settings.commands["slow"] = "{python} " + subprocess.list2cmdline([str(script)])
    model = ScriptedModel([
        ai(tool_call("run_command", {"name": "slow"})),
        ai(tool_call("write_file", {"path": "shop/after_cancel.py", "content": "x = 1\n"})),
        ai(tool_call("finish", {"summary": "done"})),
    ])
    sdir = settings.runs_dir / "cancel-cmd"
    # Like `harness cancel <id>` from another terminal: writes the session's cancel flag.
    timer = threading.Timer(1.5, lambda: cancel_session(sdir))
    timer.start()
    start = time.monotonic()
    result = run_task(settings, "run the slow command", model=model, run_id="cancel-cmd")
    timer.join()
    assert time.monotonic() - start < 15
    assert result.status == "cancelled"
    assert len(model.calls) == 1  # no model request after the cancel
    time.sleep(4)  # longer than the child's sleep
    assert not marker.exists(), "child process survived the cancel and wrote a file"
    assert not (result.dirs.workspace / "shop" / "after_cancel.py").exists()
    assert _children_alive() == []
    session = Session.load(sdir)
    journal = session.read_journal()
    done = [r for r in journal if r["type"] == "action_done"]
    assert len(done) == 1 and done[0]["cancelled"] and "CANCELLED by user" in done[0]["content"]
    assert any(r["type"] == "cancelled" for r in journal)
    assert session.data["status"] == "cancelled" and session.uncertain_actions() == []
    assert not result.state.get("checks")  # no checks run after cancel
    assert session.lock_owner() is None and not session.cancel_flag.exists()


def test_cancel_during_model_request_discards_reply(settings):
    token = CancelToken()
    target = settings.runs_dir / "cancel-model" / "workspace" / "shop" / "from_late_reply.py"

    started = []

    def slow_reply(messages):
        started.append(time.monotonic())
        threading.Timer(0.3, token.set).start()  # user cancels while the request is in flight
        time.sleep(3)  # a long model request
        return ai(tool_call("write_file", {"path": "shop/from_late_reply.py", "content": "x = 1\n"}))

    model = ScriptedModel([slow_reply, ai(tool_call("finish", {"summary": "x"}))])
    result = run_task(settings, "x", model=model, run_id="cancel-model", cancel=token)
    assert time.monotonic() - started[0] < 2.5  # returned without waiting for the reply
    assert result.status == "cancelled"
    assert not target.exists()
    time.sleep(3.5)  # the abandoned request finishes in the background ...
    assert not target.exists()  # ... but its tool call is never executed
    journal = Session.load(result.session.root).read_journal()
    assert not any(r["type"] == "action_start" for r in journal)
    assert result.state["actions"] == 0 and len(model.calls) == 1


def test_cancel_when_not_running_marks_session(settings):
    result = run_task(settings, "x", model=ScriptedModel([ai(tool_call("finish", {"summary": "s"}))]), run_id="done")
    assert "already completed" in cancel_session(result.session.root)
