"""Pure rule computation (Part 6) — deltas, alerts, maintenance R1-R4."""

from __future__ import annotations

import json
from pathlib import Path

from helpers import tzutc

from weekly_telemetry_aggregator.config import InsightsConfig
from weekly_telemetry_aggregator.insights import compute, flatten_harness_findings, run


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
):
    return {
        "period": {"start": period[0], "end": period[1]},
        "totals": {"total_cost_usd": cost, "cache_hit_rate": cache, "total_tokens": tokens},
        "skill_usage": [{"skill": s} for s in skills],
        "skills_never_loaded": list(never),
        "skill_similar_pairs": list(pairs),
        "daily_totals": list(daily),
        "skill_catalog_count": catalog,
        "generated_at": generated_at or (period[1] + "T00:00:00Z"),
    }


RUN = tzutc(2026, 8, 12)


def _cfg(**over):
    cfg = InsightsConfig()
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


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
        insights_cfg=_cfg(session_token_cap=4_000_000),
        ignored_findings=[],
    )
    cats = [f["category"] for f in out["maintenance"]["findings"]]
    assert "token-risk" in cats


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
    spikes = [a for a in out["alerts"] if a["rule"] == "daily_spike_z_min"]
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
                "cost_usd": 0.0,
                "total_tokens": 100,
                "cache_hit_rate": 0.9,
            }
            for d in range(1, 7)
        ]
        + [
            {
                "date": f"2026-08-{d:02d}",
                "cost_usd": 0.0,
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
        {"date": f"2026-07-{d:02d}", "cost_usd": 0.0, "total_tokens": 100, "cache_hit_rate": 0.9}
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
    spikes = [a for a in out["alerts"] if a["rule"] == "daily_spike_z_min"]
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


def _retire_kwargs(cur):
    return dict(
        run_time=RUN,
        current_summary=cur,
        previous_summary=None,
        current_digest=None,
        previous_digest=None,
        insights_cfg=_cfg(),
        ignored_findings=[],
    )


def test_retire_candidate_default_threshold_frozen():
    """Fige le comportement : seuil par défaut never_loaded = 8 runs (doublé, ex-4)."""
    from dataclasses import fields

    defaults = {f.name: f.default for f in fields(InsightsConfig)}
    assert defaults["never_loaded_runs_threshold"] == 8
    cur = _summary(("A", "B"), never=["dead-skill"], catalog=3)
    recents7 = [cur] + [_summary(("X", "Y"), never=["dead-skill"]) for _ in range(6)]
    below = compute(recent_summaries=recents7, **_retire_kwargs(cur))
    cats_below = [f["category"] for f in below["maintenance"]["findings"]]
    assert "retire-candidate" not in cats_below
    recents8 = recents7 + [_summary(("X", "Y"), never=["dead-skill"])]
    at = compute(recent_summaries=recents8, **_retire_kwargs(cur))
    cats_at = [f["category"] for f in at["maintenance"]["findings"]]
    assert "retire-candidate" in cats_at
