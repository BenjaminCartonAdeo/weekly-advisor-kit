"""rule_context adapter tests — scopes shaped for the declarative rules engine.

Every fixture is hard-coded here on purpose: the adapter must stay testable
independently of the state of ``rules/*.md`` (rewritten in parallel).
"""

from __future__ import annotations

from typing import Any

import pytest

from weekly_telemetry_aggregator import rule_context
from weekly_telemetry_aggregator.rule_context import (
    AGENT_LOOP_MIN_REPEATS_DEFAULT,
    AGENT_LOOP_TASK_MIN_REPEATS_DEFAULT,
    ARCHITECTURE_DRIFT_RUNS_DEFAULT,
    build_context,
)
from weekly_telemetry_aggregator.rule_loader import Rule
from weekly_telemetry_aggregator.rule_pipeline import evaluate_rule
from weekly_telemetry_aggregator.util import robust_z

SCOPES = (
    "user_prompt_repeats",
    "session_classifications",
    "findings",
    "tool_argument_loops",
    "daily_spikes",
    "architecture_drift",
)


def _architecture(state: int, *, config: str = "cfg-a", harness: int = 4) -> dict[str, Any]:
    return {
        "state_counts": {"skills": state},
        "config": {"adapter": config},
        "inventory_counts": {"rules": state},
        "harness_scope": {"roots": harness},
    }


def _summary(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "user_prompt_repeats": [
            {
                "normalized_preview": "refais le test",
                "count": 9,
                "session_id": "s1",
            }
        ],
        "session_classifications": [
            {
                "session_id": "s1",
                "intent": "debug",
                "production_review_measured": 4,
                "production_review_pct": 0.0,
                "prompt_maturity_score": 20,
                "prompt_maturity_grade": "F",
            }
        ],
        "daily_totals": [
            {"date": "2026-10-01", "cost_usd": 1.0},
            {"date": "2026-10-02", "cost_usd": 1.2},
            {"date": "2026-10-03", "cost_usd": 5.0},
            {"date": "2026-10-04", "cost_usd": 0.0},
        ],
        "tool_argument_fingerprints": {
            "read": {"fp-a": 2, "fp-b": 9},
            "task": {"fp-c": 4},
        },
        "architecture_observations": _architecture(2),
    }
    base.update(overrides)
    return base


#: baseline run previous (2 daily costs) + one older run (1 daily cost)
RECENT: list[dict[str, Any]] = [
    {"daily_totals": [{"date": "2026-09-28", "cost_usd": 1.0}]},
    {
        "daily_totals": [
            {"date": "2026-09-29", "cost_usd": 1.2},
            {"date": "2026-09-30", "cost_usd": 0.8},
        ],
        "architecture_observations": _architecture(1),
    },
]
PREVIOUS: dict[str, Any] = {
    "daily_totals": [{"date": "2026-09-27", "cost_usd": 0.9}],
    "architecture_observations": _architecture(1),
}


def _build(
    summary: dict[str, Any] | None = None, **kwargs: Any
) -> tuple[dict[str, Any], dict[str, Any]]:
    """``build_context`` over the fixture summary + baseline runs (test ergonomics).

    ``summary`` overrides fields of the current summary; every other keyword goes
    to ``build_context`` itself (previous run, findings payload, thresholds).
    """
    return build_context(
        _summary() if summary is None else summary,
        recent_summaries=RECENT,
        **kwargs,
    )


def _context(summary: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    context, _extra = _build(summary, **kwargs)
    return context


# --------------------------------------------------------------------------- #
# Shape contract: six scopes, every one of them a list
# --------------------------------------------------------------------------- #
def test_build_context_exposes_exactly_the_six_scopes_as_lists():
    context, _extra = build_context(_summary(), recent_summaries=RECENT)
    assert set(context) == set(SCOPES)
    for scope in SCOPES:
        assert isinstance(context[scope], list), scope
        assert all(isinstance(entry, dict) for entry in context[scope]), scope


def test_build_context_accepts_an_empty_current_summary():
    context, extra = build_context(None)
    assert set(context) == set(SCOPES)
    for scope in SCOPES:
        assert isinstance(context[scope], list), scope
    assert context["user_prompt_repeats"] == []
    assert context["session_classifications"] == []
    assert context["findings"] == []
    assert context["tool_argument_loops"] == []
    assert context["daily_spikes"] == []
    # architecture_drift stays a single (clean) entry: never an empty scope
    assert len(context["architecture_drift"]) == 1
    assert extra["baseline_days"] == 0
    assert extra["has_previous_summary"] is False


@pytest.mark.parametrize("scope", SCOPES)
def test_engine_resolves_every_scope_to_the_built_list(scope: str):
    """The engine must see exactly the list we built, not a collapsed mapping."""
    context, _extra = build_context(_summary(), previous_summary=PREVIOUS, recent_summaries=RECENT)
    rule = Rule(
        id=f"probe-{scope}",
        name="probe",
        group="quality",
        severity="low",
        scope=scope,
        detect={"scan": scope, "check": {"triggered": "total >= 0"}},
    )
    finding = evaluate_rule(rule, context)
    assert finding is not None, scope
    assert finding["details"]["scope"] == scope
    assert finding["details"]["scanned"] == len(context[scope])
    assert finding["occurrences"] == len(context[scope])


# --------------------------------------------------------------------------- #
# user_prompt_repeats
# --------------------------------------------------------------------------- #
def test_user_prompt_repeats_are_normalized_with_model_defaults():
    context = _context()
    entry = context["user_prompt_repeats"][0]
    assert entry["normalized_preview"] == "refais le test"
    assert entry["count"] == 9
    # absent fields are filled from the models.UserPromptRepeat defaults
    assert entry["sessions_distinct"] == 0
    assert entry["examples"] == []
    assert entry["estimated_time_saved_mins"] == 0


def test_user_prompt_repeats_drops_non_dict_entries():
    context = _context(_summary(user_prompt_repeats=[{"count": 2}, "oops", None, 42]))
    assert [entry["count"] for entry in context["user_prompt_repeats"]] == [2]


def test_user_prompt_repeats_ignores_a_non_list_block():
    assert _context(_summary(user_prompt_repeats={"count": 3}))["user_prompt_repeats"] == []
    assert _context(_summary(user_prompt_repeats="refais"))["user_prompt_repeats"] == []


# --------------------------------------------------------------------------- #
# session_classifications
# --------------------------------------------------------------------------- #
def test_session_classifications_carry_the_documented_fields():
    entry = _context()["session_classifications"][0]
    for field in (
        "session_id",
        "intent",
        "spec_driven",
        "spec_preuves",
        "cost_usd",
        "production_review_pct",
        "production_review_measured",
        "production_review_warning",
        "prompt_maturity_score",
        "prompt_maturity_grade",
        "prompt_maturity_dimensions",
    ):
        assert field in entry, field
    assert entry["production_review_pct"] == 0.0
    assert entry["spec_driven"] is False
    assert entry["spec_preuves"] == []
    assert entry["prompt_maturity_dimensions"] == {}


def test_session_classifications_default_pct_stays_none_not_zero():
    context = _context(_summary(session_classifications=[{"session_id": "s9"}]))
    assert context["session_classifications"][0]["production_review_pct"] is None
    assert context["session_classifications"][0]["prompt_maturity_grade"] == "F"


# --------------------------------------------------------------------------- #
# findings
# --------------------------------------------------------------------------- #
def test_findings_are_read_from_the_quality_findings_payload():
    payload = {
        "findings": [
            {
                "session_id": "s1",
                "category": "agent-loop",
                "severity": "medium",
                "description": "read répété",
                "evidence_summary": "tool=read; repeats=9",
            }
        ]
    }
    findings = _context(quality_findings=payload)["findings"]
    assert len(findings) == 1
    assert findings[0]["category"] == "agent-loop"
    assert findings[0]["recommendation"] == ""  # default filled


def test_findings_absent_yield_an_empty_list_without_raising():
    assert _context()["findings"] == []
    assert _context(quality_findings=None)["findings"] == []
    assert _context(quality_findings={})["findings"] == []
    assert _context(quality_findings={"findings": "broken"})["findings"] == []
    assert _context(quality_findings={"other_key": [{"a": 1}]})["findings"] == []


def test_findings_drop_non_dict_entries():
    context = _context(quality_findings={"findings": [{"category": "c1"}, "oops", None]})
    assert [entry["category"] for entry in context["findings"]] == ["c1"]


# --------------------------------------------------------------------------- #
# tool_argument_loops (flattened)
# --------------------------------------------------------------------------- #
def test_tool_argument_loops_are_flattened_one_entry_per_tool_with_max_bucket():
    loops = _context()["tool_argument_loops"]
    assert loops == [
        {"tool": "read", "total": 9, "peak": 9, "task_threshold": AGENT_LOOP_MIN_REPEATS_DEFAULT},
        {
            "tool": "task",
            "total": 4,
            "peak": 4,
            "task_threshold": AGENT_LOOP_TASK_MIN_REPEATS_DEFAULT,
        },
    ]
    assert [loop["tool"] for loop in loops] == sorted(loop["tool"] for loop in loops)


def test_tool_argument_loops_expose_the_window_total_and_the_intra_session_peak():
    """`peak` = plus gros pic d'appels identiques dans UNE session, `total` = fenêtre."""
    loops = {
        loop["tool"]: loop
        for loop in _context(
            _summary(
                tool_argument_fingerprints={
                    # 13 appels identiques, un par run de cron : total 13, pic 1
                    "weekly_run": {"fp": {"total": 13, "peak": 1}},
                    # 9 appels identiques dans une seule session : total 9, pic 9
                    "read": {"fp": {"total": 9, "peak": 9}},
                }
            )
        )["tool_argument_loops"]
    }
    assert (loops["weekly_run"]["total"], loops["weekly_run"]["peak"]) == (13, 1)
    assert (loops["read"]["total"], loops["read"]["peak"]) == (9, 9)


def test_tool_argument_loops_pair_total_and_peak_from_the_same_fingerprint():
    """Les deux nombres décrivent la MÊME empreinte — jamais deux maxima distincts.

    `write` a une empreinte très répétée dans la fenêtre mais jamais dans une
    seule session ; `grep` l'inverse. Publier `total=20 peak=9` fabriquerait une
    paire qu'aucune empreinte n'a jamais eue.
    """
    loops = {
        loop["tool"]: loop
        for loop in _context(
            _summary(
                tool_argument_fingerprints={
                    "write": {
                        "spilled": {"total": 20, "peak": 3},
                        "small": {"total": 4, "peak": 4},
                    },
                    "grep": {
                        "window-wide": {"total": 9, "peak": 2},
                        "one-session": {"total": 9, "peak": 9},
                    },
                }
            )
        )["tool_argument_loops"]
    }
    assert (loops["write"]["total"], loops["write"]["peak"]) == (4, 4)
    assert (loops["grep"]["total"], loops["grep"]["peak"]) == (9, 9)


def test_tool_argument_loops_tie_on_peak_is_broken_deterministically():
    """Pic égal, total différent → le plus gros total gagne, toujours le même."""
    summary = _summary(
        tool_argument_fingerprints={
            "grep": {
                "b-later-name": {"total": 7, "peak": 5},
                "a-earlier-name": {"total": 3, "peak": 5},
                "c-bigger-total": {"total": 9, "peak": 5},
            }
        }
    )
    first = _context(summary)["tool_argument_loops"]
    second = _context(summary)["tool_argument_loops"]
    assert first == second
    assert (first[0]["total"], first[0]["peak"]) == (9, 5)


def test_tool_argument_loops_degrade_legacy_int_buckets_to_peak_equals_total():
    """Ascendant : un bucket `int` (artifacts déjà écrits) se lit comme avant.

    `weekly-summary-2026-09-16.json` et suivants portent des buckets entiers : la
    décomposition intra-session n'y existe pas. La rattacher au total — et non à
    0 — garde le comportement observé de ces résumés inchangé : ce qui
    déclenchait avant déclenche encore.
    """
    loops = _context(
        _summary(
            tool_argument_fingerprints={
                "weekly_run": {"fp": 13},
                "read": {"fp-a": 9, "fp-b": 2},
                "task": {"fp": 4},
            }
        )
    )["tool_argument_loops"]
    assert loops == [
        {"tool": "read", "total": 9, "peak": 9, "task_threshold": AGENT_LOOP_MIN_REPEATS_DEFAULT},
        {
            "tool": "task",
            "total": 4,
            "peak": 4,
            "task_threshold": AGENT_LOOP_TASK_MIN_REPEATS_DEFAULT,
        },
        {
            "tool": "weekly_run",
            "total": 13,
            "peak": 13,
            "task_threshold": AGENT_LOOP_MIN_REPEATS_DEFAULT,
        },
    ]


def test_tool_argument_loops_degrade_partial_and_unreadable_buckets():
    """Moitié manquante → l'autre moitié ; illisible → `(0, 0)`. Jamais d'exception."""
    loops = {
        loop["tool"]: loop
        for loop in _context(
            _summary(
                tool_argument_fingerprints={
                    "half-total": {"fp": {"total": 5}},
                    "half-peak": {"fp": {"peak": 6}},
                    "empty-pair": {"fp": {}},
                    "str-bucket": {"fp": "many"},
                    "bool-bucket": {"fp": True},
                    "null-bucket": {"fp": None},
                    "nested-junk": {"fp": {"total": "7", "peak": [1]}},
                }
            )
        )["tool_argument_loops"]
    }
    assert (loops["half-total"]["total"], loops["half-total"]["peak"]) == (5, 5)
    assert (loops["half-peak"]["total"], loops["half-peak"]["peak"]) == (6, 6)
    for tool in ("empty-pair", "str-bucket", "bool-bucket", "null-bucket", "nested-junk"):
        assert (loops[tool]["total"], loops[tool]["peak"]) == (0, 0), tool


def test_tool_argument_loops_thresholds_are_task_3_and_others_8_by_default():
    loops = {loop["tool"]: loop for loop in _context()["tool_argument_loops"]}
    assert loops["task"]["task_threshold"] == 3
    assert loops["read"]["task_threshold"] == 8
    assert (AGENT_LOOP_TASK_MIN_REPEATS_DEFAULT, AGENT_LOOP_MIN_REPEATS_DEFAULT) == (3, 8)


def test_tool_argument_loops_thresholds_are_overridable():
    context = _context(loop_min_repeats=5, loop_task_min_repeats=2)
    loops = {loop["tool"]: loop["task_threshold"] for loop in context["tool_argument_loops"]}
    assert loops == {"read": 5, "task": 2}


def test_tool_argument_loops_survive_empty_and_malformed_fingerprints():
    assert _context(_summary(tool_argument_fingerprints={}))["tool_argument_loops"] == []
    assert _context(_summary(tool_argument_fingerprints=None))["tool_argument_loops"] == []
    assert _context(_summary(tool_argument_fingerprints=["read"]))["tool_argument_loops"] == []
    loops = _context(
        _summary(
            tool_argument_fingerprints={
                "read": None,
                "task": {},
                "write": {"fp": "many"},
                "grep": {"fp": {"total": 3, "peak": 6}},
            }
        )
    )["tool_argument_loops"]
    assert loops == [
        {"tool": "grep", "total": 3, "peak": 6, "task_threshold": AGENT_LOOP_MIN_REPEATS_DEFAULT},
        {"tool": "read", "total": 0, "peak": 0, "task_threshold": AGENT_LOOP_MIN_REPEATS_DEFAULT},
        {
            "tool": "task",
            "total": 0,
            "peak": 0,
            "task_threshold": AGENT_LOOP_TASK_MIN_REPEATS_DEFAULT,
        },
        {"tool": "write", "total": 0, "peak": 0, "task_threshold": AGENT_LOOP_MIN_REPEATS_DEFAULT},
    ]


def test_tool_argument_loops_rule_compares_peak_to_its_own_threshold():
    """The adapter supplies peak/total + the reference threshold, never the verdict."""
    context, _extra = build_context(_summary())
    rule = Rule(
        id="loop",
        name="loop",
        group="cost",
        severity="high",
        scope="tool_argument_loops",
        thresholds={"min_repeats": 8},
        detect={"scan": "tool_argument_loops", "match": "peak >= thresholds.min_repeats"},
    )
    finding = evaluate_rule(rule, context)
    assert finding is not None
    assert finding["occurrences"] == 1  # read=9 only; task=4 < 8


def test_tool_argument_loops_rule_stays_silent_when_only_the_total_passes():
    """La valeur du changement : 13 appels identiques, mais 1 par session ⇒ muet."""
    context, _extra = build_context(
        _summary(tool_argument_fingerprints={"weekly_run": {"fp": {"total": 13, "peak": 1}}})
    )
    rule = Rule(
        id="loop",
        name="loop",
        group="cost",
        severity="high",
        scope="tool_argument_loops",
        thresholds={"min_repeats": 8},
        detect={"scan": "tool_argument_loops", "match": "peak >= thresholds.min_repeats"},
    )
    assert evaluate_rule(rule, context) is None
    # ... et la même entrée déclenche bien sur le total : ce n'est pas le seuil qui a bougé
    on_total = Rule(
        id="loop",
        name="loop",
        group="cost",
        severity="high",
        scope="tool_argument_loops",
        thresholds={"min_repeats": 8},
        detect={"scan": "tool_argument_loops", "match": "total >= thresholds.min_repeats"},
    )
    assert evaluate_rule(on_total, context) is not None


# --------------------------------------------------------------------------- #
# daily_spikes (shared robust z)
# --------------------------------------------------------------------------- #
def test_daily_spikes_expose_day_cost_and_raw_z_for_positive_days_only():
    spikes = _context()["daily_spikes"]
    assert [spike["day"] for spike in spikes] == ["2026-10-01", "2026-10-02", "2026-10-03"]
    assert all(set(spike) == {"day", "cost_usd", "raw_z"} for spike in spikes)
    assert spikes[-1]["cost_usd"] == 5.0
    assert spikes[-1]["raw_z"] == 13.15


def test_daily_spikes_reuse_the_shared_robust_z_core(monkeypatch: pytest.MonkeyPatch):
    calls: list[list[float]] = []
    shared = rule_context._robust_z_scores

    def _spy(values: list[float]) -> list[float]:
        calls.append(list(values))
        return shared(values)

    monkeypatch.setattr(rule_context, "_robust_z_scores", _spy)
    _context()
    # baseline (3 days, current run excluded by the caller) + the candidate day
    assert calls[0] == [1.0, 1.2, 0.8, 1.0]
    assert len(calls) == 3


def test_daily_spikes_z_matches_median_plus_mad_from_util():
    baseline = [1.0, 1.2, 0.8]
    spikes = _context()["daily_spikes"]
    for spike in spikes:
        expected = round(robust_z([*baseline, spike["cost_usd"]])[-1], 2)
        assert spike["raw_z"] == expected


def test_daily_spikes_are_not_capped_here():
    """MAD≈0 baseline produces an astronomical z; the cap is a downstream concern."""
    context = _context(_summary(daily_totals=[{"date": "2026-10-03", "cost_usd": 40.0}]))
    raw_z = context["daily_spikes"][0]["raw_z"]
    assert raw_z > 10.0  # not clamped to DAILY_SPIKE_Z_CAP


def test_daily_spikes_with_empty_baseline_are_zero_not_absent():
    spikes = build_context(_summary())[0]["daily_spikes"]
    assert [spike["raw_z"] for spike in spikes] == [0.0, 0.0, 0.0]
    assert build_context(_summary())[1]["baseline_days"] == 0


def test_daily_spikes_ignore_malformed_costs():
    context = _context(
        _summary(
            daily_totals=[
                {"date": "d1", "cost_usd": "nope"},
                {"date": "d2"},
                {"cost_usd": 2.0},
                "broken",
            ]
        )
    )
    assert [spike["cost_usd"] for spike in context["daily_spikes"]] == [2.0]
    assert context["daily_spikes"][0]["day"] is None


# --------------------------------------------------------------------------- #
# architecture_drift
# --------------------------------------------------------------------------- #
def test_architecture_drift_is_always_a_single_entry():
    entries = _context()["architecture_drift"]
    assert len(entries) == 1
    assert set(entries[0]) == {
        "changed_fields",
        "drift_runs",
        "drift_threshold",
        "observation",
    }


def test_architecture_drift_lists_changed_fields_and_threshold():
    entry = _context(previous_summary=PREVIOUS)["architecture_drift"][0]
    # `_summary()` differs from `PREVIOUS` on state_counts + inventory_counts only
    assert entry["changed_fields"] == ["state_counts", "inventory_counts"]
    assert entry["drift_threshold"] == ARCHITECTURE_DRIFT_RUNS_DEFAULT == 2
    assert entry["observation"] == _architecture(2)


def test_architecture_drift_covers_config_and_harness_scope_too():
    summary = _summary(architecture_observations=_architecture(1, config="cfg-b", harness=7))
    entry = _context(summary, previous_summary=PREVIOUS)["architecture_drift"][0]
    assert entry["changed_fields"] == ["config", "harness_scope"]
    assert entry["drift_runs"] == 1


def test_architecture_drift_without_previous_summary_reports_no_drift():
    entry = _context(previous_summary=None)["architecture_drift"][0]
    assert entry["changed_fields"] == []
    assert entry["drift_runs"] == 0
    assert entry["drift_threshold"] == 2


def test_architecture_drift_without_any_observation_is_empty_but_present():
    context, _extra = build_context({"daily_totals": []}, recent_summaries=RECENT)
    entry = context["architecture_drift"][0]
    assert entry["changed_fields"] == []
    assert entry["observation"] == {}


def test_architecture_drift_counts_consecutive_runs_like_insights():
    previous = {"architecture_observations": _architecture(1)}
    recent = [
        {"architecture_observations": _architecture(1)},
        {"architecture_observations": _architecture(1)},
    ]
    entry = build_context(_summary(), previous_summary=previous, recent_summaries=recent)[0][
        "architecture_drift"
    ][0]
    # current + 2 earlier runs all differ from the previous one
    assert entry["drift_runs"] == 3

    # a run that matches the current observation interrupts the streak
    recent_with_match = [{"architecture_observations": _architecture(2)}, *recent]
    entry = build_context(
        _summary(), previous_summary=previous, recent_summaries=recent_with_match
    )[0]["architecture_drift"][0]
    assert entry["drift_runs"] == 1


def test_architecture_drift_reads_the_watch_context_mirror():
    summary = _summary()
    summary.pop("architecture_observations")
    summary["watch_context"] = {"architecture_observations": _architecture(2)}
    context = _context(summary, previous_summary=PREVIOUS)
    assert context["architecture_drift"][0]["observation"] == _architecture(2)


def test_architecture_drift_threshold_is_floored_at_one():
    entry = _context(previous_summary=PREVIOUS, architecture_drift_runs=0)["architecture_drift"][0]
    assert entry["drift_threshold"] == 1


# --------------------------------------------------------------------------- #
# extra
# --------------------------------------------------------------------------- #
def test_extra_exposes_the_adapter_knobs():
    _context_, extra = _build(
        loop_min_repeats=7, loop_task_min_repeats=2, architecture_drift_runs=3
    )
    assert extra["loop_min_repeats"] == 7
    assert extra["loop_task_min_repeats"] == 2
    assert extra["architecture_drift_runs"] == 3
    assert extra["baseline_days"] == 3
    assert extra["has_previous_summary"] is False


def test_extra_leaves_the_ignored_list_to_the_caller():
    _context_, extra = _build(previous_summary=PREVIOUS)
    assert "ignored" not in extra  # the caller owns extra["ignored"]


def test_extra_is_rendered_by_the_engine_templates():
    context, extra = build_context(_summary(), loop_min_repeats=7)
    rule = Rule(
        id="render",
        name="render",
        group="cost",
        severity="low",
        scope="tool_argument_loops",
        thresholds={"min_repeats": 999},
        description="seuil de refactorisation: {{extra.loop_min_repeats}}",
        detect={"scan": "tool_argument_loops", "check": {"triggered": "total >= 0"}},
    )
    finding = evaluate_rule(rule, context, extra=extra)
    assert finding is not None
    assert finding["description"] == "seuil de refactorisation: 7"


# --------------------------------------------------------------------------- #
# Purity
# --------------------------------------------------------------------------- #
def test_build_context_does_not_mutate_its_inputs():
    summary = _summary()
    findings = {"findings": [{"category": "c1"}]}
    previous = {"architecture_observations": _architecture(1)}
    recent = [{"daily_totals": [{"date": "d", "cost_usd": 1.0}]}]
    before = (
        dict(summary),
        dict(findings),
        dict(previous),
        dict(recent[0]),
    )
    build_context(
        summary,
        previous_summary=previous,
        recent_summaries=recent,
        quality_findings=findings,
    )
    assert (summary, findings, previous, recent[0]) == before
    assert summary["architecture_observations"] == _architecture(2)
