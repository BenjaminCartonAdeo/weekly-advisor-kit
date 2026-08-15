"""Unit tests for the pure aggregation logic (Part 1 §4) — v5.27 models/schema."""

from __future__ import annotations

from datetime import timedelta

from helpers import make_step, make_usage, tzutc

from weekly_telemetry_aggregator.aggregator import OUTLIER_MIN_ROOTS, aggregate
from weekly_telemetry_aggregator.models import (
    Period,
    SkillCatalogEntry,
    WarningEntry,
)


def _period():
    return Period(start=tzutc(2026, 8, 5, 0, 0), end=tzutc(2026, 8, 12, 0, 0))


def test_children_merged_once_into_root_totals():
    period = _period()
    root = make_usage(
        "ses_root", [make_step("ses_root", period.start + timedelta(hours=1), cost=1.0)]
    )
    child = make_usage(
        "ses_child",
        [make_step("ses_child", period.start + timedelta(hours=2), cost=2.0)],
        parent="ses_root",
    )
    summary = aggregate([root, child], period=period, generated_at=period.end)
    assert summary.totals.session_count == 1  # roots only
    assert summary.totals.total_cost_usd == 3.0  # children merged exactly once
    assert summary.subagent_totals.child_session_count == 1
    assert summary.subagent_totals.total_cost_usd == 2.0


def test_cache_hit_rate_excludes_cache_write_from_denominator():
    period = _period()
    # cache_read 60, fresh 30, cache_write 90 → hit rate must be 60/90, not 60/180.
    root = make_usage(
        "r", [make_step("r", period.start, cost=0.0, cache_read=60, fresh=30, cache_write=90)]
    )
    summary = aggregate([root], period=period, generated_at=period.end)
    assert summary.totals.cache_hit_rate == round(60 / 90, 6)
    assert summary.totals.cache_write_tokens == 90


def test_cache_hit_rate_null_when_no_input():
    period = _period()
    root = make_usage("r", [make_step("r", period.start, cost=0.0, cache_read=0, fresh=0)])
    summary = aggregate([root], period=period, generated_at=period.end)
    assert summary.totals.cache_hit_rate is None


def test_missing_pricing_steps_excluded_from_cost_totals():
    period = _period()
    paid = make_step("r", period.start, cost=0.5)
    unpaid = make_step("r", period.start + timedelta(minutes=1), cost=None)
    root = make_usage("r", [paid, unpaid])
    summary = aggregate([root], period=period, generated_at=period.end, warnings=[])
    assert summary.totals.total_cost_usd == 0.5
    assert summary.totals.total_tokens == paid.total_tokens + unpaid.total_tokens


def test_top_sessions_ordered_by_cost_desc_then_session_asc():
    period = _period()
    a = make_usage("a", [make_step("a", period.start, cost=2.0)])
    b = make_usage("b", [make_step("b", period.start, cost=3.0)])
    c = make_usage("c", [make_step("c", period.start, cost=2.0)])
    summary = aggregate([a, b, c], period=period, generated_at=period.end, top_sessions_limit=3)
    ids = [s.session_id for s in summary.top_sessions_by_cost]
    assert ids == ["b", "a", "c"]  # cost DESC, session_id ASC as tiebreak


def test_top_session_duration_and_active_time():
    period = _period()
    t0 = period.start
    steps = [
        make_step("r", t0, cost=0.1),
        make_step("r", t0 + timedelta(seconds=30), cost=0.1),
        make_step("r", t0 + timedelta(minutes=10), cost=0.1),  # >5min gap → idle
    ]
    root = make_usage("r", steps)
    summary = aggregate([root], period=period, generated_at=period.end)
    top = summary.top_sessions_by_cost[0]
    assert top.duration_seconds == 600
    assert top.active_time_seconds == 30


def test_tool_usage_estimated_tokens():
    period = _period()
    root = make_usage(
        "r",
        [make_step("r", period.start, cost=0.1)],
        tools={"read": 2, "bash": 1},
    )
    summary = aggregate([root], period=period, generated_at=period.end)
    usage = {t.tool: t for t in summary.tool_usage}
    assert usage["read"].call_count == 2
    # tool_arg_chars = 40 chars per call → 2*40 = 80 → /4 = 20 tokens
    assert usage["read"].estimated_tokens == 20
    assert usage["bash"].estimated_tokens == 10


def test_skill_usage_and_never_loaded():
    period = _period()
    root = make_usage(
        "r",
        [make_step("r", period.start, cost=0.1)],
        skills={"graphify": 2},
    )
    summary = aggregate(
        [root],
        period=period,
        generated_at=period.end,
        skill_catalog=["graphify", "unused-skill"],
    )
    assert summary.skill_usage[0].skill == "graphify"
    assert summary.skill_usage[0].load_count == 2
    assert summary.skills_never_loaded == ["unused-skill"]
    assert summary.skill_catalog_count == 2


def test_command_usage():
    period = _period()
    root = make_usage(
        "r",
        [make_step("r", period.start, cost=0.1)],
        user_turns=["bonjour", "/optimize le rapport", "/optimize encore"],
    )
    summary = aggregate([root], period=period, generated_at=period.end)
    assert summary.command_usage[0].command == "optimize"
    assert summary.command_usage[0].call_count == 2
    assert summary.command_usage[0].sessions_used_in == 1


def test_daily_totals_bucket_by_utc_day():
    period = _period()
    day1 = [make_step("r", tzutc(2026, 8, 6, 10), cost=1.0)]
    day2 = [make_step("r", tzutc(2026, 8, 7, 10), cost=2.0)]
    root = make_usage("r", day1 + day2)
    summary = aggregate([root], period=period, generated_at=period.end)
    assert [d.date for d in summary.daily_totals] == ["2026-08-06", "2026-08-07"]
    assert summary.daily_totals[0].cost_usd == 1.0
    assert summary.daily_totals[1].cost_usd == 2.0


def test_cost_outlier_detected_via_robust_z():
    period = _period()
    usages = []
    for i in range(16):
        cost = 20.0 if i == 0 else 0.1
        usages.append(
            make_usage(
                f"s{i:02d}",
                [make_step(f"s{i:02d}", period.start + timedelta(minutes=i * 5), cost=cost)],
            )
        )
    summary = aggregate(
        usages,
        period=period,
        generated_at=period.end,
        session_outlier_z=2.5,
        session_outlier_min_cost_usd=0.5,
    )
    ids = [o.session_id for o in summary.cost_outliers]
    assert ids == ["s00"]


def test_sample_too_small_warning():
    period = _period()
    root = make_usage("r", [make_step("r", period.start, cost=0.1)])
    warnings: list[WarningEntry] = []
    summary = aggregate([root], period=period, generated_at=period.end, warnings=warnings)
    assert any(
        "sample" in w.message and str(OUTLIER_MIN_ROOTS) in w.message for w in summary.warnings
    )


def test_user_prompt_repeats_exact():
    period = _period()
    turns = ["améliore ce script", "améliore ce script", "améliore ce script"]
    root = make_usage("r", [make_step("r", period.start, cost=0.1)], user_turns=turns)
    summary = aggregate(
        [root],
        period=period,
        generated_at=period.end,
        user_prompt_repeat_min=3,
        user_prompt_repeat_similarity=0.9,
        user_prompt_repeat_min_chars=5,
    )
    assert summary.user_prompt_repeats[0].count == 3
    assert "améliore ce script" in summary.user_prompt_repeats[0].normalized_preview


def test_user_prompt_repeats_quasi_duplicates():
    period = _period()
    root = make_usage(
        "r",
        [make_step("r", period.start, cost=0.1)],
        user_turns=[
            "peux-tu optimiser le flux de données stp",
            "Peux-tu optimiser le flux de données stp ?",
            "peux-tu optimiser le flux de données stp!",
        ],
    )
    summary = aggregate(
        [root],
        period=period,
        generated_at=period.end,
        user_prompt_repeat_min=3,
        user_prompt_repeat_similarity=0.9,
        user_prompt_repeat_min_chars=5,
    )
    assert summary.user_prompt_repeats[0].count == 3


def test_skill_similar_pairs():
    period = _period()
    entries = [
        SkillCatalogEntry(
            name="alpha-helper",
            description="Réinitialise les tokens de contexte",
            body="usage: alpha-helper",
        ),
        SkillCatalogEntry(
            name="beta-helper",
            description="Réinitialise les tokens de contexte du modèle",
            body="usage: beta-helper",
        ),
        SkillCatalogEntry(
            name="graphify", description="construit un graphe de connaissance", body="graph"
        ),
    ]
    summary = aggregate(
        [],
        period=period,
        generated_at=period.end,
        skill_catalog_entries=entries,
        skill_similarity_min=0.6,
    )
    assert len(summary.skill_similar_pairs) >= 1
    assert sorted(summary.skill_similar_pairs[0].skills) == ["alpha-helper", "beta-helper"]


def test_partial_telemetry_excluded_and_warned():
    period = _period()
    good = make_usage("g", [make_step("g", period.start, cost=1.0)])
    partial = make_usage("p", [make_step("p", period.start, cost=5.0)], partial=True)
    warnings: list[WarningEntry] = []
    summary = aggregate([good, partial], period=period, generated_at=period.end, warnings=warnings)
    assert summary.totals.total_cost_usd == 1.0
    assert any("partial" in w.message and w.session_id == "p" for w in summary.warnings)


def test_orphan_child_counted_in_subagent_totals():
    period = _period()
    orphan = make_usage("o", [make_step("o", period.start, cost=2.0)], parent="missing-parent")
    warnings: list[WarningEntry] = []
    summary = aggregate([orphan], period=period, generated_at=period.end, warnings=warnings)
    assert summary.subagent_totals.child_session_count == 1
    assert summary.subagent_totals.total_cost_usd == 2.0
    assert any("orphan" in w.message for w in summary.warnings)


# ============================================================ v5.28 (2.1/2.2/6.1/3.2)


def test_cost_outliers_state_small_sample():
    period = _period()
    root = make_usage("r", [make_step("r", period.start, cost=0.1)])
    summary = aggregate([root], period=period, generated_at=period.end)
    assert summary.cost_outliers_state == "skipped:small-sample"


def test_cost_outliers_state_computed_with_enough_roots():
    period = _period()
    usages = [
        make_usage(f"s{i:02d}", [make_step(f"s{i:02d}", period.start, cost=0.1)]) for i in range(16)
    ]
    summary = aggregate(usages, period=period, generated_at=period.end)
    assert summary.cost_outliers_state == "computed"


def test_cost_outliers_state_no_data():
    period = _period()
    summary = aggregate([], period=period, generated_at=period.end)
    assert summary.cost_outliers_state == "no-data"


def test_outlier_min_sessions_configurable():
    period = _period()
    # K6: plancher dur = 5 racines ; le seuil configurable ne peut que le relever.
    usages = [
        make_usage(f"s{i:02d}", [make_step(f"s{i:02d}", period.start, cost=0.1)]) for i in range(5)
    ]
    summary = aggregate(usages, period=period, generated_at=period.end, outlier_min_sessions=10)
    assert summary.cost_outliers_state == "computed:small-sample"  # 5 >= plancher, 5 < 10
    summary2 = aggregate(usages, period=period, generated_at=period.end, outlier_min_sessions=5)
    assert summary2.cost_outliers_state == "computed"


def test_by_model_cache_hit_rate():
    period = _period()
    usages = [
        make_usage("a", [make_step("a", period.start, model="m1", cache_read=9, fresh=1)]),
        make_usage("b", [make_step("b", period.start, model="m2", cache_read=0, fresh=10)]),
    ]
    summary = aggregate(usages, period=period, generated_at=period.end)
    by_model = {m.model: m for m in summary.by_model}
    assert by_model["m1"].cache_hit_rate == round(9 / 10, 6)
    assert by_model["m2"].cache_hit_rate == 0.0


def test_top_session_reported_cost_passthrough():
    period = _period()
    u = make_usage("r", [make_step("r", period.start, cost=0.5)])
    u.reported_cost_usd_lifetime = 0.55
    summary = aggregate([u], period=period, generated_at=period.end)
    assert summary.top_sessions_by_cost[0].reported_cost_usd_lifetime == 0.55


# ============================================================ v5.28 (K6)


def test_cost_outliers_computed_small_sample_mad():
    """K6: 6 racines (5-14) → état computed:small-sample avec MAD sur log-cost."""
    period = _period()
    usages = [
        make_usage(
            f"s{i:02d}", [make_step(f"s{i:02d}", period.start, cost=10.0 if i == 0 else 0.1)]
        )
        for i in range(6)
    ]
    summary = aggregate(
        usages,
        period=period,
        generated_at=period.end,
        session_outlier_z=2.5,
        session_outlier_min_cost_usd=0.5,
    )
    assert summary.cost_outliers_state == "computed:small-sample"
    assert [o.session_id for o in summary.cost_outliers] == ["s00"]
    assert not any("sample trop petit" in w.message for w in summary.warnings)


# ============================================================ v5.30 (A/B — perf + filtre compaction)


def test_prompt_repeats_filters_compaction_artifacts():
    from weekly_telemetry_aggregator.aggregator import _is_compaction_artifact

    assert _is_compaction_artifact("▣ dcp | -374.5k removed, +4.2k summary │██████")
    assert _is_compaction_artifact("dcp | -1209.7k removed, +3.5k summary")
    assert not _is_compaction_artifact("review the current code changes for over-engineering")
    assert not _is_compaction_artifact("voici mes remarques suite aux premiers tests")


def test_prompt_repeats_large_volume_is_fast():
    import time

    from helpers import make_usage

    from weekly_telemetry_aggregator.aggregator import _prompt_repeat_groups

    # 2000 prompts : ~200 uniques répétés + variations de longueur
    usages = []
    for i in range(20):
        turns = []
        for j in range(100):
            turns.append(
                f"prompt numéro {j} pour la tâche {i} avec un contenu suffisamment long pour dépasser le seuil minimum de caractères requis par la détection"
            )
        usages.append(make_usage(f"u{i:03d}", [], user_turns=turns))
    t0 = time.monotonic()
    out = _prompt_repeat_groups(usages, repeat_min=3, similarity=0.9, min_chars=80)
    dt = time.monotonic() - t0
    assert dt < 5.0, f"trop lent: {dt:.1f}s"
    # les répétitions exactes par tâche sont détectées (100 × même prompt)
    assert out and out[0].count >= 100


def test_command_name_ignores_absolute_paths():
    from weekly_telemetry_aggregator.aggregator import _command_name

    assert _command_name("/home/benjamin/.jdks/corretto-25.0.3/bin/java") is None
    assert _command_name("//") is None
    assert _command_name("/tmp/foo.sh") is None
    assert _command_name("/optimize") == "optimize"
    assert _command_name("/optimize avec des args") == "optimize"


def test_prompt_repeats_excludes_child_sessions():
    from helpers import make_usage

    from weekly_telemetry_aggregator.aggregator import _prompt_repeat_groups

    prompt = "prompt long répété utilisateur qui dépasse largement le seuil minimum de caractères requis par la détection des répétitions"
    parent = make_usage("root", [], user_turns=[prompt])
    child = make_usage("child", [], parent="root", user_turns=[prompt])
    out = _prompt_repeat_groups([parent, child], repeat_min=2, similarity=0.9, min_chars=80)
    # la répétition vient du parent seul → count 1 < 2 → rien
    assert out == []
    # avec deux tours parent : détecté
    parent2 = make_usage("root2", [], user_turns=[prompt, prompt])
    out2 = _prompt_repeat_groups([parent2], repeat_min=2, similarity=0.9, min_chars=80)
    assert out2 and out2[0].count == 2


def test_skills_targets_propagated_to_summary():
    from helpers import make_step, make_usage

    from weekly_telemetry_aggregator.aggregator import aggregate
    from weekly_telemetry_aggregator.models import SkillCatalogEntry

    period = __import__("weekly_telemetry_aggregator.models", fromlist=["Period"]).Period(
        start=tzutc(2026, 8, 5), end=tzutc(2026, 8, 12)
    )
    u = make_usage("r", [make_step("r", tzutc(2026, 8, 6), cost=0.1)])
    entries = [
        SkillCatalogEntry(
            name="jira-to-code-audit",
            description="d",
            body="b",
            target_agents=["java-pro", "backend-architect"],
        ),
        SkillCatalogEntry(name="generic", description="d", body="b"),
    ]
    summary = aggregate(
        [u],
        period=period,
        generated_at=period.end,
        skill_catalog=["jira-to-code-audit", "generic"],
        skill_catalog_entries=entries,
    )
    assert summary.skills_targets == {"jira-to-code-audit": ["java-pro", "backend-architect"]}
