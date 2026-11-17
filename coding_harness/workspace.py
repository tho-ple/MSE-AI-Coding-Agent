"""Disposable repository copy and the path policy every file tool goes through."""

from __future__ import annotations

import fnmatch
import hashlib
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .config import Settings


class PathDenied(Exception):
    """A tool asked for a path outside the allowed scope."""


GIT_ENV_FLAGS = ["-c", "safe.directory=*", "-c", "core.autocrlf=false", "-c", "core.hooksPath=NUL"]
_EXCLUDES = ["__pycache__/", "*.pyc", ".pytest_cache/", ".mypy_cache/"]


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *GIT_ENV_FLAGS, *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=check,
    )


@dataclass
class RunDirs:
    root: Path  # runs/<id>
    workspace: Path  # disposable repo copy (agent-writable scope)
    checks: Path  # acceptance + baseline tests (never visible to file tools)


class Workspace:
    """A disposable copy of the target repository.

    All file tools resolve paths through :meth:`resolve`, which rejects absolute
    paths, ``..`` escapes, symlinks leaving the copy, the copy's ``.git`` folder,
    and (for writes) anything not matching the configured ``writable`` globs.
    """

    def __init__(self, root: Path, writable: list[str], ignore: list[str]):
        self.root = root.resolve()
        self.writable = writable
        self.ignore = set(ignore)

    # ---------------------------------------------------------------- creation
    @classmethod
    def create(cls, settings: Settings, run_id: str | None = None) -> tuple["Workspace", RunDirs]:
        run_id = run_id or uuid.uuid4().hex[:8]
        root = (settings.runs_dir / run_id).resolve()
        if root.exists():
            shutil.rmtree(root)
        dirs = RunDirs(root=root, workspace=root / "workspace", checks=root / "checks")
        src = settings.target.repo.resolve()

        if settings.target.commit and (src / ".git").exists():
            # Export exactly the recorded starting commit, not whatever is in the working tree.
            dirs.workspace.mkdir(parents=True)
            archive = root / "start.tar"
            _git(src, "archive", "--format=tar", "-o", str(archive), settings.target.commit)
            shutil.unpack_archive(archive, dirs.workspace, filter="data")
            archive.unlink()
        else:
            shutil.copytree(src, dirs.workspace, ignore=shutil.ignore_patterns(*settings.target.ignore))

        # Fresh, remote-less git repo used only to compute the diff. No push target exists.
        _git(dirs.workspace, "init", "-q")
        (dirs.workspace / ".git" / "info").mkdir(parents=True, exist_ok=True)
        (dirs.workspace / ".git" / "info" / "exclude").write_text("\n".join(_EXCLUDES) + "\n")
        _git(dirs.workspace, "add", "-A")
        _git(dirs.workspace, "-c", "user.name=harness", "-c", "user.email=harness@localhost",
             "commit", "-q", "--no-verify", "-m", "baseline")

        dirs.checks.mkdir(parents=True)
        if settings.checks_dir:
            shutil.copytree(settings.checks_dir, dirs.checks, dirs_exist_ok=True)
        for rel in settings.baseline_copy:
            s = dirs.workspace / rel
            d = dirs.checks / "baseline" / rel
            if s.is_dir():
                shutil.copytree(s, d, ignore=shutil.ignore_patterns("__pycache__"))
            elif s.is_file():
                d.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(s, d)

        return cls(dirs.workspace, settings.target.writable, settings.target.ignore), dirs

    @classmethod
    def open(cls, settings: Settings, root: Path) -> "Workspace":
        """Reopen the disposable copy of an existing session (no re-export)."""
        if not (root / ".git").is_dir():
            raise FileNotFoundError(f"no workspace copy at {root}")
        return cls(root, settings.target.writable, settings.target.ignore)

    # ------------------------------------------------------------ path policy
    def resolve(self, rel: str, *, write: bool = False) -> Path:
        if not isinstance(rel, str) or not rel.strip():
            raise PathDenied("path must be a non-empty string")
        rel = rel.strip().replace("\\", "/")
        if rel.startswith("/") or (len(rel) > 1 and rel[1] == ":") or rel.startswith("~"):
            raise PathDenied(f"absolute paths are not allowed: {rel!r}")
        if "\x00" in rel:
            raise PathDenied("path contains NUL byte")
        target = (self.root / rel).resolve()
        if target != self.root and not target.is_relative_to(self.root):
            raise PathDenied(f"path escapes the repository copy: {rel!r}")
        posix = self.relpath(target)
        parts = PurePosixPath(posix).parts
        if parts and parts[0] == ".git":
            raise PathDenied("the .git directory is not accessible")
        if write and not self.is_writable(posix):
            raise PathDenied(f"path is outside the writable scope {self.writable}: {posix!r}")
        return target

    def relpath(self, p: Path) -> str:
        r = p.resolve().relative_to(self.root).as_posix()
        return "." if r == "" else r

    def is_writable(self, posix: str) -> bool:
        return any(fnmatch.fnmatchcase(posix, g) for g in self.writable)

    def is_ignored(self, p: Path) -> bool:
        return any(part in self.ignore for part in p.relative_to(self.root).parts)

    def iter_files(self, base: Path | None = None):
        """Files under ``base`` (default: the copy), skipping ignored dirs, .git and escaping symlinks."""
        base = base or self.root
        if base.is_file():
            yield base
            return
        for p in sorted(base.rglob("*")):
            if p.is_file() and not self.is_ignored(p) and self.relpath(p).split("/")[0] != ".git":
                if p.resolve().is_relative_to(self.root):
                    yield p

    def fingerprint(self) -> dict[str, str]:
        """Content hash of every file in the copy (the saved working-copy state used on resume)."""
        out = {}
        for p in self.iter_files():
            if p.suffix == ".pyc":
                continue
            out[self.relpath(p)] = hashlib.sha256(p.read_bytes()).hexdigest()[:16]
        return out

    def head(self) -> str:
        """Commit id of the copy's baseline commit (changes if someone commits inside the copy)."""
        return _git(self.root, "rev-parse", "HEAD").stdout.strip()

    # ------------------------------------------------------------------- diff
    def diff(self) -> str:
        _git(self.root, "add", "-A")
        return _git(self.root, "diff", "--cached", "--no-color", "HEAD").stdout

    def changed_files(self) -> list[str]:
        _git(self.root, "add", "-A")
        out = _git(self.root, "diff", "--cached", "--name-status", "HEAD").stdout
        return [line.replace("\t", " ") for line in out.splitlines() if line.strip()]
