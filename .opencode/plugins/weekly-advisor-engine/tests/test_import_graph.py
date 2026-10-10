"""Import-graph regression fence: the package must stay acyclic.

Two graphs are checked, and they answer different questions:

  * module-level — imports that execute at import time. Collection stops at
    ``def`` / ``class`` bodies, so a function-local import never counts.
  * deferred-inclusive — adds function-local imports. This is the view the
    ``repowise`` index reports, so it is the one that must stay at 0.

The module-level graph is a strict subgraph of the deferred one, so it cannot
find a cycle the deferred graph misses. Its value is diagnostic: when the
deferred fence fails, a green module-level fence localises the cycle to a
function body rather than to a top-of-file import.

Three self-tests run first, so a broken resolver and a broken collection
boundary both fail loudly instead of reporting "0 cycles" on a bad path:
`test_checker_detects_a_known_cycle` (the resolver bites),
`test_checker_ignores_deferred_imports_for_module_level` (a cycle living only
inside a function body is not module-level), and
`test_checker_resolves_relative_imports_in_subpackages` (relative imports
resolve against the *owning package*, including at `level > 1` and when an
alias names a submodule).
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "weekly_telemetry_aggregator"

# A `def` / `class` body is the only place an `ImportFrom` node can hide from
# module level: `from x import y` is a statement, so it can never appear inside
# a decorator, a default or an annotation expression.
_BODY_OWNERS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _modules(root: Path) -> dict[str, Path]:
    return {
        ".".join(p.relative_to(root.parent).with_suffix("").parts).removesuffix(".__init__"): p
        for p in sorted(root.rglob("*.py"))
    }


def _owning_package(module: str) -> str:
    """Package a module belongs to; an `__init__.py` owns itself."""
    return module.rsplit(".", 1)[0] if "." in module else module


def _resolve(module: str, node: ast.ImportFrom, known: set[str]) -> set[str]:
    level = node.level or 0
    if level:
        base = _owning_package(module)
        for _ in range(level - 1):
            base = _owning_package(base)
        target = f"{base}.{node.module}" if node.module else base
    else:
        target = node.module or ""
    out = {target} if target in known else set()
    for alias in node.names:  # `from . import x` may name a submodule
        candidate = f"{target}.{alias.name}"
        if candidate in known:
            out.add(candidate)
    return out


def _import_from(tree: ast.Module, *, deferred: bool) -> Iterator[ast.ImportFrom]:
    if deferred:
        yield from (n for n in ast.walk(tree) if isinstance(n, ast.ImportFrom))
        return
    for stmt in tree.body:
        if isinstance(stmt, _BODY_OWNERS):
            continue
        yield from (n for n in ast.walk(stmt) if isinstance(n, ast.ImportFrom))


def _graph(root: Path, *, deferred: bool) -> dict[str, set[str]]:
    files = _modules(root)
    known = set(files)
    graph: dict[str, set[str]] = {m: set() for m in known}
    for module, path in files.items():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in _import_from(tree, deferred=deferred):
            graph[module] |= _resolve(module, node, known)
    return graph


def _cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    """Iterative Tarjan; returns SCCs of size > 1."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    out: list[list[str]] = []
    counter = 0
    for start in sorted(graph):
        if start in index:
            continue
        index[start] = low[start] = counter
        counter += 1
        stack.append(start)
        on_stack.add(start)
        work = [(start, iter(graph[start]))]
        while work:
            node, it = work[-1]
            advanced = False
            for nxt in it:
                if nxt not in index:
                    index[nxt] = low[nxt] = counter
                    counter += 1
                    stack.append(nxt)
                    on_stack.add(nxt)
                    work.append((nxt, iter(graph[nxt])))
                    advanced = True
                    break
                if nxt in on_stack:
                    low[node] = min(low[node], index[nxt])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                comp = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    comp.append(member)
                    if member == node:
                        break
                if len(comp) > 1:
                    out.append(sorted(comp))
    return out


def test_checker_detects_a_known_cycle(tmp_path: Path) -> None:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "a.py").write_text("from .b import x\n")
    (pkg / "b.py").write_text("from .c import x\n")
    (pkg / "c.py").write_text("from .a import x\n")
    assert _cycles(_graph(pkg, deferred=False)) != []
    assert _cycles(_graph(pkg, deferred=True)) != []


def test_checker_ignores_deferred_imports_for_module_level(tmp_path: Path) -> None:
    """A cycle that only exists inside a function body is not module-level."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "a.py").write_text("def load():\n    from .b import x\n    return x\n")
    (pkg / "b.py").write_text("def load():\n    from .a import x\n    return x\n")
    assert _cycles(_graph(pkg, deferred=False)) == []
    assert _cycles(_graph(pkg, deferred=True)) != []


def test_checker_resolves_relative_imports_in_subpackages(tmp_path: Path) -> None:
    """`from ..x import y` and `from . import y` resolve against the owning package.

    The other two self-tests only ever build a *flat* package, so they cannot
    tell a correct resolver from one that resolves a relative import against
    the module name instead of the owning package -- the historical bug behind
    `edges: 0`. Every shape that needs `level` > 1 or an alias naming a
    submodule is invisible to a flat fixture, so it is asserted here.
    """
    pkg = tmp_path / "pkg"
    sub = pkg / "sub"
    sub.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (sub / "__init__.py").write_text("")
    (pkg / "leaf.py").write_text("from .sub import a\n")
    (sub / "a.py").write_text("from ..leaf import x\n")
    (sub / "b.py").write_text("from . import c\n")
    (sub / "c.py").write_text("from .b import x\n")
    graph = _graph(pkg, deferred=False)
    assert graph["pkg.sub.a"] == {"pkg.leaf"}
    assert graph["pkg.sub.b"] == {"pkg.sub", "pkg.sub.c"}
    assert graph["pkg.sub.c"] == {"pkg.sub.b"}
    assert graph["pkg.leaf"] == {"pkg.sub", "pkg.sub.a"}
    assert sorted(_cycles(graph)) == [["pkg.leaf", "pkg.sub.a"], ["pkg.sub.b", "pkg.sub.c"]]


def test_no_import_cycles_module_level() -> None:
    assert _cycles(_graph(PACKAGE, deferred=False)) == []


def test_no_import_cycles_including_deferred() -> None:
    assert _cycles(_graph(PACKAGE, deferred=True)) == []
