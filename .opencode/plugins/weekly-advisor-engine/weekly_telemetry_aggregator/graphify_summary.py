"""Deterministic, read-only architecture summary for Graphify output.

Graphify is deliberately out-of-band: this module never updates ``graph.json``
and never invokes Graphify.  It only projects a compact summary from a graph
artifact when that artifact is usable.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

GENERIC_LABELS = frozenset({"", "root", "unknown", "generic", "node", "none", "null"})


def _generic(node: dict[str, Any]) -> bool:
    label = str(node.get("norm_label") or node.get("label") or "").strip().lower()
    if label in GENERIC_LABELS or len(label) < 2:
        return True
    return label in {"object", "string", "number", "boolean", "array"}


def _source(node: dict[str, Any]) -> str:
    return str(node.get("source_file") or "").strip()


def summarize_graph(graph: dict[str, Any], *, project_root: Path | None = None) -> dict[str, Any]:
    """Return a stable architecture projection, without mutating *graph*.

    Nodes without a source file, generic nodes, and nodes whose source has
    disappeared are excluded.  Links are restricted to retained nodes and
    self-loops are omitted.  All collections are sorted for reproducibility.
    """
    raw_nodes = graph.get("nodes", [])
    nodes: dict[str, dict[str, Any]] = {}
    stale = 0
    generic = 0
    for node in raw_nodes if isinstance(raw_nodes, list) else []:
        if not isinstance(node, dict):
            continue
        node_id = str(node.get("id", "")).strip()
        source = _source(node)
        if not node_id or not source:
            continue
        if _generic(node):
            generic += 1
            continue
        if project_root is not None and not (project_root / source).exists():
            stale += 1
            continue
        nodes[node_id] = node

    links = graph.get("links", graph.get("edges", []))
    relations: Counter[tuple[str, str, str]] = Counter()
    for link in links if isinstance(links, list) else []:
        if not isinstance(link, dict):
            continue
        source, target = str(link.get("source", "")), str(link.get("target", ""))
        if source == target or source not in nodes or target not in nodes:
            continue
        relations[(source, target, str(link.get("relation", "related")))] += 1

    by_source: defaultdict[str, set[str]] = defaultdict(set)
    for node in nodes.values():
        by_source[_source(node)].add(str(node.get("file_type") or "unknown"))
    files = [
        {
            "source_file": source,
            "file_types": sorted(types),
            "node_count": sum(_source(n) == source for n in nodes.values()),
        }
        for source, types in sorted(by_source.items())
    ]
    return {
        "schema_version": 1,
        "built_at_commit": graph.get("built_at_commit"),
        "node_count": len(nodes),
        "edge_count": sum(relations.values()),
        "source_file_count": len(files),
        "filtered": {
            "generic_nodes": generic,
            "stale_nodes": stale,
            "self_loops": sum(
                1
                for link in links
                if isinstance(link, dict) and link.get("source") == link.get("target")
            ),
        },
        "files": files,
        "relations": [
            {"source": source, "target": target, "relation": relation, "weight": weight}
            for (source, target, relation), weight in sorted(relations.items())
        ],
    }


def load_and_summarize(path: Path, *, project_root: Path | None = None) -> dict[str, Any]:
    """Load JSON graph and summarize it; malformed artifacts fail explicitly."""
    with path.open(encoding="utf-8") as stream:
        graph = json.load(stream)
    if not isinstance(graph, dict):
        raise ValueError("Graphify artifact must contain a JSON object")
    return summarize_graph(graph, project_root=project_root)
