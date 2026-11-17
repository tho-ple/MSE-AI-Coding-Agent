"""Saved sessions: task record, checkpoints, a write-ahead step journal, cancellation and locking.

Layout of one session (``<sessions_dir>/<id>/``)::

    session.json   task record + latest checkpoint (atomic replace on every save)
    steps.jsonl    append-only journal: action_start / action_done, model replies, checks,
                   cancel / pause / resume decisions (fsync'd per record)
    events.jsonl   UI event log
    workspace/     the disposable repository copy (the harness's edits live here)
    checks/        acceptance + baseline checks, outside the writable area
    cancel         flag file written by ``harness cancel``; the running process polls it
    lock           pid of the process currently running the session
    diff.patch, report.json   written when a run stops (completed, cancelled or paused)

Every side-effecting tool call is journaled *before* it runs (``action_start``) and after
(``action_done``, with its result and the new working-copy fingerprint). A start without a done
record means the process died mid-action: the action is *uncertain* and is never replayed
automatically.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psutil


class Cancelled(Exception):
    """Raised inside the controller when the user cancelled the run."""


class Paused(Exception):
    """Raised when the controller must stop and wait for a user decision."""


class CancelToken:
    """Set from this process (Ctrl+C, tests) or from another process via the session's flag file."""

    def __init__(self, flag_file: Path | None = None):
        self._event = threading.Event()
        self.flag_file = flag_file

    def set(self) -> None:
        self._event.set()

    def is_set(self) -> bool:
        if not self._event.is_set() and self.flag_file is not None and self.flag_file.exists():
            self._event.set()
        return self._event.is_set()

    def raise_if_set(self) -> None:
        if self.is_set():
            raise Cancelled("cancelled by user")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_write_json(path: Path, data: Any) -> None:
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, default=str)
        f.flush()
        os.fsync(f.fileno())
    for attempt in range(20):  # Windows: a reader may hold the file briefly
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.05 * (attempt + 1))
    os.replace(tmp, path)


def diff_fingerprints(expected: dict[str, str], actual: dict[str, str]) -> list[str]:
    out = []
    for p in sorted(set(expected) | set(actual)):
        if p not in actual:
            out.append(f"deleted {p}")
        elif p not in expected:
            out.append(f"added {p}")
        elif expected[p] != actual[p]:
            out.append(f"modified {p}")
    return out


SIDE_EFFECT_TOOLS = {"edit_file", "write_file", "run_command"}
_HELD: set[str] = set()  # session dirs locked by this process


class Session:
    def __init__(self, root: Path, data: dict):
        self.root = root
        self.data = data
        self._seq = self._last_seq()

    # ------------------------------------------------------------- create/load
    @classmethod
    def create(cls, root: Path, data: dict) -> "Session":
        root.mkdir(parents=True, exist_ok=True)
        data = {"version": 2, "created": now(), "updated": now(), "status": "created", **data}
        s = cls(root, data)
        s.save()
        return s

    @classmethod
    def load(cls, root: Path) -> "Session":
        path = Path(root) / "session.json"
        if not path.exists():
            raise FileNotFoundError(f"no session at {root}")
        return cls(Path(root), json.loads(path.read_text(encoding="utf-8")))

    @property
    def id(self) -> str:
        return self.data["id"]

    @property
    def workspace(self) -> Path:
        return self.root / "workspace"

    @property
    def checks_dir(self) -> Path:
        return self.root / "checks"

    @property
    def cancel_flag(self) -> Path:
        return self.root / "cancel"

    def save(self, **updates) -> None:
        self.data.update(updates)
        self.data["updated"] = now()
        atomic_write_json(self.root / "session.json", self.data)

    # ----------------------------------------------------------------- journal
    def _last_seq(self) -> int:
        """Sequence number of the last complete record (reads only the end of the file)."""
        path = self.root / "steps.jsonl"
        if not path.exists():
            return 0
        with open(path, "rb") as f:
            size = f.seek(0, os.SEEK_END)
            chunk = 1 << 16
            while True:
                f.seek(max(size - chunk, 0))
                lines = f.read().splitlines()
                for line in reversed(lines[1:] if size > chunk else lines):
                    try:
                        return int(json.loads(line)["seq"])
                    except (ValueError, KeyError, TypeError):
                        continue  # torn line after a crash
                if chunk >= size:
                    return 0
                chunk *= 4

    def journal(self, type_: str, **data) -> dict:
        # Another process (e.g. `harness cancel`) may have appended since our last write.
        self._seq = max(self._seq, self._last_seq()) + 1
        rec = {"seq": self._seq, "ts": now(), "type": type_, **data}
        with open(self.root / "steps.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str) + "\n")
            f.flush()
            os.fsync(f.fileno())
        return rec

    def read_journal(self) -> list[dict]:
        path = self.root / "steps.jsonl"
        if not path.exists():
            return []
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:  # torn last line after a crash
                continue
        return out

    def completed_actions(self) -> dict[str, dict]:
        return {r["call_id"]: r for r in self.read_journal() if r["type"] == "action_done"}

    def uncertain_actions(self) -> list[dict]:
        """Actions that were started but never recorded as done (the process died mid-action)."""
        recs = self.read_journal()
        done = {r["call_id"] for r in recs if r["type"] == "action_done"}
        return [r for r in recs if r["type"] == "action_start" and r["call_id"] not in done]

    # ------------------------------------------------------------- lock/cancel
    def lock_owner(self) -> int | None:
        """PID of a live process running this session, or None (no lock or stale lock)."""
        path = self.root / "lock"
        try:
            pid = int(path.read_text().strip())
        except (OSError, ValueError):
            return None
        if pid == os.getpid():
            return pid if str(self.root.resolve()) in _HELD else None
        return pid if psutil.pid_exists(pid) else None

    def acquire(self) -> None:
        owner = self.lock_owner()
        if owner is not None:
            raise RuntimeError(f"session {self.id} is being run by process {owner}")
        (self.root / "lock").write_text(str(os.getpid()))
        _HELD.add(str(self.root.resolve()))

    def release(self) -> None:
        _HELD.discard(str(self.root.resolve()))
        try:
            (self.root / "lock").unlink()
        except OSError:
            pass

    def request_cancel(self) -> None:
        self.cancel_flag.write_text(now())

    def clear_cancel(self) -> None:
        try:
            self.cancel_flag.unlink()
        except OSError:
            pass


class SessionStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def new_id(self, prefix: str = "s") -> str:
        return f"{prefix}-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"

    def find(self, ref: str) -> Path:
        """Session directory from an id, unique id prefix, or path."""
        p = Path(ref)
        if (p / "session.json").exists():
            return p.resolve()
        if (self.root / ref / "session.json").exists():
            return (self.root / ref).resolve()
        matches = [d for d in self.root.glob(f"{ref}*") if (d / "session.json").exists()] if self.root.exists() else []
        if len(matches) == 1:
            return matches[0].resolve()
        if len(matches) > 1:
            raise KeyError(f"session id {ref!r} is ambiguous: {', '.join(m.name for m in matches)}")
        raise KeyError(f"no session {ref!r} in {self.root}")

    def list(self) -> list[dict]:
        if not self.root.exists():
            return []
        out = []
        for d in sorted(self.root.iterdir()):
            f = d / "session.json"
            if f.exists():
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                out.append({"id": data.get("id"), "project": data.get("project"), "status": data.get("status"),
                            "outcome": (data.get("state") or {}).get("outcome"), "updated": data.get("updated"),
                            "request": (data.get("task") or {}).get("request", "")[:60], "dir": str(d)})
        return out
