"""Config loading (Part 1 §6) + summary serialization (writer, schema v2)."""

from __future__ import annotations

import json
from pathlib import Path

from helpers import make_step, make_usage, tzutc

from weekly_telemetry_aggregator.aggregator import aggregate
from weekly_telemetry_aggregator.config import load_config
from weekly_telemetry_aggregator.models import Period
from weekly_telemetry_aggregator.writer import summary_to_dict


def test_config_defaults(tmp_path: Path):
    # Pure built-in defaults — NOT load_config(None), which in the repo cwd
    # resolves the implicit weekly-advisor/weekly-telemetry-config.json.
    from weekly_telemetry_aggregator.config import TelemetryConfig

    cfg = TelemetryConfig()
    assert cfg.lookback_days == 7
    assert cfg.top_sessions_limit == 5
    assert cfg.output_dir == Path("~/opencode-weekly-reports").expanduser()
    assert cfg.window_hours() == 7 * 24


def test_config_json_overrides(tmp_path: Path):
    conf = tmp_path / "config.json"
    conf.write_text(
        json.dumps(
            {
                "project_root": "/x/proj",
                "opencode_db_path": "/x/db/opencode.db",
                "lookback_days": 3,
                "output_dir": "~/reports",
                "top_sessions_limit": 2,
                "include_subagents": False,
                "advisor_run_title": "Revue X",
                "insights": {"weekly_budget_usd": 10, "daily_spike_z_min": 2.2},
                "audit": {"cost_per_active_minute_min": 0.9},
            }
        )
    )
    cfg = load_config(conf)
    assert cfg.project_root == Path("/x/proj")
    assert cfg.opencode_db_path == "/x/db/opencode.db"
    assert cfg.lookback_days == 3
    assert cfg.output_dir == Path("~/reports").expanduser()
    assert cfg.top_sessions_limit == 2
    assert cfg.include_subagents is False
    assert cfg.advisor_run_title == "Revue X"
    assert cfg.insights.weekly_budget_usd == 10
    assert cfg.insights.daily_spike_z_min == 2.2
    assert cfg.audit.cost_per_active_minute_min == 0.9


def test_summary_to_dict_schema_v2_fields():
    period = Period(start=tzutc(2026, 8, 5), end=tzutc(2026, 8, 12))
    usage = make_usage(
        "r",
        [make_step("r", tzutc(2026, 8, 6, 10), cost=0.5, cache_read=10, fresh=5)],
        title="Sujet",
        agent="docs",
        tools={"read": 1},
        skills={"graphify": 1},
        user_turns=["/optimize x", "salut"],
    )
    summary = aggregate([usage], period=period, generated_at=period.end, skill_catalog=["graphify"])
    data = summary_to_dict(summary)

    assert data["schema_version"] == 2
    assert set(data) >= {
        "daily_totals",
        "by_model",
        "top_sessions_by_cost",
        "cost_outliers",
        "tool_usage",
        "skill_usage",
        "command_usage",
        "skill_similar_pairs",
        "skills_never_loaded",
        "user_prompt_repeats",
        "subagent_totals",
        "warnings",
    }
    # totals have the correct cache formula + no cost_per_minute in top sessions
    assert data["totals"]["cache_hit_rate"] == round(10 / 15, 6)
    assert "cost_per_active_minute" in data["top_sessions_by_cost"][0]
    assert "cost_per_minute" not in data["top_sessions_by_cost"][0]
    # top session flag: declares subagents only when merged
    assert data["top_sessions_by_cost"][0]["includes_subagents"] is False


def test_summary_to_dict_deterministic_order():
    period = Period(start=tzutc(2026, 8, 5), end=tzutc(2026, 8, 12))
    a = make_usage("a", [make_step("a", period.start, cost=2.0)])
    b = make_usage("b", [make_step("b", period.start, cost=3.0)])
    data = summary_to_dict(aggregate([a, b], period=period, generated_at=period.end))
    # top_sessions_by_cost ordered cost DESC → b first
    assert data["top_sessions_by_cost"][0]["session_id"] == "b"
    # by_model sorted alphabetically
    models = [m["model"] for m in data["by_model"]]
    assert models == sorted(models)


def test_summary_to_dict_v528_new_fields():
    period = Period(start=tzutc(2026, 8, 5), end=tzutc(2026, 8, 12))
    u = make_usage("r", [make_step("r", period.start, cost=0.5, cache_read=10, fresh=5)])
    u.reported_cost_usd_lifetime = 0.6
    data = summary_to_dict(aggregate([u], period=period, generated_at=period.end))
    assert data["cost_outliers_state"] == "skipped:small-sample"
    assert data["by_model"][0]["cache_hit_rate"] == round(10 / 15, 6)
    assert data["top_sessions_by_cost"][0]["reported_cost_usd_lifetime"] == 0.6


def test_summary_serializes_skills_targets():
    from helpers import make_step, make_usage, tzutc
    from weekly_telemetry_aggregator.aggregator import aggregate
    from weekly_telemetry_aggregator.models import Period, SkillCatalogEntry
    from weekly_telemetry_aggregator.writer import summary_to_dict

    period = Period(start=tzutc(2026, 8, 5), end=tzutc(2026, 8, 12))
    u = make_usage("r", [make_step("r", tzutc(2026, 8, 6), cost=0.1)])
    entries = [
        SkillCatalogEntry(
            name="jira-to-code-audit", description="d", body="b", target_agents=["java-pro"]
        )
    ]
    summary = aggregate(
        [u],
        period=period,
        generated_at=period.end,
        skill_catalog=["jira-to-code-audit"],
        skill_catalog_entries=entries,
    )
    data = summary_to_dict(summary)
    assert data["skills_targets"] == {"jira-to-code-audit": ["java-pro"]}
