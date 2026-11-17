"""Application core: start, resume, cancel and review sessions. No user-interface code here.

The CLI (``cli.py``) only parses arguments, calls these functions and prints what they return.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from langchain_core.messages import messages_from_dict, messages_to_dict

from .config import LimitSettings, ProjectRegistry, Settings, load_settings
from .graph import Event, Harness, HarnessState, Recorder, outcome_record
from .llm import ChatModel, make_model
from .sandbox import make_sandbox
from .session import (SIDE_EFFECT_TOOLS, CancelToken, Cancelled, Paused, Session, SessionStore,
                      diff_fingerprints)
from .tools import ToolOutcome, ToolRegistry
from .workspace import RunDirs, Workspace


@dataclass
class RunResult:
    state: HarnessState
    dirs: RunDirs
    sandbox_contained: bool
    session: Session | None = None
    status: str = "completed"  # completed | cancelled | paused | failed
    pause_reason: str | None = None

    @property
    def outcome(self) -> str:
        return self.state.get("outcome") or self.status


def _dirs(root: Path) -> RunDirs:
    return RunDirs(root=root, workspace=root / "workspace", checks=root / "checks")


# --------------------------------------------------------------- task record
def permission_snapshot(settings: Settings) -> dict:
    """What the session was allowed to do. Revalidated on resume; any difference pauses the session."""
    return json.loads(json.dumps({
        "target_repo": str(settings.target.repo),
        "start_commit": settings.target.commit,
        "allowed_changes": list(settings.target.writable),
        "commands": dict(settings.commands),
        "checks": [[c.name, c.command] for c in settings.checks],
        "sandbox_backend": settings.sandbox.backend,
    }))


def _task_record(settings: Settings, task: str, ws: Workspace) -> dict:
    return {
        "request": task,
        "acceptance_criteria": list(settings.acceptance_criteria),
        "target_repo": str(settings.target.repo),
        "start_commit": settings.target.commit,
        "workspace_baseline": ws.head(),
        "allowed_changes": list(settings.target.writable),
        "commands": dict(settings.commands),
        "checks": [{"name": c.name, "kind": c.kind, "command": c.command} for c in settings.checks],
    }


def _serialize_state(state: HarnessState) -> dict:
    return {k: v for k, v in state.items() if k != "messages"}


def load_state(session: Session) -> HarnessState:
    state: HarnessState = dict(session.data.get("state") or {})  # type: ignore[assignment]
    state["messages"] = messages_from_dict(session.data.get("messages") or [])
    return state


# ------------------------------------------------------------------ recorder
class SessionRecorder(Recorder):
    """Persists the controller's steps: write-ahead journal per action, checkpoint per node."""

    def __init__(self, session: Session, ws: Workspace):
        super().__init__()
        self.session, self.ws = session, ws
        self.done = session.completed_actions()
        self.started = {r["call_id"] for r in session.read_journal() if r["type"] == "action_start"}

    def journal(self, type_: str, **data) -> None:
        self.session.journal(type_, **data)

    def action_start(self, call: dict, n: int) -> None:
        super().action_start(call, n)
        self.session.journal("action_start", call_id=call["id"], tool=call["name"], args=call["args"], n=n)

    def action_done(self, call: dict, outcome: ToolOutcome, side_effect: bool) -> None:
        rec = outcome_record(call, outcome)
        self.done[call["id"]] = rec
        extra = {"fingerprint": self.ws.fingerprint()} if side_effect else {}
        self.session.journal("action_done", **rec, **extra)

    def checkpoint(self, state: HarnessState) -> None:
        super().checkpoint(state)
        fp = self.ws.fingerprint()
        counters = {k: state.get(k, 0) for k in ("actions", "denied", "retries", "model_calls", "fix_round")}
        self.session.journal("checkpoint", node=state.get("last_node"), counters=counters, fingerprint=fp)
        self.session.save(status="running", state=_serialize_state(state),
                          messages=messages_to_dict(state.get("messages") or []), fingerprint=fp)


def expected_fingerprint(session: Session) -> dict[str, str] | None:
    """The latest working-copy state the harness recorded (after its own last edit or checkpoint)."""
    fp = None
    for rec in session.read_journal():
        if "fingerprint" in rec:
            fp = rec["fingerprint"]
    return fp if fp is not None else session.data.get("fingerprint")


# --------------------------------------------------------------------- start
def start_session(settings: Settings, task: str, *, model: ChatModel | None = None,
                  on_event: Callable[[Event], None] | None = None, run_id: str | None = None,
                  cancel: CancelToken | None = None, project: str | None = None,
                  config_path: str | Path | None = None, overrides: dict | None = None) -> RunResult:
    """Create a session (disposable copy + saved task record) and run it."""
    sid = run_id or SessionStore(settings.runs_dir).new_id(project or "run")
    workspace, dirs = Workspace.create(settings, sid)
    session = Session.create(dirs.root, {
        "id": sid, "project": project, "config_path": str(Path(config_path).resolve()) if config_path else None,
        "overrides": overrides or {}, "task": _task_record(settings, task, workspace),
        "permissions": permission_snapshot(settings), "limits": asdict(settings.limits),
        "context_limits": asdict(settings.context),
    })
    session.journal("task_saved", request=task, start_commit=settings.target.commit,
                    allowed_changes=settings.target.writable, checks=[c.name for c in settings.checks])
    return _execute(session, settings, workspace, model=model, on_event=on_event, cancel=cancel, task=task)


def run_task(settings: Settings, task: str, *, model: ChatModel | None = None,
             on_event: Callable[[Event], None] | None = None, run_id: str | None = None,
             cancel: CancelToken | None = None) -> RunResult:
    """Stage 1 entry point; every run is a saved session now."""
    return start_session(settings, task, model=model, on_event=on_event, run_id=run_id, cancel=cancel)


def _execute(session: Session, settings: Settings, workspace: Workspace, *, model: ChatModel | None,
             on_event: Callable[[Event], None] | None, cancel: CancelToken | None,
             task: str | None = None, state: HarnessState | None = None) -> RunResult:
    dirs = _dirs(session.root)
    sandbox = make_sandbox(settings.sandbox, dirs.workspace, dirs.checks)
    tools = ToolRegistry(workspace, sandbox, settings.limits, settings.commands, settings.target.commit)
    model_label = type(model).__name__ if model else f"{settings.model.provider}:{settings.model.name}"
    model = model or make_model(settings.model, tools.schemas())
    cancel = cancel or CancelToken()
    cancel.flag_file = session.cancel_flag
    session.clear_cancel()
    session.acquire()
    log = (dirs.root / "events.jsonl").open("a", encoding="utf-8")

    def emit(event: Event) -> None:
        log.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), **event}, default=str) + "\n")
        log.flush()
        if on_event:
            on_event(event)

    recorder = SessionRecorder(session, workspace)
    status, reason = "completed", None
    try:
        harness = Harness(settings, workspace, sandbox, model, tools, on_event=emit, recorder=recorder, cancel=cancel)
        resumed = state is not None
        emit({"kind": "start", "run_id": session.id, "workspace": str(dirs.workspace), "sandbox": sandbox.describe(),
              "contained": sandbox.contained, "model": model_label, "target_commit": settings.target.commit,
              "task": task or (state or {}).get("task"), "resumed": resumed})
        if state is None:
            state = harness.initial_state(task or "")
        recorder.last_state = state
        session.save(status="running", sandbox=sandbox.describe(), model=model_label)
        try:
            final = harness.run(state)
        except Cancelled as e:
            status, reason, final = "cancelled", str(e), recorder.last_state
            session.journal("cancelled", reason=reason, node=final.get("last_node"))
        except Paused as e:
            status, reason, final = "paused", str(e), recorder.last_state
            session.journal("pause", problems=[reason])
        except Exception as e:
            status, reason, final = "failed", f"{type(e).__name__}: {e}", recorder.last_state
            session.journal("error", error=reason)
            final = {**final, "errors": final.get("errors", []) + [reason]}
    finally:
        log.close()
        session.release()
        session.clear_cancel()
    return _finish(session, settings, workspace, sandbox.contained, final, status, reason, model_label, sandbox.describe())


def _finish(session: Session, settings: Settings, ws: Workspace, contained: bool, final: HarnessState,
            status: str, reason: str | None, model_label: str, sandbox_label: str) -> RunResult:
    final = dict(final)  # type: ignore[assignment]
    final["diff"] = ws.diff()
    final["changed_files"] = ws.changed_files()
    (session.root / "diff.patch").write_text(final["diff"], encoding="utf-8")
    report = {k: final.get(k) for k in ("task", "outcome", "end_reason", "finished", "summary", "stop_reason",
                                         "actions", "denied", "retries", "model_calls", "fix_round",
                                         "changed_files", "checks", "check_history", "plan", "errors")}
    report.update(run_id=session.id, status=status, pause_reason=reason if status == "paused" else None,
                  sandbox=sandbox_label, sandbox_contained=contained, target_repo=str(settings.target.repo),
                  target_commit=settings.target.commit, model=model_label)
    (session.root / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    session.save(status=status, pause_reason=reason if status in ("paused", "cancelled", "failed") else None,
                 state=_serialize_state(final), messages=messages_to_dict(final.get("messages") or []),
                 fingerprint=ws.fingerprint())
    session.journal("run_end", status=status, outcome=final.get("outcome"), reason=reason)
    return RunResult(final, _dirs(session.root), contained, session, status, reason if status == "paused" else None)


# -------------------------------------------------------------------- resume
@dataclass
class ResumeCheck:
    problems: list[str]
    notes: list[str]


def validate_resume(session: Session, settings: Settings, *, skip_uncertain: bool = False,
                    accept_workspace: bool = False) -> ResumeCheck:
    """Revalidate a saved session before continuing it. Problems pause the session; notes are informational.

    Hard problems (cannot be overridden): already completed, still running elsewhere, permissions/config
    changed, baseline commit changed, harness edits outside the allowed changes.
    Soft problems (need an explicit user decision): uncertain interrupted actions (``skip_uncertain``),
    working copy differs from the saved state (``accept_workspace``).
    """
    problems, notes = [], []
    status = session.data.get("status")
    if status == "completed":
        return ResumeCheck(["session already completed - nothing to resume (use review)"], [])
    owner = session.lock_owner()
    if owner is not None:
        return ResumeCheck([f"session is still being run by process {owner}"], [])
    if status == "running":
        notes.append("previous run was interrupted (the process ended without saving a final status)")

    saved, current = session.data.get("permissions") or {}, permission_snapshot(settings)
    changed = sorted(k for k in set(saved) | set(current) if saved.get(k) != current.get(k))
    if changed:
        problems.append("permissions/config changed since the session was saved: " + ", ".join(changed))
    try:
        ws = Workspace.open(settings, session.workspace)
    except FileNotFoundError as e:
        return ResumeCheck(problems + [str(e)], notes)
    if ws.head() != session.data["task"]["workspace_baseline"]:
        problems.append("the copy's baseline commit changed (someone committed in the workspace)")
    outside = [line.split()[-1] for line in ws.changed_files() if not ws.is_writable(line.split()[-1])]
    if outside:
        problems.append("changed files outside the allowed changes: " + ", ".join(outside))
    if problems:
        return ResumeCheck(problems, notes)

    # Uncertain actions: started, never recorded as done. Never replayed automatically.
    for rec in session.uncertain_actions():
        label = f"#{rec.get('n')} {rec['tool']} {json.dumps(rec.get('args'), default=str)[:120]}"
        if rec["tool"] not in SIDE_EFFECT_TOOLS:
            session.journal("action_done", call_id=rec["call_id"], tool=rec["tool"], error=True, uncertain=True,
                            resolved_by="harness (read-only)", content=(
                                "INTERRUPTED: the harness stopped while this read-only call was running. "
                                "It was not replayed; request it again if you still need the result."))
            notes.append(f"read-only action {label} was interrupted; marked as not replayed")
        elif skip_uncertain:
            session.journal("action_done", call_id=rec["call_id"], tool=rec["tool"], error=True, uncertain=True,
                            resolved_by="user (--skip-uncertain)", content=(
                                f"INTERRUPTED: the harness stopped while this {rec['tool']} call was running. "
                                "Its effect is UNKNOWN and it was NOT replayed. Check the current state "
                                "(git_diff, read_file) before repeating it."))
            notes.append(f"uncertain action {label} marked as not replayed (user decision)")
        else:
            problems.append(f"uncertain interrupted action {label}: it may or may not have taken effect. "
                            "Review the workspace, then resume with --skip-uncertain")

    expected = expected_fingerprint(session)
    actual = ws.fingerprint()
    diffs = diff_fingerprints(expected or {}, actual) if expected is not None else ["no saved working-copy state"]
    if diffs:
        if accept_workspace:
            session.journal("workspace_accepted", changes=diffs, fingerprint=actual)
            notes.append("working copy accepted as is (user decision): " + ", ".join(diffs[:10]))
        else:
            problems.append("working copy differs from the saved state: " + ", ".join(diffs[:10])
                            + (" ..." if len(diffs) > 10 else "") + " - review it, then resume with --accept-workspace")
    return ResumeCheck(problems, notes)


def resume_session(session_dir: str | Path, settings: Settings, *, model: ChatModel | None = None,
                   on_event: Callable[[Event], None] | None = None, cancel: CancelToken | None = None,
                   skip_uncertain: bool = False, accept_workspace: bool = False) -> RunResult:
    """Continue a saved session from its last safe checkpoint, after revalidation."""
    session = Session.load(Path(session_dir))
    # Consumed limits are preserved: the saved limits and counters apply, not the config's current ones.
    settings.limits = LimitSettings(**session.data["limits"])
    check = validate_resume(session, settings, skip_uncertain=skip_uncertain, accept_workspace=accept_workspace)
    state = load_state(session)
    if check.problems:
        if session.data.get("status") != "completed" and session.lock_owner() is None:
            session.save(status="paused", pause_reason="; ".join(check.problems))
            session.journal("pause", problems=check.problems, notes=check.notes)
        if on_event:
            on_event({"kind": "paused", "problems": check.problems, "notes": check.notes})
        return RunResult(state, _dirs(session.root), settings.sandbox.backend == "docker", session,
                         "paused" if session.data.get("status") != "completed" else "completed",
                         "; ".join(check.problems))
    session.journal("resume", notes=check.notes,
                    counters={k: state.get(k, 0) for k in ("actions", "denied", "retries", "model_calls", "fix_round")})
    if on_event:
        on_event({"kind": "resumed", "notes": check.notes, "node": state.get("last_node")})
    ws = Workspace.open(settings, session.workspace)
    return _execute(session, settings, ws, model=model, on_event=on_event, cancel=cancel, state=state)


def settings_for_session(session: Session, registry: ProjectRegistry | None) -> Settings:
    """Reload the settings a session was started with; the project must still be allowed."""
    project = session.data.get("project")
    if project:
        if registry is None:
            raise KeyError(f"session belongs to project {project!r} but no project registry was given")
        entry = registry.get(project)  # raises if the project is no longer allowed
        settings = load_settings(entry.config)
    elif session.data.get("config_path"):
        settings = load_settings(session.data["config_path"])
    else:
        raise KeyError("session has no project or config to reload")
    ov = session.data.get("overrides") or {}
    if ov.get("sandbox"):
        settings.sandbox.backend = ov["sandbox"]
    settings.runs_dir = session.root.parent
    return settings


# --------------------------------------------------------- cancel / inspect
def cancel_session(session_dir: str | Path) -> str:
    session = Session.load(Path(session_dir))
    status = session.data.get("status")
    if session.lock_owner() is not None:
        session.request_cancel()
        session.journal("cancel_requested")
        return f"cancel requested; process {session.lock_owner()} will stop at the next safe point"
    if status in ("completed", "cancelled"):
        return f"session is already {status}"
    session.save(status="cancelled", pause_reason="cancelled by user while not running")
    session.journal("cancelled", reason="cancelled while not running", previous_status=status)
    return f"session was {status}; marked as cancelled (resume to continue)"


def session_review(session_dir: str | Path) -> dict:
    """Everything needed to inspect a session: task, progress, steps, changed files, diff, checks, unfinished work."""
    session = Session.load(Path(session_dir))
    data, state = session.data, session.data.get("state") or {}
    limits = data.get("limits") or {}
    journal = session.read_journal()
    diff, changed = "", []
    if (session.workspace / ".git").exists():
        ws = Workspace(session.workspace, data["task"]["allowed_changes"], Settings().target.ignore)
        diff, changed = ws.diff(), ws.changed_files()
    checks = state.get("checks") or []
    uncertain = [r for r in session.uncertain_actions() if r["tool"] in SIDE_EFFECT_TOOLS]
    unfinished = []
    status = data.get("status")
    live = session.lock_owner() is not None
    if status == "running" and not live:
        unfinished.append("run was interrupted (process ended without a final status) - resume to continue")
    if status == "paused":
        unfinished.append(f"paused: {data.get('pause_reason')}")
    if status == "cancelled":
        unfinished.append("cancelled by user - resume to continue")
    if status == "failed":
        unfinished.append(f"failed: {data.get('pause_reason')}")
    for r in uncertain:
        unfinished.append(f"uncertain interrupted action #{r.get('n')} {r['tool']} (not replayed)")
    if state.get("stop_reason"):
        unfinished.append(f"stopped: {state['stop_reason']}")
    if status == "completed" and not state.get("finished"):
        unfinished.append("the model did not call finish")
    for c in checks:
        if c["status"] not in ("passed", "skipped"):
            unfinished.append(f"check {c['name']}: {c['status'].upper()}")
    if status == "completed" and not checks:
        unfinished.append("no final checks ran")
    for item in state.get("plan") or []:
        if item.get("status") != "done":
            unfinished.append(f"plan step ({item.get('status')}): {item.get('step')}")
    steps = [r for r in journal if r["type"] in ("action_done", "checks", "fix_round", "cancelled", "pause",
                                                 "resume", "workspace_accepted", "model_error", "error", "run_end")]
    return {
        "id": data["id"], "project": data.get("project"), "status": status, "live_pid": session.lock_owner(),
        "pause_reason": data.get("pause_reason"), "task": data["task"], "outcome": state.get("outcome"),
        "end_reason": state.get("end_reason"), "summary": state.get("summary"),
        "progress": {k: state.get(k, 0) for k in ("actions", "denied", "retries", "model_calls", "fix_round")},
        "limits": {k: limits.get(k) for k in ("max_actions", "max_denied", "max_retries", "max_fix_rounds")},
        "next_node": state.get("last_node"), "plan": state.get("plan") or [],
        "selected_files": state.get("selected_files") or [], "errors": state.get("errors") or [],
        "checks": checks, "check_history": state.get("check_history") or [],
        "changed_files": changed, "diff": diff, "unfinished": unfinished, "steps": steps,
        "created": data.get("created"), "updated": data.get("updated"), "dir": str(session.root),
    }


# ---------------------------------------------------------------- baseline
def run_checks_only(settings: Settings, run_id: str | None = None,
                    on_event: Callable[[Event], None] | None = None) -> list[dict]:
    """Run the final checks on the untouched starting code (to prove the acceptance check fails first).

    Checks with ``expect_on_start`` get an ``expectation`` field: whether they failed for the expected reason.
    """
    import re

    workspace, dirs = Workspace.create(settings, run_id or "baseline-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    sandbox = make_sandbox(settings.sandbox, dirs.workspace, dirs.checks)
    if on_event:
        on_event({"kind": "sandbox", "sandbox": sandbox.describe(), "contained": sandbox.contained})
    harness = Harness(settings, workspace, sandbox, model=None, tools=None, on_event=on_event or (lambda e: None))  # type: ignore[arg-type]
    checks = harness.verify({})["checks"]
    specs = {c.name: c for c in settings.checks}
    for r in checks:
        spec = specs.get(r["name"])
        if spec and spec.expect_on_start:
            ok = r["status"] == spec.expect_on_start
            why = f"status {r['status']} (expected {spec.expect_on_start})"
            if ok and spec.expect_reason:
                ok = re.search(spec.expect_reason, r["output"] or "") is not None
                why += f"; reason /{spec.expect_reason}/ {'found' if ok else 'NOT found'} in output"
            r["expectation"] = ("met: " if ok else "NOT met: ") + why
    return checks
