"""CLI dispatch tests (subcommands, default run, end-to-end run)."""

from __future__ import annotations

import json
from pathlib import Path

from helpers import active_run_file, seed_v1_file, tzutc

from weekly_telemetry_aggregator.cli import build_parser, main

RUN_TIME = tzutc(2026, 8, 12)


def _write_config(tmp_path: Path, db_path: Path) -> Path:
    conf = tmp_path / "config.json"
    conf.write_text(
        json.dumps(
            {
                "output_dir": str(tmp_path),
                "opencode_db_path": str(db_path),
                "project_root": str(tmp_path),
            }
        ),
        encoding="utf-8",
    )
    return conf


def _seed(tmp_path: Path) -> Path:
    sessions = []
    for i in range(16):
        ts = RUN_TIME - __import__("datetime").timedelta(minutes=60 + i)
        sessions.append(
            {
                "id": f"s{i:03d}",
                "title": f"T{i}",
                "start": ts,
                "updated": ts,
                "steps": [{"ts": ts, "cost": 0.1}],
            }
        )
    db = tmp_path / "opencode.db"
    seed_v1_file(db, sessions)
    return db


def test_default_command_is_run():
    args = build_parser().parse_args([])
    assert args.command == "run"
    assert callable(args.func)


def test_run_is_default(tmp_path: Path):
    db = _seed(tmp_path)
    conf = _write_config(tmp_path, db)
    rc = main(["--config", str(conf), "--anchor", RUN_TIME.isoformat()])
    assert rc in (0, 1)
    assert active_run_file(tmp_path, "weekly-summary-2026-08-12.json").exists()


def test_run_subcommand_explicit(tmp_path: Path):
    db = _seed(tmp_path)
    conf = _write_config(tmp_path, db)
    rc = main(["--config", str(conf), "--anchor", RUN_TIME.isoformat(), "run"])
    assert rc in (0, 1)


def test_show_session_renders_transcript(tmp_path: Path, capsys):
    db = tmp_path / "opencode.db"
    ts = RUN_TIME - __import__("datetime").timedelta(hours=2)
    seed_v1_file(
        db,
        [
            {
                "id": "ses_x",
                "title": "X",
                "start": ts,
                "updated": ts,
                "steps": [{"ts": ts, "cost": 0.1}],
                "texts": [{"ts": ts, "text": "bonjour monde"}],
                "tools": [
                    {
                        "ts": ts + __import__("datetime").timedelta(seconds=1),
                        "name": "read",
                        "input": {"file": "a.py"},
                        "output": "contenu",
                    }
                ],
            }
        ],
    )
    conf = _write_config(tmp_path, db)
    rc = main(["show-session", "ses_x", "--config", str(conf)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "user: bonjour monde" in out
    assert "tool: read" in out


def test_doctor_and_self_cost_dispatch(tmp_path: Path, capsys):
    db = _seed(tmp_path)
    conf = _write_config(tmp_path, db)
    rc = main(["doctor", "--config", str(conf)])
    assert rc in (0, 1, 2)
    assert "doctor" in capsys.readouterr().out.lower() or rc == 0


def test_harness_and_blocks_draft_subcommands_parse():
    parser = build_parser()
    args = parser.parse_args(["harness", "--config", "nope.json"])
    assert callable(args.func)
    args = parser.parse_args(["report-blocks-draft", "--config", "nope.json"])
    assert callable(args.func)


def test_insights_baseline_summary_flag():
    parser = build_parser()
    args = parser.parse_args(["insights", "--baseline-summary", "x.json", "--config", "nope.json"])
    assert args.baseline_summary == "x.json"


# ============================================================ v6.0.b (lookback-days)


def test_lookback_days_flag_before_and_after_subcommand():
    """v6.0.b : override de run déduit du prompt — avant OU après la sous-commande."""
    parser = build_parser()
    assert parser.parse_args(["--lookback-days", "21", "run"]).lookback_days == 21
    assert parser.parse_args(["run", "--lookback-days", "21"]).lookback_days == 21
    assert parser.parse_args(["releases", "--lookback-days", "14"]).lookback_days == 14
    assert parser.parse_args(["run"]).lookback_days is None


def test_lookback_days_rejects_below_one():
    import pytest

    with pytest.raises(SystemExit):
        build_parser().parse_args(["run", "--lookback-days", "0"])
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--lookback-days", "-3", "run"])


def test_run_lookback_days_override_widens_window(tmp_path: Path):
    """v6.0.b : run --lookback-days 21 → period.start = ancre − 21 j (config jamais touchée)."""
    db = _seed(tmp_path)
    conf = _write_config(tmp_path, db)
    rc = main(
        ["--config", str(conf), "--anchor", RUN_TIME.isoformat(), "run", "--lookback-days", "21"]
    )
    assert rc in (0, 1)
    data = json.loads(
        active_run_file(tmp_path, "weekly-summary-2026-08-12.json").read_text(encoding="utf-8")
    )
    expected_start = (RUN_TIME - __import__("datetime").timedelta(days=21)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    assert data["period"]["start"] == expected_start
    # la config sur disque n'est pas modifiée (aucune édition de config, principe v6.0)
    on_disk = json.loads(conf.read_text(encoding="utf-8"))
    assert "lookback_days" not in on_disk


# ============================================================ v5.29 (Parties 3/4 — candidats + extracts)


def test_show_session_extract_dir_writes_file(tmp_path: Path, capsys):
    db = _seed(tmp_path)
    conf = _write_config(tmp_path, db)
    extracts = tmp_path / "extracts"
    rc = main(["show-session", "s000", "--config", str(conf), "--extract-dir", str(extracts)])
    out = capsys.readouterr().out
    assert rc == 0
    target = extracts / "transcript-extract-s000.md"
    assert target.exists()
    assert "s000" in target.read_text(encoding="utf-8")
    assert "extract écrit" in out


def test_audit_candidates_subcommand(tmp_path: Path, capsys):
    db = _seed(tmp_path)
    conf = _write_config(tmp_path, db)
    assert main(["run", "--config", str(conf), "--anchor", RUN_TIME.isoformat()]) == 0
    rc = main(["audit-candidates", "--config", str(conf), "--anchor", RUN_TIME.isoformat()])
    out = capsys.readouterr().out
    assert rc == 0
    assert "audit-candidates:" in out
    data = json.loads(
        active_run_file(tmp_path, "weekly-audit-candidates-2026-08-12.json").read_text()
    )
    assert data["limit"] == 8
    assert len(data["audited"]) <= 8
    assert (
        data["audited"][0]["session_id"] == "s000"
    )  # top-cost (coûts identiques → session_id ASC)


def test_audit_candidates_requires_summary(tmp_path: Path, capsys):
    conf = _write_config(tmp_path, tmp_path / "nope.db")
    rc = main(["audit-candidates", "--config", str(conf), "--anchor", RUN_TIME.isoformat()])
    assert rc == 2
    assert "summary inexistante" in capsys.readouterr().err


def test_draft_candidates_subcommand(tmp_path: Path, capsys):
    db = _seed(tmp_path)
    conf = _write_config(tmp_path, db)
    (tmp_path / "weekly-quality-findings-2026-08-12.json").write_text(
        json.dumps(
            {
                "findings": [
                    {
                        "session_id": "s001",
                        "category": "c",
                        "severity": "high",
                        "recommendation_type": "skill-candidate",
                        "description": "d",
                        "recommendation": "r",
                        "evidence_summary": "e",
                        "impact_order_of_magnitude": "medium",
                    },
                    {
                        "session_id": "s002",
                        "category": "c",
                        "severity": "low",
                        "recommendation_type": "command-candidate",
                        "description": "d",
                        "recommendation": "r",
                    },
                    {
                        "session_id": "s003",
                        "category": "c",
                        "severity": "medium",
                        "recommendation_type": "prompting-habit",
                        "description": "d",
                        "recommendation": "r",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    rc = main(["draft-candidates", "--config", str(conf), "--anchor", RUN_TIME.isoformat()])
    out = capsys.readouterr().out
    assert rc == 0
    assert (
        "2 candidat(s)" in out
    )  # skill-candidate high + command-candidate low (prompting-habit exclu)
    data = json.loads((tmp_path / "weekly-draft-candidates-2026-08-12.json").read_text())
    assert [c["session_id"] for c in data["candidates"]] == ["s001", "s002"]
