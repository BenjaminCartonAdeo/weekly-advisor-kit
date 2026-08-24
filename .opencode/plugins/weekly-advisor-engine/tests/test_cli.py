"""CLI dispatch tests (subcommands, default run, end-to-end run)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
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
        data["audited"][0]["session_id"] == "opencode:s000"
    )  # top-cost (coûts identiques → session_id ASC), id canonique multi-harnais


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


def test_show_session_accepts_canonical_id(tmp_path: Path, capsys):
    """Id canonique <harness>:<id> routé vers le provider du harnais."""
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
                "texts": [{"ts": ts, "text": "salut"}],
            }
        ],
    )
    conf = _write_config(tmp_path, db)
    rc = main(["show-session", "opencode:ses_x", "--config", str(conf)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "user: salut" in out


# ============================================================ v6.2 (fatal dispatch)


def _doctor_conf(tmp_path: Path) -> Path:
    return _write_config(tmp_path, _seed(tmp_path))


@pytest.mark.parametrize("exc", [RuntimeError("boom"), OSError("disk full")], ids=type)
def test_main_unexpected_exception_is_fatal(tmp_path, capsys, monkeypatch, exc):
    """Spec §4 : traceback non géré → FATAL sur stderr + exit 2 (stop total)."""
    from weekly_telemetry_aggregator import cli

    def boom(args, cfg):
        raise exc

    monkeypatch.setattr(cli, "_cmd_doctor", boom)
    rc = main(["doctor", "--config", str(_doctor_conf(tmp_path))])
    assert rc == 2
    err = capsys.readouterr().err
    assert "FATAL" in err and str(exc) in err


def test_main_dispatch_success_unchanged(tmp_path, capsys, monkeypatch):
    from weekly_telemetry_aggregator import cli

    def ok(args, cfg):
        print("ok-dispatch")
        return 0

    monkeypatch.setattr(cli, "_cmd_doctor", ok)
    rc = main(["doctor", "--config", str(_doctor_conf(tmp_path))])
    assert rc == 0
    assert "ok-dispatch" in capsys.readouterr().out


@pytest.mark.parametrize("exc", [SystemExit(3), KeyboardInterrupt()], ids=["systemexit", "kbint"])
def test_main_control_flow_exceptions_traverse(tmp_path, monkeypatch, exc):
    """KeyboardInterrupt/SystemExit ne sont jamais masqués par le wrap fatal."""
    from weekly_telemetry_aggregator import cli

    def bye(args, cfg):
        raise exc

    monkeypatch.setattr(cli, "_cmd_doctor", bye)
    with pytest.raises(type(exc)):
        main(["doctor", "--config", str(_doctor_conf(tmp_path))])


def test_main_usage_error_still_exits_two_via_argparse(capsys):
    """parse_args reste hors du try : SystemExit argparse intact."""
    with pytest.raises(SystemExit) as ei:
        main(["run", "--lookback-days", "0"])
    assert ei.value.code != 0
    capsys.readouterr()  # drain argparse usage output


# ============================================================ v7 (watch-validate : câblage candidats/mémoire/racine)


def _seed_watch_inputs(tmp_path: Path) -> None:
    """Artefacts datés du run (mode legacy racine output_dir) pour watch-validate."""
    date = RUN_TIME.strftime("%Y-%m-%d")
    (tmp_path / f"weekly-watch-context-{date}.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "market_matches": [
                    {"name": "cool-tool", "npm_package": "cool-tool", "existing_state": "absent"},
                    {
                        "name": "other-tool",
                        "npm_package": "other-tool",
                        "existing_state": "observed",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    raw = {
        "findings": [
            {
                "category": "install-new",
                "severity": "medium",
                "description": "A useful market item",
                "evidence_summary": "Evidence must survive validation",
                "recommendation": "Evaluate the item with a human",
                "subject": {"name": "cool-tool", "npm_package": "cool-tool"},
            },
            {
                "category": "improve-existing",
                "severity": "medium",
                "description": "Ameliore un skill local existant",
                "evidence_summary": "Evidence",
                "recommendation": "Fusionner la capacite",
                "target_local": "local-skill",
                "subject": {"name": "other-tool", "npm_package": "other-tool"},
            },
        ]
    }
    (tmp_path / f"weekly-watch-findings-raw-{date}.json").write_text(
        json.dumps(raw), encoding="utf-8"
    )
    candidates = {
        "schema_version": 1,
        "mode": "distill",
        "candidates": [
            {
                "id": "npm:cool-tool",
                "name": "cool-tool",
                "sources": [],
                "score": {"total": 10, "breakdown": {}},
                "security": {"verdict": "clean", "reason": None},
                "summary": "une fiche",
                "signature": {"version": "1.0.0", "published_at": None},
                "local_relevance_hints": [],
            }
        ],
        "security_annex": [
            {"id": "npm:blocked-one", "name": "blocked-one", "reason": "env-exfiltration"}
        ],
    }
    (tmp_path / f"watch-candidates-{date}.json").write_text(
        json.dumps(candidates), encoding="utf-8"
    )


def _write_watch_config(tmp_path: Path, project_root: Path, **extra) -> Path:
    conf = tmp_path / "config.json"
    conf.write_text(
        json.dumps({"output_dir": str(tmp_path), "project_root": str(project_root), **extra}),
        encoding="utf-8",
    )
    return conf


def test_watch_validate_wires_memory_annex_and_project_root(tmp_path: Path):
    """v7 : le CLI passe candidats du run, mémoire inter-run et racine projet."""
    _seed_watch_inputs(tmp_path)
    project = tmp_path / "project"
    skill_dir = project / ".opencode" / "skills" / "local-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\ndescription: x\n---\ncorps\n", encoding="utf-8")
    conf = _write_watch_config(tmp_path, project)
    anchor = RUN_TIME.isoformat()

    rc = main(["watch-validate", "--config", str(conf), "--anchor", anchor])

    assert rc == 0
    date = RUN_TIME.strftime("%Y-%m-%d")
    data = json.loads((tmp_path / f"weekly-watch-findings-{date}.json").read_text(encoding="utf-8"))
    # Annexe sécurité branchée depuis watch-candidates-<date>.json.
    assert data["security_annex"] == {"blocked_count": 1, "ids": ["npm:blocked-one"]}
    # Coercition cible locale active via project_root : inventaire sans
    # « local-skill » serait coerci ; ici la cible existe → improve-existing.
    by_subject = {f["subject"]["name"]: f for f in data["findings"]}
    assert by_subject["cool-tool"]["decision"] == "install-new"
    assert by_subject["other-tool"]["category"] == "improve-existing"
    assert data["validation"]["counts"]["downgraded"] == 0
    # Mémoire post-validation écrite à la racine output_dir (pas dans le run).
    entries = {
        entry["id"]: entry
        for entry in (
            json.loads(line)
            for line in (tmp_path / "watch-memory.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    }
    assert entries["npm:cool-tool"]["history"][-1]["status"] == "recommended"


def test_watch_validate_coerces_unknown_target_to_install_new(tmp_path: Path):
    """project_root câblé : cible hors inventaire → coercition install-new."""
    _seed_watch_inputs(tmp_path)
    conf = _write_watch_config(tmp_path, tmp_path)  # racine vide → inventaire vide
    anchor = RUN_TIME.isoformat()

    main(["watch-validate", "--config", str(conf), "--anchor", anchor])

    date = RUN_TIME.strftime("%Y-%m-%d")
    data = json.loads((tmp_path / f"weekly-watch-findings-{date}.json").read_text(encoding="utf-8"))
    other = next(f for f in data["findings"] if f["subject"]["name"] == "other-tool")
    assert other["category"] == "install-new"
    assert data["validation"]["counts"]["downgraded"] == 1


def test_watch_validate_memory_file_follows_config(tmp_path: Path):
    """Le fichier mémoire honore watch_distill.memory_file (relatif à output_dir)."""
    _seed_watch_inputs(tmp_path)
    conf = _write_watch_config(
        tmp_path, tmp_path, watch_distill={"memory_file": "custom-memory.jsonl"}
    )
    anchor = RUN_TIME.isoformat()

    rc = main(["watch-validate", "--config", str(conf), "--anchor", anchor])

    assert rc == 0
    assert (tmp_path / "custom-memory.jsonl").is_file()
    assert not (tmp_path / "watch-memory.jsonl").exists()
