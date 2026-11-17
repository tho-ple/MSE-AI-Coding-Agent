"""Execution environments for repository commands and tests.

``DockerSandbox`` is the contained backend: no network, CPU/memory/PID limits,
only the workspace (rw) and checks (ro) mounted, killed as a whole on timeout.

``LocalSandbox`` is a development fallback: it scrubs the environment (no
credentials are inherited), runs with the workspace as cwd, and kills the whole
process tree on timeout (Windows Job Object / POSIX process group). It is *not*
a security boundary and the CLI says so loudly when it is used.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

import psutil

from .config import SandboxSettings


@dataclass
class CommandResult:
    command: str
    exit_code: int | None  # None => did not finish (timeout / could not start)
    output: str  # combined stdout+stderr
    duration_s: float
    timed_out: bool = False
    error: str | None = None  # sandbox-level failure (e.g. docker missing)
    cancelled: bool = False  # stopped by the user; process tree killed

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and self.error is None and not self.cancelled


class Sandbox(ABC):
    name: str = "abstract"
    contained: bool = False

    def __init__(self, settings: SandboxSettings, workspace: Path, checks: Path):
        self.settings = settings
        self.workspace = workspace
        self.checks = checks

    def render(self, template: str) -> str:
        """Fill ``{python}``, ``{checks}`` and ``{workspace}`` placeholders (shell-quoted)."""
        q = self.quote
        return template.format(python=q(self.python), checks=q(self.checks_path), workspace=q(self.workspace_path))

    @property
    @abstractmethod
    def python(self) -> str: ...

    @property
    @abstractmethod
    def checks_path(self) -> str: ...

    @property
    @abstractmethod
    def workspace_path(self) -> str: ...

    @abstractmethod
    def run(self, command: str, timeout_s: int, cancel=None) -> CommandResult:
        """Run ``command``; stop it (whole process tree) on timeout or when ``cancel.is_set()``."""

    def describe(self) -> str:
        return self.name

    def quote(self, arg: str) -> str:
        """Quote one argument for the shell that executes commands (sh in containers)."""
        return shlex.quote(arg)


# --------------------------------------------------------------------------- local
# Non-secret variables the OS/interpreter need. Everything else (API keys, tokens...) is dropped.
_SAFE_ENV = ["PATH", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "PROGRAMDATA",
             "ALLUSERSPROFILE", "PROGRAMFILES", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
             "LANG", "LC_ALL", "TZ"]


class _WinJob:
    """Windows Job Object with KILL_ON_JOB_CLOSE so no child survives a timeout."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        self.k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.k32.CreateJobObjectW.restype = wintypes.HANDLE
        self.k32.OpenProcess.restype = wintypes.HANDLE
        self.handle = self.k32.CreateJobObjectW(None, None)

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class EXTENDED(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO_COUNTERS),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        info = EXTENDED()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        self.k32.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info))

    def assign(self, pid: int) -> None:
        h = self.k32.OpenProcess(0x0001 | 0x0100, False, pid)  # TERMINATE | SET_QUOTA
        if h:
            self.k32.AssignProcessToJobObject(self.handle, h)
            self.k32.CloseHandle(h)

    def kill(self) -> None:
        self.k32.TerminateJobObject(self.handle, 1)

    def close(self) -> None:
        self.k32.CloseHandle(self.handle)


class LocalSandbox(Sandbox):
    name = "local"
    contained = False

    @property
    def python(self) -> str:
        return self.settings.python or sys.executable

    @property
    def checks_path(self) -> str:
        return str(self.checks)

    @property
    def workspace_path(self) -> str:
        return str(self.workspace)

    def quote(self, arg: str) -> str:
        return subprocess.list2cmdline([arg]) if os.name == "nt" else shlex.quote(arg)

    def _env(self, home: str) -> dict[str, str]:
        env = {k: os.environ[k] for k in _SAFE_ENV if k in os.environ}
        env.update({"HOME": home, "USERPROFILE": home, "TEMP": home, "TMP": home,
                    "APPDATA": home, "LOCALAPPDATA": home,
                    "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8", "GIT_TERMINAL_PROMPT": "0"})
        env.update(self.settings.env)
        return env

    def run(self, command: str, timeout_s: int, cancel=None) -> CommandResult:
        start = time.monotonic()
        home = tempfile.mkdtemp(prefix="harness-home-")
        out_path = Path(home) / "_output.txt"
        windows = os.name == "nt"
        job = None
        try:
            with open(out_path, "wb") as out:
                kwargs: dict = dict(cwd=self.workspace, env=self._env(home), stdin=subprocess.DEVNULL,
                                    stdout=out, stderr=subprocess.STDOUT, shell=True)
                if windows:
                    kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
                else:
                    kwargs["start_new_session"] = True
                try:
                    proc = subprocess.Popen(command, **kwargs)
                except OSError as e:
                    return CommandResult(command, None, "", time.monotonic() - start, error=str(e))
                if windows:
                    try:
                        job = _WinJob()
                        job.assign(proc.pid)
                    except Exception:  # pragma: no cover - ctypes unavailable
                        job = None
                # Poll instead of a blocking wait so we keep track of every descendant;
                # orphans cannot be found from the tree once their parent has exited.
                seen: dict[int, psutil.Process] = {}
                deadline = start + timeout_s
                cancelled = False
                while proc.poll() is None and time.monotonic() < deadline:
                    self._track(proc.pid, seen)
                    if cancel is not None and cancel.is_set():
                        cancelled = True
                        break
                    time.sleep(0.1)
                stopped = proc.poll() is None
                timed_out = stopped and not cancelled
                # Kill the shell (on timeout/cancel) and anything it left behind (always).
                self._kill(proc, job, seen, only_children=not stopped)
                if stopped:
                    proc.wait(timeout=10)
            output = out_path.read_text(encoding="utf-8", errors="replace")
            return CommandResult(command, None if stopped else proc.returncode, output,
                                 time.monotonic() - start, timed_out=timed_out, cancelled=cancelled and stopped)
        finally:
            if job:
                job.close()
            shutil.rmtree(home, ignore_errors=True)

    @staticmethod
    def _track(pid: int, seen: dict) -> None:
        try:
            for child in psutil.Process(pid).children(recursive=True):
                seen.setdefault(child.pid, child)
        except psutil.Error:
            pass

    def _kill(self, proc: subprocess.Popen, job, seen: dict, only_children: bool = False) -> None:
        # Descendants are collected while the command runs (and once more here), because
        # processes may escape the Job Object (e.g. Microsoft Store Python is activated
        # outside the job) and orphans cannot be found by walking the tree later.
        self._track(proc.pid, seen)
        if os.name == "nt":
            if job:
                job.kill()
        else:
            import signal
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        for p in seen.values():
            try:
                if p.is_running():  # compares create time, so a reused PID is not killed
                    p.kill()
            except psutil.Error:
                pass
        if not only_children and proc.poll() is None:
            proc.kill()


# -------------------------------------------------------------------------- docker
@dataclass
class ContainerEngine:
    """How to reach a container engine: native ``docker``/``podman`` or the one inside WSL."""

    kind: str  # "docker" | "podman"
    prefix: list[str]  # argv prefix, e.g. ["docker"] or ["wsl.exe", "-e", "podman", ...]
    wsl: bool = False

    @property
    def label(self) -> str:
        return f"{'wsl ' if self.wsl else ''}{self.kind}"

    def host_path(self, p: Path) -> str:
        """Path as the engine sees it (``D:/x/y`` becomes ``/mnt/d/x/y`` inside WSL)."""
        p = Path(p).resolve()
        if not self.wsl:
            return str(p)
        drive, rest = p.drive.rstrip(":").lower(), p.as_posix()[len(p.drive):]
        return f"/mnt/{drive}{rest}"


_ENGINE_CACHE: dict[str, "ContainerEngine | None"] = {}
_ENGINE_SPECS = ("docker", "podman", "wsl docker", "wsl podman")


def _engine_for(kind: str, wsl: bool) -> ContainerEngine:
    prefix = ["wsl.exe", "-e", kind] if wsl else [kind]
    if kind == "podman":
        # Quiet rootless warnings; cgroupfs works without systemd (e.g. in WSL).
        prefix += ["--log-level=error", "--cgroup-manager=cgroupfs"]
    return ContainerEngine(kind, prefix, wsl)


def _engine_works(engine: ContainerEngine) -> bool:
    try:
        return subprocess.run(engine.prefix + ["info"], capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def resolve_engine(spec: str = "auto") -> ContainerEngine | None:
    """Find a usable container engine.

    ``spec`` is ``auto`` or one of ``docker``, ``podman``, ``wsl docker``, ``wsl podman``.
    ``auto`` tries them in that order (native first, then inside WSL) and returns the first
    whose ``info`` command succeeds. The environment variable ``HARNESS_CONTAINER_ENGINE``
    overrides the configured value. The result is cached per process.
    """
    spec = os.environ.get("HARNESS_CONTAINER_ENGINE", spec).strip().lower()
    if spec in _ENGINE_CACHE:
        return _ENGINE_CACHE[spec]
    if spec == "auto":
        order = list(_ENGINE_SPECS)
    elif spec in _ENGINE_SPECS:
        order = [spec]
    else:
        raise ValueError(f"unknown container_engine {spec!r}; use auto or one of {_ENGINE_SPECS}")
    found = None
    for candidate in order:
        wsl, kind = candidate.startswith("wsl "), candidate.split()[-1]
        if not shutil.which("wsl.exe" if wsl else kind):
            continue
        engine = _engine_for(kind, wsl)
        if _engine_works(engine):
            found = engine
            break
    _ENGINE_CACHE[spec] = found
    return found


class DockerSandbox(Sandbox):
    """Runs commands in a throw-away container (Docker or Podman, native or inside WSL)."""

    name = "docker"
    contained = True

    def __init__(self, settings: SandboxSettings, workspace: Path, checks: Path):
        super().__init__(settings, workspace, checks)
        self._engine: ContainerEngine | None = None
        self._resolved = False

    @property
    def engine(self) -> ContainerEngine | None:
        if not self._resolved:
            self._engine, self._resolved = resolve_engine(self.settings.container_engine), True
        return self._engine

    @property
    def resource_limits(self) -> bool:
        limits = self.settings.resource_limits
        if isinstance(limits, str):  # "auto": rootless Podman usually cannot apply cgroup limits
            return self.engine is not None and self.engine.kind == "docker"
        return bool(limits)

    def describe(self) -> str:
        if self.engine is None:
            return "docker (no container engine found)"
        return (f"{self.engine.label}, image {self.settings.docker_image}, "
                f"resource limits {'on' if self.resource_limits else 'OFF'}")

    @property
    def python(self) -> str:
        return "python"

    @property
    def checks_path(self) -> str:
        return "/checks"

    @property
    def workspace_path(self) -> str:
        return "/workspace"

    def run(self, command: str, timeout_s: int, cancel=None) -> CommandResult:
        start = time.monotonic()
        engine = self.engine
        if engine is None:
            return CommandResult(command, None, "", 0.0,
                                 error=f"no container engine found (tried {', '.join(_ENGINE_SPECS)})")
        name = f"harness-{uuid.uuid4().hex[:10]}"
        argv = engine.prefix + [
            "run", "--rm", "--name", name,
            "--network", "none",
            "--security-opt", "no-new-privileges", "--cap-drop", "ALL",
            "--read-only", "--tmpfs", "/tmp:rw,size=256m",
            "-e", "HOME=/tmp", "-e", "PYTHONDONTWRITEBYTECODE=1",
            "-v", f"{engine.host_path(self.workspace)}:/workspace:rw",
            "-v", f"{engine.host_path(self.checks)}:/checks:ro",
            "-w", "/workspace",
        ]
        if self.resource_limits:
            argv += ["--memory", self.settings.docker_memory, "--cpus", self.settings.docker_cpus,
                     "--pids-limit", "256"]
        for k, v in self.settings.env.items():
            argv += ["-e", f"{k}={v}"]
        argv += [self.settings.docker_image, "sh", "-c", command]
        out_file = tempfile.TemporaryFile()
        try:
            try:
                proc = subprocess.Popen(argv, stdout=out_file, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
            except OSError as e:
                return CommandResult(command, None, "", 0.0, error=str(e))
            deadline = start + timeout_s
            cancelled = False
            while proc.poll() is None and time.monotonic() < deadline:
                if cancel is not None and cancel.is_set():
                    cancelled = True
                    break
                time.sleep(0.1)
            stopped = proc.poll() is None
            if stopped:  # timeout or cancel: kill the container (all its processes), then the client
                subprocess.run(engine.prefix + ["kill", name], capture_output=True, timeout=60)
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            out_file.seek(0)
            output = out_file.read().decode("utf-8", errors="replace")
            if stopped:
                return CommandResult(command, None, output, time.monotonic() - start,
                                     timed_out=not cancelled, cancelled=cancelled)
            err = None
            if proc.returncode == 125:  # the engine itself failed (image missing, daemon down...)
                err = f"{engine.label} failed to start the container: {output.strip()[:500]}"
            return CommandResult(command, proc.returncode, output, time.monotonic() - start, error=err)
        finally:
            out_file.close()

    def build_image(self, dockerfile: str | Path) -> int:
        """Build ``docker_image`` from ``dockerfile`` with the resolved engine. Returns the exit code."""
        engine = self.engine
        if engine is None:
            raise RuntimeError(f"no container engine found (tried {', '.join(_ENGINE_SPECS)})")
        dockerfile = Path(dockerfile).resolve()
        argv = engine.prefix + ["build", "-t", self.settings.docker_image,
                                "-f", engine.host_path(dockerfile), engine.host_path(dockerfile.parent)]
        return subprocess.run(argv).returncode


def make_sandbox(settings: SandboxSettings, workspace: Path, checks: Path) -> Sandbox:
    backends = {"local": LocalSandbox, "docker": DockerSandbox}
    if settings.backend not in backends:
        raise ValueError(f"unknown sandbox backend {settings.backend!r}; use one of {sorted(backends)}")
    return backends[settings.backend](settings, workspace, checks)

