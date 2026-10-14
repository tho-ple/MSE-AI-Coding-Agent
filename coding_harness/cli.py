"""Command line interface.

    harness run --config examples/cosmic/harness.yaml --task-file examples/cosmic/task.md
    harness baseline --config examples/cosmic/harness.yaml     # checks on the starting code
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from rich.console import Console
from rich.markup import escape
from rich.text import Text
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from .app import run_checks_only, run_task
from .config import load_settings

console = Console()

_STATUS_STYLE = {"passed": "green", "failed": "red", "timeout": "red", "unavailable": "yellow"}


def _print_event(e: dict, verbose: bool) -> None:
    k = e["kind"]
    if k == "start":
        console.print(Panel.fit(
            f"[bold]run[/] {e['run_id']}   [bold]model[/] {e['model']}   [bold]sandbox[/] {e['sandbox']}\n"
            f"[bold]workspace[/] {escape(e['workspace'])}\n[bold]target commit[/] {e['target_commit']}",
            title="coding harness"))
        if not e["contained"]:
            console.print("[yellow]WARNING: local sandbox backend - commands run with a scrubbed environment and "
                          "process-tree kill, but this is NOT an isolation boundary. Use backend: docker for "
                          "contained execution.[/]")
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


def _print_checks(checks: list[dict], show_output: bool) -> None:
    table = Table(title="Final checks (run by the harness)")
    for col in ("check", "status", "exit", "time"):
        table.add_column(col)
    for c in checks:
        table.add_row(c["name"], f"[{_STATUS_STYLE[c['status']]}]{c['status'].upper()}[/]", str(c["exit_code"]), f"{c['duration_s']}s")
    if not checks:
        table.add_row("(no checks configured)", "[yellow]UNVERIFIED[/]", "-", "-")
    console.print(table)
    for c in checks:
        if show_output or c["status"] != "passed":
            console.print(Panel(Text(c["output"] or "(no output)"), title=f"{c['name']} output", border_style=_STATUS_STYLE[c["status"]]))


def _load(args):
    settings = load_settings(args.config)
    if args.sandbox:
        settings.sandbox.backend = args.sandbox
    return settings


def cmd_run(args) -> int:
    settings = _load(args)
    if args.max_actions:
        settings.limits.max_actions = args.max_actions
    task = Path(args.task_file).read_text(encoding="utf-8") if args.task_file else args.task
    if not task:
        console.print("[red]Provide --task or --task-file[/]")
        return 2
    try:
        result = run_task(settings, task, on_event=lambda e: _print_event(e, args.verbose))
    except KeyboardInterrupt:
        console.print("[red]Interrupted - running commands were killed with their process tree.[/]")
        return 130
    s = result.state

    console.rule("Result")
    console.print(f"[bold]Model said done:[/] {s['finished']}   [bold]actions:[/] {s['actions']}   "
                  f"[bold]denied:[/] {s['denied']}   [bold]retries:[/] {s['retries']}")
    if s.get("summary"):
        console.print(Panel(Text(s["summary"]), title="Model summary (unverified claim)"))
    if s.get("stop_reason"):
        console.print(f"[bold red]Stopped:[/] {escape(s['stop_reason'])}")
    console.print("[bold]Changed files:[/]")
    for f in s["changed_files"] or ["(none)"]:
        console.print(f"  {escape(f)}")
    if s["diff"]:
        console.print(Panel(Syntax(s["diff"], "diff", word_wrap=True), title="Diff"))
    _print_checks(s["checks"], args.verbose)

    color = "green" if result.outcome == "success" else "red"
    console.print(Panel.fit(f"[bold {color}]{result.outcome.upper()}[/]", title="Outcome"))
    console.print(f"Artifacts: {result.dirs.root}  (report.json, diff.patch, events.jsonl, workspace/)")
    return 0 if result.outcome == "success" else 1


def cmd_baseline(args) -> int:
    settings = _load(args)
    console.print("Running the final checks against the [bold]unchanged starting code[/] ...")
    checks = run_checks_only(settings, on_event=lambda e: _print_event(e, args.verbose))
    _print_checks(checks, show_output=args.verbose)
    return 0


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
    p = argparse.ArgumentParser(prog="harness", description="LangGraph coding-agent harness (Stage 1)")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="run a coding task")
    r.add_argument("--config", required=True)
    r.add_argument("--task")
    r.add_argument("--task-file")
    r.add_argument("--max-actions", type=int)
    r.add_argument("--sandbox", choices=["docker", "local"], help="override sandbox.backend from the config")
    r.add_argument("-v", "--verbose", action="store_true")
    r.set_defaults(fn=cmd_run)

    b = sub.add_parser("baseline", help="run the final checks on the starting code")
    b.add_argument("--config", required=True)
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
