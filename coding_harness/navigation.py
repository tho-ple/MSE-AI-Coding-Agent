"""Language-aware repository navigation and read-only Git state.

Python files are parsed with :mod:`ast` (no code is imported or executed):

* symbols    - classes (with their methods), functions and UPPER_CASE constants
* imports    - absolute and relative imports, resolved to modules of the repository
* references - names, attributes and imported names that mention a symbol
* deps       - which repository modules a module imports, and which import it

Other files are listed but not parsed. The index is rebuilt for files whose
size/mtime changed, so it stays correct after the agent edits code.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .workspace import Workspace, _git


@dataclass
class Symbol:
    name: str
    qualname: str
    kind: str  # class | function | method | constant
    path: str
    line: int
    signature: str = ""

    def describe(self) -> str:
        return f"{self.path}:{self.line}: {self.kind} {self.qualname}{self.signature}"


@dataclass
class Reference:
    path: str
    line: int
    kind: str  # import | call | use | definition
    text: str


@dataclass
class ModuleInfo:
    path: str  # posix path relative to the repository root
    module: str  # dotted module name
    symbols: list[Symbol] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)  # dotted names, relative imports resolved
    deps: set[str] = field(default_factory=set)  # repository module paths this module imports
    error: str | None = None  # syntax error, if the file could not be parsed
    tree: ast.AST | None = None
    lines: list[str] = field(default_factory=list)

    @property
    def is_test(self) -> bool:
        parts = PurePosixPath(self.path).parts
        return "tests" in parts or "test" in parts or parts[-1].startswith("test_")


def module_name(rel: str, root: Path) -> str:
    """``src/allocation/domain/model.py`` -> ``allocation.domain.model`` (stops at the first non-package dir)."""
    p = PurePosixPath(rel)
    dirs = list(p.parent.parts)
    k, cur = 0, (root / p).parent
    while k < len(dirs) and (cur / "__init__.py").exists():
        k += 1
        cur = cur.parent
    parts = dirs[len(dirs) - k:]
    if p.stem != "__init__":
        parts.append(p.stem)
    return ".".join(parts) or p.stem


class RepoIndex:
    def __init__(self, workspace: Workspace):
        self.ws = workspace
        self.modules: dict[str, ModuleInfo] = {}  # by path
        self.other_files: list[str] = []
        self._stamp: dict[str, tuple[int, int]] = {}
        self.refresh()

    # ----------------------------------------------------------------- build
    def refresh(self) -> "RepoIndex":
        seen, others = set(), []
        for p in self.ws.iter_files():
            rel = self.ws.relpath(p)
            if p.suffix != ".py":
                others.append(rel)
                continue
            seen.add(rel)
            st = p.stat()
            stamp = (st.st_size, st.st_mtime_ns)
            if self._stamp.get(rel) != stamp:
                self.modules[rel] = self._parse(p, rel)
                self._stamp[rel] = stamp
        for gone in set(self.modules) - seen:
            self.modules.pop(gone, None)
            self._stamp.pop(gone, None)
        self.other_files = others
        self._resolve_deps()
        return self

    def _parse(self, p: Path, rel: str) -> ModuleInfo:
        info = ModuleInfo(rel, module_name(rel, self.ws.root))
        try:
            text = p.read_text(encoding="utf-8")
            info.lines = text.splitlines()
            tree = ast.parse(text, filename=rel)
        except (SyntaxError, UnicodeDecodeError, ValueError) as e:
            info.error = f"{type(e).__name__}: {e}"
            return info
        info.tree = tree
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                bases = ", ".join(_unparse(b) for b in node.bases)
                info.symbols.append(Symbol(node.name, node.name, "class", rel, node.lineno, f"({bases})" if bases else ""))
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        info.symbols.append(Symbol(item.name, f"{node.name}.{item.name}", "method", rel,
                                                   item.lineno, f"({_args(item)})"))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                info.symbols.append(Symbol(node.name, node.name, "function", rel, node.lineno, f"({_args(node)})"))
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    if isinstance(t, ast.Name) and t.id.isupper():
                        info.symbols.append(Symbol(t.id, t.id, "constant", rel, node.lineno))
        package = info.module if rel.endswith("__init__.py") else info.module.rpartition(".")[0]
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                info.imports += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    parts = package.split(".") if package else []
                    parts = parts[: len(parts) - (node.level - 1)] if node.level > 1 else parts
                    base = ".".join(parts + ([node.module] if node.module else []))
                else:
                    base = node.module or ""
                # "from pkg import mod" -> pkg.mod; "from pkg.mod import Name" -> pkg.mod.Name, which
                # resolves to pkg.mod by prefix. Only a star import records the base itself.
                if any(a.name == "*" for a in node.names):
                    info.imports.append(base)
                info.imports += [f"{base}.{a.name}" if base else a.name for a in node.names if a.name != "*"]
        return info

    def _resolve_deps(self) -> None:
        by_module = {m.module: path for path, m in self.modules.items()}
        for m in self.modules.values():
            m.deps = set()
            for name in m.imports:
                parts = name.split(".")
                while parts:
                    target = by_module.get(".".join(parts))
                    if target:
                        if target != m.path:
                            m.deps.add(target)
                        break
                    parts.pop()

    # --------------------------------------------------------------- queries
    def find_symbol(self, name: str) -> list[Symbol]:
        """Definitions whose name or qualified name matches ``name`` (``Product.allocate`` or ``allocate``)."""
        out = []
        for m in self.modules.values():
            for s in m.symbols:
                if name in (s.name, s.qualname):
                    out.append(s)
        return sorted(out, key=lambda s: (s.path, s.line))

    def find_references(self, name: str, under: str = ".") -> list[Reference]:
        """Places that mention ``name``: imports, calls, attribute/name uses and the definitions themselves."""
        short = name.split(".")[-1]
        refs: list[Reference] = []
        for m in self.modules.values():
            if m.tree is None or not _under(m.path, under):
                continue
            hits: dict[int, str] = {}
            for node in ast.walk(m.tree):
                kind = None
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == short:
                    kind = "definition"
                elif isinstance(node, ast.ImportFrom) and any(a.name == short for a in node.names):
                    kind = "import"
                elif isinstance(node, ast.Call):
                    f = node.func
                    if (isinstance(f, ast.Name) and f.id == short) or (isinstance(f, ast.Attribute) and f.attr == short):
                        kind = "call"
                elif isinstance(node, ast.Attribute) and node.attr == short:
                    kind = "use"
                elif isinstance(node, ast.Name) and node.id == short:
                    kind = "use"
                if kind:
                    line = getattr(node, "lineno", 0)
                    # Strongest kind per line wins (definition > import > call > use).
                    order = ["use", "call", "import", "definition"]
                    if line not in hits or order.index(kind) > order.index(hits[line]):
                        hits[line] = kind
            for line, kind in sorted(hits.items()):
                text = m.lines[line - 1].strip() if 0 < line <= len(m.lines) else ""
                refs.append(Reference(m.path, line, kind, text[:160]))
        return sorted(refs, key=lambda r: (r.path, r.line))

    def module_deps(self, path: str) -> tuple[ModuleInfo, list[str], list[str]]:
        m = self.modules.get(path)
        if m is None:
            raise KeyError(path)
        imported_by = sorted(p for p, other in self.modules.items() if path in other.deps)
        return m, sorted(m.deps), imported_by

    # ------------------------------------------------------------ summaries
    def outline(self, path: str) -> str:
        m = self.modules[path]
        if m.error:
            return f"{path}  [parse error: {m.error[:80]}]"
        parts = []
        for s in m.symbols:
            if s.kind == "class":
                methods = [x.name for x in m.symbols if x.kind == "method" and x.qualname.startswith(s.name + ".")]
                parts.append(f"class {s.name}" + (f"({', '.join(methods)})" if methods else ""))
            elif s.kind == "function":
                parts.append(f"def {s.name}")
            elif s.kind == "constant":
                parts.append(s.name)
        return f"{path}: " + ("; ".join(parts) if parts else "(no top-level symbols)")

    def repo_map(self, max_chars: int, focus: list[str] | tuple[str, ...] = (), under: str = ".") -> str:
        """Compact map: one line per module with its symbols; ``focus`` modules first, then the rest."""
        paths = [p for p in self.modules if _under(p, under)]
        ordered = [p for p in focus if p in paths] + sorted(p for p in paths if p not in focus)
        head = (f"Repository map: {len(paths)} Python modules"
                f" ({sum(1 for p in paths if self.modules[p].is_test)} test modules).")
        lines, used, shown = [head], len(head), 0
        for p in ordered:
            line = self.outline(p)
            if len(line) > 300:
                line = line[:297] + "..."
            if used + len(line) + 1 > max_chars - 120:
                break
            lines.append(line)
            used += len(line) + 1
            shown += 1
        if shown < len(ordered):
            lines.append(f"[... {len(ordered) - shown} more modules not shown; use repo_map with a path, list_files or search ...]")
        others = [f for f in self.other_files if _under(f, under)]
        if others:
            names = ", ".join(others[:15]) + (f", ... (+{len(others) - 15})" if len(others) > 15 else "")
            extra = f"Other files: {names}"
            if used + len(extra) < max_chars:
                lines.append(extra)
        return "\n".join(lines)

    # --------------------------------------------------------- file selection
    def select_files(self, task: str, k: int = 6) -> list[tuple[str, float, list[str]]]:
        """Rank modules by relevance to the task text.

        Score = path words (3 each) + defined symbol names (5 per exact match, 1 per word match)
        + term frequency in the file (capped) + 20 if the path is named in the task.
        Test modules count half. Modules that the best matches import, or that import them,
        get 30% of that match's score, so a feature's interacting modules are selected together.
        """
        terms = task_terms(task)
        if not terms:
            return []
        lowered_task = task.lower().replace("\\", "/")
        scores: dict[str, float] = {}
        reasons: dict[str, list[str]] = {}
        for path, m in self.modules.items():
            score, why = 0.0, []
            pwords = _words(PurePosixPath(path).with_suffix("").as_posix())
            hit = sorted(pwords & terms)
            if hit:
                score += 3 * len(hit)
                why.append("path: " + ", ".join(hit))
            if path.lower() in lowered_task or PurePosixPath(path).name.lower() in lowered_task.split():
                score += 20
                why.append("named in task")
            sym_hits = []
            for s in m.symbols:
                if s.name.lower() in terms:
                    score += 5
                    sym_hits.append(s.qualname)
                else:
                    w = _words(s.name) & terms
                    if w:
                        score += min(len(w), 2) * 1.0
            if sym_hits:
                why.append("defines " + ", ".join(sorted(set(sym_hits))[:5]))
            if m.lines:
                body = _word_list("\n".join(m.lines))
                tf = sum(1 for w in body if w in terms)
                if tf:
                    score += min(tf, 40) * 0.25
            if m.is_test:
                score *= 0.5
            if score > 0:
                scores[path], reasons[path] = score, why
        top = sorted(scores, key=lambda p: -scores[p])[:3]
        for p in top:
            m, deps, rdeps = self.module_deps(p)
            for d, label in [(d, f"imported by {p}") for d in deps] + [(r, f"imports {p}") for r in rdeps]:
                bonus = 0.3 * scores[p]
                scores[d] = scores.get(d, 0.0) + bonus
                reasons.setdefault(d, []).append(label)
        ranked = sorted(scores, key=lambda p: (-scores[p], p))[:k]
        return [(p, round(scores[p], 1), reasons.get(p, [])) for p in ranked]


# --------------------------------------------------------------------- git state
def git_state(ws: Workspace, start_commit: str | None = None) -> str:
    """Read-only Git view of the disposable copy: baseline commit, changed files, diff stat."""
    head = _git(ws.root, "log", "-1", "--format=%h %s").stdout.strip()
    changed = ws.changed_files()
    stat = _git(ws.root, "diff", "--cached", "--stat", "HEAD").stdout.strip()
    lines = [f"Workspace baseline commit: {head} (export of target commit {start_commit or 'working tree'})",
             "Remotes: none (push/merge/deploy are disabled)",
             f"Changed files vs. baseline: {len(changed)}"]
    lines += [f"  {c}" for c in changed]
    if stat:
        lines += ["Diff stat:", stat]
    return "\n".join(lines)


# ------------------------------------------------------------------- helpers
_STOP = {"the", "and", "for", "with", "that", "this", "from", "into", "must", "should", "when", "then", "than",
         "are", "is", "be", "not", "new", "add", "use", "using", "only", "also", "its", "it", "an", "a", "of",
         "to", "in", "on", "or", "if", "by", "as", "at", "no", "any", "all", "can", "may", "will", "keep",
         "existing", "change", "changes", "file", "files", "test", "tests", "code", "run", "py", "src", "none",
         "true", "false", "self", "return", "raise", "import", "def", "class", "str", "int", "you", "your"}


def _split_ident(word: str) -> list[str]:
    out = []
    for part in re.split(r"[_\W]+", word):
        out += re.findall(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+", part)
    return [w.lower() for w in out if w]


def _stem(w: str) -> str:
    for suf in ("ations", "ation", "ing", "ed", "es", "s"):
        if len(w) > len(suf) + 3 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def _word_list(text: str) -> list[str]:
    words = []
    for ident in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text):
        words += [_stem(w) for w in _split_ident(ident)]
    return words


def _words(text: str) -> set[str]:
    return {w for w in _word_list(text) if len(w) > 2 and w not in _STOP}


def task_terms(task: str) -> set[str]:
    """Identifier-ish words of the task: split camelCase/snake_case, lightly stemmed, stop words removed."""
    terms = _words(task)
    for ident in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", task):
        if ident.lower() not in _STOP:
            terms.add(ident.lower())
    return terms


def _under(path: str, base: str) -> bool:
    base = base.strip().replace("\\", "/").strip("/")
    return base in ("", ".") or path == base or path.startswith(base + "/")


def _unparse(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:  # pragma: no cover
        return "?"


def _args(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    text = _unparse(fn.args)
    return text if len(text) <= 80 else text[:77] + "..."
