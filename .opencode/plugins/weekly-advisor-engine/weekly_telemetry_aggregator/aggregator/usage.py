"""Builders d'usage et boucle de racine : tools, skills, commandes, sous-agents, totaux.

Région extraite de l'ancien `aggregator.py` monolithique (lignes 185-471 du commit
`02c1281`). Deux responsabilités, volontairement reunited ici :

1. les *comptages transverses* — tools (avec `total` et `peak` d'arguments), skills
   chargés / jamais chargés, slash-commands, warnings de relecture non mesurable ;
2. la *boucle de racine* — `_RootTotals` + `_process_root`, qui fusionne descendants et
   alimente totals, agrégats par modèle / harnais, totaux journaliers et `TopSession`.

`_process_root` est le seul endroit qui écrit dans `_RootTotals` : c'est la frontière
« une racine → une ligne de résumé ». `aggregate` l'appelle et ne fait que lire.

Ne dépend que de `models`, `util` (BFS des descendants) et `aggregator.text`.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta

from ..models import (
    AgentTypeUsage,
    CommandUsage,
    SessionUsage,
    SkillUsage,
    SubagentTotals,
    ToolUsage,
    TopSession,
    Totals,
    WarningEntry,
    round6,
    split_canonical_session_id,
)
from ..util import descendants_by_parent
from .text import _command_name, is_tool_narration

#: Gaps strictly below this threshold count as "active" time (5 minutes).
ACTIVE_GAP_LIMIT = timedelta(minutes=5)
#: Estimated tokens per tool call = len(args) / 4 (documented approximation).
TOKENS_PER_CHAR = 4

#: États de `command_usage_state` (B5.2) — le gabarit doit pouvoir distinguer
#: « aucune commande slash dans le corpus » de « commandes slash NON MESURÉES ».
COMMAND_USAGE_COMPUTED = "computed"
COMMAND_USAGE_NO_DATA = "no-data"

__all__ = [
    "ACTIVE_GAP_LIMIT",
    "COMMAND_USAGE_COMPUTED",
    "COMMAND_USAGE_NO_DATA",
    "TOKENS_PER_CHAR",
    "command_usage_state",
    "session_duration",
]


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
