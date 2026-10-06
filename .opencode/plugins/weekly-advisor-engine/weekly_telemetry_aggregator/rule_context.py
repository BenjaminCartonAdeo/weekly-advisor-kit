"""Adapt the production artifacts to the ``(context, extra)`` pair of the rules engine.

:func:`build_context` is the only bridge between the real telemetry artifacts and
the declarative rules engine (:func:`weekly_telemetry_aggregator.rule_pipeline.evaluate_rules`).
The engine resolves a rule's ``scope`` by dotted path and **requires every scope to
be a list** — a mapping would be collapsed to ``[the_whole_mapping]`` by the engine.
Keeping that guarantee (notably for ``tool_argument_loops``, whose source is a
nested mapping the engine cannot flatten) is this module's whole job. No I/O, no
JSON, no network: the caller hands over already-loaded dicts.

Scopes — where the data comes from
---------------------------------
``user_prompt_repeats``
    ``current_summary["user_prompt_repeats"]`` as plain dicts, normalized on the
    :class:`~weekly_telemetry_aggregator.models.UserPromptRepeat` field set so a
    partial entry can never make a rule's ``match`` blow up on a missing field.
``session_classifications``
    ``current_summary["session_classifications"]`` as plain dicts, normalized on the
    :class:`~weekly_telemetry_aggregator.models.SessionClassification` field set
    (``production_review_pct`` stays ``None`` when unmeasured — never a fake ``0``).
``findings``
    The already-computed ``weekly-quality-findings-<date>.json`` payload passed as
    ``quality_findings`` (either the whole document, read from its ``findings`` key,
    or the list itself). ``None``/absent/illisible yields an empty list — never an
    exception. This adapter never computes findings; it only exposes them.
``tool_argument_loops``
    ``current_summary["tool_argument_fingerprints"]`` is the nested mapping
    ``{tool: {fingerprint: {"total", "peak"}}}``. Flattened here into one entry per
    tool carrying the two numbers of the worst fingerprint: ``peak`` = the most
    identical calls a **single session** made (an agent-loop is "same input" in
    one session, see ``insights._agent_loop_findings``), ``total`` = the same
    fingerprint's calls over the whole window. Legacy summaries whose buckets are
    plain ints expose ``peak = total`` (see ``_bucket_pair``) — the observed
    behaviour is preserved instead of being muted by a fabricated ``0``.
    ``task_threshold`` carries the reference threshold — ``task`` gets
    :data:`~weekly_telemetry_aggregator.insights.AGENT_LOOP_TASK_MIN_REPEATS_DEFAULT`,
    every other tool
    :data:`~weekly_telemetry_aggregator.insights.AGENT_LOOP_MIN_REPEATS_DEFAULT` —
    as *data*: the rule owns the comparison and its own threshold.
``daily_spikes``
    One entry per ``current_summary["daily_totals"]`` day with ``cost_usd > 0``,
    scored against the baseline of ``recent_summaries`` (the current run is excluded
    by the caller) through the shared core ``insights._robust_z_scores`` → median +
    MAD in :func:`weekly_telemetry_aggregator.util.robust_z`. Only ``raw_z`` is
    exposed: the display cap (``DAILY_SPIKE_Z_CAP``) is applied downstream, so a
    rule can see the strength of the signal it is about to cap.
``architecture_drift``
    Always a single entry, so the scope is never empty and a rule can compare
    ``drift_runs`` to its own threshold. The observation is read with
    ``insights._architecture_observation`` (top level or ``watch_context`` mirror),
    compared with ``insights._architecture_drift`` (4 stable fields), and the
    consecutive-run streak is counted like ``insights._architecture_drift_finding``:
    1 when current differs from previous, +1 per earlier run still differing, break
    on the first one that does not.

``extra`` — options for the engine and the templates
---------------------------------------------------
Rules read them as ``{{extra.<key>}}`` (see ``rule_pipeline.render_template``).
This adapter publishes only the knobs it decided: ``loop_min_repeats``,
``loop_task_min_repeats``, ``architecture_drift_runs``, ``baseline_days`` (how many
non-zero daily costs the spike baseline holds) and ``has_previous_summary``.
``extra["ignored"]`` is **not** set here: the ignore filter belongs to the caller
(which merges it before calling ``evaluate_rules``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import MISSING, fields
from typing import Any

from .insights import (
    AGENT_LOOP_MIN_REPEATS_DEFAULT,
    AGENT_LOOP_TASK_MIN_REPEATS_DEFAULT,
    ARCHITECTURE_DRIFT_RUNS_DEFAULT,
    _architecture_drift,
    _architecture_observation,
    _robust_z_scores,
)
from .models import SessionClassification, UserPromptRepeat

__all__ = [
    "AGENT_LOOP_MIN_REPEATS_DEFAULT",
    "AGENT_LOOP_TASK_MIN_REPEATS_DEFAULT",
    "ARCHITECTURE_DRIFT_RUNS_DEFAULT",
    "build_context",
]


# --------------------------------------------------------------------------- #
# Scope builders
# --------------------------------------------------------------------------- #
def _field_defaults(cls: type[Any]) -> dict[str, Any]:
    """Declared defaults of a summary dataclass, used to normalize its dict form."""
    defaults: dict[str, Any] = {}
    for spec in fields(cls):  # type: ignore[arg-type]
        if spec.default is not MISSING:
            defaults[spec.name] = spec.default
        elif spec.default_factory is not MISSING:  # type: ignore[misc]
            defaults[spec.name] = spec.default_factory()  # type: ignore[misc]
    return defaults


#: canonical key sets (mirrors of the summary models, kept derived to avoid drift)
_PROMPT_REPEAT_DEFAULTS = _field_defaults(UserPromptRepeat)
_CLASSIFICATION_DEFAULTS = _field_defaults(SessionClassification)
#: audit + rule findings share one shape (see ``insights._agent_loop_findings``)
_FINDING_DEFAULTS: dict[str, Any] = {
    "session_id": None,
    "category": "",
    "severity": "",
    "description": "",
    "evidence_summary": "",
    "recommendation": "",
    "recommendation_type": "",
}


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _normalized_dicts(value: Any, defaults: Mapping[str, Any]) -> list[dict[str, Any]]:
    """List of dicts with every canonical key present; non-dict entries are dropped."""
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    normalized: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        entry = dict(defaults)
        entry.update(item)
        normalized.append(entry)
    return normalized


def _prompt_repeats(summary: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Scope ``user_prompt_repeats`` — repeated user prompts of the current summary."""
    return _normalized_dicts(summary.get("user_prompt_repeats"), _PROMPT_REPEAT_DEFAULTS)


def _session_classifications(summary: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Scope ``session_classifications`` — one record per active session."""
    return _normalized_dicts(summary.get("session_classifications"), _CLASSIFICATION_DEFAULTS)


def _findings(quality_findings: Mapping[str, Any] | Sequence[Any] | None) -> list[dict[str, Any]]:
    """Scope ``findings`` — quality findings already computed for this run.

    Accepts the ``weekly-quality-findings-<date>.json`` document (read from its
    ``findings`` key) or the list itself. Anything missing, malformed or of an
    unknown shape degrades to an empty list: a run without an audit must still
    evaluate its rules.
    """
    if isinstance(quality_findings, Mapping):
        payload: Any = quality_findings.get("findings")
    elif isinstance(quality_findings, Sequence) and not isinstance(quality_findings, (str, bytes)):
        payload = quality_findings
    else:
        payload = None
    return _normalized_dicts(payload, _FINDING_DEFAULTS)


def _as_int(value: Any) -> int:
    """Coerce to int, ``0`` for anything that is not a real number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)


def _bucket_pair(bucket: Any) -> tuple[int, int]:
    """``(total, peak)`` of one fingerprint bucket, both numbers of the same call-set.

    Current shape is ``{"total": int, "peak": int}``. A bucket that is a plain
    ``int`` is a **legacy summary** (every artifact written before the peak
    existed, e.g. ``weekly-summary-2026-09-16.json``): the intra-session
    breakdown was never collected, so ``peak`` falls back to ``total``. That is
    deliberate — it keeps the observed behaviour of those artifacts byte-for-byte
    (every tool that triggered before still triggers) instead of silently muting
    them with an invented ``peak = 0``. A missing or unreadable half of the pair
    falls back to the other one; both unreadable is ``(0, 0)``.
    """
    if isinstance(bucket, Mapping):
        total, peak = _as_int(bucket.get("total")), _as_int(bucket.get("peak"))
        if not total and not peak:
            return (0, 0)
        return (total or peak, peak or total)
    value = _as_int(bucket)
    return (value, value)


def _tool_argument_loops(
    summary: Mapping[str, Any],
    *,
    loop_min_repeats: int,
    loop_task_min_repeats: int,
) -> list[dict[str, Any]]:
    """Scope ``tool_argument_loops`` — nested fingerprints flattened per tool.

    ``{tool: {fingerprint: {"total", "peak"}}}`` becomes one entry per tool. The
    entry describes the **single worst fingerprint** — the one with the biggest
    intra-session ``peak``, ties broken on ``total`` then on the fingerprint name
    so the scope is deterministic — and reports its two numbers: ``peak`` (how
    many identical calls the busiest session made) and ``total`` (how many over
    the whole window). Pairing them matters: ``total = 13 / peak = 1`` is a cron
    tool called once per run, ``total = 13 / peak = 13`` is an agent loop, and
    taking each maximum independently would publish combinations no fingerprint
    ever had. Malformed buckets count as 0 rather than blowing up.
    """
    fingerprints = summary.get("tool_argument_fingerprints")
    if not isinstance(fingerprints, Mapping):
        return []
    entries: list[dict[str, Any]] = []
    for tool in sorted(fingerprints, key=str):
        buckets = fingerprints.get(tool)
        worst = None
        if isinstance(buckets, Mapping):
            for fingerprint, bucket in buckets.items():
                total, peak = _bucket_pair(bucket)
                rank = (-peak, -total, str(fingerprint))
                if worst is None or rank < worst[0]:
                    worst = (rank, total, peak)
        entries.append(
            {
                "tool": str(tool),
                "total": worst[1] if worst else 0,
                "peak": worst[2] if worst else 0,
                "task_threshold": loop_task_min_repeats if tool == "task" else loop_min_repeats,
            }
        )
    return entries


def _baseline_costs(recent_summaries: Sequence[Mapping[str, Any]]) -> list[float]:
    """Non-zero daily costs of the previous runs (current run excluded by the caller).

    Same rule as ``insights._spike_baseline``, minus the ``[1:]`` slice: here the
    caller already removed the current run, so dropping one more summary would
    silently shrink the baseline.
    """
    costs: list[float] = []
    for summary in recent_summaries:
        for day in summary.get("daily_totals") or []:
            if not isinstance(day, Mapping):
                continue
            cost = _as_float(day.get("cost_usd"))
            if cost != 0:
                costs.append(cost)
    return costs


def _daily_spikes(summary: Mapping[str, Any], baseline: Sequence[float]) -> list[dict[str, Any]]:
    """Scope ``daily_spikes`` — one entry per positive-cost day, scored vs the baseline.

    The z-score is robust (median + MAD) and computed by the shared core, with the
    candidate day appended to the baseline exactly like ``insights`` does. ``raw_z``
    is left uncapped on purpose. An empty baseline yields ``0.0`` (nothing to
    compare against, never an invented spike); ``extra["baseline_days"]`` lets a
    rule require a real baseline.
    """
    days = summary.get("daily_totals")
    if not isinstance(days, Sequence) or isinstance(days, (str, bytes)):
        return []
    spikes: list[dict[str, Any]] = []
    for day in days:
        if not isinstance(day, Mapping):
            continue
        cost = _as_float(day.get("cost_usd"))
        if cost <= 0:
            continue
        combined = [*baseline, cost]
        scores = {
            round(value, 4): z
            for value, z in zip(combined, _robust_z_scores(list(combined)), strict=False)
        }
        spikes.append(
            {
                "day": day.get("date"),
                "cost_usd": cost,
                "raw_z": scores.get(round(cost, 4), 0.0),
            }
        )
    return spikes


def _architecture_drift_entry(
    current: Mapping[str, Any],
    previous: Mapping[str, Any] | None,
    recent_summaries: Sequence[Mapping[str, Any]],
    drift_runs_threshold: int,
) -> dict[str, Any]:
    """Single entry of the ``architecture_drift`` scope (never an empty list).

    Reuses ``insights._architecture_observation`` / ``insights._architecture_drift``
    and reproduces the streak of ``insights._architecture_drift_finding``: the
    current run counts once when it differs from the previous one, then every older
    run that still differs adds one, and the first match breaks the streak.
    """
    observation = _architecture_observation(dict(current)) or {}
    changed_fields = _architecture_drift(observation, _architecture_observation(previous))
    drift_runs = 1 if changed_fields else 0
    for summary in recent_summaries:
        if not changed_fields or not _architecture_drift(
            observation, _architecture_observation(summary)
        ):
            break
        drift_runs += 1
    return {
        "changed_fields": list(changed_fields),
        "drift_runs": drift_runs,
        "drift_threshold": max(1, int(drift_runs_threshold)),
        "observation": observation,
    }


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def build_context(
    current_summary: Mapping[str, Any] | None,
    *,
    previous_summary: Mapping[str, Any] | None = None,
    recent_summaries: Sequence[Mapping[str, Any]] = (),
    quality_findings: Mapping[str, Any] | Sequence[Any] | None = None,
    loop_min_repeats: int = AGENT_LOOP_MIN_REPEATS_DEFAULT,
    loop_task_min_repeats: int = AGENT_LOOP_TASK_MIN_REPEATS_DEFAULT,
    architecture_drift_runs: int = ARCHITECTURE_DRIFT_RUNS_DEFAULT,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build the ``(context, extra)`` pair consumed by ``evaluate_rules``.

    Args:
        current_summary: the run's ``weekly-summary-<date>.json`` document (or ``None``).
        previous_summary: the immediately previous summary, for the drift comparison.
        recent_summaries: older summaries used as the daily-spike baseline. The
            current run must already be excluded by the caller.
        quality_findings: the ``weekly-quality-findings-<date>.json`` document (or its
            ``findings`` list), or ``None`` when no audit ran.
        loop_min_repeats: reference threshold exposed per tool by
            ``tool_argument_loops`` (not a rule threshold).
        loop_task_min_repeats: same, for the ``task`` tool.
        architecture_drift_runs: reference threshold exposed as ``drift_threshold``.

    Returns:
        ``context`` — the six rule scopes, each a list (see the module docstring for
        where every scope comes from). ``extra`` — the options and thresholds the
        rules may read as ``{{extra.<key>}}``; the caller may add its own keys
        (``ignored``) to the same dict before evaluating.
    """
    current = dict(current_summary) if isinstance(current_summary, Mapping) else {}
    previous = dict(previous_summary) if isinstance(previous_summary, Mapping) else None
    recent = [dict(summary) for summary in recent_summaries if isinstance(summary, Mapping)]
    baseline = _baseline_costs(recent)

    context: dict[str, Any] = {
        "user_prompt_repeats": _prompt_repeats(current),
        "session_classifications": _session_classifications(current),
        "findings": _findings(quality_findings),
        "tool_argument_loops": _tool_argument_loops(
            current,
            loop_min_repeats=int(loop_min_repeats),
            loop_task_min_repeats=int(loop_task_min_repeats),
        ),
        "daily_spikes": _daily_spikes(current, baseline),
        "architecture_drift": [
            _architecture_drift_entry(current, previous, recent, architecture_drift_runs)
        ],
    }
    extra: dict[str, Any] = {
        "loop_min_repeats": int(loop_min_repeats),
        "loop_task_min_repeats": int(loop_task_min_repeats),
        "architecture_drift_runs": max(1, int(architecture_drift_runs)),
        "baseline_days": len(baseline),
        "has_previous_summary": previous is not None,
    }
    return context, extra
