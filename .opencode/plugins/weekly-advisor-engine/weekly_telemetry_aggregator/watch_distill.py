"""Scoring déterministe et screening sécurité de la veille écosystème.

Partie « cœur » du distill : chaque item issu de ``releases.py`` reçoit un
score 0-100 reproductible (``score_item``), passe des heuristiques locales de
détection supply-chain (``screen_item`` : exfiltration d'env, injection de
prompt, chemins d'identifiants, typosquatting) puis est ordonné par ``rank``
avec un tie-breaker déterministe. Aucun réseau, aucun LLM : tout est calcul
local pur, les dates étant fournies par l'appelant (ou l'horloge process pour
la seule fenêtre « récent sans traction » du screening).

L'orchestration (``run``, étape 2.2) enchaîne fusion multi-sources → screen →
score → mémoire inter-run → quotas, écrit ``watch-candidates-<date>.json`` +
``watch-memory-digest-<date>.json`` dans le répertoire du run actif et met à
jour la mémoire append-only. Toute exception dégénère en ``mode=fallback``
(exit 1) afin que l'aval retombe sur le flux legacy sans jamais crasher.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .util import load_jsonc, parse_anchor, parse_iso_ts
from .watch_context import normalize_npm_package
from .watch_memory import (
    VALID_STATUSES,
    append_entries,
    build_digest,
    entry_from_item,
    filter_items,
    item_signature,
    load_memory,
    normalize_id,
    week_of,
)

#: Pondérations par défaut (somme = 100).
DEFAULT_WEIGHTS = {
    "authority": 25,
    "relevance": 30,
    "freshness": 20,
    "multi_source": 15,
    "traction": 10,
}

#: Autorité maximale selon la source de découverte (clé parsée de ``found_via``).
AUTHORITY_BY_SOURCE = {"repo": 25, "mcp": 20, "topic": 16, "npm": 14, "rss": 10, "radar": 8}

#: Mots-clés de pertinence, complétés par ``cfg.release_keywords`` à l'appel.
RELEVANCE_KEYWORDS = (
    "skill",
    "plugin",
    "agent",
    "context",
    "cache",
    "compaction",
    "token",
    "prompt",
    "mcp",
    "skill.md",
)

#: Paquets cœur de l'écosystème — cibles du typosquatting (distance ≤ 2).
CORE_PKGS = ("opencode-plugin", "@opencode-ai/plugin", "@opencode/plugin")

_FRESHNESS_DAYS = 90
_RECENT_DAYS = 30
_TRACTION_DIVISOR = 50
_TYPOSQUAT_DISTANCE = 2

# Heuristiques bloquantes (précision privilégiée ; pas de réseau).
_RE_ENV_EXFIL = re.compile(r"\b(?:curl|wget)\b[^\n]{0,120}\$\{?[A-Z][A-Z0-9]*(?:_[A-Z0-9_]+)+\}?")
_RE_PROMPT_INJECTION = re.compile(
    r"\b(?:ignore|disregard|forget)\b[^.\n]{0,60}\b(?:previous|prior|above|earlier)\b"
    r"|(?i:\bexfiltrat\w*)"
    r"|(?i:\bprompt[\s-]?injection\b)"
)
_RE_CREDENTIAL_PATH = re.compile(
    r"(?:\.ssh[/\\](?:id_|identity)|\.aws[/\\]credentials|\.netrc"
    r"|\.npmrc|\.pypirc|\.git-credentials|credentials\.json)",
    re.IGNORECASE,
)

# Heuristiques suspectes.
_RE_POSTINSTALL = re.compile(r"\bpost[\s_-]?install\b", re.IGNORECASE)


# ------------------------------------------------------------------ helpers


def _published_dt(value: object) -> datetime | None:
    """``published_at`` en datetime conscient UTC (naïf ⇒ UTC) ; None sinon."""

    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value
    parsed = parse_iso_ts(value)
    return parsed


def _levenshtein(a: str, b: str) -> int:
    """Distance de Levenshtein (DP deux lignes, stdlib, sans dépendance)."""

    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, start=1):
        current = [i]
        for j, char_b in enumerate(b, start=1):
            current.append(
                min(current[-1] + 1, previous[j] + 1, previous[j - 1] + (char_a != char_b))
            )
        previous = current
    return previous[-1]


def _authority_source(found_via_entry: object) -> str | None:
    """Clé de source autorité d'une entrée ``found_via`` (None si inconnue).

    ``"watch:repo:x"`` → ``"repo"`` ; ``"github:topic:t"`` → ``"topic"`` ;
    ``npm``/``mcp``/``rss``/``radar`` sont leurs propres clés.
    """

    if not isinstance(found_via_entry, str):
        return None
    parts = found_via_entry.strip().casefold().split(":")
    head = parts[0]
    if head in AUTHORITY_BY_SOURCE:
        return head
    if head == "github":
        return "topic" if len(parts) >= 2 and parts[1] == "topic" else None
    if head == "watch":
        return parts[1] if len(parts) >= 2 and parts[1] in AUTHORITY_BY_SOURCE else None
    return None


def _round(value: float) -> float:
    return round(float(value), 3)


# ------------------------------------------------------------------- scoring


def score_item(
    item: Mapping[str, Any],
    *,
    weights: dict[str, int],
    now: datetime,
    extra_keywords: Sequence[str],
) -> dict[str, Any]:
    """Score 0-100 d'un item : ``{"total", "breakdown"}`` déterministe.

    - ``authority`` : max de ``AUTHORITY_BY_SOURCE`` sur ``found_via`` parsés.
    - ``relevance`` : part des mots-clés uniques présents dans nom+description,
      plafonnée au poids (``RELEVANCE_KEYWORDS`` + ``extra_keywords``).
    - ``freshness`` : décroissance linéaire sur 90 jours depuis la publication ;
      0 si la date est absente ou illisible.
    - ``multi_source`` : poids plein dès 2 entrées ``found_via`` distinctes.
    - ``traction`` : ``stars/50`` plafonné, sinon valeur neutre (moitié du poids).

    Le total est la somme du breakdown, borné à [0, 100].
    """

    w_authority = weights.get("authority", DEFAULT_WEIGHTS["authority"])
    w_relevance = weights.get("relevance", DEFAULT_WEIGHTS["relevance"])
    w_freshness = weights.get("freshness", DEFAULT_WEIGHTS["freshness"])
    w_multi = weights.get("multi_source", DEFAULT_WEIGHTS["multi_source"])
    w_traction = weights.get("traction", DEFAULT_WEIGHTS["traction"])

    found_via = [
        entry for entry in (item.get("found_via") or []) if isinstance(entry, str)
    ]
    authorities = [
        AUTHORITY_BY_SOURCE[source]
        for source in (_authority_source(entry) for entry in found_via)
        if source is not None
    ]
    authority = _round(min(w_authority, max(authorities, default=0)))

    haystack = f"{item.get('name') or ''}\n{item.get('description') or ''}".casefold()
    keywords = tuple(dict.fromkeys((*RELEVANCE_KEYWORDS, *(k.casefold() for k in extra_keywords))))
    matches = sum(1 for keyword in keywords if keyword and keyword in haystack)
    relevance = (
        _round(min(w_relevance, w_relevance * matches / len(keywords))) if keywords else 0.0
    )

    published = _published_dt(item.get("published_at"))
    anchor = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
    if published is not None:
        age_days = max(0.0, (anchor - published).total_seconds() / 86400)
        freshness = _round(w_freshness * max(0.0, 1 - age_days / _FRESHNESS_DAYS))
    else:
        freshness = 0.0

    multi_source = _round(w_multi if len(set(found_via)) >= 2 else 0)

    stars = item.get("stars")
    if isinstance(stars, int | float) and not isinstance(stars, bool) and stars > 0:
        traction = _round(min(w_traction, stars / _TRACTION_DIVISOR))
    else:
        traction = _round(w_traction / 2)

    breakdown = {
        "authority": authority,
        "relevance": relevance,
        "freshness": freshness,
        "multi_source": multi_source,
        "traction": traction,
    }
    total = min(100.0, max(0.0, _round(sum(breakdown.values()))))
    return {"total": total, "breakdown": breakdown}


# ----------------------------------------------------------------- screening


def _typosquat_target(item: Mapping[str, Any]) -> str | None:
    """Identité npm normalisée de l'item (DRY ``normalize_npm_package``)."""

    candidate = item.get("npm_package") or item.get("name")
    if not isinstance(candidate, str):
        return None
    return normalize_npm_package(candidate)


def screen_item(item: Mapping[str, Any]) -> tuple[str, str | None]:
    """Heuristiques supply-chain locales : ``(clean|suspicious|blocked, raison)``.

    ``blocked`` : exfiltration de variable d'env via curl/wget, injection de
    prompt/exfiltration explicite, chemin d'identifiants (nom+description),
    ou typosquat (Levenshtein ≤ 2 contre ``CORE_PKGS``, hors identité exacte).
    ``suspicious`` : ratio de majuscules > 0.5 dans la description, mention
    postinstall, ou publication < 30 jours sans aucune traction (stars).
    """

    name = item.get("name") if isinstance(item.get("name"), str) else ""
    description = item.get("description") if isinstance(item.get("description"), str) else ""
    text = f"{name}\n{description}"

    if _RE_ENV_EXFIL.search(text):
        return "blocked", "env-exfiltration"
    if _RE_PROMPT_INJECTION.search(text):
        return "blocked", "prompt-injection"
    if _RE_CREDENTIAL_PATH.search(text):
        return "blocked", "credential-path"

    target = _typosquat_target(item)
    if target is not None and target not in CORE_PKGS:
        for core in CORE_PKGS:
            if _levenshtein(target, core) <= _TYPOSQUAT_DISTANCE:
                return "blocked", f"typosquat:{core}"

    letters = [char for char in description if char.isalpha()]
    if len(letters) >= 8 and sum(1 for char in letters if char.isupper()) / len(letters) > 0.5:
        return "suspicious", "description-caps"
    if _RE_POSTINSTALL.search(text):
        return "suspicious", "postinstall"

    published = _published_dt(item.get("published_at"))
    stars = item.get("stars")
    # Absence de donnée traction (None, typique npm) ≠ zéro traction : ne pas flagger.
    zero_traction = isinstance(stars, int | float) and not isinstance(stars, bool) and stars <= 0
    if published is not None and zero_traction:
        age_days = (datetime.now(UTC) - published.astimezone(UTC)).total_seconds() / 86400
        if 0 <= age_days < _RECENT_DAYS:
            return "suspicious", "recent-no-traction"

    return "clean", None


# ---------------------------------------------------------------------- rank


def rank(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Tri reproductible : ``(-score.total, -published_ts, id)``.

    Retourne une nouvelle liste (l'entrée n'est pas mutée) ; les items sans
    score/date tombent sur les valeurs neutres du tie-breaker.
    """

    epoch_min = datetime.min.replace(tzinfo=UTC)

    def sort_key(candidate: Mapping[str, Any]) -> tuple[float, float, str]:
        score = candidate.get("score")
        total = score.get("total", 0) if isinstance(score, Mapping) else 0
        published = _published_dt(candidate.get("published_at")) or epoch_min
        cid = candidate.get("id")
        return (-float(total), -published.timestamp(), str(cid) if isinstance(cid, str) else "")

    return sorted(candidates, key=sort_key)


# ------------------------------------------------------- orchestration (étape 2.2)

#: Quotas par catégorie de candidat ; le reste des slots ``top_n`` est rempli
#: au meilleur score brut toutes catégories confondues.
QUOTAS = {"new": 12, "improvable": 8, "resurfaced": 5}

#: Clés exactes d'une fiche candidate (``local_relevance_hints`` est rempli
#: par l'étape 7 — contexte projet local ; vide ici).
FICHE_KEYS = (
    "id",
    "name",
    "sources",
    "score",
    "security",
    "summary",
    "signature",
    "local_relevance_hints",
)

SCHEMA_VERSION = 1
SUMMARY_MAX_CHARS = 200
DEFAULT_TOP_N = 30
DEFAULT_MEMORY_FILE = "watch-memory.jsonl"
_QUOTA_ORDER = ("new", "improvable", "resurfaced")


def truncate_summary(description: object, limit: int = SUMMARY_MAX_CHARS) -> str:
    """Description blanchie (espaces) et tronquée à ``limit`` chars, 1-2 phrases.

    Coupe de préférence en fin de phrase ; sinon coupure franche avec ellipse.
    """

    text = " ".join(str(description or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    dot = cut.rfind(". ")
    if dot >= limit // 3:
        return cut[: dot + 1]
    # Ellipse : borne à limit-1 AVANT l'ellipse (sinon 200 + 1 = 201, cf. revue).
    return cut.rstrip()[: limit - 1].rstrip() + "…"


def _last_status(entry: Mapping[str, Any] | None) -> str | None:
    """Miroir local de ``watch_memory._last_status`` (interface T1 figée)."""

    if entry is None:
        return None
    history = entry.get("history")
    if not isinstance(history, Sequence) or isinstance(history, str | bytes | bytearray):
        return None
    for stamp in reversed(history):
        if isinstance(stamp, Mapping) and stamp.get("status") in VALID_STATUSES:
            return str(stamp["status"])
    return None


def _category(entry: Mapping[str, Any] | None) -> str:
    """Bucket quotas d'un candidat selon la mémoire : new/improvable/resurfaced.

    - absent de la mémoire → ``new`` ;
    - dernier statut ``ignored`` (resurfaccé car signature changée) →
      ``resurfaced`` ;
    - toute autre entrée connue (seen/candidate/recommended/blocked-security)
      → ``improvable``.
    """

    if entry is None:
        return "new"
    return "resurfaced" if _last_status(entry) == "ignored" else "improvable"


def _fuse_items(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Fusionne les doublons multi-sources par ``normalize_id``.

    Même identité → un item : ``found_via`` est l'union ordonnée (première
    occurrence gagne), la description **la plus longue** gagne et emporte ses
    champs scalaires (nom, version, date, stars). L'ordre d'entrée est conservé.
    """

    fused: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        iid = normalize_id(
            str(item.get("name") or ""), item.get("npm_package"), item.get("repo_url")
        )
        record = dict(item)
        record["id"] = iid
        existing = fused.get(iid)
        if existing is None:
            fused[iid] = record
            order.append(iid)
            continue
        for source in record.get("found_via") or []:
            if isinstance(source, str) and source not in existing.setdefault("found_via", []):
                existing["found_via"].append(source)
        if len(str(record.get("description") or "")) > len(
            str(existing.get("description") or "")
        ):
            # La description gagnante met à jour les scalaires, jamais id/sources.
            for key, value in record.items():
                if key not in ("id", "found_via"):
                    existing[key] = value
    return [fused[iid] for iid in order]


def _ecosystem_items(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Items d'un rapport écosystème (``new_items``, repli ``items``)."""

    for key in ("new_items", "items"):
        values = payload.get(key)
        if isinstance(values, Sequence) and not isinstance(values, str | bytes | bytearray):
            return [value for value in values if isinstance(value, Mapping)]
    return []


def _apply_quotas(
    ranked: list[dict[str, Any]],
    memory: Mapping[str, Mapping[str, Any]],
    *,
    top_n: int,
    quotas: Mapping[str, int],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Sélection bornée par ``top_n`` : quotas par catégorie puis meilleur score.

    Retourne ``(candidates, quotas_applied)`` où ``quotas_applied`` compte les
    sélectionnés par catégorie (+ ``fill_best`` pour le remplissage score).
    """

    buckets: dict[str, list[dict[str, Any]]] = {cat: [] for cat in _QUOTA_ORDER}
    for candidate in ranked:
        bucket = buckets.get(_category(memory.get(str(candidate.get("id")))))
        if bucket is not None:
            bucket.append(candidate)
    selected: list[dict[str, Any]] = []
    selected_ids: set[str] = set()
    applied: dict[str, int] = {}
    for cat in _QUOTA_ORDER:
        take = buckets[cat][: max(0, min(int(quotas.get(cat, 0)), top_n - len(selected)))]
        applied[cat] = len(take)
        selected.extend(take)
        selected_ids.update(str(candidate.get("id")) for candidate in take)
    fill = [
        candidate
        for candidate in ranked
        if str(candidate.get("id")) not in selected_ids
    ][: max(0, top_n - len(selected))]
    applied["fill_best"] = len(fill)
    return rank(selected + fill), applied


def _build_fiche(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Projette un candidat interne sur le contrat ``FICHE_KEYS``."""

    security = candidate.get("security")
    verdict = security.get("verdict") if isinstance(security, Mapping) else "clean"
    reason = security.get("reason") if isinstance(security, Mapping) else None
    score = candidate.get("score")
    breakdown = score.get("breakdown") if isinstance(score, Mapping) else {}
    return {
        "id": str(candidate.get("id")),
        "name": str(candidate.get("name") or ""),
        "sources": [str(s) for s in candidate.get("found_via") or []],
        "score": {
            "total": score.get("total") if isinstance(score, Mapping) else 0,
            "breakdown": dict(breakdown) if isinstance(breakdown, Mapping) else {},
        },
        "security": {"verdict": verdict, "reason": reason},
        "summary": truncate_summary(candidate.get("description")),
        "signature": item_signature(candidate),
        "local_relevance_hints": [],
    }


def _fallback(warnings: list[str], exit_code: int) -> tuple[dict[str, Any], int]:
    """Résultat dégradé : l'agent orchestrateur retombe sur le flux legacy."""

    return (
        {
            "schema_version": SCHEMA_VERSION,
            "mode": "fallback",
            "candidates": [],
            "security_annex": [],
            "dropped_memory": 0,
            "quotas_applied": {},
            "warnings": warnings,
        },
        exit_code,
    )


def run(cfg, *, anchor: str | None = None) -> tuple[dict[str, Any], int]:
    """Étape 2.2 : distill déterministe du rapport écosystème daté du run.

    Lit ``<run_dir>/weekly-ecosystem-<date>.json``, fusionne par identité,
    screen + score, filtre par la mémoire inter-run, applique les quotas puis
    écrit fiches + digest mémoire dans le répertoire du run actif.

    Retourne ``(résultat, exit_code)`` :
    - 0 = distill complet (mode ``distill``) ;
    - 2 = dépendance absente ou étape désactivée (écosystème manquant,
      ``watch_distill.enabled=false``) — fallback aval garanti ;
    - 1 = exception moteur inattendue — fallback aval garanti, mémoire intacte.

    La mémoire est mise à jour APRÈS la sélection : seuls les items gardés
    (non ignorés-unchanged, non bloqués en amont du filtrage) sont ré-enregistrés
    comme vus cette semaine, afin de ne jamais retourner le dernier statut
    ``ignored`` d'une entrée droppée.
    """

    try:
        return _run_impl(cfg, anchor=anchor)
    except Exception as exc:  # noqa: BLE001 — dégradation gracieuse contractualisée
        return _fallback([f"{type(exc).__name__}: {exc}"], 1)


def _run_impl(cfg, *, anchor: str | None = None) -> tuple[dict[str, Any], int]:
    from .run_state import resolve_active_run_dir
    from .writer import write_json_atomic

    wd_cfg = getattr(cfg, "watch_distill", None)
    warnings: list[str] = []
    if wd_cfg is not None and getattr(wd_cfg, "enabled", True) is False:
        return _fallback(["watch_distill désactivé (config watch_distill.enabled=false)"], 2)

    run_time = parse_anchor(anchor)
    date = run_time.strftime("%Y-%m-%d")
    week = week_of(run_time)
    out = resolve_active_run_dir(Path(cfg.output_dir), date)
    ecosystem_path = out / f"weekly-ecosystem-{date}.json"
    payload = load_jsonc(ecosystem_path)
    if payload is None:
        return _fallback(
            [f"DÉPENDANCE: écosystème absent ou illisible ({ecosystem_path.name}) "
             "— exécuter releases (étape 2) d'abord"],
            2,
        )

    weights = dict(getattr(wd_cfg, "weights", None) or DEFAULT_WEIGHTS)
    extra_keywords = tuple(getattr(cfg, "release_keywords", None) or ())
    memory_file = Path(getattr(wd_cfg, "memory_file", None) or DEFAULT_MEMORY_FILE)
    memory_path = memory_file if memory_file.is_absolute() else Path(cfg.output_dir) / memory_file

    # Mémoire absente/corrompue → warning + démarrage à vide (jamais crash).
    memory, load_warnings = load_memory(memory_path, now=run_time)
    warnings.extend(load_warnings)

    scored: list[dict[str, Any]] = []
    security_annex: list[dict[str, Any]] = []
    for item in _fuse_items(_ecosystem_items(payload)):
        verdict, reason = screen_item(item)
        if verdict == "blocked":
            security_annex.append(
                {"id": str(item.get("id")), "name": str(item.get("name") or ""), "reason": reason}
            )
            continue
        item["security"] = {"verdict": verdict, "reason": reason}
        item["score"] = score_item(
            item, weights=weights, now=run_time, extra_keywords=extra_keywords
        )
        scored.append(item)
    security_annex.sort(key=lambda row: row["id"])

    kept, dropped = filter_items(scored, memory, week)
    top_n = int(getattr(wd_cfg, "top_n", None) or DEFAULT_TOP_N)
    raw_quotas = getattr(wd_cfg, "quotas", None) or QUOTAS
    quotas = {
        cat: int(raw_quotas.get(cat, QUOTAS[cat])) if isinstance(raw_quotas, Mapping) else QUOTAS[cat]
        for cat in _QUOTA_ORDER
    }
    selected, quotas_applied = _apply_quotas(rank(kept), memory, top_n=top_n, quotas=quotas)
    candidates = [_build_fiche(candidate) for candidate in selected]

    # Persistance mémoire : snapshots « seen » des gardés uniquement (cf. docstring),
    # puis rechargement pour un digest à jour (occurrences de la semaine incluses).
    warnings.extend(append_entries(memory_path, [entry_from_item(item, week) for item in kept]))
    memory_post, post_warnings = load_memory(memory_path, now=run_time)
    warnings.extend(post_warnings)

    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "mode": "distill",
        "date": date,
        "candidates": candidates,
        "security_annex": security_annex,
        "dropped_memory": len(dropped),
        "quotas_applied": quotas_applied,
        "warnings": warnings,
    }
    digest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "week": week,
        **build_digest(memory_post, week),
    }
    write_json_atomic(out / f"watch-candidates-{date}.json", result, indent=None)
    write_json_atomic(out / f"watch-memory-digest-{date}.json", digest, indent=None)
    return result, 0
