"""Candidate selection for the LLM stages (Partie 3 §2 / Partie 4 §3) — pure."""

from __future__ import annotations

from weekly_telemetry_aggregator.candidates import select_audit_candidates, select_draft_candidates


def _summary(*, tops=None, outliers=None, repeats=None, weekly_cache=0.8):
    return {
        "totals": {"cache_hit_rate": weekly_cache},
        "top_sessions_by_cost": tops or [],
        "cost_outliers": outliers or [],
        "user_prompt_repeats": repeats or [],
    }


def test_audit_priority_order_and_dedup():
    summary = _summary(
        tops=[
            {"session_id": "a", "cost_per_active_minute": 0.1, "cache_efficiency": 0.9},
            {"session_id": "b", "cost_per_active_minute": 2.0, "cache_efficiency": 0.5},
        ],
        outliers=[{"session_id": "c", "cost_usd": 5.0, "z_score": 4.0}],
        repeats=[{"session_id": "b", "count": 4, "normalized_preview": "x"}],
    )
    cands = select_audit_candidates(
        summary, top_sessions_limit=5, cost_per_active_minute_min=0.5, cache_efficiency_gap=0.2
    )
    ids = [c["session_id"] for c in cands]
    assert ids == ["a", "b", "c"]  # priorité : top-cost d'abord (a, b), outlier ensuite (c)
    by_id = {c["session_id"]: c for c in cands}
    assert by_id["b"]["reasons"] == ["top-cost", "loop", "cache-gap", "repeated-prompts"]


def test_audit_respects_top_limit_and_gap():
    tops = [
        {"session_id": f"s{i}", "cost_per_active_minute": 0.1, "cache_efficiency": 0.9}
        for i in range(8)
    ]
    summary = _summary(tops=tops, weekly_cache=0.8)
    cands = select_audit_candidates(
        summary, top_sessions_limit=3, cost_per_active_minute_min=0.5, cache_efficiency_gap=0.2
    )
    assert [c["session_id"] for c in cands] == ["s0", "s1", "s2"]
    # cache gap détecté quand cache_efficiency < moyenne - gap
    gap = _summary(tops=[{"session_id": "x", "cache_efficiency": 0.5}], weekly_cache=0.8)
    assert [c["session_id"] for c in select_audit_candidates(gap, cache_efficiency_gap=0.2)] == [
        "x"
    ]


def test_draft_candidates_severity_order_and_cap():
    findings = {
        "findings": [
            {
                "session_id": "s1",
                "category": "c",
                "severity": "low",
                "recommendation_type": "skill-candidate",
                "description": "d",
                "recommendation": "r",
            },
            {
                "session_id": "s2",
                "category": "c",
                "severity": "high",
                "recommendation_type": "command-candidate",
                "description": "d",
                "recommendation": "r",
            },
            {
                "session_id": "s3",
                "category": "c",
                "severity": "medium",
                "recommendation_type": "prompting-habit",
                "description": "d",
                "recommendation": "r",
            },
            {
                "session_id": "s4",
                "category": "c",
                "severity": "medium",
                "recommendation_type": "skill-candidate",
                "description": "d",
                "recommendation": "r",
            },
        ]
    }
    cands = select_draft_candidates(findings, max_candidates=2)
    assert [c["session_id"] for c in cands] == ["s2", "s4"]  # high puis medium, plafond 2
    assert select_draft_candidates(None, max_candidates=3) == []
    assert select_draft_candidates({"findings": []}, max_candidates=3) == []


def test_draft_candidates_includes_command_improvement():
    """v5.30 (E) : command-improvement est un candidat de drafting comme les autres."""
    findings = {
        "findings": [
            {
                "session_id": "s1",
                "category": "command-improvement",
                "severity": "high",
                "recommendation_type": "command-improvement",
                "description": "d",
                "recommendation": "r",
            },
            {
                "session_id": "s2",
                "category": "c",
                "severity": "medium",
                "recommendation_type": "prompting-habit",
                "description": "d",
                "recommendation": "r",
            },
            {
                "session_id": "s3",
                "category": "c",
                "severity": "low",
                "recommendation_type": "command-candidate",
                "description": "d",
                "recommendation": "r",
            },
        ]
    }
    cands = select_draft_candidates(findings, max_candidates=3)
    ids = [c["session_id"] for c in cands]
    assert ids == [
        "s1",
        "s3",
    ]  # command-improvement high + command-candidate low ; prompting-habit exclu


def test_audit_candidates_keep_canonical_multi_harness_ids():
    """Ids canoniques <harness>:<id> traversent intacts ; dédup par id complet."""
    summary = _summary(
        tops=[
            {"session_id": "opencode:a", "cost_per_active_minute": 0.1, "cache_efficiency": 0.9},
            {"session_id": "copilot:b", "cost_per_active_minute": 2.0, "cache_efficiency": 0.5},
        ],
        outliers=[{"session_id": "opencode:a", "cost_usd": 5.0, "z_score": 4.0}],
    )
    cands = select_audit_candidates(
        summary, top_sessions_limit=5, cost_per_active_minute_min=0.5, cache_efficiency_gap=0.2
    )
    ids = [c["session_id"] for c in cands]
    assert ids == ["opencode:a", "copilot:b"]  # outlier dup → raisons fusionnées, pas de doublon
    assert cands[0]["reasons"] == ["top-cost", "cost-outlier"]
