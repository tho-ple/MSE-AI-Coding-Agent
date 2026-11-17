"""Repository and execution tools exposed to the model.

Every tool call goes through :meth:`ToolRegistry.call`, which

1. rejects unknown tool names,
2. validates arguments against a strict pydantic schema (no extra keys),
3. applies the workspace path policy (read scope / write scope),
4. only then executes, and bounds the output it returns.

Rejections in steps 1-3 are *denied* actions: nothing is executed.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .config import LimitSettings
from .limits import truncate
from .navigation import RepoIndex, git_state
from .sandbox import CommandResult, Sandbox
from .workspace import PathDenied, Workspace


# ------------------------------------------------------------------ arg schemas
class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ListFilesArgs(_Args):
    path: str = Field(".", description="Directory relative to the repository root.")


class ReadFileArgs(_Args):
    path: str = Field(description="File path relative to the repository root.")
    start_line: int = Field(1, ge=1, description="First line to return (1-based).")
    end_line: int | None = Field(None, ge=1, description="Last line to return (inclusive). Omit for end of file.")


class SearchArgs(_Args):
    pattern: str = Field(min_length=1, description="Text to find (a Python regex if regex=true).")
    path: str = Field(".", description="Directory or file to search, relative to the repository root.")
    regex: bool = Field(False, description="Interpret pattern as a regular expression.")


class EditFileArgs(_Args):
    path: str = Field(description="File to edit, relative to the repository root.")
    old_text: str = Field(min_length=1, description="Exact existing text to replace. Must occur exactly once.")
    new_text: str = Field(description="Replacement text.")


class WriteFileArgs(_Args):
    path: str = Field(description="File to create or overwrite, relative to the repository root.")
    content: str = Field(description="Full file content.")


class RunCommandArgs(_Args):
    name: str = Field(description="Name of a configured command.")
    target: str | None = Field(None, description="Optional file/dir/test node id inside the repository to pass on.")


class FinishArgs(_Args):
    summary: str = Field(description="What you changed and why.")


class RepoMapArgs(_Args):
    path: str = Field(".", description="Directory to map, relative to the repository root.")


class SymbolArgs(_Args):
    name: str = Field(min_length=1, max_length=200, description="Symbol name, e.g. 'allocate' or 'Product.allocate'.")


class ReferencesArgs(_Args):
    name: str = Field(min_length=1, max_length=200, description="Symbol name whose uses (imports, calls, attributes) to find.")
    path: str = Field(".", description="Only search below this directory.")


class ModuleDepsArgs(_Args):
    path: str = Field(description="Python file, relative to the repository root.")


class NoArgs(_Args):
    pass


class GitDiffArgs(_Args):
    path: str | None = Field(None, description="Limit the diff to one file or directory.")


class PlanItem(_Args):
    step: str = Field(min_length=1, max_length=300)
    status: Literal["todo", "doing", "done"] = "todo"


class PlanArgs(_Args):
    items: list[PlanItem] = Field(min_length=1, max_length=20, description="The full, updated plan.")


@dataclass
class ToolOutcome:
    tool: str
    content: str  # text returned to the model (already bounded)
    denied: bool = False  # validation/policy rejection -> nothing executed
    error: bool = False  # executed but failed (e.g. edit target not found, nonzero exit)
    finished: bool = False
    truncated: bool = False
    command: CommandResult | None = None
    cancelled: bool = False  # a running command was stopped by the user
    plan: list[dict] | None = None


@dataclass
class ToolSpec:
    name: str
    description: str
    args: type[_Args]
    fn: Callable[[_Args], ToolOutcome]

    def schema(self) -> dict:
        """OpenAI-style function schema (what ``bind_tools`` / Ollama expect)."""
        params = self.args.model_json_schema()
        params.pop("title", None)
        for p in params.get("properties", {}).values():
            p.pop("title", None)
        return {"type": "function", "function": {"name": self.name, "description": self.description, "parameters": params}}


_SKIP_SEARCH_SUFFIXES = {".pyc", ".png", ".jpg", ".gif", ".ico", ".zip", ".gz", ".db", ".sqlite", ".so", ".dll", ".exe"}


class ToolRegistry:
    def __init__(self, workspace: Workspace, sandbox: Sandbox, limits: LimitSettings, commands: dict[str, str],
                 start_commit: str | None = None):
        self.ws = workspace
        self.sandbox = sandbox
        self.limits = limits
        self.commands = commands
        self.start_commit = start_commit
        self._index: RepoIndex | None = None
        self._cancel = None
        self.specs: dict[str, ToolSpec] = {}
        self._register("list_files", "List files under a directory of the repository (recursive).", ListFilesArgs, self._list_files)
        self._register("read_file", "Read a text file. Lines are prefixed with their line numbers.", ReadFileArgs, self._read_file)
        self._register("search", "Search file contents. Returns path:line: text matches.", SearchArgs, self._search)
        self._register("repo_map", "Compact map of Python modules under a directory: classes (with methods), functions, constants.",
                       RepoMapArgs, self._repo_map)
        self._register("find_symbol", "Find where a class, function, method or constant is defined (file:line and signature).",
                       SymbolArgs, self._find_symbol)
        self._register("find_references", "Find imports, calls and uses of a symbol across Python modules (e.g. existing callers).",
                       ReferencesArgs, self._find_references)
        self._register("module_deps", "Show which repository modules a Python file imports and which modules import it.",
                       ModuleDepsArgs, self._module_deps)
        self._register("git_status", "Read-only Git state of the copy: baseline commit and files changed so far.", NoArgs, self._git_status)
        self._register("git_diff", "Read-only: the diff of your changes so far (optionally for one path).", GitDiffArgs, self._git_diff)
        self._register("update_plan", "Record your plan as a list of steps with status todo/doing/done. Shown to the user as progress.",
                       PlanArgs, self._update_plan)
        self._register("edit_file", "Replace one exact, unique occurrence of old_text with new_text in a file.", EditFileArgs, self._edit_file)
        self._register("write_file", "Create a new file or overwrite an existing one with the given content.", WriteFileArgs, self._write_file)
        names = ", ".join(sorted(commands)) or "(none configured)"
        self._register("run_command", f"Run a configured command in the sandbox and get its exit code and output. Available names: {names}.",
                       RunCommandArgs, self._run_command)
        self._register("finish", "Call when the task is done. The harness will then run the final checks itself.", FinishArgs, self._finish)

    def _register(self, name, description, args, fn):
        self.specs[name] = ToolSpec(name, description, args, fn)

    def schemas(self) -> list[dict]:
        return [s.schema() for s in self.specs.values()]

    # -------------------------------------------------------------- dispatch
    def call(self, name: str, raw_args: object, cancel=None) -> ToolOutcome:
        self._cancel = cancel
        spec = self.specs.get(name)
        if spec is None:
            return ToolOutcome(name, f"DENIED: unknown tool {name!r}. Available tools: {', '.join(self.specs)}.", denied=True)
        if not isinstance(raw_args, dict):
            return ToolOutcome(name, f"DENIED: arguments for {name} must be a JSON object, got {type(raw_args).__name__}.", denied=True)
        try:
            args = spec.args.model_validate(raw_args)
        except ValidationError as e:
            problems = "; ".join(f"{'.'.join(map(str, err['loc'])) or 'args'}: {err['msg']}" for err in e.errors())
            return ToolOutcome(name, f"DENIED: invalid arguments for {name}: {problems}", denied=True)
        try:
            outcome = spec.fn(args)
        except PathDenied as e:
            return ToolOutcome(name, f"DENIED: {e}", denied=True)
        except (OSError, UnicodeDecodeError, re.error) as e:
            return ToolOutcome(name, f"ERROR: {type(e).__name__}: {e}", error=True)
        if not outcome.denied:
            limit = self.limits.max_read_chars if name == "read_file" else self.limits.max_output_chars
            outcome.content, cut = truncate(outcome.content, limit)
            outcome.truncated = outcome.truncated or cut
        return outcome

    # ----------------------------------------------------------------- tools
    def _walk(self, base):
        return self.ws.iter_files(base)

    @property
    def index(self) -> RepoIndex:
        if self._index is None:
            self._index = RepoIndex(self.ws)
        return self._index.refresh()

    def _list_files(self, a: ListFilesArgs) -> ToolOutcome:
        base = self.ws.resolve(a.path)
        if not base.exists():
            return ToolOutcome("list_files", f"ERROR: {a.path!r} does not exist.", error=True)
        files = []
        for p in self._walk(base):
            files.append(self.ws.relpath(p))
            if len(files) > self.limits.max_list_entries:
                break
        trunc = len(files) > self.limits.max_list_entries
        files = files[: self.limits.max_list_entries]
        text = "\n".join(files) or "(no files)"
        if trunc:
            text += f"\n[... listing truncated by harness at {self.limits.max_list_entries} entries ...]"
        return ToolOutcome("list_files", text, truncated=trunc)

    def _read_file(self, a: ReadFileArgs) -> ToolOutcome:
        p = self.ws.resolve(a.path)
        if not p.is_file():
            return ToolOutcome("read_file", f"ERROR: {a.path!r} is not a file.", error=True)
        lines = p.read_text(encoding="utf-8").splitlines()
        end = min(a.end_line or len(lines), len(lines))
        body = "\n".join(f"{i:>5}| {lines[i - 1]}" for i in range(a.start_line, end + 1))
        header = f"{self.ws.relpath(p)} (lines {a.start_line}-{end} of {len(lines)})\n"
        body, cut = truncate(body, max(self.limits.max_read_chars - len(header), 0))
        return ToolOutcome("read_file", header + body, truncated=cut)

    def _search(self, a: SearchArgs) -> ToolOutcome:
        base = self.ws.resolve(a.path)
        if not base.exists():
            return ToolOutcome("search", f"ERROR: {a.path!r} does not exist.", error=True)
        rx = re.compile(a.pattern if a.regex else re.escape(a.pattern))
        hits: list[str] = []
        limit = self.limits.max_search_results
        for p in self._walk(base):
            if p.suffix.lower() in _SKIP_SEARCH_SUFFIXES:
                continue
            try:
                text = p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for n, line in enumerate(text.splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{self.ws.relpath(p)}:{n}: {line.strip()[:200]}")
                    if len(hits) > limit:
                        break
            if len(hits) > limit:
                break
        trunc = len(hits) > limit
        text = "\n".join(hits[:limit]) or "(no matches)"
        if trunc:
            text += f"\n[... results truncated by harness at {limit} matches ...]"
        return ToolOutcome("search", text, truncated=trunc)

    def _edit_file(self, a: EditFileArgs) -> ToolOutcome:
        p = self.ws.resolve(a.path, write=True)
        if not p.is_file():
            return ToolOutcome("edit_file", f"ERROR: {a.path!r} does not exist. Use write_file to create files.", error=True)
        raw = p.read_bytes().decode("utf-8")
        crlf = "\r\n" in raw
        text = raw.replace("\r\n", "\n")
        old, new = a.old_text.replace("\r\n", "\n"), a.new_text.replace("\r\n", "\n")
        count = text.count(old)
        if count != 1:
            hint = "not found" if count == 0 else f"found {count} times; include more surrounding context"
            return ToolOutcome("edit_file", f"ERROR: old_text {hint} in {a.path}. No change made.", error=True)
        updated = text.replace(old, new, 1)
        p.write_bytes((updated.replace("\n", "\r\n") if crlf else updated).encode("utf-8"))
        return ToolOutcome("edit_file", f"Edited {self.ws.relpath(p)}:\n" + _mini_diff(text, updated, a.path))

    def _write_file(self, a: WriteFileArgs) -> ToolOutcome:
        p = self.ws.resolve(a.path, write=True)
        if p.is_dir():
            return ToolOutcome("write_file", f"ERROR: {a.path!r} is a directory.", error=True)
        existed = p.exists()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(a.content, encoding="utf-8", newline="\n")
        return ToolOutcome("write_file", f"{'Overwrote' if existed else 'Created'} {self.ws.relpath(p)} ({len(a.content)} chars).")

    def _run_command(self, a: RunCommandArgs) -> ToolOutcome:
        if a.name not in self.commands:
            raise PathDenied(f"command {a.name!r} is not configured. Allowed: {', '.join(sorted(self.commands)) or 'none'}")
        cmd = self.sandbox.render(self.commands[a.name])
        if a.target:
            path_part, _, node = a.target.partition("::")
            self.ws.resolve(path_part)  # must stay inside the repository copy
            if not re.fullmatch(r"[\w./\\:\[\]-]+", a.target):
                raise PathDenied(f"target contains disallowed characters: {a.target!r}")
            cmd += " " + self.sandbox.quote(a.target.replace("\\", "/"))
        res = self.sandbox.run(cmd, self.limits.command_timeout_s, cancel=self._cancel)
        return ToolOutcome("run_command", format_command_result(res), error=not res.ok, command=res,
                           cancelled=res.cancelled)

    def _finish(self, a: FinishArgs) -> ToolOutcome:
        return ToolOutcome("finish", "Finish acknowledged. The harness will now run the final checks.", finished=True)

    # ------------------------------------------------------------ navigation
    def _repo_map(self, a: RepoMapArgs) -> ToolOutcome:
        base = self.ws.resolve(a.path)
        if not base.exists():
            return ToolOutcome("repo_map", f"ERROR: {a.path!r} does not exist.", error=True)
        rel = self.ws.relpath(base)
        return ToolOutcome("repo_map", self.index.repo_map(self.limits.max_output_chars, under=rel))

    def _find_symbol(self, a: SymbolArgs) -> ToolOutcome:
        hits = self.index.find_symbol(a.name)
        if not hits:
            return ToolOutcome("find_symbol", f"No definition of {a.name!r} found in Python modules. Try search.")
        limit = self.limits.max_search_results
        text = "\n".join(h.describe() for h in hits[:limit])
        if len(hits) > limit:
            text += f"\n[... results truncated by harness at {limit} definitions ...]"
        return ToolOutcome("find_symbol", text, truncated=len(hits) > limit)

    def _find_references(self, a: ReferencesArgs) -> ToolOutcome:
        base = self.ws.resolve(a.path)
        refs = self.index.find_references(a.name, self.ws.relpath(base))
        if not refs:
            return ToolOutcome("find_references", f"No references to {a.name!r} found in Python modules.")
        limit = self.limits.max_search_results
        text = "\n".join(f"{r.path}:{r.line}: [{r.kind}] {r.text}" for r in refs[:limit])
        if len(refs) > limit:
            text += f"\n[... results truncated by harness at {limit} references ...]"
        return ToolOutcome("find_references", text, truncated=len(refs) > limit)

    def _module_deps(self, a: ModuleDepsArgs) -> ToolOutcome:
        p = self.ws.resolve(a.path)
        rel = self.ws.relpath(p)
        try:
            m, deps, rdeps = self.index.module_deps(rel)
        except KeyError:
            return ToolOutcome("module_deps", f"ERROR: {a.path!r} is not a Python module of the repository.", error=True)
        lines = [f"{rel} (module {m.module})",
                 "imports: " + (", ".join(deps) or "(no repository modules)"),
                 "imported by: " + (", ".join(rdeps) or "(no repository modules)")]
        internal_roots = {self.index.modules[d].module.split(".")[0] for d in deps}
        ext = sorted({i.split(".")[0] for i in m.imports if i} - internal_roots)
        if ext:
            lines.append("external: " + ", ".join(ext))
        return ToolOutcome("module_deps", "\n".join(lines))

    def _git_status(self, a: NoArgs) -> ToolOutcome:
        return ToolOutcome("git_status", git_state(self.ws, self.start_commit))

    def _git_diff(self, a: GitDiffArgs) -> ToolOutcome:
        diff = self.ws.diff()
        if a.path:
            rel = self.ws.relpath(self.ws.resolve(a.path))
            chunks = re.split(r"(?m)^(?=diff --git )", diff)
            diff = "".join(c for c in chunks if c.split("\n", 1)[0].endswith(f" b/{rel}")
                           or f" b/{rel}/" in c.split("\n", 1)[0])
        return ToolOutcome("git_diff", diff or "(no changes)")

    def _update_plan(self, a: PlanArgs) -> ToolOutcome:
        items = [i.model_dump() for i in a.items]
        done = sum(1 for i in items if i["status"] == "done")
        return ToolOutcome("update_plan", f"Plan recorded ({done}/{len(items)} done).", plan=items)


def format_command_result(res: CommandResult) -> str:
    if res.error:
        status = f"SANDBOX ERROR: {res.error}"
    elif res.cancelled:
        status = "CANCELLED by user (process tree killed; result unknown)"
    elif res.timed_out:
        status = "TIMED OUT (process tree killed)"
    else:
        status = f"exit code {res.exit_code}"
    return f"$ {res.command}\n[{status}, {res.duration_s:.1f}s]\n{res.output}"


def _mini_diff(before: str, after: str, path: str) -> str:
    d = difflib.unified_diff(before.splitlines(), after.splitlines(), f"a/{path}", f"b/{path}", n=1, lineterm="")
    return "\n".join(list(d)[:60])
