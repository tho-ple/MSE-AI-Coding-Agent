"""Lint check: pyflakes on the changed files, reporting only warnings that are NEW
compared to the same file at the starting commit (pristine copy in <checks>/baseline).

    python lint_new.py <checks_dir> <file> [<file> ...]

Exit 0: no new warnings. Exit 1: new warnings (printed). Exit 2: pyflakes missing.
"""
import collections
import io
import re
import sys
from pathlib import Path

try:
    from pyflakes.api import check
    from pyflakes.reporter import Reporter
except ImportError:  # the probe normally catches this first
    print("pyflakes is not installed")
    sys.exit(2)


def messages(source: str, name: str) -> list[str]:
    out, err = io.StringIO(), io.StringIO()
    check(source, name, Reporter(out, err))
    lines = (out.getvalue() + err.getvalue()).splitlines()
    return [ln for ln in lines if ln.strip()]


def key(line: str) -> str:
    # "path:12:1: 'x' imported but unused" -> "'x' imported but unused"
    return re.sub(r"^.*?:\d+:(\d+:)?\s*", "", line)


def main() -> int:
    checks, files = Path(sys.argv[1]), sys.argv[2:]
    new_total = 0
    for f in files:
        cur = messages(Path(f).read_text(encoding="utf-8"), f)
        base_file = checks / "baseline" / f
        base = messages(base_file.read_text(encoding="utf-8"), f) if base_file.exists() else []
        remaining = collections.Counter(key(b) for b in base)
        for line in cur:
            k = key(line)
            if remaining[k]:
                remaining[k] -= 1
                continue
            print(line)
            new_total += 1
    print(f"{new_total} new pyflakes warning(s) in {len(files)} changed file(s)")
    return 1 if new_total else 0


if __name__ == "__main__":
    sys.exit(main())
