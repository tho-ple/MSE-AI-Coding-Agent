"""Optional: the full pipeline against the real Cosmic Python target with a scripted reference fix.

Skipped unless the target was set up with scripts/setup.* (needs network once).
Proves the acceptance check fails on the starting commit and passes after the change.
"""
import shutil
from pathlib import Path

import pytest

from coding_harness.app import run_checks_only, run_task
from coding_harness.config import load_settings
from coding_harness.llm import ScriptedModel, ai, tool_call
from tests.conftest import _force_remove

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "examples" / "cosmic" / "harness.yaml"

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


def test_acceptance_fails_on_starting_commit(settings):
    status = {c["name"]: c["status"] for c in run_checks_only(settings, run_id="base")}
    assert status["acceptance"] == "failed"
    assert all(v == "passed" for k, v in status.items() if k.startswith("regression"))


def test_reference_fix_passes(settings):
    model = ScriptedModel([
        ai(tool_call("search", {"pattern": "def change_batch_quantity", "path": "src"})),
        ai(tool_call("edit_file", {"path": "src/allocation/domain/model.py",
                                   "old_text": "class Product:",
                                   "new_text": "class InvalidQuantity(Exception):\n    pass\n\n\nclass Product:"})),
        ai(tool_call("edit_file", {"path": "src/allocation/domain/model.py",
                                   "old_text": "    def change_batch_quantity(self, ref: str, qty: int):\n",
                                   "new_text": "    def change_batch_quantity(self, ref: str, qty: int):\n"
                                               "        if qty < 0:\n"
                                               "            raise InvalidQuantity(f\"Invalid quantity {qty}\")\n"})),
        ai(tool_call("edit_file", {"path": "src/allocation/service_layer/handlers.py",
                                   "old_text": "class InvalidSku(Exception):\n    pass\n",
                                   "new_text": "class InvalidSku(Exception):\n    pass\n\n\nclass InvalidBatchRef(Exception):\n    pass\n"})),
        ai(tool_call("edit_file", {"path": "src/allocation/service_layer/handlers.py",
                                   "old_text": "        product = uow.products.get_by_batchref(batchref=cmd.ref)\n",
                                   "new_text": "        product = uow.products.get_by_batchref(batchref=cmd.ref)\n"
                                               "        if product is None:\n"
                                               "            raise InvalidBatchRef(f\"Invalid batch ref {cmd.ref}\")\n"})),
        ai(tool_call("run_command", {"name": "unit_tests"})),
        ai(tool_call("finish", {"summary": "Validate batch ref and quantity."})),
    ])
    result = run_task(settings, (ROOT / "examples/cosmic/task.md").read_text(), model=model, run_id="ref")
    s = result.state
    assert result.outcome == "success", s["checks"]
    assert sorted(s["changed_files"]) == ["M src/allocation/domain/model.py", "M src/allocation/service_layer/handlers.py"]


def test_writes_outside_task_scope_are_denied(settings):
    model = ScriptedModel([
        ai(tool_call("edit_file", {"path": "src/allocation/config.py", "old_text": "def", "new_text": "def"})),
        ai(tool_call("finish", {"summary": "x"})),
    ])
    result = run_task(settings, "x", model=model, run_id="scope")
    assert result.state["denied"] == 1


def test_demo_task_red_then_green(tmp_path):
    s = load_settings(ROOT / "examples" / "cosmic-demo" / "harness.yaml")
    s.sandbox.backend = "local"
    s.runs_dir = tmp_path / "runs"
    try:
        before = {c["name"]: c["status"] for c in run_checks_only(s, run_id="demo-base")}
        assert before["acceptance"] == "failed"
        model = ScriptedModel([
            ai(tool_call("edit_file", {"path": "src/allocation/service_layer/handlers.py",
                                       "old_text": "class InvalidSku(Exception):\n    pass\n",
                                       "new_text": "class InvalidSku(Exception):\n    pass\n\n\nclass InvalidBatchRef(Exception):\n    pass\n"})),
            ai(tool_call("edit_file", {"path": "src/allocation/service_layer/handlers.py",
                                       "old_text": "        product = uow.products.get_by_batchref(batchref=cmd.ref)\n",
                                       "new_text": "        product = uow.products.get_by_batchref(batchref=cmd.ref)\n"
                                                   "        if product is None:\n"
                                                   "            raise InvalidBatchRef(f\"Invalid batch ref {cmd.ref}\")\n"})),
            ai(tool_call("finish", {"summary": "Raise InvalidBatchRef for unknown batches."})),
        ])
        result = run_task(s, (ROOT / "examples/cosmic-demo/task.md").read_text(), model=model, run_id="demo")
        assert result.outcome == "success", result.state["checks"]
    finally:
        shutil.rmtree(s.runs_dir, onerror=_force_remove)
