"""Tests du module curation (R4 curation/GC + R8 TTL/décroissance)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from weekly_telemetry_aggregator import curation


def _iso(days_ago: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()


# ----------------------------------------------------------------- user-origin


def test_curation_protects_user_origin():
    """Un skill d'origin='user' ne génère jamais archive/merge/delete."""
    findings = [
        {"tag_action": "merge", "target_skill_id": "my/user-skill"},
        {"tag_action": "delete", "target_skill_id": "my/user-skill"},
        {"tag_action": "reference", "target_skill_id": "my/user-skill"},
    ]
    catalog = [
        {
            "skill_id": "my/user-skill",
            "metadata": {"origin": "user", "ttl_policy": None},
        }
    ]

    decisions = curation.decide_actions(findings, catalog)

    assert decisions, "au moins une décision attendue"
    for d in decisions:
        assert d["target_skill_id"] == "my/user-skill"
        assert d["action"] == "skip", d
        assert d["reason"] == "user-origin protected"
    # aucune action destructive ne doit fuiter
    assert all(d["action"] not in {"merge", "delete", "archive"} for d in decisions)


def test_curation_user_explicit_pin_allowed():
    """Un pin explicitement levé sur un skill user est toléré (action='pin')."""
    findings = [{"tag_action": "pin", "target_skill_id": "my/user-skill"}]
    catalog = [
        {"skill_id": "my/user-skill", "metadata": {"origin": "user", "ttl_policy": None}}
    ]

    decisions = curation.decide_actions(findings, catalog)

    assert len(decisions) == 1
    assert decisions[0]["action"] == "pin"


def test_curation_pinned_never_archived_or_deleted():
    """ttl_policy=='pin' force action='pin', jamais delete/archive."""
    findings = [{"tag_action": "delete", "target_skill_id": "pinned/skill"}]
    catalog = [
        {"skill_id": "pinned/skill", "metadata": {"origin": "weekly-background", "ttl_policy": "pin"}}
    ]

    decisions = curation.decide_actions(findings, catalog)

    assert len(decisions) == 1
    assert decisions[0]["action"] == "pin"


def test_curation_normal_emit_action():
    """Un skill non-user non-pinned émet l'action du finding."""
    findings = [{"tag_action": "merge", "target_skill_id": "dup/skill"}]
    catalog = [
        {"skill_id": "dup/skill", "metadata": {"origin": "weekly-background", "ttl_policy": None}}
    ]

    decisions = curation.decide_actions(findings, catalog)

    assert decisions[0]["action"] == "merge"


def test_curation_ignores_irrelevant_tag_action():
    """Les findings hors ensemble d'action ne produisent aucune décision."""
    findings = [{"tag_action": "summary", "target_skill_id": "x/skill"}]
    catalog = [{"skill_id": "x/skill", "metadata": {"origin": "user"}}]

    assert curation.decide_actions(findings, catalog) == []


# --------------------------------------------------------------- ttl archive


def test_ttl_archive_stale():
    """last_loaded > 90j -> archivé ; pin -> NON ; load_count==0 sur 3 runs -> archivé."""
    usage = [
        # vieux de 100j, charge normal -> doit être archivé (stale)
        {
            "skill_id": "stale/skill",
            "ttl_policy": None,
            "usage": {"last_loaded": _iso(100), "load_count": 5},
        },
        # vieux de 100j mais épinglé -> exempté
        {
            "skill_id": "pinned/old",
            "ttl_policy": "pin",
            "usage": {"last_loaded": _iso(100), "load_count": 5},
        },
        # chargé récemment, jamais utilisé sur 3 runs -> archivé (zero_three)
        {
            "skill_id": "unused/skill",
            "ttl_policy": None,
            "usage": {"last_loaded": _iso(1), "load_count": 0},
        },
    ]

    archived = curation.ttl_archive_candidates(usage, runs_seen=3, stale_days=90)

    assert "stale/skill" in archived
    assert "unused/skill" in archived
    assert "pinned/old" not in archived


def test_ttl_archive_not_stale_when_recent():
    """Un skill chargé récemment avec load_count>0 n'est pas archivé."""
    usage = [
        {
            "skill_id": "active/skill",
            "ttl_policy": None,
            "usage": {"last_loaded": _iso(2), "load_count": 12},
        }
    ]

    assert curation.ttl_archive_candidates(usage, runs_seen=5) == []


def test_ttl_archive_zero_load_needs_three_runs():
    """load_count==0 n'archive qu'après runs_seen>=3."""
    usage = [
        {
            "skill_id": "cold/skill",
            "ttl_policy": None,
            "usage": {"last_loaded": _iso(1), "load_count": 0},
        }
    ]

    assert curation.ttl_archive_candidates(usage, runs_seen=2) == []
    assert curation.ttl_archive_candidates(usage, runs_seen=3) == ["cold/skill"]


def test_ttl_archive_exempts_pin_even_zero():
    """Un skill pin même à load_count==0 n'est jamais archivé."""
    usage = [
        {
            "skill_id": "pinned/cold",
            "ttl_policy": "pin",
            "usage": {"last_loaded": _iso(1), "load_count": 0},
        }
    ]

    assert curation.ttl_archive_candidates(usage, runs_seen=10) == []
