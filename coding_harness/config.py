"""Settings: loaded from a YAML file, with secrets only from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class ModelSettings:
    provider: str = "ollama"  # "ollama" | "scripted" (tests only)
    name: str = "qwen2.5-coder:14b"
    base_url: str = "http://localhost:11434"
    temperature: float = 0.0
    num_ctx: int = 16384


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


@dataclass
class Settings:
    model: ModelSettings = field(default_factory=ModelSettings)
    target: TargetSettings = field(default_factory=TargetSettings)
    sandbox: SandboxSettings = field(default_factory=SandboxSettings)
    limits: LimitSettings = field(default_factory=LimitSettings)
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
    # Allow overriding non-secret model settings from the environment.
    model.name = os.environ.get("HARNESS_MODEL", model.name)
    model.base_url = os.environ.get("OLLAMA_HOST", model.base_url)

    return Settings(
        model=model,
        target=target,
        sandbox=sandbox,
        limits=_sub(LimitSettings, raw.get("limits")),
        commands=dict(raw.get("commands") or {}),
        checks=[_sub(CheckSpec, c) for c in raw.get("checks") or []],
        checks_dir=rel(raw.get("checks_dir")),
        baseline_copy=list(raw.get("baseline_copy") or []),
        runs_dir=rel(raw.get("runs_dir", "runs")),
    )
