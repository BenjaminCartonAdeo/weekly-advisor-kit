"""Shared tiny helpers — ISO/UTC formatting, anchor parsing, JSON reads, robust z.

Single home for the one-liners previously copy-pasted across main/releases/
insights/report/writer (audit v6.0): behavior is byte-identical to the former
per-module definitions.
"""

from __future__ import annotations

import json
import statistics
from collections.abc import Iterable, Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: Baseline findings harness — artefact racine STABLE entre runs (comme
#: run_state.json). La migration legacy racine ne doit JAMAIS la déplacer.
HARNESS_BASELINE_FILE = "weekly-harness-baseline.json"


def parse_iso_ts(value: object) -> datetime | None:
    """Tolerant ISO-8601 parse (Z→+00:00, naive→UTC); None on garbage/absent.

    Single home for the fromisoformat snippets previously copy-pasted across
    main/releases/report/watch_context/insights (audit v6.0.o).
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
    except ValueError:
        return None


def _abs(path: Path | str) -> Path:
    """Chemin absolu (~ expansé, indépendant du cwd) d'un artefact (X4).

    Contrat : la réponse d'un tool qui produit un artefact porte le chemin
    ABSOLU du fichier écrit — l'agent enchaîne les étapes en recopiant ce
    chemin dans l'appel suivante ; un chemin relatif dépend du cwd et force
    l'agent à deviner (incident post-run 07:41 : boucle ~20 min).
    """
    return Path(path).expanduser().resolve()


def descendants_by_parent(records: Iterable[tuple[str, str | None]], root_id: str) -> list[str]:
    """BFS over parent_id links: (id, parent_id) pairs → descendant ids, discovery order.

    Shared by aggregator (root subtrees) and transcript (child session render);
    the old per-module copies differed in order and complexity (O(n²) scan in
    aggregator) — one indexed BFS for all (audit v6.0.o).
    """
    by_parent: dict[str, list[str]] = {}
    for sid, parent in records:
        if parent:
            by_parent.setdefault(parent, []).append(sid)
    out: list[str] = []
    seen = {root_id}
    queue = list(by_parent.get(root_id, []))
    while queue:
        sid = queue.pop(0)
        if sid in seen:
            continue
        seen.add(sid)
        out.append(sid)
        queue.extend(by_parent.get(sid, []))
    return out


def normalise_digest_path(value: object) -> str | None:
    """Convert harness output paths to project-relative POSIX paths."""
    if not isinstance(value, str) or not value:
        return None
    normalized = value.replace("\\", "/")
    marker = normalized.find(".opencode/")
    if marker >= 0:
        return normalized[marker:]
    if normalized == ".opencode":
        return normalized
    return normalized.removeprefix("./")


def _finding_record(
    value: Mapping[str, Any], component_path: str | None, section: str, component_index: int
) -> dict[str, Any]:
    rule_value = value.get("rule") or value.get("id")
    return {
        "rule": str(rule_value or "unknown"),
        "severity": str(value.get("severity") or ""),
        "message": str(value.get("message") or value.get("detail") or ""),
        "path": normalise_digest_path(value.get("path") or component_path),
        "component_path": component_path,
        "section": section,
        "component_index": component_index,
        "detailed": True,
    }


def iter_digest_findings(digest: object) -> Iterator[dict[str, Any]]:
    """Shared walk over a harness digest (fix-candidates + lint flatten).

    Yields one record per explicit finding and per rules[] fallback row, in
    digest order (top level, then each inspection component).  Per-level dedup
    stays in the consumers: flatten keeps (rule, message) once per top level
    and once per component, remediation drops rules[] rows whose rule already
    has a detailed finding in the same component.  Each record carries
    rule/severity/message/path/component_path/section/component_index/detailed.
    """
    if not isinstance(digest, Mapping):
        return
    uncategorized_files = digest.get("uncategorized_files")
    top = digest.get("findings")
    if isinstance(top, list):
        for value in top:
            if isinstance(value, Mapping):
                yield _finding_record(value, None, "top", -1)
    inspection = digest.get("inspection")
    if not isinstance(inspection, Mapping):
        return
    for section in ("command", "claude_md", "uncategorized"):
        components = inspection.get(section)
        if not isinstance(components, list):
            continue
        for index, component in enumerate(components):
            if not isinstance(component, Mapping):
                continue
            component_path = normalise_digest_path(component.get("path"))
            if (
                component_path is None
                and section == "uncategorized"
                and isinstance(uncategorized_files, list)
                and index < len(uncategorized_files)
            ):
                component_path = normalise_digest_path(uncategorized_files[index])
            comp_findings = component.get("findings")
            if isinstance(comp_findings, list):
                for value in comp_findings:
                    if isinstance(value, Mapping):
                        yield _finding_record(value, component_path, section, index)
            rules = component.get("rules")
            if isinstance(rules, list):
                for value in rules:
                    if not isinstance(value, Mapping) or value.get("result") in (None, "pass"):
                        continue
                    yield {
                        "rule": str(value.get("rule") or "unknown"),
                        "severity": "",
                        "message": "",
                        "path": component_path,
                        "component_path": component_path,
                        "section": section,
                        "component_index": index,
                        "detailed": False,
                    }


def root_and_orphan_ids(
    records: Iterable[tuple[str, str | None]],
    *,
    known_parent_ids: set[str] | None = None,
) -> tuple[set[str], set[str]]:
    """Split (id, parent_id) pairs into (orphan_ids, root_ids).

    Shared by aggregator and the selection audit — the old per-module copies
    of this decision drifted (audit v6.0.o). An orphan = child whose parent is
    neither in the set nor known; a root = not an orphan, parent absent or
    outside the set.
    """
    records = list(records)
    included_ids = {sid for sid, _parent in records}
    orphan_ids = {
        sid
        for sid, parent in records
        if parent is not None
        and parent not in included_ids
        and (known_parent_ids is None or parent not in known_parent_ids)
    }
    root_ids = {
        sid
        for sid, parent in records
        if sid not in orphan_ids and (parent is None or parent not in included_ids)
    }
    return orphan_ids, root_ids


def relative_path(path: Path, root: Path) -> str:
    """Stable project-relative POSIX path; absolute posix fallback outside root.

    Single home shared by watch_context/harness_scope (C11): the former
    watch_context copy had the fallback, the util one raised — the unified
    version keeps the defensive fallback so a path outside root never crashes
    a deterministic inventory.
    """
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def casefold(value: str) -> str:
    """strip + casefold, shared by watch_context/watch_validation identity keys."""
    return value.strip().casefold()


def iso(dt: datetime) -> str:
    """UTC ISO-8601 with a Z suffix ("%Y-%m-%dT%H:%M:%SZ")."""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_anchor(value: str | None) -> datetime:
    """now(UTC) or ISO with Z→+00:00, UTC if naive."""
    if value is None:
        return datetime.now(UTC)
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def load_json(path: Path) -> dict | None:
    """Read a JSON dict; None on missing file or garbage (never raises)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def load_jsonc(path: Path) -> dict | None:
    """Read a JSONC dict (comments + trailing commas tolerated); None on failure.

    ``//`` and ``/* */`` comments are stripped string-aware, then trailing
    commas before a closing bracket are removed.  Strict JSON passes through
    untouched.  Shared by watch_context (opencode.jsonc plugin declarations,
    hand-edited ecosystem fixtures) — the docstring "JSONC is accepted" is
    only true since v6.0.p (C4).
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(_strip_jsonc(text))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _strip_jsonc(text: str) -> str:
    """Remove ``//``/``/* */`` comments (never inside strings) and trailing commas."""
    out: list[str] = []
    i, n = 0, len(text)
    in_string = False
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if in_string:
            out.append(ch)
            if ch == "\\" and nxt:
                out.append(nxt)
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
        elif ch == "/" and nxt == "/":
            while i < n and text[i] != "\n":
                i += 1
        elif ch == "/" and nxt == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
        elif ch == ",":
            j = i + 1
            # Trailing comma before a closer: tolerate comments/whitespace between
            # the comma and the closing bracket (JSONC fixture style).
            while j < n:
                if text[j] in " \t\r\n":
                    j += 1
                elif text[j] == "/" and j + 1 < n and text[j + 1] == "/":
                    j += 2
                    while j < n and text[j] != "\n":
                        j += 1
                elif text[j] == "/" and j + 1 < n and text[j + 1] == "*":
                    j += 2
                    while j + 1 < n and not (text[j] == "*" and text[j + 1] == "/"):
                        j += 1
                    j += 2
                else:
                    break
            if j < n and text[j] in "]}":
                i += 1  # trailing comma before a closing bracket: drop it
            else:
                out.append(ch)
                i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def read_text(path: Path) -> str | None:
    """Read a text file; None on missing/unreadable (never raises)."""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def period_hours(start_iso: str, end_iso: str) -> float | None:
    """Window duration in hours from two ISO strings; None on garbage/absent."""
    try:
        return (
            datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
            - datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
        ).total_seconds() / 3600
    except (ValueError, TypeError):
        return None


def robust_z(values: list[float]) -> list[float]:
    """Robust z-scores (median + MAD, 0.6745 scale).

    MAD==0 → fall back to mean absolute deviation so a single spike among an
    otherwise-identical baseline is still detectable; still 0 → all zeros.
    """
    if not values:
        return []
    median = statistics.median(values)
    mad = statistics.median(sorted(abs(v - median) for v in values))
    if mad == 0:
        mad = sum(abs(v - median) for v in values) / len(values)
    if mad == 0:
        return [0.0] * len(values)
    return [0.6745 * (v - median) / mad for v in values]
