"""Mémoire inter-run de la veille écosystème (``watch-memory.jsonl``).

Store append-only JSONL : chaque mise à jour écrit une ligne complète fusionnée
par ``id``, flush immédiat (crash-safe) ; la lecture fusionne les lignes par id
(dernière ligne gagnante), ignore les lignes malformées avec avertissement et
purge les entrées vues depuis plus de 26 semaines — sauf dernier statut
``recommended``|``blocked-security``.

Le module est déterministe et local : ni réseau, ni configuration globale,
ni état process. Les semaines sont des étiquettes ISO ``"YYYY-Wnn"``
(``datetime.isocalendar()``).
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from copy import deepcopy
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .util import parse_iso_ts

RETENTION_WEEKS = 26
STALE_SEEN_WEEKS = 4
RECURRENT_MIN_OCCURRENCES = 3
MAX_RECENTLY_IGNORED = 20
MAX_PREVIOUSLY_RECOMMENDED = 30
#: Plafond des récurents du digest (top-N occurrences puis id) — borne prompt.
RECURRENTS_CAP = 20

VALID_STATUSES = frozenset({"seen", "candidate", "recommended", "ignored", "blocked-security"})
_RETAINED_STATUSES = frozenset({"recommended", "blocked-security"})

_WEEK_RE = re.compile(r"^(\d{4})-W(\d{1,2})$")

_MEMORY_FIELDS = (
    "id",
    "name",
    "first_seen_week",
    "last_seen_week",
    "occurrences",
    "history",
    "last_signature",
    "note",
)


# ------------------------------------------------------------------- semaines


def week_of(dt: datetime) -> str:
    """Étiquette ISO de la semaine de ``dt`` (ex. ``"2026-W34"``)."""

    year, week, _ = dt.isocalendar()
    return f"{year}-W{week:02d}"


def _week_monday(value: object) -> date | None:
    """Lundi de la semaine ``"YYYY-Wnn"`` ; None si étiquette invalide."""

    if not isinstance(value, str):
        return None
    match = _WEEK_RE.fullmatch(value.strip())
    if match is None:
        return None
    try:
        return date.fromisocalendar(int(match[1]), int(match[2]), 1)
    except ValueError:
        return None


def _week_key(value: object) -> date:
    return _week_monday(value) or date.min


def weeks_between(old_week: str, new_week: str) -> int | None:
    """Écart en semaines pleines entre deux étiquettes ; None si illisible."""

    old, new = _week_monday(old_week), _week_monday(new_week)
    if old is None or new is None:
        return None
    return (new - old).days // 7


# ------------------------------------------------------------------ identités


def normalize_id(name: str, npm_package: str | None, repo_url: str | None) -> str:
    """Identité stable d'un item : ``npm:<pkg>``, ``gh:<owner/repo>`` ou ``url:``."""

    if isinstance(npm_package, str) and npm_package.strip():
        return f"npm:{npm_package.strip()}"
    if isinstance(repo_url, str) and repo_url.strip():
        text = repo_url.strip()
        try:
            parts = urlsplit(text)
            host = (parts.hostname or "").casefold()
            segments = [segment for segment in (parts.path or "").split("/") if segment]
        except ValueError:
            host, segments = "", []
        if host == "github.com" and len(segments) >= 2:
            owner = segments[0].casefold()
            repo = segments[1].casefold().removesuffix(".git")
            return f"gh:{owner}/{repo}"
        return f"url:{text.casefold()}"
    label = name.strip().casefold() if isinstance(name, str) else ""
    return f"url:{label or 'unknown'}"


# ------------------------------------------------------------------ signatures


def _text_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def item_signature(item: Mapping[str, Any]) -> dict[str, str | None]:
    """Signature de fraîcheur d'un item : version + date de publication."""

    return {
        "version": _text_or_none(item.get("version")),
        "published_at": _text_or_none(item.get("published_at")),
    }


def signature_changed(entry: Mapping[str, Any], sig: Mapping[str, Any]) -> bool:
    """Vrai si la version diffère ou si ``published_at`` est plus récent.

    Une entrée sans signature stockée ne prouve aucun changement (False).
    """

    stored = entry.get("last_signature") if isinstance(entry, Mapping) else None
    if not isinstance(stored, Mapping):
        return False
    old_version, new_version = stored.get("version"), sig.get("version")
    if isinstance(old_version, str) and isinstance(new_version, str) and old_version != new_version:
        return True
    old_at, new_at = stored.get("published_at"), sig.get("published_at")
    if isinstance(old_at, str) and isinstance(new_at, str):
        old_dt, new_dt = parse_iso_ts(old_at), parse_iso_ts(new_at)
        if old_dt is not None and new_dt is not None:
            return new_dt > old_dt
        return new_at > old_at
    return False


# --------------------------------------------------------------------- entrées


def entry_from_item(item: Mapping[str, Any], week: str) -> dict[str, Any]:
    """Squelette d'une ligne mémoire neuve pour un item vu la semaine ``week``."""

    return {
        "id": normalize_id(
            str(item.get("name") or ""), item.get("npm_package"), item.get("repo_url")
        ),
        "name": str(item.get("name") or ""),
        "first_seen_week": week,
        "last_seen_week": week,
        "occurrences": 1,
        "history": [{"week": week, "status": "seen"}],
        "last_signature": item_signature(item),
        "note": "",
    }


def _blank_entry(eid: str) -> dict[str, Any]:
    return {
        "id": eid,
        "name": "",
        "first_seen_week": "",
        "last_seen_week": "",
        "occurrences": 0,
        "history": [],
        "last_signature": {},
        "note": "",
    }


def _normalized_history(raw: object) -> list[dict[str, str]]:
    history: list[dict[str, str]] = []
    if not isinstance(raw, Sequence) or isinstance(raw, str | bytes | bytearray):
        return history
    for stamp in raw:
        if not isinstance(stamp, Mapping):
            continue
        status = stamp.get("status")
        week = stamp.get("week")
        if status in VALID_STATUSES and isinstance(week, str):
            history.append({"week": week, "status": status})
    return history


def _normalized_entry(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Entrée canonique avec défauts ; tolère les lignes partielles."""

    occurrences = payload.get("occurrences")
    entry = {
        "id": payload["id"],
        "name": payload.get("name") if isinstance(payload.get("name"), str) else "",
        "first_seen_week": _text_or_none(payload.get("first_seen_week")) or "",
        "last_seen_week": _text_or_none(payload.get("last_seen_week")) or "",
        "occurrences": occurrences if isinstance(occurrences, int) and occurrences > 0 else 0,
        "history": _normalized_history(payload.get("history")),
        "last_signature": deepcopy(payload["last_signature"])
        if isinstance(payload.get("last_signature"), Mapping)
        else {},
        "note": payload.get("note") if isinstance(payload.get("note"), str) else "",
    }
    return entry


def _merge_history(*histories: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    merged: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for history in histories:
        for stamp in _normalized_history(history):
            key = (stamp["week"], stamp["status"])
            if key not in seen:
                seen.add(key)
                merged.append(stamp)
    return merged


def _is_full_snapshot(update: Mapping[str, Any]) -> bool:
    return any(key in update for key in ("history", "occurrences", "first_seen_week"))


def _merge_snapshot(
    existing: Mapping[str, Any] | None, update: Mapping[str, Any]
) -> dict[str, Any]:
    """Fusionne un instantané complet (type ``entry_from_item``) dans l'existant."""

    base = _normalized_entry(existing) if isinstance(existing, Mapping) else None
    incoming = _normalized_entry(update)
    entry = _blank_entry(incoming["id"])
    for field in _MEMORY_FIELDS:
        if field in ("first_seen_week", "last_seen_week"):
            continue
        entry[field] = incoming[field] if incoming[field] else (base or {}).get(field, entry[field])
    weeks = [
        value
        for value in (
            incoming["first_seen_week"],
            incoming["last_seen_week"],
            (base or {}).get("first_seen_week", ""),
            (base or {}).get("last_seen_week", ""),
        )
        if value
    ]
    entry["first_seen_week"] = min(weeks, key=_week_key) if weeks else ""
    entry["last_seen_week"] = max(weeks, key=_week_key) if weeks else ""
    base_occ = int((base or {}).get("occurrences", 0))
    # Un squelette entrant porte toujours occurrences=1 : on n'incrémenterait
    # jamais avec max(). Une semaine strictement plus récente = nouvelle
    # occurrence ; sinon (semaine identique ou antérieure) on garde le maximum.
    if incoming["last_seen_week"] and _week_key(incoming["last_seen_week"]) > _week_key(
        (base or {}).get("last_seen_week", "")
    ):
        entry["occurrences"] = base_occ + 1
    else:
        entry["occurrences"] = max(int(incoming["occurrences"]), base_occ)
    entry["history"] = _merge_history((base or {}).get("history", []), incoming["history"])
    return entry


def _merge_event(existing: Mapping[str, Any] | None, update: Mapping[str, Any]) -> dict[str, Any]:
    """Fusionne une mise à jour événementielle ({id, week?, status?, ...})."""

    entry = (
        deepcopy(dict(existing)) if isinstance(existing, Mapping) else _blank_entry(update["id"])
    )
    name = _text_or_none(update.get("name"))
    if name is not None:
        entry["name"] = name
    week = update.get("week") or update.get("last_seen_week")
    if _week_monday(week) is None:
        week = None
    if week is not None:
        if not entry.get("first_seen_week"):
            entry["first_seen_week"] = week
        entry["last_seen_week"] = week
        entry["occurrences"] = int(entry.get("occurrences") or 0) + 1
        status = update.get("status")
        if status in VALID_STATUSES:
            stamp = {"week": week, "status": status}
            if not entry["history"] or entry["history"][-1] != stamp:
                entry["history"].append(stamp)
    signature = update.get("signature") or update.get("last_signature")
    if isinstance(signature, Mapping) and signature:
        entry["last_signature"] = deepcopy(dict(signature))
    note = _text_or_none(update.get("note"))
    if note is not None:
        entry["note"] = note
    return entry


def _merged_entry(existing: Mapping[str, Any] | None, update: Mapping[str, Any]) -> dict[str, Any]:
    if _is_full_snapshot(update):
        return _merge_snapshot(existing, update)
    return _merge_event(existing, update)


# ------------------------------------------------------- lecture / écriture


def _read_entries(path: Path) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Toutes les entrées fusionnées par id (dernière ligne gagnante) + warnings."""

    entries: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return entries, warnings
    except OSError as exc:
        return entries, [f"cannot read {path}: {exc}"]
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            warnings.append(f"ligne {lineno}: JSON invalide")
            continue
        eid = payload.get("id") if isinstance(payload, Mapping) else None
        if not isinstance(eid, str) or not eid.strip():
            warnings.append(f"ligne {lineno}: entrée sans id valide")
            continue
        entries[eid] = _normalized_entry(payload)
    return entries, warnings


def _retained(entry: Mapping[str, Any], now: datetime) -> bool:
    last = _last_status(entry)
    # Statuts protégés et statut inconnu (ligne partielle) : conservation prudente.
    if last is None or last in _RETAINED_STATUSES:
        return True
    elapsed = weeks_between(str(entry.get("last_seen_week") or ""), week_of(now))
    return elapsed is None or elapsed <= RETENTION_WEEKS


def load_memory(
    path: Path, *, now: datetime | None = None
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Charge la mémoire : ({id: entrée}, warnings), purge > 26 semaines.

    Les lignes malformées sont skippées avec un avertissement. La purge épargne
    les entrées dont le dernier statut est ``recommended`` ou
    ``blocked-security``. ``now`` permet de figer la semaine courante en test.
    """

    entries, warnings = _read_entries(path)
    current = now.astimezone(UTC) if now is not None and now.tzinfo else (now or datetime.now(UTC))
    survivors = {eid: entry for eid, entry in entries.items() if _retained(entry, current)}
    return survivors, warnings


def append_entries(path: Path, updates: Iterable[Mapping[str, Any]]) -> list[str]:
    """Ajoute chaque update en JSONL append-only (une ligne, flush, crash-safe).

    Chaque update est fusionné avec l'entrée existante par id puis réécrit en
    entier sur une seule ligne flushée immédiatement ; un crash n'abîme donc
    jamais que la dernière ligne, ignorée à la lecture suivante. Retourne les
    avertissements (updates sans id valide inclus).
    """

    entries, warnings = _read_entries(path)
    written: list[str] = []
    for index, update in enumerate(updates):
        if not isinstance(update, Mapping):
            warnings.append(f"update {index}: doit être un objet")
            continue
        eid = update.get("id")
        if not isinstance(eid, str) or not eid.strip():
            warnings.append(f"update {index}: id manquant ou invalide")
            continue
        eid = eid.strip()
        entries[eid] = _merged_entry(entries.get(eid), {**update, "id": eid})
        written.append(eid)
    if written:
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            for eid in written:
                handle.write(json.dumps(entries[eid], ensure_ascii=False, sort_keys=True) + "\n")
                handle.flush()
            os.fsync(handle.fileno())
    return warnings


# ---------------------------------------------------------------- filtrage


def _last_status(entry: Mapping[str, Any]) -> str | None:
    """Dernier statut valide de l'historique (None si jamais statué)."""

    history = entry.get("history")
    if not isinstance(history, Sequence) or isinstance(history, str | bytes | bytearray):
        return None
    for stamp in reversed(history):
        if isinstance(stamp, Mapping) and stamp.get("status") in VALID_STATUSES:
            return str(stamp["status"])
    return None


def filter_items(
    items: list[dict[str, Any]], memory: dict[str, dict[str, Any]], week: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Sépare (kept, dropped[{id, reason}]) selon la mémoire inter-run.

    Un item ``ignored`` à signature inchangée est droppé ; il resurface dès que
    la version change ou qu'une publication plus récente apparaît. Tout item
    gardé porte ``_stale_seen`` (vu dans les 4 dernières semaines) pour le tri.
    """

    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        iid = normalize_id(
            str(item.get("name") or ""), item.get("npm_package"), item.get("repo_url")
        )
        entry = memory.get(iid)
        if (
            entry is not None
            and _last_status(entry) == "ignored"
            and not signature_changed(entry, item_signature(item))
        ):
            dropped.append({"id": iid, "reason": "ignored-unchanged"})
            continue
        result = dict(item)
        result["id"] = iid
        elapsed = weeks_between(str(entry.get("last_seen_week") or ""), week) if entry else None
        result["_stale_seen"] = elapsed is not None and elapsed < STALE_SEEN_WEEKS
        kept.append(result)
    return kept, dropped


# -------------------------------------------------------------------- digest


def _latest_status_week(entry: Mapping[str, Any], status: str) -> date:
    history = entry.get("history")
    latest = date.min
    if isinstance(history, Sequence) and not isinstance(history, str | bytes | bytearray):
        for stamp in history:
            if isinstance(stamp, Mapping) and stamp.get("status") == status:
                monday = _week_monday(stamp.get("week"))
                if monday is not None and monday > latest:
                    latest = monday
    return latest


def build_digest(memory: dict[str, dict[str, Any]], week: str) -> dict[str, Any]:
    """Résumé borné pour le prompt LLM : ignorés récents, recommandations, récurents."""

    del week  # la borne temporelle vient de la purge au chargement ; signature réservée
    recently_ignored: list[dict[str, str]] = []
    recommended: list[tuple[date, str]] = []
    recurrents: list[tuple[int, str]] = []
    for eid, entry in memory.items():
        if _last_status(entry) == "ignored":
            recently_ignored.append(
                {
                    "id": eid,
                    "week": str(entry.get("last_seen_week") or ""),
                    "note": str(entry.get("note") or ""),
                }
            )
        if _latest_status_week(entry, "recommended") > date.min:
            recommended.append((_latest_status_week(entry, "recommended"), eid))
        occurrences = entry.get("occurrences")
        if isinstance(occurrences, int) and occurrences >= RECURRENT_MIN_OCCURRENCES:
            recurrents.append((-occurrences, eid))

    recently_ignored.sort(key=lambda row: row["id"])
    recently_ignored.sort(key=lambda row: _week_key(row["week"]), reverse=True)
    del recently_ignored[MAX_RECENTLY_IGNORED:]
    recommended.sort(key=lambda pair: pair[1])
    recommended.sort(key=lambda pair: pair[0], reverse=True)
    recurrents.sort()
    return {
        "recently_ignored": recently_ignored,
        "previously_recommended": [
            eid for _latest, eid in recommended[:MAX_PREVIOUSLY_RECOMMENDED]
        ],
        "recurrents": [eid for _neg_occ, eid in recurrents[:RECURRENTS_CAP]],
    }
