"""Scoring déterministe et screening sécurité de la veille écosystème.

Partie « cœur » du distill : chaque item issu de ``releases.py`` reçoit un
score 0-100 reproductible (``score_item``), passe des heuristiques locales de
détection supply-chain (``screen_item`` : exfiltration d'env, injection de
prompt, chemins d'identifiants, typosquatting) puis est ordonné par ``rank``
avec un tie-breaker déterministe. Aucun réseau, aucun LLM : tout est calcul
local pur, les dates étant fournies par l'appelant (ou l'horloge process pour
la seule fenêtre « récent sans traction » du screening).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from .util import parse_iso_ts
from .watch_context import normalize_npm_package

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
    has_traction = isinstance(stars, int | float) and not isinstance(stars, bool) and stars > 0
    if published is not None and not has_traction:
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
