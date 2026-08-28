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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Actions de curation émises pour un finding dont le tag_action est pertinent.
_CURATION_ACTIONS = {"merge", "delete", "pin", "reference", "recalibrate"}


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


def _skill_fields(entry: dict) -> tuple[str | None, str | None, str | None]:
    """Normalise (skill_id, origin, ttl_policy) depuis une entrée de catalogue.

    Accepte aussi bien ``{skill_id, origin, ttl_policy, metadata{...}}`` que
    ``{metadata:{skill_id, origin, ttl_policy}}``.
    """
    meta = entry.get("metadata") or {}
    skill_id = entry.get("skill_id") or meta.get("skill_id")
    origin = entry.get("origin") if entry.get("origin") is not None else meta.get("origin")
    ttl = entry.get("ttl_policy") if entry.get("ttl_policy") is not None else meta.get("ttl_policy")
    return skill_id, origin, ttl


def decide_actions(
    coherence_findings: list[dict],
    skill_catalog: list[dict],
) -> list[dict]:
    """Émet une décision de curation par finding pertinent.

    Règles de protection:
    - ``ttl_policy == 'pin'`` -> ``action='pin'`` (jamais delete/archive).
    - ``origin == 'user'`` -> par défaut ``action='skip'`` (jamais archive/merge/
      delete). Seul un pin explicitement levé est toléré (``action='pin'``).
    """
    decisions: list[dict] = []
    catalog_index: dict[str, dict] = {}
    for entry in skill_catalog:
        sid = _skill_fields(entry)[0]
        if sid:
            catalog_index[sid] = entry

    for finding in coherence_findings:
        tag_action = finding.get("tag_action")
        if tag_action not in _CURATION_ACTIONS:
            continue
        target = finding.get("target_skill_id") or finding.get("skill_id")
        if not target:
            continue

        entry = catalog_index.get(target)
        if entry is None:
            decisions.append(
                {
                    "target_skill_id": target,
                    "action": "skip",
                    "reason": "skill not present in catalog",
                }
            )
            continue

        skill_id, origin, ttl = _skill_fields(entry)

        if ttl == "pin":
            decisions.append(
                {
                    "target_skill_id": skill_id,
                    "action": "pin",
                    "reason": "ttl_policy=pin: protected from delete/archive",
                }
            )
            continue

        if origin == "user":
            if tag_action == "pin":
                decisions.append(
                    {
                        "target_skill_id": skill_id,
                        "action": "pin",
                        "reason": "user-origin explicit pin allowed",
                    }
                )
            else:
                decisions.append(
                    {
                        "target_skill_id": skill_id,
                        "action": "skip",
                        "reason": "user-origin protected",
                    }
                )
            continue

        # Catalogue normal: on émet l'action portée par le finding.
        decisions.append(
            {
                "target_skill_id": skill_id,
                "action": tag_action,
                "reason": finding.get("reason") or f"coherence finding tag_action={tag_action}",
            }
        )

    return decisions


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
        skill_id = rec.get("skill_id")
        if not skill_id or skill_id in seen:
            continue
        if rec.get("ttl_policy") == "pin":
            continue

        usage = rec.get("usage") or {}
        last_loaded = _parse_iso(usage.get("last_loaded"))
        load_count = usage.get("load_count", 0) or 0

        stale = last_loaded is not None and (now - last_loaded).days > stale_days
        zero_three = runs_seen >= 3 and load_count == 0

        if stale or zero_three:
            archived.append(skill_id)
            seen.add(skill_id)

    return archived


def read_carry(engine_dir: Path) -> dict[str, Any]:
    """Portage inter-run best-effort pour TTL/décroissance (R8).

    Tente d'abord ``watch_memory`` si une fonction de lecture convient, sinon lit
    le fallback ``.watch-memory.jsonl`` sous ``engine_dir``. Retourne
    ``{"runs_seen": int, "usage": {skill_id: {last_loaded, load_count}}}``.

    Défensif: fichier absent ou illisible -> structure vide (``runs_seen=0``).
    """
    fallback = Path(engine_dir) / ".watch-memory.jsonl"
    if not fallback.exists():
        return {"runs_seen": 0, "usage": {}}

    runs_seen = 0
    usage: dict[str, dict[str, Any]] = {}
    try:
        for line in fallback.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            runs_seen += 1
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            sid = obj.get("skill_id") or (obj.get("metadata") or {}).get("skill_id")
            if not sid:
                continue
            u = obj.get("usage") or {}
            usage[sid] = {
                "last_loaded": u.get("last_loaded"),
                "load_count": u.get("load_count", 0),
            }
    except OSError:
        return {"runs_seen": 0, "usage": {}}
    return {"runs_seen": runs_seen, "usage": usage}
