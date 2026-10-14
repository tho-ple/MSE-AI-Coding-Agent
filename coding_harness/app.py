"""Wires settings, workspace, sandbox, tools and model into a harness run."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .config import Settings
from .graph import Event, Harness, HarnessState
from .llm import ChatModel, make_model
from .sandbox import make_sandbox
from .tools import ToolRegistry
from .workspace import RunDirs, Workspace


@dataclass
class RunResult:
    state: HarnessState
    dirs: RunDirs
    sandbox_contained: bool

    @property
    def outcome(self) -> str:
        return self.state["outcome"]


def run_task(settings: Settings, task: str, *, model: ChatModel | None = None,
             on_event: Callable[[Event], None] | None = None, run_id: str | None = None) -> RunResult:
    run_id = run_id or datetime.now().strftime("%Y%m%d-%H%M%S")
    workspace, dirs = Workspace.create(settings, run_id)
    sandbox = make_sandbox(settings.sandbox, dirs.workspace, dirs.checks)
    tools = ToolRegistry(workspace, sandbox, settings.limits, settings.commands)
    model_label = type(model).__name__ if model else f"{settings.model.provider}:{settings.model.name}"
    model = model or make_model(settings.model, tools.schemas())

    log = (dirs.root / "events.jsonl").open("w", encoding="utf-8")

    def emit(event: Event) -> None:
        log.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), **event}, default=str) + "\n")
        log.flush()
        if on_event:
            on_event(event)

    emit({"kind": "start", "run_id": run_id, "workspace": str(dirs.workspace), "sandbox": sandbox.describe(),
          "contained": sandbox.contained, "model": model_label, "target_commit": settings.target.commit, "task": task})
    try:
        harness = Harness(settings, workspace, sandbox, model, tools, on_event=emit)
        state = harness.run(task)
    finally:
        log.close()

    (dirs.root / "diff.patch").write_text(state["diff"], encoding="utf-8")
    report = {k: state.get(k) for k in ("task", "outcome", "finished", "summary", "stop_reason",
                                        "actions", "denied", "retries", "changed_files", "checks")}
    report.update(run_id=run_id, sandbox=sandbox.describe(), sandbox_contained=sandbox.contained,
                  target_repo=str(settings.target.repo), target_commit=settings.target.commit,
                  model=model_label)
    (dirs.root / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return RunResult(state, dirs, sandbox.contained)


def run_checks_only(settings: Settings, run_id: str | None = None,
                    on_event: Callable[[Event], None] | None = None) -> list[dict]:
    """Run the final checks on the untouched starting code (to prove the acceptance check fails first)."""
    workspace, dirs = Workspace.create(settings, run_id or "baseline-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    sandbox = make_sandbox(settings.sandbox, dirs.workspace, dirs.checks)
    if on_event:
        on_event({"kind": "sandbox", "sandbox": sandbox.describe(), "contained": sandbox.contained})
    harness = Harness(settings, workspace, sandbox, model=None, tools=None)  # type: ignore[arg-type]
    return harness.verify({})["checks"]
