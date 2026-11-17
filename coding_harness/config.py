"""Settings: loaded from a YAML file, with secrets only from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class ModelSettings:
    provider: str = "ollama"  # "ollama" | "openai" (any OpenAI-compatible API) | "scripted" (tests only)
    name: str = "qwen2.5-coder:14b"
    base_url: str = "http://localhost:11434"
    temperature: float = 0.0
    num_ctx: int = 16384  # Ollama only; a remote server decides its own context window
    api_key_env: str = "OPENAI_API_KEY"  # provider "openai": NAME of the env var holding the key (never the key)
    request_timeout_s: float = 600.0  # provider "openai": per-request timeout


@dataclass
class TargetSettings:
    repo: Path = Path(".")
    commit: str | None = None  # recorded starting commit; checked out into the copy
    writable: list[str] = field(default_factory=lambda: ["**"])  # globs the agent may edit
    ignore: list[str] = field(
        default_factory=lambda: [".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache", "node_modules"]
    )


@dataclass
class SandboxSettings:
    backend: str = "local"  # "docker" (contained) | "local" (dev fallback)
    python: str = "python"  # interpreter used for {python} in commands
    # "auto" | "docker" | "podman" | "wsl docker" | "wsl podman" (env HARNESS_CONTAINER_ENGINE overrides)
    container_engine: str = "auto"
    docker_image: str = "coding-harness-sandbox:latest"
    dockerfile: str | None = None  # used by `harness build-image`
    resource_limits: bool | str = "auto"  # "auto": on for Docker, off for rootless Podman
    docker_memory: str = "1g"
    docker_cpus: str = "1"
    env: dict[str, str] = field(default_factory=dict)  # extra, non-secret env for commands


@dataclass
class CheckSpec:
    name: str
    command: str
    timeout_s: int | None = None
    kind: str = "test"  # "test" | "lint" | "build" (display and grouping only)
    # Optional availability probe. If it fails, the check is reported as "unavailable" (never passed).
    probe: str | None = None
    # Baseline expectation on the starting code (``harness baseline`` verifies it):
    expect_on_start: str | None = None  # e.g. "failed"
    expect_reason: str | None = None  # regex that must appear in the output on the starting code


@dataclass
class LimitSettings:
    max_actions: int = 30  # executed tool calls
    max_denied: int = 5  # rejected tool calls (unknown tool, bad args, out of scope)
    max_retries: int = 3  # model errors / replies without a tool call
    max_output_chars: int = 6000  # per tool result returned to the model
    max_read_chars: int = 20000  # read_file payload before truncation
    max_search_results: int = 50
    max_list_entries: int = 300
    command_timeout_s: int = 120
    # Edit-test-fix loop: how often failed final checks are fed back to the model.
    # 0 = verify once (Stage 1 behaviour). The example configs use 3.
    max_fix_rounds: int = 0
    fix_feedback_chars: int = 2500  # check output per failed check fed back to the model


@dataclass
class ContextSettings:
    """Size limits for what is sent to the model per request (see README "Context")."""

    max_chars: int = 48000  # whole message list per model request (~12k tokens at ~4 chars/token)
    repo_map_chars: int = 3500  # compact repository map in the first message
    selected_files: int = 6  # ranked relevant files listed in the first message
    excerpt_chars: int = 9000  # total budget for file excerpts of the top-ranked files
    excerpt_files: int = 3  # how many top-ranked files get an excerpt
    keep_recent_groups: int = 6  # newest model turns (reply + tool results) kept verbatim
    old_tool_output_chars: int = 500  # older tool results are shortened to this
    summary_chars: int = 3000  # harness summary that replaces dropped turns


@dataclass
class Settings:
    model: ModelSettings = field(default_factory=ModelSettings)
    target: TargetSettings = field(default_factory=TargetSettings)
    sandbox: SandboxSettings = field(default_factory=SandboxSettings)
    limits: LimitSettings = field(default_factory=LimitSettings)
    context: ContextSettings = field(default_factory=ContextSettings)
    # Trusted acceptance criteria shown to the model with the request (never compressed away).
    acceptance_criteria: list[str] = field(default_factory=list)
    # Named commands the agent may run during the loop (e.g. unit tests).
    commands: dict[str, str] = field(default_factory=dict)
    # Checks that always run after the loop. The model cannot skip or edit them.
    checks: list[CheckSpec] = field(default_factory=list)
    # Directory with acceptance checks; copied outside the agent's writable area.
    checks_dir: Path | None = None
    # Paths copied from the *starting* code into the checks area (pristine regression tests).
    baseline_copy: list[str] = field(default_factory=list)
    runs_dir: Path = Path("runs")


def _sub(cls, data: dict[str, Any] | None):
    data = dict(data or {})
    known = cls.__dataclass_fields__.keys()
    unknown = set(data) - set(known)
    if unknown:
        raise ValueError(f"Unknown {cls.__name__} keys: {sorted(unknown)}")
    return cls(**data)


def load_settings(path: str | Path) -> Settings:
    """Load settings; relative paths are resolved against the config file's directory."""
    path = Path(path).resolve()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    base = path.parent

    def rel(p: str | None) -> Path | None:
        if p is None:
            return None
        q = Path(os.path.expandvars(p))
        return q if q.is_absolute() else (base / q).resolve()

    target = _sub(TargetSettings, raw.get("target"))
    target.repo = rel(str(target.repo))
    sandbox = _sub(SandboxSettings, raw.get("sandbox"))
    # Interpreter given as a relative path (e.g. a venv) is resolved like other paths.
    if any(sep in sandbox.python for sep in ("/", "\\")):
        py = rel(sandbox.python)
        # Same config on Windows (venv/Scripts/python.exe) and POSIX (venv/bin/python).
        if not py.exists():
            alt = py.parent.parent / ("bin/python" if py.parent.name == "Scripts" else "Scripts/python.exe")
            py = alt if alt.exists() else py
        sandbox.python = str(py)

    if sandbox.dockerfile:
        sandbox.dockerfile = str(rel(sandbox.dockerfile))

    model = _sub(ModelSettings, raw.get("model"))
    # Allow overriding non-secret model settings from the environment (.env is loaded by the CLI).
    provider = os.environ.get("HARNESS_PROVIDER", model.provider).strip().lower()
    if provider != model.provider:
        model.provider = provider
        if provider == "openai":
            model.base_url = ""  # the Ollama URL from the config does not apply
    model.name = os.environ.get("HARNESS_MODEL", model.name)
    url_env = "OPENAI_BASE_URL" if model.provider == "openai" else "OLLAMA_HOST"
    model.base_url = os.environ.get(url_env, model.base_url)

    return Settings(
        model=model,
        target=target,
        sandbox=sandbox,
        limits=_sub(LimitSettings, raw.get("limits")),
        context=_sub(ContextSettings, raw.get("context")),
        acceptance_criteria=[str(c) for c in raw.get("acceptance_criteria") or []],
        commands=dict(raw.get("commands") or {}),
        checks=[_sub(CheckSpec, c) for c in raw.get("checks") or []],
        checks_dir=rel(raw.get("checks_dir")),
        baseline_copy=list(raw.get("baseline_copy") or []),
        runs_dir=rel(raw.get("runs_dir", "runs")),
    )


# ------------------------------------------------------------------ allowed projects
@dataclass
class ProjectEntry:
    name: str
    config: Path
    description: str = ""


@dataclass
class ProjectRegistry:
    """The projects a user may select. Anything not listed here cannot be started or resumed."""

    path: Path
    sessions_dir: Path
    projects: dict[str, ProjectEntry]

    def get(self, name: str) -> ProjectEntry:
        if name not in self.projects:
            allowed = ", ".join(sorted(self.projects)) or "none"
            raise KeyError(f"project {name!r} is not in the allowed projects ({allowed}) of {self.path}")
        return self.projects[name]


def load_projects(path: str | Path) -> ProjectRegistry:
    path = Path(path).resolve()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    base = path.parent
    unknown = set(raw) - {"sessions_dir", "projects"}
    if unknown:
        raise ValueError(f"Unknown keys in {path.name}: {sorted(unknown)}")
    projects = {}
    for name, entry in (raw.get("projects") or {}).items():
        if not isinstance(entry, dict) or "config" not in entry:
            raise ValueError(f"project {name!r} needs a 'config' path")
        cfg = Path(entry["config"])
        projects[name] = ProjectEntry(name, cfg if cfg.is_absolute() else (base / cfg).resolve(),
                                      str(entry.get("description", "")))
    sessions = Path(raw.get("sessions_dir", "runs"))
    return ProjectRegistry(path, sessions if sessions.is_absolute() else (base / sessions).resolve(), projects)


# ------------------------------------------------------------------ .env
def load_dotenv(path: str | Path) -> list[str]:
    """Load ``KEY=VALUE`` lines from a local ``.env`` into ``os.environ``.

    Variables that are already set win. Returns the names that were set (never values).
    The file is git-ignored; it is the place for the model API key. Only the model client
    reads the key: sandboxes get an allow-listed environment and never see it.
    """
    path = Path(path)
    if not path.is_file():
        return []
    loaded = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded
