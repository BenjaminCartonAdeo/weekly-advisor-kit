"""Déterministes P6 classifiers — intent / spec-driven / production-review / maturity.

Aucun LLM, aucun I/O : les classifieurs opèrent sur `SessionUsage` et retournent
des structures sérialisables.
"""

from __future__ import annotations

from datetime import timedelta

from helpers import tzutc

from weekly_telemetry_aggregator.classifiers import (
    GRADE_THRESHOLDS,
    REVIEW_GAP_SECONDS,
    classify_intent,
    classify_production_review,
    classify_session,
    detect_spec_driven,
    grade_for_score,
    score_prompt_maturity,
)
from weekly_telemetry_aggregator.models import SessionUsage


def _usage(sid="ses_x", *, turns=None, tools=None, **kwargs) -> SessionUsage:
    return SessionUsage(
        session_id=sid,
        user_turns=list(turns or []),
        tool_calls=dict(tools or {}),
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# P6.1 — intent
# --------------------------------------------------------------------------- #


def test_intent_planning_beats_debug():
    usage = _usage(turns=["plan the fix for this erreur"], tools={"edit": 1})
    assert classify_intent(usage) == "planning"


def test_intent_debug_french():
    assert classify_intent(_usage(turns=["corrige l'erreur de parsing"])) == "debug"


def test_intent_review():
    assert classify_intent(_usage(turns=["review this file please"])) == "review"


def test_intent_explore_keyword():
    usage = _usage(turns=["où est la fonction principale"], tools={"read": 3})
    assert classify_intent(usage) == "explore"


def test_intent_explore_from_read_tools_without_keywords():
    assert classify_intent(_usage(turns=["bonjour"], tools={"read": 3})) == "explore"


def test_intent_implementation_with_edit_tools():
    usage = _usage(turns=["ajoute une fonction utilitaire"], tools={"edit": 2, "read": 1})
    assert classify_intent(usage) == "implementation"


def test_intent_empty_defaults_to_implementation():
    assert classify_intent(_usage()) == "implementation"


def test_intent_priority_review_over_explore():
    usage = _usage(turns=["check how does the cache work"], tools={"read": 1})
    assert classify_intent(usage) == "review"


# --------------------------------------------------------------------------- #
# P6.2 — spec_driven
# --------------------------------------------------------------------------- #


def test_spec_driven_collects_all_signals():
    usage = _usage(
        turns=[
            "voici la spec: docs/prd.md",
            "the system must be idempotent and doit rester stable",
            "- premier point",
            "/plan",
        ]
    )
    result = detect_spec_driven(usage)
    assert result["is_spec"] is True
    preuves = result["preuves"]
    assert "path:docs/prd.md" in preuves
    assert "modal:must" in preuves
    assert "modal:doit" in preuves
    assert "list:bullet" in preuves
    assert "command:/plan" in preuves


def test_spec_not_driven():
    result = detect_spec_driven(_usage(turns=["juste une question rapide"]))
    assert result["is_spec"] is False
    assert result["preuves"] == []


def test_spec_md_path_without_keyword_not_counted():
    result = detect_spec_driven(_usage(turns=["lis README.md s'il te plait"]))
    assert result["is_spec"] is False


def test_spec_list_variants_detected():
    ordered = detect_spec_driven(_usage(turns=["1. première étape"]))
    checkbox = detect_spec_driven(_usage(turns=["[ ] tâche à faire"]))
    assert "list:ordered" in ordered["preuves"]
    assert "list:checkbox" in checkbox["preuves"]


# --------------------------------------------------------------------------- #
# P6.3 — production_review
# --------------------------------------------------------------------------- #


def test_review_null_and_warning_when_user_timestamps_missing():
    usage = _usage("ses_a", tools={"edit": 1}, harness="opencode")
    result = classify_production_review(usage)
    assert result["review_pct"] is None
    assert result["measured"] == 0
    assert result["warning"] == "review-unmeasurable:opencode"


def test_review_gap_exactly_30s_is_not_reviewed():
    base = tzutc(2026, 8, 6, 10)
    usage = _usage(
        turns=["fais"],
        tools={"edit": 1},
        edit_write_timestamps=[base],
        user_turn_timestamps=[base + timedelta(seconds=REVIEW_GAP_SECONDS)],
    )
    assert classify_production_review(usage)["review_pct"] == 0.0


def test_review_gap_over_30s_is_reviewed():
    base = tzutc(2026, 8, 6, 10)
    usage = _usage(
        turns=["fais"],
        tools={"edit": 1},
        edit_write_timestamps=[base],
        user_turn_timestamps=[base + timedelta(seconds=REVIEW_GAP_SECONDS + 1)],
    )
    result = classify_production_review(usage)
    assert result["review_pct"] == 1.0
    assert result["reviewed"] == 1
    assert result["measured"] == 1
    assert result["warning"] is None


def test_review_mixed_ratio():
    base = tzutc(2026, 8, 6, 10)
    usage = _usage(
        turns=["fais"],
        tools={"edit": 2},
        edit_write_timestamps=[base, base + timedelta(minutes=2)],
        user_turn_timestamps=[
            base + timedelta(seconds=10),  # non relu
            base + timedelta(minutes=3),  # relu (2 min > 30 s)
        ],
    )
    assert classify_production_review(usage)["review_pct"] == 0.5


def test_review_no_edit_activity_has_no_warning():
    usage = _usage(turns=["discussion"], tools={"read": 5}, harness="opencode")
    result = classify_production_review(usage)
    assert result["review_pct"] is None
    assert result["warning"] is None


def test_review_edit_without_following_turn_is_unmeasured():
    base = tzutc(2026, 8, 6, 10)
    usage = _usage(
        turns=["fais"],
        tools={"edit": 1},
        edit_write_timestamps=[base],
        user_turn_timestamps=[base - timedelta(minutes=5)],
    )
    result = classify_production_review(usage)
    assert result["review_pct"] is None
    assert result["measured"] == 0


# --------------------------------------------------------------------------- #
# P6.4 — prompt_maturity
# --------------------------------------------------------------------------- #


def test_grade_boundaries_are_exact():
    assert grade_for_score(80) == "A"
    assert grade_for_score(79) == "B"
    assert grade_for_score(65) == "B"
    assert grade_for_score(64) == "C"
    assert grade_for_score(50) == "C"
    assert grade_for_score(49) == "D"
    assert grade_for_score(40) == "D"
    assert grade_for_score(39) == "F"
    assert GRADE_THRESHOLDS["A"] == 80


def test_maturity_empty_is_f():
    result = score_prompt_maturity(_usage())
    assert result["score"] == 0
    assert result["grade"] == "F"


def test_maturity_dimensions_are_bounded():
    usage = _usage(
        turns=[
            "Dans src/app.py, ajoute la fonction parse() — the function must validate and ensure tests pass.",
            "1. vérifie avec pytest et ruff",
            "3. ne pas casser l'API",
        ]
    )
    result = score_prompt_maturity(usage)
    dims = result["dimensions"]
    assert set(dims) == {"specificite", "contexte", "contrainte", "verifiabilite", "iterativite"}
    assert all(0 <= v <= 20 for v in dims.values())
    assert 0 <= result["score"] <= 100
    assert result["score"] == sum(dims.values())


def test_maturity_rich_prompt_scores_higher_than_poor():
    rich = score_prompt_maturity(
        _usage(
            turns=[
                "Dans le fichier src/app.py, ajoute la fonction parse() — must ensure tests pass.",
                "vérifie avec pytest",
            ]
        )
    )
    poor = score_prompt_maturity(_usage(turns=["fais ça"]))
    assert rich["score"] > poor["score"]


def test_grade_for_score_matches_maturity_result():
    usage = _usage(
        turns=["Dans src/app.py ajoute parse() must ensure tests pass", "vérifie pytest"]
    )
    result = score_prompt_maturity(usage)
    assert result["grade"] == grade_for_score(result["score"])


# --------------------------------------------------------------------------- #
# classify_session — assemblage
# --------------------------------------------------------------------------- #


def test_classify_session_assembles_all_detectors():
    base = tzutc(2026, 8, 6, 10)
    usage = _usage(
        "ses_full",
        turns=["plan the spec: docs/plan.md", "the system must ensure tests"],
        tools={"edit": 1},
        harness="claude-code",
        edit_write_timestamps=[base],
        user_turn_timestamps=[base + timedelta(minutes=1)],
    )
    sc = classify_session(usage)
    assert sc.session_id == "ses_full"
    assert sc.intent == "planning"
    assert sc.spec_driven is True
    assert sc.production_review_pct == 1.0
    assert sc.production_review_measured == 1
    assert sc.production_review_warning is None
    assert 0 <= sc.prompt_maturity_score <= 100
    assert sc.prompt_maturity_grade in {"A", "B", "C", "D", "F"}
    assert sc.prompt_maturity_dimensions


def test_classify_session_warning_surfaces_harness():
    usage = _usage("ses_w", turns=["edit ça"], tools={"write": 1}, harness="copilot-cli")
    sc = classify_session(usage)
    assert sc.production_review_pct is None
    assert sc.production_review_warning == "review-unmeasurable:copilot-cli"
