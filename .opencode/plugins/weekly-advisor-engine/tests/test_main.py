"""Orchestration tests: run()/doctor()/self_cost() on a seeded V1 DB."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from helpers import (
    FakeSessionProvider,
    active_run_file,
    seed_hybrid_file,
    seed_v1_file,
    tzutc,
)

from weekly_telemetry_aggregator.config import TelemetryConfig
from weekly_telemetry_aggregator.costing import self_cost
from weekly_telemetry_aggregator.main import (
    ACTIVE_CUTOFF_MINUTES,
    CROSS_CHECK_ABS,
    DEFAULT_HARNESS_COST_RATE_USD_PER_MTOK,
    EXIT_OK,
    EXIT_PARTIAL,
    EXIT_TOTAL_FAILURE,
    HARNESS_COST_RATES_USD_PER_MTOK,
    _audit_record,
    _check_migrations,
    _copilot_doctor_details,
    _doctor_draft_targets,
    _doctor_harness_eval_version,
    _doctor_opencode_version,
    _doctor_output_dir_guard,
    _doctor_output_probe,
    _doctor_project_root,
    _doctor_session_providers,
    _doctor_tool_presence,
    _doctor_watch_repos,
    _fetch_session_reads,
    _harness_cost_rates,
    _placeholder_fields,
    _placeholder_message,
    _session_part_timestamps,
    _SessionReads,
    _truncate,
    _unknown_session_source_types,
    _usage_active_excluded,
    _usage_advisor_excluded,
    _usage_cost_warnings,
    _usage_no_steps_status,
    _version_tuple,
    build_usage,
    doctor,
    estimate_costs,
    harness,
    run,
)
from weekly_telemetry_aggregator.models import Period, SessionUsage, StepFinish, WarningEntry
from weekly_telemetry_aggregator.sqlite_reader import PartRecord

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


def test_run_writes_run_provenance(tmp_path: Path, monkeypatch):
    db = _seed_n(tmp_path / "opencode.db", 1)
    cfg = _cfg(tmp_path, db)
    monkeypatch.setattr(
        "weekly_telemetry_aggregator.main._run_provenance",
        lambda *_: {
            "repository_path": "/repo",
            "branch": "main",
            "commit_sha": "abc",
            "working_tree_dirty": False,
            "run_started_at": "2026-08-12T00:00:00Z",
            "pipeline_version": "test",
        },
    )
    assert run(cfg, anchor=RUN_TIME.isoformat()) == EXIT_OK
    data = json.loads(active_run_file(tmp_path, "weekly-summary-2026-08-12.json").read_text())
    assert data["run_provenance"]["commit_sha"] == "abc"
    assert data["run_provenance"]["start_time"] == "2026-08-12T00:00:00Z"
    assert data["run_provenance"]["run_started_at"] == data["run_provenance"]["start_time"]


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


def test_build_usage_collects_tool_fingerprints():
    class FingerprintAdapter:
        name = "fake"

        def session_steps(self, *a):
            return [type("S", (), {"model": "m", "cost": 0.1})()]

        def session_tools(self, *a):
            return {}, {}, {}

        def session_tool_fingerprints(self, *a):
            return ({"bash": {"abc": 2}}, {"bash": {"def": 1}})

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
        FingerprintAdapter(),
        period=period,
        run_time=RUN_TIME,
        cfg=_cfg(Path("/tmp"), Path("/x.db")),
        warnings=warnings,
    )
    assert failed is False
    assert usage is not None
    assert usage.tool_arg_fingerprints == {"bash": {"abc": 2}}
    assert usage.tool_result_fingerprints == {"bash": {"def": 1}}


def test_build_usage_populates_edit_and_user_timestamps():
    """Régression P6.3 : les timestamps edit/write et user doivent être peuplés.

    Sans ce câblage, `classify_production_review` retourne `review_pct=None` pour
    toute session ayant de l'activité edit/write (champs déclarés mais jamais écrits).
    """
    from weekly_telemetry_aggregator.sqlite_reader import PartRecord

    base = RUN_TIME - timedelta(minutes=30)
    later = base + timedelta(seconds=40)

    class TimestampAdapter:
        name = "fake"

        def session_steps(self, *a):
            return [type("S", (), {"model": "m", "cost": 0.1})()]

        def session_tools(self, *a):
            return {"edit": 1}, {}, {}

        def session_tool_fingerprints(self, *a):
            return {}, {}

        def session_user_turns(self, *a):
            return ["fais"]

        def session_context_chars(self, *a):
            return {}

        def session_aggregates(self, *a):
            return None

        def session_parts(self, *a):
            return [
                PartRecord(ts=base, kind="user", text="fais"),
                PartRecord(ts=base + timedelta(seconds=5), kind="tool", tool_name="edit"),
                PartRecord(ts=base + timedelta(seconds=10), kind="tool", tool_name="read"),
                PartRecord(ts=later, kind="user", text="relis"),
            ]

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
        TimestampAdapter(),
        period=period,
        run_time=RUN_TIME,
        cfg=_cfg(Path("/tmp"), Path("/x.db")),
        warnings=warnings,
    )
    assert failed is False
    assert usage is not None
    assert usage.user_turn_timestamps == [base, later]
    assert usage.edit_write_timestamps == [base + timedelta(seconds=5)]


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


def test_doctor_placeholder_project_root_is_problem(tmp_path: Path, capsys):
    """Install réelle : placeholders /path/to/ jamais substitués → PROBLEM explicite
    et actionnable, pas le fatal générique « project_root manquant »."""
    db = _seed_n(tmp_path / "opencode.db", 3)
    cfg = _cfg(tmp_path, db)
    cfg.project_root = Path("/path/to/weekly-advisor-kit")
    assert doctor(cfg) == EXIT_TOTAL_FAILURE
    out = capsys.readouterr().out
    assert "config jamais adaptée à cette installation" in out
    assert "substituer project_root" in out
    assert "/path/to/" in out
    assert "project_root manquant" not in out


def test_doctor_valid_config_no_placeholder_problem(tmp_path: Path, capsys):
    """Régression zéro : config substituée → aucun PROBLEM placeholder nouveau."""
    _ = (tmp_path / ".opencode").mkdir()
    db = _seed_n(tmp_path / "opencode.db", 3)
    cfg = _cfg(tmp_path, db)
    cfg.project_root = tmp_path
    assert doctor(cfg) in (EXIT_OK, EXIT_PARTIAL)
    out = capsys.readouterr().out
    assert "jamais adaptée" not in out


def test_doctor_warns_output_dir_under_plugins_tree(tmp_path: Path, fake_opencode, capsys):
    """Run lancé cwd=moteur → reports/ dans le plugin : doctor doit nommer le défaut.

    Le warning est non-bloquant (rc reste EXIT_OK) : la racine du kit contient
    bien .opencode et la config est chargée, seul l'output_dir pollue l'arbre
    plugins (observé 24/08)."""
    kit = tmp_path / "kit"
    plugin_reports = kit / ".opencode" / "plugins" / "weekly-advisor-engine" / "reports"
    plugin_reports.mkdir(parents=True)
    db = _seed_n(tmp_path / "sessions.db", 3)
    cfg = _cfg(tmp_path, db, output_dir=plugin_reports)
    cfg.project_root = kit  # racine du kit (contient .opencode) — repo audité légitime

    rc = doctor(cfg, cwd=tmp_path, config_loaded=True)

    assert rc in (EXIT_OK, EXIT_PARTIAL)  # warning, pas un problème bloquant
    out = capsys.readouterr().out
    assert "output_dir résout sous l'arbre plugins" in out


def test_placeholder_fields_detect_windows_style_separators(tmp_path: Path):
    """CI Windows : str(Path) y produit des « \\ » — le substring « path/to » ne
    matchait plus et la garde devenait muette (doctor muet + run sans warning).
    Chaînes brutes Windows-style : la détection doit normaliser les séparateurs
    avant match, quel que soit l'OS du runner."""
    db = _seed_n(tmp_path / "opencode.db", 3)
    cfg = _cfg(tmp_path, db)
    cfg.project_root = "C:\\fake\\path\\to\\weekly"
    cfg.output_dir = "D:\\other\\path\\to\\reports"
    assert _placeholder_fields(cfg) == ["project_root", "output_dir"]


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


# ================================================== v6.2 cellule 1.2 (doctor multi-providers)


def test_doctor_lists_active_provider_status_and_source(tmp_path: Path, capsys, fake_opencode):
    """Cellule 1.2 : le doctor itère sur les providers du registre — section
    statut+source par harnais, chemin de la base détectée affiché, compteur de
    migrations conservé pour l'adapter SQLite."""
    _ = (tmp_path / ".opencode").mkdir()
    db = _seed_n(tmp_path / "opencode.db", 3)
    cfg = _cfg(tmp_path, db)
    cfg.project_root = tmp_path
    rc = doctor(cfg)
    out = capsys.readouterr().out
    assert rc in (EXIT_OK, EXIT_PARTIAL)
    assert "doctor: [opencode] OK" in out
    assert str(db) in out  # source (chemin de la base) affichée
    assert "migrations=" in out  # check migrations conservé


def test_doctor_no_provider_available_exits_two(tmp_path: Path, capsys):
    """Exit 2 dès qu'AUCUNE source n'est disponible : base absente ou toutes
    sources désactivées — plus aucun message 'base OpenCode' en dur."""
    _ = (tmp_path / ".opencode").mkdir()
    cfg = _cfg(tmp_path, tmp_path / "missing.db")
    cfg.project_root = tmp_path
    assert doctor(cfg) == EXIT_TOTAL_FAILURE
    assert "aucune source de sessions disponible" in capsys.readouterr().out

    capsys.readouterr()
    cfg2 = _cfg(tmp_path, tmp_path / "missing.db")
    cfg2.project_root = tmp_path
    cfg2.session_sources = [{"type": "opencode", "enabled": False}]
    assert doctor(cfg2) == EXIT_TOTAL_FAILURE


def test_doctor_closes_provider_even_when_check_schema_raises(tmp_path: Path, monkeypatch):
    """#9 : un provider dont check_schema() lève doit être close() quand même —
    la connexion n'est jamais fuite, même en échec de schéma."""
    import weekly_telemetry_aggregator.main as main_mod

    class BrokenProvider(FakeSessionProvider):
        def check_schema(self) -> None:
            raise RuntimeError("boom")

    _ = (tmp_path / ".opencode").mkdir()
    cfg = _cfg(tmp_path, tmp_path / "absent.db")
    cfg.project_root = tmp_path
    ko_src = BrokenProvider("alpha", [])
    ok_src = FakeSessionProvider("opencode", [])
    monkeypatch.setattr(main_mod, "build_providers", lambda _cfg: [ko_src, ok_src])
    rc = doctor(cfg)
    assert ko_src.closed is True  # close garanti malgré check_schema KO
    assert ok_src.closed is True
    assert rc == EXIT_PARTIAL  # mixte OK/KO → partiel (cf. #13)


def test_doctor_exit_partial_on_mixed_sources(tmp_path: Path, monkeypatch, capsys):
    """#13 : ≥1 source utilisable ET ≥1 source KO → EXIT_PARTIAL (1) ; tout KO → 2 ;
    tout OK → 0. La constante EXIT_PARTIAL cesse d'être morte dans doctor()."""
    import weekly_telemetry_aggregator.main as main_mod

    class BrokenProvider(FakeSessionProvider):
        def check_schema(self) -> None:
            raise RuntimeError("boom")

    _ = (tmp_path / ".opencode").mkdir()
    cfg = _cfg(tmp_path, tmp_path / "absent.db")
    cfg.project_root = tmp_path
    ok_src = FakeSessionProvider("opencode", [])
    ko_src = BrokenProvider("alpha", [])
    monkeypatch.setattr(main_mod, "build_providers", lambda _cfg: [ok_src, ko_src])
    assert doctor(cfg) == EXIT_PARTIAL
    out = capsys.readouterr().out
    assert "[alpha] KO" in out
    assert "[opencode] OK" in out


def test_doctor_generic_multi_harness_sections(tmp_path: Path, monkeypatch, capsys, fake_opencode):
    """Générique : aucune liste de harnais en dur — un futur ClaudeCodeProvider
    s'affiche comme n'importe quel provider sans modifier le doctor."""
    import weekly_telemetry_aggregator.main as main_mod

    _ = (tmp_path / ".opencode").mkdir()
    cfg = _cfg(tmp_path, tmp_path / "unused.db")
    cfg.project_root = tmp_path
    monkeypatch.setattr(
        main_mod,
        "build_providers",
        lambda _cfg: [
            FakeSessionProvider("opencode", []),
            FakeSessionProvider("claudecode", []),
        ],
    )
    rc = doctor(cfg)
    out = capsys.readouterr().out
    assert rc in (EXIT_OK, EXIT_PARTIAL)
    assert "doctor: [opencode] OK" in out
    assert "doctor: [claudecode] OK" in out


# ================================================== cellule 2.1 (cibles de drafting au doctor)


def test_doctor_shows_default_draft_target(tmp_path: Path, capsys, fake_opencode):
    """Marqueur .opencode présent → cible affichée en mode défaut (A2 : les
    marqueurs ne décident plus), warning qui ne parle plus de marqueur."""
    _ = (tmp_path / ".opencode").mkdir()
    db = _seed_n(tmp_path / "opencode.db", 3)
    cfg = _cfg(tmp_path, db)
    cfg.project_root = tmp_path
    assert doctor(cfg) in (EXIT_OK, EXIT_PARTIAL)
    out = capsys.readouterr().out
    assert "doctor: cibles de drafting: opencode (défaut)" in out
    assert "WARNING" not in out or "marqueur" not in out


def test_doctor_ignores_claude_marker(tmp_path: Path, capsys):
    """A2 : `.claude` + `.opencode` → plus de priorité de détection, le défaut
    opencode s'applique (régression du run 2026-10-03, cible claude-code)."""
    _ = (tmp_path / ".claude").mkdir()
    _ = (tmp_path / ".opencode").mkdir()
    db = _seed_n(tmp_path / "opencode.db", 3)
    cfg = _cfg(tmp_path, db)
    cfg.project_root = tmp_path
    _ = doctor(cfg)
    out = capsys.readouterr().out
    assert "doctor: cibles de drafting: opencode (défaut)" in out
    # A3 : le warning de marqueur *nomme* le harnais étranger — comportement
    # voulu. Ce qui ne doit jamais apparaître, c'est claude-code comme cible
    # résolue (le marqueur n'a aucun pouvoir de décision).
    assert "cibles de drafting: claude-code" not in out
    assert "marqueur(s) de harnais hors cible résolue : .claude/ → claude-code" in out


def test_doctor_shows_config_override_draft_target(tmp_path: Path, capsys, fake_opencode):
    """Override config > défaut : cible affichée en mode config."""
    _ = (tmp_path / ".opencode").mkdir()
    db = _seed_n(tmp_path / "opencode.db", 3)
    cfg = _cfg(tmp_path, db)
    cfg.project_root = tmp_path
    cfg = replace(
        cfg,
        draft_targets=replace(cfg.draft_targets, mode="override", targets=["codex"]),
    )
    assert doctor(cfg) in (EXIT_OK, EXIT_PARTIAL)
    assert "doctor: cibles de drafting: codex (config)" in capsys.readouterr().out


def test_doctor_shows_legacy_draft_target(tmp_path: Path, capsys, fake_opencode):
    """[] = legacy : toutes les cibles affichées en mode legacy."""
    _ = (tmp_path / ".opencode").mkdir()
    db = _seed_n(tmp_path / "opencode.db", 3)
    cfg = _cfg(tmp_path, db)
    cfg.project_root = tmp_path
    cfg = replace(cfg, draft_targets=replace(cfg.draft_targets, mode="legacy"))
    assert doctor(cfg) in (EXIT_OK, EXIT_PARTIAL)
    assert "doctor: cibles de drafting: toutes cibles (legacy)" in capsys.readouterr().out


def test_doctor_warns_default_draft_target_without_marker(tmp_path: Path, capsys, fake_opencode):
    """Aucun draft_targets explicite → défaut opencode affiché + WARNING (rc
    inchangé : la sentinelle project_root-sans-.opencode reste le vrai blocant)."""
    empty_root = tmp_path / "vide"
    empty_root.mkdir()  # aucun marqueur
    db = _seed_n(tmp_path / "opencode.db", 3)
    cfg = _cfg(tmp_path, db)
    cfg.project_root = empty_root
    assert doctor(cfg) == EXIT_TOTAL_FAILURE  # sentinelle existante, inchangée
    out = capsys.readouterr().out
    assert "doctor: cibles de drafting: opencode (défaut)" in out
    assert "WARNING: aucun draft_targets explicite en config" in out


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


def test_doctor_warns_below_harness_minimum(tmp_path: Path, monkeypatch, capsys):
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
    assert "minimum requis" in out


def test_doctor_accepts_newer_harness_version(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/bin/harness-eval" if name == "harness-eval" else None
    )
    monkeypatch.setattr(
        "subprocess.run",
        lambda args, **kwargs: (
            _FakeProc(0, stdout="harness-eval 8.2.0\n") if "--version" in args else _FakeProc(0)
        ),
    )
    cfg = _cfg(tmp_path, tmp_path / "opencode.db")
    cfg.project_root = tmp_path
    cfg.harness_eval_version = "7.9.0"
    doctor(cfg)
    out = capsys.readouterr().out
    assert "harness-eval" not in out


def test_harness_digest_problems_flags_incompatible_shape():
    from weekly_telemetry_aggregator.harness_scope import harness_digest_problems

    assert harness_digest_problems({"inspection": {"summary": {"errors": 0}}}) == []
    assert harness_digest_problems({"rules": [{"rule": "x"}]}) == []
    problems = harness_digest_problems({"unexpected": True})
    assert len(problems) == 1 and "incompatible" in problems[0]
    assert harness_digest_problems(None) != []


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
    # recent trié par updated décroissant (le plus récent en premier) — id canonique
    assert sel["recent"][0]["session_id"] == "opencode:ses_active"


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
    assert by_id["opencode:ses_child"]["parent_id"] == "opencode:ses_parent"
    assert by_id["opencode:ses_parent"]["parent_id"] is None


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
    """Le schéma post-migration (metadata `session` + telemetry `part`) est détecté par l'adaptateur unifié."""
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
    assert adapter.name == "opencode"
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
    """Adaptateur unifié : les sessions CLI (`session`) ET serveur (`session_v2`) sont comptées."""
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
    assert adapter.name == "opencode"
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


def test_detect_v1_session_only(tmp_path: Path):
    """OpenCode V1 (table `session` seule, sans session_v2) est détecté (v6.x).

    Régression : l'ancien DualAdapter exigeait session_v2 et rejetait les bases
    V1 pures (ex. Nicolas, opencode 1.18.19). L'adaptateur unifié accepte
    `session` OU `session_v2` (ou les deux).
    """
    import sqlite3

    from weekly_telemetry_aggregator.sqlite_reader import detect_db

    db = tmp_path / "opencode.db"
    ts = RUN_TIME - timedelta(hours=2)
    ts_ms = int(ts.timestamp() * 1000)
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE session (
            id TEXT PRIMARY KEY, parent_id TEXT, title TEXT, model TEXT, agent TEXT,
            directory TEXT, cost REAL, tokens_input REAL, tokens_output REAL,
            tokens_reasoning REAL, tokens_cache_read REAL, tokens_cache_write REAL,
            time_created INTEGER, time_updated INTEGER
        );
        CREATE TABLE part (session_id TEXT, data TEXT, time_created INTEGER);
        CREATE TABLE message (session_id TEXT, data TEXT, time_created INTEGER);
        CREATE TABLE migration (id INTEGER PRIMARY KEY);
        """
    )
    conn.execute("INSERT INTO migration (id) VALUES (0)")
    conn.execute(
        "INSERT INTO session (id, parent_id, title, model, agent, directory, cost, "
        "tokens_input, tokens_output, tokens_reasoning, tokens_cache_read, "
        "tokens_cache_write, time_created, time_updated) "
        "VALUES (?, NULL, 'V1 only', '{}', NULL, NULL, 0.0, 0,0,0,0,0, ?, ?)",
        ("ses_v1", ts_ms, ts_ms),
    )
    conn.execute(
        "INSERT INTO part (session_id, data, time_created) VALUES (?, ?, ?)",
        ("ses_v1", '{"type":"step-finish","cost":0.5,"tokens":{"input":5,"output":1}}', ts_ms),
    )
    conn.commit()
    conn.close()

    path, adapter = detect_db(str(db))
    assert adapter.name == "opencode"
    # V1 pur : uniquement la table `session`.
    assert adapter._session_tables == ["session"]
    metas = adapter.list_sessions(0)
    assert {m.session_id for m in metas} == {"ses_v1"}


def test_detect_non_opencode_db_raises_clear_error(tmp_path: Path):
    """Une base non-OpenCode lève DataSourceError avec family/path exploitables."""
    import sqlite3

    from weekly_telemetry_aggregator.sqlite_reader import DataSourceError, detect_db

    db = tmp_path / "opencode.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE unrelated (id INTEGER)")
    conn.commit()
    conn.close()

    with pytest.raises(DataSourceError) as exc:
        detect_db(str(db))
    assert exc.value.path is not None
    assert isinstance(exc.value.family, str)
    assert "non reconnu" in str(exc.value)


def test_parse_tool_invocation_and_file_shapes(tmp_path: Path):
    """Forward-compat v1.18.19+ : tool-invocation + file (filename/url/source.path)."""
    import sqlite3

    from weekly_telemetry_aggregator.sqlite_reader import detect_db

    db = tmp_path / "opencode.db"
    ts = RUN_TIME - timedelta(hours=2)
    ts_ms = int(ts.timestamp() * 1000)
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE session (
            id TEXT PRIMARY KEY, parent_id TEXT, title TEXT, model TEXT, agent TEXT,
            directory TEXT, cost REAL, tokens_input REAL, tokens_output REAL,
            tokens_reasoning REAL, tokens_cache_read REAL, tokens_cache_write REAL,
            time_created INTEGER, time_updated INTEGER
        );
        CREATE TABLE part (session_id TEXT, data TEXT, time_created INTEGER);
        CREATE TABLE message (session_id TEXT, data TEXT, time_created INTEGER);
        CREATE TABLE migration (id INTEGER PRIMARY KEY);
        """
    )
    conn.execute("INSERT INTO migration (id) VALUES (0)")
    conn.execute(
        "INSERT INTO session (id, parent_id, title, model, agent, directory, cost, "
        "tokens_input, tokens_output, tokens_reasoning, tokens_cache_read, "
        "tokens_cache_write, time_created, time_updated) "
        "VALUES (?, NULL, 'NewShape', '{}', NULL, NULL, 0.0, 0,0,0,0,0, ?, ?)",
        ("ses_new", ts_ms, ts_ms),
    )
    conn.execute(
        "INSERT INTO part (session_id, data, time_created) VALUES (?, ?, ?)",
        (
            "ses_new",
            '{"type":"tool-invocation","toolInvocation":{"state":"result","toolName":"myTool","args":{"x":1},"result":"done"}}',
            ts_ms,
        ),
    )
    conn.execute(
        "INSERT INTO part (session_id, data, time_created) VALUES (?, ?, ?)",
        (
            "ses_new",
            '{"type":"file","filename":"foo/bar.md","url":"file:///x/foo/bar.md",'
            '"source":{"text":{"value":"@foo/bar.md","start":0,"end":9},"type":"file","path":"foo/bar.md"}}',
            ts_ms,
        ),
    )
    conn.commit()
    conn.close()

    path, adapter = detect_db(str(db))
    assert adapter.name == "opencode"

    tool_calls, _arg_chars, _skills = adapter.session_tools("ses_new", 0, 9_999_999_999_999)
    assert tool_calls.get("myTool") == 1, f"tool name not parsed: {tool_calls}"

    parts = {p.kind: p for p in adapter.session_parts("ses_new")}
    assert parts["tool"].tool_name == "myTool"
    assert parts["tool"].tool_input == '{"x": 1}'
    assert parts["tool"].tool_output == "done"
    assert parts["file"].text == "foo/bar.md"


def test_self_cost_falls_back_to_weekly_advisor_session(tmp_path: Path, capsys):
    """v5.30 (E) : self-cost trouve la session agent la plus récente sans titre exact."""
    from weekly_telemetry_aggregator.costing import self_cost

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


def test_rerun_different_window_coexists(tmp_path: Path):
    """v6.0.k (F1) : fenêtres différentes = runs distincts (dir UUID par run)."""
    db = _seed_n(tmp_path / "opencode.db", 2)
    cfg = _cfg(tmp_path, db)
    cfg.lookback_days = 7
    run(cfg, anchor=RUN_TIME.isoformat())
    cfg.lookback_days = 15
    rc = run(cfg, anchor=RUN_TIME.isoformat())
    assert rc == EXIT_OK
    runs = sorted(d for d in (tmp_path / "runs").glob("2026-08-12-*") if d.is_dir())
    assert len(runs) == 2


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


def test_build_selection_preserves_statuses_when_recent_audit_is_bounded():
    """Le plafond de ``recent`` ne masque aucun statut du journal d'audit."""
    from weekly_telemetry_aggregator.main import _build_selection

    audit = [
        {"session_id": "s-active", "status": "active", "updated": "2026-08-12T10:00:00Z"},
        {"session_id": "s-included", "status": "included", "updated": "2026-08-12T09:00:00Z"},
        {"session_id": "s-error", "status": "error", "updated": "2026-08-12T08:00:00Z"},
    ]

    selection = _build_selection(audit, limit=1)

    # ``recent`` is intentionally bounded, but counters remain an untruncated
    # status index so the JOIN can expose every worker/session disposition.
    assert [record["session_id"] for record in selection["recent"]] == ["s-active"]
    assert selection["recent"][0]["status"] == "active"
    assert selection["excluded_active"] == 1
    assert selection["excluded_error"] == 1
    assert selection["counted_all"] == 1


# ============================================================ multi-harnais (cellule 1.1)

FAKE_TS = tzutc(2026, 8, 11, 22, 0, 0)  # dans la fenêtre, hors cut-off session active


def _two_fake_sources():
    from helpers import FakeSessionProvider, fake_meta, make_step

    src_a = FakeSessionProvider(
        "alpha",
        [fake_meta("alpha", "a1", title="Alpha one", updated=FAKE_TS)],
        steps_by_session={"a1": [make_step("a1", FAKE_TS, cost=0.4)]},
    )
    src_b = FakeSessionProvider(
        "beta",
        [fake_meta("beta", "b1", title="Beta one", updated=FAKE_TS)],
        steps_by_session={"b1": [make_step("b1", FAKE_TS, cost=0.6)]},
    )
    return [src_a, src_b]


def test_run_fuses_two_fake_sources_with_canonical_ids(tmp_path: Path, monkeypatch):
    """Deux sources factices → usages fusionnés, ids canoniques <harness>:<id>."""
    import weekly_telemetry_aggregator.main as main_mod

    sources = _two_fake_sources()
    monkeypatch.setattr(main_mod, "build_providers", lambda _cfg: sources)
    cfg = _cfg(tmp_path, tmp_path / "absent.db")  # base absente : ne doit pas être ouverte
    rc = run(cfg, anchor=RUN_TIME.isoformat())
    assert rc == EXIT_OK
    assert all(src.closed for src in sources)  # cycle de vie possédé par run()
    data = json.loads(
        (active_run_file(tmp_path, "weekly-summary-2026-08-12.json")).read_text(encoding="utf-8")
    )
    assert data["totals"]["session_count"] == 2
    assert data["totals"]["total_cost_usd"] == pytest.approx(1.0)
    top_ids = {t["session_id"] for t in data["top_sessions_by_cost"]}
    assert top_ids == {"alpha:a1", "beta:b1"}
    recent_ids = {r["session_id"] for r in data["selection"]["recent"]}
    assert recent_ids == {"alpha:a1", "beta:b1"}


def test_run_dedups_same_harness_duplicate_ids_first_source_wins(tmp_path: Path, monkeypatch):
    """Deux sources du même harnais exposant le même id canonique → compté UNE fois.

    La première source (ordre cfg.session_sources) gagne : contenu = ses données ;
    UserWarning récapitulative émise + trace dans summary.warnings.
    """
    from helpers import FakeSessionProvider, fake_meta, make_step

    import weekly_telemetry_aggregator.main as main_mod

    src_a = FakeSessionProvider(
        "alpha",
        [fake_meta("alpha", "a1", title="Alpha one", updated=FAKE_TS)],
        steps_by_session={"a1": [make_step("a1", FAKE_TS, cost=0.4)]},
    )
    src_b = FakeSessionProvider(
        "alpha",
        [fake_meta("alpha", "a1", title="Alpha dup", updated=FAKE_TS)],
        steps_by_session={"a1": [make_step("a1", FAKE_TS, cost=9.9)]},
    )
    monkeypatch.setattr(main_mod, "build_providers", lambda _cfg: [src_a, src_b])
    cfg = _cfg(tmp_path, tmp_path / "absent.db")
    with pytest.warns(UserWarning, match="doublon.*alpha"):
        rc = run(cfg, anchor=RUN_TIME.isoformat())
    assert rc == EXIT_OK
    data = json.loads(
        (active_run_file(tmp_path, "weekly-summary-2026-08-12.json")).read_text(encoding="utf-8")
    )
    assert data["totals"]["session_count"] == 1  # compté une seule fois
    assert data["totals"]["total_cost_usd"] == pytest.approx(0.4)  # première source gagne
    top = next(t for t in data["top_sessions_by_cost"] if t["session_id"] == "alpha:a1")
    assert top["cost_usd"] == pytest.approx(0.4)
    assert any("doublon" in w["message"] and "alpha" in w["message"] for w in data["warnings"])


def test_run_same_harness_disjoint_ids_all_counted_no_dedup_warning(tmp_path: Path, monkeypatch):
    """Sources du même harnais avec ids disjoints → tous comptés, aucun warning dédup."""
    import warnings as warnings_mod

    from helpers import FakeSessionProvider, fake_meta, make_step

    import weekly_telemetry_aggregator.main as main_mod

    src_a = FakeSessionProvider(
        "alpha",
        [fake_meta("alpha", "a1", title="Alpha one", updated=FAKE_TS)],
        steps_by_session={"a1": [make_step("a1", FAKE_TS, cost=0.4)]},
    )
    src_b = FakeSessionProvider(
        "alpha",
        [fake_meta("alpha", "a2", title="Alpha two", updated=FAKE_TS)],
        steps_by_session={"a2": [make_step("a2", FAKE_TS, cost=0.6)]},
    )
    monkeypatch.setattr(main_mod, "build_providers", lambda _cfg: [src_a, src_b])
    cfg = _cfg(tmp_path, tmp_path / "absent.db")
    with warnings_mod.catch_warnings():
        warnings_mod.simplefilter("error")  # tout warning → échec
        rc = run(cfg, anchor=RUN_TIME.isoformat())
    assert rc == EXIT_OK
    data = json.loads(
        (active_run_file(tmp_path, "weekly-summary-2026-08-12.json")).read_text(encoding="utf-8")
    )
    assert data["totals"]["session_count"] == 2
    assert data["totals"]["total_cost_usd"] == pytest.approx(1.0)


def test_run_dedup_warning_message_recap_format(tmp_path: Path, monkeypatch):
    """Message récapitulatif unique : « N session(s) en doublon ignorée(s) depuis <h> source #k »."""
    from helpers import FakeSessionProvider, fake_meta

    import weekly_telemetry_aggregator.main as main_mod

    src_a = FakeSessionProvider(
        "alpha",
        [fake_meta("alpha", "a1", updated=FAKE_TS), fake_meta("alpha", "a2", updated=FAKE_TS)],
    )
    src_b = FakeSessionProvider(
        "alpha",
        [fake_meta("alpha", "a2", updated=FAKE_TS), fake_meta("alpha", "a3", updated=FAKE_TS)],
    )
    monkeypatch.setattr(main_mod, "build_providers", lambda _cfg: [src_a, src_b])
    cfg = _cfg(tmp_path, tmp_path / "absent.db")
    with pytest.warns(UserWarning) as recorded:
        rc = run(cfg, anchor=RUN_TIME.isoformat())
    assert rc == EXIT_OK
    dup_warnings = [w for w in recorded if "doublon" in str(w.message)]
    assert len(dup_warnings) == 1  # récapitulatif unique
    assert "1 session(s) en doublon ignorée(s) depuis alpha source #2" in str(
        dup_warnings[0].message
    )


def test_run_warns_on_placeholder_config(tmp_path: Path):
    """#12 : la garde placeholders n'est plus réservée au doctor — run() émet un
    warning visible (stdout + summary) et continue (non fatal, zéro régression
    pour les configs valides)."""
    db = _seed_n(tmp_path / "opencode.db", 3)
    cfg = _cfg(tmp_path, db)
    cfg.project_root = Path("/path/to/weekly-advisor-kit")
    with pytest.warns(UserWarning, match="jamais adaptée"):
        rc = run(cfg, anchor=RUN_TIME.isoformat())
    assert rc == EXIT_OK  # non fatal : le run aboutit
    data = json.loads(
        (active_run_file(tmp_path, "weekly-summary-2026-08-12.json")).read_text(encoding="utf-8")
    )
    assert any("jamais adaptée" in w["message"] for w in data["warnings"])
    assert any("/path/to/" in w["message"] for w in data["warnings"])


def test_run_does_not_mutate_provider_metas(tmp_path: Path, monkeypatch):
    """#10 : la canonisation du parent_id se fait sur une copie (dataclasses.replace) —
    les HarnessSession du provider restent intactes, la fusion racine/enfant marche
    toujours downstream."""
    from helpers import fake_meta, make_step

    import weekly_telemetry_aggregator.main as main_mod

    root_meta = fake_meta("alpha", "root", title="Root", updated=FAKE_TS)
    child_meta = fake_meta("alpha", "child", title="Child", parent="root", updated=FAKE_TS)
    src = FakeSessionProvider(
        "alpha",
        [root_meta, child_meta],
        steps_by_session={
            "root": [make_step("root", FAKE_TS, cost=1.0)],
            "child": [make_step("child", FAKE_TS, cost=0.5)],
        },
    )
    monkeypatch.setattr(main_mod, "build_providers", lambda _cfg: [src])
    cfg = _cfg(tmp_path, tmp_path / "absent.db")
    cfg.include_subagents = True
    assert run(cfg, anchor=RUN_TIME.isoformat()) == EXIT_OK
    # metas originales intactes (parent_id brut préservé)
    assert child_meta.parent_id == "root"
    assert root_meta.parent_id is None
    # build_usage a bien lu la COPIE canonisée : fusion enfant→racine effective
    data = json.loads(
        (active_run_file(tmp_path, "weekly-summary-2026-08-12.json")).read_text(encoding="utf-8")
    )
    sel = data["selection"]
    assert sel["counted"] == 1
    assert sel["merged_children"] == 1


def test_run_merges_child_into_root_across_canonical_ids(tmp_path: Path, monkeypatch):
    """parent_id brut re-namespacé → fusion racine/enfant valable en multi-source."""
    from helpers import FakeSessionProvider, fake_meta, make_step

    import weekly_telemetry_aggregator.main as main_mod

    src = FakeSessionProvider(
        "alpha",
        [
            fake_meta("alpha", "root", title="Root", updated=FAKE_TS),
            fake_meta("alpha", "child", title="Child", parent="root", updated=FAKE_TS),
        ],
        steps_by_session={
            "root": [make_step("root", FAKE_TS, cost=1.0)],
            "child": [make_step("child", FAKE_TS, cost=0.5)],
        },
    )
    monkeypatch.setattr(main_mod, "build_providers", lambda _cfg: [src])
    cfg = _cfg(tmp_path, tmp_path / "absent.db")
    cfg.include_subagents = True
    rc = run(cfg, anchor=RUN_TIME.isoformat())
    assert rc == EXIT_OK
    data = json.loads(
        (active_run_file(tmp_path, "weekly-summary-2026-08-12.json")).read_text(encoding="utf-8")
    )
    sel = data["selection"]
    assert sel["counted"] == 1  # racine seule ; l'enfant est fusionné
    assert sel["merged_children"] == 1
    root_row = next(t for t in data["top_sessions_by_cost"] if t["session_id"] == "alpha:root")
    assert root_row["cost_usd"] == pytest.approx(1.5)


def test_run_zero_active_source_falls_back_to_local_db_with_warning(tmp_path: Path):
    """Aucun provider actif → warning + repli comportement historique detect_db."""
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
                "steps": [{"ts": ts, "cost": 1.0}],
            }
        ],
    )
    cfg = _cfg(tmp_path, db)
    cfg.session_sources = [{"type": "harnais-inexistant"}]
    with pytest.warns(UserWarning, match="repli sur la base OpenCode locale"):
        rc = run(cfg, anchor=RUN_TIME.isoformat())
    assert rc == EXIT_OK
    data = json.loads(
        (active_run_file(tmp_path, "weekly-summary-2026-08-12.json")).read_text(encoding="utf-8")
    )
    assert {t["session_id"] for t in data["top_sessions_by_cost"]} == {"opencode:ses_x"}


def test_run_writes_cost_estimates_for_unpriced_sessions(tmp_path: Path):
    """Steps sans coût → estimation tokens × taux du harnais (opencode défaut)."""
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
                "steps": [{"ts": ts, "input": 900_000}],  # output défaut 10 → 900_010 tokens
            },
            {
                "id": "ses_priced",
                "title": "Avec prix",
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
    estimates = data["cost_estimates"]
    assert set(estimates) == {"opencode:ses_noprice"}  # session avec prix → absente
    # taux opencode par défaut (9.0 $/Mtok) : 900_010 × 9.0 / 1e6
    assert estimates["opencode:ses_noprice"] == pytest.approx(round(900_010 * 9.0 / 1e6, 6))
    # champ first-class : l'ancien emplacement selection ne porte plus la clé
    assert "cost_estimates" not in data["selection"]


def test_run_cost_rate_override_per_source(tmp_path: Path):
    """Clé extra cost_rate_usd_per_mtok d'une source → taux surchargé."""
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
                "steps": [{"ts": ts, "input": 900_000}],
            }
        ],
    )
    cfg = _cfg(tmp_path, db)
    cfg.session_sources = [{"type": "opencode", "cost_rate_usd_per_mtok": 2.0}]
    run(cfg, anchor=RUN_TIME.isoformat())
    data = json.loads(
        (active_run_file(tmp_path, "weekly-summary-2026-08-12.json")).read_text(encoding="utf-8")
    )
    estimates = data["cost_estimates"]
    assert estimates["opencode:ses_noprice"] == pytest.approx(round(900_010 * 2.0 / 1e6, 6))


def test_run_omits_cost_estimates_when_nothing_to_estimate(tmp_path: Path):
    """Toutes sessions avec coût enregistré → champ absent (only-if-non-empty)."""
    db = tmp_path / "opencode.db"
    ts = RUN_TIME - timedelta(hours=2)
    seed_v1_file(
        db,
        [
            {
                "id": "ses_priced",
                "title": "Avec prix",
                "start": ts,
                "updated": ts,
                "steps": [{"ts": ts, "cost": 0.2}],
            }
        ],
    )
    cfg = _cfg(tmp_path, db)
    run(cfg, anchor=RUN_TIME.isoformat())
    data = json.loads(
        (active_run_file(tmp_path, "weekly-summary-2026-08-12.json")).read_text(encoding="utf-8")
    )
    assert "cost_estimates" not in data
    assert "cost_estimates" not in data["selection"]


def test_legacy_summary_with_selection_cost_estimates_still_readable(tmp_path: Path):
    """Artefact legacy (clé sous selection) : lecture brute inchangée.

    writer.py ne désérialise pas WeeklySummary (les consommateurs lisent le
    JSON en dict brut) — un ancien artefact reste donc tel quel lisible et
    n'entre jamais en collision avec le nouveau champ top-level.
    """
    legacy = {
        "schema_version": 2,
        "selection": {"window_touched": 3, "cost_estimates": {"opencode:ses_x": 0.42}},
    }
    path = tmp_path / "weekly-summary-legacy.json"
    path.write_text(json.dumps(legacy), encoding="utf-8")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["selection"]["cost_estimates"] == {"opencode:ses_x": 0.42}
    assert data.get("cost_estimates") is None  # pas de collision top-level


# ---- cellule 2.2 : projection multi-harnais + baseline + matrice 5.5 ----------


def _make_kit(tmp_path: Path) -> Path:
    kit = tmp_path / "kit"
    (kit / ".opencode/skills/kit-skill").mkdir(parents=True)
    (kit / ".opencode/commands").mkdir(parents=True)
    (kit / ".opencode/skills/kit-skill/SKILL.md").write_text("kit skill", encoding="utf-8")
    (kit / ".opencode/commands/kit-cmd.md").write_text("kit cmd", encoding="utf-8")
    return kit


def test_harness_projects_detected_harness_and_records_orphans(tmp_path: Path, monkeypatch):
    project = tmp_path / "project"
    (project / ".claude/skills/user-skill").mkdir(parents=True)
    (project / ".claude/skills/user-skill/SKILL.md").write_text("real", encoding="utf-8")

    def fake_run(args, **kwargs):
        if "--version" in args:
            return _FakeProc(0, stdout="harness-eval 7.9.0")
        projection = Path(args[2])
        # fichiers réels + contenu engine injecté, tous dans la projection
        assert (projection / ".claude/skills/user-skill/SKILL.md").is_file()
        assert (projection / ".claude/skills/kit-skill/SKILL.md").is_file()
        assert (projection / ".claude/commands/kit-cmd.md").is_file()
        output = Path(args[args.index("--output") + 1])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("{}", encoding="utf-8")
        return _FakeProc(0)

    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/harness-eval")
    monkeypatch.setattr("subprocess.run", fake_run)
    cfg = _cfg(tmp_path / "reports", tmp_path / "opencode.db", kit_root=_make_kit(tmp_path))
    cfg.project_root = project
    # A2 : plus de détection par marqueur — la projection de `.claude/skills`
    # suppose désormais un draft_targets explicite.
    cfg = replace(
        cfg,
        draft_targets=replace(cfg.draft_targets, mode="override", targets=["claude-code"]),
    )

    assert harness(cfg, anchor=RUN_TIME.isoformat()) == EXIT_OK

    digest = json.loads(
        active_run_file(cfg.output_dir, "weekly-harness-digest-2026-08-12.json").read_text(
            encoding="utf-8"
        )
    )
    draft_block = digest["draft_targets"]
    assert draft_block["mode"] == "override"
    assert draft_block["harnesses"] == ["claude-code"]
    assert ".claude/skills/kit-skill/SKILL.md" in draft_block["orphan_files"]
    assert ".claude/skills/user-skill/SKILL.md" not in draft_block["orphan_files"]
    assert draft_block["extra_projection_roots"] == [".claude/skills"]
    assert draft_block["surface_decision"]["decision"] == "portability"


def test_harness_baseline_created_then_reused(tmp_path: Path, monkeypatch):
    findings_per_call = [{"rule": "quality/a", "path": ".opencode/cmd.md"}]
    calls = 0

    def fake_run(args, **kwargs):
        nonlocal calls
        if "--version" in args:
            return _FakeProc(0, stdout="harness-eval 7.9.0")
        calls += 1
        output = Path(args[args.index("--output") + 1])
        output.parent.mkdir(parents=True, exist_ok=True)
        findings = (
            findings_per_call
            if calls == 1
            else [
                {"rule": "quality/a", "path": ".opencode/cmd.md"},
                {"rule": "quality/b", "path": ".opencode/new.md"},
            ]
        )
        output.write_text(json.dumps({"findings": findings}), encoding="utf-8")
        return _FakeProc(0)

    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/harness-eval")
    monkeypatch.setattr("subprocess.run", fake_run)
    cfg = _cfg(tmp_path / "reports", tmp_path / "opencode.db", kit_root=tmp_path / "empty-kit")
    (tmp_path / "empty-kit" / ".opencode").mkdir(parents=True)  # kit vide → zéro injection
    cfg.project_root = tmp_path / "project"
    (cfg.project_root / ".opencode/commands").mkdir(parents=True)
    (cfg.project_root / ".opencode/commands/cmd.md").write_text("cmd", encoding="utf-8")

    assert harness(cfg, anchor=RUN_TIME.isoformat()) == EXIT_OK
    baseline_path = cfg.output_dir / "weekly-harness-baseline.json"
    digest1 = json.loads(
        active_run_file(cfg.output_dir, "weekly-harness-digest-2026-08-12.json").read_text(
            encoding="utf-8"
        )
    )
    assert digest1["harness_baseline"]["status"] == "created"
    assert digest1["harness_baseline"]["captured_on"] == "2026-08-12"
    assert digest1["harness_baseline"]["finding_count"] == 1

    assert harness(cfg, anchor=RUN_TIME.isoformat()) == EXIT_OK
    digest2 = json.loads(
        active_run_file(cfg.output_dir, "weekly-harness-digest-2026-08-12.json").read_text(
            encoding="utf-8"
        )
    )
    assert digest2["harness_baseline"]["status"] == "reused"
    assert digest2["harness_baseline"]["captured_on"] == "2026-08-12"
    assert digest2["harness_baseline"]["new_findings"] == [
        {"rule": "quality/b", "path": ".opencode/new.md"}
    ]
    stored = json.loads(baseline_path.read_text(encoding="utf-8"))
    assert stored["finding_count"] == 1  # la baseline n'est jamais réécrite


def test_doctor_prints_remediation_surface_5_5(tmp_path: Path, fake_opencode, capsys):
    _ = (tmp_path / ".opencode").mkdir()
    db = _seed_n(tmp_path / "opencode.db", 1)
    project = tmp_path / "project"
    (project / ".opencode").mkdir(parents=True)
    (project / ".claude").mkdir()
    cfg = _cfg(tmp_path / "reports", db)
    cfg.project_root = project
    # A2 : la surface portability ne s'atteint plus par marqueur, seulement par
    # draft_targets explicite.
    cfg = replace(
        cfg,
        draft_targets=replace(cfg.draft_targets, mode="override", targets=["claude-code"]),
    )
    assert doctor(cfg) in (EXIT_OK, EXIT_PARTIAL)
    out = capsys.readouterr().out
    assert "surface de remédiation 5.5: portability" in out
    assert "cibles de drafting: claude-code" in out


def test_run_dedups_resumed_session_fork(tmp_path: Path, monkeypatch):
    """R3 : une session reprise (copie du transcript sous un nouvel id) est comptée UNE fois.

    La copie partage ses premiers turns avec l'original (contenu copié au fork,
    timestamps d'origine conservés) → seule la plus complète est retenue ;
    trace `resumed-duplicate` dans l'audit de sélection + warning.
    """
    from helpers import FakeSessionProvider, fake_meta, make_step

    import weekly_telemetry_aggregator.main as main_mod

    turns = [f"turn {i} — contenu partagé" for i in range(10)]

    class TurnProvider(FakeSessionProvider):
        def __init__(self, harness, metas, *, turns_by_session, **kw):
            super().__init__(harness, metas, **kw)
            self._turns = {self._key(sid): list(ts) for sid, ts in turns_by_session.items()}

        def session_user_turns(self, session_id: str, start_ms: int, end_ms: int):
            return self._turns.get(session_id, [])

    original = fake_meta("opencode", "ses_old", title="Original", updated=FAKE_TS)
    resumed = fake_meta("opencode", "ses_new", title="Resumed copy", updated=FAKE_TS)
    src = TurnProvider(
        "opencode",
        [original, resumed],
        steps_by_session={
            "ses_old": [make_step("ses_old", FAKE_TS, cost=0.4)],
            "ses_new": [make_step("ses_new", FAKE_TS, cost=0.2)],
        },
        turns_by_session={
            "ses_old": turns[:8],
            "ses_new": [*turns, "turn 10 — suite après reprise"],
        },
    )
    monkeypatch.setattr(main_mod, "build_providers", lambda _cfg: [src])
    cfg = _cfg(tmp_path, tmp_path / "absent.db")
    rc = run(cfg, anchor=RUN_TIME.isoformat())
    assert rc == EXIT_OK
    data = json.loads(
        (active_run_file(tmp_path, "weekly-summary-2026-08-12.json")).read_text(encoding="utf-8")
    )
    # La copie est PLUS complète (11 turns vs 8) → elle survit ; l'originale,
    # strictement préfixe de la copie, est absorbée (sémantique resume-fork).
    assert data["totals"]["session_count"] == 1
    assert data["selection"]["resumed_duplicates"] == 1
    statuses = {r["session_id"]: r["status"] for r in data["selection"]["recent"]}
    merged = [sid for sid, st in statuses.items() if st == "resumed-duplicate"]
    kept = {t["session_id"] for t in data["top_sessions_by_cost"]}
    assert len(merged) == 1 and merged[0] not in kept
    warn_msgs = [w["message"] for w in data.get("warnings", [])]
    assert any("session reprise fusionnée" in m for m in warn_msgs)


# ============================================================ A4 : global_roots
#
# Une racine skills globale est une surface LECTURE SEULE. Elle doit pouvoir
# alimenter le catalogue (OpenCode la charge réellement) sans jamais devenir
# une surface projetable : ni destination de projection, ni origine de draft,
# ni cible du scan de remédiation.


def test_global_roots_default_is_the_opencode_global_skills_root():
    from weekly_telemetry_aggregator.config import DEFAULT_GLOBAL_SKILL_ROOT

    assert TelemetryConfig().global_roots == [DEFAULT_GLOBAL_SKILL_ROOT]
    assert Path.home() / ".config" / "opencode" / "skills" == DEFAULT_GLOBAL_SKILL_ROOT


def _config_from(tmp_path: Path, payload: dict):
    """Config parsée depuis un vrai fichier — même chemin que le run."""
    from weekly_telemetry_aggregator.config import load_config

    conf = tmp_path / "weekly-telemetry-config.json"
    conf.write_text(json.dumps(payload), encoding="utf-8")
    return load_config(conf)


def test_global_roots_absent_key_keeps_documented_default(tmp_path):
    cfg = _config_from(tmp_path, {})
    assert len(cfg.global_roots) == 1
    assert cfg.global_roots[0] == Path.home() / ".config" / "opencode" / "skills"


def test_global_roots_empty_list_means_no_global_root(tmp_path):
    """Zéro racine globale est une config valide : le scan se comporte sans elle."""
    cfg = _config_from(tmp_path, {"global_roots": []})
    assert cfg.global_roots == []


def test_global_roots_config_expands_and_dedups(tmp_path):
    cfg = _config_from(tmp_path, {"global_roots": ["~/a-skills", "~/a-skills", "~/b-skills"]})
    assert cfg.global_roots == [Path.home() / "a-skills", Path.home() / "b-skills"]


def test_global_roots_malformed_warns_and_keeps_default(tmp_path):
    from weekly_telemetry_aggregator.config import DEFAULT_GLOBAL_SKILL_ROOT

    with pytest.warns(UserWarning, match="global_roots mal formé"):
        cfg = _config_from(tmp_path, {"global_roots": "~/.config/opencode/skills"})
    assert cfg.global_roots == [DEFAULT_GLOBAL_SKILL_ROOT]


def test_global_roots_non_string_entries_warn_and_are_skipped(tmp_path):
    with pytest.warns(UserWarning, match="global_roots : entrée ignorée"):
        cfg = _config_from(tmp_path, {"global_roots": ["~/ok-skills", 42, "", None]})
    assert cfg.global_roots == [Path.home() / "ok-skills"]


def test_global_roots_are_never_project_roots(tmp_path):
    """Invariant A4 : une racine globale n'est jamais une destination de projection."""
    from weekly_telemetry_aggregator.draft_targets import resolve_draft_targets
    from weekly_telemetry_aggregator.harness_scope import harness_extra_roots
    from weekly_telemetry_aggregator.main import resolve_skill_surface

    project = tmp_path / "project"
    glob = tmp_path / "global"
    (project / ".opencode").mkdir(parents=True)
    glob.mkdir()

    cfg = TelemetryConfig(project_root=project, global_roots=[glob])
    resolved = resolve_draft_targets(project, cfg.draft_targets)
    surface = resolve_skill_surface(project, resolved, cfg.global_roots)
    assert surface.global_roots == (glob,)
    assert glob not in surface.project_roots
    assert set(surface.project_roots).isdisjoint(surface.global_roots)
    # Aucune racine globale ne peut apparaître parmi les racines projetables.
    assert all(not Path(root).is_absolute() for root in harness_extra_roots(resolved))


def test_skill_surface_never_mixes_a_global_root_into_project_roots(tmp_path):
    """Un `global_roots` redéclarant une racine projet est retiré des racines projet."""
    from weekly_telemetry_aggregator.draft_targets import resolve_draft_targets
    from weekly_telemetry_aggregator.main import resolve_skill_surface

    project = tmp_path / "project"
    clash = project / ".opencode" / "skills"
    cfg = TelemetryConfig(project_root=project, global_roots=[clash])
    resolved = resolve_draft_targets(project, cfg.draft_targets)
    surface = resolve_skill_surface(project, resolved, cfg.global_roots)
    assert surface.project_roots == ()
    assert surface.global_roots == (clash,)


def test_skill_surface_without_resolved_harness_has_no_project_root(tmp_path):
    """Surface non déclarée n'est pas inventée : pas de racine projet."""
    from weekly_telemetry_aggregator.main import resolve_skill_surface

    surface = resolve_skill_surface(tmp_path, None, [tmp_path / "global"])
    assert surface.project_roots == ()
    assert surface.global_roots == (tmp_path / "global",)


def test_skill_surface_legacy_mode_covers_every_known_skills_root(tmp_path):
    from weekly_telemetry_aggregator.draft_targets import resolve_draft_targets
    from weekly_telemetry_aggregator.main import resolve_skill_surface

    project = tmp_path / "project"
    cfg = TelemetryConfig(project_root=project, global_roots=[])
    cfg = replace(cfg, draft_targets=replace(cfg.draft_targets, mode="legacy", targets=[]))
    surface = resolve_skill_surface(project, resolve_draft_targets(project, cfg.draft_targets), [])
    # Ordre = priorité des harnais (DRAFT_TARGET_PRIORITY), déterministe.
    assert surface.project_roots == (
        project / ".claude" / "skills",
        project / ".opencode" / "skills",
        project / ".github" / "prompts",
        project / ".github" / "skills",
        project / ".agents",
    )


def test_skill_surface_is_global_classifies_read_only_records(tmp_path):
    from weekly_telemetry_aggregator.main import resolve_skill_surface

    surface = resolve_skill_surface(tmp_path, None, [tmp_path / "global"])
    assert surface.is_global(tmp_path / "global" / "swarm-worker-protocol" / "SKILL.md") is True
    assert surface.is_global(tmp_path / "project" / "demo" / "SKILL.md") is False


def test_harness_extra_roots_opencode_case_unchanged(tmp_path):
    """Attestation : `harness_extra_roots` sur le cas opencode reste ∅.

    Le catalogue A5 n'y touche pas ; on épingle le contrat pour qu'une future
    table de surface ne réintroduise pas `.claude`/`.agents` en projection.
    """
    from weekly_telemetry_aggregator.draft_targets import (
        DRAFT_HARNESS_LAYOUTS,
        resolve_draft_targets,
    )
    from weekly_telemetry_aggregator.harness_scope import harness_extra_roots

    project = tmp_path / "project"
    _ = (project / ".opencode").mkdir(parents=True)
    cfg = TelemetryConfig(project_root=project)
    resolved = resolve_draft_targets(project, cfg.draft_targets)
    assert resolved.harnesses == ("opencode",)
    assert harness_extra_roots(resolved) == ()
    # Parité avec la formule pré-A5 pour le harnais opencode.
    legacy = {t for t in DRAFT_HARNESS_LAYOUTS["opencode"].skills}
    legacy.discard(".opencode/skills")
    assert set(harness_extra_roots(resolved)) == legacy


def test_remediation_target_guard_rejects_a_global_root_path(tmp_path):
    """Invariant A4 : la remédiation ne peut viser que `.opencode/…` du projet."""
    from weekly_telemetry_aggregator.harness_remediation import _resolve_target

    project = tmp_path / "project"
    allowed = project / ".opencode" / "skills" / "x" / "SKILL.md"
    allowed.parent.mkdir(parents=True)
    allowed.write_text("x\n", encoding="utf-8")
    for value in ("~/.config/opencode/skills/x/SKILL.md", "/etc/skills/x/SKILL.md", "../x"):
        with pytest.raises(ValueError):
            _resolve_target(project, value)
    assert _resolve_target(project, ".opencode/skills/x/SKILL.md")[1].endswith("SKILL.md")


# ================================================ A5 : un seul catalogue de skills


def _opencode_surface(project: Path, global_roots: list[Path]):
    """Surface skills telle que le run la résout pour le harnais opencode."""
    from weekly_telemetry_aggregator.draft_targets import resolve_draft_targets
    from weekly_telemetry_aggregator.main import resolve_skill_surface

    cfg = TelemetryConfig(project_root=project, global_roots=global_roots)
    return resolve_skill_surface(
        project, resolve_draft_targets(project, cfg.draft_targets), global_roots
    )


def test_skill_layouts_constant_is_gone():
    """A5 : plus de liste codée en dur à 3 harnais dans main."""
    from weekly_telemetry_aggregator import main as main_mod

    assert not hasattr(main_mod, "SKILL_LAYOUTS")


def test_default_opencode_surface_no_longer_scans_claude_or_agents(tmp_path):
    """A5 : `.claude/skills` et `.agents` sortent du catalogue opencode."""
    from weekly_telemetry_aggregator.draft_targets import resolve_draft_targets
    from weekly_telemetry_aggregator.main import resolve_skill_surface, scan_skill_records

    project = tmp_path / "project"
    for layout in (".opencode", ".claude", ".agents"):
        skill = project / layout / "skills" / f"{layout[1:]}-skill"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(f"---\nname: {layout[1:]}\n---\n", encoding="utf-8")

    cfg = TelemetryConfig(project_root=project, global_roots=[])
    surface = resolve_skill_surface(project, resolve_draft_targets(project, cfg.draft_targets), [])
    assert [r.skill_id for r in scan_skill_records(surface)] == ["opencode-skill"]


def _skill_file(root: Path, sid: str, *, description: str = "d", **meta) -> Path:
    lines = "".join(f"{key}: {value}\n" for key, value in meta.items())
    path = root / sid / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {sid}\ndescription: {description}\n{lines}---\n\ncorps\n", encoding="utf-8"
    )
    return path


def test_scan_skill_records_dedups_by_canonical_path_not_by_name(tmp_path):
    """A5 : deux homonymes dans deux racines sont deux fichiers distincts."""
    from weekly_telemetry_aggregator.main import scan_skill_records

    project = tmp_path / "project"
    glob = tmp_path / "global"
    _skill_file(project / ".opencode" / "skills", "swarm-worker-protocol")
    _skill_file(glob, "swarm-worker-protocol")
    surface = _opencode_surface(project, [glob])
    records = scan_skill_records(surface)
    assert [r.skill_id for r in records] == ["swarm-worker-protocol"] * 2
    assert len({r.path for r in records}) == 2
    # Ordre de la surface : projet d'abord, puis la racine lecture seule.
    assert [r.is_global for r in records] == [False, True]


def test_scan_skill_records_collapses_a_symlinked_duplicate(tmp_path):
    """A5 : deux chemins, un fichier ⇒ une seule fiche (clé = chemin canonique)."""
    from weekly_telemetry_aggregator.main import scan_skill_records

    project = tmp_path / "project"
    real = project / ".opencode" / "skills"
    _skill_file(real, "demo")
    alias = tmp_path / "alias-root"
    alias.mkdir()
    (alias / "demo").symlink_to(real / "demo")
    surface = _opencode_surface(project, [alias])
    assert len(scan_skill_records(surface)) == 1


def test_scan_skill_records_excludes_archive_at_any_depth(tmp_path):
    """A6 : `_archive/**` n'est plus chargeable ⇒ jamais catalogué."""
    from weekly_telemetry_aggregator.main import scan_skill_records

    project = tmp_path / "project"
    root = project / ".opencode" / "skills"
    _skill_file(root, "live-skill")
    _skill_file(root / "_archive", "archived-skill")
    _skill_file(root / "_archive" / "2026-09-12", "mcp-builder")
    _skill_file(root / "not_archive", "kept-skill")
    surface = _opencode_surface(project, [])
    # `not_archive` n'est PAS un segment `_archive` : il reste catalogué.
    assert [r.skill_id for r in scan_skill_records(surface)] == ["live-skill", "kept-skill"]


def test_scan_skill_records_reads_each_file_once(tmp_path):
    """A5 : un seul frontmatter par `SKILL.md`, les deux vues en découlent."""
    from weekly_telemetry_aggregator.main import scan_skill_records

    project = tmp_path / "project"
    _skill_file(
        project / ".opencode" / "skills",
        "demo",
        description="ma description",
        target_agents="opencode, claude-code",
    )
    (record,) = scan_skill_records(_opencode_surface(project, []))
    assert record.description == "ma description"
    assert record.target_agents == ("opencode", "claude-code")
    assert record.as_catalog_entry().name == "demo"
    assert record.as_catalog_entry().description == "ma description"


def test_scan_skill_records_tolerates_a_missing_root(tmp_path):
    from weekly_telemetry_aggregator.main import scan_skill_records

    surface = _opencode_surface(tmp_path / "absent", [tmp_path / "absent-global"])
    assert scan_skill_records(surface) == []


# ================================================ A6 : `never_loaded` restreint


def test_scan_skill_catalog_names_exclude_unreachable_skills(tmp_path):
    """A6 : `never_loaded` ne peut comparer que des skills atteignables."""
    from weekly_telemetry_aggregator.main import scan_skill_catalog, scan_skill_records

    project = tmp_path / "project"
    root = project / ".opencode" / "skills"
    _skill_file(root, "reachable-skill")
    _skill_file(root, "user-skill", origin="user")
    _skill_file(
        root,
        "weekly-safety-guardrails",
        description="Garde-fous partagés. Never load standalone.",
    )
    records = scan_skill_records(_opencode_surface(project, []))
    names, count, _entries = scan_skill_catalog(records)
    assert names == ["reachable-skill"]
    assert count == 1


def test_scan_skill_catalog_entries_still_expose_protected_skills(tmp_path):
    """Un skill `origin=user` reste auditable : absent des noms, présent en entries."""
    from weekly_telemetry_aggregator.main import scan_skill_catalog, scan_skill_records

    project = tmp_path / "project"
    root = project / ".opencode" / "skills"
    _skill_file(root, "reachable-skill")
    _skill_file(root, "user-skill", origin="user")
    records = scan_skill_records(_opencode_surface(project, []))
    names, _count, entries = scan_skill_catalog(records)
    assert names == ["reachable-skill"]
    assert sorted(entry.name for entry in entries) == ["reachable-skill", "user-skill"]
    protections = {r.skill_id: r.as_protection_entry()["metadata"]["origin"] for r in records}
    assert protections["user-skill"] == "user"


def test_archived_skill_cannot_be_listed_as_never_loaded(tmp_path):
    """A6 bout-en-bout : un skill archivé sort du nom ET des entries."""
    from weekly_telemetry_aggregator.main import scan_skill_catalog, scan_skill_records

    project = tmp_path / "project"
    root = project / ".opencode" / "skills"
    _skill_file(root, "live-skill")
    _skill_file(root / "_archive" / "2026-09-12", "mcp-builder")
    records = scan_skill_records(_opencode_surface(project, []))
    names, count, entries = scan_skill_catalog(records)
    assert [entry.name for entry in entries] == ["live-skill"]


# ================================ skill-curate : racines apply alignées sur A5


def _curate_cfg(project: Path, **draft_over) -> TelemetryConfig:
    """Config `skill-curate` avec un `draft_targets` explicite."""
    cfg = TelemetryConfig(project_root=project)
    return replace(cfg, draft_targets=replace(cfg.draft_targets, **draft_over))


def test_skill_dirs_for_default_target_is_the_opencode_root_only(tmp_path):
    """`_skill_dirs_for` suit la table A1 : sous le défaut opencode, `.claude/skills`
    et `.agents/skills` sortent de l'univers apply — le catalogue ne les lit pas."""
    from weekly_telemetry_aggregator.cli import _skill_dirs_for

    project = tmp_path / "project"
    roots = _skill_dirs_for(_curate_cfg(project))
    assert roots == [project.resolve() / ".opencode" / "skills"]


def test_skill_dirs_for_follows_the_resolved_target(tmp_path):
    """Cible résolue = surface projet : un override déplace l'univers apply."""
    from weekly_telemetry_aggregator.cli import _skill_dirs_for

    project = tmp_path / "project"
    cfg = _curate_cfg(project, mode="override", targets=["claude-code"])
    assert _skill_dirs_for(cfg) == [project.resolve() / ".claude" / "skills"]
    # Codex projette `.agents`, pas `.agents/skills` (constante codée en dur obsolète).
    codex = _curate_cfg(project, mode="override", targets=["codex"])
    assert _skill_dirs_for(codex) == [project.resolve() / ".agents"]


def test_skill_dirs_for_legacy_covers_every_known_harness_root(tmp_path):
    """Mode legacy : toutes les cibles connues, dans l'ordre de priorité A1."""
    from weekly_telemetry_aggregator.cli import _skill_dirs_for

    project = (tmp_path / "project").resolve()
    roots = _skill_dirs_for(_curate_cfg(tmp_path / "project", mode="legacy"))
    assert roots == [
        project / ".claude" / "skills",
        project / ".opencode" / "skills",
        project / ".github" / "prompts",
        project / ".github" / "skills",
        project / ".agents",
    ]


def test_skill_dirs_for_never_returns_a_global_root(tmp_path, monkeypatch):
    """Invariant A4 préservé : `--apply` ne peut pas muter une racine globale,
    même quand le project_root *est* le home ou un lien symbolique vers lui."""
    from weekly_telemetry_aggregator.cli import _skill_dirs_for

    home = tmp_path / "home"
    (home / ".claude" / "skills").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))

    # project_root == home : la racine projet résout DANS une racine globale.
    assert _skill_dirs_for(_curate_cfg(home, mode="override", targets=["claude-code"])) == []
    # Même chose via un lien symbolique depuis un project_root distinct.
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    (project / ".claude" / "skills").symlink_to(
        home / ".claude" / "skills", target_is_directory=True
    )
    cfg = _curate_cfg(project, mode="override", targets=["claude-code"])
    assert _skill_dirs_for(cfg) == []
    # Le catalogage opencode, lui, n'est pas concerné : `.opencode/skills` passe.
    assert _skill_dirs_for(_curate_cfg(home)) == [home.resolve() / ".opencode" / "skills"]


# ==================================================================== E1 : checks du doctor
#
# Le bloc doctor n'était couvert qu'À TRAVERS `doctor()` (et via `test_cli`) :
# chaque check intermédiaire n'était donc jamais asserté sur son verdict exact
# (liste `problems` / `warnings`, valeur de retour, texte imprimé). Ces tests
# figent ce contrat AVANT l'extraction du bloc hors de `main` : ce sont eux qui
# détectent une dérive de comportement pendant un déplacement de code.


def _char_cfg(tmp_path: Path) -> TelemetryConfig:
    """Config minimale et valide pour exercer un check isolé (jamais le run)."""
    cfg = TelemetryConfig()
    cfg.output_dir = tmp_path / "out"
    cfg.project_root = tmp_path
    return cfg


def _isolated_path(tmp_path: Path, monkeypatch, scripts: dict[str, str]) -> None:
    """PATH réduit à un seul dossier : seuls les binaires listés sont résolvables."""
    bin_dir = tmp_path / "isolated-bin"
    bin_dir.mkdir(exist_ok=True)
    for name, script in scripts.items():
        exe = bin_dir / name
        exe.write_text(script, encoding="utf-8")
        exe.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))


class _Meta:
    """Descripteur de session minimal — champs lus par `_audit_record`/`build_usage`."""

    def __init__(self, **over):
        self.session_id = "ses_1"
        self.title = "Titre"
        self.directory = None
        self.agent = "build"
        self.parent_id = None
        self.cost = 1.5
        self.time_updated = None
        self.__dict__.update(over)


class _Row(dict):
    pass


class _Query:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _Conn:
    """Double de `sqlite3.Connection` — une réponse (int ou Exception) par table."""

    def __init__(self, answers: dict[str, int | Exception]):
        self.answers = answers
        self.queries: list[str] = []

    def execute(self, sql: str):
        self.queries.append(sql)
        answer = self.answers[sql.split("FROM ")[-1].strip()]
        if isinstance(answer, Exception):
            raise answer
        return _Query(_Row({"n": answer}))


class _SqliteAdapter:
    def __init__(self, conn: _Conn):
        self.conn = conn


class _ProviderWithMigrations(FakeSessionProvider):
    """Provider exposant `db_path` + un adapter SQLite (chemin `_doctor_*`)."""

    def __init__(self, harness, conn):
        super().__init__(harness, [])
        self.db_path = "/stub/sessions.db"
        self._adapter = _SqliteAdapter(conn)


class _BrokenProvider(FakeSessionProvider):
    def check_schema(self) -> None:
        raise RuntimeError("boom")


# --------------------------------------------------------------- _version_tuple


def test_version_tuple_pads_and_truncates_to_three_numbers():
    assert _version_tuple("1.18.0") == (1, 18, 0)
    assert _version_tuple("1.18") == (1, 18, 0)  # complété par des zéros
    assert _version_tuple("7") == (7, 0, 0)
    assert _version_tuple("1.2.3.4") == (1, 2, 3)  # tronqué au 3e nombre
    assert _version_tuple("v2.3.4") == (2, 3, 4)  # préfixe non numérique ignoré


def test_version_tuple_without_digit_is_none():
    assert _version_tuple("unknown") is None
    assert _version_tuple("") is None


# --------------------------------------------------------------- _check_migrations


def test_check_migrations_reads_the_migration_counter():
    conn = _Conn({"migration": 42})
    assert _check_migrations(_SqliteAdapter(conn)) == 42
    assert conn.queries == ["SELECT count(*) AS n FROM migration"]


def test_check_migrations_falls_back_to_data_migration_table():
    conn = _Conn({"migration": RuntimeError("no such table"), "data_migration": 7})
    assert _check_migrations(_SqliteAdapter(conn)) == 7
    assert conn.queries == [
        "SELECT count(*) AS n FROM migration",
        "SELECT count(*) AS n FROM data_migration",
    ]


def test_check_migrations_absent_on_non_standard_schema_is_none():
    conn = _Conn({"migration": RuntimeError("x"), "data_migration": RuntimeError("y")})
    assert _check_migrations(_SqliteAdapter(conn)) is None


# --------------------------------------------------------------- _doctor_project_root


def test_placeholder_message_names_the_fields_and_the_file():
    assert _placeholder_message(["project_root", "output_dir"]) == (
        "config jamais adaptée à cette installation — substituer "
        "project_root/output_dir dans weekly-telemetry-config.json "
        "(placeholders /path/to/ détectés)"
    )


def test_doctor_project_root_none_names_the_missing_field(tmp_path: Path):
    cfg = _char_cfg(tmp_path)
    cfg.project_root = None
    problems: list[str] = []
    warnings: list[str] = []
    _doctor_project_root(cfg, tmp_path, config_loaded=True, problems=problems, warnings=warnings)
    assert problems == ["project_root manquant dans la config"]
    assert warnings == []


def test_doctor_project_root_without_opencode_dir_is_problem(tmp_path: Path):
    cfg = _char_cfg(tmp_path)  # project_root=tmp_path, aucun .opencode
    problems: list[str] = []
    warnings: list[str] = []
    _doctor_project_root(cfg, tmp_path, config_loaded=True, problems=problems, warnings=warnings)
    assert problems == [
        f"project_root {tmp_path} ne contient pas .opencode/ — "
        "adapter la config (clone : project_root = chemin absolu de votre repo)"
    ]
    assert warnings == []


def test_doctor_project_root_healthy_tree_is_silent(tmp_path: Path):
    _ = (tmp_path / ".opencode").mkdir()
    cfg = _char_cfg(tmp_path)
    problems: list[str] = []
    warnings: list[str] = []
    _doctor_project_root(cfg, tmp_path, config_loaded=True, problems=problems, warnings=warnings)
    assert problems == []
    assert warnings == []


def test_doctor_project_root_warns_only_when_the_config_is_nowhere(tmp_path: Path):
    _ = (tmp_path / ".opencode").mkdir()
    cfg = _char_cfg(tmp_path)
    problems: list[str] = []
    warnings: list[str] = []
    _doctor_project_root(cfg, tmp_path, config_loaded=False, problems=problems, warnings=warnings)
    assert problems == []
    assert warnings == [
        f"config introuvable au cwd ({tmp_path}) ni au project_root — vérifier --dir du cron"
    ]
    # config présente au cwd → plus aucun warning
    _ = (tmp_path / "weekly-telemetry-config.json").write_text("{}", encoding="utf-8")
    problems, warnings = [], []
    _doctor_project_root(cfg, tmp_path, config_loaded=False, problems=problems, warnings=warnings)
    assert warnings == []


def test_doctor_project_root_config_loaded_suppresses_the_cwd_hint(tmp_path: Path):
    _ = (tmp_path / ".opencode").mkdir()
    cfg = _char_cfg(tmp_path)
    problems: list[str] = []
    warnings: list[str] = []
    _doctor_project_root(
        cfg, tmp_path / "elsewhere", config_loaded=True, problems=problems, warnings=warnings
    )
    assert problems == []
    assert warnings == []


# --------------------------------------------------------------- _doctor_output_dir_guard


def test_doctor_output_dir_guard_flags_the_plugins_tree(tmp_path: Path):
    plugin_reports = tmp_path / "kit" / ".opencode" / "plugins" / "engine" / "reports"
    plugin_reports.mkdir(parents=True)
    cfg = _char_cfg(tmp_path)
    cfg.output_dir = plugin_reports
    warnings: list[str] = []
    _doctor_output_dir_guard(cfg, warnings)
    assert len(warnings) == 1
    assert warnings[0].startswith("output_dir résout sous l'arbre plugins")
    assert warnings[0].endswith(
        "run probablement lancé depuis le dossier du moteur ; déplacer reports/ "
        "hors du plugin et relancer les étapes depuis la racine du projet"
    )


def test_doctor_output_dir_guard_silent_outside_the_plugins_tree(tmp_path: Path):
    cfg = _char_cfg(tmp_path)
    cfg.output_dir = tmp_path / "reports"
    warnings: list[str] = []
    _doctor_output_dir_guard(cfg, warnings)
    assert warnings == []


def test_doctor_output_dir_guard_requires_plugins_right_after_opencode(tmp_path: Path):
    """`.opencode` seul, ou `plugins` plus loin dans le chemin : pas un plugin tree."""
    sibling = tmp_path / ".opencode" / "skills" / "plugins" / "reports"
    sibling.mkdir(parents=True)
    cfg = _char_cfg(tmp_path)
    cfg.output_dir = sibling
    warnings: list[str] = []
    _doctor_output_dir_guard(cfg, warnings)
    assert warnings == []


# --------------------------------------------------------------- _doctor_opencode_version


def test_doctor_opencode_version_passes_at_or_above_the_pin(tmp_path: Path, monkeypatch):
    _isolated_path(tmp_path, monkeypatch, {"opencode": "#!/bin/sh\necho '1.18.0'\n"})
    cfg = _char_cfg(tmp_path)
    problems: list[str] = []
    warnings: list[str] = []
    _doctor_opencode_version(cfg, "opencode", problems, warnings)
    assert problems == []
    assert warnings == []


def test_doctor_opencode_version_below_the_pin_is_problem(tmp_path: Path, monkeypatch):
    _isolated_path(tmp_path, monkeypatch, {"opencode": "#!/bin/sh\necho '1.0.0'\n"})
    cfg = _char_cfg(tmp_path)
    problems: list[str] = []
    warnings: list[str] = []
    _doctor_opencode_version(cfg, "opencode", problems, warnings)
    assert problems == ["opencode 1.0.0 < 1.18.0 — épinglage du schéma non garanti"]
    assert warnings == []


def test_doctor_opencode_version_unreachable_binary_is_warning_only(tmp_path: Path, monkeypatch):
    """v6.0.f : un PATH étroit (cron) ne bloque pas la revue — le pin devient une note."""
    _isolated_path(tmp_path, monkeypatch, {"opencode": "#!/bin/sh\necho '1.18.0'\n"})
    cfg = _char_cfg(tmp_path)
    problems: list[str] = []
    warnings: list[str] = []
    _doctor_opencode_version(cfg, "opencode_absent_zzz", problems, warnings)
    assert problems == []
    assert warnings == ["opencode introuvable ou non exécutable (opencode_absent_zzz)"]


# --------------------------------------------------------------- _doctor_session_providers


def test_doctor_session_providers_reports_source_and_migrations(
    tmp_path: Path, monkeypatch, capsys
):
    import weekly_telemetry_aggregator.main as main_mod

    source = _ProviderWithMigrations("opencode", _Conn({"migration": 42}))
    monkeypatch.setattr(main_mod, "build_providers", lambda _cfg: [source])
    problems: list[str] = []
    warnings: list[str] = []
    partial = _doctor_session_providers(_char_cfg(tmp_path), problems, warnings)
    assert partial is False  # 1 source sur 1 → pas de dégradation partielle
    assert problems == []
    assert warnings == []
    assert source.closed is True
    assert capsys.readouterr().out.splitlines() == [
        "doctor: [opencode] OK (/stub/sessions.db, migrations=42)"
    ]


def test_doctor_session_providers_returns_partial_on_mixed_sources(tmp_path: Path, monkeypatch):
    import weekly_telemetry_aggregator.main as main_mod

    ok_source = FakeSessionProvider("opencode", [])
    ko_source = _BrokenProvider("alpha", [])
    monkeypatch.setattr(main_mod, "build_providers", lambda _cfg: [ok_source, ko_source])
    problems: list[str] = []
    warnings: list[str] = []
    partial = _doctor_session_providers(_char_cfg(tmp_path), problems, warnings)
    assert partial is True
    assert problems == []
    assert warnings == ["[alpha] schéma illisible (boom)"]
    assert ok_source.closed is True
    assert ko_source.closed is True  # close() garanti même quand check_schema() lève


def test_doctor_session_providers_all_broken_is_problem_not_partial(tmp_path: Path, monkeypatch):
    import weekly_telemetry_aggregator.main as main_mod

    sources = [_BrokenProvider("alpha", []), _BrokenProvider("beta", [])]
    monkeypatch.setattr(main_mod, "build_providers", lambda _cfg: sources)
    problems: list[str] = []
    warnings: list[str] = []
    partial = _doctor_session_providers(_char_cfg(tmp_path), problems, warnings)
    assert partial is False  # 0 source utilisable → PROBLEM, jamais un partiel
    assert problems == [
        "aucune source de sessions disponible — vérifier session_sources / bases locales"
    ]
    assert warnings == ["[alpha] schéma illisible (boom)", "[beta] schéma illisible (boom)"]


def test_doctor_session_providers_no_source_configured_is_problem(tmp_path: Path, monkeypatch):
    import weekly_telemetry_aggregator.main as main_mod

    monkeypatch.setattr(main_mod, "build_providers", lambda _cfg: [])
    problems: list[str] = []
    warnings: list[str] = []
    partial = _doctor_session_providers(_char_cfg(tmp_path), problems, warnings)
    assert partial is False
    assert problems == [
        "aucune source de sessions disponible — vérifier session_sources / bases locales"
    ]
    assert warnings == []


def test_doctor_session_providers_weak_migration_counter_warns(tmp_path: Path, monkeypatch):
    import weekly_telemetry_aggregator.main as main_mod

    source = _ProviderWithMigrations("opencode", _Conn({"migration": 41}))
    monkeypatch.setattr(main_mod, "build_providers", lambda _cfg: [source])
    problems: list[str] = []
    warnings: list[str] = []
    assert _doctor_session_providers(_char_cfg(tmp_path), problems, warnings) is False
    assert problems == []
    assert warnings == [
        "[opencode] compteur de migrations faible (41) — vérifier la version du harnais"
    ]


def test_doctor_session_providers_non_standard_schema_warns(tmp_path: Path, monkeypatch):
    """Adapter SQLite sans les deux tables de migrations : warning, jamais une erreur."""
    import weekly_telemetry_aggregator.main as main_mod

    source = _ProviderWithMigrations(
        "opencode",
        _Conn({"migration": RuntimeError("x"), "data_migration": RuntimeError("y")}),
    )
    monkeypatch.setattr(main_mod, "build_providers", lambda _cfg: [source])
    problems: list[str] = []
    warnings: list[str] = []
    assert _doctor_session_providers(_char_cfg(tmp_path), problems, warnings) is False
    assert problems == []
    assert warnings == ["[opencode] compteur de migrations introuvable — schéma non standard"]


# --------------------------------------------------------------- _doctor_output_probe


def test_doctor_output_probe_creates_the_dir_and_removes_the_probe(tmp_path: Path, capsys):
    cfg = _char_cfg(tmp_path)
    problems: list[str] = []
    _doctor_output_probe(cfg, problems)
    assert problems == []
    assert cfg.output_dir.is_dir()
    assert not (cfg.output_dir / ".doctor-write-probe").exists()
    assert capsys.readouterr().out.splitlines() == [
        f"doctor: output_dir accessible en écriture: {cfg.output_dir}"
    ]


def test_doctor_output_probe_unwritable_output_dir_is_problem(tmp_path: Path):
    blocker = tmp_path / "blocker"
    _ = blocker.write_text("x", encoding="utf-8")
    cfg = _char_cfg(tmp_path)
    cfg.output_dir = blocker
    problems: list[str] = []
    _doctor_output_probe(cfg, problems)
    assert len(problems) == 1
    assert problems[0].startswith("output_dir non accessible en écriture:")


# --------------------------------------------------------------- _doctor_tool_presence


def test_doctor_tool_presence_warns_once_per_missing_tool(tmp_path: Path, monkeypatch):
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    warnings: list[str] = []
    _doctor_tool_presence(warnings)
    assert warnings == [
        "harness-eval absent du PATH (rien n'est lancé, mais l'étape correspondante sera dégradée)",
        "git absent du PATH (rien n'est lancé, mais l'étape correspondante sera dégradée)",
    ]


def test_doctor_tool_presence_silent_when_both_tools_present(tmp_path: Path, monkeypatch):
    _isolated_path(
        tmp_path, monkeypatch, {"harness-eval": "#!/bin/sh\nexit 0\n", "git": "#!/bin/sh\nexit 0\n"}
    )
    warnings: list[str] = []
    _doctor_tool_presence(warnings)
    assert warnings == []


# --------------------------------------------------------------- _doctor_harness_eval_version


def test_doctor_harness_eval_version_silent_when_absent_from_path(tmp_path: Path, monkeypatch):
    _isolated_path(tmp_path, monkeypatch, {})
    cfg = _char_cfg(tmp_path)
    warnings: list[str] = []
    _doctor_harness_eval_version(cfg, warnings)
    assert warnings == []


def test_doctor_harness_eval_version_silent_without_a_configured_minimum(
    tmp_path: Path, monkeypatch
):
    _isolated_path(tmp_path, monkeypatch, {"harness-eval": "#!/bin/sh\necho '1.0.0'\n"})
    cfg = _char_cfg(tmp_path)
    cfg.harness_eval_version = ""
    warnings: list[str] = []
    _doctor_harness_eval_version(cfg, warnings)
    assert warnings == []


def test_doctor_harness_eval_version_below_minimum_warns(tmp_path: Path, monkeypatch):
    _isolated_path(tmp_path, monkeypatch, {"harness-eval": "#!/bin/sh\necho '7.1.0'\n"})
    cfg = _char_cfg(tmp_path)
    warnings: list[str] = []
    _doctor_harness_eval_version(cfg, warnings)
    assert warnings == [
        "harness-eval 7.1.0 < minimum requis 7.9.0 — mettre à jour : "
        "uv tool install --upgrade harness-eval"
    ]


def test_doctor_harness_eval_version_unreadable_version_warns(tmp_path: Path, monkeypatch):
    _isolated_path(tmp_path, monkeypatch, {"harness-eval": "#!/bin/sh\necho 'nightly'\n"})
    cfg = _char_cfg(tmp_path)
    warnings: list[str] = []
    _doctor_harness_eval_version(cfg, warnings)
    assert warnings == ["harness-eval --version illisible ('nightly') — attendu ≥ 7.9.0"]


# --------------------------------------------------------------- _doctor_watch_repos


def test_doctor_watch_repos_silent_when_not_configured(tmp_path: Path):
    cfg = _char_cfg(tmp_path)
    assert cfg.watch_repos == []
    warnings: list[str] = []
    _doctor_watch_repos(cfg, warnings)
    assert warnings == []


def test_doctor_watch_repos_warns_when_gh_absent(tmp_path: Path, monkeypatch):
    _isolated_path(tmp_path, monkeypatch, {})
    cfg = _char_cfg(tmp_path)
    cfg.watch_repos = ["o/r"]
    warnings: list[str] = []
    _doctor_watch_repos(cfg, warnings)
    assert warnings == [
        "watch_repos configuré mais gh absent du PATH — repos privés/renommés non suivis"
    ]


def test_doctor_watch_repos_warns_when_gh_not_authenticated(tmp_path: Path, monkeypatch):
    _isolated_path(tmp_path, monkeypatch, {"gh": "#!/bin/sh\necho 'not logged in' >&2\nexit 1\n"})
    cfg = _char_cfg(tmp_path)
    cfg.watch_repos = ["o/r"]
    warnings: list[str] = []
    _doctor_watch_repos(cfg, warnings)
    assert warnings == [
        "watch_repos configuré mais gh non authentifié — repos privés indisponibles (gh auth login)"
    ]


# --------------------------------------------------------------- _doctor_draft_targets


def test_doctor_draft_targets_prints_target_and_remediation_surface(tmp_path: Path, capsys):
    _ = (tmp_path / ".opencode").mkdir()
    cfg = _char_cfg(tmp_path)
    warnings: list[str] = []
    _doctor_draft_targets(cfg, warnings)
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "doctor: cibles de drafting: opencode (défaut)"
    assert out[1].startswith("doctor: surface de remédiation 5.5: ")
    # aucun draft_targets explicite → le défaut est annoncé, la cible reste opencode
    assert warnings == [
        f"aucun draft_targets explicite en config ({cfg.project_root}) — "
        "défaut opencode appliqué pour la projection des drafts"
    ]


def test_doctor_draft_targets_names_the_missing_projection_surface(tmp_path: Path):
    """Marqueur absent : le warning nomme en plus la surface absente du projet."""
    empty_root = tmp_path / "vide"
    empty_root.mkdir()  # aucun marqueur, aucun draft_targets explicite
    cfg = _char_cfg(tmp_path)
    cfg.project_root = empty_root
    warnings: list[str] = []
    _doctor_draft_targets(cfg, warnings)
    assert warnings == [
        f"aucun draft_targets explicite en config ({empty_root}) — défaut opencode appliqué "
        "pour la projection des drafts ; aucun marqueur pour la cible résolue (opencode) : "
        ".opencode/ — surface de projection absente du projet"
    ]


# --------------------------------------------------------------- _unknown_session_source_types


def test_unknown_session_source_types_accepts_known_and_skips_disabled(tmp_path: Path, monkeypatch):
    from weekly_telemetry_aggregator.providers import registry

    monkeypatch.setattr(
        registry, "discover_provider_factories", lambda: {"opencode": object(), "codex": object()}
    )
    cfg = _char_cfg(tmp_path)
    cfg.session_sources = [{"type": "opencode"}, {"type": "inconnu", "enabled": False}]
    assert _unknown_session_source_types(cfg) == ([], ["codex", "opencode"])


def test_unknown_session_source_types_reports_every_unknown_shape(tmp_path: Path, monkeypatch):
    from weekly_telemetry_aggregator.providers import registry

    monkeypatch.setattr(
        registry, "discover_provider_factories", lambda: {"opencode": object(), "codex": object()}
    )
    cfg = _char_cfg(tmp_path)
    cfg.session_sources = [{"type": "zzz"}, {"type": 42}, "pas-un-dict"]
    unknown, supported = _unknown_session_source_types(cfg)
    assert supported == ["codex", "opencode"]
    # repr() de chaque forme : l'int non-str devient "42" (sans guillemets)
    assert unknown == ["'pas-un-dict'", "'zzz'", "42"]  # trié, dédupliqué


def test_unknown_session_source_types_fail_soft_on_an_illisible_registry(
    tmp_path: Path, monkeypatch
):
    from weekly_telemetry_aggregator.providers import registry

    def _boom():
        raise RuntimeError("registre cassé")

    monkeypatch.setattr(registry, "discover_provider_factories", _boom)
    assert _unknown_session_source_types(_char_cfg(tmp_path)) == ([], [])


# --------------------------------------------------------------- _copilot_doctor_details


def test_copilot_doctor_details_lists_home_schema_sessions_and_fts():
    provider = type(
        "P",
        (),
        {
            "harness": "copilot-cli",
            "home": "/home/u/.copilot",
            "schema_version": 4,
            "_sessions": {"a": 1, "b": 2},
            "_tables": {"search_index"},
        },
    )()
    assert _copilot_doctor_details(provider) == [
        "home=/home/u/.copilot",
        "schema_version=4",
        "sessions=2",
        "fts=oui",
    ]


def test_copilot_doctor_details_marks_absent_fields_with_question_marks():
    provider = type("P", (), {"harness": "copilot-cli"})()
    assert _copilot_doctor_details(provider) == ["home=?", "schema_version=?"]


def test_copilot_doctor_details_reports_a_missing_fts_index():
    provider = type("P", (), {"harness": "copilot-cli", "_tables": {"autre"}})()
    assert _copilot_doctor_details(provider) == ["home=?", "schema_version=?", "fts=non"]


def test_copilot_doctor_details_ignores_other_harnesses():
    provider = type("P", (), {"harness": "opencode", "home": "/x"})()
    assert _copilot_doctor_details(provider) == []


def test_copilot_doctor_details_never_raises():
    class _Boom:
        @property
        def harness(self):
            raise RuntimeError("boom")

    assert _copilot_doctor_details(_Boom()) == []


# --------------------------------------------------------------- doctor() : code de sortie


def test_doctor_problems_outrank_partial_sources(tmp_path: Path, monkeypatch, capsys):
    """Un PROBLEM (rc 2) l'emporte sur la dégradation partielle (rc 1) : l'ordre de
    décision de doctor() est figé ici, pas seulement « rc != 0 »."""
    import weekly_telemetry_aggregator.main as main_mod

    _isolated_path(tmp_path, monkeypatch, {"opencode": "#!/bin/sh\necho '1.0.0'\n"})
    _ = (tmp_path / ".opencode").mkdir()
    ok_source, ko_source = FakeSessionProvider("opencode", []), _BrokenProvider("alpha", [])
    monkeypatch.setattr(main_mod, "build_providers", lambda _cfg: [ok_source, ko_source])
    cfg = _char_cfg(tmp_path)

    assert doctor(cfg) == EXIT_TOTAL_FAILURE  # et non EXIT_PARTIAL

    out = capsys.readouterr().out.splitlines()
    problems = [line for line in out if line.startswith("doctor: PROBLEM: ")]
    warnings = [line for line in out if line.startswith("doctor: WARNING: ")]
    assert problems == [
        "doctor: PROBLEM: opencode 1.0.0 < 1.18.0 — épinglage du schéma non garanti"
    ]
    assert "doctor: WARNING: [alpha] schéma illisible (boom)" in warnings
    # tous les WARNING sont imprimés avant les PROBLEM
    assert out.index(warnings[0]) < out.index(problems[0])


def test_doctor_mixed_sources_without_problem_returns_partial(tmp_path: Path, monkeypatch):
    import weekly_telemetry_aggregator.main as main_mod

    _isolated_path(tmp_path, monkeypatch, {"opencode": "#!/bin/sh\necho '1.18.0'\n"})
    _ = (tmp_path / ".opencode").mkdir()
    ok_source, ko_source = FakeSessionProvider("opencode", []), _BrokenProvider("alpha", [])
    monkeypatch.setattr(main_mod, "build_providers", lambda _cfg: [ok_source, ko_source])
    assert doctor(_char_cfg(tmp_path)) == EXIT_PARTIAL


# ================================================================= E1 : build_usage
#
# Idem : `build_usage` et ses helpers n'étaient testés qu'à travers trois cas
# d'intégration. Les vérifications de fenêtre (bornes), d'agrégation, d'ordre
# d'émission et de forme des sorties vides sont figées ici.


class _ReadsAdapter:
    """Adapter de lecture déterministe — une clé par méthode du protocol utilisé."""

    harness = "opencode"

    def __init__(self, *, fail: str | None = None):
        self.fail = fail
        self.windows: list[tuple[str, int, int]] = []

    def _call(self, value):
        if self.fail is not None:
            raise RuntimeError(self.fail)
        return value

    def _window(self, session_id, start_ms, end_ms):
        self.windows.append((session_id, start_ms, end_ms))

    def has_telemetry_rows(self, session_id) -> bool:
        return self._call(True)

    def session_steps(self, session_id, start_ms, end_ms):
        self._window(session_id, start_ms, end_ms)
        return self._call(
            [StepFinish(session_id=session_id, timestamp=RUN_TIME, model="m", cost=0.5)]
        )

    def session_tools(self, session_id, start_ms, end_ms):
        return self._call(({"edit": 2}, {"edit": 40}, {"ma-skill": 1}))

    def session_tool_fingerprints(self, session_id, start_ms, end_ms):
        return self._call(({"edit": {"fp": 1}}, {"edit": {"fp2": 1}}))

    def session_user_turns(self, session_id, start_ms, end_ms):
        return self._call(["fais ça"])

    def session_context_chars(self, session_id, start_ms, end_ms):
        return self._call({"m": 120})

    def session_aggregates(self, session_id):
        return self._call({"cost": 0.5})

    def session_parts(self, session_id):
        return self._call([])


def _period() -> Period:
    return Period(start=RUN_TIME - timedelta(days=7), end=RUN_TIME)


def _step(model: str = "m", cost: float | None = 1.0, tokens: float = 0.0) -> StepFinish:
    return StepFinish(
        session_id="s",
        timestamp=RUN_TIME,
        model=model,
        tokens_input=tokens,
        cost=cost,
    )


# --------------------------------------------------------------- _truncate


def test_truncate_collapses_whitespace_and_drops_empties():
    assert _truncate("") is None
    assert _truncate("   \n\t ") is None
    assert _truncate("a  b\tc\nd") == "a b c d"


def test_truncate_boundary_is_exact_limit_characters():
    assert _truncate("abcdefghij", limit=10) == "abcdefghij"  # pile au seuil : inchangé
    assert _truncate("abcdefghijk", limit=10) == "abcdefghi…"  # seuil+1 : ellipsis comprise
    assert len(_truncate("x" * 500)) == 80  # jamais plus que la limite
    assert _truncate("x" * 500)[-1] == "…"


# --------------------------------------------------------------- _audit_record


def test_audit_record_traces_the_disposition():
    assert _audit_record(_Meta(), "included") == {
        "session_id": "ses_1",
        "title": "Titre",
        "agent": "build",
        "parent_id": None,
        "cost": 1.5,
        "updated": "",
        "status": "included",
    }


def test_audit_record_normalises_title_and_truncates_at_60():
    assert _audit_record(_Meta(title="  a|b\tc  "), "included")["title"] == "a¦b c"
    assert _audit_record(_Meta(title="z" * 80), "included")["title"] == "z" * 60 + "..."
    assert _audit_record(_Meta(title="   "), "included")["title"] is None


def test_audit_record_renders_the_update_timestamp_as_text():
    record = _audit_record(_Meta(time_updated=RUN_TIME), "included")
    assert record["updated"] == str(RUN_TIME)


def test_audit_record_keeps_worker_outcomes_only_when_present():
    plain = _audit_record(_Meta(), "included")
    assert not {"rc", "truncated", "worker_status"} & set(plain)
    rich = _audit_record(_Meta(rc=1, truncated=True, worker_status="done"), "error")
    assert (rich["rc"], rich["truncated"], rich["worker_status"]) == (1, True, "done")
    only_rc = _audit_record(_Meta(rc=0, truncated=None), "included")
    assert only_rc["rc"] == 0
    assert "truncated" not in only_rc


# --------------------------------------------------------------- _SessionReads


def test_session_reads_dataclass_exposes_the_raw_read_shape():
    import dataclasses

    assert [f.name for f in dataclasses.fields(_SessionReads)] == [
        "steps",
        "tool_calls",
        "tool_arg_chars",
        "skills",
        "tool_arg_fps",
        "tool_result_fps",
        "turns",
        "context_chars",
        "aggregates",
    ]
    reads = _SessionReads(
        steps=[],
        tool_calls={},
        tool_arg_chars={},
        skills={},
        tool_arg_fps={},
        tool_result_fps={},
        turns=[],
        context_chars={},
        aggregates=None,
    )
    assert reads.aggregates is None


# --------------------------------------------------------------- _usage_active_excluded


def test_usage_active_excluded_boundary_is_inclusive_at_the_cutoff(tmp_path: Path):
    # le seuil est un LITTERAL (10 min, v5.18) : la constante importée ne doit pas
    # pouvoir suivre une dérive du comportement — c'est le contrat qui est figé ici.
    assert ACTIVE_CUTOFF_MINUTES == 10
    cfg = _char_cfg(tmp_path)
    warnings: list[WarningEntry] = []
    audit: list[dict] = []
    meta = _Meta(time_updated=RUN_TIME - timedelta(minutes=10))
    assert _usage_active_excluded(meta, RUN_TIME, cfg, warnings, audit) is True
    assert [w.message for w in warnings] == [
        "session active exclue des totaux (télémétrie incomplète)"
    ]
    assert warnings[0].partial is False  # exclusion opérationnelle, pas un telemetry gap
    assert [r["status"] for r in audit] == ["active"]


def test_usage_active_excluded_just_outside_the_cutoff(tmp_path: Path):
    cfg = _char_cfg(tmp_path)
    warnings: list[WarningEntry] = []
    audit: list[dict] = []
    meta = _Meta(time_updated=RUN_TIME - timedelta(minutes=11))  # 1 min hors du cutoff
    assert _usage_active_excluded(meta, RUN_TIME, cfg, warnings, audit) is False
    assert warnings == []
    assert audit == []


def test_usage_active_excluded_disabled_by_config(tmp_path: Path):
    cfg = _char_cfg(tmp_path)
    cfg.exclude_active_sessions = False
    warnings: list[WarningEntry] = []
    audit: list[dict] = []
    assert (
        _usage_active_excluded(_Meta(time_updated=RUN_TIME), RUN_TIME, cfg, warnings, audit)
        is False
    )
    assert warnings == []
    assert audit == []


def test_usage_active_excluded_ignores_a_session_without_update_time(tmp_path: Path):
    cfg = _char_cfg(tmp_path)
    warnings: list[WarningEntry] = []
    audit: list[dict] = []
    assert _usage_active_excluded(_Meta(), RUN_TIME, cfg, warnings, audit) is False
    assert warnings == []
    assert audit == []


# --------------------------------------------------------------- _usage_advisor_excluded


def test_usage_advisor_excluded_is_silent_by_design(tmp_path: Path):
    cfg = _char_cfg(tmp_path)
    warnings: list[WarningEntry] = []
    audit: list[dict] = []
    assert _usage_advisor_excluded(_Meta(title=cfg.advisor_run_title), cfg, audit) is True
    assert [r["status"] for r in audit] == ["advisor"]
    assert warnings == []  # jamais de warning : l'auto-pollution est exclue par design


def test_usage_advisor_excluded_keeps_other_sessions(tmp_path: Path):
    cfg = _char_cfg(tmp_path)
    audit: list[dict] = []
    assert _usage_advisor_excluded(_Meta(title="Autre"), cfg, audit) is False
    assert audit == []
    # l'audit est optionnel : aucun crash quand il est désactivé
    assert _usage_advisor_excluded(_Meta(title=cfg.advisor_run_title), cfg, None) is True


# --------------------------------------------------------------- _fetch_session_reads


def test_fetch_session_reads_returns_the_raw_reads():
    warnings: list[WarningEntry] = []
    audit: list[dict] = []
    reads = _fetch_session_reads(_ReadsAdapter(), _Meta(), 1, 2, warnings, audit)
    assert isinstance(reads, _SessionReads)
    assert reads.tool_calls == {"edit": 2}
    assert reads.tool_arg_chars == {"edit": 40}
    assert reads.skills == {"ma-skill": 1}
    assert reads.tool_arg_fps == {"edit": {"fp": 1}}
    assert reads.tool_result_fps == {"edit": {"fp2": 1}}
    assert reads.turns == ["fais ça"]
    assert reads.context_chars == {"m": 120}
    assert reads.aggregates == {"cost": 0.5}
    assert warnings == []
    assert audit == []


def test_fetch_session_reads_failure_is_partial_and_audited():
    """Une lecture ratée ne tue jamais le run : warning partial + audit « error »."""
    warnings: list[WarningEntry] = []
    audit: list[dict] = []
    assert _fetch_session_reads(_ReadsAdapter(fail="boom"), _Meta(), 1, 2, warnings, audit) is None
    assert [w.message for w in warnings] == ["session read failed: boom"]
    assert warnings[0].partial is True  # telemetry gap → EXIT_PARTIAL
    assert [r["status"] for r in audit] == ["error"]


# --------------------------------------------------------------- _usage_no_steps_status


def test_usage_no_steps_status_is_silent_without_audit():
    warnings: list[WarningEntry] = []
    assert _usage_no_steps_status(_ReadsAdapter(), _Meta(), None, warnings) is None
    assert warnings == []


def test_usage_no_steps_status_reports_no_activity():
    warnings: list[WarningEntry] = []
    audit: list[dict] = []
    _usage_no_steps_status(_ReadsAdapter(), _Meta(), audit, warnings)
    assert [r["status"] for r in audit] == ["no-activity"]
    assert warnings == []


def test_usage_no_steps_status_reports_an_unflushed_session():
    class _NoRows(_ReadsAdapter):
        def has_telemetry_rows(self, session_id) -> bool:
            return False

    warnings: list[WarningEntry] = []
    audit: list[dict] = []
    _usage_no_steps_status(_NoRows(), _Meta(), audit, warnings)
    assert [r["status"] for r in audit] == ["unflushed"]
    assert [w.message for w in warnings] == [
        "session sans télémétrie persistée en DB (0 message/part — client actif ?)"
    ]


# --------------------------------------------------------------- _session_part_timestamps


def test_session_part_timestamps_splits_user_from_edit_write():
    base = RUN_TIME - timedelta(minutes=10)

    class _Parts(_ReadsAdapter):
        def session_parts(self, session_id):
            return [
                PartRecord(ts=base + timedelta(seconds=30), kind="tool", tool_name="Edit"),
                PartRecord(ts=base, kind="user"),
                PartRecord(ts=base + timedelta(seconds=10), kind="tool", tool_name="read"),
                PartRecord(ts=base + timedelta(seconds=20), kind="reasoning"),
            ]

    users, edits, parts = _session_part_timestamps(_Parts(), _Meta())
    assert users == [base]
    assert edits == [base + timedelta(seconds=30)]  # casse normalisée, ordre conservé
    assert len(parts) == 4


def test_session_part_timestamps_tolerates_a_provider_without_parts():
    class _NoParts(_ReadsAdapter):
        def session_parts(self, session_id):
            raise RuntimeError("provider sans parts")

    assert _session_part_timestamps(_NoParts(), _Meta()) == ([], [], [])


def test_session_part_timestamps_empty_input_shapes():
    assert _session_part_timestamps(_ReadsAdapter(), _Meta()) == ([], [], [])


# --------------------------------------------------------------- _usage_cost_warnings


def test_usage_cost_warnings_reports_missing_pricing_sorted_and_deduped(tmp_path: Path):
    cfg = _char_cfg(tmp_path)
    warnings: list[WarningEntry] = []
    steps = [_step("zeta", None), _step("alpha", None), _step("zeta", None)]
    _usage_cost_warnings(_Meta(), cfg, steps, None, [], None, warnings)
    assert [w.message for w in warnings] == ["missing-pricing:alpha", "missing-pricing:zeta"]
    assert all(w.partial is False for w in warnings)
    assert all(w.parts_cost is None for w in warnings)


def test_usage_cost_warnings_cross_check_parts_against_lifetime(tmp_path: Path):
    cfg = _char_cfg(tmp_path)
    warnings: list[WarningEntry] = []
    parts = [PartRecord(ts=RUN_TIME, kind="step-finish", cost=5.0)]
    _usage_cost_warnings(_Meta(), cfg, [_step(cost=1.0)], {"cost": 1.0}, parts, 1.0, warnings)
    assert [w.message for w in warnings] == [
        "cross-check mismatch: parts cost $5.0000 vs session_v2 $1.0000"
    ]
    assert (warnings[0].parts_cost, warnings[0].session_v2_cost) == (5.0, 1.0)


def test_usage_cost_warnings_detects_windowed_cost_above_lifetime(tmp_path: Path):
    cfg = _char_cfg(tmp_path)
    warnings: list[WarningEntry] = []
    parts = [PartRecord(ts=RUN_TIME, kind="step-finish", cost=10.0)]
    _usage_cost_warnings(_Meta(), cfg, [_step(cost=20.0)], {"cost": 10.0}, parts, 10.0, warnings)
    assert [w.message for w in warnings] == [
        "windowed cost $20.0000 > lifetime $10.0000 "
        "(enfants/compaction non couverts par session.cost)"
    ]
    assert (warnings[0].parts_cost, warnings[0].session_v2_cost) == (20.0, 10.0)


def test_usage_cost_warnings_cross_check_abs_is_a_floor(tmp_path: Path):
    """Tolérance = max(CROSS_CHECK_ABS, pct × coût) : le plancher absolu gagne quand le
    pourcentage sous-estimerait l'écart, et la comparaison reste stricte."""
    cfg = _char_cfg(tmp_path)
    cfg.cross_check_tolerance_pct = 0.0
    assert CROSS_CHECK_ABS == 0.01
    warnings: list[WarningEntry] = []
    parts = [PartRecord(ts=RUN_TIME, kind="step-finish", cost=0.02)]
    _usage_cost_warnings(_Meta(), cfg, [_step(cost=0.01)], {"cost": 0.01}, parts, 0.01, warnings)
    assert warnings == []  # écart == plancher : pas de warning (strict >)
    parts = [PartRecord(ts=RUN_TIME, kind="step-finish", cost=0.05)]
    _usage_cost_warnings(_Meta(), cfg, [_step(cost=0.01)], {"cost": 0.01}, parts, 0.01, warnings)
    assert [w.message for w in warnings] == [
        "cross-check mismatch: parts cost $0.0500 vs session_v2 $0.0100"
    ]


def test_usage_cost_warnings_silent_when_cross_checks_agree(tmp_path: Path):
    cfg = _char_cfg(tmp_path)
    warnings: list[WarningEntry] = []
    parts = [PartRecord(ts=RUN_TIME, kind="step-finish", cost=1.0)]
    _usage_cost_warnings(_Meta(), cfg, [_step(cost=1.0)], {"cost": 1.0}, parts, 1.0, warnings)
    assert warnings == []


def test_usage_cost_warnings_without_reported_cost_skips_the_cross_checks(tmp_path: Path):
    cfg = _char_cfg(tmp_path)
    warnings: list[WarningEntry] = []
    parts = [PartRecord(ts=RUN_TIME, kind="step-finish", cost=99.0)]
    _usage_cost_warnings(_Meta(), cfg, [_step("m", None)], {"cost": 99.0}, parts, None, warnings)
    assert [w.message for w in warnings] == ["missing-pricing:m"]


def test_usage_cost_warnings_cross_check_is_best_effort(tmp_path: Path):
    """Un agrégat illisible ne doit jamais transformer un cross-check en crash."""
    cfg = _char_cfg(tmp_path)
    warnings: list[WarningEntry] = []
    parts = [PartRecord(ts=RUN_TIME, kind="step-finish", cost=5.0)]
    _usage_cost_warnings(_Meta(), cfg, [_step(cost=1.0)], None, parts, 1.0, warnings)
    assert warnings == []


def test_usage_cost_warnings_empty_input_adds_nothing(tmp_path: Path):
    cfg = _char_cfg(tmp_path)
    warnings: list[WarningEntry] = []
    _usage_cost_warnings(_Meta(), cfg, [], None, [], None, warnings)
    assert warnings == []


# --------------------------------------------------------------- _harness_cost_rates


def test_harness_cost_rates_defaults_without_overrides(tmp_path: Path):
    assert HARNESS_COST_RATES_USD_PER_MTOK == {"opencode": 9.0}  # taux documenté
    cfg = _char_cfg(tmp_path)
    assert _harness_cost_rates(cfg) == dict(HARNESS_COST_RATES_USD_PER_MTOK)


def test_harness_cost_rates_overrides_per_source_type(tmp_path: Path):
    cfg = _char_cfg(tmp_path)
    cfg.session_sources = [{"type": "opencode"}, {"type": "codex", "cost_rate_usd_per_mtok": 2.5}]
    assert _harness_cost_rates(cfg) == {**HARNESS_COST_RATES_USD_PER_MTOK, "codex": 2.5}


def test_harness_cost_rates_keeps_the_default_on_an_unreadable_rate(tmp_path: Path):
    cfg = _char_cfg(tmp_path)
    cfg.session_sources = [
        {"type": "opencode", "cost_rate_usd_per_mtok": "pas-un-nombre"},
        {"type": "codex", "cost_rate_usd_per_mtok": None},
        "pas-un-dict",
    ]
    assert _harness_cost_rates(cfg) == dict(HARNESS_COST_RATES_USD_PER_MTOK)


# --------------------------------------------------------------- estimate_costs


def _usage_with(steps, session_id="ses_1", harness="") -> SessionUsage:
    return SessionUsage(session_id=session_id, steps=steps, harness=harness)


def test_estimate_costs_empty_input_is_an_empty_mapping():
    assert estimate_costs([]) == {}
    assert estimate_costs([], rates={"opencode": 9.0}) == {}


def test_estimate_costs_prices_only_priceless_sessions_with_tokens():
    usages = [
        _usage_with([_step("m", None, tokens=1_000_000.0)], "ses_a", "opencode"),
        _usage_with([_step("m", 1.0, tokens=1_000_000.0)], "ses_b", "opencode"),
        _usage_with([_step("m", None, tokens=0.0)], "ses_c", "opencode"),
        _usage_with([], "ses_d", "opencode"),
    ]
    assert estimate_costs(usages, rates={"opencode": 9.0}) == {"ses_a": 9.0}


def test_estimate_costs_falls_back_to_the_default_rate_for_an_unknown_harness():
    assert DEFAULT_HARNESS_COST_RATE_USD_PER_MTOK == 5.0  # taux documenté
    usages = [_usage_with([_step("m", None, tokens=1_000_000.0)], "ses_a", "inconnu")]
    assert estimate_costs(usages, rates={"opencode": 9.0}) == {
        "ses_a": DEFAULT_HARNESS_COST_RATE_USD_PER_MTOK
    }


def test_estimate_costs_honours_an_explicit_default_rate():
    usages = [_usage_with([_step("m", None, tokens=1_000_000.0)], "ses_a", "x")]
    assert estimate_costs(usages, rates={}, default_rate=3.0) == {"ses_a": 3.0}


def test_estimate_costs_derives_the_harness_from_the_canonical_session_id():
    usages = [_usage_with([_step("m", None, tokens=1_000_000.0)], "codex:ses_a", "")]
    # la clé du résultat reste l'id canonique complet ; seul le harnais est dérivé
    assert estimate_costs(usages, rates={"codex": 1.0}) == {"codex:ses_a": 1.0}


def test_estimate_costs_uses_the_documented_defaults_without_rates():
    assert estimate_costs(
        [_usage_with([_step("m", None, tokens=1_000_000.0)], "opencode:ses_a", "")]
    ) == {"opencode:ses_a": HARNESS_COST_RATES_USD_PER_MTOK["opencode"]}


# --------------------------------------------------------------- build_usage


def test_build_usage_windows_every_read_on_the_period(tmp_path: Path):
    period = _period()
    adapter = _ReadsAdapter()
    usage, failed = build_usage(
        _Meta(),
        adapter,
        period=period,
        run_time=RUN_TIME,
        cfg=_char_cfg(tmp_path),
        warnings=[],
    )
    assert failed is False
    assert usage is not None
    assert adapter.windows == [
        ("ses_1", int(period.start.timestamp() * 1000), int(period.end.timestamp() * 1000))
    ]
    assert adapter.windows[0][1] < adapter.windows[0][2]  # fenêtre non inversée


def test_build_usage_maps_every_read_into_the_usage(tmp_path: Path):
    audit: list[dict] = []
    usage, failed = build_usage(
        _Meta(title="Mon titre"),
        _ReadsAdapter(),
        period=_period(),
        run_time=RUN_TIME,
        cfg=_char_cfg(tmp_path),
        warnings=[],
        audit=audit,
    )
    assert failed is False
    assert usage is not None
    assert usage.session_id == "ses_1"
    assert usage.title == "Mon titre"
    assert usage.agent_type == "build"
    assert usage.tool_calls == {"edit": 2}
    assert usage.tool_arg_chars == {"edit": 40}
    assert usage.skills_loaded == {"ma-skill": 1}
    assert usage.tool_arg_fingerprints == {"edit": {"fp": 1}}
    assert usage.tool_result_fingerprints == {"edit": {"fp2": 1}}
    assert usage.user_turns == ["fais ça"]
    assert usage.context_chars == {"m": 120}
    assert usage.first_user_text == "fais ça"
    assert usage.reported_cost_usd_lifetime == 0.5
    assert usage.harness == "opencode"  # repris de l'adapter quand la meta n'en porte pas
    assert [r["status"] for r in audit] == ["included"]


def test_build_usage_first_user_text_is_truncated_at_80(tmp_path: Path):
    class _LongTurns(_ReadsAdapter):
        def session_user_turns(self, session_id, start_ms, end_ms):
            return ["   ", "b" * 200]

    usage, failed = build_usage(
        _Meta(),
        _LongTurns(),
        period=_period(),
        run_time=RUN_TIME,
        cfg=_char_cfg(tmp_path),
        warnings=[],
    )
    assert failed is False
    assert usage is not None
    assert usage.first_user_text == "b" * 79 + "…"  # le premier tour non vide gagne


def test_build_usage_active_session_is_excluded_before_any_read(tmp_path: Path):
    adapter = _ReadsAdapter()
    warnings: list[WarningEntry] = []
    usage, failed = build_usage(
        _Meta(time_updated=RUN_TIME - timedelta(minutes=1)),
        adapter,
        period=_period(),
        run_time=RUN_TIME,
        cfg=_char_cfg(tmp_path),
        warnings=warnings,
    )
    assert (usage, failed) == (None, False)  # exclusion, pas un telemetry gap
    assert adapter.windows == []
    assert [w.message for w in warnings] == [
        "session active exclue des totaux (télémétrie incomplète)"
    ]


def test_build_usage_advisor_session_is_excluded_silently(tmp_path: Path):
    cfg = _char_cfg(tmp_path)
    adapter = _ReadsAdapter()
    warnings: list[WarningEntry] = []
    audit: list[dict] = []
    usage, failed = build_usage(
        _Meta(title=cfg.advisor_run_title),
        adapter,
        period=_period(),
        run_time=RUN_TIME,
        cfg=cfg,
        warnings=warnings,
        audit=audit,
    )
    assert (usage, failed) == (None, False)
    assert adapter.windows == []
    assert warnings == []
    assert [r["status"] for r in audit] == ["advisor"]


def test_build_usage_read_failure_is_reported_as_failed_and_partial(tmp_path: Path):
    warnings: list[WarningEntry] = []
    audit: list[dict] = []
    usage, failed = build_usage(
        _Meta(),
        _ReadsAdapter(fail="boom"),
        period=_period(),
        run_time=RUN_TIME,
        cfg=_char_cfg(tmp_path),
        warnings=warnings,
        audit=audit,
    )
    assert (usage, failed) == (None, True)
    assert [w.partial for w in warnings] == [True]
    assert [r["status"] for r in audit] == ["error"]


def test_build_usage_empty_window_returns_none_not_failed(tmp_path: Path):
    class _NoSteps(_ReadsAdapter):
        def session_steps(self, session_id, start_ms, end_ms):
            return []

    audit: list[dict] = []
    usage, failed = build_usage(
        _Meta(),
        _NoSteps(),
        period=_period(),
        run_time=RUN_TIME,
        cfg=_char_cfg(tmp_path),
        warnings=[],
        audit=audit,
    )
    assert (usage, failed) == (None, False)
    assert [r["status"] for r in audit] == ["no-activity"]


def test_build_usage_without_audit_still_builds_the_usage(tmp_path: Path):
    usage, failed = build_usage(
        _Meta(),
        _ReadsAdapter(),
        period=_period(),
        run_time=RUN_TIME,
        cfg=_char_cfg(tmp_path),
        warnings=[],
    )
    assert failed is False
    assert usage is not None
    assert usage.session_id == "ses_1"


def test_build_usage_reports_no_lifetime_cost_without_aggregates(tmp_path: Path):
    class _NoAggregates(_ReadsAdapter):
        def session_aggregates(self, session_id):
            return None

    usage, failed = build_usage(
        _Meta(),
        _NoAggregates(),
        period=_period(),
        run_time=RUN_TIME,
        cfg=_char_cfg(tmp_path),
        warnings=[],
    )
    assert failed is False
    assert usage is not None
    assert usage.reported_cost_usd_lifetime is None


def test_build_usage_emits_missing_pricing_for_the_session(tmp_path: Path):
    class _Unpriced(_ReadsAdapter):
        def session_steps(self, session_id, start_ms, end_ms):
            return [_step("gpt-x", None)]

        def session_aggregates(self, session_id):
            return None  # pas de référence lifetime → aucun cross-check

    warnings: list[WarningEntry] = []
    usage, failed = build_usage(
        _Meta(),
        _Unpriced(),
        period=_period(),
        run_time=RUN_TIME,
        cfg=_char_cfg(tmp_path),
        warnings=warnings,
    )
    assert failed is False
    assert usage is not None
    assert [w.message for w in warnings] == ["missing-pricing:gpt-x"]
