"""Domain model (Part 1 §4) — pure dataclasses, no I/O. Serialization in `writer.py`."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

#: Maximum number of warnings persisted per run (spec §4).
MAX_WARNINGS = 50
#: Soft lower bound below which outlier z-scores are statistically meaningless (v5.23).
OUTLIER_MIN_SESSIONS = 15


def round6(value: float) -> float:
    """Round monetary amounts to 6 decimals (normative serialization rule)."""
    return round(float(value), 6)


def canonical_session_id(harness: str, session_id: str) -> str:
    """Identifiant canonique multi-harnais : ``"<harness>:<session_id>"``."""
    return f"{harness}:{session_id}"


def split_canonical_session_id(canonical: str) -> tuple[str | None, str]:
    """Décompose ``"<harness>:<session_id>"`` → ``(harness, session_id)``.

    Id sans préfixe (ou préfixe vide) → ``(None, canonical)`` — tolérant aux
    identifiants bruts d'un harnais unique.
    """
    harness, sep, raw = canonical.partition(":")
    if not sep or not harness:
        return None, canonical
    return harness, raw


@dataclass(slots=True)
class StepFinish:
    """One assistant step inside the window (per step-finish part + assistant message).

    Aggregation is limited to the window — never to the session lifetime.
    """

    session_id: str
    timestamp: datetime
    model: str  # normalized "provider/model" key
    tokens_input: float = 0.0
    tokens_output: float = 0.0
    tokens_reasoning: float = 0.0
    tokens_cache_read: float = 0.0
    tokens_cache_write: float = 0.0
    #: OpenCode-recorded cost (None = not persisted → `missing-pricing:<model>`).
    cost: float | None = None
    #: harnais d'origine (multi-harnais, vNext) — "" pour le flux historique.
    harness: str = ""

    @property
    def total_tokens(self) -> int:
        return round(
            self.tokens_input
            + self.tokens_output
            + self.tokens_reasoning
            + self.tokens_cache_read
            + self.tokens_cache_write
        )


@dataclass(slots=True)
class SessionUsage:
    """Aggregated usage of one session, limited to the window."""

    session_id: str
    title: str | None = None
    project_path: str | None = None
    agent_type: str | None = None
    parent_id: str | None = None
    steps: list[StepFinish] = field(default_factory=list)
    tool_calls: dict[str, int] = field(default_factory=dict)
    tool_arg_chars: dict[str, int] = field(default_factory=dict)
    skills_loaded: dict[str, int] = field(default_factory=dict)
    user_turns: list[str] = field(default_factory=list)
    #: Part char counts per category (chars/4 → tokens): file, tool_result, text, reasoning.
    context_chars: dict[str, int] = field(default_factory=dict)
    first_user_text: str | None = None
    #: session_v2 lifetime cost (cross-check reference, v5.28).
    reported_cost_usd_lifetime: float | None = None
    #: harnais d'origine (multi-harnais, vNext) — "" pour le flux historique.
    harness: str = ""

    @property
    def cost_usd(self) -> float:
        return round6(sum(s.cost for s in self.steps if s.cost is not None))


@dataclass(slots=True)
class Totals:
    session_count: int = 0
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    fresh_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    #: cache_read / (cache_read + fresh_input); None when no input (div-by-zero → null).
    cache_hit_rate: float | None = 0.0


@dataclass(slots=True)
class DailyTotal:
    date: str
    cost_usd: float = 0.0
    total_tokens: int = 0
    cache_hit_rate: float | None = 0.0


@dataclass(slots=True)
class ModelUsage:
    model: str
    session_count: int = 0
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    cache_hit_rate: float | None = None


@dataclass(slots=True)
class TopSession:
    session_id: str
    title_or_topic: str | None = None
    cost_usd: float = 0.0
    #: session_v2 lifetime cost as recorded by OpenCode (cross-check reference).
    reported_cost_usd_lifetime: float | None = None
    total_tokens: int = 0
    project_path: str | None = None
    duration_seconds: int = 0
    active_time_seconds: int = 0
    cost_per_active_minute: float | None = None
    api_call_count: int = 0
    includes_subagents: bool = False
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cache_efficiency: float | None = None
    context_composition: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class CostOutlier:
    session_id: str
    cost_usd: float = 0.0
    z_score: float = 0.0


@dataclass(slots=True)
class ToolUsage:
    tool: str
    call_count: int = 0
    estimated_tokens: int = 0


@dataclass(slots=True)
class SkillUsage:
    skill: str
    load_count: int = 0
    sessions_used_in: int = 0


@dataclass(slots=True)
class CommandUsage:
    command: str
    call_count: int = 0
    sessions_used_in: int = 0


@dataclass(slots=True)
class SkillSimilarPair:
    skills: list[str]
    similarity: float = 0.0


@dataclass(slots=True)
class UserPromptRepeat:
    normalized_preview: str
    count: int = 0
    session_id: str = ""
    avg_chars: int = 0


@dataclass(slots=True)
class AgentTypeUsage:
    agent_type: str
    session_count: int = 0
    cost_usd: float = 0.0


@dataclass(slots=True)
class SubagentTotals:
    child_session_count: int = 0
    total_cost_usd: float = 0.0
    by_agent_type: list[AgentTypeUsage] = field(default_factory=list)


@dataclass(slots=True)
class WarningEntry:
    session_id: str | None = None
    message: str = ""
    #: Reliability-degrading (telemetry gap) → the run reports EXIT_PARTIAL.
    #: Operational/informational warnings (active excluded, cross-check, sample)
    #: keep the run at EXIT_OK — they are findings of the report, not failures.
    partial: bool = False
    #: structured cross-check figures for cost_discrepancies (v5.28 K7).
    parts_cost: float | None = None
    session_v2_cost: float | None = None


@dataclass(slots=True)
class Period:
    start: datetime
    end: datetime


@dataclass(slots=True)
class SkillCatalogEntry:
    """One SKILL.md of the universe (name + description/body for similarity pairs, v5.25)."""

    name: str
    description: str = ""
    body: str = ""
    #: agents ciblés déclarés dans metadata.target_agents (v5.30, drafting) —
    #: traçabilité du public d'un skill auto-rédigé (R1 nuancé, rapport).
    target_agents: list[str] = field(default_factory=list)


@dataclass(slots=True)
class WeeklySummary:
    period: Period
    generated_at: datetime
    totals: Totals = field(default_factory=Totals)
    daily_totals: list[DailyTotal] = field(default_factory=list)
    by_model: list[ModelUsage] = field(default_factory=list)
    top_sessions_by_cost: list[TopSession] = field(default_factory=list)
    cost_outliers: list[CostOutlier] = field(default_factory=list)
    #: "computed" | "skipped:small-sample" | "no-data" (v5.28, machine-readable).
    cost_outliers_state: str = "computed"
    tool_usage: list[ToolUsage] = field(default_factory=list)
    skill_usage: list[SkillUsage] = field(default_factory=list)
    command_usage: list[CommandUsage] = field(default_factory=list)
    skill_similar_pairs: list[SkillSimilarPair] = field(default_factory=list)
    skill_catalog_source: str = "filesystem"
    skill_catalog_count: int = 0
    skills_never_loaded: list[str] = field(default_factory=list)
    #: skills auto-rédigés → agents ciblés (metadata.target_agents, v5.30).
    skills_targets: dict[str, list[str]] = field(default_factory=dict)
    user_prompt_repeats: list[UserPromptRepeat] = field(default_factory=list)
    subagent_totals: SubagentTotals = field(default_factory=SubagentTotals)
    #: selection audit — why each window-touched session was/wasn't counted (v5.28).
    selection: dict = field(default_factory=dict)
    #: coûts estimés ($, round6) des sessions sans AUCUN coût enregistré,
    #: par id canonique — None si rien à estimer (clé absente à la sérialisation).
    cost_estimates: dict[str, float] | None = None
    warnings: list[WarningEntry] = field(default_factory=list)
