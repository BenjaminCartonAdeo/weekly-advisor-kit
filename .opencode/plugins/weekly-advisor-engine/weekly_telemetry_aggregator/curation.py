"""Skill curation & TTL garbage-collection (R4 curation/GC + R8 TTL/décroissance).

Fonctions pures:

- ``decide_actions(coherence_findings, skill_catalog) -> list[dict]``
  Émet une décision par finding dont ``tag_action`` est une action de curation
  pertinente, en appliquant les règles de protection ``origin`` / ``ttl_policy``.

- ``ttl_archive_candidates(usage_records, runs_seen, stale_days=90) -> list[str]``
  Retourne les ``skill_id`` à archiver selon la politique TTL (jamais ceux en
  ``ttl_policy=='pin'``).

Un helper ``read_carry`` assure le portage inter-run (``runs_seen``, décroissance)
en réutilisant ``watch_memory`` si pertinent, sinon le fallback
``.watch-memory.jsonl`` de l'engine.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Actions de curation émises pour un finding dont le tag_action est pertinent.
_CURATION_ACTIONS = {
    "archive",
    "merge",
    "delete",
    "pin",
    "reference",
    "recalibrate",
    "fix",
}
_PROTECTION_ACTIONS = {"pin"}
# Stable tie-breaks keep one final decision per skill regardless of finding order.
_ACTION_PRIORITY = {
    "pin": 0,
    "recalibrate": 10,
    "fix": 11,
    "merge": 20,
    "reference": 21,
    "delete": 22,
    "archive": 23,
}
_SOURCE_PRIORITY = {"coherence": 0, "r4": 1, "ttl": 2}
_VALID_ORIGINS = frozenset({"user", "bundled", "weekly-foreground", "weekly-background"})
_NULL_TTLS = frozenset({"", "none", "null"})


def manifest_metadata(
    decisions: Iterable[Mapping[str, Any]], *, generated_at: str, anchor: str
) -> dict[str, Any]:
    """Build additive v2 manifest metadata from serialized curation decisions."""
    serialized = [dict(decision) for decision in decisions]
    by_action = dict(sorted(Counter(str(d.get("action") or "") for d in serialized).items()))
    skipped_details = [decision for decision in serialized if decision.get("status") == "skipped"]
    return {
        "schema_version": 2,
        "generated_at": generated_at,
        "anchor": anchor,
        "summary": {"by_action": by_action},
        "skipped_details": skipped_details,
    }


def _parse_iso(value: str | None) -> datetime | None:
    """Parse une date ISO-8601 (gère le suffixe ``Z``). None si absent/illisible."""
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _text_field(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _normalized_ttl(value: object) -> str | None:
    text = _text_field(value)
    if text is None or text.casefold() in _NULL_TTLS:
        return None
    return text.casefold()


def _skill_fields(entry: Mapping[str, Any] | object) -> tuple[str | None, str | None, str | None]:
    """Normalise (skill_id, origin, ttl_policy) depuis une entrée de catalogue.

    Accepte aussi bien ``{skill_id, origin, ttl_policy, metadata{...}}`` que
    ``{metadata:{skill_id, origin, ttl_policy}}``.
    """
    if not isinstance(entry, Mapping):
        return None, None, None
    raw_meta = entry.get("metadata")
    meta = raw_meta if isinstance(raw_meta, Mapping) else {}
    skill_id = _text_field(entry.get("skill_id")) or _text_field(meta.get("skill_id"))
    origin_values = [
        value.casefold()
        for value in (_text_field(meta.get("origin")), _text_field(entry.get("origin")))
        if value is not None
    ]
    # A duplicate/merged row must never weaken explicit user protection.
    origin = "user" if "user" in origin_values else (origin_values[0] if origin_values else None)
    ttl_values = [
        _normalized_ttl(value) for value in (meta.get("ttl_policy"), entry.get("ttl_policy"))
    ]
    ttl = (
        "pin"
        if "pin" in ttl_values
        else next((value for value in ttl_values if value is not None), None)
    )
    return skill_id, origin, ttl


def _split_skill_ids(value: object) -> list[str]:
    """Return trimmed skill ids from list/scalar legacy finding shapes."""
    if isinstance(value, str):
        values: Iterable[object] = value.split(",")
    elif isinstance(value, list | tuple | set):
        values = value
    else:
        values = ()
    return [item.strip() for item in values if isinstance(item, str) and item.strip()]


def _signal_archive_ids(signal: object) -> list[str]:
    """Read the R4 ``strong_8of8`` signal in list and legacy scalar forms."""
    if isinstance(signal, Mapping):
        signal = signal.get("curation_signal", signal)
    if not isinstance(signal, Mapping):
        return []
    candidates = signal.get("R4_archive_candidates")
    if not isinstance(candidates, Mapping):
        return []
    return _split_skill_ids(candidates.get("strong_8of8"))


def normalize_curation_findings(
    coherence: object,
    curation_signal: object | None = None,
) -> list[dict]:
    """Normalize coherence/R4 archive findings into individual actionable records.

    ``weekly-coherence-findings`` historically alternated between a list with a
    comma-concatenated archive target and a root object carrying the authoritative
    ``curation_signal.R4_archive_candidates.strong_8of8`` list.  Normalizing both
    here gives the decision engine one source of truth and makes CLI/report output
    independent of the producer's shape.
    """
    root_signal = curation_signal
    if isinstance(coherence, Mapping):
        root_signal = coherence.get("curation_signal", root_signal)
        raw_findings = coherence.get("findings") or []
    else:
        raw_findings = coherence or []
    if not isinstance(raw_findings, list):
        raw_findings = []

    normalized: list[dict] = []
    for finding in raw_findings:
        if not isinstance(finding, Mapping):
            continue
        action = finding.get("tag_action")
        if action == "archive":
            ids = _split_skill_ids(finding.get("target_skill_id") or finding.get("skill_id"))
            for skill_id in ids:
                normalized.append(
                    {
                        **finding,
                        "target_skill_id": skill_id,
                        "_source": finding.get("_source") or "coherence",
                    }
                )
            continue
        normalized.append(dict(finding))

    for skill_id in _signal_archive_ids(root_signal):
        normalized.append(
            {
                "tag_action": "archive",
                "target_skill_id": skill_id,
                "reason": "R4 strong_8of8 (load_count==0 >=3 runs)",
                "_source": "r4",
            }
        )
    return normalized


def decide_actions(
    coherence_findings: list[dict] | Mapping[str, object],
    skill_catalog: list[dict],
    curation_signal: object | None = None,
) -> list[dict]:
    """Emit one deterministic final decision per skill.

    Règles de protection:
    - ``ttl_policy == 'pin'`` -> ``action='pin'`` (jamais delete/archive).
    - ``origin == 'user'`` -> par défaut ``action='skip'`` (jamais archive/merge/
      delete). Seul un pin explicitement levé est toléré (``action='pin'``).

    Precedence is explicit: protection, then recalibration/fix, then proposal or
    archive.  This prevents an R4 archive signal from competing with a stronger
    recalibration finding and avoids duplicate decisions in reports.
    """
    catalog_index: dict[str, Mapping[str, Any]] = {}
    for entry in skill_catalog:
        if not isinstance(entry, Mapping):
            continue
        sid = _skill_fields(entry)[0]
        if sid:
            previous = catalog_index.get(sid)
            if previous is None or _catalog_priority(entry) < _catalog_priority(previous):
                # Protection precedence is independent of catalogue order:
                # ttl_policy=pin, then origin=user, always beats weaker rows.
                catalog_index[sid] = entry

    findings = normalize_curation_findings(coherence_findings, curation_signal)
    grouped: dict[str, list[dict]] = {}
    for finding in findings:
        tag_action = finding.get("tag_action")
        if tag_action not in _CURATION_ACTIONS:
            continue
        target = finding.get("target_skill_id") or finding.get("skill_id")
        if not isinstance(target, str) or not target.strip():
            continue
        grouped.setdefault(target.strip(), []).append(finding)

    decisions: list[dict] = []
    for target in sorted(grouped):
        target_findings = grouped[target]
        entry = catalog_index.get(target)

        if entry is None:
            # R4 archive candidates are still useful in a dry-run when a
            # catalogue is unavailable.  A non-archive proposal remains a
            # safe skip: applying it cannot prove the target exists locally.
            finding = _select_finding(target_findings, allow_archive=True)
            if finding is not None and finding.get("tag_action") == "archive":
                decisions.append(_decision_from_finding(target, finding, "archive"))
            else:
                decisions.append(
                    {
                        "target_skill_id": target,
                        "action": "skip",
                        "reason": "skill not present in catalog",
                        "source": _finding_source(min(target_findings, key=_finding_sort_key)),
                    }
                )
            continue

        skill_id, origin, ttl = _skill_fields(entry)
        skill_id = skill_id or target

        # Protection takes precedence over every producer finding.
        if ttl == "pin":
            decisions.append(
                {
                    "target_skill_id": skill_id,
                    "action": "pin",
                    "reason": "ttl_policy=pin: protected from delete/archive",
                    "source": _finding_source(min(target_findings, key=_finding_sort_key)),
                }
            )
            continue

        if origin == "user":
            if any(f.get("tag_action") in _PROTECTION_ACTIONS for f in target_findings):
                decisions.append(
                    {
                        "target_skill_id": skill_id,
                        "action": "pin",
                        "reason": "user-origin explicit pin allowed",
                        "source": _finding_source(min(target_findings, key=_finding_sort_key)),
                    }
                )
            else:
                decisions.append(
                    {
                        "target_skill_id": skill_id,
                        "action": "skip",
                        "reason": "user-origin protected",
                        "source": _finding_source(min(target_findings, key=_finding_sort_key)),
                    }
                )
            continue

        finding = _select_finding(target_findings, allow_archive=True)
        if finding is None:
            continue
        decisions.append(_decision_from_finding(skill_id, finding, str(finding["tag_action"])))

    return decisions


def _finding_source(finding: Mapping[str, object]) -> str:
    source = finding.get("_source") or finding.get("source")
    return str(source) if source else "coherence"


def _catalog_priority(entry: Mapping[str, Any] | object) -> tuple[int, int, str]:
    """Sort duplicate catalog rows by protection strength, not input order."""

    _sid, origin, ttl = _skill_fields(entry)
    protection = 0 if ttl == "pin" else 1 if origin == "user" else 2
    metadata = entry.get("metadata") if isinstance(entry, Mapping) else None
    richness = len(metadata) if isinstance(metadata, Mapping) else 0
    # The serialized tie-break makes equal-strength duplicate rows deterministic.
    stable = (
        repr(sorted((str(key), repr(value)) for key, value in entry.items()))
        if isinstance(entry, Mapping)
        else ""
    )
    return protection, -richness, stable


def select_catalog_entry(
    skill_catalog: Iterable[Mapping[str, Any] | object], skill_id: str
) -> Mapping[str, Any] | None:
    """Return strongest duplicate catalogue row for ``skill_id``.

    Curation receives summaries from multiple producers.  Their row order is
    not an authority: a later weak row must never overwrite a user/pinned row.
    """

    matches = [
        entry
        for entry in skill_catalog
        if _skill_fields(entry)[0] == skill_id and isinstance(entry, Mapping)
    ]
    return min(matches, key=_catalog_priority) if matches else None


def catalog_entry_is_complete(entry: Mapping[str, Any] | object) -> bool:
    """Whether a catalogue row is safe to use as an apply authorization.

    Reports may contain a name-only ``skill_catalog_entries`` fallback.  Such a
    row is enough for display/dry-run but not enough to authorize a filesystem
    move.  ``ttl_policy: null`` is complete when the key is explicitly present.
    """

    skill_id, origin, ttl = _skill_fields(entry)
    if skill_id is None or origin not in _VALID_ORIGINS:
        return False
    raw_meta = entry.get("metadata") if isinstance(entry, Mapping) else None
    meta = raw_meta if isinstance(raw_meta, Mapping) else {}
    ttl_present = "ttl_policy" in meta or (isinstance(entry, Mapping) and "ttl_policy" in entry)
    return ttl_present and (ttl is None or ttl in {"decay", "pin"})


def _finding_sort_key(finding: Mapping[str, object]) -> tuple[int, int, str, str]:
    action = str(finding.get("tag_action") or "")
    source = _finding_source(finding)
    return (
        _ACTION_PRIORITY.get(action, 99),
        _SOURCE_PRIORITY.get(source, 99),
        str(finding.get("reason") or finding.get("recommendation") or ""),
        str(finding.get("target_skill_id") or finding.get("skill_id") or ""),
    )


def _select_finding(target_findings: list[dict], *, allow_archive: bool) -> dict | None:
    """Pick one finding using the curation precedence contract.

    The caller handles catalogue protections before invoking this helper.  The
    remaining order is explicit and stable: ``pin`` (an explicit protection),
    ``recalibrate``/``fix``, proposal actions, then archive.  Proposal actions
    therefore prevent a weaker archive signal from mutating the same skill.
    """
    eligible = [
        finding
        for finding in target_findings
        if finding.get("tag_action") in _CURATION_ACTIONS
        and (allow_archive or finding.get("tag_action") != "archive")
    ]
    return min(eligible, key=_finding_sort_key) if eligible else None


def _decision_from_finding(skill_id: str, finding: Mapping[str, object], action: str) -> dict:
    reason = finding.get("reason") or finding.get("recommendation")
    return {
        "target_skill_id": skill_id,
        "action": action,
        "reason": str(reason or f"coherence finding tag_action={action}"),
        "source": _finding_source(finding),
    }


def ttl_archive_candidates(
    usage_records: list[dict],
    runs_seen: int,
    stale_days: int = 90,
) -> list[str]:
    """Retourne les ``skill_id`` à archiver selon la politique TTL (R8).

    Condition d'archive (hors ``ttl_policy=='pin'``, toujours exempté):
    - ``usage.last_loaded`` vieux de strictement plus de ``stale_days`` (date
      calculable),
    - OU ``usage.load_count == 0`` sur >= 3 runs consécutifs
      (``runs_seen >= 3 and load_count == 0``).

    ``last_loaded`` absent/illisible n'est pas considéré comme "vieux" (âge
    incalculable) ; un skill jamais chargé est rattrapé par la règle
    ``load_count==0`` une fois ``runs_seen >= 3``.
    """
    now = datetime.now(UTC)
    archived: list[str] = []
    seen: set[str] = set()

    for rec in usage_records:
        if not isinstance(rec, Mapping):
            continue
        skill_id = rec.get("skill_id")
        if not skill_id or skill_id in seen:
            continue
        rec_meta = rec.get("metadata") or {}
        origin = rec.get("origin")
        if origin is None and isinstance(rec_meta, Mapping):
            origin = rec_meta.get("origin")
        if origin == "user":
            continue
        ttl_policy = rec.get("ttl_policy")
        if ttl_policy is None and isinstance(rec_meta, Mapping):
            ttl_policy = rec_meta.get("ttl_policy")
        if ttl_policy == "pin":
            continue

        usage = rec.get("usage") or {}
        if not isinstance(usage, Mapping):
            usage = {}
        last_loaded = _parse_iso(usage.get("last_loaded"))
        load_count = usage.get("load_count", 0) or 0

        stale = last_loaded is not None and (now - last_loaded).days > stale_days
        zero_three = runs_seen >= 3 and load_count == 0

        if stale or zero_three:
            archived.append(skill_id)
            seen.add(skill_id)

    return archived


def build_catalog_from_skills(project_root: Path | None) -> list[dict]:
    """Catalogue de skills avec ``metadata.origin`` / ``ttl_policy`` / ``usage`` du disque.

    Source autorité pour l'auto-load (Phase 1) quand aucun catalogue explicite
    n'est fourni à ``skill-curate`` : le frontmatter ``metadata.*`` de chaque
    ``SKILL.md``. Repli robuste — la summary ne porte que ``skill_catalog_count``
    (pas ``origin``/``ttl_policy``), or ``decide_actions`` a besoin de ces champs
    pour la protection ``user``/``pin``.

    Entrée : ``{"skill_id": <nom dossier>, "metadata": {origin, ttl_policy, usage}}``.
    """
    from .main import _skill_dirs
    from .safe_git_write import frontmatter_blocks

    catalog: list[dict] = []
    for root in _skill_dirs(project_root):
        if not root.is_dir():
            continue
        for skill_md in sorted(root.glob("**/SKILL.md")):
            if not skill_md.is_file():
                continue
            sid = skill_md.parent.name
            meta, _body, _err = frontmatter_blocks(skill_md)
            nested = meta.get("metadata")
            nested = nested if isinstance(nested, Mapping) else {}
            catalog.append(
                {
                    "skill_id": sid,
                    "metadata": {
                        # ``frontmatter_blocks`` is intentionally a tiny parser;
                        # nested YAML keys also appear at the top level.  Read
                        # both forms so protection remains effective on disk.
                        "origin": nested.get("origin") or meta.get("origin"),
                        "ttl_policy": nested.get("ttl_policy") or meta.get("ttl_policy"),
                        "usage": nested.get("usage") or meta.get("usage"),
                    },
                }
            )
    return catalog


def _memory_paths(
    engine_dir: Path | None,
    output_dir: Path | None,
    memory_file: str | Path | None,
) -> list[Path]:
    """Resolve configured memory first, then retain the pre-v6 engine fallback."""

    paths: list[Path] = []
    if memory_file is not None:
        configured = Path(memory_file).expanduser()
        if not configured.is_absolute() and output_dir is not None:
            configured = Path(output_dir).expanduser() / configured
        elif not configured.is_absolute() and engine_dir is not None:
            configured = Path(engine_dir).expanduser() / configured
        paths.append(configured)
    if output_dir is not None:
        paths.append(Path(output_dir).expanduser() / "watch-memory.jsonl")
    if engine_dir is not None:
        paths.append(Path(engine_dir).expanduser() / ".watch-memory.jsonl")

    unique: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        if path not in seen:
            seen.add(path)
            unique.append(path)
    return unique


def _run_marker(obj: Mapping[str, Any]) -> str | None:
    """Extract a stable run marker; item/line count is never a run count."""

    for key in ("run_id", "run_marker", "run", "run_date", "date", "week"):
        value = obj.get(key)
        if (
            isinstance(value, str | int | float)
            and not isinstance(value, bool)
            and str(value).strip()
        ):
            return str(value).strip()
        if isinstance(value, Mapping):
            for nested_key in ("run_id", "id", "date", "week"):
                nested = value.get(nested_key)
                if (
                    isinstance(nested, str | int | float)
                    and not isinstance(nested, bool)
                    and str(nested).strip()
                ):
                    return str(nested).strip()
    # watch-memory entries generally carry their observation week in history.
    history = obj.get("history")
    if isinstance(history, list):
        weeks = {
            str(stamp.get("week"))
            for stamp in history
            if isinstance(stamp, Mapping) and isinstance(stamp.get("week"), str)
        }
        if weeks:
            return max(weeks)
    return None


def _carry_usage(obj: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Normalize skill usage records from legacy and configured memory shapes."""

    records: list[dict[str, Any]] = []

    def add_record(
        skill_id: object, payload: object, inherited: Mapping[str, Any] | None = None
    ) -> None:
        if not isinstance(skill_id, str) or not skill_id.strip():
            return
        record = payload if isinstance(payload, Mapping) else {}
        usage = record.get("usage") if isinstance(record.get("usage"), Mapping) else record
        if not isinstance(usage, Mapping):
            usage = {}
        metadata = record.get("metadata") if isinstance(record.get("metadata"), Mapping) else {}
        if inherited:
            metadata = {**inherited, **metadata}
        origin = record.get("origin")
        if origin is None:
            origin = metadata.get("origin")
        ttl_policy = record.get("ttl_policy")
        if ttl_policy is None:
            ttl_policy = metadata.get("ttl_policy")
        records.append(
            {
                "skill_id": skill_id.strip(),
                "origin": origin,
                "ttl_policy": ttl_policy,
                "usage": {
                    "last_loaded": usage.get("last_loaded"),
                    "load_count": usage.get("load_count", 0),
                },
            }
        )

    sid = obj.get("skill_id") or (
        obj.get("metadata", {}).get("skill_id")
        if isinstance(obj.get("metadata"), Mapping)
        else None
    )
    raw_usage = obj.get("usage")
    if isinstance(raw_usage, Mapping):
        # A mapping keyed by skill id is a supported compact snapshot.  A
        # mapping with usage fields is the single-record shape handled above.
        if any(key in raw_usage for key in ("last_loaded", "load_count")):
            if sid:
                add_record(sid, obj)
        else:
            for skill_id, payload in raw_usage.items():
                add_record(
                    skill_id,
                    payload,
                    obj.get("metadata") if isinstance(obj.get("metadata"), Mapping) else None,
                )
    elif isinstance(raw_usage, list):
        for item in raw_usage:
            if isinstance(item, Mapping):
                add_record(item.get("skill_id") or item.get("name"), item)

    if sid and not isinstance(raw_usage, Mapping | list):
        add_record(sid, obj)

    raw_skills = obj.get("skills")
    if isinstance(raw_skills, Mapping):
        for skill_id, payload in raw_skills.items():
            add_record(skill_id, payload)
    elif isinstance(raw_skills, list):
        for item in raw_skills:
            if isinstance(item, Mapping):
                add_record(item.get("skill_id") or item.get("name"), item)
    return records


def read_carry(
    engine_dir: Path | None = None,
    output_dir: Path | None = None,
    memory_file: str | Path | None = None,
    *,
    watch_memory: str | Path | None = None,
) -> dict[str, Any]:
    """Portage inter-run best-effort pour TTL/décroissance (R8).

    Lit d'abord le fichier ``watch_distill.memory_file`` sous ``output_dir`` puis
    conserve le fallback historique ``.watch-memory.jsonl`` sous ``engine_dir``.
    ``watch_memory`` est un alias explicite de ``memory_file``. Retourne
    ``{"runs_seen": int, "usage": {skill_id: {last_loaded, load_count}}}``.

    Défensif: fichier absent ou illisible -> structure vide (``runs_seen=0``).
    """
    if memory_file is None and watch_memory is not None:
        memory_file = watch_memory
    selected: Path | None = None
    for candidate in _memory_paths(engine_dir, output_dir, memory_file):
        try:
            if candidate.is_file():
                selected = candidate
                break
        except OSError:
            continue
    if selected is None:
        return {"runs_seen": 0, "usage": {}}

    run_markers: set[str] = set()
    explicit_runs = 0
    usage: dict[str, dict[str, Any]] = {}
    try:
        lines = selected.read_text(encoding="utf-8").splitlines()
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                decoded = json.loads(line)
            except json.JSONDecodeError:
                continue
            # JSON scalars/lists are malformed carry records, not mappings.
            if not isinstance(decoded, Mapping):
                continue
            marker = _run_marker(decoded)
            if marker is not None:
                run_markers.add(marker)
            value = decoded.get("runs_seen")
            if isinstance(value, int) and not isinstance(value, bool):
                explicit_runs = max(explicit_runs, value)
            for record in _carry_usage(decoded):
                sid = record["skill_id"]
                previous = usage.get(sid, {})
                origin = record.get("origin") or previous.get("origin")
                ttl_policy = record.get("ttl_policy") or previous.get("ttl_policy")
                if previous.get("origin") == "user" or origin == "user":
                    origin = "user"
                if previous.get("ttl_policy") == "pin" or ttl_policy == "pin":
                    ttl_policy = "pin"
                usage[sid] = {
                    "origin": origin,
                    "ttl_policy": ttl_policy,
                    "last_loaded": record["usage"].get("last_loaded"),
                    "load_count": record["usage"].get("load_count", 0),
                }
    except OSError:
        return {"runs_seen": 0, "usage": {}}
    return {"runs_seen": max(len(run_markers), explicit_runs), "usage": usage}
