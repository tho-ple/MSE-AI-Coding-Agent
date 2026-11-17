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
from coding_harness.llm import ScriptedModel, ai, tool_call
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


def _edit(path, old, new):
    return tool_call("edit_file", {"path": path, "old_text": old, "new_text": new})


COMMANDS = "src/allocation/domain/commands.py"
MODEL = "src/allocation/domain/model.py"
HANDLERS = "src/allocation/service_layer/handlers.py"

REFERENCE = [
    ai(tool_call("find_symbol", {"name": "Allocate"}), tool_call("find_references", {"name": "COMMAND_HANDLERS"})),
    ai(tool_call("module_deps", {"path": HANDLERS})),
    ai(_edit(COMMANDS, "@dataclass\nclass CreateBatch(Command):",
             "@dataclass\nclass Deallocate(Command):\n    orderid: str\n    sku: str\n    qty: int\n\n\n"
             "@dataclass\nclass CreateBatch(Command):")),
    ai(_edit(MODEL, "class Product:\n", "class NotAllocated(Exception):\n    pass\n\n\nclass Product:\n"),
       _edit(MODEL, "    def change_batch_quantity(self, ref: str, qty: int):\n",
             "    def deallocate(self, line: OrderLine) -> str:\n"
             "        batch = next((b for b in self.batches if b.has(line)), None)\n"
             "        if batch is None:\n"
             "            raise NotAllocated(f\"Order {line.orderid} is not allocated for {line.sku}\")\n"
             "        batch.deallocate(line)\n"
             "        self.version_number += 1\n"
             "        return batch.reference\n\n"
             "    def change_batch_quantity(self, ref: str, qty: int):\n"),
       # Bug in the first attempt: Batch.has / Batch.deallocate are missing.
       ),
    ai(_edit(HANDLERS, "def reallocate(\n",
             "def deallocate(\n    cmd: commands.Deallocate,\n    uow: unit_of_work.AbstractUnitOfWork,\n):\n"
             "    line = OrderLine(cmd.orderid, cmd.sku, cmd.qty)\n    with uow:\n"
             "        product = uow.products.get(sku=line.sku)\n        if product is None:\n"
             "            raise InvalidSku(f\"Invalid sku {line.sku}\")\n"
             "        product.deallocate(line)\n        uow.commit()\n\n\ndef reallocate(\n"),
       _edit(HANDLERS, "    commands.ChangeBatchQuantity: change_batch_quantity,\n",
             "    commands.ChangeBatchQuantity: change_batch_quantity,\n    commands.Deallocate: deallocate,\n")),
    ai(tool_call("finish", {"summary": "Deallocate command, domain method and handler added."})),
    # -- fix round 1: acceptance fails with AttributeError 'Batch' object has no attribute 'has' --
    ai(_edit(MODEL, "    def deallocate_one(self) -> OrderLine:\n",
             "    def has(self, line: OrderLine) -> bool:\n        return line in self._allocations\n\n"
             "    def deallocate(self, line: OrderLine):\n        if line in self._allocations:\n"
             "            self._allocations.remove(line)\n\n"
             "    def deallocate_one(self) -> OrderLine:\n")),
    ai(tool_call("run_command", {"name": "unit_tests"})),
    ai(tool_call("finish", {"summary": "Added Batch.has / Batch.deallocate."})),
]


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
