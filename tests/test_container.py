"""Contained execution with a real container engine (Docker/Podman, native or in WSL).

Skipped unless an engine and the sandbox image are available (scripts/setup.* builds it).
"""
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from coding_harness.app import run_task
from coding_harness.config import load_settings
from coding_harness.llm import ScriptedModel, ai, tool_call
from coding_harness.sandbox import ContainerEngine, DockerSandbox, resolve_engine
from tests.conftest import _force_remove

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "examples" / "cosmic-demo"


def _image_available() -> bool:
    if not (ROOT / "targets" / "cosmicpython" / ".git").exists():
        return False
    engine = resolve_engine("auto")
    if engine is None:
        return False
    image = load_settings(DEMO / "harness.yaml").sandbox.docker_image
    return subprocess.run(engine.prefix + ["image", "inspect", image], capture_output=True).returncode == 0


pytestmark = pytest.mark.skipif(not _image_available(), reason="no container engine / sandbox image available")


@pytest.fixture
def settings(tmp_path):
    s = load_settings(DEMO / "harness.yaml")
    s.sandbox.backend = "docker"
    s.runs_dir = tmp_path / "runs"
    yield s
    if s.runs_dir.exists():
        shutil.rmtree(s.runs_dir, onerror=_force_remove)


@pytest.fixture
def sandbox(settings, tmp_path):
    ws, checks = tmp_path / "ws", tmp_path / "checks"
    ws.mkdir()
    checks.mkdir()
    (checks / "marker.txt").write_text("read-only")
    return DockerSandbox(settings.sandbox, ws, checks), ws


def test_wsl_path_translation():
    eng = ContainerEngine("podman", ["wsl.exe", "-e", "podman"], wsl=True)
    assert eng.host_path(Path("D:/Dev/a b/c")) == "/mnt/d/Dev/a b/c"


def test_network_is_blocked(sandbox):
    sb, _ = sandbox
    res = sb.run("python -c \"import urllib.request; urllib.request.urlopen('http://example.com', timeout=3)\"", 60)
    assert res.exit_code not in (0, None) and res.error is None


def test_only_workspace_is_writable(sandbox):
    sb, ws = sandbox
    res = sb.run("echo hi > /workspace/ok.txt && (echo x > /checks/marker.txt || echo CHECKS_RO) "
                 "&& (echo x > /etc/evil 2>/dev/null || echo ROOT_RO)", 60)
    assert res.exit_code == 0, res.output
    assert "CHECKS_RO" in res.output and "ROOT_RO" in res.output
    assert (ws / "ok.txt").read_text().strip() == "hi"


def test_stuck_command_is_killed(sandbox):
    sb, ws = sandbox
    start = time.monotonic()
    res = sb.run("sleep 4 && echo late > /workspace/late.txt", 2)
    assert res.timed_out
    assert time.monotonic() - start < 30
    time.sleep(4)
    assert not (ws / "late.txt").exists()


def test_demo_red_to_green_in_container(settings):
    model = ScriptedModel([
        ai(tool_call("edit_file", {"path": "src/allocation/service_layer/handlers.py",
                                   "old_text": "class InvalidSku(Exception):\n    pass\n",
                                   "new_text": "class InvalidSku(Exception):\n    pass\n\n\nclass InvalidBatchRef(Exception):\n    pass\n"})),
        ai(tool_call("edit_file", {"path": "src/allocation/service_layer/handlers.py",
                                   "old_text": "        product = uow.products.get_by_batchref(batchref=cmd.ref)\n",
                                   "new_text": "        product = uow.products.get_by_batchref(batchref=cmd.ref)\n"
                                               "        if product is None:\n"
                                               "            raise InvalidBatchRef(f\"Invalid batch ref {cmd.ref}\")\n"})),
        ai(tool_call("run_command", {"name": "unit_tests"})),
        ai(tool_call("finish", {"summary": "done"})),
    ])
    result = run_task(settings, (DEMO / "task.md").read_text(), model=model, run_id="ctr")
    assert result.sandbox_contained
    assert result.outcome == "success", result.state["checks"]
