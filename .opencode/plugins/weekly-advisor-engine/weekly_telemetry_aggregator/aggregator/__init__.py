"""Pure aggregation logic (Part 1) — no I/O, no network, unit-testable in isolation.

Input: list of `SessionUsage` (window-filtered upstream) + options.
Output: `WeeklySummary` following the spec's normative serialization rules (v5.22/v5.25):
- `top_sessions_by_cost` ordered by (cost_usd DESC, session_id ASC)
- lists otherwise sorted by their stable alphabetical key
- amounts rounded to 6 decimals; divisions by zero → null; warnings capped at 50

SOUS-PAQUET. Ce module ne porte plus que l'assembleur `aggregate`, le plafond de
warnings `_cap_warnings`, les constantes du domaine et les factories d'accumulateurs ;
la région text/noise est partie dans `aggregator.text`. Le découpage suit les
dépendances, pas la taille : `aggregate` lisait ses 13 dépendances locales dans quatre
régions sans rapport du même fichier. Chaque nom public reste ré-exporté ici (`__all__`),
donc `from .aggregator import X` ne change pour aucun appelant.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from datetime import datetime

from ..classifiers import classify_sessions
from ..models import (
    MAX_WARNING_SESSION_SAMPLE,
    MAX_WARNINGS,
    OUTLIER_MIN_SESSIONS,
    DailyTotal,
    HarnessUsage,
    ModelUsage,
    Period,
    SessionUsage,
    WarningEntry,
    WeeklySummary,
    round6,
)
from ..util import root_and_orphan_ids

# --- régions repeats / outliers : ré-exportées depuis leurs sous-modules -------
from .outliers import (
    OUTLIER_MIN_ROOTS,
    RESUME_FINGERPRINT_TURNS,
    SKILL_BODY_WINDOW,
    SKILL_PAIRS_CAP,
    _cost_outliers,
    _robust_z,
    _skill_similar_pairs,
    compute_cost_outliers_state,
    dedup_resumed_usages,
    resume_fingerprint,
)
from .repeats import (
    PROMPT_REPEATS_CAP,
    _accumulate_repeat_turn,
    _accumulate_usage_repeats,
    _build_skill_draft,
    _emit_repeat_group,
    _emit_repeat_list,
    _is_repeat_candidate,
    _new_repeat_bucket,
    _prompt_repeat_groups,
    _repeat_examples,
)
from .text import (
    _CANCEL_TURNS,
    _CODE_BLOCK_RE,
    _COMPACTION_MARKERS,
    _CONTROL_TURNS,
    _CORRECTION_PREFIXES,
    _FINGERPRINT_MIN_TOKENS,
    _NOISE_MAX_CHARS,
    _NOISE_SEPARATOR_RE,
    _NOISE_YESNO_RE,
    _NON_WORD_RE,
    _NUMBER_RE,
    _POSIX_PATH_RE,
    _QUOTED_RE,
    _STOPWORD_SOURCE,
    _STOPWORDS,
    _TOOL_NARRATION_RE,
    _WINDOWS_PATH_RE,
    _canonical_prompt,
    _command_name,
    _is_cancel_turn,
    _is_compaction_artifact,
    _is_correction_turn,
    _strip_quotes,
    is_noise,
    is_tool_narration,
    normalize_fingerprint,
    normalize_prompt,
)

# --- region text/noise : ré-exportée depuis `aggregator.text` -------------------
# `from .aggregator import is_noise` doit continuer de résoudre : ces noms sont
# importés ailleurs (main.py, tests/, rule_loader via son propre exemplaire).
# --- région usage : ré-exportée depuis `aggregator.usage` -----------------------
from .usage import (
    ACTIVE_GAP_LIMIT,
    COMMAND_USAGE_COMPUTED,
    COMMAND_USAGE_NO_DATA,
    TOKENS_PER_CHAR,
    _build_command_usage,
    _build_skill_usage,
    _build_subagent_totals,
    _build_tool_usage,
    _cache_hit_rate,
    _descendants,
    _estimated_tokens,
    _is_exploitable_turn,
    _process_root,
    _RootTotals,
    _unmeasurable_warnings,
    _usage_agg,
    command_usage_state,
    session_duration,
)

__all__ = [
    "ACTIVE_GAP_LIMIT",
    "COMMAND_USAGE_COMPUTED",
    "COMMAND_USAGE_NO_DATA",
    "OUTLIER_MIN_ROOTS",
    "PROMPT_COMPARE_CHARS",
    "PROMPT_REPEATS_CAP",
    "RESUME_FINGERPRINT_TURNS",
    "SKILL_BODY_WINDOW",
    "SKILL_PAIRS_CAP",
    "TOKENS_PER_CHAR",
    "aggregate",
    "command_usage_state",
    "compute_cost_outliers_state",
    "dedup_resumed_usages",
    "is_noise",
    "is_tool_narration",
    "normalize_fingerprint",
    "normalize_prompt",
    "resume_fingerprint",
    "session_duration",
    "_CANCEL_TURNS",
    "_CODE_BLOCK_RE",
    "_COMPACTION_MARKERS",
    "_CONTROL_TURNS",
    "_CORRECTION_PREFIXES",
    "_FINGERPRINT_MIN_TOKENS",
    "_NOISE_MAX_CHARS",
    "_NOISE_SEPARATOR_RE",
    "_NOISE_YESNO_RE",
    "_NON_WORD_RE",
    "_NUMBER_RE",
    "_POSIX_PATH_RE",
    "_QUOTED_RE",
    "_RootTotals",
    "_STOPWORDS",
    "_STOPWORD_SOURCE",
    "_TOOL_NARRATION_RE",
    "_WINDOWS_PATH_RE",
    "_accumulate_repeat_turn",
    "_accumulate_usage_repeats",
    "_build_command_usage",
    "_build_skill_draft",
    "_build_skill_usage",
    "_build_subagent_totals",
    "_build_tool_usage",
    "_cache_hit_rate",
    "_canonical_prompt",
    "_cap_warnings",
    "_command_name",
    "_cost_outliers",
    "_descendants",
    "_emit_repeat_group",
    "_emit_repeat_list",
    "_estimated_tokens",
    "_is_cancel_turn",
    "_is_compaction_artifact",
    "_is_correction_turn",
    "_is_exploitable_turn",
    "_is_repeat_candidate",
    "_new_repeat_bucket",
    "_process_root",
    "_prompt_repeat_groups",
    "_repeat_examples",
    "_robust_z",
    "_skill_similar_pairs",
    "_strip_quotes",
    "_unmeasurable_warnings",
    "_usage_agg",
]


#: Quasi-duplicate prompt comparison window (first N normalized chars, v5.19).
PROMPT_COMPARE_CHARS = 100


def aggregate(
    usages: list[SessionUsage],
    *,
    period: Period,
    generated_at: datetime,
    top_sessions_limit: int = 5,
    include_subagents: bool = True,
    skill_catalog: list[str] | None = None,
    skill_catalog_entries=None,
    skill_catalog_snapshot: list[dict] | None = None,
    warnings: list[WarningEntry] | None = None,
    known_parent_ids: set[str] | None = None,
    session_outlier_z: float = 3.0,
    session_outlier_min_cost_usd: float = 0.5,
    outlier_min_sessions: int = OUTLIER_MIN_SESSIONS,
    user_prompt_repeat_min: int = 3,
    user_prompt_repeat_similarity: float = 0.9,
    user_prompt_repeat_min_chars: int = 80,
    skill_similarity_min: float = 0.8,
) -> WeeklySummary:
    """Aggregate window-filtered session usages into a WeeklySummary.

    Roots are sessions without a parent active in the window (children →
    subagent_totals). Orphans (parent declared but absent and not in
    `known_parent_ids`) are counted in subagent_totals without attachment and
    journalised. Totals merge children into their root exactly once.
    """
    catalog = sorted(set(skill_catalog or []))
    catalog_entries = sorted(skill_catalog_entries or [], key=lambda e: e.name)
    skills_targets = {e.name: list(e.target_agents) for e in catalog_entries if e.target_agents}
    all_warnings = list(warnings or [])

    by_id: dict[str, SessionUsage] = {u.session_id: u for u in usages}
    orphan_ids, root_ids = root_and_orphan_ids(
        ((u.session_id, u.parent_id) for u in usages), known_parent_ids=known_parent_ids
    )
    for sid in sorted(orphan_ids):
        all_warnings.append(
            WarningEntry(
                session_id=sid,
                message="orphan child session (parent not found); counted in subagent_totals",
            )
        )
    children = [u for u in usages if u.parent_id is not None and u.parent_id in by_id]
    roots = [u for u in usages if u.session_id in root_ids]
    children_ids = {u.session_id for u in children}

    # ---- totals (roots + children merged once) + per-root aggregated rows ----
    acc = _RootTotals()
    for root in sorted(roots, key=lambda u: u.session_id):
        _process_root(
            root, by_id, acc, include_subagents=include_subagents, children_ids=children_ids
        )
    totals = acc.totals
    root_costs = acc.root_costs
    model_agg = acc.model_agg
    harness_agg = acc.harness_agg
    day_cost = acc.day_cost
    day_tokens = acc.day_tokens
    day_cache = acc.day_cache
    day_fresh = acc.day_fresh
    top_sessions = acc.top_sessions

    totals.cache_hit_rate = _cache_hit_rate(
        float(totals.cache_read_tokens), float(totals.fresh_input_tokens)
    )
    daily_totals = [
        DailyTotal(
            date=day,
            cost_usd=day_cost[day],
            total_tokens=day_tokens[day],
            cache_hit_rate=round6(day_cache[day] / (day_cache[day] + day_fresh[day]))
            if (day_cache[day] + day_fresh[day]) > 0
            else None,
        )
        for day in sorted(day_cost)
    ]

    # ---- cost outliers (roots, window cost) ----
    cost_outliers = _cost_outliers(
        [(sid, root_costs[sid]) for sid in sorted(root_costs)],
        z_min=session_outlier_z,
        min_cost=session_outlier_min_cost_usd,
    )
    n_roots = len(roots)
    cost_outliers_state = compute_cost_outliers_state(n_roots, outlier_min_sessions, all_warnings)

    # ---- by_model (normalized provider/model keys) ----
    by_model = [
        ModelUsage(
            model=key,
            session_count=len(agg["sessions"]),
            total_tokens=agg["tokens"],
            total_cost_usd=round6(agg["cost"]),
            cache_hit_rate=_cache_hit_rate(agg["cache_read"], agg["fresh"]),
        )
        for key, agg in sorted(model_agg.items())
    ]

    # ---- by_harness (miroir de by_model : sessions, tokens, cost) ----
    by_harness = [
        HarnessUsage(
            harness=key,
            session_count=len(agg["sessions"]),
            total_tokens=agg["tokens"],
            total_cost_usd=round6(agg["cost"]),
            cache_hit_rate=_cache_hit_rate(agg["cache_read"], agg["fresh"]),
        )
        for key, agg in sorted(harness_agg.items())
    ]

    # ---- top sessions by cost: select top N, output ordered (cost DESC, session_id ASC) ----
    ordered_all = sorted(top_sessions, key=lambda s: (-s.cost_usd, s.session_id))
    selected = ordered_all[: max(0, top_sessions_limit)]

    # ---- tool usage (roots + children, window only) ----
    tool_usage, argument_fingerprints, result_fingerprints = _build_tool_usage(usages)

    # ---- skill usage + never loaded ----
    skill_usage, skills_never_loaded = _build_skill_usage(usages, catalog)

    # ---- command usage (v5.22) ----
    command_usage = _build_command_usage(usages)

    # ---- skill similar pairs (v5.25) ----
    skill_similar_pairs = _skill_similar_pairs(catalog_entries, skill_similarity_min)

    # ---- repeated user prompts (v5.15/v5.19) ----
    user_prompt_repeats = _prompt_repeat_groups(
        usages,
        repeat_min=user_prompt_repeat_min,
        similarity=user_prompt_repeat_similarity,
        min_chars=user_prompt_repeat_min_chars,
    )

    # ---- deterministic session classifications (P6) ----
    session_classifications = classify_sessions(usages)
    # De-noise: one aggregated review-unmeasurable warning per harness (not per session).
    all_warnings.extend(_unmeasurable_warnings(session_classifications))

    # ---- subagent totals (children + orphans, spec §8) ----
    subagent_totals = _build_subagent_totals(children, usages, orphan_ids)

    return WeeklySummary(
        period=period,
        generated_at=generated_at,
        totals=totals,
        daily_totals=daily_totals,
        by_model=by_model,
        by_harness=by_harness,
        top_sessions_by_cost=selected,
        all_sessions=ordered_all,
        cost_outliers=cost_outliers,
        cost_outliers_state=cost_outliers_state,
        tool_usage=tool_usage,
        skill_usage=skill_usage,
        command_usage=command_usage,
        skill_similar_pairs=skill_similar_pairs,
        skill_catalog_count=len(catalog),
        skill_catalog_entries=list(skill_catalog_snapshot or []),
        skills_never_loaded=skills_never_loaded,
        skills_targets=skills_targets,
        user_prompt_repeats=user_prompt_repeats,
        session_classifications=session_classifications,
        subagent_totals=subagent_totals,
        tool_argument_fingerprints={
            tool: {
                fingerprint: {"total": bucket["total"], "peak": bucket["peak"]}
                for fingerprint, bucket in sorted(values.items())
            }
            for tool, values in sorted(argument_fingerprints.items())
        },
        tool_result_fingerprints={
            tool: dict(sorted(values.items()))
            for tool, values in sorted(result_fingerprints.items())
        },
        warnings=_cap_warnings(all_warnings),
    )


def _cap_warnings(warnings: list[WarningEntry]) -> list[WarningEntry]:
    """Regroupe les messages identiques, puis plafonne à MAX_WARNINGS (D5).

    Avant D5, 50 warnings « session active exclue » (un par session exclue)
    saturaient le cap et évauchaient tous les autres messages du run. Le
    regroupement précède désormais le plafond : une entité par message distinct,
    porteuse de `count` (multiplicateur) et d'un échantillon de `session_ids`.

    `MAX_WARNINGS` plafonne les entités distinctes **et** le nombre de lignes.
    Les warnings à payload chiffré (``parts_cost``/``session_v2_cost``) ne sont
    jamais regroupés : ils sont un canal structuré consommé 1:1 par
    ``insights._collect_cost_discrepancies`` (K7) — ils passent donc en tête de
    cap, sinon un mur de messages informationnels affamerait ce canal.

    Idempotent : `count` est re-sommé et `session_ids` re-unifiés, donc un
    ré-cap (main.py) sur une liste déjà agrégée ne perd pas le multiplicateur.
    """
    plain: dict[str, list[WarningEntry]] = defaultdict(list)
    structured: list[WarningEntry] = []
    for w in warnings:
        if w.parts_cost is not None or w.session_v2_cost is not None:
            structured.append(w)
        else:
            plain[w.message].append(w)

    def _order(entries: list[WarningEntry]) -> list[WarningEntry]:
        return sorted(entries, key=lambda w: (w.session_id or "", w.message))

    entities: list[WarningEntry] = []
    for message in sorted(plain):
        group = plain[message]
        head = group[0]
        total = sum(e.count for e in group)
        sids = sorted(
            {e.session_id for e in group if e.session_id}
            | {sid for e in group for sid in (e.session_ids or [])}
        )
        entities.append(
            replace(
                head,
                # `any` et non `head.partial` : une entité groupée reste partial
                # si au moins une occurrence l'est (EXIT_PARTIAL, main.py:1090).
                partial=any(e.partial for e in group),
                count=total,
                session_ids=sids[:MAX_WARNING_SESSION_SAMPLE] or None,
            )
        )
    return [*_order(structured), *_order(entities)][:MAX_WARNINGS]
