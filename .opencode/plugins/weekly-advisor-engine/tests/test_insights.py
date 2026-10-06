"""Pure rule computation (Part 6) — deltas, alerts, maintenance R1-R4."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from helpers import tzutc

from weekly_telemetry_aggregator.config import InsightsConfig
from weekly_telemetry_aggregator.insights import (
    EXCLUSION_RATE_MAX_DEFAULT,
    compute,
    flatten_harness_findings,
    run,
)


def _summary(
    period,
    *,
    cost=1.0,
    cache=0.8,
    tokens=1000,
    skills=(),
    never=(),
    pairs=(),
    daily=(),
    generated_at=None,
    catalog=0,
    node_skills=True,
    entries=None,
):
    summary = {
        "period": {"start": period[0], "end": period[1]},
        "totals": {"total_cost_usd": cost, "cache_hit_rate": cache, "total_tokens": tokens},
        "skill_usage": [{"skill": s} for s in skills],
        "skills_never_loaded": list(never),
        "skill_similar_pairs": list(pairs),
        "daily_totals": list(daily),
        "skill_catalog_count": catalog,
        "generated_at": generated_at or (period[1] + "T00:00:00Z"),
    }
    if entries is not None:
        # D3 : la clé seule (même vide) active les filtres de catalogue.
        summary["skill_catalog_entries"] = list(entries)
    return summary


RUN = tzutc(2026, 8, 12)


def _cfg(**over):
    return replace(InsightsConfig(), **over)


def test_weekly_budget_alert_and_cost_delta():
    cur = _summary(("2026-08-05", "2026-08-12"), cost=50.0)
    prev = _summary(("2026-07-29", "2026-08-05"), cost=25.0)
    out = compute(
        run_time=RUN,
        current_summary=cur,
        previous_summary=prev,
        current_digest=None,
        previous_digest=None,
        recent_summaries=[cur, prev],
        insights_cfg=_cfg(),
        ignored_findings=[],
    )
    assert any(a["rule"] == "weekly_budget_usd" for a in out["alerts"])
    assert out["deltas"]["cost_wow_pct"] == 100.0


def test_cache_alert_and_delta():
    cur = _summary(("A", "B"), cost=1.0, cache=0.3)
    prev = _summary(("C", "D"), cost=1.0, cache=0.7)
    out = compute(
        run_time=RUN,
        current_summary=cur,
        previous_summary=prev,
        current_digest=None,
        previous_digest=None,
        recent_summaries=[cur, prev],
        insights_cfg=_cfg(),
        ignored_findings=[],
    )
    assert any(a["rule"] == "cache_hit_rate_min" for a in out["alerts"])
    assert out["deltas"]["cache_hit_rate_delta"] == -0.4


def test_monthly_budget_via_recent_summaries():
    cur = _summary(("A", "B"), cost=60.0, generated_at="2026-08-12T00:00:00Z")
    older = _summary(("C", "D"), cost=60.0, generated_at="2026-07-20T00:00:00Z")
    out = compute(
        run_time=RUN,
        current_summary=cur,
        previous_summary=older,
        current_digest=None,
        previous_digest=None,
        recent_summaries=[cur, older],
        insights_cfg=_cfg(monthly_budget_usd=100.0),
        ignored_findings=[],
    )
    assert any(a["rule"] == "monthly_budget_usd" for a in out["alerts"])


def test_token_risk_finding_above_cap():
    """D2 — réécrit : le cap est en coût (`session_cost_cap`), plus en tokens.

    L'ancien test figeait un cap de 4 M tokens ; or les tokens par dollar
    varient d'un modèle à l'autre, donc ce cap ne disait rien du budget.
    """
    cur = _summary(("A", "B"), cost=10.0)
    cur["top_sessions_by_cost"] = [
        {"session_id": "s1", "cost_usd": 3.0, "total_tokens": 5_000_000},
        {"session_id": "s2", "cost_usd": 1.0, "total_tokens": 1_000_000},
    ]
    out = compute(
        run_time=RUN,
        current_summary=cur,
        previous_summary=None,
        current_digest=None,
        previous_digest=None,
        recent_summaries=[cur],
        insights_cfg=_cfg(session_cost_cap=2.0),
        ignored_findings=[],
    )
    hits = [f for f in out["maintenance"]["findings"] if f["category"] == "token-risk"]
    assert [f["session_id"] for f in hits] == ["s1"]


def test_architecture_drift_is_observation_only_and_thresholded():
    cur = _summary(("A", "B"))
    prev = _summary(("C", "D"))
    older = _summary(("E", "F"))
    cur["architecture_observations"] = {
        "state_counts": {"declared": 2, "observed": 0, "absent": 0, "unknown": 0},
        "config": {"available": True, "valid": True},
        "inventory_counts": {"plugins": 2},
        "harness_scope": {"profile": "advisory"},
    }
    prev["architecture_observations"] = {
        "state_counts": {"declared": 1, "observed": 0, "absent": 0, "unknown": 0},
        "config": {"available": True, "valid": True},
        "inventory_counts": {"plugins": 1},
        "harness_scope": {"profile": "advisory"},
    }
    older["architecture_observations"] = prev["architecture_observations"]
    out = compute(
        run_time=RUN,
        current_summary=cur,
        previous_summary=prev,
        current_digest=None,
        previous_digest=None,
        recent_summaries=[cur, prev, older],
        insights_cfg=_cfg(),
        ignored_findings=[],
    )
    drift = [f for f in out["maintenance"]["findings"] if f["category"] == "architecture-drift"]
    assert drift and drift[0]["observation_only"] is True
    assert not any(a["rule"] == "architecture-drift" for a in out["alerts"])


def test_daily_spike_alert():
    cur = _summary(("A", "B"), daily=[{"date": "2026-08-12", "cost_usd": 20.0}])
    prior = _summary(("C", "D"), daily=[{"date": d, "cost_usd": 0.1} for d in range(6)])
    out = compute(
        run_time=RUN,
        current_summary=cur,
        previous_summary=prior,
        current_digest=None,
        previous_digest=None,
        recent_summaries=[cur, prior],
        insights_cfg=_cfg(),
        ignored_findings=[],
    )
    spikes = [a for a in out["alerts"] if a["rule"] == "daily_spike"]
    assert spikes and spikes[0]["day"] == "2026-08-12"


def test_lint_delta_by_rule():
    cur = _summary(("A", "B"))
    prev = _summary(("C", "D"))
    cd = {"findings": [{"rule": "E501", "severity": "error"}, {"rule": "F841"}]}
    pd = {"findings": [{"rule": "E501"}]}
    out = compute(
        run_time=RUN,
        current_summary=cur,
        previous_summary=prev,
        current_digest=cd,
        previous_digest=pd,
        recent_summaries=[cur, prev],
        insights_cfg=_cfg(),
        ignored_findings=[],
    )
    assert out["deltas"]["lint_violations_delta_by_rule"] == {"E501": 0, "F841": 1}


def test_retire_candidate_r1():
    cur = _summary(("A", "B"), never=["dead-skill"], catalog=3)
    recents = [cur] + [_summary(("X", "Y"), never=["dead-skill"]) for _ in range(3)]
    out = compute(
        run_time=RUN,
        current_summary=cur,
        previous_summary=None,
        current_digest=None,
        previous_digest=None,
        recent_summaries=recents,
        insights_cfg=_cfg(never_loaded_runs_threshold=4),
        ignored_findings=[],
    )
    cats = [f["category"] for f in out["maintenance"]["findings"]]
    assert "retire-candidate" in cats


def test_ignored_findings_suppress_retire():
    cur = _summary(("A", "B"), never=["dead-skill"], catalog=3)
    recents = [cur] + [_summary(("X", "Y"), never=["dead-skill"]) for _ in range(3)]
    out = compute(
        run_time=RUN,
        current_summary=cur,
        previous_summary=None,
        current_digest=None,
        previous_digest=None,
        recent_summaries=recents,
        insights_cfg=_cfg(never_loaded_runs_threshold=4, cache_write_zero_runs=99),
        ignored_findings=["skill-maintenance:dead-skill"],
    )
    assert out["maintenance"]["findings"] == []


def test_merge_candidate_from_similar_pairs():
    cur = _summary(("A", "B"), pairs=[{"skills": ["alpha", "beta"], "similarity": 0.93}])
    out = compute(
        run_time=RUN,
        current_summary=cur,
        previous_summary=None,
        current_digest=None,
        previous_digest=None,
        recent_summaries=[cur],
        insights_cfg=_cfg(),
        ignored_findings=[],
    )
    cats = [f["category"] for f in out["maintenance"]["findings"]]
    assert "merge-candidate" in cats


def test_fix_candidate_from_digest_findings():
    cur = _summary(("A", "B"))
    cd = {"findings": [{"rule": "ruff-E501", "message": "ligne trop longue"}]}
    out = compute(
        run_time=RUN,
        current_summary=cur,
        previous_summary=None,
        current_digest=cd,
        previous_digest=None,
        recent_summaries=[cur],
        insights_cfg=_cfg(),
        ignored_findings=[],
    )
    cats = [f["category"] for f in out["maintenance"]["findings"]]
    assert "fix-candidate" in cats


def test_newly_loaded_and_newly_silent_skills():
    cur = _summary(("A", "B"), skills=["x", "y"])
    prev = _summary(("C", "D"), skills=["x", "z"])
    out = compute(
        run_time=RUN,
        current_summary=cur,
        previous_summary=prev,
        current_digest=None,
        previous_digest=None,
        recent_summaries=[cur, prev],
        insights_cfg=_cfg(),
        ignored_findings=[],
    )
    assert out["deltas"]["skills"]["newly_loaded"] == ["y"]
    assert out["deltas"]["skills"]["newly_silent"] == ["z"]


def test_run_writes_insights_from_summary(tmp_path: Path):
    date = "2026-08-12"
    summary = _summary(
        ("2026-08-05", "2026-08-12"), cost=50.0, catalog=2, generated_at=f"{date}T00:00:00Z"
    )
    (tmp_path / f"weekly-summary-{date}.json").write_text(json.dumps(summary), encoding="utf-8")
    from weekly_telemetry_aggregator.config import TelemetryConfig

    cfg = TelemetryConfig()
    cfg.output_dir = tmp_path
    rc = run(cfg, anchor=RUN.isoformat())
    out = json.loads((tmp_path / f"weekly-insights-{date}.json").read_text(encoding="utf-8"))
    assert rc == 0
    assert any(a["rule"] == "weekly_budget_usd" for a in out["alerts"])


def test_run_missing_summary_is_total_failure(tmp_path: Path):
    from weekly_telemetry_aggregator.config import TelemetryConfig
    from weekly_telemetry_aggregator.main import EXIT_TOTAL_FAILURE

    cfg = TelemetryConfig()
    cfg.output_dir = tmp_path
    assert run(cfg, anchor=RUN.isoformat()) == EXIT_TOTAL_FAILURE


# ============================================================ v5.28 (P0/P1/P2)


def test_flatten_harness_findings_per_component():
    """Real 7.9.0 digests carry findings per inspection component (top-level empty)."""
    digest = {
        "findings": [],
        "inspection": {
            "command": [
                {
                    "path": "a.sh",
                    "findings": [
                        {
                            "rule": "security/mcp-tool-poisoning",
                            "severity": "warning",
                            "message": "m1",
                        }
                    ],
                }
            ],
            "claude_md": [],
            "uncategorized": [
                {
                    "path": "x",
                    "findings": [
                        {
                            "rule": "security/mcp-tool-poisoning",
                            "severity": "warning",
                            "message": "m1",
                        },
                        {"rule": "security/obfuscation", "severity": "error", "message": "m2"},
                    ],
                    "rules": [
                        {"rule": "security/obfuscation", "result": "fail"},
                        {"rule": "security/unbounded-delegation", "result": "fail"},
                    ],
                }
            ],
        },
    }
    flat = flatten_harness_findings(digest)
    counts = __import__("collections").Counter(f["rule"] for f in flat)
    assert counts["security/mcp-tool-poisoning"] == 2  # une par composant (command + uncategorized)
    assert counts["security/obfuscation"] == 1  # rules fallback doublon ignoré (finding détaillé)
    assert counts["security/unbounded-delegation"] == 1  # fallback sans finding
    assert all(f["severity"] for f in flat if f["message"])
    assert flatten_harness_findings(None) == []
    assert flatten_harness_findings({}) == []


def test_lint_delta_by_rule_with_per_component_digest():
    """P0: deltas lint must not degrade with real 7.9.0 digests (per-component findings)."""
    cur = _summary(("A", "B"))
    prev = _summary(("C", "D"))
    cd = {
        "inspection": {
            "uncategorized": [
                {
                    "path": "a",
                    "findings": [
                        {"rule": "E501", "severity": "error", "message": "m1"},
                        {"rule": "F841", "severity": "error", "message": "m2"},
                    ],
                }
            ]
        }
    }
    pd = {
        "inspection": {
            "uncategorized": [
                {"path": "a", "findings": [{"rule": "E501", "severity": "error", "message": "m1"}]}
            ]
        }
    }
    out = compute(
        run_time=RUN,
        current_summary=cur,
        previous_summary=prev,
        current_digest=cd,
        previous_digest=pd,
        recent_summaries=[cur, prev],
        insights_cfg=_cfg(),
        ignored_findings=[],
    )
    assert out["deltas"]["lint_violations_delta_by_rule"] == {"E501": 0, "F841": 1}


def test_fix_candidate_from_per_component_digest():
    cur = _summary(("A", "B"))
    cd = {
        "inspection": {
            "uncategorized": [
                {"path": "a", "findings": [{"rule": "ruff-E501", "message": "ligne trop longue"}]}
            ]
        }
    }
    out = compute(
        run_time=RUN,
        current_summary=cur,
        previous_summary=None,
        current_digest=cd,
        previous_digest=None,
        recent_summaries=[cur],
        insights_cfg=_cfg(),
        ignored_findings=[],
    )
    cats = [f["category"] for f in out["maintenance"]["findings"]]
    assert "fix-candidate" in cats


def test_cache_write_zero_alert_after_two_runs():
    cur = _summary(("A", "B"))
    prev = _summary(("C", "D"))
    out = compute(
        run_time=RUN,
        current_summary=cur,
        previous_summary=prev,
        current_digest=None,
        previous_digest=None,
        recent_summaries=[cur, prev],
        insights_cfg=_cfg(cache_write_zero_runs=2),
        ignored_findings=[],
    )
    assert any(a["rule"] == "cache_write_zero_runs" for a in out["alerts"])


def test_cache_write_zero_no_alert_when_write_observed():
    cur = _summary(("A", "B"))
    prev = _summary(("C", "D"))
    prev["totals"]["cache_write_tokens"] = 100
    out = compute(
        run_time=RUN,
        current_summary=cur,
        previous_summary=prev,
        current_digest=None,
        previous_digest=None,
        recent_summaries=[cur, prev],
        insights_cfg=_cfg(cache_write_zero_runs=2),
        ignored_findings=[],
    )
    assert not any(a["rule"] == "cache_write_zero_runs" for a in out["alerts"])


def test_run_baseline_summary_used_when_no_previous(tmp_path: Path):
    from weekly_telemetry_aggregator.config import TelemetryConfig
    from weekly_telemetry_aggregator.main import EXIT_OK

    date = "2026-08-12"
    summary = _summary(("2026-08-05", date), generated_at=f"{date}T00:00:00Z")
    baseline = _summary(("2026-07-29", "2026-08-05"), cost=0.5)
    (tmp_path / f"weekly-summary-{date}.json").write_text(json.dumps(summary), encoding="utf-8")
    bp = tmp_path / "baseline.json"
    bp.write_text(json.dumps(baseline), encoding="utf-8")
    cfg = TelemetryConfig()
    cfg.output_dir = tmp_path
    cfg.project_root = tmp_path
    rc = run(cfg, anchor=RUN.isoformat(), baseline_summary_path=str(bp))
    assert rc == EXIT_OK
    out = json.loads((tmp_path / f"weekly-insights-{date}.json").read_text(encoding="utf-8"))
    assert out["previous_run_date"] == "2026-08-05"
    assert out["baseline_summary_file"] == str(bp)
    state = json.loads((tmp_path / "previous_run.json").read_text(encoding="utf-8"))
    assert state["run_date"] == date
    assert state["summary_file"] == f"weekly-summary-{date}.json"


def test_run_state_file_drives_next_discovery(tmp_path: Path):
    from weekly_telemetry_aggregator.config import TelemetryConfig
    from weekly_telemetry_aggregator.main import EXIT_OK

    cfg = TelemetryConfig()
    cfg.output_dir = tmp_path
    cfg.project_root = tmp_path
    d1 = "2026-08-12"
    (tmp_path / f"weekly-summary-{d1}.json").write_text(
        json.dumps(_summary(("2026-08-05", d1), generated_at=f"{d1}T00:00:00Z")),
        encoding="utf-8",
    )
    assert run(cfg, anchor=RUN.isoformat()) == EXIT_OK
    d2 = "2026-08-19"
    (tmp_path / f"weekly-summary-{d2}.json").write_text(
        json.dumps(_summary(("2026-08-12", d2), generated_at=f"{d2}T00:00:00Z")),
        encoding="utf-8",
    )
    assert run(cfg, anchor=tzutc(2026, 8, 19).isoformat()) == EXIT_OK
    out = json.loads((tmp_path / f"weekly-insights-{d2}.json").read_text(encoding="utf-8"))
    assert out["previous_run_date"] == d1


def test_run_discovers_previous_digest_by_glob(tmp_path: Path):
    from weekly_telemetry_aggregator.config import TelemetryConfig
    from weekly_telemetry_aggregator.main import EXIT_OK

    date = "2026-08-12"
    summary = _summary(("2026-08-05", date), generated_at=f"{date}T00:00:00Z")
    (tmp_path / f"weekly-summary-{date}.json").write_text(json.dumps(summary), encoding="utf-8")
    (tmp_path / f"weekly-harness-digest-{date}.json").write_text(
        json.dumps(
            {
                "inspection": {
                    "uncategorized": [
                        {
                            "path": "a",
                            "findings": [
                                {"rule": "E501", "severity": "error", "message": "m1"},
                                {"rule": "F841", "severity": "error", "message": "m2"},
                            ],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "weekly-harness-digest-2026-08-05.json").write_text(
        json.dumps(
            {
                "inspection": {
                    "uncategorized": [
                        {
                            "path": "a",
                            "findings": [{"rule": "E501", "severity": "error", "message": "m1"}],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    cfg = TelemetryConfig()
    cfg.output_dir = tmp_path
    cfg.project_root = tmp_path
    assert run(cfg, anchor=RUN.isoformat()) == EXIT_OK
    out = json.loads((tmp_path / f"weekly-insights-{date}.json").read_text(encoding="utf-8"))
    assert out["deltas"]["lint_violations_delta_by_rule"] == {"E501": 0, "F841": 1}


def test_discover_previous_finds_migrated_legacy_artifacts(tmp_path: Path):
    """C2 (v6.0.p) : artefacts migrés vers runs/<id>/legacy/ restent la baseline WoW."""
    from weekly_telemetry_aggregator.insights import _discover_previous

    legacy = tmp_path / "runs" / "2026-08-12-abcd1234" / "legacy"
    legacy.mkdir(parents=True)
    (legacy / "weekly-summary-2026-08-05.json").write_text(
        json.dumps(_summary(("2026-07-29", "2026-08-05"))), encoding="utf-8"
    )
    prev = _discover_previous("weekly-summary-*.json", "2026-08-19", tmp_path)
    assert prev is not None
    assert prev["period"]["start"] == "2026-07-29"


# ============================================================ v5.28 (K5/K7/K8/K11)


def _summary_warnings_with_mismatch() -> dict:
    return {
        "period": {"start": "2026-08-05T00:00:00Z", "end": "2026-08-12T00:00:00Z"},
        "generated_at": "2026-08-12T00:00:00Z",
        "totals": {
            "total_cost_usd": 1.0,
            "cache_hit_rate": 0.9,
            "total_tokens": 100,
            "cache_write_tokens": 0,
        },
        "warnings": [
            {
                "session_id": "ses_x",
                "message": "cross-check mismatch: parts cost $0.5081 vs session_v2 $0.3663",
                "partial": False,
                "parts_cost": 0.5081,
                "session_v2_cost": 0.3663,
            }
        ],
    }


def test_cost_discrepancies_structured():
    from weekly_telemetry_aggregator.insights import compute

    data = compute(
        run_time=tzutc(2026, 8, 12),
        current_summary=_summary_warnings_with_mismatch(),
        previous_summary=None,
        current_digest=None,
        previous_digest=None,
        recent_summaries=[_summary_warnings_with_mismatch()],
        insights_cfg=InsightsConfig(),
        ignored_findings=[],
    )
    assert data["cost_discrepancies"] == [
        {"session_id": "ses_x", "parts_cost_usd": 0.5081, "session_v2_cost_usd": 0.3663}
    ]


def test_cache_write_zero_adds_maintenance_finding():
    from weekly_telemetry_aggregator.insights import compute

    data = compute(
        run_time=tzutc(2026, 8, 12),
        current_summary=_summary_warnings_with_mismatch(),
        previous_summary=None,
        current_digest=None,
        previous_digest=None,
        recent_summaries=[
            _summary_warnings_with_mismatch(),  # run actuel (cache_write=0)
            _summary_warnings_with_mismatch(),  # run précédent (cache_write=0)
        ],
        insights_cfg=InsightsConfig(cache_write_zero_runs=2),
        ignored_findings=[],
    )
    assert any(a["rule"] == "cache_write_zero_runs" for a in data["alerts"])
    assert any(
        f.get("recommendation_type") == "cache-write-zero" for f in data["maintenance"]["findings"]
    )


def test_harness_ignored_rules_filter_counts():
    from weekly_telemetry_aggregator.insights import compute

    digest = {
        "inspection": {
            "uncategorized": [
                {
                    "findings": [
                        {"rule": "security/mcp-tool-poisoning", "severity": "high", "message": "x"},
                        {"rule": "security/obfuscation", "severity": "high", "message": "y"},
                    ]
                }
            ]
        }
    }
    data = compute(
        run_time=tzutc(2026, 8, 12),
        current_summary=_summary_warnings_with_mismatch(),
        previous_summary=None,
        current_digest=digest,
        previous_digest=digest,  # nécessaire pour calculer les deltas lint
        recent_summaries=[_summary_warnings_with_mismatch()],
        insights_cfg=InsightsConfig(lint_violations_max=1),
        ignored_findings=[],
        harness_ignored_rules=["security/mcp-tool-poisoning"],
    )
    # 1 seule règle non ignorée → pas d'alerte lint_violations_max (seuil 1)
    assert not any(a["rule"] == "lint_violations_max" for a in data["alerts"])
    deltas = data["deltas"]["lint_violations_delta_by_rule"]
    assert "security/mcp-tool-poisoning" not in deltas
    assert "security/obfuscation" in deltas  # delta 0 (digests identiques) mais règle présente


def test_run_first_run_writes_baseline_marker(tmp_path: Path):
    """K11: premier run sans état → baseline 'first-run' + fichier weekly-baseline-<date>.json."""
    from weekly_telemetry_aggregator.config import TelemetryConfig
    from weekly_telemetry_aggregator.insights import run as insights_run

    cfg = TelemetryConfig()
    cfg.output_dir = tmp_path
    (tmp_path / "weekly-summary-2026-08-12.json").write_text(
        json.dumps(
            _summary(
                ("2026-08-05T00:00:00Z", "2026-08-12T00:00:00Z"),
                generated_at="2026-08-12T00:00:00Z",
            )
        ),
        encoding="utf-8",
    )
    rc = insights_run(cfg, anchor="2026-08-12T00:00:00Z")
    assert rc == 0
    data = json.loads((tmp_path / "weekly-insights-2026-08-12.json").read_text(encoding="utf-8"))
    assert data.get("baseline") == "first-run"
    assert (tmp_path / "weekly-baseline-2026-08-12.json").exists()
    assert (tmp_path / "previous_run.json").exists()


def test_r1_mentions_declared_target():
    """v5.30 : R1 mentionne la cible déclarée d'un skill jamais chargé."""
    from weekly_telemetry_aggregator.insights import compute

    cur = _summary(("A", "B"), never=["jira-to-code-audit"], catalog=2)
    cur["skills_targets"] = {"jira-to-code-audit": ["java-pro", "backend-architect"]}
    recents = [cur] + [_summary(("X", "Y"), never=["jira-to-code-audit"]) for _ in range(3)]
    out = compute(
        run_time=RUN,
        current_summary=cur,
        previous_summary=None,
        current_digest=None,
        previous_digest=None,
        recent_summaries=recents,
        insights_cfg=_cfg(never_loaded_runs_threshold=3),
        ignored_findings=[],
    )
    hits = [f for f in out["maintenance"]["findings"] if "jira-to-code-audit" in f["description"]]
    assert hits and "cible déclarée : java-pro, backend-architect" in hits[0]["description"]


# ============================================================ v5.31 (gaps run 15j)


def test_window_mismatch_neutralizes_volume_deltas():
    """v5.31 : fenêtres de durées différentes → cost_wow/tokens sautés + warning."""
    cur = _summary(
        ("2026-07-01T00:00:00Z", "2026-07-16T00:00:00Z"),
        cost=67.0,
        generated_at="2026-07-16T00:00:00Z",
    )
    prev = _summary(
        ("2026-07-01T00:00:00Z", "2026-07-15T00:00:00Z"),
        cost=51.0,
        generated_at="2026-07-15T00:00:00Z",
    )
    out = compute(
        run_time=tzutc(2026, 7, 16),
        current_summary=cur,
        previous_summary=prev,
        current_digest=None,
        previous_digest=None,
        recent_summaries=[cur, prev],
        insights_cfg=InsightsConfig(cost_wow_pct_max=15.0),
        ignored_findings=[],
    )
    assert out["deltas"]["cost_wow_pct"] is None
    assert out["deltas"]["total_tokens_delta_pct"] is None
    assert any("fenêtre précédente" in w for w in out["deltas"].get("_warnings", []))
    assert not any(a["rule"] == "cost_wow_pct_max" for a in out["alerts"])


def test_daily_spike_z_capped_when_mad_zero():
    """v5.31 : un z astronomique (MAD≈0) est borné à DAILY_SPIKE_Z_CAP avec note."""
    from weekly_telemetry_aggregator.insights import DAILY_SPIKE_Z_CAP

    cur = _summary(
        ("2026-08-01T00:00:00Z", "2026-08-15T00:00:00Z"),
        cost=40.0,
        generated_at="2026-08-15T00:00:00Z",
    )
    cur["daily_totals"] = (
        [
            {"date": "2026-08-07", "cost_usd": 39.35, "total_tokens": 100, "cache_hit_rate": 0.9},
        ]
        + [
            {
                "date": f"2026-08-{d:02d}",
                "cost_usd": 0.1,
                "total_tokens": 100,
                "cache_hit_rate": 0.9,
            }
            for d in range(1, 7)
        ]
        + [
            {
                "date": f"2026-08-{d:02d}",
                "cost_usd": 0.1,
                "total_tokens": 100,
                "cache_hit_rate": 0.9,
            }
            for d in range(8, 15)
        ]
    )
    prev = _summary(
        ("2026-07-01T00:00:00Z", "2026-08-01T00:00:00Z"),
        cost=10.0,
        generated_at="2026-08-01T00:00:00Z",
    )
    prev["daily_totals"] = [
        {"date": f"2026-07-{d:02d}", "cost_usd": 0.1, "total_tokens": 100, "cache_hit_rate": 0.9}
        for d in range(1, 32)
    ]
    out = compute(
        run_time=tzutc(2026, 8, 15),
        current_summary=cur,
        previous_summary=prev,
        current_digest=None,
        previous_digest=None,
        recent_summaries=[cur, prev],
        insights_cfg=InsightsConfig(daily_spike_z_min=3.0),
        ignored_findings=[],
    )
    spikes = [a for a in out["alerts"] if a["rule"] == "daily_spike"]
    assert spikes
    assert max(a["observed"] for a in spikes) <= DAILY_SPIKE_Z_CAP
    assert any("MAD≈0" in a.get("note", "") for a in spikes)


# ============================================================ v6.0.n (ancre glissante)


def test_discover_previous_prefers_strictly_older(tmp_path: Path):
    """P2 : un summary de date strictement antérieure reste prioritaire."""
    from weekly_telemetry_aggregator.insights import _discover_previous

    runs = tmp_path / "runs"
    older = runs / "2026-08-14-aaa"
    current = runs / "2026-08-16-aaa"
    same_prev = runs / "2026-08-16-bbb"
    for d in (older, current, same_prev):
        d.mkdir(parents=True)
    (older / "weekly-summary-2026-08-14.json").write_text(
        json.dumps({"run": "older"}), encoding="utf-8"
    )
    (current / "weekly-summary-2026-08-16.json").write_text(
        json.dumps({"run": "current"}), encoding="utf-8"
    )
    (same_prev / "weekly-summary-2026-08-16.json").write_text(
        json.dumps({"run": "same-date"}), encoding="utf-8"
    )
    found = _discover_previous("weekly-summary-*.json", "2026-08-16", tmp_path, exclude_dir=current)
    assert found == {"run": "older"}


def test_discover_previous_fallback_same_date_outside_current_run(tmp_path: Path):
    """P2 : sans antérieur strict, le plus récent même-date hors run courant est pris."""
    from weekly_telemetry_aggregator.insights import _discover_previous

    runs = tmp_path / "runs"
    current = runs / "2026-08-16-aaa"
    same_prev = runs / "2026-08-16-bbb"
    dirty = runs / "2026-08-16-ccc"
    for d in (current, same_prev, dirty):
        d.mkdir(parents=True)
    (current / "weekly-summary-2026-08-16.json").write_text(
        json.dumps({"run": "current"}), encoding="utf-8"
    )
    (same_prev / "weekly-summary-2026-08-16.json").write_text(
        json.dumps({"run": "same-date"}), encoding="utf-8"
    )
    (dirty / "weekly-summary-2026-08-16.json").write_text(
        json.dumps({"run": "same-date-2"}), encoding="utf-8"
    )
    found = _discover_previous("weekly-summary-*.json", "2026-08-16", tmp_path, exclude_dir=current)
    assert found == {"run": "same-date-2"}


def test_run_lint_coverage_alert_when_allowlist_narrow(tmp_path: Path):
    """P8 : alerte lint_coverage quand la part hors allowlist dépasse le seuil."""
    from weekly_telemetry_aggregator.config import TelemetryConfig
    from weekly_telemetry_aggregator.main import EXIT_OK

    date = "2026-08-12"
    (tmp_path / f"weekly-summary-{date}.json").write_text(
        json.dumps(_summary(("2026-08-05", date), generated_at=f"{date}T00:00:00Z")),
        encoding="utf-8",
    )
    digest = {
        "inspection": {"summary": {"total": 5}},
        "harness_scope": {"include_patterns": [], "unscoped_file_count": 30},
    }
    (tmp_path / f"weekly-harness-digest-{date}.json").write_text(
        json.dumps(digest), encoding="utf-8"
    )
    cfg = TelemetryConfig()
    cfg.output_dir = tmp_path
    cfg.project_root = tmp_path
    assert run(cfg, anchor=RUN.isoformat()) == EXIT_OK
    out = json.loads((tmp_path / f"weekly-insights-{date}.json").read_text(encoding="utf-8"))
    alert = next((a for a in out["alerts"] if a["rule"] == "lint_coverage"), None)
    assert alert is not None
    assert alert["threshold"] == 0.7
    assert alert["observed"] < 0.7


def test_run_lint_coverage_no_alert_when_allowlist_wide(tmp_path: Path):
    """P8 : au-dessus du seuil, pas d'alerte lint_coverage."""
    from weekly_telemetry_aggregator.config import TelemetryConfig
    from weekly_telemetry_aggregator.main import EXIT_OK

    date = "2026-08-12"
    (tmp_path / f"weekly-summary-{date}.json").write_text(
        json.dumps(_summary(("2026-08-05", date), generated_at=f"{date}T00:00:00Z")),
        encoding="utf-8",
    )
    digest = {
        "inspection": {"summary": {"total": 5}},
        "harness_scope": {"include_patterns": [], "unscoped_file_count": 1},
    }
    (tmp_path / f"weekly-harness-digest-{date}.json").write_text(
        json.dumps(digest), encoding="utf-8"
    )
    cfg = TelemetryConfig()
    cfg.output_dir = tmp_path
    cfg.project_root = tmp_path
    assert run(cfg, anchor=RUN.isoformat()) == EXIT_OK
    out = json.loads((tmp_path / f"weekly-insights-{date}.json").read_text(encoding="utf-8"))
    assert all(a["rule"] != "lint_coverage" for a in out["alerts"])


# ============================================================ seuil retrait R1


def _retire_kwargs(cur, **over):
    return dict(
        run_time=RUN,
        current_summary=cur,
        previous_summary=None,
        current_digest=None,
        previous_digest=None,
        insights_cfg=_cfg(**over),
        ignored_findings=[],
    )


def test_retire_candidate_default_threshold_is_an_evidence_floor():
    """D3 — réécrit `test_retire_candidate_default_threshold_frozen`.

    L'ancien test figeait « jamais chargé sur 8 runs » : avec 8 runs
    d'historique au maximum, cette valeur est un **plafond de mesure**, pas un
    seuil de qualité — la règle ne pouvait plus discriminer (34 findings sur le
    run du 03/10, dont 12 sur des skills `ttl_policy=pin`).

    `never_loaded_runs_threshold` devient un **plancher de preuve** (« combien de
    runs de preuve exiger avant de juger ») et la série doit **saturer** la
    fenêtre observée. Le test vérifie les deux rôles séparément.
    """
    from dataclasses import fields

    defaults = {f.name: f.default for f in fields(InsightsConfig)}
    assert defaults["never_loaded_runs_threshold"] == 8
    cur = _summary(("A", "B"), never=["dead-skill"], catalog=3)
    # (1) plancher de preuve : 7 runs < seuil 8 → on ne juge pas.
    recents7 = [cur] + [_summary(("X", "Y"), never=["dead-skill"]) for _ in range(6)]
    below = compute(recent_summaries=recents7, **_retire_kwargs(cur))
    cats_below = [f["category"] for f in below["maintenance"]["findings"]]
    assert "retire-candidate" not in cats_below
    # (2) plancher atteint ET fenêtre saturée (8/8) → fiche émise.
    recents8 = recents7 + [_summary(("X", "Y"), never=["dead-skill"])]
    at = compute(recent_summaries=recents8, **_retire_kwargs(cur))
    cats_at = [f["category"] for f in at["maintenance"]["findings"]]
    assert "retire-candidate" in cats_at


def test_retire_candidate_partial_streak_no_longer_fires():
    """D3 — une série partielle n'est plus un signal de qualité.

    Fenêtre de 8 runs, seuil 4 (valeur de la config livrée) : l'ancien
    prédicat `count >= 4` émettait un finding pour une série de 4/8. D3 exige la
    saturation — plus rien. C'est le test qui prouve que le seuil discrimine.
    """
    cur = _summary(("A", "B"), never=["dead-skill"], catalog=3)
    # séries de 4 puis de 7 runs sur une fenêtre de 8
    hist = [_summary(("X", "Y"), never=["dead-skill"] if i < 7 else []) for i in range(8)]
    hist[0] = cur
    out = compute(recent_summaries=hist, **_retire_kwargs(cur))
    cats = [f["category"] for f in out["maintenance"]["findings"]]
    assert "retire-candidate" not in cats
    assert out["maintenance"]["stats"]["never_loaded_consecutive"]["dead-skill"] == 7


def test_retire_candidate_saturated_streak_still_fires_with_evidence_floor():
    """D3 — saturation + plancher de preuve + catalogue : le cas nominal passe."""
    cur = _summary(
        ("A", "B"), never=["dead-skill"], catalog=1, entries=[{"skill_id": "dead-skill"}]
    )
    hist = [cur] + [_summary(("X", "Y"), never=["dead-skill"]) for _ in range(3)]
    out = compute(
        recent_summaries=hist,
        **_retire_kwargs(cur, never_loaded_runs_threshold=4),
    )
    cats = [f["category"] for f in out["maintenance"]["findings"]]
    assert "retire-candidate" in cats


def test_retire_candidate_excludes_protected_skills():
    """D3 — `origin=user`, `ttl_policy=pin` et « never load standalone » exclus.

    Les trois protections viennent de `curation` (`_skill_fields` /
    `is_reachable_skill`) : insights n'invente pas de seconde règle. La branche
    « never load standalone » s'active dès que l'entrée de catalogue porte une
    `description`.
    """
    entries = [
        {"skill_id": "user-skill", "metadata": {"origin": "user"}},
        {"skill_id": "pinned-skill", "metadata": {"ttl_policy": "pin"}},
        {
            "skill_id": "guardrails",
            "metadata": {
                "description": "Never load standalone. Shared guardrails loaded by branch skills."
            },
        },
        {"skill_id": "truly-dead"},
    ]
    never = ["user-skill", "pinned-skill", "guardrails", "truly-dead"]
    cur = _summary(("A", "B"), never=never, catalog=4, entries=entries)
    hist = [cur] + [_summary(("X", "Y"), never=never) for _ in range(3)]
    out = compute(recent_summaries=hist, **_retire_kwargs(cur, never_loaded_runs_threshold=4))
    hits = [
        f["description"]
        for f in out["maintenance"]["findings"]
        if f["category"] == "retire-candidate"
    ]
    assert len(hits) == 1
    assert "truly-dead" in hits[0]
    for absent in ("user-skill", "pinned-skill", "guardrails"):
        assert absent not in hits[0]


def test_retire_candidate_skips_skills_absent_from_catalog():
    """D3 — un skill jamais chargé mais absent du catalogue n'a rien à supprimer."""
    entries = [{"skill_id": "kept"}]
    never = ["kept", "ghost"]
    cur = _summary(("A", "B"), never=never, catalog=2, entries=entries)
    hist = [cur] + [_summary(("X", "Y"), never=never) for _ in range(3)]
    out = compute(recent_summaries=hist, **_retire_kwargs(cur, never_loaded_runs_threshold=4))
    hits = [
        f["description"]
        for f in out["maintenance"]["findings"]
        if f["category"] == "retire-candidate"
    ]
    assert len(hits) == 1 and "kept" in hits[0]


def test_retire_candidate_without_catalog_entries_keeps_legacy_behaviour():
    """D3 — rétro-compat : un summary sans `skill_catalog_entries` (ancien artefact).

    La fonction ne lisait pas cette clé avant D3 : la garder inerte évite de
    supprimer rétroactivement les findings des runs d'avant la publication du
    catalogue. Seule l'exigence de saturation est nouvelle.
    """
    cur = _summary(("A", "B"), never=["legacy-skill"], catalog=3)
    assert "skill_catalog_entries" not in cur
    hist = [cur] + [_summary(("X", "Y"), never=["legacy-skill"]) for _ in range(3)]
    out = compute(recent_summaries=hist, **_retire_kwargs(cur, never_loaded_runs_threshold=4))
    hits = [f for f in out["maintenance"]["findings"] if f["category"] == "retire-candidate"]
    assert len(hits) == 1 and "legacy-skill" in hits[0]["description"]
    # la télémétrie brute reste exposée pour tous les skills jamais chargés
    assert "legacy-skill" in out["maintenance"]["stats"]["never_loaded_consecutive"]


def test_retire_candidate_description_states_the_saturated_window():
    """D3 — la fiche dit « fenêtre saturée » et non « 8 runs consécutifs »."""
    never = ["dead-skill"]
    cur = _summary(("A", "B"), never=never, catalog=1)
    hist = [cur] + [_summary(("X", "Y"), never=never) for _ in range(3)]
    out = compute(recent_summaries=hist, **_retire_kwargs(cur, never_loaded_runs_threshold=4))
    hit = next(f for f in out["maintenance"]["findings"] if f["category"] == "retire-candidate")
    assert "l'ensemble des 4 runs observés" in hit["description"]
    assert "4/4 runs (fenêtre saturée)" in hit["evidence_summary"]


def test_token_risk_finding_uses_cost_cap_not_token_cap():
    """D2 — le déclencheur est `cost_usd > session_cost_cap`, plus les tokens."""
    cur = _summary(("A", "B"), cost=10.0)
    cur["top_sessions_by_cost"] = [
        {"session_id": "rich", "cost_usd": 3.0, "total_tokens": 1_000},
        {"session_id": "under", "cost_usd": 0.2, "total_tokens": 5_000_000},
    ]
    out = compute(recent_summaries=[cur], **_retire_kwargs(cur, session_cost_cap=0.5))
    hits = [f for f in out["maintenance"]["findings"] if f["category"] == "token-risk"]
    assert len(hits) == 1
    assert hits[0]["session_id"] == "rich"
    assert "coût $3.00 > cap $0.50" in hits[0]["description"]


def test_token_risk_token_volume_alone_no_longer_fires():
    """D2 — 36 M tokens sous le cap de coût ne déclenchent plus.

    Les tokens par dollar varient d'un modèle à l'autre : un cap en tokens
    classait les sessions selon le modèle qui les a produites, pas selon le
    budget. Le cap par défaut est aligné sur `session_outlier_min_cost_usd`
    (0,5 $) pour que R3 et les cost-outliers ne se contredisent plus.
    """
    cur = _summary(("A", "B"), cost=1.0)
    cur["top_sessions_by_cost"] = [
        {"session_id": "bloat", "cost_usd": 0.20, "total_tokens": 36_026_999},
    ]
    out = compute(recent_summaries=[cur], **_retire_kwargs(cur))
    hits = [f for f in out["maintenance"]["findings"] if f["category"] == "token-risk"]
    assert hits == []


def test_token_risk_session_cost_cap_is_configurable():
    cur = _summary(("A", "B"), cost=10.0)
    cur["top_sessions_by_cost"] = [{"session_id": "s", "cost_usd": 1.2, "total_tokens": 10}]
    loose = compute(recent_summaries=[cur], **_retire_kwargs(cur, session_cost_cap=2.0))
    assert [f for f in loose["maintenance"]["findings"] if f["category"] == "token-risk"] == []
    tight = compute(recent_summaries=[cur], **_retire_kwargs(cur, session_cost_cap=1.0))
    assert len([f for f in tight["maintenance"]["findings"] if f["category"] == "token-risk"]) == 1


def test_agent_loop_finding_uses_tool_fingerprint_thresholds():
    """Seuils par outil : `task` à 3 (seuil 3) et `read` à 2 (seuil 2) déclenchent.

    Un seul constat agrégé par run (la règle `agent-loop` porte les seuils, plus
    le détecteur Python qui en portait un par entrée) : les outils sont donc
    énumérés dans `examples`, et les seuils effectifs dans le texte de la règle.
    """
    cur = _summary(("A", "B"))
    cur["tool_argument_fingerprints"] = {"task": {"fp": 3}, "read": {"fp": 2}}
    cur["tool_result_fingerprints"] = {"task": {"empty": 3}}
    out = compute(
        run_time=RUN,
        current_summary=cur,
        previous_summary=None,
        current_digest=None,
        previous_digest=None,
        recent_summaries=[cur],
        insights_cfg=_cfg(),
        ignored_findings=[],
        loop_min_repeats=2,
        loop_task_min_repeats=3,
    )
    loops = [f for f in out["maintenance"]["findings"] if f["category"] == "agent-loop"]
    assert len(loops) == 1
    # scope trié par nom d'outil : read avant task
    # (peak = pic intra-session ; ce résumé porte des buckets entiers, d'où
    #  max_peak == max_total — même déclenchement qu'avant le pic)
    assert loops[0]["evidence_summary"] == (
        "rule=agent-loop; max_peak=3; max_total=3; task_loops=1; examples=read|task"
    )


def _agent_loops(cur, **over):
    out = compute(
        run_time=RUN,
        current_summary=cur,
        previous_summary=None,
        current_digest=None,
        previous_digest=None,
        recent_summaries=[cur],
        insights_cfg=_cfg(),
        ignored_findings=[],
        **over,
    )
    return [f for f in out["maintenance"]["findings"] if f["category"] == "agent-loop"]


def test_agent_loop_ignores_constant_tool_output_with_distinct_arguments():
    """B1 : même sortie constante + arguments DIFFÉRENTS ⇒ aucun finding.

    `raw_output` est constant par outil (`edit` → 'Edit applied successfully.',
    `write` → 'Wrote file successfully.') : le canal résultat ne doit pas pouvoir
    déclencher une boucle. Régression du run 2026-10-03 (7 des 9 findings agent-loop).
    """
    cur = _summary(("A", "B"))
    cur["tool_argument_fingerprints"] = {
        "edit": {"path-a": 1, "path-b": 1, "path-c": 1},
        "write": {"path-a": 1, "path-b": 1},
    }
    # 3 appels edit et 2 appels write, tous sur le MÊME bucket de résultat.
    cur["tool_result_fingerprints"] = {
        "edit": {"sha256-constant-edit-output": 3},
        "write": {"sha256-constant-write-output": 2},
    }
    assert _agent_loops(cur, loop_min_repeats=2, loop_task_min_repeats=3) == []


def test_agent_loop_result_channel_cannot_inflate_repeats():
    """B1 : le canal résultat ne peut pas accroître un bucket d'arguments sous le seuil."""
    cur = _summary(("A", "B"))
    cur["tool_argument_fingerprints"] = {"glob": {"glob-a": 2, "glob-b": 2}}
    cur["tool_result_fingerprints"] = {"glob": {"sha256-constant": 76}}
    loops = _agent_loops(cur, loop_min_repeats=8, loop_task_min_repeats=3)
    assert loops == []


def test_agent_loop_replay_2026_10_03_flags_read_and_skill_only():
    """B1 : rejoue la distribution réelle du run 2026-10-03 (plan §3.2), seuil 8.

    Les buckets d'arguments sont parfaits (1 par appel pour `edit`), seul le canal
    résultat est dégénéré. Attendus : `read` 11 et `skill` 22 — les 7 autres outils
    tombent sous le seuil une fois le canal résultat retiré du calcul. La règle
    `agent-loop` remonte UN constat agrégé par run, pas un par outil.
    """
    # (outil, n_buckets_args, max_args, n_buckets_result, max_result)
    distribution = [
        ("edit", 344, 1, 1, 341),
        ("write", 76, 1, 1, 76),
        ("apply_patch", 36, 1, 2, 18),
        ("glob", 140, 4, 72, 76),
        ("read", 1079, 11, 1074, 9),
        ("bash", 1722, 5, 1633, 55),
        ("skill", 30, 22, 12, 5),
    ]

    def spread(n_buckets, peak):
        return {"p0": peak, **{f"b{i}": 1 for i in range(1, n_buckets)}}

    cur = _summary(("2026-09-26", "2026-10-03"))
    cur["tool_argument_fingerprints"] = {
        tool: spread(na, ma) for tool, na, ma, _nr, _mr in distribution
    }
    cur["tool_result_fingerprints"] = {
        tool: spread(nr, mr) for tool, _na, _ma, nr, mr in distribution
    }
    loops = _agent_loops(cur)  # seuils par défaut : 8 / 3
    assert len(loops) == 1
    assert loops[0]["evidence_summary"] == (
        "rule=agent-loop; max_peak=22; max_total=22; task_loops=0; examples=read|skill"
    )


# ============================================================ D5 — taux d'exclusion


def _summary_with_selection(selection: dict, warnings=None) -> dict:
    summary = _summary(("2026-08-05", "2026-08-12"))
    summary["selection"] = selection
    summary["warnings"] = warnings if warnings is not None else []
    return summary


def _compute(current: dict):
    return compute(
        run_time=RUN,
        current_summary=current,
        previous_summary=None,
        current_digest=None,
        previous_digest=None,
        recent_summaries=[current],
        insights_cfg=_cfg(),
        ignored_findings=[],
    )


def test_exclusion_rate_alert_on_observed_run_shape():
    """D5 : 53/178 = 29,8 % → alerte medium, seuil 0,20 (run réel 2026-10-03)."""
    current = _summary_with_selection(
        {
            "window_touched": 178,
            "counted": 66,
            "excluded_active": 53,
            "excluded_no_activity": 5,
            "excluded_unflushed": 11,
        },
        warnings=[
            # Warnings déjà agrégés par l'agrégateur : count=50, pas 50 lignes.
            {
                "session_id": "ses_first",
                "message": "session active exclue des totaux (télémétrie incomplète)",
                "partial": False,
                "count": 50,
                "session_ids": ["ses_first"],
            },
            {"session_id": "ses_x", "message": "orphan child session", "partial": False},
        ],
    )

    alert = next(a for a in _compute(current)["alerts"] if a["rule"] == "exclusion_rate")

    assert alert["severity"] == "medium"
    assert alert["threshold"] == EXCLUSION_RATE_MAX_DEFAULT
    assert alert["observed"] == round(53 / 178, 4)
    assert alert["note"] == "53 active(s), 5 no-activity, 11 unflushed sur 178 touchée(s)"
    # mécanisme de regroupement remonté au niveau de l'alerte
    assert alert["repeated_warnings"] == [
        {"message": "session active exclue des totaux (télémétrie incomplète)", "count": 50}
    ]


def test_exclusion_rate_silent_on_empty_window():
    """D5 : fenêtre vide → pas de division par zéro, pas d'alerte."""
    current = _summary_with_selection({"window_touched": 0, "excluded_active": 0})
    assert not any(a["rule"] == "exclusion_rate" for a in _compute(current)["alerts"])


def test_exclusion_rate_silent_below_threshold():
    current = _summary_with_selection({"window_touched": 178, "excluded_active": 10})
    assert not any(a["rule"] == "exclusion_rate" for a in _compute(current)["alerts"])


def test_exclusion_rate_silent_without_selection():
    """Pas de `selection` (summary ancien / fixture) → muet, pas d'alerte parasite."""
    current = _summary(("2026-08-05", "2026-08-12"))
    assert "selection" not in current
    assert not any(a["rule"] == "exclusion_rate" for a in _compute(current)["alerts"])


def test_exclusion_rate_threshold_override():
    """Seuil surchargeable : lu sur InsightsConfig quand l'attribut existe.

    `InsightsConfig` est `slots=True`, le setter est donc refusé en test — on
    teste la branche de surcharge via un stub sur l'appel pur.
    """
    from types import SimpleNamespace

    from weekly_telemetry_aggregator.insights import _exclusion_rate_alert

    summary = _summary_with_selection({"window_touched": 100, "excluded_active": 10})
    alert = _exclusion_rate_alert(summary, SimpleNamespace(exclusion_rate_max=0.05))

    assert alert["threshold"] == 0.05
    assert alert["observed"] == 0.1
    assert "repeated_warnings" not in alert  # aucun warning répété → clé absente


# ================================================ moteur de règles déclaratif (P1)
#
# Les trois détecteurs Python (`_daily_spike_alerts`, `_agent_loop_findings`,
# `_architecture_drift_finding`) ont été supprimés : ce sont désormais les
# fichiers `rules/*.md` qui décident, via rule_loader/rule_pipeline/rule_context.
# Ces tests vérifient le CÂBLAGE (le contrat de sortie reste identique pour
# `report.py`) — pas la logique des règles, déjà couverte par `test_rules.py`.


def _rule_compute(
    cur, *, prev=None, recent=None, ignored=(), cfg_over=None, quality_findings=None, **over
):
    """`compute()` avec le jeu d'arguments usuel, paramétrable.

    `cfg_over` tune `InsightsConfig`, le reste passe tel quel à `compute()`
    (`architecture_drift_runs`, …).
    """
    return compute(
        run_time=RUN,
        current_summary=cur,
        previous_summary=prev,
        current_digest=None,
        previous_digest=None,
        recent_summaries=recent if recent is not None else [cur] + ([prev] if prev else []),
        insights_cfg=_cfg(**(cfg_over or {})),
        ignored_findings=list(ignored),
        quality_findings=quality_findings,
        **over,
    )


def _rule_findings(out, category):
    return [f for f in out["maintenance"]["findings"] if f["category"] == category]


def _calm_week():
    """Semaine tranquille : rien ne doit déclencher, ni alerte ni constat."""
    cur = _summary(("A", "B"), cost=1.0, cache=0.9)
    prev = _summary(("C", "D"), cost=1.0, cache=0.9)
    # sans ça, la règle cache_write_zero (Python, 2 runs) alerterait aussi
    for summary in (cur, prev):
        summary["totals"]["cache_write_tokens"] = 1000
    cur["tool_argument_fingerprints"] = {
        "read": {f"read-fp-{i}": 1 for i in range(30)},
        "grep": {"grep-fp-1": 2},
        "task": {"task-fp-1": 1},
    }
    cur["architecture_observations"] = prev["architecture_observations"] = {
        "state_counts": {"declared": 2, "observed": 0, "absent": 0, "unknown": 0},
        "config": {"available": True, "valid": True},
    }
    prev["daily_totals"] = [{"date": "2026-08-11", "cost_usd": 1.0}]
    cur["daily_totals"] = [{"date": "2026-08-12", "cost_usd": 1.0}]
    return cur, prev


def _mad_zero_week():
    """Semaine au MAD≈0 : le z robuste explose, la cap d'affichage doit mordre."""
    cur = _summary(("A", "B"), cost=40.0, generated_at="2026-08-15T00:00:00Z")
    cur["daily_totals"] = [{"date": "2026-08-07", "cost_usd": 39.35}] + [
        {"date": f"2026-08-{d:02d}", "cost_usd": 0.1} for d in range(1, 15)
    ]
    prev = _summary(("C", "D"), cost=10.0, generated_at="2026-08-01T00:00:00Z")
    prev["daily_totals"] = [{"date": f"2026-07-{d:02d}", "cost_usd": 0.1} for d in range(1, 32)]
    return cur, prev


def test_compute_output_shape_is_unchanged_by_the_rule_engine():
    """Contrat `report.py` : les 8 clés de premier niveau, ni plus ni moins."""
    cur, prev = _calm_week()

    out = _rule_compute(cur, prev=prev)

    assert set(out) == {
        "schema_version",
        "period",
        "previous_run_date",
        "cost_discrepancies",
        "generated_at",
        "deltas",
        "alerts",
        "maintenance",
    }
    assert set(out["maintenance"]) == {"findings", "stats"}


def test_calm_week_produces_zero_rule_findings():
    """Preuve de routage, pas seulement « la suite est verte ».

    Sans signal (pas de boucle d'outils, pas de dérive, pas de pic), le moteur
    doit être totalement muet : ni `alerts` ni `maintenance.findings` ne doivent
    porter les trois règles migrées.
    """
    cur, prev = _calm_week()

    out = _rule_compute(cur, prev=prev)

    assert not any(a["rule"] == "daily_spike" for a in out["alerts"])
    assert not _rule_findings(out, "agent-loop")
    assert not _rule_findings(out, "architecture-drift")
    # une semaine calme n'alerte sur rien du tout : budget, cache et WoW sont
    # eux aussi des détecteurs Python, et ils doivent rester eux aussi silencieux
    assert out["alerts"] == []


def test_daily_spike_routed_to_alerts_not_maintenance_findings():
    """`sink: alerts` → le pic atterrit dans `alerts`, JAMAIS dans les constats.

    C'est la raison d'être du `sink` : un pic de coût s'explique, il ne se
    répare pas — donc il ne doit jamais polluer la liste des constats de
    maintenance que `report.py` rend comme actions à faire.
    """
    from weekly_telemetry_aggregator.insights import DAILY_SPIKE_Z_CAP

    cur, prev = _mad_zero_week()

    out = _rule_compute(cur, prev=prev)

    spikes = [a for a in out["alerts"] if a["rule"] == "daily_spike"]
    assert spikes, "le pic MAD≈0 doit produire une alerte"
    # ...et rien dans les constats de maintenance
    assert not any(
        f.get("recommendation_type") == "daily-spike" for f in out["maintenance"]["findings"]
    )
    # cap d'affichage appliquée au z brut, jamais le z astronomique
    assert max(a["observed"] for a in spikes) <= DAILY_SPIKE_Z_CAP
    assert max(a["observed"] for a in spikes) == DAILY_SPIKE_Z_CAP
    assert any("MAD≈0" in a.get("note", "") for a in spikes)


def test_daily_spike_alert_keeps_the_legacy_alert_shape():
    """Les 6 clés que `report.py` et les anciens tests lisaient, toujours là."""
    cur, prev = _mad_zero_week()

    out = _rule_compute(cur, prev=prev)
    alert = next(a for a in out["alerts"] if a["rule"] == "daily_spike")

    assert set(alert) == {"rule", "threshold", "observed", "severity", "day", "note"}
    assert alert["severity"] == "medium"
    assert alert["threshold"] == _cfg().daily_spike_z_min
    assert alert["day"] == "2026-08-07"


def _three_spike_week():
    """Trois journées qui spikent, dont le z MAX n'est PAS le premier jour.

    Coûts choisis pour que ``robust_z`` (médiane + MAD sur baseline+jour) donne
    z = 3.37 / 4.72 / 7.42 : les trois passent `z_min = 3.0`, le max est le
    dernier jour. C'est exactement le cas que l'agrégation N alertes-en-1
    attribuait faux : elle publiait `day = days[0]` avec `observed = max_raw_z`.
    """
    cur = _summary(
        ("2026-09-10T00:00:00Z", "2026-09-13T00:00:00Z"),
        cost=30.0,
        generated_at="2026-09-13T00:00:00Z",
    )
    cur["daily_totals"] = [
        {"date": "2026-09-10", "cost_usd": 7.0},
        {"date": "2026-09-11", "cost_usd": 9.0},
        {"date": "2026-09-12", "cost_usd": 13.0},
    ]
    prev = _summary(
        ("2026-09-03T00:00:00Z", "2026-09-10T00:00:00Z"),
        cost=16.0,
        generated_at="2026-09-10T00:00:00Z",
    )
    prev["daily_totals"] = [
        {"date": f"2026-08-{d:02d}", "cost_usd": cost}
        for d, cost in zip(range(1, 9), [1.0, 2.0, 3.0, 2.0, 1.0, 2.0, 3.0, 2.0], strict=True)
    ]
    return cur, prev


def test_daily_spike_fans_out_one_alert_per_spiking_day_with_its_own_z():
    """UNE alerte par jour qui spike, `day` et `observed` du MÊME jour.

    Régression du défaut bloquant : la règle agrégeait les N pics en un seul et
    l'adaptateur publiait `day = days[0]` avec `observed = max_raw_z` — un z
    appartenant à un autre jour, et les N-1 autres pics disparaissaient de
    `alerts`. Le port de `_daily_spike_alerts` (une alerte par jour) est ici.
    """
    cur, prev = _three_spike_week()

    out = _rule_compute(cur, prev=prev)
    spikes = [a for a in out["alerts"] if a["rule"] == "daily_spike"]

    # (i) trois alertes, une par jour qui spike — pas une seule agrégée
    assert len(spikes) == 3
    # (ii) chaque `day` porte son PROPRE z, pas celui du max (qui est le 3e jour)
    assert [(a["day"], a["observed"]) for a in spikes] == [
        ("2026-09-10", 3.37),
        ("2026-09-11", 4.72),
        ("2026-09-12", 7.42),
    ]
    # (iii) la forme gelée est inchangée sur chaque ligne
    for alert in spikes:
        assert set(alert) == {"rule", "threshold", "observed", "severity", "day", "note"}
        assert alert["threshold"] == _cfg().daily_spike_z_min
        assert alert["severity"] == "medium"
    # aucun z n'est borné ici (max 7.42 < cap 10.0) → aucune note
    assert [a["note"] for a in spikes] == ["", "", ""]
    # le max n'est PAS attribué au premier jour : c'est le piège exact
    assert spikes[0]["observed"] != max(a["observed"] for a in spikes)


def test_daily_spike_note_reflects_each_days_own_z_against_the_cap():
    """La note « MAD≈0, z borné » se décide par le z DU JOUR, pas par le max.

    Deux jours spikent, un seul dépasse la cap : la note va sur le bon jour, et
    l'autre affiche son z réel.
    """
    from weekly_telemetry_aggregator.insights import DAILY_SPIKE_Z_CAP

    cur, prev = _three_spike_week()
    cur["daily_totals"][0]["cost_usd"] = 40.0  # z astronomique → borné
    # z du 1er jour recalculé : la baseline est dominée par 40.0, donc
    # on vérifie la propriété plutôt qu'une valeur figée.
    out = _rule_compute(cur, prev=prev)

    spikes = [a for a in out["alerts"] if a["rule"] == "daily_spike"]
    assert len(spikes) == 3
    # une seule note, et elle est sur le jour dont le z dépasse la cap
    noted = [a for a in spikes if "MAD≈0" in a["note"]]
    assert len(noted) == 1
    assert noted[0]["day"] == "2026-09-10"
    assert noted[0]["observed"] == DAILY_SPIKE_Z_CAP
    # les deux autres jours gardent leur z propre, sans note
    clean = [a for a in spikes if a["day"] != "2026-09-10"]
    assert [a["note"] for a in clean] == ["", ""]
    assert all(a["observed"] < DAILY_SPIKE_Z_CAP for a in clean)


def test_daily_spike_z_min_config_overrides_the_rule_threshold():
    """`InsightsConfig.daily_spike_z_min` est mappé sur `thresholds.z_min`.

    Preuve par l'effet observable : le même contexte spike, mais un seuil de
    config intenable doit rendre la règle muette. Sans l'override le seuil
    resterait le 3.0 figé dans le `.md`.
    """
    cur, prev = _mad_zero_week()

    loud = _rule_compute(cur, prev=prev)
    muted = _rule_compute(cur, prev=prev, cfg_over={"daily_spike_z_min": 99.0})

    assert [a for a in loud["alerts"] if a["rule"] == "daily_spike"]
    assert not [a for a in muted["alerts"] if a["rule"] == "daily_spike"]


def test_rule_thresholds_are_loaded_with_the_config_override():
    """Le mapping est visible dans les règles chargées (pas seulement en effet)."""
    from weekly_telemetry_aggregator.insights import _rule_set

    rules = {rule.id: rule for rule in _rule_set(4.5, 8, 3)}
    assert rules["daily-spike"].thresholds["z_min"] == 4.5
    # les seuils frères ne sont pas écrasés par le deep-merge
    assert rules["daily-spike"].thresholds["z_cap"] == 10.0
    assert rules["daily-spike"].thresholds["min_occurrences"] == 1
    # les knobs de boucle survivent à la migration
    assert rules["agent-loop"].thresholds["loop_min_repeats"] == 8
    retuned = {rule.id: rule for rule in _rule_set(4.5, 5, 2)}
    assert retuned["agent-loop"].thresholds["loop_min_repeats"] == 5
    assert retuned["agent-loop"].thresholds["loop_task_min_repeats"] == 2
    assert retuned["agent-loop"].thresholds["min_occurrences"] == 1


def test_agent_loop_repeats_knobs_still_retune_the_rule():
    """Preuve par l'effet : `loop_min_repeats` assouplit, il ne became pas décoratif."""
    cur = _summary(("A", "B"))
    cur["tool_argument_fingerprints"] = {"read": {"fp": 5}}

    strict = _rule_compute(cur, loop_min_repeats=8)
    loose = _rule_compute(cur, loop_min_repeats=5)

    assert not _rule_findings(strict, "agent-loop")
    assert _rule_findings(loose, "agent-loop")


def test_agent_loop_finding_keeps_the_legacy_key_set():
    """Une boucle d'outils reste un constat de maintenance aux 8 clés historiques."""
    cur = _summary(("A", "B"))
    cur["tool_argument_fingerprints"] = {"read": {"fp": 11}, "skill": {"fp": 22}}

    out = _rule_compute(cur)
    loops = _rule_findings(out, "agent-loop")

    assert len(loops) == 1
    assert set(loops[0]) == {
        "session_id",
        "category",
        "severity",
        "description",
        "evidence_summary",
        "recommendation",
        "recommendation_type",
        "impact_order_of_magnitude",
    }
    assert loops[0]["recommendation_type"] == "agent-loop"
    assert loops[0]["impact_order_of_magnitude"] == "medium"
    assert loops[0]["severity"] == "medium"
    assert loops[0]["session_id"] is None
    # les outils sont dans `examples`, le détail chiffré dans `evidence_summary`
    assert "read" in loops[0]["evidence_summary"]


def test_agent_loop_thresholds_are_per_tool_from_the_rule_file():
    """`task` à 4 déclenche (seuil 3), `grep` à 7 non (seuil 8)."""
    cur = _summary(("A", "B"))
    cur["tool_argument_fingerprints"] = {"task": {"fp": 4}, "grep": {"fp": 7}}

    out = _rule_compute(cur)

    loops = _rule_findings(out, "agent-loop")
    assert len(loops) == 1
    assert "task" in loops[0]["evidence_summary"]
    assert "grep" not in loops[0]["evidence_summary"]


def test_agent_loop_task_threshold_knob_still_retunes_the_rule():
    """`loop_task_min_repeats` n'est pas plus décoratif que `loop_min_repeats`.

    L'override touche les DEUX clés de `agent-loop` : sans cela, une boucle `task`
    à 4 resterait bloquée à 3 quel que soit le knob.
    """
    cur = _summary(("A", "B"))
    cur["tool_argument_fingerprints"] = {"task": {"fp": 4}}

    strict = _rule_compute(cur, loop_task_min_repeats=3)
    loose = _rule_compute(cur, loop_task_min_repeats=5)

    assert _rule_findings(strict, "agent-loop")  # 4 >= 3
    assert not _rule_findings(loose, "agent-loop")  # 4 < 5


def test_architecture_drift_runs_knob_reaches_the_rule_threshold_too():
    """Le knob atteint `thresholds.drift_runs`, pas seulement le scope field.

    `match` compare à `drift_threshold` (champ de scope, wiré par l'adaptateur),
    mais la description interpole `{{thresholds.drift_runs}}` : sans l'override le
    constat afficherait « défaut : 2 » pendant que le run évalue 3.
    """
    from weekly_telemetry_aggregator.insights import _rule_set

    rules = {rule.id: rule for rule in _rule_set(3.0, 8, 3, 5)}

    assert rules["architecture-drift"].thresholds["drift_runs"] == 5
    # les seuils frères ne sont pas écrasés par le deep-merge
    assert rules["architecture-drift"].thresholds["min_occurrences"] == 1


def test_prompt_min_repeats_has_no_public_knob_and_is_owned_by_the_rule_file():
    """Constat d'audit, pas un bug : `prompt_min_repeats` n'est KNOBÉ par personne.

    `TelemetryConfig.user_prompt_repeat_min` (3 par défaut) ne sert qu'à
    l'AGRÉGATEUR : il décide quels groupes entrent dans
    `summary["user_prompt_repeats"]`. Il n'est jamais transmis à `compute()` ni à
    `_rule_set()`, donc la règle garde son 4 déclaré dans le `.md`. On le fixe ici
    pour que le jour où quelqu'un câble le knob, ce test le dise.
    """
    from weekly_telemetry_aggregator.insights import _rule_set

    rules = {rule.id: rule for rule in _rule_set(3.0, 8, 3, 2)}

    assert rules["prompt-loop"].thresholds["prompt_min_repeats"] == 4.0
    # ...et ce seuil est réellement appliqué par la règle, à 3 et à 4
    cur = _summary(("A", "B"))
    cur["user_prompt_repeats"] = [{"normalized_preview": "refais le test", "count": 4}]
    assert _rule_findings(_rule_compute(cur), "prompt-loop")
    cur["user_prompt_repeats"] = [{"normalized_preview": "refais le test", "count": 3}]
    assert not _rule_findings(_rule_compute(cur), "prompt-loop")


def test_rule_alert_falls_back_for_a_rule_with_no_alert_block():
    """Filet de sécurité : une future règle `sink: alerts` se rend sans patch Python.

    Le bloc `alert:` est porté par la règle (`details["alert"]`) ; sans lui, le
    rendu dégrade proprement au lieu d'inventer un `observed` à 0.
    """
    from weekly_telemetry_aggregator.insights import _rule_alert, _signal_metric

    finding = {
        "id": "brand-new-rule",
        "severity": "medium",
        "details": {
            "metrics": {"occurrences": 2, "pct": 0.5, "worst_ratio": 4.25, "days": ["2026-08-07"]},
            "thresholds": {},
        },
    }

    alert = _rule_alert(finding)

    assert alert["rule"] == "brand_new_rule"  # tirets -> underscores, comme les autres
    assert alert["observed"] == 4.25  # la 1re mesure, pas occurrences/pct
    assert alert["threshold"] == 0.0  # aucun seuil déclaré : jamais un seuil inventé
    assert alert["note"] == ""  # 4.25 sous la cap par défaut
    assert alert["day"] == "2026-08-07"
    # `_signal_metric` ignore ce qui compte au lieu de mesurer
    assert _signal_metric({"occurrences": 9, "pct": 0.9, "worst": 1.5}) == 1.5
    assert _signal_metric({"occurrences": 9, "pct": 0.9}) == 0.0


def test_rule_alert_reads_the_declared_alert_block_and_names_itself_from_the_id():
    """Le rendu lit `details["alert"]` ; le nom d'alerte est l'id, jamais un alias.

    L'ancien alias `daily_spike_z_min` restait indiscernable d'une sortie legacy
    alors que le détecteur legacy est supprimé : le nom vient désormais de l'id.
    """
    from weekly_telemetry_aggregator.insights import _rule_alert

    finding = {
        "id": "daily-spike",
        "severity": "medium",
        "details": {
            "metrics": {"max_raw_z": 4.05, "days": ["2026-08-07"]},
            "thresholds": {"z_min": 3.0, "z_cap": 10.0},
            "alert": {
                "signal": "max_raw_z",
                "threshold": "z_min",
                "cap": "z_cap",
                "rows": "spikes",
                "row_signal": "raw_z",
                "row_day": "day",
            },
        },
    }

    alert = _rule_alert(finding)

    assert alert["rule"] == "daily_spike"  # tirets -> underscores, dérivé de l'id
    assert alert["observed"] == 4.05
    assert alert["threshold"] == 3.0
    assert alert["day"] == "2026-08-07"
    assert alert["note"] == ""


def test_architecture_drift_finding_keeps_the_legacy_key_set_and_is_never_an_alert():
    """Observation-only : un constat de maintenance, jamais une alerte bloquante."""
    cur = _summary(("A", "B"))
    prev = _summary(("C", "D"))
    older = _summary(("E", "F"))
    obs_prev = {
        "state_counts": {"declared": 1, "observed": 0, "absent": 0, "unknown": 0},
        "config": {"available": True, "valid": True},
    }
    cur["architecture_observations"] = {
        "state_counts": {"declared": 2, "observed": 0, "absent": 0, "unknown": 0},
        "config": {"available": True, "valid": True},
    }
    prev["architecture_observations"] = older["architecture_observations"] = obs_prev

    out = _rule_compute(cur, prev=prev, recent=[cur, prev, older])
    drift = _rule_findings(out, "architecture-drift")

    assert len(drift) == 1
    assert set(drift[0]) == {
        "session_id",
        "category",
        "severity",
        "description",
        "evidence_summary",
        "recommendation",
        "recommendation_type",
        "action",
        "observation_only",
    }
    assert drift[0]["observation_only"] is True
    assert drift[0]["action"] == "recalibrate"
    assert not any(a["rule"] == "architecture-drift" for a in out["alerts"])


def test_architecture_drift_silent_below_the_run_streak():
    """Streak = 2 (laPrevious est recomptée), seuil = 3 → muet.

    La streak est ce qui déclenche, pas la simple présence d'une dérive : un
    changement isolé qui se repeat une fois ne doit pas remonter. On monte donc
    le seuil plutôt que d'inventer un scénario sans run précédent (sans
    `previous_summary` la dérive vaut 0 par construction).
    """
    cur = _summary(("A", "B"))
    prev = _summary(("C", "D"))
    older = _summary(("E", "F"))
    cur["architecture_observations"] = {"config": {"available": True, "valid": True}}
    prev["architecture_observations"] = {"config": {"available": False, "valid": False}}
    # `older` iguale `current` → la streak s'arrête net à 2
    older["architecture_observations"] = cur["architecture_observations"]

    recent = [cur, prev, older]
    assert _rule_findings(_rule_compute(cur, prev=prev, recent=recent), "architecture-drift")
    assert not _rule_findings(
        _rule_compute(cur, prev=prev, recent=recent, architecture_drift_runs=3),
        "architecture-drift",
    )


def _evidence_examples(finding):
    """The ``examples=a|b|c`` tail of the evidence line, as a set of tools.

    Asserted separately from the metrics because the metrics are NOT trimmed by
    ``apply_finding_ignore`` (only ``examples`` and ``details["targets"]`` are):
    an ``agent-loop`` finding keeps its ``task_loops=…`` metric after ``task`` is
    ignored, so ``"task" not in evidence_summary`` would be a false assertion.
    """
    return set(finding["evidence_summary"].split("examples=")[-1].split("|"))


def test_ignored_findings_are_merged_into_extra_and_filter_by_target():
    """`extra["ignored"]` porte `cfg.ignored_findings` ; le canal visé est la CIBLE.

    `agent-loop` ne déclare plus `ignore: [task]`, donc `"agent-loop:task"` ne peut
    plus être consommé par `is_rule_ignored` : il n'atteint que le canal par cible,
    qui retire les findings dont **toutes** les cibles sont `task`. Une boucle
    `read` survit donc au même ignore qui éteignait hier la règle entière.
    Seule la forme nu (`"agent-loop"`) éteint encore la règle, avant évaluation.
    """
    cur = _summary(("A", "B"))
    # `read` à 11 (seuil 8) et `task` à 4 (seuil 3) : les deux déclenchent, dans un
    # seul finding qui porte les deux cibles.
    cur["tool_argument_fingerprints"] = {"read": {"fp": 11}, "task": {"fp": 4}}

    both = _rule_findings(_rule_compute(cur), "agent-loop")
    assert len(both) == 1
    assert _evidence_examples(both[0]) == {"read", "task"}

    # La forme cible : `task` sort de la preuve, `read` reste → le finding survit.
    trimmed = _rule_findings(_rule_compute(cur, ignored=["agent-loop:task"]), "agent-loop")
    assert len(trimmed) == 1
    assert _evidence_examples(trimmed[0]) == {"read"}

    # La forme BARE atteint le même canal (les deux formes sont acceptées).
    bare = _rule_findings(_rule_compute(cur, ignored=["task"]), "agent-loop")
    assert len(bare) == 1
    assert _evidence_examples(bare[0]) == {"read"}

    # Un finding qui ne porte QUE `task` disparaît : plus aucune cible survivante.
    lone = _summary(("A", "B"))
    lone["tool_argument_fingerprints"] = {"task": {"fp": 4}}
    assert _rule_findings(_rule_compute(lone), "agent-loop")
    assert not _rule_findings(_rule_compute(lone, ignored=["agent-loop:task"]), "agent-loop")

    # La forme nu éteint toujours la règle ENTIÈRE, et avant évaluation.
    assert not _rule_findings(_rule_compute(cur, ignored=["agent-loop"]), "agent-loop")


def test_agent_loop_declares_no_rule_level_ignore_target():
    """Le reliquat est fermé : `agent-loop` n'a plus de cible d'ignore par règle.

    Tant que le frontmatter portait `ignore: [task]`, `"agent-loop:task"` était
    consommé par `is_rule_ignored` et la règle disparaissait avant d'être évaluée —
    aucun autre outil n'était alors filtrable. Ce test épingle l'absence de la clé
    pour que la régression soit visible sans repasser par les effets.
    """
    from weekly_telemetry_aggregator.rule_loader import load_rules

    rule = next(rule for rule in load_rules() if rule.id == "agent-loop")

    assert rule.ignore == []


def test_ignored_target_trims_only_the_agent_loop_targets_it_names():
    """`agent-loop:<outil>` passe par le canal CIBLE, la règle elle-même survit.

    Contrepartie du test précédent : le filtre réduit la preuve aux outils encore
    signalés au lieu d'éteindre la règle. Toutes les cibles d'`agent-loop` suivent
    ce chemin — `task` comprise, depuis que son entrée de frontmatter a été retirée.
    En ignorant `agent-loop:read`, le constat `agent-loop` reste publié — réduit à
    `skill`. C'est `rule_pipeline.evaluate_rule` qui applique ce filtre, donc le
    chemin de production (`_production_rule_results`, qui passe par le point
    d'entrée batch `evaluate_rules`) en bénéficie sans câblage particulier.
    """
    cur = _summary(("A", "B"))
    cur["tool_argument_fingerprints"] = {"read": {"fp": 12}, "skill": {"fp": 22}}

    loops = _rule_findings(_rule_compute(cur, ignored=["agent-loop:read"]), "agent-loop")

    assert len(loops) == 1
    assert "read" not in loops[0]["evidence_summary"]
    assert "skill" in loops[0]["evidence_summary"]
    # `details["targets"]` ne fuit pas dans la forme du constat de maintenance
    assert set(loops[0]) == {
        "session_id",
        "category",
        "severity",
        "description",
        "evidence_summary",
        "recommendation",
        "recommendation_type",
        "impact_order_of_magnitude",
    }


def test_ignoring_every_agent_loop_target_removes_the_finding():
    """Le finding tombe quand plus aucune de ses cibles n'est signalée."""
    cur = _summary(("A", "B"))
    cur["tool_argument_fingerprints"] = {"read": {"fp": 12}, "skill": {"fp": 22}}

    assert _rule_findings(_rule_compute(cur), "agent-loop")
    assert not _rule_findings(
        _rule_compute(cur, ignored=["agent-loop:read", "agent-loop:skill"]), "agent-loop"
    )


def test_architecture_drift_ignore_target_drops_the_rule():
    cur = _summary(("A", "B"))
    prev = _summary(("C", "D"))
    older = _summary(("E", "F"))
    cur["architecture_observations"] = {"config": {"available": False, "valid": False}}
    prev["architecture_observations"] = older["architecture_observations"] = {
        "config": {"available": True, "valid": True}
    }

    kept = _rule_compute(cur, prev=prev, recent=[cur, prev, older])
    muted = _rule_compute(
        cur, prev=prev, recent=[cur, prev, older], ignored=["architecture-drift:observation_only"]
    )

    assert _rule_findings(kept, "architecture-drift")
    assert not _rule_findings(muted, "architecture-drift")


def test_spike_baseline_days_still_exposed_in_stats():
    """`spike_baseline_days` survit : il vient désormais de l'adaptateur.

    Plus de double parcours de la baseline (l'ancien `_spike_baseline` est
    supprimé) — le compteur est lu dans `extra["baseline_days"]`.
    """
    from weekly_telemetry_aggregator.insights import _production_rule_results

    cur, prev = _mad_zero_week()

    # 31 journées non nulles dans le summary précédent
    _, _, extra = _production_rule_results(
        current_summary=cur,
        previous_summary=prev,
        recent_summaries=[cur, prev],
        insights_cfg=_cfg(),
        ignored_findings=[],
    )
    assert extra["baseline_days"] == 31
    assert _rule_compute(cur, prev=prev)["maintenance"]["stats"]["spike_baseline_days"] == 31


def test_quality_findings_scope_reaches_the_engine_through_compute():
    """Le scope `findings` est câblé : un finding anti-learning déclenche la règle.

    Preuve que le moteur n'est pas un jouet : une règle déclarée uniquement dans
    un `.md` produit un constat de maintenance sans une ligne de Python dédiée.
    """
    cur = _summary(("A", "B"))

    out = _rule_compute(
        cur,
        quality_findings={
            "findings": [
                {
                    "session_id": "ses_1",
                    "category": "quality",
                    "description": "flaky test only in staging",
                }
            ]
        },
    )

    anti = _rule_findings(out, "anti-learning")
    assert anti and anti[0]["recommendation_type"] == "anti-learning"


def test_malformed_quality_findings_degrade_to_an_empty_scope():
    """Un audit absent/illisible ne doit jamais faire échouer le run."""
    for payload in (None, {}, {"findings": None}, {"findings": "nope"}, [1, 2, 3]):
        out = _rule_compute(_summary(("A", "B")), recent=[], quality_findings=payload)
        assert not _rule_findings(out, "anti-learning")


def test_run_reads_the_quality_findings_file_from_disk(tmp_path: Path):
    """Chaîne complète : `run()` charge le fichier d'audit et le passe au moteur."""
    from weekly_telemetry_aggregator.config import TelemetryConfig

    date = "2026-08-12"
    summary = _summary(("2026-08-05", "2026-08-12"), cost=1.0, catalog=2)
    (tmp_path / f"weekly-summary-{date}.json").write_text(json.dumps(summary), encoding="utf-8")
    (tmp_path / f"weekly-quality-findings-{date}.json").write_text(
        json.dumps(
            {"findings": [{"session_id": "ses_1", "description": "leaked api key in the log"}]}
        ),
        encoding="utf-8",
    )
    cfg = TelemetryConfig()
    cfg.output_dir = tmp_path

    rc = run(cfg, anchor=RUN.isoformat())
    out = json.loads((tmp_path / f"weekly-insights-{date}.json").read_text(encoding="utf-8"))

    assert rc == 0
    assert _rule_findings(out, "anti-learning")


def test_run_without_audit_file_still_evaluates_the_other_rules(tmp_path: Path):
    """Pas d'audit → le run continue, les autres règles s'évaluent quand même."""
    from weekly_telemetry_aggregator.config import TelemetryConfig

    date = "2026-08-12"
    summary = _summary(("2026-08-05", "2026-08-12"), cost=50.0, catalog=2)
    summary["tool_argument_fingerprints"] = {"read": {"fp": 12}}
    (tmp_path / f"weekly-summary-{date}.json").write_text(json.dumps(summary), encoding="utf-8")
    cfg = TelemetryConfig()
    cfg.output_dir = tmp_path

    rc = run(cfg, anchor=RUN.isoformat())
    out = json.loads((tmp_path / f"weekly-insights-{date}.json").read_text(encoding="utf-8"))

    assert rc == 0
    assert _rule_findings(out, "agent-loop")


# ==================================================== isolation par règle (P2)
#
# Le moteur lève `RuleError` sur une règle mal écrite (DSL, scope, sink) — mais une
# règle déclarative est du `.md` éditable et le DSL ne normalise pas TOUT : une
# expression comme `text() + 1` remonte une `TypeError` brute de `_eval_BinOp`.
# Appelé en bloc, l'exception tuait l'étape `insights` ENTIÈRE — une seule règle
# cassée supprimait le rapport de la semaine. `rule_pipeline.evaluate_rules` attrape
# par règle et rend un constat `rule-error` : le rayon d'action de l'échec est la
# règle.
#
# Ces 7 tests vérifient le CÂBLAGE et le RENDU dans l'artifact `insights` (forme
# maintenance à 7 clés, `evidence_summary`). Le comportement du batch lui-même —
# isolation, tri, forme des `details.metrics` — est pinné dans `test_rules.py`,
# où vit désormais l'API.
#
# Le contexte vient de `build_context` (l'adaptateur de production), jamais d'un
# contexte fabriqué à la main : ces tests vérifient le CÂBLAGE, pas le DSL (déjà
# couvert par `test_rules.py` / `test_rule_context.py`).


def _broken_rule(rule_id: str = "aaa-broken", *, source: str = "broken-loop.md"):
    """Un `Rule` dont l'expression `aggregate` est inévaluable → `RuleError`.

    `source` alimente `Rule.source_path` (le fichier dont le diagnostic doit citer le
    nom) ; `source=""` simule une règle sans chemin de source.
    """
    from weekly_telemetry_aggregator.insights import _rule_set

    base = next(rule for rule in _rule_set(4.5, 8, 3, 2) if rule.id == "agent-loop")
    return replace(
        base,
        id=rule_id,
        source_path=f"/rules/{source}" if source else "",
        detect={
            "scan": "sessions",
            "match": "true",
            "aggregate": {"max_x": "nope(1)"},
            "check": {"max_x": "> 0"},
        },
    )


def _rules_with(*extra):
    """Le jeu livré avec `extra` en TÊTE — donc évalués en premier, avant lui."""
    from weekly_telemetry_aggregator.insights import _rule_set

    return extra + tuple(_rule_set(4.5, 8, 3, 2))


def _patch_rules(monkeypatch, rules):
    monkeypatch.setattr(
        "weekly_telemetry_aggregator.insights._rule_set", lambda *a, **k: rules, raising=True
    )


def _loop_week():
    """Semaine qui déclenche `agent-loop` (fp identicales en série)."""
    cur = _summary(("A", "B"))
    cur["tool_argument_fingerprints"] = {"read": {"fp": 12}}
    return cur


def test_a_broken_rule_degrades_to_one_rule_error_finding(monkeypatch):
    """La règle qui lève devient UN constat `rule-error` — le run ne meurt plus."""
    _patch_rules(monkeypatch, _rules_with(_broken_rule()))

    out = _rule_compute(_loop_week())

    errors = _rule_findings(out, "rule-error")
    assert len(errors) == 1
    finding = errors[0]
    assert set(finding) == {
        "session_id",
        "category",
        "severity",
        "description",
        "evidence_summary",
        "recommendation",
        "recommendation_type",
    }
    assert finding["severity"] == "low"
    assert finding["recommendation_type"] == "rule-error"
    assert finding["session_id"] is None
    # l'opérateur doit pouvoir corriger la règle depuis le rapport seul :
    # id de règle + message du moteur dans la description ET dans l'évidence.
    assert "aaa-broken" in finding["description"]
    assert "aaa-broken" in finding["evidence_summary"]
    assert "nope" in finding["evidence_summary"]
    # le nom de fichier vient de `Rule.source_path`, pas d'un chemin inventé
    assert "broken-loop.md" in finding["evidence_summary"]
    assert finding["evidence_summary"].count("broken-loop.md") == 1


def test_a_broken_rule_does_not_contaminate_the_rules_evaluated_after_it(monkeypatch):
    """POINT CENTRAL : la règle cassée est en tête du lot, les suivantes parlent."""
    # `aaa-broken` est évalué AVANT `agent-loop` : sans le try/except par règle,
    # l'appel en bloc lèverait ici et `compute()` ne rendrait rien du tout.
    _patch_rules(monkeypatch, _rules_with(_broken_rule()))

    out = _rule_compute(_loop_week())

    assert _rule_findings(out, "rule-error"), "le diagnostic de la règle cassée est attendu"
    assert _rule_findings(out, "agent-loop"), "la règle suivante doit produire SON constat"


def test_each_broken_rule_gets_its_own_diagnostic(monkeypatch):
    """Un constat par règle en échec — jamais un seul, jamais un regroupement."""
    _patch_rules(monkeypatch, _rules_with(_broken_rule("aaa-broken"), _broken_rule("bbb-broken")))

    out = _rule_compute(_loop_week())

    errors = _rule_findings(out, "rule-error")
    assert len(errors) == 2
    assert sum("aaa-broken" in error["evidence_summary"] for error in errors) == 1
    assert sum("bbb-broken" in error["evidence_summary"] for error in errors) == 1


def test_rule_findings_stay_sorted_by_id_with_a_broken_rule_in_the_batch():
    """Le tri par id du point d'entrée batch est conservé, diagnostic compris.

    `evaluate_rules` est le SEUL point d'entrée batch : ce test vérifie qu'il est
    bien celui que le câblage de production appelle, avec le jeu de règles et
    l'adaptateur de contexte réels, et que le diagnostic y est trié comme les autres
    (sinon l'ordre du rapport dépendrait de l'ordre du `.md`).
    """
    from weekly_telemetry_aggregator.rule_context import build_context
    from weekly_telemetry_aggregator.rule_pipeline import evaluate_rules

    cur = _loop_week()
    context, extra = build_context(cur)
    extra["ignored"] = []

    ids = [
        finding["id"]
        for finding in evaluate_rules(_rules_with(_broken_rule()), context, extra=extra)
    ]

    assert "rule-error" in ids, "le lot doit contenir le diagnostic, sinon le test est creux"
    assert ids == sorted(ids)


def test_a_healthy_rule_set_produces_no_rule_error_finding():
    """Zéro diagnostic sur un jeu sain : le renderer n'invente pas de `rule-error`.

    Contrepartie de l'isolation : `evaluate_rules` n'émet un diagnostic que pour une
    règle qui a réellement levé. Sans ce garde-fou, un `except Exception` trop large
    se contaminerait en bruit `low` dans le rapport de chaque semaine.
    """
    out = _rule_compute(_loop_week())

    assert not _rule_findings(out, "rule-error")
    assert _rule_findings(out, "agent-loop"), "le jeu sain doit bien produire ses constats"


def test_an_ignored_broken_rule_produces_no_diagnostic(monkeypatch):
    """Le filtre d'ignore passe AVANT l'évaluation : une règle ignorée ne coûte rien."""
    _patch_rules(monkeypatch, _rules_with(_broken_rule("aaa-broken")))

    out = _rule_compute(_loop_week(), ignored=["aaa-broken"])

    assert not _rule_findings(out, "rule-error")
    assert _rule_findings(out, "agent-loop")


def test_a_broken_rule_without_a_source_path_still_names_itself():
    """`source_path` vide → l'évidence retombe sur l'id, sans `source=` creux."""
    from weekly_telemetry_aggregator.insights import _rule_maintenance_finding
    from weekly_telemetry_aggregator.rule_loader import RuleError
    from weekly_telemetry_aggregator.rule_pipeline import rule_error_finding

    # le diagnostic passe par le rendu de production, comme en run
    finding = _rule_maintenance_finding(
        rule_error_finding(_broken_rule("aaa-broken", source=""), RuleError("boom from engine"))
    )

    assert "source=" not in finding["evidence_summary"]
    assert "aaa-broken" in finding["description"]
    assert "aaa-broken" in finding["evidence_summary"]
    assert "boom from engine" in finding["evidence_summary"]
