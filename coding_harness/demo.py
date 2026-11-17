"""A model-free demo of the Stage 2 feature task (``harness demo``).

``DemoModel`` replays a pre-written reference solution for the ``cosmic-feature`` project: it
navigates, edits ``commands.py``, ``model.py`` and ``handlers.py``, calls ``finish`` with a bug
(``Batch.has`` / ``Batch.deallocate`` missing), receives the failed acceptance check from the harness,
fixes it and finishes again. Everything else is the real harness: tools, permissions, sandbox,
checks, sessions, cancellation and resume.

The reply is chosen by the number of assistant turns already in the conversation, not by a call
counter, so a demo run that was cancelled (Ctrl+C / ``harness cancel``) continues correctly after
``harness resume``. The demo history stays far below the context limit, so no turn is compacted away.

Used by ``tests/test_cosmic_feature.py`` as the scripted reference implementation as well.
"""

from __future__ import annotations

import time
import uuid
from typing import Sequence

from langchain_core.messages import AIMessage, BaseMessage

from .llm import ChatModel, ai, tool_call

DEMO_PROJECT = "cosmic-feature"
DEMO_MODEL_LABEL = "DemoModel"

COMMANDS = "src/allocation/domain/commands.py"
MODEL = "src/allocation/domain/model.py"
HANDLERS = "src/allocation/service_layer/handlers.py"


def _edit(path: str, old: str, new: str) -> dict:
    return tool_call("edit_file", {"path": path, "old_text": old, "new_text": new})


def _plan(*statuses: str) -> dict:
    steps = ["add Deallocate command (commands.py)", "Product.deallocate + NotAllocated (model.py)",
             "deallocate handler + COMMAND_HANDLERS (handlers.py)"]
    return tool_call("update_plan", {"items": [{"step": s, "status": st} for s, st in zip(steps, statuses)]})


REFERENCE_REPLIES: list[AIMessage] = [
    # 1. navigation: where are commands and handlers defined, what does the handler module import?
    ai(tool_call("find_symbol", {"name": "Allocate"}), tool_call("find_references", {"name": "COMMAND_HANDLERS"})),
    ai(tool_call("module_deps", {"path": HANDLERS}), _plan("doing", "todo", "todo")),
    # 2. edits across three modules
    ai(_edit(COMMANDS, "@dataclass\nclass CreateBatch(Command):",
             "@dataclass\nclass Deallocate(Command):\n    orderid: str\n    sku: str\n    qty: int\n\n\n"
             "@dataclass\nclass CreateBatch(Command):")),
    ai(_edit(MODEL, "class Product:\n", "class NotAllocated(Exception):\n    pass\n\n\nclass Product:\n"),
       # Bug in the first attempt: Batch.has / Batch.deallocate do not exist.
       _edit(MODEL, "    def change_batch_quantity(self, ref: str, qty: int):\n",
             "    def deallocate(self, line: OrderLine) -> str:\n"
             "        batch = next((b for b in self.batches if b.has(line)), None)\n"
             "        if batch is None:\n"
             "            raise NotAllocated(f\"Order {line.orderid} is not allocated for {line.sku}\")\n"
             "        batch.deallocate(line)\n"
             "        self.version_number += 1\n"
             "        return batch.reference\n\n"
             "    def change_batch_quantity(self, ref: str, qty: int):\n"),
       _plan("done", "doing", "todo")),
    ai(_edit(HANDLERS, "def reallocate(\n",
             "def deallocate(\n    cmd: commands.Deallocate,\n    uow: unit_of_work.AbstractUnitOfWork,\n):\n"
             "    line = OrderLine(cmd.orderid, cmd.sku, cmd.qty)\n    with uow:\n"
             "        product = uow.products.get(sku=line.sku)\n        if product is None:\n"
             "            raise InvalidSku(f\"Invalid sku {line.sku}\")\n"
             "        product.deallocate(line)\n        uow.commit()\n\n\ndef reallocate(\n"),
       _edit(HANDLERS, "    commands.ChangeBatchQuantity: change_batch_quantity,\n",
             "    commands.ChangeBatchQuantity: change_batch_quantity,\n    commands.Deallocate: deallocate,\n"),
       _plan("done", "done", "done")),
    ai(tool_call("finish", {"summary": "Deallocate command, domain method and handler added."})),
    # -- fix round 1: the harness feeds back "'Batch' object has no attribute 'has'" --
    ai(_edit(MODEL, "    def deallocate_one(self) -> OrderLine:\n",
             "    def has(self, line: OrderLine) -> bool:\n        return line in self._allocations\n\n"
             "    def deallocate(self, line: OrderLine):\n        if line in self._allocations:\n"
             "            self._allocations.remove(line)\n\n"
             "    def deallocate_one(self) -> OrderLine:\n")),
    ai(tool_call("run_command", {"name": "unit_tests"})),
    ai(tool_call("finish", {"summary": "Added the missing Batch.has / Batch.deallocate."})),
]


class DemoModel(ChatModel):
    """Replays ``REFERENCE_REPLIES``; ``delay_s`` per reply leaves time to press Ctrl+C."""

    def __init__(self, delay_s: float = 0.0, replies: Sequence[AIMessage] = REFERENCE_REPLIES):
        self.delay_s = delay_s
        self.replies = list(replies)

    def invoke(self, messages: Sequence[BaseMessage]) -> AIMessage:
        if self.delay_s:
            time.sleep(self.delay_s)
        turn = sum(1 for m in messages if isinstance(m, AIMessage))
        if turn >= len(self.replies):
            raise RuntimeError(f"demo script has only {len(self.replies)} replies (turn {turn + 1} requested)")
        reply = self.replies[turn]
        return AIMessage(content=reply.content,
                         tool_calls=[{**tc, "id": f"call_{uuid.uuid4().hex[:8]}"} for tc in reply.tool_calls])
