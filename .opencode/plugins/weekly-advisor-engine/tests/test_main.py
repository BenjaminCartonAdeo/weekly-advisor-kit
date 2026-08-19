"""Orchestration tests: run()/doctor()/self_cost() on a seeded V1 DB."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import timedelta
from pathlib import Path

import pytest
from helpers import active_run_file, seed_hybrid_file, seed_v1_file, tzutc

from weekly_telemetry_aggregator.config import TelemetryConfig
from weekly_telemetry_aggregator.main import (
    EXIT_OK,
    EXIT_PARTIAL,
    EXIT_TOTAL_FAILURE,
    build_usage,
    doctor,
    harness,
    run,
    self_cost,
)
from weekly_telemetry_aggregator.models import Period, WarningEntry

RUN_TIME = tzutc(2026, 8, 12)


def _cfg(tmp_path: Path, db_path: Path, **over) -> TelemetryConfig:
    cfg = TelemetryConfig()
    cfg.output_dir = tmp_path
    cfg.opencode_db_path = str(db_path)
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


@pytest.fixture
def fake_opencode(tmp_path: Path, monkeypatch) -> None:
    """Fake `opencode` binary in PATH — doctor's version check is hermetic (CI has none)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / "opencode"
    exe.write_text("#!/bin/sh\necho '1.18.0'\n", encoding="utf-8")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")


def _seed_n(db_path: Path, n: int, *, window_mins_ago: int = 60) -> Path:
    sessions = []
    base = RUN_TIME - timedelta(minutes=window_mins_ago)
    for i in range(n):
        ts = base + timedelta(minutes=i)
        sessions.append(
            {
                "id": f"ses_{i:03d}",
                "title": f"Session {i}",
                "start": ts,
                "updated": ts + timedelta(seconds=30),
                "steps": [{"ts": ts, "cost": 0.1}],
            }
        )
    seed_v1_file(db_path, sessions)
    return db_path


def test_run_writes_summary_and_exit_zero(tmp_path: Path):
    db = _seed_n(tmp_path / "opencode.db", 16)
    cfg = _cfg(tmp_path, db)
    rc = run(cfg, anchor=RUN_TIME.isoformat())
    out = active_run_file(tmp_path, "weekly-summary-2026-08-12.json")
    assert out.exists()
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["schema_version"] == 2
    assert data["totals"]["session_count"] == 16
    assert rc == EXIT_OK  # aucune warning partiel sur un seed sain


def test_run_excludes_active_session_with_warning(tmp_path: Path):
    db = tmp_path / "opencode.db"
    active_ts = RUN_TIME - timedelta(minutes=2)  # updated < 10 min before run_time
    seed_v1_file(
        db,
        [
            {
                "id": "ses_active",
                "title": "En cours",
                "start": active_ts,
                "updated": active_ts,
                "steps": [{"ts": active_ts, "cost": 5.0}],
            }
        ],
    )
    cfg = _cfg(tmp_path, db)
    rc = run(cfg, anchor=RUN_TIME.isoformat())
    data = json.loads(
        (active_run_file(tmp_path, "weekly-summary-2026-08-12.json")).read_text(encoding="utf-8")
    )
    assert data["totals"]["session_count"] == 0
    assert any(w["message"].startswith("session active exclue") for w in data["warnings"])
    assert all("partial" in w for w in data["warnings"])  # champ sérialisé
    assert all(w["partial"] is False for w in data["warnings"])
    assert rc == EXIT_OK  # exclusion active = warning info, pas une dégradation


def test_run_excludes_advisor_session_silently(tmp_path: Path):
    db = tmp_path / "opencode.db"
    ts = RUN_TIME - timedelta(hours=2)
    seed_v1_file(
        db,
        [
            {
                "id": "ses_advisor",
                "title": "Lance la revue hebdomadaire",
                "start": ts,
                "updated": ts,
                "steps": [{"ts": ts, "cost": 50.0}],
            },
            {
                "id": "ses_norm",
                "title": "Normal",
                "start": ts,
                "updated": ts,
                "steps": [{"ts": ts, "cost": 0.1}],
            },
        ],
    )
    cfg = _cfg(tmp_path, db)
    run(cfg, anchor=RUN_TIME.isoformat())
    data = json.loads(
        (active_run_file(tmp_path, "weekly-summary-2026-08-12.json")).read_text(encoding="utf-8")
    )
    assert data["totals"]["session_count"] == 1  # advisor not counted, no warning about it
    assert not any("advisor" in w["message"] for w in data["warnings"])


def test_run_prefilters_sessions_updated_before_window(tmp_path: Path):
    db = tmp_path / "opencode.db"
    old = RUN_TIME - timedelta(days=30)
    seed_v1_file(
        db,
        [
            {
                "id": "ses_old",
                "title": "Vieille",
                "start": old,
                "updated": old,
                "steps": [{"ts": old, "cost": 9.0}],
            }
        ],
    )
    cfg = _cfg(tmp_path, db)
    run(cfg, anchor=RUN_TIME.isoformat())
    data = json.loads(
        (active_run_file(tmp_path, "weekly-summary-2026-08-12.json")).read_text(encoding="utf-8")
    )
    assert data["totals"]["session_count"] == 0
    assert data["totals"]["total_cost_usd"] == 0.0


def test_run_missing_pricing_and_cross_check_warnings(tmp_path: Path):
    db = tmp_path / "opencode.db"
    ts = RUN_TIME - timedelta(hours=2)
    seed_v1_file(
        db,
        [
            {
                "id": "ses_noprice",
                "title": "Sans prix",
                "start": ts,
                "updated": ts,
                "steps": [{"ts": ts, "cost": None}],
            },
            {
                "id": "ses_xcheck",
                "title": "Cross",
                "start": ts,
                "updated": ts,
                "agg_cost": 999.0,
                "steps": [{"ts": ts, "cost": 0.1}],
            },
        ],
    )
    cfg = _cfg(tmp_path, db)
    run(cfg, anchor=RUN_TIME.isoformat())
    data = json.loads(
        (active_run_file(tmp_path, "weekly-summary-2026-08-12.json")).read_text(encoding="utf-8")
    )
    msgs = [w["message"] for w in data["warnings"]]
    assert any(m.startswith("missing-pricing:") for m in msgs)
    assert any(m.startswith("cross-check mismatch") for m in msgs)


def test_rerun_same_anchor_creates_fresh_run_dir(tmp_path: Path):
    """v6.0.k (F1) : re-run même ancre = nouveau répertoire UUID, zéro collision."""
    db = _seed_n(tmp_path / "opencode.db", 16)
    cfg = _cfg(tmp_path, db)
    assert run(cfg, anchor=RUN_TIME.isoformat()) == EXIT_OK
    first = active_run_file(tmp_path, "weekly-summary-2026-08-12.json")
    assert first.exists()
    assert run(cfg, anchor=RUN_TIME.isoformat()) == EXIT_OK  # nouveau run indépendant
    runs = sorted(d for d in (tmp_path / "runs").glob("2026-08-12-*") if d.is_dir())
    assert len(runs) == 2
    state = json.loads((tmp_path / "run_state.json").read_text(encoding="utf-8"))
    assert state["run_dir"] in {f"runs/{d.name}" for d in runs}
    # le run actif (state) contient son summary ; le premier run est intact
    assert (tmp_path / state["run_dir"] / "weekly-summary-2026-08-12.json").is_file()
    assert first.exists()  # artefacts du premier run préservés


def test_build_usage_read_failure_marks_failed():
    class BadAdapter:
        name = "fake"

        def session_steps(self, *a):
            raise RuntimeError("boom")

        def session_tools(self, *a):
            return {}, {}, {}

        def session_user_turns(self, *a):
            return []

        def session_context_chars(self, *a):
            return {}

        def session_aggregates(self, *a):
            return None

    period = Period(start=RUN_TIME - timedelta(days=7), end=RUN_TIME)
    warnings: list[WarningEntry] = []
    usage, failed = build_usage(
        type(
            "M",
            (),
            {
                "session_id": "s",
                "time_updated": RUN_TIME - timedelta(hours=1),
                "title": "T",
                "directory": None,
                "agent": None,
                "parent_id": None,
            },
        )(),
        BadAdapter(),
        period=period,
        run_time=RUN_TIME,
        cfg=_cfg(Path("/tmp"), Path("/x.db")),
        warnings=warnings,
    )
    assert usage is None
    assert failed is True
    assert any(w.message == "session read failed: boom" for w in warnings)


def test_run_partial_only_on_read_failure(tmp_path: Path, monkeypatch):
    import weekly_telemetry_aggregator.main as main_mod

    db = _seed_n(tmp_path / "opencode.db", 16)
    cfg = _cfg(tmp_path, db)

    def _failing(meta, adapter, *, period, run_time, cfg, warnings, audit=None):
        # the real build_usage records the telemetry-gap warning itself
        warnings.append(
            WarningEntry(
                session_id=meta.session_id, message="session read failed: boom", partial=True
            )
        )
        return None, True

    monkeypatch.setattr(main_mod, "build_usage", _failing)
    assert run(cfg, anchor=RUN_TIME.isoformat()) == EXIT_PARTIAL


def test_doctor_ok_on_valid_db(tmp_path: Path, fake_opencode):
    _ = (tmp_path / ".opencode").mkdir()
    db = _seed_n(tmp_path / "opencode.db", 3)
    cfg = _cfg(tmp_path, db)
    cfg.project_root = tmp_path
    assert doctor(cfg) in (EXIT_OK, EXIT_PARTIAL)  # env warnings (opencode/git) tolerated


def test_doctor_opencode_missing_is_warning_not_fatal(tmp_path: Path, capsys):
    """v6.0.f : opencode hors PATH (cron étroit) = note non fatale — le run est lancé
    par opencode lui-même et le pipeline lit opencode.db directement. La DB absente
    reste la vraie fatalité (test_doctor_missing_db_is_problem)."""
    _ = (tmp_path / ".opencode").mkdir()
    db = _seed_n(tmp_path / "opencode.db", 3)
    cfg = _cfg(tmp_path, db)
    cfg.project_root = tmp_path
    rc = doctor(cfg, opencode_bin="opencode_absent_zzz")
    out = capsys.readouterr().out
    assert rc in (EXIT_OK, EXIT_PARTIAL)
    assert "WARNING: opencode introuvable" in out
    assert "PROBLEM: opencode" not in out


def test_doctor_missing_db_is_problem(tmp_path: Path):
    _ = (tmp_path / ".opencode").mkdir()
    cfg = _cfg(tmp_path, tmp_path / "missing.db")
    cfg.project_root = tmp_path
    assert doctor(cfg) == EXIT_TOTAL_FAILURE


def test_doctor_project_root_without_opencode_is_problem(tmp_path: Path):
    """Sentinelle d'installation : project_root sans .opencode/ = config non adaptée
    (clone : placeholder /path/to/...) — bloquant, jamais un warning silencieux."""
    db = _seed_n(tmp_path / "opencode.db", 3)
    cfg = _cfg(tmp_path, db)
    cfg.project_root = tmp_path / "vide"
    assert doctor(cfg) == EXIT_TOTAL_FAILURE


def test_doctor_missing_project_root(tmp_path: Path):
    db = _seed_n(tmp_path / "opencode.db", 3)
    cfg = _cfg(tmp_path, db)
    cfg.project_root = None
    assert doctor(cfg) == EXIT_TOTAL_FAILURE


def test_doctor_warns_when_config_nowhere(tmp_path: Path, capsys, fake_opencode):
    """Layout kit : cwd=moteur (config au cwd) ≠ project_root (repo audité) — warning
    uniquement si la config est introuvable partout, plus jamais sur cwd≠project_root."""
    db = _seed_n(tmp_path / "opencode.db", 3)
    cfg = _cfg(tmp_path, db)
    audited = tmp_path / "audited"
    (audited / ".opencode").mkdir(parents=True)  # repo audité légitime (layout kit)
    cfg.project_root = audited  # repo audité sans config — mode kit légitime
    (tmp_path / "weekly-telemetry-config.json").write_text("{}", encoding="utf-8")
    assert doctor(cfg, cwd=tmp_path) in (EXIT_OK, EXIT_PARTIAL)  # config au cwd → aucun warning
    out = (capsys.readouterr().out + capsys.readouterr().err).lower()
    assert "vérifier --dir du cron" not in out
    # vrai défaut : config introuvable au cwd ET au project_root
    capsys.readouterr()
    doctor(cfg, cwd=tmp_path / "nowhere")
    out = (capsys.readouterr().out + capsys.readouterr().err).lower()
    assert "config introuvable" in out
    # plugin : config passée explicitement (--config) → le warning n'est jamais émis
    capsys.readouterr()
    doctor(cfg, cwd=tmp_path / "nowhere", config_loaded=True)
    out = (capsys.readouterr().out + capsys.readouterr().err).lower()
    assert "config introuvable" not in out


def test_self_cost_finds_advisor_session(tmp_path: Path, capsys):
    db = tmp_path / "opencode.db"
    ts = RUN_TIME - timedelta(hours=2)
    seed_v1_file(
        db,
        [
            {
                "id": "ses_advisor",
                "title": "Lance la revue hebdomadaire",
                "start": ts,
                "updated": ts,
                "agg_cost": 1.25,
                "steps": [{"ts": ts, "cost": 1.25}],
            }
        ],
    )
    cfg = _cfg(tmp_path, db)
    assert self_cost(cfg) == EXIT_OK
    assert "$1.2500" in capsys.readouterr().out


def test_self_cost_no_session_is_partial(tmp_path: Path, capsys):
    db = tmp_path / "opencode.db"
    seed_v1_file(
        db, [{"id": "s", "title": "Normal", "start": RUN_TIME, "updated": RUN_TIME, "steps": []}]
    )
    cfg = _cfg(tmp_path, db)
    assert self_cost(cfg) == EXIT_PARTIAL


# ============================================================ v5.28 (P0/P1/P2)


class _FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_harness_missing_binary_is_total_failure(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    cfg = _cfg(tmp_path, tmp_path / "opencode.db")
    cfg.project_root = tmp_path
    assert harness(cfg, anchor=RUN_TIME.isoformat()) == EXIT_TOTAL_FAILURE


def test_harness_runs_and_writes_digest(tmp_path: Path, monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return _FakeProc(0)

    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/harness-eval")
    monkeypatch.setattr("subprocess.run", fake_run)
    cfg = _cfg(tmp_path, tmp_path / "opencode.db")
    cfg.project_root = tmp_path
    assert harness(cfg, anchor=RUN_TIME.isoformat()) == EXIT_OK
    lint_call = [c for c in calls if "harness-lint" in c]
    assert lint_call
    assert lint_call[0][-1] == str(
        active_run_file(tmp_path, "weekly-harness-digest-2026-08-12.json")
    )


def test_harness_uses_one_temporary_projection_and_merges_scope(tmp_path: Path, monkeypatch):
    project = tmp_path / "project"
    (project / ".opencode").mkdir(parents=True)
    (project / ".opencode" / "commands").mkdir()
    (project / ".opencode" / "node_modules").mkdir()
    (project / ".opencode" / "plugins" / "weekly-advisor-engine").mkdir(parents=True)
    (project / ".opencode" / "commands" / "review.md").write_text("review", encoding="utf-8")
    (project / ".opencode" / "node_modules" / "vendor.js").write_text("vendor", encoding="utf-8")
    (project / ".opencode" / "plugins" / "weekly-advisor-engine" / "source.py").write_text(
        "vendor", encoding="utf-8"
    )
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        if "--version" in args:
            return _FakeProc(0, stdout="harness-eval 7.9.0")
        calls.append(args)
        projection = Path(args[2])
        output = Path(args[args.index("--output") + 1])
        assert (projection / ".opencode/commands/review.md").exists()
        assert not (projection / ".opencode/node_modules/vendor.js").exists()
        assert not (projection / ".opencode/plugins/weekly-advisor-engine/source.py").exists()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(
                {
                    "metadata": {"components_scanned": 1},
                    "inspection": {
                        "uncategorized": [
                            {
                                "path": str(projection / ".opencode/commands/review.md"),
                                "findings": [],
                            }
                        ]
                    },
                }
            ),
            encoding="utf-8",
        )
        return _FakeProc(1)

    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/harness-eval")
    monkeypatch.setattr("subprocess.run", fake_run)
    cfg = _cfg(tmp_path / "reports", tmp_path / "opencode.db")
    cfg.project_root = project
    assert harness(cfg, anchor=RUN_TIME.isoformat()) == EXIT_OK

    assert len(calls) == 1
    lint_call = calls[0]
    assert lint_call[0:2] == ["/usr/bin/harness-eval", "harness-lint"]
    assert Path(lint_call[2]) != project
    digest = json.loads(
        (active_run_file(cfg.output_dir, "weekly-harness-digest-2026-08-12.json")).read_text(
            encoding="utf-8"
        )
    )
    assert digest["inspection"]["uncategorized"][0]["path"] == ".opencode/commands/review.md"
    assert digest["harness_include"]["included_file_count"] == 1
    assert digest["harness_counts"]["components_scanned"] == 1


def test_harness_projection_is_cleaned_up_when_subprocess_fails(tmp_path: Path, monkeypatch):
    project = tmp_path / "project"
    (project / ".opencode" / "commands").mkdir(parents=True)
    (project / ".opencode" / "commands" / "review.md").write_text("review", encoding="utf-8")
    created: list[Path] = []
    real_temporary_directory = tempfile.TemporaryDirectory

    class TrackingTemporaryDirectory:
        def __init__(self, *args, **kwargs):
            self._inner = real_temporary_directory(*args, **kwargs)
            self.name = self._inner.name
            created.append(Path(self.name))

        def __enter__(self):
            return self._inner.__enter__()

        def __exit__(self, *args):
            return self._inner.__exit__(*args)

    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/harness-eval")

    def failing_run(args, **kwargs):
        if "--version" in args:
            return _FakeProc(0, stdout="harness-eval 7.9.0")
        raise OSError("harness unavailable")

    monkeypatch.setattr("subprocess.run", failing_run)
    monkeypatch.setattr(
        "weekly_telemetry_aggregator.main.tempfile.TemporaryDirectory", TrackingTemporaryDirectory
    )
    cfg = _cfg(tmp_path / "reports", tmp_path / "opencode.db")
    cfg.project_root = project

    assert harness(cfg, anchor=RUN_TIME.isoformat()) == EXIT_TOTAL_FAILURE
    assert created
    assert all(not path.exists() for path in created)


def test_harness_tool_rc1_is_ok(tmp_path: Path, monkeypatch):
    """Exit 1 from harness-lint = violations found, still a successful step."""
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/harness-eval")
    monkeypatch.setattr("subprocess.run", lambda *a, **k: _FakeProc(1))
    cfg = _cfg(tmp_path, tmp_path / "opencode.db")
    cfg.project_root = tmp_path
    assert harness(cfg, anchor=RUN_TIME.isoformat()) == EXIT_OK


def test_harness_unexpected_rc_is_total_failure(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/harness-eval")
    monkeypatch.setattr("subprocess.run", lambda *a, **k: _FakeProc(3))
    cfg = _cfg(tmp_path, tmp_path / "opencode.db")
    cfg.project_root = tmp_path
    assert harness(cfg, anchor=RUN_TIME.isoformat()) == EXIT_TOTAL_FAILURE


def test_doctor_warns_on_harness_version_drift(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/bin/harness-eval" if name == "harness-eval" else None
    )
    monkeypatch.setattr(
        "subprocess.run",
        lambda args, **kwargs: (
            _FakeProc(0, stdout="harness-eval 7.8.0\n") if "--version" in args else _FakeProc(0)
        ),
    )
    cfg = _cfg(tmp_path, tmp_path / "opencode.db")
    cfg.project_root = tmp_path
    cfg.harness_eval_version = "7.9.0"
    doctor(cfg)
    out = capsys.readouterr().out
    assert "dérive de version" in out


def test_cross_check_tolerance_silences_small_mismatch(tmp_path: Path):
    db = tmp_path / "opencode.db"
    ts = RUN_TIME - timedelta(hours=2)
    seed_v1_file(
        db,
        [
            {
                "id": "ses_x",
                "title": "X",
                "start": ts,
                "updated": ts,
                "agg_cost": 0.11,
                "steps": [{"ts": ts, "cost": 0.1}],
            }
        ],
    )
    cfg = _cfg(tmp_path, db, cross_check_tolerance_pct=0.5)
    run(cfg, anchor=RUN_TIME.isoformat())
    data = json.loads(
        (active_run_file(tmp_path, "weekly-summary-2026-08-12.json")).read_text(encoding="utf-8")
    )
    msgs = [w["message"] for w in data["warnings"]]
    assert not any(m.startswith("cross-check mismatch") for m in msgs)


def test_run_writes_reported_cost_usd(tmp_path: Path):
    db = tmp_path / "opencode.db"
    ts = RUN_TIME - timedelta(hours=2)
    seed_v1_file(
        db,
        [
            {
                "id": "ses_r",
                "title": "R",
                "start": ts,
                "updated": ts,
                "agg_cost": 0.25,
                "steps": [{"ts": ts, "cost": 0.25}],
            }
        ],
    )
    cfg = _cfg(tmp_path, db)
    run(cfg, anchor=RUN_TIME.isoformat())
    data = json.loads(
        (active_run_file(tmp_path, "weekly-summary-2026-08-12.json")).read_text(encoding="utf-8")
    )
    assert data["top_sessions_by_cost"][0]["reported_cost_usd_lifetime"] == 0.25


# ============================================================ v5.28 (selection audit)


def test_run_writes_selection_audit(tmp_path: Path):
    """Chaque session touchée dans la fenêtre est tracée (comptée ou exclue + raison)."""
    db = tmp_path / "opencode.db"
    now = RUN_TIME
    old = RUN_TIME - timedelta(days=30)  # hors fenêtre
    seed_v1_file(
        db,
        [
            {
                "id": "ses_incl",
                "title": "Incluse",
                "start": now - timedelta(hours=3),
                "updated": now - timedelta(hours=2),
                "steps": [{"ts": now - timedelta(hours=2), "cost": 0.3}],
            },
            {
                "id": "ses_active",
                "title": "En cours",
                "start": now - timedelta(minutes=2),
                "updated": now - timedelta(minutes=2),
                "steps": [{"ts": now - timedelta(minutes=2), "cost": 9.0}],
            },
            {
                "id": "ses_advisor",
                "title": "Lance la revue hebdomadaire",
                "start": now - timedelta(hours=5),
                "updated": now - timedelta(hours=5),
                "steps": [{"ts": now - timedelta(hours=5), "cost": 4.0}],
            },
            {
                "id": "ses_noact",
                "title": "Sans télémétrie fenêtre",
                "start": old,
                "updated": now - timedelta(days=1),
                "steps": [{"ts": old, "cost": 2.0}],
            },
        ],
    )
    cfg = _cfg(tmp_path, db)
    rc = run(cfg, anchor=RUN_TIME.isoformat())
    data = json.loads(
        (active_run_file(tmp_path, "weekly-summary-2026-08-12.json")).read_text(encoding="utf-8")
    )
    sel = data["selection"]
    assert rc == EXIT_OK
    assert sel["window_touched"] == 4
    assert sel["counted"] == 1
    assert sel["excluded_active"] == 1
    assert sel["excluded_no_activity"] == 1
    assert sel["excluded_advisor"] == 1
    assert sel["excluded_error"] == 0
    assert len(sel["recent"]) == 4
    assert {r["status"] for r in sel["recent"]} == {"included", "active", "advisor", "no-activity"}
    # recent trié par updated décroissant (le plus récent en premier)
    assert sel["recent"][0]["session_id"] == "ses_active"


# ============================================================ v5.28 (K1/K2/K9/K10/K11)


def test_run_marks_unflushed_session_and_warns(tmp_path: Path):
    """Session touchée mais 0 message/part sur toute la DB → statut `unflushed` + warning."""
    db = tmp_path / "opencode.db"
    ts = RUN_TIME - timedelta(hours=3)
    seed_v1_file(
        db,
        [
            {
                "id": "ses_live",
                "title": "Session active non flushée",
                "start": ts,
                "updated": ts,
                # aucune ligne message/part : has_telemetry_rows -> False
            },
            {
                "id": "ses_old",
                "title": "Vieille avec télémétrie hors fenêtre",
                "start": RUN_TIME - timedelta(days=30),
                "updated": RUN_TIME - timedelta(days=1),
                "steps": [{"ts": RUN_TIME - timedelta(days=30), "cost": 1.0}],
            },
        ],
    )
    cfg = _cfg(tmp_path, db)
    run(cfg, anchor=RUN_TIME.isoformat())
    data = json.loads(
        (active_run_file(tmp_path, "weekly-summary-2026-08-12.json")).read_text(encoding="utf-8")
    )
    sel = data["selection"]
    statuses = {r["status"] for r in sel["recent"]}
    assert statuses == {"unflushed", "no-activity"}
    assert any("télémétrie persistée" in w["message"] for w in data["warnings"]), (
        "warning K1 attendu"
    )
    # K2: 0 comptée sur 2 touchées -> warning dédié
    assert any("0 session comptée" in w["message"] for w in data["warnings"])


def test_run_zero_touched_warning(tmp_path: Path):
    """Aucune session mise à jour dans la fenêtre → warning explicite."""
    db = tmp_path / "opencode.db"
    old = RUN_TIME - timedelta(days=30)
    seed_v1_file(
        db,
        [
            {
                "id": "ses_old",
                "title": "Old",
                "start": old,
                "updated": old,
                "steps": [{"ts": old, "cost": 1.0}],
            }
        ],
    )
    cfg = _cfg(tmp_path, db)
    run(cfg, anchor=RUN_TIME.isoformat())
    data = json.loads(
        (active_run_file(tmp_path, "weekly-summary-2026-08-12.json")).read_text(encoding="utf-8")
    )
    assert data["selection"]["window_touched"] == 0
    assert any("aucune session mise à jour" in w["message"] for w in data["warnings"])


def test_run_period_mismatch_message(tmp_path: Path, capsys):
    db = _seed_n(tmp_path / "opencode.db", 2)
    cfg = _cfg(tmp_path, db)
    run(cfg, anchor=RUN_TIME.isoformat())
    rc = run(
        cfg, anchor=(RUN_TIME + timedelta(days=1)).isoformat()
    )  # ancre différente, même date ? non -> autre fichier
    assert rc == EXIT_OK
    # ancre même date mais période différente : RUN_TIME vs RUN_TIME+1min
    rc2 = run(cfg, anchor=(RUN_TIME + timedelta(minutes=5)).isoformat())
    assert rc2 == EXIT_OK
    out = capsys.readouterr().out
    assert "re-run fenêtre" in out  # v6.0.k : même date => nouveau run isolé, informé


def test_selection_audit_has_parent_id(tmp_path: Path):
    db = tmp_path / "opencode.db"
    ts = RUN_TIME - timedelta(hours=2)
    seed_v1_file(
        db,
        [
            {
                "id": "ses_parent",
                "title": "Racine",
                "start": ts,
                "updated": ts,
                "steps": [{"ts": ts, "cost": 0.1}],
            },
            {
                "id": "ses_child",
                "title": "Enfant",
                "parent": "ses_parent",
                "start": ts,
                "updated": ts,
                "steps": [{"ts": ts, "cost": 0.2}],
            },
        ],
    )
    cfg = _cfg(tmp_path, db)
    run(cfg, anchor=RUN_TIME.isoformat())
    data = json.loads(
        (active_run_file(tmp_path, "weekly-summary-2026-08-12.json")).read_text(encoding="utf-8")
    )
    by_id = {r["session_id"]: r for r in data["selection"]["recent"]}
    assert by_id["ses_child"]["parent_id"] == "ses_parent"
    assert by_id["ses_parent"]["parent_id"] is None


def test_doctor_warns_when_gh_missing_with_watch_repos(
    tmp_path: Path, monkeypatch, capsys, fake_opencode
):
    db = _seed_n(tmp_path / "opencode.db", 2)
    cfg = _cfg(tmp_path, db)
    (tmp_path / ".opencode").mkdir()
    cfg.project_root = tmp_path
    cfg.watch_repos = ["adeo/ai-skills"]
    monkeypatch.setattr("weekly_telemetry_aggregator.main.shutil.which", lambda t: None)
    rc = doctor(cfg)
    out = capsys.readouterr().out
    assert rc == EXIT_OK
    assert "gh absent du PATH" in out


def test_selection_counts_unflushed(tmp_path: Path):
    db = tmp_path / "opencode.db"
    ts = RUN_TIME - timedelta(hours=2)
    seed_v1_file(db, [{"id": "ses_live", "title": "Live", "start": ts, "updated": ts}])
    cfg = _cfg(tmp_path, db)
    run(cfg, anchor=RUN_TIME.isoformat())
    data = json.loads(
        (active_run_file(tmp_path, "weekly-summary-2026-08-12.json")).read_text(encoding="utf-8")
    )
    sel = data["selection"]
    assert sel["excluded_unflushed"] == 1
    assert sel["excluded_no_activity"] == 0


# ============================================================ v5.28 (root cause — adaptateur v1-live)


def test_detect_prefers_live_session_table(tmp_path: Path):
    """Le schéma post-migration (metadata `session` + telemetry `part`) est détecté en v1-dual."""
    from weekly_telemetry_aggregator.sqlite_reader import detect_db

    db = tmp_path / "opencode.db"
    ts = RUN_TIME - timedelta(hours=2)
    seed_hybrid_file(
        db,
        [
            {
                "id": "ses_live",
                "title": "Live",
                "start": ts,
                "updated": ts,
                "steps": [{"ts": ts, "cost": 0.1}],
            }
        ],
    )
    path, adapter = detect_db(str(db))
    assert adapter.name == "v1-dual"
    assert adapter.latest_updated_ms() > 0


def test_run_counts_sessions_from_live_table(tmp_path: Path):
    """Le pipeline compte les sessions de la table `session` (pas le miroir session_v2)."""
    db = tmp_path / "opencode.db"
    base = RUN_TIME - timedelta(hours=6)
    sessions = []
    for i in range(8):
        ts = base + timedelta(minutes=10 * i)
        sessions.append(
            {
                "id": f"ses_{i:03d}",
                "title": f"Session {i}",
                "start": ts,
                "updated": ts,
                "steps": [{"ts": ts, "cost": 0.1}],
            }
        )
    seed_hybrid_file(db, sessions)
    cfg = _cfg(tmp_path, db)
    run(cfg, anchor=RUN_TIME.isoformat())
    data = json.loads(
        (active_run_file(tmp_path, "weekly-summary-2026-08-12.json")).read_text(encoding="utf-8")
    )
    assert data["totals"]["session_count"] == 8
    assert data["selection"]["counted"] == 8


def test_dual_adapter_counts_both_tables(tmp_path: Path):
    """v1-dual : les sessions CLI (`session`) ET serveur (`session_v2`) sont comptées."""
    import sqlite3

    from weekly_telemetry_aggregator.sqlite_reader import detect_db

    db = tmp_path / "opencode.db"
    ts = RUN_TIME - timedelta(hours=4)
    seed_hybrid_file(
        db,
        [
            {
                "id": "ses_cli_1",
                "title": "CLI 1",
                "start": ts,
                "updated": ts,
                "steps": [{"ts": ts, "cost": 0.1}],
            },
            {
                "id": "ses_cli_2",
                "title": "CLI 2",
                "start": ts,
                "updated": ts,
                "steps": [{"ts": ts, "cost": 0.2}],
            },
        ],
    )
    conn = sqlite3.connect(str(db))
    # session CLI supplémentaire UNIQUEMENT dans `session` (pas dans session_v2)
    conn.execute(
        "INSERT INTO session (id, parent_id, title, model, agent, directory, cost, tokens_input, "
        "tokens_output, tokens_reasoning, tokens_cache_read, tokens_cache_write, time_created, time_updated) "
        "VALUES ('ses_cli_3', NULL, 'CLI 3', '{}', NULL, NULL, 0.0, 0, 0, 0, 0, 0, ?, ?)",
        (int(ts.timestamp() * 1000), int(ts.timestamp() * 1000)),
    )
    conn.execute(
        "INSERT INTO part (session_id, data, time_created) VALUES (?, ?, ?)",
        (
            "ses_cli_3",
            '{"type": "step-finish", "cost": 0.3, "tokens": {"input": 10, "output": 2}}',
            int(ts.timestamp() * 1000),
        ),
    )
    conn.commit()
    conn.close()

    path, adapter = detect_db(str(db))
    assert adapter.name == "v1-dual"
    metas = adapter.list_sessions(0)
    ids = {m.session_id for m in metas}
    assert {"ses_cli_1", "ses_cli_2", "ses_cli_3"} <= ids  # les deux mondes
    cfg = _cfg(tmp_path, db)
    run(cfg, anchor=RUN_TIME.isoformat())
    data = json.loads(
        (active_run_file(tmp_path, "weekly-summary-2026-08-12.json")).read_text(encoding="utf-8")
    )
    assert data["totals"]["session_count"] == 3
    assert data["totals"]["total_cost_usd"] == round(0.1 + 0.2 + 0.3, 6)


def test_self_cost_falls_back_to_weekly_advisor_session(tmp_path: Path, capsys):
    """v5.30 (E) : self-cost trouve la session agent la plus récente sans titre exact."""
    from weekly_telemetry_aggregator.main import self_cost

    db = tmp_path / "opencode.db"
    ts = RUN_TIME - timedelta(hours=3)
    seed_hybrid_file(
        db,
        [
            {
                "id": "ses_agent",
                "title": "Rapport de surveillance",
                "agent": "infrastructure/weekly-advisor",
                "start": ts,
                "updated": ts,
                "agg_cost": 1.25,
                "steps": [],
            },
            {
                "id": "ses_autre",
                "title": "Autre",
                "agent": "python-expert",
                "start": ts,
                "updated": ts,
                "agg_cost": 9.0,
                "steps": [],
            },
        ],
    )
    cfg = _cfg(tmp_path, db)
    cfg.advisor_run_title = "Lance la revue hebdomadaire"  # titre absent du seed
    rc = self_cost(cfg)
    out = capsys.readouterr().out
    assert rc == EXIT_OK
    assert "1.2500" in out


def test_rerun_same_day_different_window_is_isolated(tmp_path: Path, capsys):
    """v6.0.k (F1) : même jour + fenêtre différente = runs distincts, pas d'écrasement."""
    db = _seed_n(tmp_path / "opencode.db", 2)
    cfg = _cfg(tmp_path, db)
    cfg.lookback_days = 7
    run(cfg, anchor=RUN_TIME.isoformat())
    cfg.lookback_days = 14  # même jour, fenêtre différente
    rc = run(cfg, anchor=(RUN_TIME + timedelta(minutes=1)).isoformat())
    assert rc == EXIT_OK
    runs = sorted(d for d in (tmp_path / "runs").glob("2026-08-12-*") if d.is_dir())
    assert len(runs) == 2
    summaries = sorted((tmp_path / "runs").glob("2026-08-12-*/weekly-summary-2026-08-12.json"))
    assert len(summaries) == 2  # aucune perte (v5.31 c est résolu par design)
    out = capsys.readouterr().out
    assert "re-run fenêtre" in out


def test_windowed_cost_above_lifetime_warns(tmp_path: Path):
    """v5.30 (4) : coût fenêtré > lifetime session.cost → warning (enfants/compaction)."""
    db = tmp_path / "opencode.db"
    ts = RUN_TIME - timedelta(hours=3)
    seed_v1_file(
        db,
        [
            {
                "id": "ses_big",
                "title": "Grosse session",
                "start": ts,
                "updated": ts,
                "steps": [{"ts": ts, "cost": 5.0}],  # fenêtré 5.0
                "agg_cost": 0.4,  # lifetime session_v2 = 0.4
            }
        ],
    )
    cfg = _cfg(tmp_path, db)
    run(cfg, anchor=RUN_TIME.isoformat())
    data = json.loads(
        (active_run_file(tmp_path, "weekly-summary-2026-08-12.json")).read_text(encoding="utf-8")
    )
    assert any("windowed cost" in w["message"] for w in data["warnings"])
    mismatch = [w for w in data["warnings"] if "windowed cost" in w["message"]][0]
    assert mismatch["parts_cost"] == 5.0 and mismatch["session_v2_cost"] == 0.4


def test_parse_skill_md_target_agents(tmp_path: Path):
    """v5.30 : metadata.target_agents en liste YAML est parsé sans tiret ni newline."""
    from weekly_telemetry_aggregator.main import _parse_skill_md

    skill = tmp_path / "demo" / "SKILL.md"
    skill.parent.mkdir()
    skill.write_text(
        "---\nname: demo\ndescription: un skill de test\nmetadata:\n"
        "  target_agents:\n    - java-pro\n    - backend-architect\n---\n# Corps\n",
        encoding="utf-8",
    )
    desc, body, targets = _parse_skill_md(skill)
    assert desc == "un skill de test"
    assert targets == ["java-pro", "backend-architect"]
    # inline aussi
    skill2 = tmp_path / "demo2" / "SKILL.md"
    skill2.parent.mkdir()
    skill2.write_text(
        "---\nname: demo2\ndescription: x\nmetadata:\n  target_agents: [typescript-pro]\n---\n",
        encoding="utf-8",
    )
    _, _, targets2 = _parse_skill_md(skill2)
    assert targets2 == ["typescript-pro"]


def test_run_lookback_days_override(tmp_path: Path, capsys):
    """v6.0.b : run(..., lookback_days=21) → fenêtre 21 j, config intacte."""
    db = _seed_n(tmp_path / "opencode.db", 2)
    cfg = _cfg(tmp_path, db)
    rc = run(cfg, anchor=RUN_TIME.isoformat(), lookback_days=21)
    assert rc == EXIT_OK
    out = capsys.readouterr().out
    assert "fenêtre 21 j" in out
    data = json.loads(
        (active_run_file(tmp_path, "weekly-summary-2026-08-12.json")).read_text(encoding="utf-8")
    )
    assert data["period"]["start"] == (RUN_TIME - timedelta(days=21)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert cfg.lookback_days == 21  # mutation en mémoire seulement


def test_rerun_different_window_coexists(tmp_path: Path, capsys):
    """v6.0.k (F1) : fenêtres différentes = runs distincts ; --force est un no-op informatif."""
    db = _seed_n(tmp_path / "opencode.db", 2)
    cfg = _cfg(tmp_path, db)
    cfg.lookback_days = 7
    run(cfg, anchor=RUN_TIME.isoformat())
    cfg.lookback_days = 15
    rc = run(cfg, force=True, anchor=RUN_TIME.isoformat())
    assert rc == EXIT_OK
    runs = sorted(d for d in (tmp_path / "runs").glob("2026-08-12-*") if d.is_dir())
    assert len(runs) == 2
    out = capsys.readouterr().out
    assert "--force sans objet" in out


def test_build_selection_marks_in_window():
    """P7 : les sessions actives post-fenêtre sont marquées hors fenêtre (§1)."""
    from weekly_telemetry_aggregator.main import _build_selection

    audit = [
        {"session_id": "s1", "status": "included", "updated": "2026-08-12T09:00:00Z"},
        {"session_id": "s2", "status": "active", "updated": "2026-08-19T07:00:00Z"},
        {"session_id": "s3", "status": "no-activity", "updated": "2026-07-01T00:00:00Z"},
    ]
    sel = _build_selection(
        audit,
        limit=10,
        period={"start": "2026-08-05T00:00:00Z", "end": "2026-08-12T23:59:59Z"},
    )
    by_id = {r["session_id"]: r for r in sel["recent"]}
    assert by_id["s1"]["in_window"] is True
    assert by_id["s2"]["in_window"] is False
    assert by_id["s3"]["in_window"] is False
