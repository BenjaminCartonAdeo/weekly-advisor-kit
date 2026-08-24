"""X4 — toute réponse de tool produisant un artefact porte le chemin ABSOLU.

Incident post-run 07:41 : l'agent hebdo devinait les chemins d'artefacts
(réponses à chemins relatifs dépendant du cwd, confusion runs/<id> vs
runs/current, Glob racine `reports/**` à 0 match) → boucle ~20 min. Contrat :
la réponse de chaque étape productrice d'artefact contient le chemin absolu du
fichier écrit, même quand `output_dir` est relatif.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from helpers import make_step, make_usage, seed_v1_file, tzutc

from weekly_telemetry_aggregator.aggregator import aggregate
from weekly_telemetry_aggregator.cli import main as cli_main
from weekly_telemetry_aggregator.config import TelemetryConfig, load_config
from weekly_telemetry_aggregator.main import EXIT_OK, harness, run
from weekly_telemetry_aggregator.models import Period
from weekly_telemetry_aggregator.report import report_prep
from weekly_telemetry_aggregator.writer import summary_to_dict

RUN = tzutc(2026, 8, 12)
DATE = "2026-08-12"


class _FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _session() -> dict:
    ts = tzutc(2026, 8, 11, 10)
    return {
        "id": "ses_x4",
        "title": "X4 abs paths",
        "start": ts,
        "updated": ts,
        "steps": [{"ts": ts, "cost": 0.5}],
        "agg_cost": 0.5,
    }


def test_run_response_contains_absolute_summary_path(tmp_path, monkeypatch, capsys):
    """run() : la réponse porte le chemin absolu de la summary, output_dir relatif."""
    monkeypatch.chdir(tmp_path)  # cwd ≠ destination : un chemin relatif serait faux
    db = tmp_path / "opencode.db"
    seed_v1_file(db, [_session()])
    cfg = TelemetryConfig()
    cfg.output_dir = Path("reports")  # RELATIF — scénario incident
    cfg.opencode_db_path = str(db)
    rc = run(cfg, anchor=RUN.isoformat())
    out = capsys.readouterr().out
    assert rc == EXIT_OK
    match = re.search(r"file=(\S+)", out)
    assert match, out
    summary = Path(match.group(1))
    assert summary.is_absolute()
    assert summary.name == f"weekly-summary-{DATE}.json"
    assert summary.is_file()


def test_harness_response_contains_absolute_digest_path(tmp_path, monkeypatch, capsys):
    """harness : la réponse porte le chemin absolu du digest, output_dir relatif."""

    def fake_run(args, **kwargs):
        if "--version" in args:
            return _FakeProc(0, stdout="harness-eval 7.9.0")
        output = Path(args[args.index("--output") + 1])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps({"metadata": {"components_scanned": 0}, "inspection": {}}),
            encoding="utf-8",
        )
        return _FakeProc(0)

    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/harness-eval")
    monkeypatch.setattr("subprocess.run", fake_run)
    cfg = TelemetryConfig()
    cfg.output_dir = Path("reports")  # RELATIF — scénario incident
    cfg.project_root = tmp_path
    rc = harness(cfg, anchor=RUN.isoformat())
    out = capsys.readouterr().out
    assert rc == EXIT_OK
    match = re.search(r"harness: digest (\S+) \(rc=", out)
    assert match, out
    digest = Path(match.group(1))
    assert digest.is_absolute()
    assert digest.name == f"weekly-harness-digest-{DATE}.json"
    assert digest.is_file()


def test_report_prep_draft_is_absolute_from_relative_config(tmp_path, monkeypatch):
    """report-prep : config relative chargée → normalisée ; le draft rendu est absolu."""
    monkeypatch.chdir(tmp_path)
    conf = tmp_path / "weekly-telemetry-config.json"
    conf.write_text(json.dumps({"output_dir": "reports"}), encoding="utf-8")
    cfg = load_config(conf)
    # Normalisation à la source : TOUTE config chargée a un output_dir absolu.
    assert cfg.output_dir.is_absolute()
    assert cfg.output_dir == tmp_path / "reports"
    period = Period(start=tzutc(2026, 8, 5), end=RUN)
    u = make_usage("r", [make_step("r", tzutc(2026, 8, 6, 10), cost=0.5)], title="S")
    data = summary_to_dict(aggregate([u], period=period, generated_at=RUN))
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    (cfg.output_dir / f"weekly-summary-{DATE}.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8"
    )
    cfg.project_root = tmp_path
    cfg.open_browser = False
    draft, ctx = report_prep(cfg, anchor=RUN.isoformat())
    assert draft is not None and ctx is not None
    assert draft.is_absolute()
    assert draft.name == f"weekly-report-draft-{DATE}.md"
    assert draft.is_file()


def test_cli_audit_candidates_response_carries_absolute_file(tmp_path, monkeypatch, capsys):
    """Chaîne CLI complète (cli.py intact) : `audit-candidates` imprime file=<absolu>."""
    monkeypatch.chdir(tmp_path)
    db = tmp_path / "opencode.db"
    seed_v1_file(db, [_session()])
    conf = tmp_path / "weekly-telemetry-config.json"
    conf.write_text(
        json.dumps({"output_dir": "reports", "opencode_db_path": str(db)}),
        encoding="utf-8",
    )
    argv = ["--config", str(conf), "--anchor", RUN.isoformat()]
    assert cli_main([*argv, "run"]) == EXIT_OK
    capsys.readouterr()  # drain run
    assert cli_main([*argv, "audit-candidates"]) == EXIT_OK
    out = capsys.readouterr().out
    match = re.search(r"file=(\S+)", out)
    assert match, out
    candidates = Path(match.group(1))
    assert candidates.is_absolute()
    assert candidates.name == f"weekly-audit-candidates-{DATE}.json"
    assert candidates.is_file()
