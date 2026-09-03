"""Candidate selection for the LLM stages (Partie 3 §2 / Partie 4 §3) — pure."""

from __future__ import annotations

from weekly_telemetry_aggregator.candidates import (
    consolidate_candidates,
    generate_skill_id,
    is_anti_learning,
    select_audit_candidates,
    select_draft_candidates,
)


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


def test_provenance_skill_id_stable():
    """R1 : skill_id déterministe, insensible à la casse/espace."""
    assert generate_skill_id("Foo Bar") == generate_skill_id("foo bar")
    assert generate_skill_id("  Baz QUx ") == generate_skill_id("baz qux")
    assert len(generate_skill_id("anything")) == len("skill_") + 8


def test_anti_learning_drop():
    """R2 : patterns à NE PAS capturer → True ; cas légitime → False."""
    transient = {
        "description": "transient timeout failure on flaky network",
        "evidence": "",
        "recommendation_type": "skill-candidate",
    }
    env_specific = {
        "description": "environment-specific prohibition in staging only",
        "evidence": "",
        "recommendation_type": "skill-candidate",
    }
    secret = {
        "description": "leaked api key credential found in logs",
        "evidence": "password=xyz",
        "recommendation_type": "skill-candidate",
    }
    pr_ref = {
        "description": "fix applied, see PR #123 for context",
        "evidence": "JIRA ticket ABC-9 referenced",
        "recommendation_type": "skill-candidate",
    }

    assert is_anti_learning(transient) is True
    assert is_anti_learning(env_specific) is True
    assert is_anti_learning(secret) is True
    assert is_anti_learning(pr_ref) is True

    legit = {
        "description": "cache expensive repeated prompts via a memoization skill",
        "evidence": "",
        "recommendation_type": "skill-candidate",
    }
    assert is_anti_learning(legit) is False


def test_consolidation_patches_existing():
    """R3 : overlap/skill_id existant → patch + target ; sinon create."""
    patching = {
        "session_id": "s1",
        "name": "Retry Policy",
        "recommendation_type": "skill-candidate",
        "overlaps_with": ["skill_abc12345"],
    }
    creating = {
        "session_id": "s2",
        "name": "Prompt Cache",
        "recommendation_type": "skill-candidate",
    }

    result = consolidate_candidates([patching, creating])

    assert result[0]["action"] == "patch"
    assert result[0]["target_skill_id"] == "skill_abc12345"
    assert result[0]["skill_id"] == generate_skill_id("Retry Policy")
    assert result[0]["origin"] == "weekly-background"

    assert result[1]["action"] == "create"
    assert "target_skill_id" not in result[1]
    assert result[1]["skill_id"] == generate_skill_id("Prompt Cache")
    assert result[1]["origin"] == "weekly-background"


def test_select_draft_drops_anti_learning():
    """R2 : findings anti-learning (is_anti_learning True) jamais draft candidates,
    même avec recommendation_type='skill-candidate'. Légitime présent."""
    findings = {
        "findings": [
            {
                "session_id": "s1",
                "category": "c",
                "severity": "high",
                "recommendation_type": "skill-candidate",
                "description": "PR #123 secret token leaked in logs",
                "recommendation": "r",
            },
            {
                "session_id": "s2",
                "category": "c",
                "severity": "high",
                "recommendation_type": "skill-candidate",
                "description": "cache expensive repeated prompts via a memoization skill",
                "recommendation": "r",
            },
            {
                "session_id": "s3",
                "category": "c",
                "severity": "low",
                "recommendation_type": "command-candidate",
                "description": "one-off manual fix, not worth capturing",
                "recommendation": "r",
            },
        ]
    }
    cands = select_draft_candidates(findings, max_candidates=3)
    ids = [c["session_id"] for c in cands]
    assert "s1" not in ids  # anti-learning (PR #123 secret token) → DROP
    assert "s3" not in ids  # anti-learning (one-off) → DROP
    assert "s2" in ids  # légitime → présent
    # s2 prioritaire (high légitime seul restant)
    assert ids == ["s2"]


def test_select_draft_enriches_with_provenance():
    """select_draft_candidates conserve tri/sévérité ET attache provenance (R1/R3)."""
    findings = {
        "findings": [
            {
                "session_id": "s1",
                "name": "Low Skill",
                "severity": "low",
                "recommendation_type": "skill-candidate",
            },
            {
                "session_id": "s2",
                "name": "High Skill",
                "severity": "high",
                "recommendation_type": "command-candidate",
            },
            {
                "session_id": "s3",
                "name": "Med Skill",
                "severity": "medium",
                "recommendation_type": "prompting-habit",
            },
        ]
    }
    cands = select_draft_candidates(findings, max_candidates=2)
    ids = [c["session_id"] for c in cands]
    assert ids == ["s2", "s1"]  # high puis low, prompting-habit exclu — tri préservé
    assert all(c["origin"] == "weekly-background" for c in cands)
    assert all(c["action"] == "create" for c in cands)
    assert cands[0]["skill_id"] == generate_skill_id("High Skill")
