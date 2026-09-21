"""Candidate selection for the LLM stages (Partie 3 §2 / Partie 4 §3) — pur.

The agent never picks sessions by judgment: audit candidates come from the
Part 1 JSON signals (top cost, outliers, loop, cache gap, repeated prompts),
deduplicated and priority-ordered; draft candidates come from the Part 3
findings archive (skill-candidate / command-candidate), capped by
`max_candidates_per_run`. Everything here is deterministic and unit-testable.
"""

from __future__ import annotations

import hashlib

#: types transmis à la Partie 4 pour drafting — création OU amélioration d'une
#: commande existante (v5.30, E : pattern coûteux lancé par une commande).
_DRAFT_TYPES = {"skill-candidate", "command-candidate", "command-improvement"}
_SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}
#: Seuil de coût fenêtré ($) au-delà duquel une session non-spec-driven est priorisée (P6).
_NON_SPEC_COST_MIN_USD = 1.0
#: Priorité des classes de signal (D1) — l'ordre du docstring, rendu explicite
#: pour que le coût puisse départager **à l'intérieur** d'une classe sans jamais
#: franchir une frontière de classe.
_SIGNAL_ORDER = (
    "top-cost",
    "cost-outlier",
    "loop",
    "cache-gap",
    "repeated-prompts",
    "code-non-relu",
    "maturity-F",
    "non-spec coûteuse",
)
_SIGNAL_RANK = {reason: rank for rank, reason in enumerate(_SIGNAL_ORDER)}

# Mots-clés heuristiques d'exclusion (R2 anti-learning) — DROP si présents.
_ANTI_LEARNING_SECRET = ("secret", "password", "token", "api key", "credential")
_ANTI_LEARNING_REF = ("pr #", "ticket", "jira", "gh-")
_ANTI_LEARNING_TRANSIENT = (
    "transient",
    "transitory",
    "flaky",
    "intermittent",
    "sporadic",
    "temporary failure",
)
_ANTI_LEARNING_ENV = (
    "environment-specific",
    "env-specific",
    "environment specific",
    "only in staging",
    "only in dev",
    "dev-only",
    "staging-only",
)
_ANTI_LEARNING_ONEOFF = (
    "one-off",
    "one off",
    "one-time",
    "one time",
    "single occurrence",
    "rare occurrence",
)


def generate_skill_id(name: str) -> str:
    """ID déterministe d'un skill : ``skill_`` + 8 hex de sha256(nom normalisé).

    Normalisation = ``strip().lower()`` (stable, idempotent, multi-plateforme).
    """
    digest = hashlib.sha256(name.strip().lower().encode("utf-8")).hexdigest()
    return f"skill_{digest[:8]}"


def is_anti_learning(finding: dict) -> bool:
    """Retourne True si le finding décrit un pattern à NE PAS capturer (DROP).

    Patterns exclus :
      - secret (secret, password, token, api key, credential)
      - référence PR/ticket (``PR #``, ``ticket``, ``JIRA``, ``GH-``)
      - échec transitoire / flaky / intermittent
      - prohibition spécifique à l'environnement
      - récit one-off (occurence unique, anecdotique)

    Inspecte ``description``, ``evidence`` et ``recommendation_type``.
    Heuristique simple et déterministe.
    """
    text = " ".join(
        str(finding.get(k, "")) for k in ("description", "evidence", "recommendation_type")
    ).lower()

    groups = (
        _ANTI_LEARNING_SECRET,
        _ANTI_LEARNING_REF,
        _ANTI_LEARNING_TRANSIENT,
        _ANTI_LEARNING_ENV,
        _ANTI_LEARNING_ONEOFF,
    )
    return any(any(w in text for w in group) for group in groups)


def _candidate_name(cand: dict) -> str:
    """Nom normalisé pour génération d'ID (fallback par pertinence)."""
    return str(
        cand.get("name")
        or cand.get("skill_name")
        or cand.get("recommendation")
        or cand.get("category")
        or cand.get("recommendation_type")
        or ""
    ).strip()


def consolidate_candidates(candidates: list[dict]) -> list[dict]:
    """Enrichit chaque candidat : create vs patch, attache skill_id/origin.

    - ``overlaps_with`` non vide OU ``skill_id`` existant → ``action='patch'``,
      ``target_skill_id`` = ``overlaps_with[0]`` (sinon l'id existant).
    - Sinon ``action='create'``.
    Toujours : ``skill_id=generate_skill_id(name)`` et ``origin='weekly-background'``.

    Fonction pure : retourne de nouveaux dicts (copie superficielle), ne mute
    pas l'entrée. Le tri/sévérité hérité des appelants est préservé.
    """
    result: list[dict] = []
    for cand in candidates:
        enriched = dict(cand)
        name = _candidate_name(enriched)
        existing_skill_id = enriched.get("skill_id")
        overlaps = enriched.get("overlaps_with") or []

        if overlaps or existing_skill_id:
            enriched["action"] = "patch"
            enriched["target_skill_id"] = overlaps[0] if overlaps else existing_skill_id
        else:
            enriched["action"] = "create"

        enriched["skill_id"] = generate_skill_id(name) if name else (existing_skill_id or "")
        enriched["origin"] = "weekly-background"
        result.append(enriched)
    return result


def _as_float(value: object) -> float:
    """Float défensif — champ absent, ``null`` ou non numérique vaut 0.0."""
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _entry_cost(entry: dict) -> float:
    return _as_float(entry.get("cost_usd"))


def select_audit_candidates(
    summary: dict,
    *,
    top_sessions_limit: int = 5,
    cost_per_active_minute_min: float = 0.5,
    cache_efficiency_gap: float = 0.2,
) -> list[dict]:
    """Ordered audit candidates (Partie 3 §2), priority: top cost, outliers,
    loop, cache gap, repeated prompts. One entry per session, reasons merged.

    D1 — le coût est un poids **À L'INTÉRIEUR** d'une classe de signal, pas un
    critère qui écrase les signaux. L'ordre reste primairement « priorité de la
    classe du premier signal » (`_SIGNAL_ORDER`, qui reproduit l'ordre
    d'insertion historique) ; le coût n'est qu'une clé secondaire : à signal
    équivalent, la session la plus coûteuse passe devant. Un tri coût global
    aurait écarté une session à 0,61 $ signalée « code-non-relu » au profit
    d'une Dependabot à 0,0018 $ signalée « repeated-prompts ».

    Chaque entrée porte `cost_usd` (index construit une fois depuis
    `top_sessions_by_cost` puis `all_sessions`, complété par le coût du payload
    signal) et `score`, le score composite qui produit exactement cet ordre :

        score = (nb_classes - rang_du_premier_signal) + 0.5 * part_de_cout

    La part de cout est normalisée sur le total des candidats et plafonnée à
    0.5 : elle ne peut donc jamais faire franchir une frontière de classe (les
    classes sont distantes de 1.0). `list.sort` étant stable, deux candidats de
    même classe et même coût gardent l'ordre d'insertion.

    NOT capped here — the caller slices by `audit_max_sessions` (audited vs
    unaudited).
    """
    ordered: list[dict] = []
    index: dict[str, int] = {}
    cost_index: dict[str, float] = {}
    #: coût porté par le payload signal déclencheur (outlier, classification…).
    source_cost: dict[str, float] = {}
    for bucket in ("top_sessions_by_cost", "all_sessions"):
        for row in summary.get(bucket) or []:
            if not isinstance(row, dict):
                continue
            sid = str(row.get("session_id") or "")
            if sid and row.get("cost_usd") is not None:
                cost_index.setdefault(sid, _as_float(row["cost_usd"]))
    # Orchestrators may copy the same outcomes into both top-level and
    # selection payloads. Merge by session id so provenance stays one-to-one.
    worker_statuses_by_id: dict[str, dict] = {}

    def _merge_worker_status(target: dict, source: dict) -> None:
        """Merge outcome metadata without allowing healthy duplicates to hide failures."""
        for key in ("session_id", "worker_status", "status"):
            if key in source and source[key] and key not in target:
                target[key] = source[key]
        if "rc" in source:
            try:
                rc = int(source["rc"])
            except (TypeError, ValueError):
                rc = 1
            try:
                target["rc"] = max(int(target.get("rc", 0) or 0), rc)
            except (TypeError, ValueError):
                target["rc"] = rc
        if "truncated" in source:
            target["truncated"] = bool(target.get("truncated", False) or source["truncated"])

    for status in [
        *(summary.get("worker_statuses") or []),
        *((summary.get("selection") or {}).get("worker_statuses") or []),
    ]:
        if isinstance(status, dict) and status.get("session_id"):
            session_id = str(status["session_id"])
            _merge_worker_status(worker_statuses_by_id.setdefault(session_id, {}), status)
    worker_statuses = list(worker_statuses_by_id.values())
    worker_status_by_id = {
        str(status.get("session_id")): status
        for status in worker_statuses
        if isinstance(status, dict) and status.get("session_id")
    }

    def _add(session_id: str, reason: str, source: dict | None = None) -> None:
        if not session_id:
            return
        if source and source.get("cost_usd") is not None:
            source_cost.setdefault(session_id, _as_float(source["cost_usd"]))
        if session_id in index:
            entry = ordered[index[session_id]]
            if reason not in entry["reasons"]:
                entry["reasons"].append(reason)
            status = dict(worker_status_by_id.get(session_id) or {})
            status.update(source or {})
            _merge_worker_status(entry, status)
            return
        index[session_id] = len(ordered)
        entry = {"session_id": session_id, "reasons": [reason]}
        status = dict(worker_status_by_id.get(session_id) or {})
        status.update(source or {})
        _merge_worker_status(entry, status)
        ordered.append(entry)

    top = summary.get("top_sessions_by_cost", [])
    for s in top[: max(0, top_sessions_limit)]:
        _add(str(s.get("session_id") or ""), "top-cost", s)
    for o in summary.get("cost_outliers", []):
        _add(str(o.get("session_id") or ""), "cost-outlier", o)
    weekly_cache = (summary.get("totals") or {}).get("cache_hit_rate")
    for s in top:
        cpm = s.get("cost_per_active_minute")
        if cpm is not None and cpm >= cost_per_active_minute_min:
            _add(str(s.get("session_id") or ""), "loop", s)
        ce = s.get("cache_efficiency")
        if weekly_cache is not None and ce is not None and ce < weekly_cache - cache_efficiency_gap:
            _add(str(s.get("session_id") or ""), "cache-gap", s)
    for r in summary.get("user_prompt_repeats", []):
        _add(str(r.get("session_id") or ""), "repeated-prompts", r)
    for sc in summary.get("session_classifications", []):
        sid = str(sc.get("session_id") or "")
        if not sid:
            continue
        measured = int(sc.get("production_review_measured") or 0)
        pct = sc.get("production_review_pct")
        if measured > 0 and pct is not None and float(pct) == 0.0:
            _add(sid, "code-non-relu", sc)
        if str(sc.get("prompt_maturity_grade") or "").upper() == "F":
            _add(sid, "maturity-F", sc)
        try:
            cost = float(sc.get("cost_usd") or 0.0)
        except (TypeError, ValueError):
            cost = 0.0
        if not sc.get("spec_driven") and cost >= _NON_SPEC_COST_MIN_USD:
            _add(sid, "non-spec coûteuse", sc)
    resolved = {
        str(entry["session_id"]): cost_index.get(
            str(entry["session_id"]), source_cost.get(str(entry["session_id"]), 0.0)
        )
        for entry in ordered
    }
    total_cost = sum(resolved.values())
    for entry in ordered:
        sid = str(entry["session_id"])
        cost = resolved[sid]
        entry["cost_usd"] = cost
        # Rang de la classe = rang du PREMIER signal de l'entrée.
        first_reason = str((entry.get("reasons") or [""])[0])
        rank = _SIGNAL_RANK.get(first_reason, len(_SIGNAL_ORDER))
        share = cost / total_cost if total_cost > 0 else 0.0
        entry["score"] = round((len(_SIGNAL_ORDER) - rank) + 0.5 * share, 6)
    # Tri stable : la classe de signal reste primaire, le coût est secondaire.
    ordered.sort(key=lambda e: -e["score"])
    return ordered


def split_audit_candidates(candidates: list[dict], limit: int) -> tuple[list[dict], list[dict]]:
    """Découpe (audités, reportés) selon `audit_max_sessions` — pur.

    Le second élément (`carried_over`) reprend les candidats non retenus par
    la limite pour reprise en tête de file au run suivant (P2 : fin du
    silently-ignored quand 9 candidats pour limite 8).
    """
    n = max(0, int(limit))
    return list(candidates[:n]), list(candidates[n:])


def prepend_carried_over(candidates: list[dict], previous_carried: list[dict] | None) -> list[dict]:
    """Remet en tête de file le `carried_over` du run précédent — pur.

    Déduplique par ``session_id`` (le candidat courant garde sa version
    fraîche) ; les entrées reportées réinjectées portent la raison
    ``carried-over`` pour traçabilité.

    D1 : le bloc réinjecté est trié par **coût décroissant** (stable). C'est le
    seul endroit où le backlog du run précédent réécrit l'ordre d'audit — sans ce
    tri, une session reportée à 0,0018 $ réinjectée en tête masque les sessions
    à 0,34–0,61 $ du run courant, quel que soit le tri de
    `select_audit_candidates`.
    Une entrée sans ``cost_usd`` (artefact antérieur à D1) vaut 0.0 : le tri
    stable lui conserve exactement l'ordre stocké.
    """
    if not previous_carried:
        return list(candidates)
    seen = {str(c.get("session_id")) for c in candidates if c.get("session_id")}
    front: list[dict] = []
    for entry in previous_carried:
        sid = str(entry.get("session_id") or "")
        if not sid or sid in seen:
            continue
        seen.add(sid)
        reinjected = dict(entry)
        reasons = list(reinjected.get("reasons") or [])
        if "carried-over" not in reasons:
            reasons.append("carried-over")
        reinjected["reasons"] = reasons
        front.append(reinjected)
    front.sort(key=lambda e: -_entry_cost(e))
    return front + list(candidates)


def select_draft_candidates(findings: dict | None, *, max_candidates: int = 3) -> list[dict]:
    """skill/command-candidate/command-improvement findings, severity DESC (tiebreak session_id),
    capped at `max_candidates` (Partie 4 §3 — plafond combiné skills+commands).
    """
    if not findings:
        return []
    candidates = [
        f
        for f in findings.get("findings", [])
        if not is_anti_learning(f)
        and f.get("recommendation_type") in _DRAFT_TYPES  # R2: drop anti-learning
    ]
    candidates.sort(
        key=lambda f: (_SEVERITY_ORDER.get(f.get("severity", "low"), 3), f.get("session_id") or "")
    )
    selected = candidates[: max(0, max_candidates)]
    return consolidate_candidates(selected)  # R1 provenance + R3 consolidation umbrella
