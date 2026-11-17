"""Command line interface (presentation only; the logic lives in app.py).

    harness projects                                   # allowed projects
    harness start  --project cosmic-feature --task-file examples/cosmic-feature/task.md
    harness sessions                                   # saved sessions
    harness status <id>                                # progress, steps, limits
    harness cancel <id>                                # stop a running session (from another terminal)
    harness resume <id> [--skip-uncertain] [--accept-workspace]
    harness review <id>                                # changed files, diff, checks, unfinished work
    harness baseline --project cosmic-feature          # checks on the starting code
    harness run --config examples/cosmic/harness.yaml --task-file examples/cosmic/task.md   # Stage 1 style
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
from pathlib import Path

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from .app import (cancel_session, resume_session, run_checks_only, session_review, settings_for_session,
                  start_session)
from .config import load_dotenv, load_projects, load_settings
from .session import CancelToken, Session, SessionStore

console = Console()

_STATUS_STYLE = {"passed": "green", "failed": "red", "timeout": "red", "unavailable": "yellow", "skipped": "dim"}
_SESSION_STYLE = {"completed": "green", "running": "cyan", "paused": "yellow", "cancelled": "yellow", "failed": "red",
                  "created": "dim"}


def _print_event(e: dict, verbose: bool) -> None:
    k = e["kind"]
    if k == "start":
        console.print(Panel.fit(
            f"[bold]session[/] {e['run_id']}{'  (resumed)' if e.get('resumed') else ''}   [bold]model[/] {e['model']}   "
            f"[bold]sandbox[/] {e['sandbox']}\n"
            f"[bold]workspace[/] {escape(e['workspace'])}\n[bold]target commit[/] {e['target_commit']}",
            title="coding harness"))
        if not e["contained"]:
            console.print("[yellow]WARNING: local sandbox backend - commands run with a scrubbed environment and "
                          "process-tree kill, but this is NOT an isolation boundary. Use backend: docker for "
                          "contained execution.[/]")
    elif k == "context_selected":
        files = ", ".join(f"{f['path']} ({f['score']})" for f in e["files"]) or "(none)"
        console.print(f"[bold]context[/] selected files: {escape(files)}")
    elif k == "context" and (e.get("compacted") or verbose):
        console.print(f"[dim]context: {e['chars_sent']}/{e['budget']} chars sent (history {e['chars_full']}; "
                      f"shortened {e['shortened']}, summarized {e['dropped_groups']} turns)[/]")
    elif k == "sandbox":
        console.print(f"[bold]sandbox[/] {escape(e['sandbox'])}")
    elif k == "thinking":
        console.print("[dim]... asking model[/]")
    elif k == "model_reply" and e.get("content") and verbose:
        console.print(f"[cyan]model:[/] {escape(e['content'][:500])}")
    elif k == "model_error":
        console.print(f"[red]model error[/] (retry {e['retries']}): {escape(e['error'])}")
    elif k == "action":
        style = "red" if e["error"] else "green"
        trunc = " [yellow](output truncated)[/]" if e["truncated"] else ""
        console.print(f"[{style}]#{e['n']} {e['tool']}[/] {escape(json.dumps(e['args'], ensure_ascii=False))}{trunc}")
        if verbose or e["tool"] in ("edit_file", "run_command"):
            console.print(Text(e['result'][:1200], style="dim"))
    elif k == "reused":
        console.print(f"[dim]reused saved result of {e['tool']} ({e['call_id']}) - not replayed[/]")
    elif k == "denied":
        console.print(f"[bold red]{escape(e['reason'])}[/] (tool: {escape(str(e['tool']))}, denied so far: {e['denied']})")
    elif k == "limit":
        console.print(f"[bold red]LIMIT[/] {escape(e['reason'])}")
    elif k == "stopped":
        console.print(f"[bold red]Loop stopped by harness:[/] {escape(e['reason'])}")
    elif k == "check_start":
        console.print(f"[bold]check[/] {e['name']}: [dim]{escape(e['command'])}[/]")
    elif k == "check_done":
        console.print(f"  -> [{_STATUS_STYLE[e['status']]}]{e['status'].upper()}[/] (exit {e['exit_code']}, {e['duration_s']}s)")
    elif k == "fix_round":
        console.print(f"[bold yellow]fix round {e['round']}[/]: failed checks fed back to the model: {', '.join(e['failed'])}")
    elif k == "cancelled":
        console.print(f"[bold yellow]cancelled[/] during {escape(e['during'])}")
    elif k == "paused":
        console.print("[bold yellow]Session paused - needs your decision:[/]")
        for p in e["problems"]:
            console.print(f"  - {escape(p)}")
    elif k == "resumed":
        console.print(f"[bold]resuming[/] after node {e['node']}")
        for n in e["notes"]:
            console.print(f"  - {escape(n)}")


def _print_checks(checks: list[dict], show_output: bool) -> None:
    table = Table(title="Final checks (run by the harness)")
    for col in ("check", "type", "status", "exit", "time"):
        table.add_column(col)
    for c in checks:
        table.add_row(c["name"], c.get("type", "test"), f"[{_STATUS_STYLE[c['status']]}]{c['status'].upper()}[/]",
                      str(c["exit_code"]), f"{c['duration_s']}s")
    if not checks:
        table.add_row("(no checks ran)", "-", "[yellow]UNVERIFIED[/]", "-", "-")
    console.print(table)
    for c in checks:
        if show_output or c["status"] not in ("passed", "skipped"):
            console.print(Panel(Text(c["output"] or "(no output)"), title=f"{c['name']} output",
                                border_style=_STATUS_STYLE[c["status"]]))


def _registry(args):
    path = Path(args.projects or os.environ.get("HARNESS_PROJECTS", "projects.yaml"))
    return load_projects(path) if path.exists() else None


def _store(args) -> SessionStore:
    if getattr(args, "sessions_dir", None):
        return SessionStore(Path(args.sessions_dir))
    reg = _registry(args)
    return SessionStore(reg.sessions_dir if reg else Path("runs"))


def _cancel_on_ctrl_c() -> CancelToken:
    """First Ctrl+C: cancel at the next safe point (child processes are killed). Second: hard stop."""
    token = CancelToken()

    def handler(signum, frame):
        if token.is_set():
            raise KeyboardInterrupt
        console.print("\n[yellow]Cancelling... (press Ctrl+C again to abort hard)[/]")
        token.set()

    signal.signal(signal.SIGINT, handler)
    return token


def _print_result(result, verbose: bool) -> int:
    s = result.state
    console.rule(f"Result - session {result.session.id if result.session else ''}")
    if result.status != "completed":
        style = _SESSION_STYLE.get(result.status, "yellow")
        console.print(f"[bold {style}]Session {result.status.upper()}[/]"
                      + (f": {escape(result.pause_reason)}" if result.pause_reason else ""))
    console.print(f"[bold]Model said done:[/] {s.get('finished')}   [bold]actions:[/] {s.get('actions', 0)}   "
                  f"[bold]denied:[/] {s.get('denied', 0)}   [bold]retries:[/] {s.get('retries', 0)}   "
                  f"[bold]fix rounds:[/] {s.get('fix_round', 0)}")
    if s.get("summary"):
        console.print(Panel(Text(s["summary"]), title="Model summary (unverified claim)"))
    if s.get("stop_reason"):
        console.print(f"[bold red]Stopped:[/] {escape(s['stop_reason'])}")
    if s.get("end_reason"):
        console.print(f"[bold]Loop ended:[/] {escape(s['end_reason'])}")
    console.print("[bold]Changed files:[/]")
    for f in s.get("changed_files") or ["(none)"]:
        console.print(f"  {escape(f)}")
    if s.get("diff"):
        console.print(Panel(Syntax(s["diff"], "diff", word_wrap=True), title="Diff"))
    if result.status == "completed":
        _print_checks(s.get("checks", []), verbose)
        color = "green" if result.outcome == "success" else "red"
        console.print(Panel.fit(f"[bold {color}]{result.outcome.upper()}[/]", title="Outcome"))
    else:
        console.print(f"Continue with: [bold]harness resume {result.session.id}[/]   inspect: "
                      f"[bold]harness review {result.session.id}[/]")
    console.print(f"Artifacts: {result.dirs.root}  (session.json, steps.jsonl, report.json, diff.patch, workspace/)")
    return 0 if result.outcome == "success" else 1


# ------------------------------------------------------------------ commands
def cmd_projects(args) -> int:
    reg = _registry(args)
    if reg is None:
        console.print("[red]No projects.yaml found (use --projects or HARNESS_PROJECTS).[/]")
        return 2
    table = Table(title=f"Allowed projects ({reg.path})")
    for col in ("project", "config", "description"):
        table.add_column(col)
    for p in reg.projects.values():
        table.add_row(p.name, str(p.config), p.description)
    console.print(table)
    console.print(f"Sessions are saved in {reg.sessions_dir}")
    return 0


def _task_text(args) -> str | None:
    return Path(args.task_file).read_text(encoding="utf-8") if args.task_file else args.task


def cmd_start(args) -> int:
    reg = _registry(args)
    if reg is None:
        console.print("[red]No projects.yaml found.[/]")
        return 2
    try:
        entry = reg.get(args.project)
    except KeyError as e:
        console.print(f"[red]{escape(str(e))}[/]")
        return 2
    settings = load_settings(entry.config)
    settings.runs_dir = reg.sessions_dir
    overrides = {}
    if args.sandbox:
        settings.sandbox.backend = overrides["sandbox"] = args.sandbox
    if args.max_actions:
        settings.limits.max_actions = args.max_actions
    task = _task_text(args)
    if not task:
        console.print("[red]Provide --task or --task-file[/]")
        return 2
    result = start_session(settings, task, on_event=lambda e: _print_event(e, args.verbose), cancel=_cancel_on_ctrl_c(),
                           project=entry.name, config_path=entry.config, overrides=overrides)
    return _print_result(result, args.verbose)


def cmd_run(args) -> int:
    settings = load_settings(args.config)
    overrides = {}
    if args.sandbox:
        settings.sandbox.backend = overrides["sandbox"] = args.sandbox
    if args.max_actions:
        settings.limits.max_actions = args.max_actions
    task = _task_text(args)
    if not task:
        console.print("[red]Provide --task or --task-file[/]")
        return 2
    result = start_session(settings, task, on_event=lambda e: _print_event(e, args.verbose), cancel=_cancel_on_ctrl_c(),
                           config_path=args.config, overrides=overrides)
    return _print_result(result, args.verbose)


def cmd_sessions(args) -> int:
    store = _store(args)
    table = Table(title=f"Sessions in {store.root}")
    for col in ("id", "project", "status", "outcome", "updated", "request"):
        table.add_column(col)
    for s in store.list():
        st = s["status"] or "?"
        table.add_row(s["id"], s["project"] or "-", f"[{_SESSION_STYLE.get(st, 'white')}]{st}[/]", s["outcome"] or "-",
                      s["updated"] or "", escape(s["request"].replace("\n", " ")))
    console.print(table)
    return 0


def _find(args) -> Path | None:
    try:
        return _store(args).find(args.session)
    except KeyError as e:
        console.print(f"[red]{escape(str(e))}[/]")
        return None


def cmd_status(args) -> int:
    d = _find(args)
    if d is None:
        return 2
    r = session_review(d)
    st = r["status"]
    live = f" (running in process {r['live_pid']})" if r["live_pid"] else ""
    console.print(Panel.fit(
        f"[bold]session[/] {r['id']}   [bold]project[/] {r['project'] or '-'}\n"
        f"[bold]status[/] [{_SESSION_STYLE.get(st, 'white')}]{st}[/]{live}   [bold]outcome[/] {r['outcome'] or '-'}\n"
        f"[bold]start commit[/] {r['task']['start_commit']}   [bold]next[/] after node {r['next_node']}\n"
        f"[bold]allowed changes[/] {', '.join(r['task']['allowed_changes'])}", title="status"))
    p, lim = r["progress"], r["limits"]
    console.print(f"actions {p['actions']}/{lim['max_actions']}   denied {p['denied']}/{lim['max_denied']}   "
                  f"retries {p['retries']}/{lim['max_retries']}   fix rounds {p['fix_round']}/{lim['max_fix_rounds']}   "
                  f"model calls {p['model_calls']}")
    if r["pause_reason"]:
        console.print(f"[yellow]{escape(r['pause_reason'])}[/]")
    if r["plan"]:
        console.print("[bold]Plan:[/]")
        for item in r["plan"]:
            mark = {"done": "[green]x[/]", "doing": "[cyan]>[/]", "todo": " "}[item["status"]]
            console.print(f"  [{mark}] {escape(item['step'])}")
    steps = r["steps"][-args.steps:]
    table = Table(title=f"Last {len(steps)} of {len(r['steps'])} steps")
    for col in ("#", "time", "step", "detail"):
        table.add_column(col)
    for s in steps:
        if s["type"] == "action_done":
            flag = "DENIED" if s.get("denied") else "UNCERTAIN" if s.get("uncertain") else \
                "CANCELLED" if s.get("cancelled") else "error" if s.get("error") else "ok"
            detail = f"{s['tool']}: {flag}"
        elif s["type"] == "checks":
            detail = f"round {s['round']}: " + ", ".join(f"{c['name']}={c['status']}" for c in s["results"])
        else:
            detail = json.dumps({k: v for k, v in s.items() if k not in ("seq", "ts", "type")}, default=str)[:120]
        table.add_row(str(s["seq"]), s["ts"][11:19], s["type"], escape(detail))
    console.print(table)
    if r["errors"]:
        console.print("[bold red]Errors:[/]")
        for e in r["errors"]:
            console.print(f"  {escape(e)}")
    return 0


def cmd_cancel(args) -> int:
    d = _find(args)
    if d is None:
        return 2
    console.print(cancel_session(d))
    return 0


def cmd_resume(args) -> int:
    d = _find(args)
    if d is None:
        return 2
    session = Session.load(d)
    try:
        settings = settings_for_session(session, _registry(args))
    except (KeyError, FileNotFoundError) as e:
        console.print(f"[red]Cannot resume: {escape(str(e))}[/]")
        return 2
    result = resume_session(d, settings, on_event=lambda e: _print_event(e, args.verbose), cancel=_cancel_on_ctrl_c(),
                            skip_uncertain=args.skip_uncertain, accept_workspace=args.accept_workspace)
    if result.pause_reason and ("nothing to resume" in result.pause_reason or "still being run" in result.pause_reason):
        console.print(f"[yellow]{escape(result.pause_reason)}[/]")
        return 1
    return _print_result(result, args.verbose)


def cmd_review(args) -> int:
    d = _find(args)
    if d is None:
        return 2
    r = session_review(d)
    st = r["status"]
    console.rule(f"Review - session {r['id']}")
    console.print(Panel(Text(r["task"]["request"].strip()), title="Request (saved with the task)"))
    console.print(f"[bold]Status:[/] [{_SESSION_STYLE.get(st, 'white')}]{st}[/]   [bold]outcome:[/] {r['outcome'] or '-'}"
                  f"   [bold]start commit:[/] {r['task']['start_commit']}")
    if r["end_reason"]:
        console.print(f"[bold]Loop ended:[/] {escape(r['end_reason'])}")
    if r["summary"]:
        console.print(Panel(Text(r["summary"]), title="Model summary (unverified claim)"))
    console.print("[bold]Changed files:[/]")
    for f in r["changed_files"] or ["(none)"]:
        console.print(f"  {escape(f)}")
    if r["diff"]:
        console.print(Panel(Syntax(r["diff"], "diff", word_wrap=True), title="Final diff"))
    if r["check_history"]:
        console.print("[bold]Check history:[/] " + " | ".join(
            f"round {h['round']}: " + ", ".join(f"{c['name']}={c['status']}" for c in h["checks"])
            for h in r["check_history"]))
    _print_checks(r["checks"], args.full_output)
    console.print("[bold]Unfinished work:[/]")
    for u in r["unfinished"] or ["(none)"]:
        console.print(f"  - {escape(u)}")
    return 0 if r["outcome"] == "success" and st == "completed" else 1


def cmd_baseline(args) -> int:
    if args.project:
        reg = _registry(args)
        if reg is None:
            console.print("[red]No projects.yaml found.[/]")
            return 2
        settings = load_settings(reg.get(args.project).config)
        settings.runs_dir = reg.sessions_dir
    elif args.config:
        settings = load_settings(args.config)
    else:
        console.print("[red]Provide --project or --config[/]")
        return 2
    if args.sandbox:
        settings.sandbox.backend = args.sandbox
    console.print("Running the final checks against the [bold]unchanged starting code[/] ...")
    checks = run_checks_only(settings, on_event=lambda e: _print_event(e, args.verbose))
    _print_checks(checks, show_output=args.verbose)
    bad = 0
    for c in checks:
        if "expectation" in c:
            ok = c["expectation"].startswith("met")
            bad += not ok
            console.print(f"[{'green' if ok else 'red'}]{c['name']}: expectation {escape(c['expectation'])}[/]")
    return 1 if bad else 0


def cmd_build_image(args) -> int:
    """Build the sandbox image with whatever container engine is available (incl. WSL)."""
    from .sandbox import DockerSandbox

    settings = load_settings(args.config)
    if not settings.sandbox.dockerfile:
        console.print("[red]sandbox.dockerfile is not set in the config[/]")
        return 2
    sandbox = DockerSandbox(settings.sandbox, Path("."), Path("."))
    if sandbox.engine is None:
        console.print("[red]No container engine found (tried docker, podman, wsl docker, wsl podman).[/]")
        return 1
    console.print(f"Building [bold]{settings.sandbox.docker_image}[/] with {sandbox.describe()} ...")
    return sandbox.build_image(settings.sandbox.dockerfile)


def cmd_graph(args) -> int:
    """Print the compiled LangGraph controller as Mermaid (paste into mermaid.live)."""
    from .graph import Harness

    graph = Harness(settings=None, workspace=None, sandbox=None, model=None, tools=None).build()  # type: ignore[arg-type]
    print(graph.get_graph().draw_mermaid())
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv(Path.cwd() / ".env")  # model settings + API key; never passed to the sandbox
    p = argparse.ArgumentParser(prog="harness", description="LangGraph coding-agent harness (Stage 2)")
    p.add_argument("--projects", help="allowed projects file (default: projects.yaml or env HARNESS_PROJECTS)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def task_args(sp):
        sp.add_argument("--task")
        sp.add_argument("--task-file")
        sp.add_argument("--max-actions", type=int)
        sp.add_argument("--sandbox", choices=["docker", "local"], help="override sandbox.backend from the config")
        sp.add_argument("-v", "--verbose", action="store_true")

    sub.add_parser("projects", help="list the allowed projects").set_defaults(fn=cmd_projects)

    s = sub.add_parser("start", help="start a task on an allowed project")
    s.add_argument("--project", required=True)
    task_args(s)
    s.set_defaults(fn=cmd_start)

    r = sub.add_parser("run", help="run a task from a config file (Stage 1 style; also saved as a session)")
    r.add_argument("--config", required=True)
    task_args(r)
    r.set_defaults(fn=cmd_run)

    def session_cmd(name, help_, fn):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("session", help="session id, unique id prefix, or session directory")
        sp.add_argument("--sessions-dir")
        sp.set_defaults(fn=fn)
        return sp

    ls = sub.add_parser("sessions", help="list saved sessions")
    ls.add_argument("--sessions-dir")
    ls.set_defaults(fn=cmd_sessions)
    st = session_cmd("status", "show progress and steps of a session", cmd_status)
    st.add_argument("--steps", type=int, default=15)
    session_cmd("cancel", "cancel a running session (or mark a stopped one as cancelled)", cmd_cancel)
    rs = session_cmd("resume", "resume a session from its last safe checkpoint", cmd_resume)
    rs.add_argument("--skip-uncertain", action="store_true",
                    help="mark uncertain interrupted actions as not replayed (after you reviewed the workspace)")
    rs.add_argument("--accept-workspace", action="store_true",
                    help="adopt the current working copy although it differs from the saved state")
    rs.add_argument("-v", "--verbose", action="store_true")
    rv = session_cmd("review", "changed files, final diff, check results and unfinished work", cmd_review)
    rv.add_argument("--full-output", action="store_true")

    b = sub.add_parser("baseline", help="run the final checks on the starting code")
    b.add_argument("--config")
    b.add_argument("--project")
    b.add_argument("--sandbox", choices=["docker", "local"], help="override sandbox.backend from the config")
    b.add_argument("-v", "--verbose", action="store_true")
    b.set_defaults(fn=cmd_baseline)

    bi = sub.add_parser("build-image", help="build the sandbox image (docker/podman, native or in WSL)")
    bi.add_argument("--config", required=True)
    bi.set_defaults(fn=cmd_build_image)

    g = sub.add_parser("graph", help="print the LangGraph controller as Mermaid")
    g.set_defaults(fn=cmd_graph)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
