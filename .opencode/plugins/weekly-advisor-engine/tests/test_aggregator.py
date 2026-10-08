"""Unit tests for the pure aggregation logic (Part 1 §4) — v5.27 models/schema."""

from __future__ import annotations

from datetime import timedelta

from helpers import make_step, make_usage, tzutc

from weekly_telemetry_aggregator.aggregator import (
    OUTLIER_MIN_ROOTS,
    _cap_warnings,
    aggregate,
    dedup_resumed_usages,
    resume_fingerprint,
)
from weekly_telemetry_aggregator.models import (
    MAX_WARNING_SESSION_SAMPLE,
    MAX_WARNINGS,
    Period,
    SkillCatalogEntry,
    WarningEntry,
)


def _period():
    return Period(start=tzutc(2026, 8, 5, 0, 0), end=tzutc(2026, 8, 12, 0, 0))


def _insights_cfg(**over):
    from dataclasses import replace

    from weekly_telemetry_aggregator.config import InsightsConfig

    return replace(InsightsConfig(), **over)


def test_review_unmeasurable_warning_aggregated_per_harness():
    """Dé-bruitage P6.3 : un seul warning par harnais, pas un par session."""
    period = _period()
    usages = []
    for i in range(4):
        u = make_usage(
            f"ses_oc_{i}", [make_step(f"ses_oc_{i}", period.start, cost=0.1)], tools={"edit": 1}
        )
        u.harness = "opencode"
        usages.append(u)
    for i in range(2):
        u = make_usage(
            f"ses_cc_{i}", [make_step(f"ses_cc_{i}", period.start, cost=0.1)], tools={"write": 1}
        )
        u.harness = "claude-code"
        usages.append(u)

    summary = aggregate(usages, period=period, generated_at=period.end)

    review_warnings = [w for w in summary.warnings if w.message.startswith("review-unmeasurable")]
    assert len(review_warnings) == 2
    assert [w.message for w in review_warnings] == [
        "review-unmeasurable:claude-code (2 sessions)",
        "review-unmeasurable:opencode (4 sessions)",
    ]


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
        skills={"demo-skill": 2},
    )
    summary = aggregate(
        [root],
        period=period,
        generated_at=period.end,
        skill_catalog=["demo-skill", "unused-skill"],
    )
    assert summary.skill_usage[0].skill == "demo-skill"
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
            name="gamma-helper", description="construit un graphe de connaissance", body="graph"
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


def test_aggregate_merges_child_across_canonical_ids():
    """Ids canoniques multi-harnais : fusion racine/enfant inchangée."""
    from helpers import make_step, make_usage, tzutc

    ts = tzutc(2026, 8, 11, 22)
    root = make_usage("alpha:root", [make_step("alpha:root", ts, cost=1.0)])
    child = make_usage("alpha:child", [make_step("alpha:child", ts, cost=0.5)], parent="alpha:root")
    summary = aggregate(
        [root, child],
        period=_period(),
        generated_at=_period().end,
        known_parent_ids={"alpha:root"},
        include_subagents=True,
    )
    assert summary.totals.session_count == 1
    assert abs(summary.totals.total_cost_usd - 1.5) < 1e-9
    top = {t.session_id: t for t in summary.top_sessions_by_cost}
    assert top["alpha:root"].includes_subagents is True


# --------------------------------------------------------------------------- resume dedup


def _turns(n: int, seed: str = "t") -> list[str]:
    return [f"{seed}-{i}" for i in range(n)]


def test_resume_fingerprint_none_below_threshold():
    from weekly_telemetry_aggregator.aggregator import RESUME_FINGERPRINT_TURNS

    short = make_usage("ses_a", [], user_turns=_turns(RESUME_FINGERPRINT_TURNS - 1))
    assert resume_fingerprint(short) is None


def test_dedup_resumed_keeps_richest_and_drops_copy():
    """Fork : la copie reprise partage les premiers turns → on ne garde que la plus riche."""
    base = _turns(10)
    original = make_usage("ses_old", [], user_turns=base)
    resumed = make_usage(
        "ses_new", [], user_turns=[*base, "suite-10", "suite-11"], title="continued"
    )
    kept, records = dedup_resumed_usages([original, resumed])
    assert [u.session_id for u in kept] == ["ses_new"]  # la plus complète gagne
    assert records == [{"kept_session_id": "ses_new", "dropped_session_id": "ses_old"}]


def test_dedup_resumed_tie_breaks_on_smallest_session_id():
    turns = _turns(8)
    a = make_usage("ses_bbb", [], user_turns=turns)
    b = make_usage("ses_aaa", [], user_turns=turns)
    kept, records = dedup_resumed_usages([a, b])
    assert [u.session_id for u in kept] == ["ses_aaa"]
    assert records == [{"kept_session_id": "ses_aaa", "dropped_session_id": "ses_bbb"}]


def test_dedup_resumed_ignores_identical_first_prompt_only():
    """Invocations distinctes d'une même commande : seul le turn 1 coïncide → rien à fusionner."""
    a = make_usage("ses_cmd1", [], user_turns=[*["prompt commun"], *_turns(9, seed="a")])
    b = make_usage("ses_cmd2", [], user_turns=[*["prompt commun"], *_turns(9, seed="b")])
    kept, records = dedup_resumed_usages([a, b])
    assert {u.session_id for u in kept} == {"ses_cmd1", "ses_cmd2"}
    assert records == []


def test_dedup_resumed_skips_short_sessions():
    """Moins de RESUME_FINGERPRINT_TURNS turns → pas de dédup (risque de faux positifs)."""
    a = make_usage("ses_s1", [], user_turns=_turns(3))
    b = make_usage("ses_s2", [], user_turns=_turns(3))
    kept, records = dedup_resumed_usages([a, b])
    assert {u.session_id for u in kept} == {"ses_s1", "ses_s2"}
    assert records == []


def test_dedup_resumed_scoped_by_harness_and_project():
    """Même contenu mais harnais/projet différents → sessions distinctes."""
    turns = _turns(8)
    a = make_usage("ses_x", [], user_turns=turns)
    b = make_usage("ses_y", [], user_turns=turns)
    b.harness = "claude"
    b.project_path = "/other/project"
    kept, records = dedup_resumed_usages([a, b])
    assert {u.session_id for u in kept} == {"ses_x", "ses_y"}
    assert records == []


def test_dedup_resumed_chain_keeps_single_survivor():
    """Chaîne de reprises A ⊂ B ⊂ C → un seul survivant, 2 enregistrements de fusion."""
    base = _turns(12)
    a = make_usage("ses_a", [], user_turns=base[:8])
    b = make_usage("ses_b", [], user_turns=base[:10])
    c = make_usage("ses_c", [], user_turns=base)
    kept, records = dedup_resumed_usages([c, a, b])
    assert [u.session_id for u in kept] == ["ses_c"]
    assert {r["dropped_session_id"] for r in records} == {"ses_a", "ses_b"}
    assert all(r["kept_session_id"] == "ses_c" for r in records)


def test_aggregate_preserves_deterministic_tool_fingerprints():
    usage = make_usage("r", [], tools={"task": 3})
    usage.tool_arg_fingerprints = {"task": {"b": 1, "a": 2}}
    usage.tool_result_fingerprints = {"task": {"same": 3}}
    summary = aggregate([usage], period=_period(), generated_at=_period().end)
    assert summary.tool_argument_fingerprints == {
        "task": {"a": {"total": 2, "peak": 2}, "b": {"total": 1, "peak": 1}}
    }
    assert summary.tool_result_fingerprints == {"task": {"same": 3}}


def test_aggregate_tool_fingerprint_peak_is_the_busiest_single_session():
    """`peak` = max des `count` par session, `total` = somme sur la fenêtre.

    Une boucle d'agent et un outil cron appelé une fois par run donnent le MÊME
    total ; seul le pic les sépare (`total == peak` contre `total = N, peak = 1`).
    """
    cron = make_usage("ses_cron_1", [], tools={"weekly_run": 1})
    cron.tool_arg_fingerprints = {"weekly_run": {"same-args": 1}}
    loop = make_usage("ses_loop", [], tools={"read": 9})
    loop.tool_arg_fingerprints = {"read": {"same-args": 9}}

    summary = aggregate([cron, loop], period=_period(), generated_at=_period().end)
    assert summary.tool_argument_fingerprints == {
        "read": {"same-args": {"total": 9, "peak": 9}},
        "weekly_run": {"same-args": {"total": 1, "peak": 1}},
    }

    # same window total, two very different shapes
    two_cron = make_usage("ses_cron_2", [], tools={"weekly_run": 1})
    two_cron.tool_arg_fingerprints = {"weekly_run": {"same-args": 1}}
    summary = aggregate([cron, two_cron, loop], period=_period(), generated_at=_period().end)
    assert summary.tool_argument_fingerprints["weekly_run"] == {
        "same-args": {"total": 2, "peak": 1}
    }
    assert summary.tool_argument_fingerprints["read"] == {"same-args": {"total": 9, "peak": 9}}


def test_aggregate_tool_fingerprint_peak_of_a_single_session_equals_its_count():
    """Une seule session dans la fenêtre : `peak == total`, jamais une division."""
    usage = make_usage("solo", [], tools={"grep": 4})
    usage.tool_arg_fingerprints = {"grep": {"fp": 4}}

    summary = aggregate([usage], period=_period(), generated_at=_period().end)
    assert summary.tool_argument_fingerprints == {"grep": {"fp": {"total": 4, "peak": 4}}}


def test_by_harness_sums_match_totals():
    """Σ by_harness (sessions/tokens/cost) == totaux du résumé."""
    period = _period()
    a = make_usage("a", [make_step("a", period.start, cost=1.0)])
    a.harness = "opencode"
    b = make_usage("b", [make_step("b", period.start, cost=2.0)])
    b.harness = "claude-code"
    c = make_usage("c", [make_step("c", period.start, cost=3.0)])
    c.harness = "opencode"
    summary = aggregate([a, b, c], period=period, generated_at=period.end)
    by_h = {h.harness: h for h in summary.by_harness}
    assert set(by_h) == {"opencode", "claude-code"}
    assert sum(h.session_count for h in summary.by_harness) == summary.totals.session_count
    assert sum(h.total_tokens for h in summary.by_harness) == summary.totals.total_tokens
    assert round(sum(h.total_cost_usd for h in summary.by_harness), 6) == round(
        summary.totals.total_cost_usd, 6
    )
    assert by_h["opencode"].session_count == 2
    assert by_h["claude-code"].session_count == 1
    assert by_h["opencode"].total_cost_usd == 4.0


def test_top_session_inherits_usage_harness():
    """Le harness du SessionUsage (ou du préfixe canonique) propage vers TopSession."""
    period = _period()
    a = make_usage("opencode:ses_a", [make_step("opencode:ses_a", period.start, cost=1.0)])
    a.harness = "opencode"
    b = make_usage("claude-code:ses_b", [make_step("claude-code:ses_b", period.start, cost=2.0)])
    # b.harness vide → repli sur le préfixe canonique de session_id
    summary = aggregate([a, b], period=period, generated_at=period.end, top_sessions_limit=2)
    by_id = {s.session_id: s for s in summary.top_sessions_by_cost}
    assert by_id["opencode:ses_a"].harness == "opencode"
    assert by_id["claude-code:ses_b"].harness == "claude-code"
    assert {s.harness for s in summary.all_sessions} == {"opencode", "claude-code"}


def test_all_sessions_sorted_by_cost_desc_then_id():
    """all_sessions = racines comptées triées (-cost_usd, session_id)."""
    period = _period()
    a = make_usage("a", [make_step("a", period.start, cost=2.0)])
    b = make_usage("b", [make_step("b", period.start, cost=3.0)])
    c = make_usage("c", [make_step("c", period.start, cost=2.0)])
    summary = aggregate([a, b, c], period=period, generated_at=period.end, top_sessions_limit=1)
    assert [s.session_id for s in summary.all_sessions] == ["b", "a", "c"]
    assert [s.session_id for s in summary.top_sessions_by_cost] == ["b"]  # top N restreint
    assert len(summary.all_sessions) == summary.totals.session_count == 3


def test_is_noise_separators_system_and_bounds():
    from weekly_telemetry_aggregator.aggregator import is_noise

    assert is_noise("═" * 20)
    assert is_noise("résultat\n" + "=" * 12)
    assert is_noise("system: tu es un assistant")
    assert is_noise("x" * 2001)
    assert not is_noise("corrige le bug de parsing du fichier de config")


def test_is_noise_control_turns_and_yesno():
    from weekly_telemetry_aggregator.aggregator import is_noise

    for control in ("continue", "try again", "yes", "no", "cancel", "abort", "stop", "retry"):
        assert is_noise(control), control
    assert is_noise("continue to iterate")
    assert is_noise("y")
    assert is_noise("n!")
    assert is_noise("?!")
    assert not is_noise("continue la refonte du module de facturation")


def test_normalize_fingerprint_collapses_values_and_order():
    from weekly_telemetry_aggregator.aggregator import normalize_fingerprint

    a = normalize_fingerprint("Refactor le module 12 et le module 34 s'il te plaît")
    b = normalize_fingerprint("Refactor le module 87 et le module 5 s'il te plaît")
    assert a == b and a

    assert normalize_fingerprint("alpha beta gamma delta") == normalize_fingerprint(
        "delta gamma beta alpha"
    )


def test_normalize_fingerprint_code_strings_paths_and_empty():
    from weekly_telemetry_aggregator.aggregator import normalize_fingerprint

    assert normalize_fingerprint("```py\nprint(1)\n```") == normalize_fingerprint(
        "```js\nconsole.log(2)\n```"
    )
    assert normalize_fingerprint('config "alpha" ici') == normalize_fingerprint('config "beta" ici')
    assert normalize_fingerprint("ouvre /home/benjamin/dev/projet") == normalize_fingerprint(
        "ouvre /tmp/autre/chemin"
    )
    assert normalize_fingerprint("!!! ??? ...") == ""
    assert normalize_fingerprint("") == ""


def test_prompt_repeat_groups_by_fingerprint():
    from helpers import make_usage

    from weekly_telemetry_aggregator.aggregator import _prompt_repeat_groups

    turns = [
        "Analyse le rapport numéro 12 et propose des actions concrètes pour le sprint en cours",
        "Analyse le rapport numéro 34 et propose des actions concrètes pour le sprint en cours",
        "Analyse le rapport numéro 78 et propose des actions concrètes pour le sprint en cours",
    ]
    usage = make_usage("s1", [], user_turns=turns)
    out = _prompt_repeat_groups([usage], repeat_min=3, similarity=0.9, min_chars=20)
    assert len(out) == 1
    assert out[0].count == 3
    assert out[0].sessions_distinct == 1
    assert out[0].estimated_time_saved_mins == 6


def test_prompt_repeat_groups_cap_and_sort_order():
    from helpers import make_usage

    from weekly_telemetry_aggregator.aggregator import _prompt_repeat_groups

    usages = []
    for i in range(25):
        prompt = (
            f"Tâche récurrente sujet{chr(97 + i)} à automatiser avec suffisamment de contenu "
            "pour dépasser le seuil minimum de caractères de détection"
        )
        usages.append(make_usage(f"s{i:02d}", [], user_turns=[prompt, prompt, prompt]))
    out = _prompt_repeat_groups(usages, repeat_min=3, similarity=0.9, min_chars=40)
    assert len(out) == 20
    keys = [(r.count, r.session_id) for r in out]
    assert keys == sorted(keys, key=lambda k: (-k[0], k[1]))


def test_prompt_repeat_groups_excludes_compaction_artifacts():
    from helpers import make_usage

    from weekly_telemetry_aggregator.aggregator import _prompt_repeat_groups

    art = "▣ dcp | -1209.7k removed, +3.5k summary │█░░"
    usage = make_usage("s1", [], user_turns=[art, art, art, art])
    out = _prompt_repeat_groups([usage], repeat_min=3, similarity=0.9, min_chars=1)
    assert out == []


def test_user_prompt_repeat_enriched_fields():
    from helpers import make_usage

    from weekly_telemetry_aggregator.aggregator import _prompt_repeat_groups

    prompt = "Optimise le pipeline de collecte avec un contenu assez long pour dépasser le seuil"
    a = make_usage("s1", [], user_turns=[prompt, prompt])
    a.harness = "opencode"
    b = make_usage("s2", [], user_turns=[prompt])
    b.harness = "claude"
    out = _prompt_repeat_groups([a, b], repeat_min=3, similarity=0.9, min_chars=20)
    assert len(out) == 1
    rep = out[0]
    assert rep.count == 3
    assert rep.sessions_distinct == 2
    assert rep.harnesses_distinct == 2
    assert rep.cancel_rate == 0.0
    assert rep.estimated_time_saved_mins == 6
    assert rep.examples == [prompt]
    assert "# Skill" in rep.skill_draft
    assert "## When to use" in rep.skill_draft
    assert "## Steps" in rep.skill_draft
    assert "## Example prompts" in rep.skill_draft


# --- B2 / B3 / B4 / B5 — filtres boilerplate et narration d'outil ------------


def test_normalize_fingerprint_drops_tool_narration():
    """B4 : la narration d'outillage ne produit AUCUNE empreinte.

    Les 4 premiers tokens TRIÉS de « Called the Read tool with the following
    input: … » sont identiques pour tous les outils d'une langue — le mégabucket
    `called|following|read|tool` (× 7 sur le run 2026-10-03) absorbait le budget de
    findings sans signaler d'anomalie.
    """
    from weekly_telemetry_aggregator.aggregator import normalize_fingerprint

    narration = 'Called the Read tool with the following input: {"filePath":"/home/benjamin/a"}'
    assert normalize_fingerprint(narration) == ""
    # même narration, autre outil → toujours aucune empreinte (pas de forme figée)
    for other in (
        "Called the Glob tool with the following input: {}",
        "Running the Bash tool with the following input: {}",
        'Invoking the Edit tool with the following input: {"path":"a"}',
    ):
        assert normalize_fingerprint(other) == "", other
    # une intention réelle qui contient les mêmes mots reste fingerprintée
    assert normalize_fingerprint(
        "le tool called following read needs a schema change in the parser"
    )


def test_normalize_fingerprint_drops_degenerate_single_token():
    """B4 : sous 2 tokens normalisés, l'empreinte ne distingue plus rien.

    Sur le run 2026-10-03 ces buckets portaient `str` (× 8) et `es` (× 2). Seuil à 2
    et non 3 : « améliore ce script » (2 tokens) est une intention répétée légitime.
    """
    from weekly_telemetry_aggregator.aggregator import normalize_fingerprint

    assert normalize_fingerprint("```py\nprint(1)\n```") == ""  # 1 token : `code`
    assert normalize_fingerprint("/home/benjamin/dev/projet") == ""  # 1 token : `path`
    assert normalize_fingerprint("améliore ce script") == "améliore|script"


def test_prompt_repeats_ignores_tool_narration_megabucket():
    """B4 bout-en-bout : 30 narrations ne produisent aucun groupe de répétition."""
    from helpers import make_usage

    from weekly_telemetry_aggregator.aggregator import _prompt_repeat_groups

    narration = 'Called the Read tool with the following input: {"filePath":"/home/benjamin/%d"}'
    usages = [make_usage(f"s{i}", [], user_turns=[narration % i] * 3) for i in range(10)]
    assert _prompt_repeat_groups(usages, repeat_min=3, similarity=0.9, min_chars=20) == []


def test_prompt_repeats_dominant_repeat_still_reported():
    """B4.2 — refus argumenté : le seuil de support RELATIF au corpus est un anti-Goal.

    Le plan demandait « un bucket ne compte que s'il est anormal pour CE corpus ». Sur
    ce corpus, 100 prompts identiques = 100 % du total : un plafond de part (ou un
    plancher proportionnel) tuerait la tête de la métrique. Ce test verrouille le
    comportement voulu — la nature du tour discrimine, pas son poids.
    """
    from helpers import make_usage

    from weekly_telemetry_aggregator.aggregator import _prompt_repeat_groups

    dominant = "Tâche récurrente à automatiser avec suffisamment de contenu pour dépasser le seuil"
    usages = [make_usage(f"s{i:02d}", [], user_turns=[dominant] * 100) for i in range(20)]
    out = _prompt_repeat_groups(usages, repeat_min=3, similarity=0.9, min_chars=40)
    assert len(out) == 1
    assert out[0].count == 2000


def test_command_name_strips_client_quoting():
    """B5.1 : le client sérialise l'invocation, la stored turn est quotée.

    Sur le run 2026-10-03 la seule slash-command du corpus est stockée
    `"/swarmx test: …"` : sans retrait des guillemets, `command_usage` restait vide
    alors que le corpus en contient.
    """
    from weekly_telemetry_aggregator.aggregator import _command_name

    assert _command_name("\"/swarmx test: réponds JUSTE 'ok'\"") == "swarmx"
    assert _command_name("'/caveman ultra'") == "caveman"
    assert _command_name("/optimize") == "optimize"


def test_command_name_ignores_tool_narration_with_slash_payload():
    """B5.1 : la narration ne devient jamais une commande, même chargée d'un chemin."""
    from weekly_telemetry_aggregator.aggregator import _command_name, is_tool_narration

    narration = 'Called the Read tool with the following input: {"filePath":"/home/benjamin/a"}'
    assert is_tool_narration(narration)
    assert _command_name(narration) is None
    assert not is_tool_narration("/optimize le rapport")
    assert not is_tool_narration("utilise opencode-browser pour recharger la page")


def test_command_usage_state_distinguishes_no_data_from_measured_empty():
    """B5.2 : « aucune commande slash » ≠ « commandes slash non mesurées »."""
    from helpers import make_usage

    from weekly_telemetry_aggregator.aggregator import COMMAND_USAGE_NO_DATA, command_usage_state

    # aucun tour du tout → rien n'a été mesuré
    assert command_usage_state([make_usage("s0", [], user_turns=[])]) == COMMAND_USAGE_NO_DATA
    # des tours, mais aucun slash-command → mesuré, et le compte 0 est vrai
    assert (
        command_usage_state([make_usage("s1", [], user_turns=["bonjour", "continue"])])
        == "computed"
    )
    # une narration seule ne prouve pas que le canal est fonctionnel
    narration = 'Called the Read tool with the following input: {"filePath":"/a"}'
    assert (
        command_usage_state([make_usage("s2", [], user_turns=[narration] * 3)])
        == COMMAND_USAGE_NO_DATA
    )
    # une vraie commande → mesuré
    assert command_usage_state([make_usage("s3", [], user_turns=["/optimize le rapport"])]) == (
        "computed"
    )


def test_agent_loop_distribution_unchanged_by_result_channel_boilerplate():
    """B2 — HONNÊTETÉ : le filtre du canal RÉSULTAT ne change AUCUN finding.

    La règle `agent-loop` (`rules/agent-loop.md`, plus le détecteur Python
    `_agent_loop_findings` qui a été supprimé) lit `tool_argument_loops`, que
    `rule_context` construit depuis `tool_argument_fingerprints` seul. Le filtre de
    boilerplate agit sur `tool_result_fingerprints`, canal que plus aucun scope ne
    consomme. Preuve arithmétique sur la distribution de référence du run 2026-10-03
    (§3.2) : 344 buckets d'arguments pour `edit` avec max 1, et un bucket RÉSULTAT
    unique de cardinal 341 → à seuil 8, exactement 2 outils : `read` 11, `skill` 22.
    """
    from weekly_telemetry_aggregator.insights import _production_rule_results

    edit_arg_buckets = {f"edit-fp-{i}": 1 for i in range(344)}  # 344 appels, 344 empreintes
    summary = {
        "tool_argument_fingerprints": {
            "edit": edit_arg_buckets,
            "write": {f"write-fp-{i}": 1 for i in range(76)},
            "read": {f"read-fp-{i}": 1 for i in range(1078)} | {"read-fp-repeated": 11},
            "skill": {f"skill-fp-{i}": 1 for i in range(36)} | {"skill-fp-reloaded": 22},
            "glob": {f"glob-fp-{i}": 1 for i in range(140)},
            "bash": {f"bash-fp-{i}": 1 for i in range(1722)},
            "apply_patch": {f"patch-fp-{i}": 1 for i in range(36)},
        },
        # le canal que B2 vide : une constante par outil, cardinal = nombre d'appels
        "tool_result_fingerprints": {
            "edit": {"constant": 341},
            "write": {"constant": 76},
            "glob": {"constant": 76},
            "grep": {"constant": 31},
        },
    }
    _, findings, _ = _production_rule_results(
        current_summary=summary,
        previous_summary=None,
        recent_summaries=[summary],
        insights_cfg=_insights_cfg(),
        ignored_findings=[],
        loop_min_repeats=8,
        loop_task_min_repeats=8,
    )
    assert len(findings) == 1
    loop = findings[0]
    assert loop["category"] == "agent-loop"
    # les 2 outils qui dépassent le seuil 8, et eux seuls. Ce résumé porte des
    # buckets ENTIERS (format d'un artifact déjà écrit) : le pic retombe sur le
    # total, donc les deux métriques affichent 22 et le déclenchement est
    # exactement celui d'avant.
    assert loop["evidence_summary"] == (
        "rule=agent-loop; max_peak=22; max_total=22; task_loops=0; examples=read|skill"
    )

    # et le canal RÉSULTAT seul ne produirait RIEN : le test est bien découplé
    emptied = {**summary, "tool_result_fingerprints": {}}
    assert (
        _production_rule_results(
            current_summary=emptied,
            previous_summary=None,
            recent_summaries=[emptied],
            insights_cfg=_insights_cfg(),
            ignored_findings=[],
            loop_min_repeats=8,
            loop_task_min_repeats=8,
        )[1]
        == findings
    )


# ============================================================ D5 — cap de warnings


def test_cap_warnings_groups_identical_messages_before_cap():
    """D5 : 50 warnings identiques → 1 entité `count=50`, pas 50 lignes."""
    repeated = [
        WarningEntry(
            session_id=f"ses_{i:03d}",
            message="session active exclue des totaux (télémétrie incomplète)",
        )
        for i in range(MAX_WARNINGS)
    ]
    other = WarningEntry(session_id="ses_other", message="orphan child session")
    capped = _cap_warnings([*repeated, other])

    assert len(capped) == 2
    active = next(w for w in capped if w.message.startswith("session active exclue"))
    assert active.count == MAX_WARNINGS
    assert active.session_ids == [f"ses_{i:03d}" for i in range(MAX_WARNING_SESSION_SAMPLE)]
    assert other.count == 1


def test_cap_warnings_is_idempotent_on_already_grouped_entries():
    """D5 : le ré-cap de main.py ne doit pas effacer le multiplicateur."""
    grouped = _cap_warnings(
        [
            WarningEntry(session_id=f"ses_{i}", message="session read failed: boom", partial=True)
            for i in range(4)
        ]
    )
    assert grouped[0].count == 4
    assert grouped[0].partial is True

    again = _cap_warnings([*grouped, WarningEntry(message="orphan child session")])
    assert next(w for w in again if w.message.startswith("session read failed")).count == 4


def test_cap_warnings_caps_distinct_entities_and_keeps_structured_payloads():
    """D5 : MAX_WARNINGS plafonne les messages distincts ; K7 n'est pas regroupé."""
    distinct = [WarningEntry(session_id=f"ses_{i}", message=f"message {i}") for i in range(80)]
    # Deux écarts cross-check de sessions différentes, même arithmétique → même
    # message, mais payload chiffré par session : insights les consomme 1:1.
    structured = [
        WarningEntry(
            session_id=f"ses_x{i}",
            message="cross-check mismatch: parts cost $0.5081 vs session_v2 $0.3663",
            parts_cost=0.5081,
            session_v2_cost=0.3663,
        )
        for i in range(2)
    ]
    capped = _cap_warnings([*distinct, *structured])

    assert len(capped) == MAX_WARNINGS
    # 2 lignes K7 non regroupées (même message, payload par session) + 48 entités
    # informationnelles distinctes.
    assert [w.count for w in capped if w.parts_cost is not None] == [1, 1]
    assert len({w.message for w in capped if w.parts_cost is None}) == MAX_WARNINGS - 2


# ============ D1 — tests de caractérisation (filet de sécurité des commits D2-D4)
#
# 30 des 41 defs de niveau supérieur de `aggregator.py` ne sont nommés par AUCUN test
# existant. Les blocs ci-dessous caractérisent le COMPORTEMENT OBSERVÉ avant tout
# déplacement de code : les régions qu'ils couvrent (text/noise, repeats, outliers,
# usage builders) sont exactement celles qui quittent `aggregator/__init__.py` en D2/D3/D4.

# --- text / noise : normalisations, narration, compaction, empreintes ---------

# Famille de prompts prouvée de même empreinte par
# `test_prompt_repeat_groups_by_fingerprint` : le nombre est normalisé en `num`,
# donc « 12 » / « 34 » / « 78 » partagent les 4 premiers tokens de contenu.
_FAMILY_A = "Analyse le rapport numéro 12 et propose des actions concrètes pour le sprint en cours"
_FAMILY_A_34 = (
    "Analyse le rapport numéro 34 et propose des actions concrètes pour le sprint en cours"
)
_FAMILY_A_78 = (
    "Analyse le rapport numéro 78 et propose des actions concrètes pour le sprint en cours"
)
# Variantes « correction » : le préfixe `no ` occupe le 1er token de contenu, donc
# l'empreinte reste celle de la famille (tri des 4 PREMIERS tokens).
_FAMILY_CORR_12 = (
    "no analyse le rapport numéro 12 et propose des actions concrètes pour le sprint en cours"
)
_FAMILY_CORR_34 = (
    "no analyse le rapport numéro 34 et propose des actions concrètes pour le sprint en cours"
)
_NARRATION = 'Called the Read tool with the following input: {"filePath":"/home/benjamin/a"}'


def test_normalize_prompt_flattens_lowercases_and_strips_trailing_punctuation():
    from weekly_telemetry_aggregator.aggregator import normalize_prompt

    assert normalize_prompt("  Corrige   le BUG.\n") == "corrige le bug"
    assert normalize_prompt("Déployer MAINTENANT!") == "déployer maintenant"
    # le strip précède le rstrip de ponctuation : l'espace AVANT « ? » reste
    assert normalize_prompt("Quoi ?") == "quoi "
    assert normalize_prompt("Quoi?") == "quoi"
    assert normalize_prompt("ferme le crochet)") == "ferme le crochet"
    # rstrip (pas strip) : la ponctuation INTERNE est conservée
    assert normalize_prompt("a.b, c") == "a.b, c"
    # tous les tokens disparaissent
    assert normalize_prompt("...") == ""
    # `str(text)` : un None devient la chaîne « none », pas une exception
    assert normalize_prompt(None) == "none"
    assert normalize_prompt(42) == "42"


def test_strip_quotes_retires_une_seule_paire_encadrante():
    from weekly_telemetry_aggregator.aggregator import _strip_quotes

    assert _strip_quotes('"/swarmx test: réponds ok"') == "/swarmx test: réponds ok"
    assert _strip_quotes("'/caveman ultra'") == "/caveman ultra"  # le slash reste
    assert _strip_quotes('  "  padded  "  ') == "padded"
    # paires non appariées → texte rendu tel quel
    assert _strip_quotes("\"mixed '") == "\"mixed '"
    assert _strip_quotes('"inner" tail') == '"inner" tail'
    # longueur 1 : jamais de paire
    assert _strip_quotes('"') == '"'
    assert _strip_quotes("") == ""


def test_is_tool_narration_requires_the_verb_tool_frame():
    """Le cadre est `<verbe> [the] <nom> (tool|function)` — pas le simple fait de citer « tool »."""
    from weekly_telemetry_aggregator.aggregator import is_tool_narration

    for verb in ("called", "calling", "running", "executing", "reading", "searching"):
        assert is_tool_narration(f"{verb} the Read tool with the following input"), verb
    assert is_tool_narration("running my-tool function")
    assert is_tool_narration("Invoking the Edit tool")
    # le verbe seul ne suffit pas
    assert not is_tool_narration("reads the file")
    assert not is_tool_narration("called him yesterday")
    assert not is_tool_narration("optimize the tool call budget")
    assert not is_tool_narration("")
    # la guillemetisation du client est retirée AVANT le test (B5.1)
    assert is_tool_narration(f'"{_NARRATION}"')


def test_is_compaction_artifact_marker_rules_are_exact():
    from weekly_telemetry_aggregator.aggregator import _is_compaction_artifact

    # vide ou blancs purs : rien à indexer
    assert _is_compaction_artifact("") is True
    assert _is_compaction_artifact("   \n ") is True
    # marqueurs de bloc de statut, en tête de tour
    for marker in ("▣", "█", "░", "│", "⏿"):
        assert _is_compaction_artifact(f"{marker} dcp | quelque chose") is True, marker
    # la formule « dcp | » exige `removed` ET (`summary` OU `compact`)
    assert _is_compaction_artifact("dcp | -374.5k removed, +4.2k summary") is True
    assert _is_compaction_artifact("DCP | -1k REMOVED and COMPACT") is True
    assert _is_compaction_artifact("dcp | remove the duplicated branch") is False
    assert _is_compaction_artifact("please remove this file") is False
    # un tour humain est un tour humain
    assert _is_compaction_artifact(_FAMILY_A) is False


def test_noise_length_bound_counts_the_raw_prompt_not_the_stripped_one():
    """La borne de 2000 chars porte sur le prompt BRUT (`len(raw)`), pas sur la version strippée."""
    from weekly_telemetry_aggregator.aggregator import is_noise

    assert is_noise("x" * 2001) is True
    assert is_noise("x" * 2000) is False
    # 2000 caractères utiles + 10 de retraitements = au-dessus de la borne
    assert is_noise("  " * 5 + "x" * 2000 + "  " * 5) is True
    # `str(prompt)` : None devient « None », qui n'est pas du bruit
    assert is_noise(None) is False


def test_canonical_prompt_prefers_the_shortest_over_twenty_normalised_chars():
    """Pool = les prompts ≥ 20 chars normalisés s'il y en a ; sinon tous ; tie-break (len, p)."""
    from weekly_telemetry_aggregator.aggregator import _canonical_prompt

    long_prompt = "corrige le parsing du module de facturation"
    # un prompt court est IGNORÉ dès qu'un prompt long existe
    assert _canonical_prompt([long_prompt, "tiny"]) == long_prompt
    # aucun assez long → le plus court, puis l'ordre lexicographique
    assert _canonical_prompt(["tiny", "also tiny"]) == "tiny"
    # égalité de longueur → lexicographique
    assert _canonical_prompt(["bbb prompt", "aaa prompt"]) == "aaa prompt"


def test_cancel_and_correction_turn_predicates_are_exact_and_prefixed():
    from weekly_telemetry_aggregator.aggregator import _is_cancel_turn, _is_correction_turn

    for turn in ("cancel", "abort", "stop", "annule", "abandonne"):
        assert _is_cancel_turn(turn) is True, turn
    # égalité stricte, pas un préfixe
    assert _is_cancel_turn("cancel the migration now") is False
    assert _is_cancel_turn("") is False
    # les préfixes portent leur espace : « no » seul n'est PAS une correction
    for bare in ("no", "non", "pas", "not"):
        assert _is_correction_turn(bare) is False, bare
    for turn in (
        "no ",
        "non ",
        "nope",
        "pas ",
        "not ",
        "actually ",
        "instead ",
        "plutot ",
        "plutôt ",
        "wait ",
    ):
        assert _is_correction_turn(turn) is True, turn
    assert _is_correction_turn("corrige le module") is False
    assert _is_correction_turn("") is False


def test_estimated_tokens_divides_by_four_with_banker_rounding():
    from weekly_telemetry_aggregator.aggregator import _estimated_tokens

    assert _estimated_tokens(0) == 0
    assert _estimated_tokens(3) == 1  # 0.75 -> 1
    assert _estimated_tokens(4) == 1
    assert _estimated_tokens(400) == 100
    assert _estimated_tokens(403) == 101  # 100.75 -> 101
    assert _estimated_tokens(402) == 100  # 100.5 -> 100 (arrondi pair)


def test_cache_hit_rate_ignores_cache_write_and_nulls_on_empty_denominator():
    from weekly_telemetry_aggregator.aggregator import _cache_hit_rate
    from weekly_telemetry_aggregator.models import round6

    # cache_write n'entre PAS au dénominateur : 60/(60+30), pas 60/180
    assert _cache_hit_rate(60, 30) == round(60 / 90, 6)
    assert _cache_hit_rate(0, 0) is None
    # dénominateur nul OU négatif -> None (jamais de division par zéro)
    assert _cache_hit_rate(5, -5) is None
    assert _cache_hit_rate(-1, -1) is None
    # arrondi 6 décimales
    assert _cache_hit_rate(1, 2) == round6(1 / 3)


def test_session_duration_sorts_steps_and_ignores_gaps_over_five_minutes():
    from weekly_telemetry_aggregator.aggregator import session_duration

    p = _period()
    # désordonné à dessein : le calcul trie par timestamp
    usage = make_usage(
        "s",
        [
            make_step("s", p.start + timedelta(seconds=3660)),
            make_step("s", p.start),
            make_step("s", p.start + timedelta(seconds=60)),
        ],
    )
    assert session_duration(usage) == (3660, 60)  # le trou d'1 h ne compte pas
    assert session_duration(make_usage("empty", [])) == (0, 0)
    # un pas unique -> durée nulle, et donc aucun coût par minute active
    assert session_duration(make_usage("one", [make_step("one", p.start)])) == (0, 0)


def test_descendants_is_a_bfs_over_parent_links_in_discovery_order():
    from weekly_telemetry_aggregator.aggregator import _descendants

    root = make_usage("r", [])
    c1 = make_usage("c1", [], parent="r")
    c2 = make_usage("c2", [], parent="r")
    g1 = make_usage("g1", [], parent="c1")  # petit-enfant
    by_id = {u.session_id: u for u in (root, c1, c2, g1)}

    assert [u.session_id for u in _descendants(by_id, root)] == ["c1", "c2", "g1"]
    assert [u.session_id for u in _descendants(by_id, c1)] == ["g1"]
    assert _descendants(by_id, g1) == []


# --- repeats : buckets, accumulation, émission, plafonnement --------------------


def test_new_repeat_bucket_shape_is_zeroed_and_independent():
    from weekly_telemetry_aggregator.aggregator import _new_repeat_bucket

    first = _new_repeat_bucket()
    assert first == {
        "count": 0,
        "chars_sum": 0,
        "prompts": [],
        "sessions": set(),
        "harnesses": set(),
        "cancels": 0,
        "corrections": 0,
        "first": None,
        "last": None,
    }
    second = _new_repeat_bucket()
    first["prompts"].append("x")
    first["sessions"].add("s")
    assert second["prompts"] == [] and second["sessions"] == set()  # pas d'état partagé


def test_accumulate_repeat_turn_skips_unfingerprinted_and_keeps_window_bounds():
    from weekly_telemetry_aggregator.aggregator import _accumulate_repeat_turn

    p = _period()
    usage = make_usage("s1", [])
    usage.harness = "opencode"
    t0 = p.start
    t1 = p.start + timedelta(hours=2)
    groups: dict[str, dict] = {}

    # narration / prompt dégénéré -> AUCUN bucket créé
    for turn in (_NARRATION, "améliore", ""):
        _accumulate_repeat_turn(groups, usage, turn, t0, t1)
        assert groups == {}, turn

    # PAS de filtre de compaction ici : `_accumulate_repeat_turn` n'indexe que ce qui
    # a une empreinte. C'est `_is_repeat_candidate` (appelé par `_accumulate_usage_repeats`)
    # qui écarte les artefacts de compaction. Un appel direct les laisserait passer.
    _accumulate_repeat_turn(groups, usage, "▣ dcp | -1k removed, +2k summary", t0, t1)
    assert list(groups) == ["dcp|num|num|removed"]
    groups = {}

    _accumulate_repeat_turn(groups, usage, _FAMILY_A, t0, t1)
    _accumulate_repeat_turn(groups, usage, _FAMILY_A_34, t1, t0)  # fenêtre inversée
    (fingerprint,) = groups
    bucket = groups[fingerprint]
    assert len(fingerprint.split("|")) == 4  # 4 tokens de contenu, triés
    assert bucket["count"] == 2
    assert bucket["chars_sum"] == len(_FAMILY_A) + len(_FAMILY_A_34)
    assert bucket["prompts"] == [_FAMILY_A, _FAMILY_A_34]
    assert bucket["sessions"] == {"s1"}
    assert bucket["harnesses"] == {"opencode"}
    # la fenêtre inversée ne recule ni n'avance first/last
    assert bucket["first"] == t0
    assert bucket["last"] == t1

    # harness absent + fenêtre absente : rien n'est inventé
    bare: dict[str, dict] = {}
    _accumulate_repeat_turn(bare, make_usage("s2", []), _FAMILY_A, None, None)
    only = next(iter(bare.values()))
    assert only["harnesses"] == set()
    assert only["first"] is None and only["last"] is None


def test_accumulate_repeat_turn_counts_corrections_but_cancels_stay_unreachable():
    """HONNÊTETÉ : `cancels` est structurellement inatteignable, `corrections` non.

    `_CANCEL_TURNS` ne contient que des tours d'un seul mot ; or `normalize_fingerprint`
    exige ≥ 2 tokens de contenu. Un tour d'annulation ne peut donc jamais produire
    d'empreinte, donc jamais de bucket, donc jamais d'incrément de `cancels`
    (`cancel_rate` reste toujours à 0.0). Une tour de correction, elle, peut être
    fingerprintée et est comptée.
    """
    from weekly_telemetry_aggregator.aggregator import _accumulate_repeat_turn, _is_cancel_turn

    p = _period()
    usage = make_usage("s1", [])
    t0, t1 = p.start, p.start + timedelta(days=1)

    # --- cancels : le prédicat fonctionne, la porte d'entrée est fermée
    assert _is_cancel_turn("cancel") is True
    cancels: dict[str, dict] = {}
    for turn in ("cancel", "abort.", "stop!", "abandonne", "annule"):
        _accumulate_repeat_turn(cancels, usage, turn, t0, t1)
    assert cancels == {}

    # --- corrections : comptées
    corrections: dict[str, dict] = {}
    _accumulate_repeat_turn(corrections, usage, _FAMILY_CORR_12, t0, t1)
    _accumulate_repeat_turn(corrections, usage, _FAMILY_CORR_34, t0, t1)
    assert len(corrections) == 1
    bucket = next(iter(corrections.values()))
    assert bucket["corrections"] == 2
    assert bucket["cancels"] == 0


def test_repeat_examples_dedupes_strips_and_respects_the_limit():
    from weekly_telemetry_aggregator.aggregator import _repeat_examples

    assert _repeat_examples(["a", "a", " b ", "", "c", "d", "e"], limit=3) == ["a", "b", "c"]
    assert _repeat_examples([]) == []
    # limite par défaut = 5
    assert _repeat_examples([f"turn {i}" for i in range(10)]) == [f"turn {i}" for i in range(5)]
    # la comparaison porte sur la version STRIPPÉE : « a » duplique « a »
    assert _repeat_examples([" a ", "a", "b"]) == ["a", "b"]


def test_is_repeat_candidate_filters_compaction_noise_and_empty():
    from weekly_telemetry_aggregator.aggregator import _is_repeat_candidate

    assert _is_repeat_candidate(_FAMILY_A) is True
    assert _is_repeat_candidate("") is False  # normalisé vide
    assert _is_repeat_candidate("   \n ") is False
    assert _is_repeat_candidate("continue") is False  # bruit : commande de contrôle
    assert _is_repeat_candidate("▣ dcp | -1k removed, +2k summary") is False  # compaction
    # la narration n'est PAS du bruit : c'est le fingerprint qui la neutralise ensuite
    assert _is_repeat_candidate(_NARRATION) is True


def test_accumulate_usage_repeats_skips_children_and_derives_window_from_steps():
    from weekly_telemetry_aggregator.aggregator import _accumulate_usage_repeats

    p = _period()
    parent = make_usage("p", [], user_turns=[_FAMILY_A])
    child = make_usage("c", [], parent="p", user_turns=[_FAMILY_A])

    child_groups: dict[str, dict] = {}
    _accumulate_usage_repeats(child_groups, child)
    assert child_groups == {}  # v5.30 (7) : les tours des enfants ne sont pas indexés

    parent_groups: dict[str, dict] = {}
    _accumulate_usage_repeats(parent_groups, parent)
    assert sum(b["count"] for b in parent_groups.values()) == 1
    assert next(iter(parent_groups.values()))["first"] is None  # pas de steps -> pas de fenêtre

    stepped = make_usage(
        "p2",
        [make_step("p2", p.start + timedelta(hours=3)), make_step("p2", p.start)],
        user_turns=[_FAMILY_A],
    )
    groups: dict[str, dict] = {}
    _accumulate_usage_repeats(groups, stepped)
    bucket = next(iter(groups.values()))
    assert bucket["first"] == p.start
    assert bucket["last"] == p.start + timedelta(hours=3)


def test_emit_repeat_group_fills_every_field_and_honours_both_thresholds():
    from weekly_telemetry_aggregator.aggregator import (
        _accumulate_repeat_turn,
        _emit_repeat_group,
        _new_repeat_bucket,
    )

    p = _period()
    t0 = p.start
    t1 = p.start + timedelta(days=1)
    groups: dict[str, dict] = {}
    s1 = make_usage("s1", [])
    s1.harness = "opencode"
    s2 = make_usage("s2", [])
    s2.harness = "claude"
    for turn, usage in ((_FAMILY_A, s1), (_FAMILY_A_34, s1), (_FAMILY_A_78, s2)):
        _accumulate_repeat_turn(groups, usage, turn, t0, t1)

    bucket = next(iter(groups.values()))
    out = _emit_repeat_group(bucket, repeat_min=3, min_chars=20)
    assert out is not None
    assert out.count == 3
    assert out.session_id == "s1"  # min des sessions triées
    assert out.sessions_distinct == 2
    assert out.harnesses_distinct == 2
    assert out.avg_chars == round(bucket["chars_sum"] / 3)
    assert out.cancel_rate == 0.0  # voir test_accumulate_repeat_turn_counts_corrections…
    assert out.avg_correction_turns == 0.0
    assert out.first_seen == t0.isoformat()
    assert out.last_seen == t1.isoformat()
    assert out.estimated_time_saved_mins == 6  # count × 2 minutes
    assert out.examples == [_FAMILY_A, _FAMILY_A_34, _FAMILY_A_78]
    # canonique = plus court des prompts ≥ 20 chars normalisés, tie-break lexicographique
    assert out.normalized_preview == _FAMILY_A.lower()[:80]
    assert out.skill_draft.startswith(f"# Skill: {_FAMILY_A.lower()[:80]}")

    # count sous le seuil -> rien
    assert _emit_repeat_group(bucket, repeat_min=4, min_chars=20) is None
    # preview canonique sous le plancher de caractères -> rien
    assert _emit_repeat_group(bucket, repeat_min=3, min_chars=10_000) is None

    # groupe sans session : session_id vide et avg_correction_turns à 0.0 (pas de ZeroDivision)
    orphan = _new_repeat_bucket()
    orphan.update(count=5, chars_sum=100, prompts=[_FAMILY_A] * 5, first=t0, last=t0)
    out_orphan = _emit_repeat_group(orphan, repeat_min=3, min_chars=20)
    assert out_orphan is not None
    assert out_orphan.session_id == ""
    assert out_orphan.sessions_distinct == 0
    assert out_orphan.avg_correction_turns == 0.0


def test_emit_repeat_list_sorts_count_desc_then_session_id_and_caps_at_twenty():
    from weekly_telemetry_aggregator.aggregator import _accumulate_repeat_turn, _emit_repeat_list

    def _family(letter: str) -> str:
        return (
            f"Tâche récurrente sujet{letter} à automatiser avec suffisamment de contenu "
            "pour dépasser le seuil de caractères de détection"
        )

    p = _period()
    t0, t1 = p.start, p.start + timedelta(days=1)
    groups: dict[str, dict] = {}
    for i in range(22):
        usage = make_usage(f"s{i:02d}", [])
        for _ in range(3):
            _accumulate_repeat_turn(groups, usage, _family(chr(97 + i)), t0, t1)
    heavy = make_usage("s99", [])
    for _ in range(5):
        _accumulate_repeat_turn(groups, heavy, _family("z"), t0, t1)
    assert len(groups) == 23

    out = _emit_repeat_list(groups, repeat_min=3, min_chars=40)
    assert len(out) == 20  # PROMPT_REPEATS_CAP
    # count DESC d'abord (s99 en 5), puis session_id ASC ; les 2 derniers sXX tombent
    assert [r.session_id for r in out] == ["s99"] + [f"s{i:02d}" for i in range(19)]
    assert [r.count for r in out] == [5] + [3] * 19
    # un seuil de répétition plus haut vide la liste
    assert _emit_repeat_list(groups, repeat_min=6, min_chars=40) == []


def test_build_skill_draft_is_the_documented_markdown_skeleton():
    from weekly_telemetry_aggregator.aggregator import _build_skill_draft

    draft = _build_skill_draft("Optimise le pipeline", 4, 2, ["ex1", "ex2", "ex3", "ex4"])
    assert draft.splitlines() == [
        "# Skill: Optimise le pipeline",
        "",
        "## When to use",
        "Réponse répétée 4× sur 2 session(s) — automatiser ce flux.",
        "",
        "## Steps",
        "1. Reproduire le flux récurrent et figer ses entrées/sorties.",
        "2. Encapsuler la procédure dans un skill portable.",
        "3. Valider sur un cas réel avant généralisation.",
        "",
        "## Example prompts",
        "- ex1",
        "- ex2",
        "- ex3",
    ]
    # 3 exemples au plus, chacun tronqué à 120 caractères
    lines = _build_skill_draft("L", 1, 1, ["x" * 200, "y", "z", "w"]).splitlines()
    assert lines[-3:] == [f"- {'x' * 120}", "- y", "- z"]


# --- outliers : paires de skills, z-scores robustes, dédup de reprise -----------


def test_skill_similar_pairs_compares_window_and_caps_at_five():
    from weekly_telemetry_aggregator.aggregator import SKILL_PAIRS_CAP, _skill_similar_pairs

    assert SKILL_PAIRS_CAP == 5
    entries = [
        SkillCatalogEntry(name="alpha", description="corrige le parsing", body="corps alpha"),
        SkillCatalogEntry(name="beta", description="corrige le parsing", body="corps alpha"),
        SkillCatalogEntry(
            name="gamma", description="publie le rapport de charge hebdomadaire", body="autre"
        ),
    ]
    pairs = _skill_similar_pairs(entries, 0.9)
    assert [p.skills for p in pairs] == [["alpha", "beta"]]
    assert pairs[0].similarity == 1.0
    # sous le seuil de similarité -> aucune paire
    assert _skill_similar_pairs(entries, 1.01) == []

    # C(5,2) = 10 paires à 1.0 -> plafonnées à 5, tri (similarity DESC, noms)
    twins = [
        SkillCatalogEntry(name=f"s{i}", description="meme texte", body="corps identique")
        for i in range(5)
    ]
    capped = _skill_similar_pairs(twins, 0.9)
    assert len(capped) == SKILL_PAIRS_CAP
    assert [p.skills for p in capped] == [
        ["s0", "s1"],
        ["s0", "s2"],
        ["s0", "s3"],
        ["s0", "s4"],
        ["s1", "s2"],
    ]
    # moins de deux skills -> aucune paire (pas de ValueError sur range(-1))
    assert _skill_similar_pairs(twins[:1], 0.0) == []


def test_robust_z_is_zeroed_on_a_constant_series_and_empty_input():
    from weekly_telemetry_aggregator.aggregator import _robust_z

    assert _robust_z([]) == {}
    assert _robust_z([1.0, 1.0, 1.0, 1.0]) == {"0": 0.0, "1": 0.0, "2": 0.0, "3": 0.0}
    # MAD nul -> repli déviation moyenne absolue : un pic unique reste détectable
    spike = _robust_z([1.0, 1.0, 1.0, 1.0, 9.0])
    assert spike["4"] > 0
    assert all(spike[str(i)] == 0.0 for i in range(4))
    # les clés sont les INDEX en chaînes, pas les valeurs
    assert sorted(spike) == ["0", "1", "2", "3", "4"]


def test_cost_outliers_are_log_scale_and_bounded_by_both_thresholds():
    from weekly_telemetry_aggregator.aggregator import _cost_outliers

    roots = [("s0", 1.0), ("s1", 1.0), ("s2", 1.1), ("s3", 50.0)]
    out = _cost_outliers(roots, z_min=3.0, min_cost=0.5)
    assert [(o.session_id, o.cost_usd) for o in out] == [("s3", 50.0)]
    assert out[0].z_score >= 3.0
    # tri (z DESC, session_id ASC) ; à 4 valeurs la médiane est la MOYENNE des deux
    # centraux (0.0207), donc les deux coûts égaux sortent à z = -0.6745, sous 0
    assert [o.session_id for o in _cost_outliers(roots, z_min=0.0, min_cost=0.0)] == [
        "s3",
        "s2",
    ]
    assert [o.z_score for o in _cost_outliers(roots, z_min=-1.0, min_cost=0.0)] == [
        54.695441,
        0.6745,
        -0.6745,
        -0.6745,
    ]
    # le plancher en dollars filtre même un z élevé
    assert _cost_outliers(roots, z_min=3.0, min_cost=60.0) == []
    # K6 : les z sont invariants par mise à l'échelle (donc calculés sur log10, pas en linéaire)
    doubled = [(sid, cost * 2) for sid, cost in roots]
    assert [o.session_id for o in _cost_outliers(doubled, z_min=3.0, min_cost=0.5)] == ["s3"]
    assert [o.z_score for o in _cost_outliers(doubled, z_min=0.0, min_cost=0.0)] == [
        o.z_score for o in _cost_outliers(roots, z_min=0.0, min_cost=0.0)
    ]
    # coût nul -> plancher 1e-6 au lieu de log10(0) : pas de ValueError
    assert _cost_outliers([("a", 0.0), ("b", 0.0)], z_min=0.0, min_cost=0.0) is not None


def test_compute_cost_outliers_state_warns_once_below_the_five_root_floor():
    from weekly_telemetry_aggregator.aggregator import (
        OUTLIER_MIN_ROOTS,
        compute_cost_outliers_state,
    )

    assert OUTLIER_MIN_ROOTS == 5
    warnings: list[WarningEntry] = []
    assert compute_cost_outliers_state(0, 15, warnings) == "no-data"
    assert warnings == []

    assert compute_cost_outliers_state(3, 15, warnings) == "skipped:small-sample"
    assert len(warnings) == 1
    assert warnings[0].session_id is None
    assert warnings[0].message == "sample trop petit (3 sessions < 5), cost_outliers peu fiables"

    assert compute_cost_outliers_state(5, 15, warnings) == "computed:small-sample"
    assert compute_cost_outliers_state(20, 15, warnings) == "computed"
    assert len(warnings) == 1  # aucun warning au-dessus du plancher


# --- usage builders : tools, skills, commandes, sous-agents, racine ------------


def test_build_tool_usage_sums_calls_and_separates_total_from_peak():
    from weekly_telemetry_aggregator.aggregator import _build_tool_usage

    u1 = make_usage("s1", [], tools={"read": 3, "edit": 1})
    u2 = make_usage("s2", [], tools={"read": 2})
    # 3 sessions : le même fingerprint d'arguments apparaît 2, 4 et 1 fois
    u3 = make_usage("s3", [])
    u1.tool_arg_fingerprints = {"read": {"fpA": 2, "fpB": 1}}
    u2.tool_arg_fingerprints = {"read": {"fpA": 4}}
    u3.tool_arg_fingerprints = {"read": {"fpA": 1}}
    u1.tool_result_fingerprints = {"read": {"rA": 3}}
    u2.tool_result_fingerprints = {"read": {"rA": 1, "rB": 2}}

    tool_usage, argument_fingerprints, result_fingerprints = _build_tool_usage([u1, u2, u3])

    # tri par nom de tool ; estimated_tokens = arg_chars / 4 (make_usage → 40 chars/call)
    assert [(t.tool, t.call_count, t.estimated_tokens) for t in tool_usage] == [
        ("edit", 1, 10),
        ("read", 5, 50),
    ]
    # total = somme sur la fenêtre ; peak = max d'une seule session
    assert dict(argument_fingerprints["read"]["fpA"]) == {"total": 7, "peak": 4}
    assert dict(argument_fingerprints["read"]["fpB"]) == {"total": 1, "peak": 1}
    # le canal RÉSULTAT n'a qu'un total (pas de notion de pic)
    assert dict(result_fingerprints["read"]) == {"rA": 4, "rB": 2}
    # corpus vide -> trois structures vides, pas d'exception
    assert _build_tool_usage([]) == ([], {}, {})


def test_build_skill_usage_counts_loads_and_keeps_catalog_order_for_never_loaded():
    from weekly_telemetry_aggregator.aggregator import _build_skill_usage

    u1 = make_usage("s1", [], skills={"beta": 1, "alpha": 2})
    u2 = make_usage("s2", [], skills={"alpha": 1})
    catalog = ["gamma", "alpha", "delta", "beta"]  # ordre du catalogue, non trié
    skill_usage, never_loaded = _build_skill_usage([u1, u2], catalog)

    assert [(s.skill, s.load_count, s.sessions_used_in) for s in skill_usage] == [
        ("alpha", 3, 2),
        ("beta", 1, 1),
    ]
    # jamais chargés : ordre du CATALOGUE préservé (pas un sorted()) — ici « gamma »
    # précède « delta », donc un tri alphabétique se verrait.
    assert never_loaded == ["gamma", "delta"]
    assert _build_skill_usage([], []) == ([], [])


def test_build_command_usage_counts_only_exploitable_slash_turns():
    from weekly_telemetry_aggregator.aggregator import _build_command_usage

    u1 = make_usage("s1", [], user_turns=["/swarmx test", "/swarmx deploy", "bonjour"])
    u2 = make_usage("s2", [], user_turns=['"/swarmx test: réponds ok"'])
    u3 = make_usage("s3", [], user_turns=[_NARRATION, "/home/benjamin/java -version", "/"])
    commands = _build_command_usage([u1, u2, u3])
    assert [(c.command, c.call_count, c.sessions_used_in) for c in commands] == [("swarmx", 3, 2)]
    assert _build_command_usage([]) == []


def test_is_exploitable_turn_requires_content_and_excludes_narration():
    from weekly_telemetry_aggregator.aggregator import _is_exploitable_turn

    assert _is_exploitable_turn("/optimize le rapport") is True
    assert _is_exploitable_turn("bonjour") is True  # exploitable même sans slash
    assert _is_exploitable_turn("") is False
    assert _is_exploitable_turn("   ") is False
    assert _is_exploitable_turn(_NARRATION) is False


def test_unmeasurable_warnings_are_one_line_per_harness_sorted():
    from weekly_telemetry_aggregator.aggregator import _unmeasurable_warnings
    from weekly_telemetry_aggregator.models import SessionClassification

    classifications = [
        SessionClassification(session_id="a", production_review_warning="review-unmeasurable:oc"),
        SessionClassification(session_id="b", production_review_warning="review-unmeasurable:oc"),
        SessionClassification(
            session_id="c", production_review_warning="review-unmeasurable:claude"
        ),
        SessionClassification(session_id="d", production_review_warning=None),
    ]
    warnings = _unmeasurable_warnings(classifications)
    assert [w.message for w in warnings] == [
        "review-unmeasurable:claude (1 sessions)",
        "review-unmeasurable:oc (2 sessions)",
    ]
    assert all(w.session_id is None for w in warnings)  # warning global, pas par session
    assert _unmeasurable_warnings([]) == []


def test_build_subagent_totals_counts_children_plus_orphans_and_groups_by_agent():
    from weekly_telemetry_aggregator.aggregator import _build_subagent_totals

    p = _period()
    root = make_usage("r", [make_step("r", p.start, cost=1.0)])
    child = make_usage("c", [make_step("c", p.start, cost=2.0)], parent="r", agent="build")
    orphan = make_usage("o", [make_step("o", p.start, cost=4.0)])  # agent_type None
    totals = _build_subagent_totals([child], [root, child, orphan], {"o"})

    assert totals.child_session_count == 2  # l'enfant + l'orphelin
    assert totals.total_cost_usd == 6.0
    # agent absent -> "unknown" ; tri alphabétique des agents
    assert [(a.agent_type, a.session_count, a.cost_usd) for a in totals.by_agent_type] == [
        ("build", 1, 2.0),
        ("unknown", 1, 4.0),
    ]
    # sans enfant ni orphelin : zéros, pas d'exception
    empty = _build_subagent_totals([], [root], set())
    assert (empty.child_session_count, empty.total_cost_usd, empty.by_agent_type) == (0, 0.0, [])


def test_usage_agg_is_a_stable_defaultdict_factory():
    from weekly_telemetry_aggregator.aggregator import _usage_agg

    agg = _usage_agg()
    bucket = agg["anthropic/claude-x"]
    assert bucket == {
        "sessions": set(),
        "tokens": 0,
        "cost": 0.0,
        "cache_read": 0.0,
        "fresh": 0.0,
    }
    assert agg["anthropic/claude-x"] is bucket  # même objet à chaque accès
    assert _usage_agg()["m"] is not bucket  # deux appelants n'échangent pas d'état


def test_root_totals_starts_zeroed_and_is_per_instance():
    from weekly_telemetry_aggregator.aggregator import _RootTotals

    acc = _RootTotals()
    assert acc.totals.session_count == 0
    assert acc.totals.total_cost_usd == 0.0
    assert acc.root_costs == {}
    assert acc.model_agg == {} and acc.harness_agg == {}
    assert dict(acc.day_cost) == {} and dict(acc.day_tokens) == {}
    assert acc.top_sessions == []

    other = _RootTotals()
    assert other.totals is not acc.totals  # default_factory, pas un défaut partagé
    acc.totals.session_count = 3
    assert other.totals.session_count == 0


def test_process_root_merges_descendants_and_writes_one_top_session_row():
    from weekly_telemetry_aggregator.aggregator import _process_root, _RootTotals
    from weekly_telemetry_aggregator.models import round6

    p = _period()
    root = make_usage(
        "r",
        [make_step("r", p.start + timedelta(hours=1), cost=1.0, cache_read=40, cache_write=5)],
        context_chars={"file": 400, "tool_result": 40, "text": 8},
    )
    root.harness = "opencode"
    child = make_usage(
        "c",
        [make_step("c", p.start + timedelta(hours=2), cost=2.0, cache_read=10)],
        parent="r",
        agent="build",
        context_chars={"file": 400},
    )
    by_id = {"r": root, "c": child}
    acc = _RootTotals()
    _process_root(root, by_id, acc, include_subagents=True, children_ids={"c"})

    assert acc.totals.session_count == 1  # la racine compte une fois
    assert acc.totals.total_cost_usd == 3.0
    assert acc.totals.total_tokens == 275  # (100+10+0+40+5) + (100+10+0+10+0)
    assert acc.totals.cache_read_tokens == 50
    assert acc.totals.cache_write_tokens == 5
    assert acc.totals.fresh_input_tokens == 200
    assert acc.totals.output_tokens == 20
    assert acc.totals.reasoning_tokens == 0
    assert acc.root_costs == {"r": 3.0}

    model = acc.model_agg["anthropic/claude-x"]
    assert model["sessions"] == {"r"}
    assert model["tokens"] == 275
    assert model["cost"] == 3.0
    assert model["cache_read"] == 50
    assert model["fresh"] == 200
    harness = acc.harness_agg["opencode"]
    assert harness["sessions"] == {"r"}
    assert harness["cost"] == 3.0 and harness["tokens"] == 275

    assert dict(acc.day_cost) == {"2026-08-05": 3.0}
    assert dict(acc.day_tokens) == {"2026-08-05": 275}
    assert dict(acc.day_cache) == {"2026-08-05": 50}
    assert dict(acc.day_fresh) == {"2026-08-05": 200}

    (row,) = acc.top_sessions
    assert row.session_id == "r"
    assert row.cost_usd == 3.0
    assert row.total_tokens == 275
    assert row.api_call_count == 2  # les DEUX steps fusionnés
    assert row.includes_subagents is True
    assert row.cache_read_tokens == 50 and row.cache_write_tokens == 5
    assert row.cache_efficiency == round6(50 / 250)
    assert row.harness == "opencode"
    assert row.project_path is None
    # contexte : racine + enfants, chars/4
    assert row.context_composition == {
        "file_tokens": 200,
        "tool_result_tokens": 10,
        "text_tokens": 2,
        "reasoning_tokens": 0,
    }
    # une seule étape -> durée nulle -> pas de coût par minute active
    assert (row.duration_seconds, row.active_time_seconds) == (0, 0)
    assert row.cost_per_active_minute is None


def test_process_root_excludes_subagents_and_falls_back_to_the_canonical_harness():
    from weekly_telemetry_aggregator.aggregator import _process_root, _RootTotals

    p = _period()
    root = make_usage("r", [make_step("r", p.start, cost=1.0)])
    child = make_usage("c", [make_step("c", p.start, cost=2.0)], parent="r")
    by_id = {"r": root, "c": child}

    acc = _RootTotals()
    _process_root(root, by_id, acc, include_subagents=False, children_ids={"c"})
    assert acc.root_costs == {"r": 1.0}
    assert acc.totals.total_cost_usd == 1.0
    assert acc.top_sessions[0].includes_subagents is False
    assert acc.top_sessions[0].api_call_count == 1

    # harness vide -> préfixe de l'id canonique ; absent des deux côtés -> ""
    acc_plain = _RootTotals()
    _process_root(root, by_id, acc_plain, include_subagents=False, children_ids=set())
    assert acc_plain.top_sessions[0].harness == ""
    assert "" in acc_plain.harness_agg

    prefixed = make_usage("oc:ses_1", [make_step("oc:ses_1", p.start, cost=1.0)])
    acc_pref = _RootTotals()
    _process_root(
        prefixed, {"oc:ses_1": prefixed}, acc_pref, include_subagents=False, children_ids=set()
    )
    assert acc_pref.top_sessions[0].harness == "oc"
