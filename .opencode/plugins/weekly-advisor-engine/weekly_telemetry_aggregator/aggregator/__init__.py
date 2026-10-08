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
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from ..classifiers import classify_sessions
from ..models import (
    MAX_WARNING_SESSION_SAMPLE,
    MAX_WARNINGS,
    OUTLIER_MIN_SESSIONS,
    AgentTypeUsage,
    CommandUsage,
    DailyTotal,
    HarnessUsage,
    ModelUsage,
    Period,
    SessionUsage,
    SkillUsage,
    SubagentTotals,
    ToolUsage,
    TopSession,
    Totals,
    WarningEntry,
    WeeklySummary,
    round6,
    split_canonical_session_id,
)
from ..util import descendants_by_parent, root_and_orphan_ids

# --- region text/noise : ré-exportée depuis `aggregator.text` -------------------
# `from .aggregator import is_noise` doit continuer de résoudre : ces noms sont
# importés ailleurs (main.py, tests/, rule_loader via son propre exemplaire).
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


#: Gaps strictly below this threshold count as "active" time (5 minutes).
ACTIVE_GAP_LIMIT = timedelta(minutes=5)
#: Estimated tokens per tool call = len(args) / 4 (documented approximation).
TOKENS_PER_CHAR = 4
#: Quasi-duplicate prompt comparison window (first N normalized chars, v5.19).
PROMPT_COMPARE_CHARS = 100


def _estimated_tokens(arg_chars: int) -> int:
    return round(arg_chars / TOKENS_PER_CHAR)


def _cache_hit_rate(cache_read: float, fresh_input: float) -> float | None:
    """Unique formula: cache_read / (cache_read + fresh_input); null on no input.

    cache_write is a cost, not a miss — it never enters the denominator.
    """
    denom = cache_read + fresh_input
    if denom <= 0:
        return None
    return round6(cache_read / denom)


def session_duration(usage: SessionUsage) -> tuple[int, int]:
    """(duration_seconds, active_time_seconds) for one session, from its window steps."""
    steps = sorted(usage.steps, key=lambda s: s.timestamp)
    if not steps:
        return 0, 0
    first, last = steps[0].timestamp, steps[-1].timestamp
    duration = max(0, int((last - first).total_seconds()))
    active = 0
    prev = first
    for step in steps[1:]:
        gap = step.timestamp - prev
        if gap < ACTIVE_GAP_LIMIT:
            active += int(gap.total_seconds())
        prev = step.timestamp
    return duration, active


def _descendants(by_id: dict[str, SessionUsage], root: SessionUsage) -> list[SessionUsage]:
    """All direct/indirect children of a root session (indexed BFS, util)."""
    ids = descendants_by_parent(
        ((u.session_id, u.parent_id) for u in by_id.values()), root.session_id
    )
    return [by_id[sid] for sid in ids]


def _build_tool_usage(
    usages: list[SessionUsage],
) -> tuple[list[ToolUsage], dict[str, dict[str, dict[str, int]]], dict[str, dict[str, int]]]:
    """Tool usage + fingerprints (roots + children, window only).

    Argument buckets carry **two** numbers per fingerprint: ``total`` (identical
    calls over the whole window) and ``peak`` (identical calls in the busiest
    *single* session). An agent loop is N identical calls inside one session; a
    cron tool is 1 identical call per run, so its window `total` climbs to 13
    while its `peak` stays at 1. Summing alone flattened the two into an
    indistinguishable counter — the `count` of a `SessionUsage` is already
    per-session, so the peak is a plain `max` over the sessions and costs nothing
    to keep (nothing is recomputed from the raw payloads).
    """
    tool_counts: dict[str, int] = defaultdict(int)
    tool_chars: dict[str, int] = defaultdict(int)
    argument_fingerprints: dict[str, dict[str, dict[str, int]]] = defaultdict(
        lambda: defaultdict(lambda: {"total": 0, "peak": 0})
    )
    result_fingerprints: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for u in usages:
        for tool, count in u.tool_calls.items():
            tool_counts[tool] += count
            tool_chars[tool] += u.tool_arg_chars.get(tool, 0)
        for tool, fingerprints in u.tool_arg_fingerprints.items():
            for fingerprint, count in fingerprints.items():
                bucket = argument_fingerprints[tool][fingerprint]
                bucket["total"] += count
                bucket["peak"] = max(bucket["peak"], count)
        for tool, fingerprints in u.tool_result_fingerprints.items():
            for fingerprint, count in fingerprints.items():
                result_fingerprints[tool][fingerprint] += count
    tool_usage = [
        ToolUsage(
            tool=t, call_count=tool_counts[t], estimated_tokens=_estimated_tokens(tool_chars[t])
        )
        for t in sorted(tool_counts)
    ]
    return tool_usage, argument_fingerprints, result_fingerprints


def _build_skill_usage(
    usages: list[SessionUsage], catalog: list[str]
) -> tuple[list[SkillUsage], list[str]]:
    """Skill usage + never-loaded list."""
    skill_counts: dict[str, int] = defaultdict(int)
    skill_sessions: dict[str, set] = defaultdict(set)
    for u in usages:
        for skill, count in u.skills_loaded.items():
            skill_counts[skill] += count
            skill_sessions[skill].add(u.session_id)
    skill_usage = [
        SkillUsage(skill=s, load_count=skill_counts[s], sessions_used_in=len(skill_sessions[s]))
        for s in sorted(skill_counts)
    ]
    loaded = set(skill_counts)
    return skill_usage, [s for s in catalog if s not in loaded]


def _build_command_usage(usages: list[SessionUsage]) -> list[CommandUsage]:
    """Command usage (v5.22). B5.1 : tours filtrés par `_command_name` (narration écartée,
    guillemets retirés) — plus aucun tour non exempt ne produit de commande."""
    command_counts: dict[str, int] = defaultdict(int)
    command_sessions: dict[str, set] = defaultdict(set)
    for u in usages:
        for turn in u.user_turns:
            name = _command_name(turn)
            if name is None:
                continue
            command_counts[name] += 1
            command_sessions[name].add(u.session_id)
    return [
        CommandUsage(
            command=c, call_count=command_counts[c], sessions_used_in=len(command_sessions[c])
        )
        for c in sorted(command_counts)
    ]


#: États de `command_usage_state` (B5.2) — le gabarit doit pouvoir distinguer
#: « aucune commande slash dans le corpus » de « commandes slash NON MESURÉES ».
COMMAND_USAGE_COMPUTED = "computed"
COMMAND_USAGE_NO_DATA = "no-data"


def command_usage_state(
    usages: list[SessionUsage], command_usage: list[CommandUsage] | None = None
) -> str:
    """`computed` | `no-data` — le compte 0 n'est pas une mesure (B5.2).

    Une liste vide ne distingue pas deux assertions opposées : « ce corpus ne
    contient aucune slash-command » (vrai, mesuré) et « le canal des tours
    utilisateur est vide / filtré, donc je n'ai rien mesuré » (aveu d'aveuglement).
    Sur le run 2026-10-03, B5.1 fait remonter `swarmx` → le premier cas est
    désormais démontrable au lieu d'être supposé.
    """
    if command_usage is None:
        command_usage = _build_command_usage(usages)
    if command_usage:
        return COMMAND_USAGE_COMPUTED
    return (
        COMMAND_USAGE_COMPUTED
        if any(_is_exploitable_turn(t) for u in usages for t in u.user_turns)
        else COMMAND_USAGE_NO_DATA
    )


def _is_exploitable_turn(turn: str) -> bool:
    """Un tour non vide qui aurait pu être une slash-command (narration exclue, B5.1)."""
    return bool(turn and turn.strip()) and not is_tool_narration(turn)


def _unmeasurable_warnings(session_classifications) -> list[WarningEntry]:
    """One aggregated review-unmeasurable warning per harness (not per session)."""
    _unmeasurable: dict[str, int] = defaultdict(int)
    for sc in session_classifications:
        warning = sc.production_review_warning
        if not warning:
            continue
        harness = warning[len("review-unmeasurable:") :]
        _unmeasurable[harness] += 1
    return [
        WarningEntry(message=f"review-unmeasurable:{harness} ({_unmeasurable[harness]} sessions)")
        for harness in sorted(_unmeasurable)
    ]


def _build_subagent_totals(
    children: list[SessionUsage], usages: list[SessionUsage], orphan_ids: set[str]
) -> SubagentTotals:
    """Subagent totals (children + orphans, spec §8)."""
    sub_children = children + [u for u in usages if u.session_id in orphan_ids]
    by_agent: dict[str, dict] = defaultdict(lambda: {"count": 0, "cost": 0.0})
    for c in sub_children:
        agent = c.agent_type or "unknown"
        by_agent[agent]["count"] += 1
        by_agent[agent]["cost"] = round6(by_agent[agent]["cost"] + c.cost_usd)
    return SubagentTotals(
        child_session_count=len(sub_children),
        total_cost_usd=round6(sum(c.cost_usd for c in sub_children)),
        by_agent_type=[
            AgentTypeUsage(a, d["count"], round6(d["cost"])) for a, d in sorted(by_agent.items())
        ],
    )


def _usage_agg() -> dict:
    """Fabrique defaultdict pour model_agg / harness_agg."""
    return defaultdict(
        lambda: {"sessions": set(), "tokens": 0, "cost": 0.0, "cache_read": 0.0, "fresh": 0.0}
    )


@dataclass(slots=True)
class _RootTotals:
    """Accumulateurs de la boucle roots (totals + lignes agrégées par root)."""

    totals: Totals = field(default_factory=Totals)
    root_costs: dict[str, float] = field(default_factory=dict)
    model_agg: dict = field(default_factory=_usage_agg)
    harness_agg: dict = field(default_factory=_usage_agg)
    day_cost: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    day_tokens: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    day_cache: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    day_fresh: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    top_sessions: list[TopSession] = field(default_factory=list)


def _process_root(
    root: SessionUsage,
    by_id: dict[str, SessionUsage],
    acc: _RootTotals,
    *,
    include_subagents: bool,
    children_ids: set[str],
) -> None:
    """Traite un root : fusionne descendants, MAJ accumulateurs + TopSession."""
    descendants = _descendants(by_id, root) if include_subagents else []
    merged = [s for u in [root, *descendants] for s in u.steps]
    cost = round6(sum(s.cost for s in merged if s.cost is not None))
    acc.root_costs[root.session_id] = cost
    tokens = sum(s.total_tokens for s in merged)
    acc.totals.session_count += 1
    acc.totals.total_tokens += tokens
    acc.totals.total_cost_usd = round6(acc.totals.total_cost_usd + cost)
    cache_read = round(sum(s.tokens_cache_read for s in merged))
    cache_write = round(sum(s.tokens_cache_write for s in merged))
    fresh = round(sum(s.tokens_input for s in merged))
    output = round(sum(s.tokens_output for s in merged))
    reasoning = round(sum(s.tokens_reasoning for s in merged))
    acc.totals.cache_read_tokens += cache_read
    acc.totals.cache_write_tokens += cache_write
    acc.totals.fresh_input_tokens += fresh
    acc.totals.output_tokens += output
    acc.totals.reasoning_tokens += reasoning
    for s in merged:
        agg = acc.model_agg[s.model]
        agg["sessions"].add(root.session_id)
        agg["tokens"] += s.total_tokens
        agg["cost"] = round6(agg["cost"] + (s.cost if s.cost is not None else 0.0))
        agg["cache_read"] += s.tokens_cache_read
        agg["fresh"] += s.tokens_input
        _bucket = s.timestamp.strftime("%Y-%m-%d")
        acc.day_cost[_bucket] = round6(
            acc.day_cost[_bucket] + (s.cost if s.cost is not None else 0.0)
        )
        acc.day_tokens[_bucket] += s.total_tokens
        acc.day_cache[_bucket] += s.tokens_cache_read
        acc.day_fresh[_bucket] += s.tokens_input
    has_children = include_subagents and any(c.session_id in children_ids for c in descendants)
    duration, active = session_duration(root)
    harness = root.harness or split_canonical_session_id(root.session_id)[0] or ""
    h_agg = acc.harness_agg[harness]
    h_agg["sessions"].add(root.session_id)
    h_agg["tokens"] += tokens
    h_agg["cost"] = round6(h_agg["cost"] + cost)
    h_agg["cache_read"] += cache_read
    h_agg["fresh"] += fresh
    context_chars: dict[str, int] = {"file": 0, "tool_result": 0, "text": 0, "reasoning": 0}
    for u in [root, *descendants]:
        for k in ("file", "tool_result", "text", "reasoning"):
            context_chars[k] += u.context_chars.get(k, 0)
    acc.top_sessions.append(
        TopSession(
            session_id=root.session_id,
            title_or_topic=root.title or root.first_user_text,
            cost_usd=cost,
            reported_cost_usd_lifetime=root.reported_cost_usd_lifetime,
            total_tokens=tokens,
            project_path=root.project_path,
            duration_seconds=duration,
            active_time_seconds=active,
            cost_per_active_minute=round6(cost / (active / 60.0)) if active > 0 else None,
            api_call_count=len(merged),
            includes_subagents=has_children,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            cache_efficiency=_cache_hit_rate(cache_read, fresh),
            harness=harness,
            context_composition={  # chars/4 estimation (spec §2, TokenScope pattern)
                "file_tokens": _estimated_tokens(context_chars["file"]),
                "tool_result_tokens": _estimated_tokens(context_chars["tool_result"]),
                "text_tokens": _estimated_tokens(context_chars["text"]),
                "reasoning_tokens": _estimated_tokens(context_chars["reasoning"]),
            },
        )
    )


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
