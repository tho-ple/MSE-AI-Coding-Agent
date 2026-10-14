"""Execution: exit codes captured, credentials not inherited, stuck commands killed with their children."""
import sys
import time

from coding_harness.config import SandboxSettings
from coding_harness.sandbox import LocalSandbox, make_sandbox


def _sandbox(tmp_path):
    ws, checks = tmp_path / "ws", tmp_path / "checks"
    ws.mkdir()
    checks.mkdir()
    return LocalSandbox(SandboxSettings(python=sys.executable), ws, checks), ws


def test_captures_output_and_exit_code(tmp_path):
    sb, _ = _sandbox(tmp_path)
    res = sb.run(sb.render("{python} -c \"import sys; print('hi'); sys.exit(7)\""), 30)
    assert res.exit_code == 7 and "hi" in res.output and not res.ok


def test_secrets_are_not_inherited(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    monkeypatch.setenv("MY_TOKEN", "t0k3n")
    sb, _ = _sandbox(tmp_path)
    res = sb.run(sb.render("{python} -c \"import os; print(sorted(os.environ))\""), 30)
    assert res.exit_code == 0
    assert "OPENAI_API_KEY" not in res.output and "MY_TOKEN" not in res.output


def test_runs_in_workspace(tmp_path):
    sb, ws = _sandbox(tmp_path)
    res = sb.run(sb.render("{python} -c \"import os; print(os.getcwd())\""), 30)
    assert res.output.strip().lower() == str(ws).lower()


def test_timeout_kills_process_tree_and_no_late_writes(tmp_path):
    sb, ws = _sandbox(tmp_path)
    marker = ws / "late_write.txt"
    # Parent spawns a child that sleeps, then writes a file. Both must be killed.
    child = f"import time; time.sleep(4); open({str(marker)!r}, 'w').write('late')"
    parent = f"import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(60)"
    script = ws / "stuck.py"
    script.write_text(parent)
    start = time.monotonic()
    res = sb.run(sb.render("{python} stuck.py"), 2)
    assert res.timed_out and res.exit_code is None and not res.ok
    assert time.monotonic() - start < 20
    time.sleep(5)  # longer than the child's sleep
    assert not marker.exists(), "child process survived the timeout and wrote a file"


def test_unknown_backend_rejected(tmp_path):
    import pytest
    with pytest.raises(ValueError):
        make_sandbox(SandboxSettings(backend="yolo"), tmp_path, tmp_path)


def test_background_child_killed_when_command_exits(tmp_path):
    sb, ws = _sandbox(tmp_path)
    marker = ws / "bg_write.txt"
    child = f"import time; time.sleep(3); open({str(marker)!r}, 'w').write('late')"
    (ws / "spawn.py").write_text(
        f"import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(1)")
    res = sb.run(sb.render("{python} spawn.py"), 30)
    assert res.exit_code == 0
    time.sleep(4)
    assert not marker.exists(), "background child kept running after the command finished"
