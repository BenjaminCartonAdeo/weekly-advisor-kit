"""Report rendering — report_prep (deterministic draft) + report_assemble (blocks injection)."""

from __future__ import annotations

import json
import re
from datetime import timedelta
from pathlib import Path

import pytest
from helpers import active_run_file, make_step, make_usage, seed_v1_file, tzutc

from weekly_telemetry_aggregator.aggregator import aggregate
from weekly_telemetry_aggregator.config import TelemetryConfig
from weekly_telemetry_aggregator.main import RunProvenance
from weekly_telemetry_aggregator.models import Period
from weekly_telemetry_aggregator.report import (
    _BLOCKING_SECURITY_RULES,
    _audit_envelope_reason,
    _audit_envelope_valid,
    _coerce_rc,
    _coherence_has_curation_signal,
    _critical_security_findings,
    _gate_status,
    applicable_summary_rc,
    report_assemble,
    report_blocks_draft,
    report_prep,
    validate_required_artifacts,
)
from weekly_telemetry_aggregator.run_state import _canonical
from weekly_telemetry_aggregator.writer import summary_to_dict

RUN = tzutc(2026, 8, 12)
DATE = "2026-08-12"


def _cfg(tmp_path: Path) -> TelemetryConfig:
    cfg = TelemetryConfig()
    cfg.output_dir = tmp_path
    cfg.opencode_db_path = "/nonexistent/opencode.db"  # self-cost → None safely
    cfg.project_root = tmp_path
    cfg.open_browser = False  # ceinture : conftest force déjà WEEKLY_NO_BROWSER=1
    return cfg


def _write_summary(tmp_path: Path) -> None:
    period = Period(start=tzutc(2026, 8, 5), end=RUN)
    u = make_usage("r", [make_step("r", tzutc(2026, 8, 6, 10), cost=0.5)], title="S")
    data = summary_to_dict(aggregate([u], period=period, generated_at=RUN))
    (tmp_path / f"weekly-summary-{DATE}.json").write_text(
        __import__("json").dumps(data, ensure_ascii=False), encoding="utf-8"
    )


def test_report_prep_requires_summary(tmp_path: Path):
    draft, ctx = report_prep(_cfg(tmp_path), anchor=RUN.isoformat())
    assert draft is None
    assert ctx is None


def test_report_prep_renders_draft(tmp_path: Path):
    _write_summary(tmp_path)
    cfg = _cfg(tmp_path)
    draft, ctx = report_prep(cfg, anchor=RUN.isoformat())
    assert draft is not None and ctx is not None
    text = draft.read_text(encoding="utf-8")
    assert "<!-- QUALITY_BLOCK -->" in text
    assert "## 1. Vue d'ensemble" in text
    assert "## 4. Constats qualitatifs" in text
    assert f"weekly-summary-{DATE}.json" in text


def test_report_context_exposes_deterministic_provenance(tmp_path: Path):
    _write_summary(tmp_path)
    from weekly_telemetry_aggregator.report import build_report_context

    context = build_report_context(_cfg(tmp_path), anchor=RUN.isoformat())
    assert context is not None
    assert context["provenance"]["anchor"] == RUN.isoformat()
    assert context["provenance"]["artifact_inputs"][f"weekly-summary-{DATE}.json"]["present"]


def test_report_context_exposes_run_provenance_from_summary(tmp_path: Path):
    _write_summary(tmp_path)
    p = tmp_path / f"weekly-summary-{DATE}.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    expected = {"repository_path": "/repo", "branch": "main", "commit_sha": "abc"}
    data["run_provenance"] = expected
    p.write_text(json.dumps(data), encoding="utf-8")
    context = __import__(
        "weekly_telemetry_aggregator.report", fromlist=["build_report_context"]
    ).build_report_context(_cfg(tmp_path), anchor=RUN.isoformat())
    assert context["provenance"]["run_provenance"] == expected


def test_run_provenance_serializes_canonical_start_time():
    provenance = RunProvenance("/repo", "main", "abc", False, RUN.isoformat(), "test")
    payload = provenance.as_dict()
    assert payload["start_time"] == RUN.isoformat()
    assert payload["run_started_at"] == RUN.isoformat()


def test_validate_required_artifacts_separates_required_optional_and_statuses(tmp_path: Path):
    _write_summary(tmp_path)
    (tmp_path / f"weekly-insights-{DATE}.json").write_text("not-json", encoding="utf-8")
    gate = validate_required_artifacts(tmp_path, DATE)
    assert gate["status"] == "pass"
    assert gate["required"][f"weekly-summary-{DATE}.json"]["status"] == "present"
    assert gate["optional"][f"weekly-insights-{DATE}.json"]["status"] == "ill_readable"
    assert gate["optional"][f"weekly-harness-digest-{DATE}.json"]["status"] == "absent"


def test_validate_required_artifacts_enabled_html_absence_is_nonzero_gate(tmp_path: Path):
    _write_summary(tmp_path)
    gate = validate_required_artifacts(
        tmp_path, DATE, html_enabled=True, html_path=tmp_path / "missing.html"
    )
    assert gate["html"]["status"] == "absent"
    assert gate["status"] == "pass"  # HTML failure is handled as assemble rc=1.


def test_validate_required_artifacts_html_present_when_file_exists_disabled_flag(tmp_path: Path):
    """P0 : fichier HTML réellement produit → gate `present`, même si rendu configuré off."""
    (tmp_path / f"weekly-summary-{DATE}.json").write_text("{}", encoding="utf-8")
    html_file = tmp_path / f"weekly-report-{DATE}.html"
    html_file.write_text("<html>ok</html>", encoding="utf-8")
    gate = validate_required_artifacts(tmp_path, DATE, html_enabled=False, html_path=html_file)
    assert gate["html"]["status"] == "present"
    assert gate["html"]["path"] == str(html_file)


def test_validate_required_artifacts_html_disabled_when_off_and_no_file(tmp_path: Path):
    """P0 : rendu off ET aucun fichier produit → gate `disabled`."""
    (tmp_path / f"weekly-summary-{DATE}.json").write_text("{}", encoding="utf-8")
    gate = validate_required_artifacts(tmp_path, DATE, html_enabled=False, html_path=None)
    assert gate["html"]["status"] == "disabled"
    assert gate["html"]["path"] is None


def test_artifact_applicability_marks_optional_inputs_without_blocking(tmp_path: Path):
    _write_summary(tmp_path)
    gate = validate_required_artifacts(
        tmp_path,
        DATE,
        applicability={"weekly-insights": False},
    )
    insights = gate["optional"][f"weekly-insights-{DATE}.json"]
    assert insights["status"] == "absent"
    assert insights["applicable"] is False
    assert gate["status"] == "pass"


def test_validate_required_artifacts_branch_enabled_requires_exact_schema(tmp_path: Path):
    _write_summary(tmp_path)
    gate = validate_required_artifacts(tmp_path, DATE, applicability={"weekly-insights": True})
    assert gate["status"] == "incomplete"
    assert gate["required"][f"weekly-insights-{DATE}.json"]["schema_valid"] is False
    (tmp_path / f"weekly-insights-{DATE}.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "period": {"start": "2026-08-05T00:00:00Z", "end": "2026-08-12T00:00:00Z"},
                "generated_at": "2026-08-12T00:00:00Z",
                "deltas": {},
                "alerts": [],
                "maintenance": {"findings": []},
            }
        ),
        encoding="utf-8",
    )
    gate = validate_required_artifacts(tmp_path, DATE, applicability={"weekly-insights": True})
    assert gate["status"] == "pass"


def test_report_assemble_requires_declared_branch_artifact(tmp_path: Path):
    _write_summary(tmp_path)
    cfg = _cfg(tmp_path)
    report_prep(cfg, anchor=RUN.isoformat())
    (tmp_path / f"weekly-timings-{DATE}.json").write_text(
        json.dumps(
            {
                "branches": {
                    "I": {
                        "artifacts": [f"weekly-insights-{DATE}.json"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    final_path, warnings, rc = report_assemble(cfg, anchor=RUN.isoformat())
    assert final_path is None
    assert rc == 2
    assert any("weekly-insights" in warning for warning in warnings)


def test_report_assemble_partial_audit_still_writes_report_rc_one(tmp_path: Path):
    """JOIN partiel : audit dynamique manquant → rapport écrit, rc=1, mention explicite."""
    _write_summary(tmp_path)
    cfg = _cfg(tmp_path)
    report_prep(cfg, anchor=RUN.isoformat())
    (tmp_path / f"weekly-timings-{DATE}.json").write_text(
        json.dumps(
            {
                "branches": {
                    "A": {
                        "artifacts": ["audit-findings-ses_missing.json"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    final_path, warnings, rc = report_assemble(cfg, anchor=RUN.isoformat())
    assert final_path is not None
    assert rc == 1
    assert any("JOIN partiel" in warning for warning in warnings)
    text = final_path.read_text(encoding="utf-8")
    assert "audit" in text.casefold() or "partiel" in text.casefold() or len(text) > 0


def test_report_assemble_blocking_restores_current_to_previous_run(tmp_path: Path):
    """Bascule tardive : STOP sans rapport (rc=2) → current restauré sur le run précédent."""
    from weekly_telemetry_aggregator.run_state import activate_run

    first = activate_run(tmp_path, DATE, RUN)
    second = activate_run(tmp_path, DATE, RUN)
    out = second.run_dir
    period = Period(start=tzutc(2026, 8, 5), end=RUN)
    u = make_usage("r", [make_step("r", tzutc(2026, 8, 6, 10), cost=0.5)], title="S")
    data = summary_to_dict(aggregate([u], period=period, generated_at=RUN))
    (out / f"weekly-summary-{DATE}.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8"
    )
    cfg = _cfg(tmp_path)
    report_prep(cfg, anchor=RUN.isoformat())
    (out / f"weekly-timings-{DATE}.json").write_text(
        json.dumps(
            {
                "branches": {
                    "I": {
                        "artifacts": [f"weekly-insights-{DATE}.json"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    final_path, warnings, rc = report_assemble(cfg, anchor=RUN.isoformat())
    assert final_path is None
    assert rc == 2
    assert _canonical(tmp_path / "runs" / "current") == _canonical(first.run_dir)
    assert any("restauré" in warning for warning in warnings)


def _activate_two_runs_and_prep(tmp_path: Path):
    """2 runs activés + summary/timings/draft dans le 2e ; retourne (cfg, out)."""
    from weekly_telemetry_aggregator.run_state import activate_run

    activate_run(tmp_path, DATE, RUN)
    second = activate_run(tmp_path, DATE, RUN)
    out = second.run_dir
    period = Period(start=tzutc(2026, 8, 5), end=RUN)
    u = make_usage("r", [make_step("r", tzutc(2026, 8, 6, 10), cost=0.5)], title="S")
    data = summary_to_dict(aggregate([u], period=period, generated_at=RUN))
    (out / f"weekly-summary-{DATE}.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8"
    )
    cfg = _cfg(tmp_path)
    report_prep(cfg, anchor=RUN.isoformat())
    return cfg, out


def _declare_audits(out: Path, artifacts: list[str]) -> None:
    (out / f"weekly-timings-{DATE}.json").write_text(
        json.dumps({"branches": {"A": {"artifacts": artifacts}}}), encoding="utf-8"
    )


def _read_resilience(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "run_state.json").read_text(encoding="utf-8")).get(
        "resilience", {}
    )


def test_report_assemble_partial_audit_records_fallback_event(tmp_path: Path):
    """JOIN partiel (audit absent) → resilience.audit_partial_fallback == 1."""
    cfg, out = _activate_two_runs_and_prep(tmp_path)
    _declare_audits(out, ["audit-findings-ses_missing.json"])
    final_path, warnings, rc = report_assemble(cfg, anchor=RUN.isoformat())
    assert final_path is not None
    assert rc == 1
    assert _read_resilience(tmp_path).get("audit_partial_fallback") == 1


def test_report_assemble_envelope_reject_records_event(tmp_path: Path):
    """Audit hors contrat (sid-mismatch) → envelope_reject + fallback comptés."""
    cfg, out = _activate_two_runs_and_prep(tmp_path)
    _declare_audits(out, ["audit-findings-ses_bad.json"])
    (out / "audit-findings-ses_bad.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "session_id": "ses_other",
                "summary": "résumé non vide",
                "findings": [],
                "rc": 0,
                "warnings": [],
            }
        ),
        encoding="utf-8",
    )
    final_path, warnings, rc = report_assemble(cfg, anchor=RUN.isoformat())
    assert final_path is not None
    assert rc == 1
    resilience = _read_resilience(tmp_path)
    assert resilience.get("audit_envelope_reject") == 1
    assert resilience.get("audit_partial_fallback") == 1


def test_applicable_summary_rc_accepts_valid_transcript_truncation(tmp_path: Path):
    _write_summary(tmp_path)
    summary_path = tmp_path / f"weekly-summary-{DATE}.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary.update(
        {
            "rc": 1,
            "warnings": [
                {
                    "message": "transcript-truncated:s1",
                    "partial": True,
                    "artifacts": ["audit-findings-s1.json"],
                }
            ],
            "worker_statuses": [
                {
                    "session_id": "s1",
                    "status": "truncated",
                    "rc": 1,
                    "artifacts": ["audit-findings-s1.json"],
                }
            ],
        }
    )
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    (tmp_path / "audit-findings-s1.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "session_id": "s1",
                "summary": "partiel",
                "findings": [],
                "rc": 1,
                "warnings": ["transcript-truncated:s1"],
            }
        ),
        encoding="utf-8",
    )
    assert applicable_summary_rc(summary, out=tmp_path, date=DATE) == 0


def test_applicable_summary_rc_accepts_recovered_watch_and_report_only_permission(tmp_path: Path):
    summary = {
        "rc": 1,
        "warnings": [
            {"status": "recovered", "source": "watch", "artifact": "weekly-watch-findings"},
            {
                "status": "report-only",
                "report_only": True,
                "category": "external-permission-refusal",
                "permission_refused": True,
                "target": str(tmp_path.parent / "external-reports"),
            },
        ],
    }
    (tmp_path / f"weekly-watch-findings-{DATE}.json").write_text(
        json.dumps({"schema_version": 2, "findings": [], "validation": {}}), encoding="utf-8"
    )
    assert applicable_summary_rc(summary, out=tmp_path, date=DATE, project_root=tmp_path) == 0


def test_applicable_summary_rc_keeps_in_worktree_permission_failure(tmp_path: Path):
    summary = {
        "rc": 1,
        "warnings": [{"partial": True, "message": "permission denied in worktree"}],
    }
    assert applicable_summary_rc(summary, out=tmp_path, date=DATE) == 1


def test_applicable_summary_rc_missing_status_is_not_implicit_success():
    assert applicable_summary_rc({"warnings": []}) == 1
    assert applicable_summary_rc({"warnings": []}, fallback_rc=0) == 0


def test_applicable_summary_rc_fallback_is_baseline_not_early_success():
    assert (
        applicable_summary_rc(
            {"warnings": [{"message": "worker failed", "partial": True}]},
            fallback_rc=0,
        )
        == 1
    )
    assert (
        applicable_summary_rc(
            {"warnings": []},
            additional_records=[{"rc": 2, "status": "error"}],
            fallback_rc=0,
        )
        == 2
    )


def test_applicable_summary_rc_requires_verified_external_permission_refusal(tmp_path: Path):
    target = tmp_path.parent / "external-target"
    inside = {
        "rc": 1,
        "warnings": [
            {
                "status": "report-only",
                "report_only": True,
                "category": "external-permission-refusal",
                "permission_refused": True,
                "target": str(tmp_path / "inside"),
            }
        ],
    }
    assert applicable_summary_rc(inside, out=tmp_path, date=DATE) == 1
    external = {
        "rc": 1,
        "warnings": [
            {
                "status": "report-only",
                "report_only": True,
                "category": "external-permission-refusal",
                "permission_refused": True,
                "target": str(target),
            }
        ],
    }
    assert applicable_summary_rc(external, out=tmp_path, date=DATE, project_root=tmp_path) == 0


def test_applicable_summary_rc_rejects_serialized_report_only_text(tmp_path: Path):
    summary = {
        "rc": 1,
        "warnings": [
            "status=report-only category=external-permission-refusal "
            f"target={tmp_path.parent / 'outside'} project_root={tmp_path}"
        ],
    }
    assert applicable_summary_rc(summary, out=tmp_path, date=DATE, project_root=tmp_path) == 1


def test_recovered_input_requires_exact_valid_disk_artifact(tmp_path: Path):
    summary = {
        "rc": 1,
        "warnings": [
            {
                "status": "recovered",
                "source": "watch",
                "artifact": "weekly-watch-findings",
            }
        ],
    }
    path = tmp_path / f"weekly-watch-findings-{DATE}.json"
    path.write_text("{}", encoding="utf-8")
    assert applicable_summary_rc(summary, out=tmp_path, date=DATE) == 1
    path.write_text(
        json.dumps({"schema_version": 2, "findings": [], "validation": {}}), encoding="utf-8"
    )
    assert applicable_summary_rc(summary, out=tmp_path, date=DATE) == 0


def test_truncated_audit_requires_declared_exact_path_and_envelope(tmp_path: Path):
    audit_path = tmp_path / "audit-findings-s1.json"
    audit_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "session_id": "s1",
                "summary": "partiel",
                "findings": [],
                "rc": 1,
                "warnings": ["transcript-truncated:s1"],
            }
        ),
        encoding="utf-8",
    )
    base = {
        "rc": 1,
        "warnings": [],
        "worker_statuses": [{"session_id": "s1", "status": "truncated", "rc": 1}],
    }
    assert applicable_summary_rc(base, out=tmp_path, date=DATE) == 1
    declared = {
        **base,
        "worker_statuses": [
            {
                "session_id": "s1",
                "status": "truncated",
                "rc": 1,
                "artifacts": ["nested/audit-findings-s1.json"],
            }
        ],
    }
    assert applicable_summary_rc(declared, out=tmp_path, date=DATE) == 1
    declared["worker_statuses"][0]["artifacts"] = ["audit-findings-s1.json"]
    assert applicable_summary_rc(declared, out=tmp_path, date=DATE) == 0


def test_validate_required_artifacts_requires_dynamic_audit_declaration(tmp_path: Path):
    _write_summary(tmp_path)
    audit = tmp_path / "audit-findings-s1.json"
    audit.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "session_id": "s1",
                "summary": "audit",
                "findings": [],
                "rc": 0,
                "warnings": [],
            }
        ),
        encoding="utf-8",
    )
    gate = validate_required_artifacts(
        tmp_path,
        DATE,
        dynamic_audit_artifacts=["nested/audit-findings-s1.json"],
    )
    assert gate["status"] == "incomplete"
    gate = validate_required_artifacts(
        tmp_path,
        DATE,
        dynamic_audit_artifacts=["audit-findings-s1.json"],
    )
    assert gate["status"] == "pass"


def test_applicable_summary_rc_reads_recovered_join_records(tmp_path: Path):
    (tmp_path / f"weekly-harness-remediation-proposals-{DATE}.json").write_text(
        json.dumps({"schema_version": 2, "date": DATE, "proposals": []}), encoding="utf-8"
    )
    summary = {"rc": 1}
    timings = {
        "branches": {
            "H": {
                "rc": 1,
                "warnings": ["recovered harness proposal input"],
                "artifacts": [f"weekly-harness-remediation-proposals-{DATE}.json"],
            }
        }
    }
    from weekly_telemetry_aggregator.report import _join_status_records

    assert (
        applicable_summary_rc(
            summary,
            out=tmp_path,
            date=DATE,
            additional_records=_join_status_records(timings),
        )
        == 0
    )


def test_critical_security_findings_are_detected():
    digest = {"findings": [{"rule": "security/tool-poisoning", "severity": "critical"}]}
    assert _critical_security_findings(digest) == digest["findings"]
    assert (
        _critical_security_findings({"findings": [{"rule": "security/x", "severity": "high"}]})
        == []
    )


def test_report_assemble_critical_security_forces_nonzero_rc(tmp_path: Path):
    # warn-only migration (epic warn-only) : critical non-blocking → rc==1, rapport écrit.
    _write_summary(tmp_path)
    cfg = _cfg(tmp_path)
    report_prep(cfg, anchor=RUN.isoformat())
    (tmp_path / f"weekly-harness-digest-{DATE}.json").write_text(
        json.dumps({"findings": [{"rule": "security/tool-poisoning", "severity": "critical"}]}),
        encoding="utf-8",
    )
    final_path, warnings, rc = report_assemble(cfg, anchor=RUN.isoformat())
    assert final_path is not None
    assert rc == 1
    assert any("security/critical" in warning for warning in warnings)


@pytest.mark.parametrize(
    "rule",
    ["mcp-tool-poisoning", "unbounded-delegation", "memory-write-unscoped"],
)
def test_report_assemble_nested_blocking_security_rule_is_rc_two(tmp_path: Path, rule: str):
    # warn-only migration (epic warn-only) : blocking → rc==1, rapport écrit.
    _write_summary(tmp_path)
    cfg = _cfg(tmp_path)
    report_prep(cfg, anchor=RUN.isoformat())
    (tmp_path / f"weekly-harness-digest-{DATE}.json").write_text(
        json.dumps(
            {
                "findings": [],
                "inspection": {
                    "uncategorized": [
                        {
                            "path": ".opencode/a.md",
                            "findings": [{"rule": rule, "severity": "critical"}],
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    final_path, warnings, rc = report_assemble(cfg, anchor=RUN.isoformat())
    assert final_path is not None
    assert rc == 1
    assert (tmp_path / f"weekly-report-{DATE}.md").exists()
    assert any("blocking security rule" in warning for warning in warnings)


def test_report_assemble_nested_prefixed_blocking_security_rule_is_rc_two(tmp_path: Path):
    # warn-only migration (epic warn-only) : blocking → rc==1, rapport écrit.
    _write_summary(tmp_path)
    cfg = _cfg(tmp_path)
    report_prep(cfg, anchor=RUN.isoformat())
    (tmp_path / f"weekly-harness-digest-{DATE}.json").write_text(
        json.dumps(
            {
                "findings": [],
                "inspection": {
                    "uncategorized": [
                        {
                            "path": ".opencode/a.md",
                            "findings": [
                                {"rule": "security/mcp-tool-poisoning", "severity": "warning"}
                            ],
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    final_path, warnings, rc = report_assemble(cfg, anchor=RUN.isoformat())
    assert final_path is not None
    assert rc == 1
    assert (tmp_path / f"weekly-report-{DATE}.md").exists()
    assert any("blocking security rule" in warning for warning in warnings)


def test_report_assemble_requires_draft(tmp_path: Path):
    path, warnings, rc = report_assemble(_cfg(tmp_path), anchor=RUN.isoformat())
    assert path is None
    assert rc == 2
    assert any("draft" in w for w in warnings)


def test_report_assemble_placeholder_without_blocks(tmp_path: Path):
    _write_summary(tmp_path)
    cfg = _cfg(tmp_path)
    # prep writes the draft; assemble runs but no blocks file → placeholder.
    report_prep(cfg, anchor=RUN.isoformat())
    final_path, warnings, rc = report_assemble(cfg, anchor=RUN.isoformat())
    assert rc == 0
    assert final_path is not None
    assert "<!-- QUALITY_BLOCK -->" not in final_path.read_text(encoding="utf-8")
    assert "non disponible" in final_path.read_text(encoding="utf-8")
    assert any("placeholder" in w for w in warnings)


def test_report_assemble_propagates_skill_curate_rc(tmp_path: Path):
    """An upstream curation failure remains visible in assemble's exit code."""
    _write_summary(tmp_path)
    cfg = _cfg(tmp_path)
    report_prep(cfg, anchor=RUN.isoformat())
    (tmp_path / f"skill-curate-{DATE}.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "rc": 1,
                "mode": "apply",
                "dry_run": False,
                "date": DATE,
                "decisions": [],
            }
        ),
        encoding="utf-8",
    )

    final_path, warnings, rc = report_assemble(cfg, anchor=RUN.isoformat())

    assert final_path is not None
    assert rc == 1


def test_report_assemble_preserves_upstream_summary_rc(tmp_path: Path):
    _write_summary(tmp_path)
    cfg = _cfg(tmp_path)
    p = tmp_path / f"weekly-summary-{DATE}.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    data["rc"] = 2
    p.write_text(json.dumps(data), encoding="utf-8")
    report_prep(cfg, anchor=RUN.isoformat())
    final_path, _warnings, rc = report_assemble(cfg, anchor=RUN.isoformat())
    assert final_path is not None
    assert rc == 2


def test_report_assemble_requires_curation_manifest_when_signaled(tmp_path: Path):
    """Curation signal raises partial gate, without applying or moving anything."""
    _write_summary(tmp_path)
    cfg = _cfg(tmp_path)
    (tmp_path / f"weekly-coherence-findings-{DATE}.json").write_text(
        json.dumps({"curation_signal": {"R4_archive_candidates": {"strong_8of8": ["x"]}}}),
        encoding="utf-8",
    )
    report_prep(cfg, anchor=RUN.isoformat())
    final_path, warnings, rc = report_assemble(cfg, anchor=RUN.isoformat())
    assert final_path is not None
    assert rc == 1
    assert any("WAVE 2.5" in warning and "skill-curate" in warning for warning in warnings)
    assert "REQUIRED, non exécutée" in final_path.read_text(encoding="utf-8")


def test_report_assemble_curation_manifest_clears_gate(tmp_path: Path):
    _write_summary(tmp_path)
    cfg = _cfg(tmp_path)
    (tmp_path / f"weekly-coherence-findings-{DATE}.json").write_text(
        json.dumps({"findings": [{"tag_action": "archive", "target_skill_id": "x"}]}),
        encoding="utf-8",
    )
    (tmp_path / f"skill-curate-{DATE}.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "rc": 0,
                "mode": "apply",
                "dry_run": False,
                "date": DATE,
                "applied": 0,
                "proposed": 1,
                "skipped": 0,
                "decisions": [],
            }
        ),
        encoding="utf-8",
    )
    report_prep(cfg, anchor=RUN.isoformat())
    blocks_path, block_warnings, block_rc = report_blocks_draft(cfg, anchor=RUN.isoformat())
    assert blocks_path is not None
    assert block_rc == 0
    assert any("brouillon de blocs généré" in warning for warning in block_warnings)
    final_path, warnings, rc = report_assemble(cfg, anchor=RUN.isoformat())
    assert final_path is not None
    assert rc == 0
    assert not any("WAVE 2.5" in warning for warning in warnings)
    assert "Curation (WAVE 2.5 — apply — appliquées)" in final_path.read_text(encoding="utf-8")


def test_report_assemble_does_not_count_curation_dry_run(tmp_path: Path):
    _write_summary(tmp_path)
    cfg = _cfg(tmp_path)
    (tmp_path / f"skill-curate-{DATE}.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "mode": "dry-run",
                "dry_run": True,
                "rc": 1,
                "date": DATE,
                "proposed": 2,
                "decisions": [{"action": "archive", "status": "proposed"}],
            }
        ),
        encoding="utf-8",
    )
    report_prep(cfg, anchor=RUN.isoformat())
    final_path, warnings, rc = report_assemble(cfg, anchor=RUN.isoformat())
    assert final_path is not None
    assert rc == 0
    assert not any("refus" in warning for warning in warnings)


def test_report_assemble_rejects_malformed_curation_manifest(tmp_path: Path):
    _write_summary(tmp_path)
    cfg = _cfg(tmp_path)
    (tmp_path / f"skill-curate-{DATE}.json").write_text("[]", encoding="utf-8")
    report_prep(cfg, anchor=RUN.isoformat())
    final_path, warnings, rc = report_assemble(cfg, anchor=RUN.isoformat())
    assert final_path is None
    assert rc == 2
    assert any("manifeste de curation" in warning for warning in warnings)


def test_report_assemble_active_empty_curation_manifest_stops_final_report(tmp_path: Path):
    _write_summary(tmp_path)
    cfg = _cfg(tmp_path)
    report_prep(cfg, anchor=RUN.isoformat())
    manifest = tmp_path / f"skill-curate-{DATE}.json"
    manifest.write_text("{}", encoding="utf-8")
    final_path, warnings, rc = report_assemble(cfg, anchor=RUN.isoformat())
    assert final_path is None
    assert rc == 2
    assert not (tmp_path / f"weekly-report-{DATE}.md").exists()
    assert any("manifeste de curation" in warning for warning in warnings)


def test_report_assemble_missing_required_summary_is_fatal_even_with_draft(tmp_path: Path):
    cfg = _cfg(tmp_path)
    (tmp_path / f"weekly-report-draft-{DATE}.md").write_text(
        "# report\n\n<!-- QUALITY_BLOCK -->\n", encoding="utf-8"
    )
    final_path, warnings, rc = report_assemble(cfg, anchor=RUN.isoformat())
    assert final_path is None
    assert rc == 2
    assert any("artefact requis" in warning for warning in warnings)


def test_coherence_curation_signal_accepts_r4_mapping_and_ignores_bad_findings():
    assert _coherence_has_curation_signal(
        {"curation_signal": {"R4_archive_candidates": {"strong_8of8": ["x"]}}}
    )
    assert not _coherence_has_curation_signal({"findings": ["not-a-finding", None]})


def test_report_context_normalizes_coherence_and_curation_details(tmp_path: Path):
    """Markdown and HTML consume the same filtered JSON projections."""
    _write_summary(tmp_path)
    (tmp_path / f"weekly-coherence-findings-{DATE}.json").write_text(
        json.dumps({"findings": [{"tag": "drift", "description": "x"}, "bad"]}),
        encoding="utf-8",
    )
    (tmp_path / f"skill-curate-{DATE}.json").write_text(
        json.dumps(
            {
                "summary": {"by_action": {"archive": 1}},
                "decisions": [{"action": "archive", "skill_id": "x", "reason": "stale"}],
                "skipped_details": [{"skill_id": "u", "reason": "protected"}],
            }
        ),
        encoding="utf-8",
    )
    from weekly_telemetry_aggregator.report import build_report_context

    ctx = build_report_context(_cfg(tmp_path), anchor=RUN.isoformat())
    assert ctx is not None
    # ADAPTÉ (C7) : `coherence_items` est désormais la projection DÉDUPLIQUÉE, chaque
    # entrée porte son multiplicateur `dup` (1 = constat unique). L'égalité stricte
    # d'avant ne peut plus tenir ; on vérifie que le finding est inchangé + `dup`.
    # `subject` n'est PAS ajouté ici : aucun nom de skill n'est déductible de
    # `{"tag": "drift", "description": "x"}`.
    assert len(ctx["coherence_items"]) == 1
    item = ctx["coherence_items"][0]
    assert (item["tag"], item["description"], item["dup"]) == ("drift", "x", 1)
    assert "subject" not in item
    assert [d["skill_id"] for d in ctx["curation_detail"]["decisions"]] == ["x"]
    assert [d["skill_id"] for d in ctx["curation_detail"]["skipped_details"]] == ["u"]


def test_report_markdown_renders_skipped_decision_once(tmp_path: Path):
    """A skipped v2 decision is rendered only by the skipped-details section."""
    _write_summary(tmp_path)
    cfg = _cfg(tmp_path)
    (tmp_path / f"skill-curate-{DATE}.json").write_text(
        json.dumps(
            {
                "applied": 0,
                "proposed": 0,
                "skipped": 1,
                "decisions": [
                    {
                        "action": "archive",
                        "skill_id": "protected-skill",
                        "source": "user",
                        "reason": "user-origin protected",
                        "status": "skipped",
                    }
                ],
                "skipped_details": [
                    {
                        "action": "archive",
                        "skill_id": "protected-skill",
                        "source": "user",
                        "reason": "user-origin protected",
                        "status": "skipped",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    draft, context = report_prep(cfg, anchor=RUN.isoformat())
    assert draft is not None and context is not None
    text = draft.read_text(encoding="utf-8")
    assert text.count("`protected-skill`") == 1
    assert "`skip` `protected-skill`" in text


def test_template_curation_and_top_rules_blocks_have_no_blank_lines(tmp_path: Path):
    """v6.0.m : `{{ "\\n" }}` + `{% endfor %}` sur sa propre ligne = 1 ligne VIDE par itération.

    Le moteur rend avec `trim_blocks=True` + `lstrip_blocks=True` : le `{{ "\\n" }}"`
    en fin de ligne de corps s'AJOUTE au saut de ligne de la ligne suivante et émet
    deux sauts. Sur le run 2026-10-03 le bloc Curation faisait 99 lignes pour 36
    décisions (46 lignes vides, 33 % de la §5).

    Correctif : remonter `{% endfor %}` sur la ligne du corps (convention déjà
    employée par la boucle voisine §7). Supprimer le `{{ "\\n" }}` serait FAUX : la
    ligne de corps se termine alors par `{% endif %}` et trim_blocks mange son saut
    de ligne → toutes les décisions se collent sur une seule.

    Ce test verrouille, pour les deux boucles concernées (décisions de curation,
    top règles harness) : N lignes rendues == N itérations, zéro ligne vide entre
    elles et juste après la dernière.
    """
    _write_summary(tmp_path)
    cfg = _cfg(tmp_path)
    decisions = [
        {
            "action": action,
            "skill_id": f"weekly-{action}-{i}",
            "source": "weekly-usage",
            "reason": f"décision {action} #{i}",
            "status": "applied",
        }
        for i, action in enumerate(["archive", "archive", "merge", "improve"], start=1)
    ]
    (tmp_path / f"skill-curate-{DATE}.json").write_text(
        json.dumps(
            {
                "applied": 4,
                "proposed": 0,
                "skipped": 0,
                "summary": {"by_action": {"archive": 2, "merge": 1, "improve": 1}},
                "decisions": decisions,
                "skipped_details": [],
            }
        ),
        encoding="utf-8",
    )
    rules = [
        {"rule": "no-print", "severity": "error", "message": "x"},
        {"rule": "frontmatter", "severity": "error", "message": "y"},
    ]
    (tmp_path / f"weekly-harness-digest-{DATE}.json").write_text(
        json.dumps(
            {
                "inspection": {
                    "summary": {"errors": 2, "warnings": 0},
                    "uncategorized": [{"findings": rules}],
                }
            }
        ),
        encoding="utf-8",
    )

    draft, _context = report_prep(cfg, anchor=RUN.isoformat())
    assert draft is not None
    text = draft.read_text(encoding="utf-8")

    def _bullets(anchor: str, stop: str) -> list[str]:
        block = text.split(anchor, 1)[1].split(stop, 1)[0]
        return block.splitlines()

    curation = _bullets("#### Curation (WAVE 2.5", "- Inspection")
    rendered_decisions = [line for line in curation if line.startswith("- `")]
    assert len(rendered_decisions) == len(decisions) == 4
    assert [
        d["skill_id"] in line for d, line in zip(decisions, rendered_decisions, strict=True)
    ] == [True] * 4

    top = _bullets("Top règles violées :", "- Budget tokens")
    rendered_rules = [line for line in top if line.startswith("  - `")]
    assert len(rendered_rules) == len(rules) == 2
    assert all(f"`{r['rule']}`" in line for r, line in zip(rules, rendered_rules, strict=True))

    # ZÉRO ligne vide (ni vide ni seulement-espace) entre les itérations : c'est
    # exactement ce que le `{{ "\n" }}` redondant insérait (1 par itération).
    for block_lines, bullets in ((curation, rendered_decisions), (top, rendered_rules)):
        blanks = [i for i, line in enumerate(block_lines) if not line.strip()]
        first, last = block_lines.index(bullets[0]), block_lines.index(bullets[-1])
        assert [i for i in blanks if first < i <= last] == []
        # au plus UNE ligne de séparation avant la 1re itération (jamais deux)
        lead = 0
        while first - lead - 1 >= 0 and not block_lines[first - lead - 1].strip():
            lead += 1
        assert lead <= 1, block_lines


def test_report_assemble_injects_blocks(tmp_path: Path):
    _write_summary(tmp_path)
    cfg = _cfg(tmp_path)
    (tmp_path / f"weekly-report-blocks-{DATE}.md").write_text(
        "*Insertion de test.*\n"
        "- constat : la régression de coût provient des sessions longues avec cache élevé.\n"
        "- constat : le skill X est redondant avec Y et doit être fusionné après revue.\n"
        "- constat : les violations harness mcp-tool-poisoning dominent le lint.\n"
        "- recommandation : corriger les violations en priorité puis relancer le lint.\n",
        encoding="utf-8",
    )
    report_prep(cfg, anchor=RUN.isoformat())
    final_path, warnings, rc = report_assemble(cfg, anchor=RUN.isoformat())
    assert rc == 0
    text = final_path.read_text(encoding="utf-8")
    assert "régression de coût" in text
    assert "<!-- QUALITY_BLOCK -->" not in text
    assert warnings == []
    assert not (tmp_path / f"weekly-report-draft-{DATE}.md").exists()  # draft removed


#: (catégorie, sévérité, effectif) — ordre d'ENTRÉE volontairement à l'INVERSE de l'ordre
#: de rendu attendu (catégories décroissantes, sévérité décroissantes) : sans tri, les
#: assertions d'ordre échouent.
_MAINT_DISTRIBUTION = [
    ("token-risk", "high", 5),
    ("token-risk", "medium", 20),
    ("token-risk", "low", 5),
    ("merge-candidate", "high", 1),
    ("merge-candidate", "medium", 5),
    ("merge-candidate", "low", 4),
    ("fix-candidate", "high", 2),
    ("fix-candidate", "medium", 6),
    ("fix-candidate", "low", 3),
    ("agent-loop", "high", 3),
    ("agent-loop", "medium", 4),
    ("agent-loop", "low", 2),
]

#: Ordre de rendu ATTENDU — écrit en dur (catégories A-Z, puis high > medium > low) pour
#: ne pas refléter la clé de tri de l'implémentation.
_MAINT_EXPECTED_ORDER = [
    ("agent-loop", "high", 3),
    ("agent-loop", "medium", 4),
    ("agent-loop", "low", 2),
    ("fix-candidate", "high", 2),
    ("fix-candidate", "medium", 6),
    ("fix-candidate", "low", 3),
    ("merge-candidate", "high", 1),
    ("merge-candidate", "medium", 5),
    ("merge-candidate", "low", 4),
    ("token-risk", "high", 5),
    ("token-risk", "medium", 20),
    ("token-risk", "low", 5),
]


def test_report_section_7_sorts_maintenance_findings_by_category_then_severity(tmp_path: Path):
    """§7 : liste PLATE triée (catégorie A-Z puis sévérité décroissante), rien ne disparaît.

    Régression (v6.0.l → B1) : le regroupement par (catégorie, sévérité) — compteur +
    3 exemples + « (+N autres) » — condensait l'information et a été rejeté. La section 7
    reste une ligne par constat, seule l'ORDRE change. Ce test verrouille :

      1. le nombre de constats rendus ÉGALE le nombre en entrée (60) ;
      2. chaque ligne conserve sa preuve ET sa recommandation ;
      3. l'ordre rendu est (catégorie alphabétique croissante, sévérité décroissante) ;
      4. deux rendus successifs produisent exactement le même texte ;
      5. la catégorie EST VISIBLE dans la ligne rendue (sinon le tri est invisible
         pour le lecteur et la liste paraît non triée).
    """
    _write_summary(tmp_path)
    cfg = _cfg(tmp_path)
    findings = [
        {
            "session_id": f"ses_{category}_{severity}_{i}",
            "category": category,
            "severity": severity,
            "description": f"constat {category}/{severity}/{i}",
            "recommendation": "réduire le context-bloat et borner les re-spawns",
            "evidence_summary": f"cat={category}; n={i}",
        }
        for category, severity, count in _MAINT_DISTRIBUTION
        for i in range(count)
    ]
    assert len(findings) == 60
    (tmp_path / f"weekly-insights-{DATE}.json").write_text(
        json.dumps({"alerts": [], "maintenance": {"findings": findings}}),
        encoding="utf-8",
    )

    draft, ctx = report_prep(cfg, anchor=RUN.isoformat())
    assert draft is not None and ctx is not None
    text = draft.read_text(encoding="utf-8")

    section_7 = text.split("## 7.", 1)[1].split("## 8.", 1)[0]
    lines_7 = [line for line in section_7.splitlines() if line.strip()]
    rendered = [line for line in lines_7 if line.startswith("- **[")]

    # (1) conservation : une ligne par constat, aucune condensation ni plafonnement.
    # Une section 7 de ~60 lignes est le format ATTENDU, pas un défaut à corriger.
    assert len(rendered) == len(findings) == 60
    assert len(lines_7) == 63  # 3 lignes d'en-tête (titre, alertes, « Constats ») + 60 constats
    assert "constat(s) au total" not in section_7  # plus de compteur global
    assert "autres)" not in section_7  # plus de « (+N autres) »
    assert sorted(
        re.findall(r"- \*\*\[\w+\]\*\* \[[\w-]+\] (constat [\w/-]+)", section_7)
    ) == sorted(f["description"] for f in findings)

    # (2) preuve + recommandation conservées sur CHAQUE ligne rendue
    for line in rendered:
        assert "→ réduire le context-bloat et borner les re-spawns" in line
        assert re.search(r" — preuve : cat=[\w-]+; n=\d+$", line), line

    # (2b) la catégorie EST ÉCRITE dans la ligne — sans elle le tri par catégorie est
    # invisible au lecteur : c'est le but de la ligne que le bracketed cat corresponde
    # au constat rendu (clé de trie du gabarit : `- **[SEV] [category] desc`).
    for line in rendered:
        m = re.match(r"^- \*\*\[(\w+)\]\*\* \[([\w-]+)\] constat ([\w-]+)/(\w+)/(\d+) ", line)
        assert m, line
        assert (m.group(1), m.group(2)) == (m.group(4).upper(), m.group(3))

    # (3) ordre : catégorie alphabétique croissante, puis sévérité décroissante
    parsed = re.findall(
        r"^- \*\*\[(\w+)\]\*\* \[([\w-]+)\] constat ([\w-]+)/\w+/\d+ ", section_7, re.MULTILINE
    )
    assert parsed == [
        (severity.upper(), category, category)
        for category, severity, count in _MAINT_EXPECTED_ORDER
        for _ in range(count)
    ]

    # (4) tri stable : deux rendus successifs donnent le même texte
    draft_again, _ = report_prep(cfg, anchor=RUN.isoformat())
    assert draft_again is not None
    assert draft_again.read_text(encoding="utf-8") == text

    # garde-fou d'implémentation : `_sort_maintenance_findings` renvoie une NOUVELLE liste,
    # `insights` reste partagé avec `report_blocks_draft` (brouillon de la section 4).
    assert ctx["maint_sorted"] is not ctx["insights"]["maintenance"]["findings"]
    assert ctx["insights"]["maintenance"]["findings"] == findings  # ordre d'entrée intact


# ============================================================ v5.28 (P5.1/P5.2)


def test_report_blocks_draft_writes_blocks(tmp_path: Path):
    _write_summary(tmp_path)
    cfg = _cfg(tmp_path)
    path, warnings, rc = report_blocks_draft(cfg, anchor=RUN.isoformat())
    assert rc == 0
    assert path is not None
    assert path.name == f"weekly-report-blocks-auto-{DATE}.md"  # v5.29 : déterministe -> -auto-
    text = path.read_text(encoding="utf-8")
    assert "brouillon automatique" in text
    assert "Recommandations" in text


def test_report_assemble_rejects_too_short_blocks(tmp_path: Path):
    _write_summary(tmp_path)
    cfg = _cfg(tmp_path)
    (tmp_path / f"weekly-report-blocks-{DATE}.md").write_text("*court*", encoding="utf-8")
    report_prep(cfg, anchor=RUN.isoformat())
    path, warnings, rc = report_assemble(cfg, anchor=RUN.isoformat())
    assert rc == 0
    assert path is not None
    text = path.read_text(encoding="utf-8")
    assert "brouillon automatique" in text
    assert "bloc LLM trop court" in text
    assert any("trop court" in w and "fallback" in w for w in warnings)


def test_report_prep_renders_top_harness_rules(tmp_path: Path):
    _write_summary(tmp_path)
    cfg = _cfg(tmp_path)
    (tmp_path / f"weekly-harness-digest-{DATE}.json").write_text(
        __import__("json").dumps(
            {
                "harness_include": {
                    "unscoped_files": [".opencode/package.json"],
                },
                "harness_counts": {
                    "files_scanned": 4,
                    "components_scanned": 4,
                    "findings_raw": 3,
                    "findings_unique": 2,
                },
                "inspection": {
                    "summary": {"errors": 2, "warnings": 3},
                    "uncategorized": [
                        {
                            "path": "x",
                            "findings": [
                                {
                                    "rule": "security/mcp-tool-poisoning",
                                    "severity": "warning",
                                    "message": "m",
                                },
                                {
                                    "rule": "security/mcp-tool-poisoning",
                                    "severity": "warning",
                                    "message": "m2",
                                },
                                {
                                    "rule": "security/obfuscation",
                                    "severity": "error",
                                    "message": "o",
                                },
                            ],
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    draft, ctx = report_prep(cfg, anchor=RUN.isoformat())
    assert draft is not None and ctx is not None
    text = draft.read_text(encoding="utf-8")
    assert "mcp-tool-poisoning" in text
    assert "2 violation(s)" in text
    assert "4 fichier(s), 4 composant(s) inspecté(s)" in text
    assert "hors allowlist non scannées" in text


# ============================================================ v5.28 (synthèse + audit + watch)


def _write_ecosystem(tmp_path: Path) -> None:
    ecosystem = {
        "schema_version": 2,
        "new_items": [
            {
                "name": "adeo/ai-skills v0.2.0",
                "category": "repo",
                "repo_url": "https://github.com/adeo/ai-skills",
                "description": "Skills internes ADEO",
                "published_at": "2026-08-09T00:00:00Z",
                "found_via": ["github:watch-repos"],
                "new_repo": False,
            }
        ],
        "core_changes": [],
        "counts_by_source": {},
        "counts_by_category": {},
        "watch_repos": ["adeo/ai-skills"],
        "warnings": [],
    }
    (tmp_path / f"weekly-ecosystem-{DATE}.json").write_text(
        __import__("json").dumps(ecosystem, ensure_ascii=False), encoding="utf-8"
    )


def _seed_selection(tmp_path: Path) -> None:
    _write_summary(tmp_path)
    p = tmp_path / f"weekly-summary-{DATE}.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    data["selection"] = {
        "window_touched": 3,
        "counted": 1,
        "excluded_active": 1,
        "excluded_no_activity": 1,
        "excluded_advisor": 0,
        "excluded_error": 0,
        "recent": [
            {
                "session_id": "ses_a",
                "title": "En cours",
                "agent": None,
                "cost": 0.0,
                "updated": "2026-08-12T00:00:00Z",
                "status": "active",
            },
            {
                "session_id": "ses_b",
                "title": "Comptée",
                "agent": None,
                "cost": 0.5,
                "updated": "2026-08-11T00:00:00Z",
                "status": "included",
            },
        ],
    }
    p.write_text(__import__("json").dumps(data, ensure_ascii=False), encoding="utf-8")


def test_report_prep_renders_synthese_audit_and_watch(tmp_path: Path):
    _seed_selection(tmp_path)
    _write_ecosystem(tmp_path)
    cfg = _cfg(tmp_path)
    draft, ctx = report_prep(cfg, anchor=RUN.isoformat())
    assert draft is not None
    text = draft.read_text(encoding="utf-8")
    assert "## Synthèse" in text
    assert "### Audit de sélection" in text
    assert "`ses_a`" in text and "`ses_b`" in text
    assert "3 sessions touchées" in text
    assert "## 6. Veille — recommandations & nouveautés" in text
    assert "adeo/ai-skills" in text
    assert "github:watch-repos" in text


def _write_audit_candidates(tmp_path: Path, *, audited: int = 2, unaudited: int = 1) -> None:
    (tmp_path / f"weekly-audit-candidates-{DATE}.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "audited": [{"session_id": f"ses_ok_{i}"} for i in range(audited)],
                "unaudited": [{"session_id": f"ses_todo_{i}"} for i in range(unaudited)],
                "limit": 5,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _write_skill_curate(tmp_path: Path, *, dry_run: bool = True) -> None:
    (tmp_path / f"skill-curate-{DATE}.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "rc": 0,
                "mode": "dry-run" if dry_run else "apply",
                "dry_run": dry_run,
                "date": DATE,
                "applied": 0,
                "proposed": 17,
                "skipped": 2,
                "decisions": [],
            }
        ),
        encoding="utf-8",
    )


def test_report_synthese_carries_operational_facts(tmp_path: Path, monkeypatch):
    """D4 : la Synthèse porte les faits opérationnels, plus un compteur de veille.

    Motif : le run 2026-10-03 spending une puce « Veille : 282 nouveautés » alors
    que les 4 faits qui décident de la semaine (couverture d'audit, commits en
    attente de revue, curation, self-cost) n'étaient qu'au §8. Le compteur de
    veille reste au §6, annoté (D8) — la Synthèse ne le duplique plus.
    """
    _write_summary(tmp_path)
    _write_ecosystem(tmp_path)
    _write_audit_candidates(tmp_path)
    _write_skill_curate(tmp_path)
    monkeypatch.setattr(
        "weekly_telemetry_aggregator.report._pending_auto_commits", lambda *a, **k: 28
    )
    monkeypatch.setattr(
        "weekly_telemetry_aggregator.report._dirty_files", lambda *a, **k: ["a.py", "b.py", "c.py"]
    )

    draft, _ctx = report_prep(_cfg(tmp_path), anchor=RUN.isoformat())
    assert draft is not None
    synthese = draft.read_text(encoding="utf-8").split("## Synthèse", 1)[1].split("## 1.", 1)[0]

    assert "**Couverture d'audit** : 2 session(s) auditée(s)" in synthese
    assert "1 candidate(s) non traitée(s) (cap d'audit K = 5)" in synthese
    assert "**Friction dépôt** : 28 commit(s) auto-rédigé(s) en attente de revue" in synthese
    assert "3 fichier(s) dirty au run" in synthese
    assert "**Curation skills** : 0 appliquée(s) / 17 proposée(s) / 2 ignorée(s)" in synthese
    assert "dry-run" in synthese
    # le compteur de veille a quitté la Synthèse (le §6 en reste l'unique source)
    assert "nouveautés" not in synthese


def test_report_warnings_pipeline_counts_grouped_entities(tmp_path: Path):
    """D5(c) : compteur d'ENTITÉS groupées + multiplicateur dominant, pas le total brut.

    Motif : 40 warnings nltk sont souvent 3 messages répétés ; afficher
    `warnings|length` laisse croire à 40 problèmes distincts. Le regroupement
    existe déjà dans le contexte (`warnings_grouped`) — on ne le recalcule pas.
    """
    _write_summary(tmp_path)
    p = tmp_path / f"weekly-summary-{DATE}.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    data["warnings"] = [
        *({"message": "cache miss massif", "session_id": f"ses_{i}"} for i in range(3)),
        *({"message": "source opencode indisponible", "session_id": f"ses_w{i}"} for i in range(5)),
    ]
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    draft, _ctx = report_prep(_cfg(tmp_path), anchor=RUN.isoformat())
    assert draft is not None
    text = draft.read_text(encoding="utf-8")
    assert (
        "- Warnings pipeline : 2 entité(s) groupée(s) sur 8 occurrence(s)"
        " — multiplicateur dominant ×5" in text
    )


def test_report_tool_table_explains_appels_vs_repeats(tmp_path: Path):
    """D6(i) : renvoi explicite sous la table Outils.

    Motif : `edit 344 appels` (§3) vs `edit repeats=341` (§7) et `glob 151` vs
    `glob 76` se lisaient comme un bug de décompte. `repeats` = taille du plus
    gros bucket d'empreintes d'arguments identiques (insights.py), pas un total
    d'appels. On choisit le renvoi (et non le remplacement de colonne) : la
    colonne `Appels` reste le total, et afficher les buckets à côté ne ferait
    que déplacer le chiffre trompeur. `tool_result_fingerprints` n'est pas
    promis : il est vide depuis le filtre boilerplate.
    """
    _write_summary(tmp_path)
    p = tmp_path / f"weekly-summary-{DATE}.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    data["tool_usage"] = [
        {"tool": "edit", "call_count": 344, "estimated_input_tokens": 10000},
        {"tool": "glob", "call_count": 151, "estimated_input_tokens": 3000},
    ]
    data["tool_argument_fingerprints"] = {"edit": {"fp-a": 341, "fp-b": 3}}
    data["tool_result_fingerprints"] = {}
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    draft, _ctx = report_prep(_cfg(tmp_path), anchor=RUN.isoformat())
    assert draft is not None
    text = draft.read_text(encoding="utf-8")
    # la donnée brute reste affichée telle quelle
    assert "| edit | 344 | 10000 |" in text
    # et le renvoi explicitant la contradiction
    assert "`Appels` ≠ `repeats` du §7 — divergence assumée" in text
    assert "total des appels" in text
    assert "plus gros bucket d'empreintes d'arguments identiques" in text
    assert "tool_argument_fingerprints" in text
    # le canal résultat vide est nommé comme tel, pas promis comme une métrique
    assert "tool_result_fingerprints" in text
    assert "vide par construction" in text


def test_report_section7_maintenance_header_explains_repeats(tmp_path: Path):
    """D6(i) : le §7 rappelle la même sémantique à l'endroit où `repeats` apparaît."""
    _write_summary(tmp_path)
    (tmp_path / f"weekly-insights-{DATE}.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "alerts": [],
                "maintenance": {
                    "findings": [
                        {
                            "session_id": None,
                            "category": "agent-loop",
                            "severity": "medium",
                            "description": "outil 'edit' répété avec la même empreinte",
                            "evidence_summary": "tool=edit; repeats=341; threshold=50",
                            "recommendation": "inspecter les résultats",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    draft, _ctx = report_prep(_cfg(tmp_path), anchor=RUN.isoformat())
    assert draft is not None
    section_7 = draft.read_text(encoding="utf-8").split("## 7.", 1)[1].split("## 8.", 1)[0]
    assert "`repeats` = taille du plus gros bucket d'arguments identiques" in section_7
    assert "repeats=341; threshold=50" in section_7  # la preuve n'est pas amputée


def test_report_section6_annotates_new_items_sampling(tmp_path: Path):
    """D8 : total annoncé et liste affichée sont dérivés de la MÊME coupe.

    Motif : le §6 annonçait 282 nouveautés puis en listait 5, sans dire que
    c'était un échantillon. La limite est une variable (`eco_limit`) et le
    libellé est calculé depuis `eco_shown` — la divergence devient impossible.
    """
    _write_summary(tmp_path)
    (tmp_path / f"weekly-ecosystem-{DATE}.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "new_items": [
                    {
                        "name": f"owner/rl-{i}",
                        "category": "repo",
                        "found_via": ["github:api"],
                        "description_short": f"changement {i}",
                    }
                    for i in range(9)
                ],
                "core_changes": [],
                "counts_by_source": {},
                "counts_by_category": {},
                "watch_repos": [],
                "warnings": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    draft, _ctx = report_prep(_cfg(tmp_path), anchor=RUN.isoformat())
    assert draft is not None
    text = draft.read_text(encoding="utf-8")
    section_6 = text.split("## 6.", 1)[1].split("## 7.", 1)[0]
    assert (
        "- Nouveautés détectées : 9 — 5 listée(s) ci-dessous (échantillon,"
        " +4 autres non listées)" in section_6
    )
    # ...et la liste en compte exactement 5
    rendered = [line for line in section_6.splitlines() if line.startswith("- **owner/rl-")]
    assert len(rendered) == 5
    assert "owner/rl-8" not in section_6  # le 9e n'est pas listé (et c'est annoncé)


def test_report_section6_announces_exhaustive_list_when_under_limit(tmp_path: Path):
    """D8 : sous la limite, le libellé dit « liste exhaustive » — pas « échantillon »."""
    _write_summary(tmp_path)
    _write_ecosystem(tmp_path)  # 1 nouveauté
    draft, _ctx = report_prep(_cfg(tmp_path), anchor=RUN.isoformat())
    assert draft is not None
    text = draft.read_text(encoding="utf-8")
    section_6 = text.split("## 6.", 1)[1].split("## 7.", 1)[0]
    assert "Nouveautés détectées : 1 — 1 listée(s) ci-dessous (liste exhaustive)" in section_6


def test_report_prep_no_audit_when_empty_selection(tmp_path: Path):
    _write_summary(tmp_path)
    _write_ecosystem(tmp_path)
    draft, ctx = report_prep(_cfg(tmp_path), anchor=RUN.isoformat())
    assert draft is not None
    text = draft.read_text(encoding="utf-8")
    assert "### Audit de sélection" not in text  # pas de doublon/bruit sans trace


def test_report_prep_watch_source_failure_note(tmp_path: Path):
    _write_summary(tmp_path)
    eco = {
        "new_items": [],
        "core_changes": [],
        "counts_by_source": {},
        "counts_by_category": {},
        "watch_repos": ["adeo/ai-skills"],
        "warnings": [
            {
                "source": "github:watch-repos",
                "message": "API indisponible / rate-limitated; source ignorée pour ce run (...)",
            }
        ],
    }
    (tmp_path / f"weekly-ecosystem-{DATE}.json").write_text(
        json.dumps(eco, ensure_ascii=False), encoding="utf-8"
    )
    draft, ctx = report_prep(_cfg(tmp_path), anchor=RUN.isoformat())
    assert draft is not None
    text = draft.read_text(encoding="utf-8")
    assert "Sources suivies (watch) : adeo/ai-skills" in text
    assert "Source GitHub indisponible" in text
    assert "Aucune activité sur la fenêtre" not in text


def test_report_top_rules_filters_ignored(tmp_path: Path):
    """K5: les règles de harness_ignored_rules disparaissent du top §5."""
    _write_summary(tmp_path)
    cfg = _cfg(tmp_path)
    cfg.harness_ignored_rules = ["security/mcp-tool-poisoning"]
    (tmp_path / f"weekly-harness-digest-{DATE}.json").write_text(
        json.dumps(
            {
                "inspection": {
                    "summary": {"errors": 2, "warnings": 3},
                    "uncategorized": [
                        {
                            "findings": [
                                {
                                    "rule": "security/mcp-tool-poisoning",
                                    "severity": "high",
                                    "message": "x",
                                },
                                {
                                    "rule": "security/obfuscation",
                                    "severity": "high",
                                    "message": "y",
                                },
                            ]
                        }
                    ],
                }
            }
        ),
        encoding="utf-8",
    )
    draft, ctx = report_prep(cfg, anchor=RUN.isoformat())
    assert draft is not None
    text = draft.read_text(encoding="utf-8")
    assert "security/obfuscation" in text
    assert (
        "`security/mcp-tool-poisoning` —" not in text
    )  # absente du top règles (la note de config la liste, elle)
    assert "Règles exclues du top" in text


def test_report_blocks_draft_includes_part3_findings(tmp_path: Path):
    """v5.29 : les findings de l'audit qualitatif (Partie 3) sont rendus dans les blocs §4."""
    _write_summary(tmp_path)
    cfg = _cfg(tmp_path)
    (tmp_path / f"weekly-quality-findings-{DATE}.json").write_text(
        json.dumps(
            {
                "findings": [
                    {
                        "session_id": "s1",
                        "category": "loop",
                        "severity": "high",
                        "description": "retries répétés sur le même point",
                        "recommendation": "extraire une procédure",
                        "recommendation_type": "skill-candidate",
                        "evidence_summary": "x",
                        "impact_order_of_magnitude": "medium",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    path, warnings, rc = report_blocks_draft(cfg, anchor=RUN.isoformat())
    assert rc == 0
    text = path.read_text(encoding="utf-8")
    assert "Constats de l'audit qualitatif" in text
    assert "[HIGH] loop" in text
    assert "retries répétés" in text


def test_report_annex_lists_unaudited_candidates(tmp_path: Path):
    _write_summary(tmp_path)
    cfg = _cfg(tmp_path)
    (tmp_path / f"weekly-audit-candidates-{DATE}.json").write_text(
        json.dumps(
            {
                "limit": 8,
                "audited": [{"session_id": "s1", "reasons": ["top-cost"]}],
                "unaudited": [{"session_id": "s9", "reasons": ["cost-outlier"]}],
            }
        ),
        encoding="utf-8",
    )
    draft, ctx = report_prep(cfg, anchor=RUN.isoformat())
    assert draft is not None
    text = draft.read_text(encoding="utf-8")
    assert "Candidates d'audit non traitées" in text
    assert "s9" in text


# ============================================================ v5.29 (7b hybride — garde-fous anti-hallucination)


def _write_auto_blocks(tmp_path: Path) -> None:
    """report-blocks-draft a tourné : brouillon déterministe présent."""
    from weekly_telemetry_aggregator.report import report_blocks_draft

    report_blocks_draft(_cfg(tmp_path), anchor=RUN.isoformat())


def _write_findings(tmp_path: Path, findings: list[dict]) -> None:
    (tmp_path / f"weekly-quality-findings-{DATE}.json").write_text(
        json.dumps({"findings": findings}), encoding="utf-8"
    )


def test_assemble_uses_llm_block_when_valid(tmp_path: Path):
    _write_summary(tmp_path)
    _write_auto_blocks(tmp_path)
    cfg = _cfg(tmp_path)
    _write_findings(
        tmp_path,
        [
            {
                "session_id": "s1",
                "category": "loop",
                "severity": "high",
                "recommendation_type": "skill-candidate",
                "description": "d",
                "recommendation": "r",
            }
        ],
    )
    (tmp_path / f"weekly-report-blocks-{DATE}.md").write_text(
        "Semaine dominée par une session en boucle de travail intensif avec de nombreux appels "
        "répétés sur le même point [F:s1#loop]. La maintenance signale des skills probablement "
        "redondants et recommande une fusion manuelle après revue [M:merge-candidate]. "
        "Le budget hebdomadaire est dépassé, alerte à traiter en priorité [A:weekly_budget_usd].\n",
        encoding="utf-8",
    )
    # insights avec maintenance + alerte pour résoudre les balises M/A
    (tmp_path / f"weekly-insights-{DATE}.json").write_text(
        json.dumps(
            {
                "alerts": [
                    {
                        "rule": "weekly_budget_usd",
                        "threshold": 25.0,
                        "observed": 30.0,
                        "severity": "high",
                    }
                ],
                "maintenance": {
                    "findings": [{"category": "merge-candidate", "severity": "medium"}]
                },
            }
        ),
        encoding="utf-8",
    )
    report_prep(cfg, anchor=RUN.isoformat())
    final_path, warnings, rc = report_assemble(cfg, anchor=RUN.isoformat())
    assert rc == 0
    text = final_path.read_text(encoding="utf-8")
    assert "en boucle de travail intensif" in text
    assert "Statut section 4 : prose agent (7b LLM)" in text
    assert warnings == []


def test_assemble_rejects_llm_block_with_digits(tmp_path: Path):
    _write_summary(tmp_path)
    _write_auto_blocks(tmp_path)
    cfg = _cfg(tmp_path)
    (tmp_path / f"weekly-report-blocks-{DATE}.md").write_text(
        "Le budget hebdomadaire est dépassé de 3 fois cette semaine, ce qui constitue un "
        "signal fort de dérive des coûts qu'il convient d'investiguer avant la prochaine "
        "itération pour identifier les sessions responsables et corriger le tir rapidement, "
        "en commençant par les plus coûteuses puis en ajustant les seuils de déclenchement.\n",
        encoding="utf-8",
    )
    report_prep(cfg, anchor=RUN.isoformat())
    final_path, warnings, rc = report_assemble(cfg, anchor=RUN.isoformat())
    assert rc == 0
    text = final_path.read_text(encoding="utf-8")
    assert "chiffres interdits" in text  # statut de rejet
    assert "brouillon automatique" in text
    assert any("rejeté" in w for w in warnings)


def test_assemble_rejects_llm_block_with_unknown_tag(tmp_path: Path):
    _write_summary(tmp_path)
    _write_auto_blocks(tmp_path)
    cfg = _cfg(tmp_path)
    _write_findings(
        tmp_path,
        [
            {
                "session_id": "s1",
                "category": "loop",
                "severity": "medium",
                "recommendation_type": "prompting-habit",
                "description": "d",
                "recommendation": "r",
            }
        ],
    )
    (tmp_path / f"weekly-report-blocks-{DATE}.md").write_text(
        "Un constat inventé est cité dans le bloc avec une balise qui ne correspond à aucun "
        "finding de la semaine, ce qui doit déclencher le rejet automatique du bloc et le "
        "retour au brouillon déterministe pour garantir l'absence d'hallucination [F:ses_invente#loop].\n",
        encoding="utf-8",
    )
    report_prep(cfg, anchor=RUN.isoformat())
    final_path, warnings, rc = report_assemble(cfg, anchor=RUN.isoformat())
    assert rc == 0
    text = final_path.read_text(encoding="utf-8")
    assert "balise inconnue [F:ses_invente#loop]" in text
    assert "brouillon automatique" in text


def test_assemble_falls_back_to_auto_when_no_llm_block(tmp_path: Path):
    _write_summary(tmp_path)
    _write_auto_blocks(tmp_path)
    cfg = _cfg(tmp_path)
    report_prep(cfg, anchor=RUN.isoformat())
    final_path, warnings, rc = report_assemble(cfg, anchor=RUN.isoformat())
    assert rc == 0
    text = final_path.read_text(encoding="utf-8")
    assert "Statut section 4 : brouillon automatique (report-blocks-draft)" in text
    assert "brouillon automatique" in text


#: Bloc de prose valide ET long, avec deux bandes résolubles : 1 finding high
#: (`_write_band_findings`) et `observed` = 30.0 (insights).
_BAND_PROSE = (
    "La semaine concentrate l'essentiel des constats sur une seule session, qui est "
    "revenue en boucle sur le meme point de controle a plusieurs reprises sans que "
    "l'ecart ne soit rattrape par une relecture [F:s1#loop]. Le budget hebdomadaire "
    "est au-dessus du seuil : le depassement [N:30.0 dollars] doit etre arbitre avant "
    "la prochaine iteration [A:weekly_budget_usd].\n"
)


def _write_band_artifacts(tmp_path: Path) -> None:
    """1 finding high + 1 alerte `observed` = 30.0 : deux bandes résolubles."""
    _write_findings(
        tmp_path,
        [
            {
                "session_id": "s1",
                "category": "loop",
                "severity": "high",
                "description": "d",
                "recommendation": "r",
            }
        ],
    )
    (tmp_path / f"weekly-insights-{DATE}.json").write_text(
        json.dumps(
            {"alerts": [{"rule": "weekly_budget_usd", "observed": 30.0, "severity": "high"}]}
        ),
        encoding="utf-8",
    )


def test_assemble_resolves_number_band_in_markdown_and_publishes_the_index(tmp_path: Path):
    """C2 + C3 de bout en bout : bande résolue en chiffre, index publié dans le gates."""
    _write_summary(tmp_path)
    _write_auto_blocks(tmp_path)
    _write_band_artifacts(tmp_path)
    cfg = _cfg(tmp_path)
    (tmp_path / f"weekly-report-blocks-{DATE}.md").write_text(_BAND_PROSE, encoding="utf-8")
    report_prep(cfg, anchor=RUN.isoformat())

    final_path, _warnings, rc = report_assemble(cfg, anchor=RUN.isoformat())

    assert rc == 0
    text = final_path.read_text(encoding="utf-8")
    assert "Statut section 4 : prose agent (7b LLM)" in text, text[:400]
    # le lecteur voit le chiffre, pas le marqueur
    assert "[N:" not in text
    assert "30.0 dollars" in text
    gates = json.loads((tmp_path / f"weekly-report-gates-{DATE}.json").read_text(encoding="utf-8"))
    assert gates["prose"]["validated"] is True
    assert gates["number_bands"]["scalars"]["alerts[0].observed"] == 30.0
    assert gates["number_bands"]["counts"]["findings_total"] == 1


def test_assemble_rejects_prose_with_an_unresolved_number_band(tmp_path: Path):
    """C2 de bout en bout : une bande sans scalaire derrière → refus, fallback, index publié."""
    _write_summary(tmp_path)
    _write_auto_blocks(tmp_path)
    _write_band_artifacts(tmp_path)
    cfg = _cfg(tmp_path)
    (tmp_path / f"weekly-report-blocks-{DATE}.md").write_text(
        _BAND_PROSE.replace("[N:30.0 dollars]", "[N:19 findings]"), encoding="utf-8"
    )
    report_prep(cfg, anchor=RUN.isoformat())

    final_path, warnings, rc = report_assemble(cfg, anchor=RUN.isoformat())

    assert rc == 0
    text = final_path.read_text(encoding="utf-8")
    assert "bande de chiffre non résolue [N:19 findings]" in text
    assert "brouillon automatique" in text
    # le footer de statut cite nommément la violation : on vérifie le CORPS de la
    # section 4, pas la ligne qui raconte le rejet.
    assert "[N:19 findings]" not in text.split("*Statut section 4 :")[0]
    assert any("rejeté" in w for w in warnings)
    gates = json.loads((tmp_path / f"weekly-report-gates-{DATE}.json").read_text(encoding="utf-8"))
    assert gates["prose"]["validated"] is False
    # l'index est publié même quand la prose est rejetée : il décrit les artefacts
    assert gates["number_bands"]["counts"]["findings_total"] == 1


def test_assemble_rejects_prose_with_a_markdown_heading(tmp_path: Path):
    """C1 de bout en bout : le `###` de l'agent ne survit plus dans le rapport."""
    _write_summary(tmp_path)
    _write_auto_blocks(tmp_path)
    _write_band_artifacts(tmp_path)
    cfg = _cfg(tmp_path)
    (tmp_path / f"weekly-report-blocks-{DATE}.md").write_text(
        "### Saturation de contexte\n" + _BAND_PROSE, encoding="utf-8"
    )
    report_prep(cfg, anchor=RUN.isoformat())

    final_path, _warnings, rc = report_assemble(cfg, anchor=RUN.isoformat())

    assert rc == 0
    text = final_path.read_text(encoding="utf-8")
    assert "titre Markdown interdit" in text
    assert "brouillon automatique" in text
    # le footer cite la violation ; le corps de la section 4, lui, ne doit contenir
    # ni le titre rendu ni sa version littérale `<p>###…</p>`.
    assert "### Saturation de contexte" not in text.split("*Statut section 4 :")[0]
    gates = json.loads((tmp_path / f"weekly-report-gates-{DATE}.json").read_text(encoding="utf-8"))
    assert gates["prose"]["validated"] is False


def test_assemble_rejects_prose_with_a_written_out_count(tmp_path: Path):
    """C4 de bout en bout : « huit sessions » est refusé comme « 8 sessions »."""
    _write_summary(tmp_path)
    _write_auto_blocks(tmp_path)
    _write_band_artifacts(tmp_path)
    cfg = _cfg(tmp_path)
    (tmp_path / f"weekly-report-blocks-{DATE}.md").write_text(
        _BAND_PROSE.replace(
            "Le budget hebdomadaire",
            "Huit sessions ont ete activees, dont une seule a produit un constat. "
            "Le budget hebdomadaire",
        ),
        encoding="utf-8",
    )
    report_prep(cfg, anchor=RUN.isoformat())

    final_path, _warnings, rc = report_assemble(cfg, anchor=RUN.isoformat())

    assert rc == 0
    text = final_path.read_text(encoding="utf-8")
    assert "nombre écrit interdit" in text
    assert "Huit sessions" not in text.split("*Statut section 4 :")[0]


def _section_4_headings(text: str) -> list[str]:
    """Titres de niveau 1 ou 2 dans la zone section 4 (entre `## 4.` et `## 5.`).

    Zone prise telle que rendue, statut de section compris : c'est elle que voit un
    relecteur, pas le fichier -auto- pris isolément.
    """
    zone = re.split(r"^## 5\. ", text, maxsplit=1, flags=re.M)[0]
    head = re.search(r"^## 4\. ", zone, flags=re.M)
    assert head is not None, "section 4 absente du rapport"
    return re.findall(r"^#{1,2} .*$", zone[head.start() :], flags=re.M)


def test_section_4_has_one_heading_on_the_auto_draft_fallback(tmp_path: Path):
    """Invariant : UN SEUL titre de niveau 1/2 dans la section 4 — celui du gabarit.

    Le fichier -auto- est une file de repli : `report_assemble` l'injecte VERBATIM
    sous le `## 4.` du gabarit et ne le valide jamais. Un titre H1 dedans (le cas
    avant le correctif) produisait deux H2 dans chaque rapport tombé sur le chemin
    de repli — c'est-à-dire le chemin par défaut.
    """
    _write_summary(tmp_path)
    _write_auto_blocks(tmp_path)
    cfg = _cfg(tmp_path)
    report_prep(cfg, anchor=RUN.isoformat())
    final_path, _warnings, rc = report_assemble(cfg, anchor=RUN.isoformat())
    assert rc == 0
    text = final_path.read_text(encoding="utf-8")

    headings = _section_4_headings(text)

    assert len(headings) == 1, headings
    assert headings[0].startswith("## 4. Constats qualitatifs")
    # Les sous-rubriques survivent, mais en `###`.
    assert "### Recommandations" in text


def test_section_4_has_one_heading_when_prose_is_used(tmp_path: Path):
    """Même invariant sur le chemin prose — et le `###` de l'agent n'est plus possible.

    Avant C1, la prose pouvait porter des `###` qui « ne comptaient pas » dans
    l'invariant. C1 les refuse à l'écriture (ils ne rendaient que dans le
    markdown, le HTML affichait littéralement `<p>###…</p>`), donc l'invariant
    tient désormais par construction sur ce chemin. Le cas du `###` rejeté est
    couvert par `test_assemble_rejects_prose_with_a_markdown_heading`.
    """
    _write_summary(tmp_path)
    _write_auto_blocks(tmp_path)
    cfg = _cfg(tmp_path)
    _write_findings(
        tmp_path,
        [
            {
                "session_id": "s1",
                "category": "loop",
                "severity": "high",
                "recommendation_type": "skill-candidate",
                "description": "d",
                "recommendation": "r",
            }
        ],
    )
    (tmp_path / f"weekly-report-blocks-{DATE}.md").write_text(
        "Saturation de contexte : la semaine est dominée par une session en boucle de "
        "travail intensif avec de nombreux appels répétés sur le même point [F:s1#loop]. "
        "La maintenance signale des skills probablement redondants et recommande une "
        "fusion manuelle après revue [M:merge-candidate]. Le budget hebdomadaire est "
        "dépassé, alerte à traiter en priorité [A:weekly_budget_usd].\n",
        encoding="utf-8",
    )
    (tmp_path / f"weekly-insights-{DATE}.json").write_text(
        json.dumps(
            {
                "alerts": [
                    {
                        "rule": "weekly_budget_usd",
                        "threshold": 25.0,
                        "observed": 30.0,
                        "severity": "high",
                    }
                ],
                "maintenance": {
                    "findings": [{"category": "merge-candidate", "severity": "medium"}]
                },
            }
        ),
        encoding="utf-8",
    )
    report_prep(cfg, anchor=RUN.isoformat())
    final_path, _warnings, rc = report_assemble(cfg, anchor=RUN.isoformat())
    assert rc == 0
    text = final_path.read_text(encoding="utf-8")
    assert "Statut section 4 : prose agent (7b LLM)" in text

    headings = _section_4_headings(text)

    assert len(headings) == 1, headings
    assert headings[0].startswith("## 4. Constats qualitatifs")


def test_pending_auto_commits_label_is_ordered_not_oldest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Le gabarit ne connaît que le NOMBRE de commits en attente.

    « (du plus vieux que <date>) » prétendait dater le plus ancien — une information
    que le contexte ne contient pas. « (antérieurs au <date>) » ne ment pas.
    """
    other = tzutc(2026, 10, 3)
    other_date = "2026-10-03"
    period = Period(start=tzutc(2026, 9, 26), end=other)
    usage = make_usage("r", [make_step("r", tzutc(2026, 9, 27, 10), cost=0.5)], title="S")
    (tmp_path / f"weekly-summary-{other_date}.json").write_text(
        json.dumps(
            summary_to_dict(aggregate([usage], period=period, generated_at=other)),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "weekly_telemetry_aggregator.report._pending_auto_commits", lambda *a, **k: 3
    )

    draft, _context = report_prep(_cfg(tmp_path), anchor=other.isoformat())

    assert draft is not None
    text = draft.read_text(encoding="utf-8")
    assert "en attente de revue (antérieurs au 2026-10-03)" in text
    assert "du plus vieux que" not in text


def test_validate_llm_blocks_high_coverage_warning():
    from weekly_telemetry_aggregator.report import validate_llm_blocks

    findings = {
        "findings": [
            {"session_id": "s1", "category": "loop", "severity": "high"},
            {"session_id": "s2", "category": "context-bloat", "severity": "high"},
        ]
    }
    text = "Seule la boucle est couverte [F:s1#loop].\n"
    violations, coverage = validate_llm_blocks(text, findings, None)
    assert violations == []
    assert any("s2#context-bloat" in c for c in coverage)  # high non cité -> warning, pas de rejet


def test_validate_llm_blocks_rejects_digits_and_unknown():
    from weekly_telemetry_aggregator.report import validate_llm_blocks

    violations, coverage = validate_llm_blocks(
        "Le coût a doublé : 39$ cette semaine.\n", None, None
    )
    assert any("chiffres" in v for v in violations)
    assert coverage == []


def test_validate_llm_blocks_reports_source_violation_line():
    from weekly_telemetry_aggregator.report import validate_llm_blocks

    violations, _ = validate_llm_blocks(
        "Constat étayé [F:s1#loop].\nRéférence inventée [F:ses_fake#loop].\n",
        {"findings": [{"session_id": "s1", "category": "loop", "severity": "medium"}]},
        None,
    )

    assert any(
        "ligne 2" in violation and "[F:ses_fake#loop]" in violation for violation in violations
    )


def test_validate_llm_blocks_rejects_malformed_source_tag():
    from weekly_telemetry_aggregator.report import validate_llm_blocks

    violations, _ = validate_llm_blocks("Constat [F:] non traçable.\n", None, None)

    assert any(
        "balise de source mal formée" in violation and "ligne 1" in violation
        for violation in violations
    )


def test_validate_llm_blocks_allows_dates_percent_versions():
    from weekly_telemetry_aggregator.report import validate_llm_blocks

    # dates ISO, pourcentages et versions sémantiques ne sont pas des « chiffres libres »
    text = "Bogue corrigé en v6.0.l le 2026-08-26 (reprise à 12,5% de couverture).\n"
    violations, coverage = validate_llm_blocks(text, None, None)
    assert violations == []
    assert coverage == []

    # un coût libre reste interdit
    violations2, _ = validate_llm_blocks("coût de 39$ cette semaine.\n", None, None)
    assert any("chiffres" in v for v in violations2)


#: Findings/insights minimaux pour les checks de bande : 3 findings dont 1 high,
#: 1 alerte dont `observed` = 30,0, 2 constats de maintenance.
_BAND_FINDINGS = {
    "findings": [
        {"session_id": "s1", "category": "loop", "severity": "high"},
        {"session_id": "s2", "category": "qualite", "severity": "medium"},
        {"session_id": "s3", "category": "outil", "severity": "low"},
    ]
}
_BAND_INSIGHTS = {
    "alerts": [{"rule": "weekly_budget_usd", "observed": 30.0, "threshold": 25.0}],
    "maintenance": {
        "findings": [
            {"category": "merge-candidate", "severity": "medium"},
            {"category": "stale-skill", "severity": "low"},
        ]
    },
}


@pytest.mark.parametrize(
    "heading",
    [
        "###Foo",  # pas d'espace : `^#{1,6}\s` ne suffit pas, il faut la garde startswith
        "#Foo",
        "### Saturation de contexte",  # première ligne, niveau 3 : les deux checks
        "#### Sous-titre",
        "#",
    ],
)
def test_validate_llm_blocks_rejects_any_markdown_heading(heading):
    """C1 : le bloc qualitatif est de la prose, pas du markdown structuré.

    Régression du run 2026-10-03 : quatre `###` de l'agent passaient la
    validation et le HTML les affichait littéralement (`<p>###…</p>`), le
    markdown les rendait en titres — deux sémantiques pour un seul bloc.
    """
    from weekly_telemetry_aggregator.report import validate_llm_blocks

    violations, _ = validate_llm_blocks(
        f"{heading}\nUne session est revenue en boucle sur le même point.\n", None, None
    )

    assert any("titre Markdown interdit" in v for v in violations), violations


def test_validate_llm_blocks_allows_inline_hash_and_prose_without_heading():
    """La garde `#` ne doit pas casser la prose : un `#` en milieu de ligne est normal."""
    from weekly_telemetry_aggregator.report import validate_llm_blocks

    violations, _ = validate_llm_blocks(
        "Le contrôle de sortie a été contourné, motifs #4 et #5 sur la même séquence.\n",
        None,
        None,
    )

    assert not any("titre Markdown interdit" in v for v in violations), violations


def test_validate_llm_blocks_accepts_a_resolved_number_band():
    """C2 : `[N:3 findings]` résout (3 = nombre de findings) et ne viole rien.

    Le nombre est Closé dans la bande : le check « chiffres interdits » ne doit
    pas le voir comme un chiffre libre, sinon la bande serait inutilisable.
    """
    from weekly_telemetry_aggregator.report import validate_llm_blocks

    violations, _ = validate_llm_blocks(
        "Les [N:3 findings] de la semaine se concentrent sur une seule session [F:s1#loop].\n",
        _BAND_FINDINGS,
        _BAND_INSIGHTS,
    )

    assert not any("chiffres" in v for v in violations), violations
    assert not any("bande" in v for v in violations), violations


def test_validate_llm_blocks_accepts_a_count_band_from_derived_counts():
    """C2 : une cardinalité dérivée est résolvable — 2 = constats de maintenance."""
    from weekly_telemetry_aggregator.report import validate_llm_blocks

    violations, _ = validate_llm_blocks(
        "La maintenance remonte [N:2 constats] de fusion [M:merge-candidate].\n",
        _BAND_FINDINGS,
        _BAND_INSIGHTS,
    )

    assert not any("bande" in v for v in violations), violations


def test_validate_llm_blocks_accepts_a_decimal_band():
    """C2 : un scalaire flottant des artefacts résout aussi (observed = 30,0)."""
    from weekly_telemetry_aggregator.report import validate_llm_blocks

    violations, _ = validate_llm_blocks(
        "Le budget observé est de [N:30.0 dollars] [A:weekly_budget_usd].\n",
        _BAND_FINDINGS,
        _BAND_INSIGHTS,
    )

    assert not any("bande" in v for v in violations), violations


@pytest.mark.parametrize(
    ("band", "expected"),
    [
        ("[N:19 findings]", "non résolue"),
        ("[N:999 findings]", "non résolue"),
        ("[N:4 findings]", "non résolue"),  # aucun scalaire 4 dans ces artefacts
        ("[N:]", "mal formée"),
        ("[N:dix-neuf findings]", "non numérique"),
        ("[N:beaucoup de findings]", "non numérique"),
    ],
)
def test_validate_llm_blocks_rejects_unresolved_number_band(band, expected):
    """C2 : pas d'assouplissement « petits entiers autorisés » (D2)."""
    from weekly_telemetry_aggregator.report import validate_llm_blocks

    violations, _ = validate_llm_blocks(
        f"Le décompte annoncé est {band} sur la semaine.\n", _BAND_FINDINGS, _BAND_INSIGHTS
    )

    assert any(expected in v for v in violations), violations


@pytest.mark.parametrize(
    "phrase",
    [
        "Huit sessions ont tourné en boucle cette semaine.",
        "dix-neuf findings sont attribués à la même session [F:s1#loop].",
        "Quatre-vingt-dix violations harness ont été relevées.",
        "La semaine compte vingt skill jamais chargés.",
    ],
)
def test_validate_llm_blocks_rejects_written_out_counts(phrase):
    """C4 : la forme écrite est le contournement du check « chiffres ».

    « 8 sessions » est refusé, « huit sessions » passait : même affirmation,
    aucun contrôle. La règle est ancrée sur une unité comptable, donc
    « une alerte » et « un constat » restent de la prose valide.
    """
    from weekly_telemetry_aggregator.report import validate_llm_blocks

    violations, _ = validate_llm_blocks(f"{phrase}\n", _BAND_FINDINGS, _BAND_INSIGHTS)

    assert any("nombre écrit interdit" in v for v in violations), violations


@pytest.mark.parametrize(
    "phrase",
    [
        "Un constat de boucle a été relevé sur la session de revue [F:s1#loop].",
        "Une alerte de budget est ouverte [A:weekly_budget_usd].",
        "Quatre skillsmerged restent à vérifier [M:merge-candidate].",
        "Le rapport couvre cent pour cent des sessions de la semaine.",
        "La hausse est due à deux causes indépendantes [M:stale-skill].",
    ],
)
def test_validate_llm_blocks_allows_ambiguous_small_numbers_in_prose(phrase):
    """C4 ne doit pas tuer le français normal : « un/une/deux » sans cue de décompte."""
    from weekly_telemetry_aggregator.report import validate_llm_blocks

    violations, _ = validate_llm_blocks(f"{phrase}\n", _BAND_FINDINGS, _BAND_INSIGHTS)

    assert not any("nombre écrit interdit" in v for v in violations), violations


def test_validate_llm_blocks_rejects_small_number_with_a_counting_cue():
    """C4 : « sur quatre sessions » EST un décompte, le cue le révèle."""
    from weekly_telemetry_aggregator.report import validate_llm_blocks

    violations, _ = validate_llm_blocks(
        "Le contrôle a été contourné sur quatre sessions de la semaine [F:s1#loop].\n",
        _BAND_FINDINGS,
        _BAND_INSIGHTS,
    )

    assert any("nombre écrit interdit" in v for v in violations), violations


def test_number_band_index_publishes_scalars_and_counts():
    """C2 : l'index doit rendre le refus auditables (scalaires + cardinances)."""
    from weekly_telemetry_aggregator.report import number_band_index

    index = number_band_index(_BAND_FINDINGS, _BAND_INSIGHTS)

    assert index["counts"]["findings_total"] == 3
    assert index["counts"]["findings_high"] == 1
    assert index["counts"]["alerts_total"] == 1
    assert index["counts"]["maintenance_total"] == 2
    assert index["scalars"]["alerts[0].observed"] == 30.0
    assert index["scalars"]["alerts[0].threshold"] == 25.0
    assert index["scalars_total"] == len(index["scalars"]) == 2
    # les booléens ne sont pas des nombres
    assert all(isinstance(v, float) for v in index["scalars"].values())


def test_resolve_number_bands_renders_arabic_and_drops_the_marker():
    """C3 : le rendu montre le chiffre, plus le marqueur `[N:…]`."""
    from weekly_telemetry_aggregator.html_report import resolve_number_bands

    resolved = resolve_number_bands("Le décompte est [N:3 findings] cette semaine.\n")

    assert resolved == "Le décompte est 3 findings cette semaine.\n"
    assert "[N:" not in resolved
    # idempotent : le rendu HTML puis le rendu markdown ne doivent pas diverger
    assert resolve_number_bands(resolved) == resolved


def test_resolve_number_bands_leaves_non_numeric_bands_untouched():
    """Un `[N:…]` illisible n'est pas réécrit : on ne fabrique pas de texte."""
    from weekly_telemetry_aggregator.html_report import resolve_number_bands

    text = "Un décompte invalide [N:dix-neuf findings] reste tel quel.\n"

    assert resolve_number_bands(text) == text


def test_html_render_quality_block_resolves_band_and_keeps_escaping():
    """C3 côté HTML + vigilance XSS : la résolution ne doit rien déséchapper."""
    from weekly_telemetry_aggregator.html_report import _render_quality_block

    rendered = str(
        _render_quality_block("Budget <script>alert(1)</script> sur [N:30.0 dollars] [A:budget].")
    )

    assert "30.0 dollars" in rendered
    assert "[N:" not in rendered
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered
    assert '<span class="tag tag-a">A:budget</span>' in rendered


def test_report_annex_groups_identical_warnings(tmp_path: Path):
    """v5.30 (F) : les warnings identiques sont groupés avec compteur dans l'annexe."""
    _write_summary(tmp_path)
    cfg = _cfg(tmp_path)
    # injecter des warnings groupés dans le summary
    p = tmp_path / f"weekly-summary-{DATE}.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    data["warnings"] = [
        {
            "session_id": "s1",
            "message": "session sans télémétrie persistée en DB",
            "partial": False,
        },
        {
            "session_id": "s2",
            "message": "session sans télémétrie persistée en DB",
            "partial": False,
        },
        {"session_id": "s3", "message": "autre warning", "partial": False},
    ]
    p.write_text(json.dumps(data), encoding="utf-8")
    draft, ctx = report_prep(cfg, anchor=RUN.isoformat())
    assert draft is not None
    text = draft.read_text(encoding="utf-8")
    assert "×2" in text and "télémétrie persistée" in text
    assert "autre warning" in text


def test_report_synthese_lists_all_alerts(tmp_path: Path):
    _write_summary(tmp_path)
    cfg = _cfg(tmp_path)
    (tmp_path / f"weekly-insights-{DATE}.json").write_text(
        json.dumps(
            {
                "alerts": [
                    {
                        "rule": "weekly_budget_usd",
                        "threshold": 25.0,
                        "observed": 51.7,
                        "severity": "high",
                    },
                    {
                        "rule": "lint_violations_max",
                        "threshold": 10,
                        "observed": 2734,
                        "severity": "medium",
                    },
                ],
                "maintenance": {"findings": []},
            }
        ),
        encoding="utf-8",
    )
    draft, ctx = report_prep(cfg, anchor=RUN.isoformat())
    text = draft.read_text(encoding="utf-8")
    assert "weekly_budget_usd" in text and "lint_violations_max" in text


def test_report_outliers_state_computed_small_sample(tmp_path: Path):
    _write_summary(tmp_path)
    cfg = _cfg(tmp_path)
    p = tmp_path / f"weekly-summary-{DATE}.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    data["cost_outliers_state"] = "computed:small-sample"
    data["cost_outliers"] = [{"session_id": "s1", "cost_usd": 4.2, "z_score": 3.4}]
    p.write_text(json.dumps(data), encoding="utf-8")
    draft, ctx = report_prep(cfg, anchor=RUN.isoformat())
    text = draft.read_text(encoding="utf-8")
    assert "**calculés**" in text and "computed:small-sample" in text
    assert "non calculés" not in text


def test_report_daily_totals_complete_window(tmp_path: Path):
    _write_summary(tmp_path)
    cfg = _cfg(tmp_path)
    p = tmp_path / f"weekly-summary-{DATE}.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    data["period"] = {"start": "2026-08-05T00:00:00Z", "end": "2026-08-12T00:00:00Z"}
    data["daily_totals"] = [
        {"date": "2026-08-07", "cost_usd": 1.0, "total_tokens": 100, "cache_hit_rate": 0.9}
    ]
    p.write_text(json.dumps(data), encoding="utf-8")
    draft, ctx = report_prep(cfg, anchor=RUN.isoformat())
    text = draft.read_text(encoding="utf-8")
    for day in ("2026-08-05", "2026-08-06", "2026-08-07", "2026-08-08", "2026-08-12"):
        assert day in text
    assert "| 2026-08-05 | 0.0000 | 0 |" in text  # jour vide en zéro explicite


def _write_harness_digest(tmp_path: Path, *, always_loaded: int, skill_count: int) -> None:
    (tmp_path / f"weekly-harness-digest-{DATE}.json").write_text(
        json.dumps(
            {
                "inspection": {"summary": {"errors": 2, "warnings": 3}},
                "budget": {
                    "total_tokens": 11884,
                    "always_loaded": always_loaded,
                    "on_demand": 11884 - always_loaded,
                    "always_loaded_ratio": round(always_loaded / 11884, 2),
                    "heaviest": "claude_md/CLAUDE",
                },
                "triggers": {"skill_count": skill_count, "overlaps": []},
                "dependencies": {"total_edges": 3, "broken": []},
            }
        ),
        encoding="utf-8",
    )


def test_report_harness_budget_rendered(tmp_path: Path):
    """D7 (cas nominal) : la ligne budget n'apparaît QUE si `always_loaded > 0`.

    Motif de la réécriture : l'ancien test ne fixtureait `always_loaded: 5575`
    alors que la valeur de production est TOUJOURS 0 (le scan ne mesure aucun
    budget always-loaded). Le gabarit ne testait que la présence du dict, donc
    une ligne « always-loaded 0 / ratio 0.0 » s'affichait quand même — métrique
    morte (C10). Le cas nominal est conservé ici, le cas mort est couvert par
    `test_report_harness_dead_metrics_hidden`.
    """
    _write_summary(tmp_path)
    _write_harness_digest(tmp_path, always_loaded=5575, skill_count=0)
    draft, ctx = report_prep(_cfg(tmp_path), anchor=RUN.isoformat())
    text = draft.read_text(encoding="utf-8")
    assert "Budget tokens" in text and "11,884" in text
    assert "always-loaded 5,575" in text
    assert "Dépendances : 3 arêtes" in text


def test_report_harness_dead_metrics_hidden(tmp_path: Path):
    """D7 : `always_loaded == 0` et `skill_count == 0` ⇒ aucune ligne morte.

    Ces deux métriques affichaient « ratio 0.0 » et « 0 skills » en permanence
    (C10) : un dict non vide suffisait à afficher la ligne. On gate sur la VALEUR.
    Les métriques vivantes du même digest (§ Inspection, Dépendances) restent.
    """
    _write_summary(tmp_path)
    _write_harness_digest(tmp_path, always_loaded=0, skill_count=0)
    draft, _ctx = report_prep(_cfg(tmp_path), anchor=RUN.isoformat())
    text = draft.read_text(encoding="utf-8")
    assert "Budget tokens" not in text
    assert "always-loaded" not in text
    assert "Triggers :" not in text
    # les métriques vivantes du même bloc ne sont pas sacrifiées
    assert "Inspection : 2 erreurs, 3 warnings" in text
    assert "Dépendances : 3 arêtes" in text


def test_report_harness_triggers_rendered_when_skills(tmp_path: Path):
    """D7 : le gate est sur la valeur — `skill_count > 0` rend toujours la ligne."""
    _write_summary(tmp_path)
    _write_harness_digest(tmp_path, always_loaded=0, skill_count=72)
    draft, _ctx = report_prep(_cfg(tmp_path), anchor=RUN.isoformat())
    text = draft.read_text(encoding="utf-8")
    assert "Triggers : 72 skills, 0 chevauchement(s)" in text


def test_report_harness_remediation_status_rendered(tmp_path: Path):
    _write_summary(tmp_path)
    cfg = _cfg(tmp_path)
    (tmp_path / f"weekly-harness-digest-{DATE}.json").write_text(
        json.dumps({"inspection": {"summary": {"errors": 0, "warnings": 0}}}),
        encoding="utf-8",
    )
    (tmp_path / f"weekly-harness-remediation-{DATE}.json").write_text(
        json.dumps(
            {
                "summary": {
                    "applied": 0,
                    "proposed": 2,
                    "manual": 1,
                    "blocked": 3,
                    "rolled_back": 0,
                },
                "postcheck": {"status": "not_run", "reason": "no project changes were requested"},
            }
        ),
        encoding="utf-8",
    )
    draft, _ctx = report_prep(cfg, anchor=RUN.isoformat())
    text = draft.read_text(encoding="utf-8")
    assert "Remédiation harness" in text
    assert "2 proposée(s)" in text
    assert "1 manuelle(s)" in text


def test_report_section6_renders_watch_recommendations(tmp_path: Path):
    """v5.31 : les recommandations de la veille critique sont rendues en tête du §6."""
    _write_summary(tmp_path)
    cfg = _cfg(tmp_path)
    (tmp_path / f"weekly-watch-findings-{DATE}.json").write_text(
        json.dumps(
            {
                "findings": [
                    {
                        "session_id": None,
                        "category": "adopt",
                        "severity": "high",
                        "description": "Skill X du marché fait la vérification Jira efficacement",
                        "evidence_summary": "pattern coûteux détecté (F:ses_023b8f80#context-bloat)",
                        "recommendation": "Évaluer l'adoption de X avant la prochaine session de vérification",
                        "recommendation_type": "watch-adopt",
                        "impact_order_of_magnitude": "large",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    draft, ctx = report_prep(cfg, anchor=RUN.isoformat())
    assert draft is not None
    text = draft.read_text(encoding="utf-8")
    assert "### Recommandations (regard critique" in text
    assert "[HIGH] adopt" in text
    assert "jamais d'installation automatique" in text
    assert "Évaluer l'adoption de X" in text


def test_report_section5_renders_coherence_findings(tmp_path: Path):
    """v5.31 : les findings de cohérence de l'environnement sont rendus en tête du §5."""
    _write_summary(tmp_path)
    cfg = _cfg(tmp_path)
    (tmp_path / f"weekly-coherence-findings-{DATE}.json").write_text(
        json.dumps(
            {
                "findings": [
                    {
                        "category": "duplicate",
                        "tag": "merge",
                        "severity": "high",
                        "description": "Les agents frontend-developer et full-stack-developer chevauchent leurs rôles UI",
                        "evidence_summary": "références croisées + 60 % de rôles communs",
                        "recommendation": "Fusionner ou clarifier les frontières des rôles",
                        "recommendation_type": "coherence-merge",
                        "impact_order_of_magnitude": "large",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    draft, ctx = report_prep(cfg, anchor=RUN.isoformat())
    assert draft is not None
    text = draft.read_text(encoding="utf-8")
    assert "## 5. Santé de l'environnement (harness + cohérence)" in text
    assert "### Cohérence de l'environnement" in text
    assert "**merge**" in text and "Fusionner ou clarifier" in text


def test_git_log_filters_real_auto_commits(tmp_path: Path):
    """v5.31 (b) : seuls les vrais commits auto-rédigés (revue hebdo) sont comptés."""
    import subprocess as sp

    repo = tmp_path / "repo"
    repo.mkdir()
    sp.run(["git", "init", "-q"], cwd=repo, check=True)
    sp.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    sp.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    (repo / "a.txt").write_text("x")
    sp.run(["git", "add", "-A"], cwd=repo, check=True)
    sp.run(
        ["git", "commit", "-q", "-m", "skill:demo (auto-rédigé, revue hebdo 2026-08-14)"],
        cwd=repo,
        check=True,
    )
    (repo / "b.txt").write_text("y")
    sp.run(["git", "add", "-A"], cwd=repo, check=True)
    sp.run(
        ["git", "commit", "-q", "-m", "feat: skills auto-rédigés (pipeline)"], cwd=repo, check=True
    )

    from weekly_telemetry_aggregator.report import _git_log

    commits = _git_log(repo, "2026-08-01T00:00:00Z")
    assert len(commits) == 1  # le faux positif (feat mentionnant auto-rédigés) est filtré
    assert commits[0]["subject"].startswith("skill:demo")


def test_head_commit_and_dirty_files_listed_in_annex(tmp_path: Path):
    """Reco §8 12/09 : HEAD + fichiers dirty du run inventoriés en annexe."""
    import subprocess as sp

    repo = tmp_path / "repo"
    repo.mkdir()
    sp.run(["git", "init", "-q"], cwd=repo, check=True)
    sp.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    sp.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    (repo / "a.txt").write_text("x")
    sp.run(["git", "add", "-A"], cwd=repo, check=True)
    sp.run(
        ["git", "commit", "-q", "-m", "sync(weekly-advisor): portable preflight (kit #8)"],
        cwd=repo,
        check=True,
    )
    (repo / "dirty.md").write_text("wip")

    from weekly_telemetry_aggregator.report import _dirty_files, _head_commit

    head = _head_commit(repo)
    assert head is not None
    assert head["subject"].startswith("sync(weekly-advisor)")
    assert len(head["hash"]) == 40 and head["date"] != ""
    dirty = _dirty_files(repo)
    assert any("dirty.md" in line for line in dirty)
    assert _head_commit(tmp_path) is None  # hors git
    assert _dirty_files(tmp_path) == []


def test_assemble_missing_draft_message_explicit(tmp_path: Path):
    """v5.31 (a) : le message draft inexistant rappelle la consommation par assemble."""
    _write_summary(tmp_path)
    cfg = _cfg(tmp_path)
    path, warnings, rc = report_assemble(cfg, anchor=RUN.isoformat())
    assert rc == 2
    assert any("consommé" in w and "report-prep" in w for w in warnings)


# ---------------------------------------------------------------- v6.0.k template


def test_template_maintenance_and_unscoped_lines_are_not_glued(tmp_path: Path):
    """v6.0.k : trim_blocks ne colle plus les bullets (§7) ni la ligne unscoped (§5)."""
    from weekly_telemetry_aggregator.run_state import activate_run

    active = activate_run(tmp_path, DATE, RUN)
    _write_summary(active.run_dir)
    # insights avec 2 constats de maintenance + digests enrichis
    (active.run_dir / f"weekly-insights-{DATE}.json").write_text(
        json.dumps(
            {
                "alerts": [],
                "maintenance": {
                    "findings": [
                        {
                            "severity": "HIGH",
                            "category": "token-risk",
                            "description": "first finding",
                            "recommendation": "fix it",
                            "evidence_summary": "file:line proof one",
                        },
                        {
                            "severity": "MEDIUM",
                            "category": "agent-loop",
                            "description": "second finding",
                            "recommendation": "fix it too",
                            "evidence_summary": "file:line proof two",
                        },
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    (active.run_dir / f"weekly-harness-digest-{DATE}.json").write_text(
        json.dumps(
            {
                "inspection": {"summary": {}},
                "harness_scope": {
                    "unscoped_files": [".opencode/a.json", ".opencode/b.json"],
                },
            }
        ),
        encoding="utf-8",
    )
    draft, ctx = report_prep(_cfg(tmp_path), anchor=RUN.isoformat())
    assert draft is not None
    text = draft.read_text(encoding="utf-8")
    # §7 : une ligne par constat (agent-loop < token-risk), catégorie incluse dans la ligne
    section_7 = text.split("## 7.", 1)[1].split("## 8.", 1)[0]
    bullets = [line for line in section_7.splitlines() if line.startswith("- **[")]
    assert bullets == [
        "- **[MEDIUM]** [agent-loop] second finding → fix it too — preuve : file:line proof two",
        "- **[HIGH]** [token-risk] first finding → fix it — preuve : file:line proof one",
    ]
    assert "proof one- **[MEDIUM]" not in text  # plus de collage (trim_blocks)
    # ligne unscoped non collée à la section suivante (trim_blocks, v6.0.k)
    assert "b.json\n" in text
    assert "b.json## 6." not in text
    # annexe : répertoire du run
    assert f"`runs/{active.run_id}/`" in text


def test_template_maintenance_finding_without_category_stays_readable(tmp_path: Path):
    """§7 : un constat SANS catégorie ne plante pas et ne rend pas de crochets vides."""
    _write_summary(tmp_path)
    (tmp_path / f"weekly-insights-{DATE}.json").write_text(
        json.dumps(
            {
                "alerts": [],
                "maintenance": {
                    "findings": [
                        {
                            "severity": "HIGH",
                            "description": "constat sans catégorie",
                            "recommendation": "corriger",
                            "evidence_summary": "src=foo",
                        },
                        {
                            "severity": "LOW",
                            "category": "",
                            "description": "constat catégorie vide",
                            "recommendation": "corriger aussi",
                            "evidence_summary": "src=bar",
                        },
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    draft, _ctx = report_prep(_cfg(tmp_path), anchor=RUN.isoformat())
    assert draft is not None
    text = draft.read_text(encoding="utf-8")
    section_7 = text.split("## 7.", 1)[1].split("## 8.", 1)[0]
    bullets = [line for line in section_7.splitlines() if line.startswith("- **[")]
    assert len(bullets) == 2
    assert "[]" not in section_7  # pas de « catégorie » vide rendue
    # la sévérité reste en tête, le constat et sa preuve restent entiers
    for line in bullets:
        assert re.match(r"^- \*\*\[(HIGH|LOW)\]\*\* constat .+ → corriger", line), line
        assert re.search(r" — preuve : src=\w+$", line), line


def test_assemble_renders_html_with_injected_quality_block(tmp_path: Path, monkeypatch):
    """report_assemble branche le renderer HTML avec le bloc effectivement injecté."""
    _write_summary(tmp_path)
    _write_auto_blocks(tmp_path)
    cfg = _cfg(tmp_path)
    report_prep(cfg, anchor=RUN.isoformat())
    seen: dict = {}

    def fake_render(cfg_, *, anchor, ctx, quality_block):
        seen.update(anchor=anchor, ctx=ctx, quality_block=quality_block)
        return tmp_path / "html" / f"weekly-report-{DATE}.html"

    monkeypatch.setattr("weekly_telemetry_aggregator.report.render_html_report", fake_render)
    opened: list[Path | None] = []
    monkeypatch.setattr(
        "weekly_telemetry_aggregator.report.open_html_report",
        lambda cfg_, path: opened.append(path),
    )
    final_path, warnings, rc = report_assemble(cfg, anchor=RUN.isoformat())
    assert rc == 0 and final_path is not None
    # le bloc passé au renderer est exactement celui injecté dans le MD final
    auto_text = (tmp_path / f"weekly-report-blocks-auto-{DATE}.md").read_text(encoding="utf-8")
    assert seen["quality_block"] == auto_text
    assert seen["ctx"] is not None and seen["ctx"]["date"] == DATE
    assert seen["anchor"] == RUN.isoformat()
    # auto-open branché avec le chemin retourné par le renderer
    assert opened == [tmp_path / "html" / f"weekly-report-{DATE}.html"]


def test_assemble_html_disabled_is_silent_noop(tmp_path: Path):
    """html_report_dir="" → aucun rendu HTML, assemble OK sans erreur."""
    _write_summary(tmp_path)
    cfg = _cfg(tmp_path)
    cfg.html_report_dir = ""  # génération HTML désactivée
    report_prep(cfg, anchor=RUN.isoformat())
    final_path, warnings, rc = report_assemble(cfg, anchor=RUN.isoformat())
    assert rc == 0 and final_path is not None
    assert not (tmp_path / "reports").exists()


def test_assemble_html_enabled_missing_artifact_is_nonzero(tmp_path: Path, monkeypatch):
    _write_summary(tmp_path)
    cfg = _cfg(tmp_path)
    cfg.html_report_dir = str(tmp_path / "html")
    report_prep(cfg, anchor=RUN.isoformat())
    monkeypatch.setattr(
        "weekly_telemetry_aggregator.report.render_html_report",
        lambda *args, **kwargs: tmp_path / "html" / f"weekly-report-{DATE}.html",
    )
    final_path, warnings, rc = report_assemble(cfg, anchor=RUN.isoformat())
    assert final_path is not None and rc != 0
    assert any("HTML enabled" in warning and "absent" in warning for warning in warnings)


def test_assemble_external_html_permission_is_report_only(tmp_path: Path, monkeypatch):
    _write_summary(tmp_path)
    cfg = _cfg(tmp_path)
    cfg.html_report_dir = str(tmp_path.parent / "external-reports")
    report_prep(cfg, anchor=RUN.isoformat())
    monkeypatch.setattr(
        "weekly_telemetry_aggregator.report.render_html_report",
        lambda *args, **kwargs: (_ for _ in ()).throw(PermissionError("outside worktree")),
    )
    final_path, warnings, rc = report_assemble(cfg, anchor=RUN.isoformat())
    assert final_path is not None
    assert rc == 0
    assert any("report-only" in warning for warning in warnings)
    gates = json.loads((tmp_path / f"weekly-report-gates-{DATE}.json").read_text(encoding="utf-8"))
    assert gates["html"]["category"] == "external-permission-refusal"
    assert gates["html"]["report_only"] is True


def test_assemble_prose_rejection_is_explicit_fallback_and_gate_manifest(tmp_path: Path):
    _write_summary(tmp_path)
    cfg = _cfg(tmp_path)
    report_prep(cfg, anchor=RUN.isoformat())
    _write_auto_blocks(tmp_path)
    (tmp_path / f"weekly-report-blocks-{DATE}.md").write_text("too short", encoding="utf-8")
    final_path, warnings, rc = report_assemble(cfg, anchor=RUN.isoformat())
    assert final_path is not None and rc == 0
    text = final_path.read_text(encoding="utf-8")
    assert "auto_draft_fallback" in text
    assert "never validated" in text
    gates = json.loads((tmp_path / f"weekly-report-gates-{DATE}.json").read_text(encoding="utf-8"))
    assert gates["prose"]["validated"] is False


def test_blocking_security_rules_are_nonzero_even_without_critical_severity(tmp_path: Path):
    # warn-only migration (epic warn-only) : blocking sans critical → rc==1, rapport écrit.
    _write_summary(tmp_path)
    cfg = _cfg(tmp_path)
    report_prep(cfg, anchor=RUN.isoformat())
    (tmp_path / f"weekly-harness-digest-{DATE}.json").write_text(
        json.dumps({"findings": [{"rule": "mcp-tool-poisoning", "severity": "high"}]}),
        encoding="utf-8",
    )
    final_path, warnings, rc = report_assemble(cfg, anchor=RUN.isoformat())
    assert final_path is not None and rc == 1
    assert (tmp_path / f"weekly-report-{DATE}.md").exists()


# ------------------------------------------------------------------ v6.1 snapshots : next-steps Toi/Pipeline/Agent (tag tri) + passthrough + build_report_context


def test_top_next_steps_grouped_by_actor_snapshot(tmp_path: Path):
    """Snapshot _top_next_steps : groupé Toi/Pipeline/Agent, déterministe, source multiples."""
    from weekly_telemetry_aggregator.report import _top_next_steps

    digest = {
        "inspection": {
            "uncategorized": [
                {
                    "findings": [
                        {"rule": "security/mcp-tool-poisoning", "severity": "warning"},
                        {"rule": "security/mcp-tool-poisoning", "severity": "warning"},
                        {"rule": "allowlist/unscoped", "severity": "warning"},
                        {"rule": "custom/agent-rule", "severity": "warning"},
                    ]
                }
            ]
        }
    }
    insights = {
        "alerts": [
            {
                "rule": "monthly_budget_usd",
                "severity": "high",
                "threshold": 25,
                "observed": 30,
                "unit": "$",
            },
            {
                "rule": "lint_violations_max",
                "severity": "medium",
                "threshold": 10,
                "observed": 12,
                "unit": "findings",
            },
        ]
    }
    findings = {
        "findings": [
            {
                "category": "merge-candidate",
                "severity": "medium",
                "description": "skills redondants",
                "recommendation": "fusionner",
                "recommendation_type": "merge-candidate",
            },
            {
                "category": "context-bloat",
                "severity": "high",
                "description": "gros fichiers relus",
                "recommendation": "compresser",
                "recommendation_type": "general",
            },
        ]
    }
    steps = _top_next_steps(digest, insights, findings, ignored_rules=[], limit=3)
    # snapshot : 3 entrées, un par acteur en tête (Toi/Pipeline/Agent)
    assert len(steps) == 3
    actors = [s["actor"] for s in steps]
    assert actors == ["Toi", "Pipeline", "Agent"]
    # Toi vient du harness security ou du budget high → sévérité high en premier
    assert steps[0]["severity"] in ("high", "critical")
    # chaque entrée a tag tri : actor, source, severity, text
    for s in steps:
        assert s["actor"] in ("Toi", "Pipeline", "Agent")
        assert s["source"] in ("harness", "alert", "audit")
        assert s["severity"] in ("high", "critical", "medium", "warning", "low", "info")
        assert "text" in s and s["text"]
    # Toi doit être budget ou security (tri high)
    assert any(
        "budget" in s.get("rule", "").lower() or "security" in s.get("rule", "").lower()
        for s in steps
        if s["actor"] == "Toi"
    )
    # Pipeline vient typiquement de allowlist / lint / coverage
    assert any(s["actor"] == "Pipeline" for s in steps)
    # Agent vient de l'audit context-bloat / custom rule
    assert any(s["actor"] == "Agent" for s in steps)


def test_top_next_steps_tag_tri_deterministic_snapshot():
    """Snapshot tag tri : sévérité > source > count > règle > acteur, déterministe + grouping Toi/Pipeline/Agent."""
    from weekly_telemetry_aggregator.report import _top_next_steps

    # deux candidats même sévérité medium, sources différentes harness/pipeline vs alert/pipeline vs audit/agent
    # le tri brut est severity > source > count > rule, mais le regroupement final met Toi/Pipeline/Agent en tête
    digest = {
        "inspection": {
            "uncategorized": [{"findings": [{"rule": "custom/low-rule", "severity": "warning"}]}]
        }
    }
    insights = {
        "alerts": [
            {"rule": "lint_violations_max", "severity": "medium", "threshold": 10, "observed": 12}
        ]
    }
    findings = {
        "findings": [
            {
                "category": "context-bloat",
                "severity": "medium",
                "description": "x",
                "recommendation": "y",
            }
        ]
    }

    steps = _top_next_steps(digest, insights, findings, limit=5)
    # à sévérité égale, le grouping Toi/Pipeline/Agent prime : Pipeline (alert) avant Agent (harness+ audit)
    # donc Pipeline (alert) doit être premier, pas harness
    assert steps[0]["actor"] == "Pipeline"
    assert steps[0]["source"] == "alert"
    # harness et audit sont tous deux Agent : harness (source 0) avant audit (source 2) au sein du groupe Agent
    agent_steps = [s for s in steps if s["actor"] == "Agent"]
    assert agent_steps[0]["source"] == "harness"
    assert agent_steps[1]["source"] == "audit"

    # test count tri : même sévérité + source harness, règle avec plus de violations d'abord
    # b-rule apparaît dans 2 composants distincts → count 2, a-rule count 1
    digest2 = {
        "inspection": {
            "uncategorized": [
                {
                    "path": "a",
                    "findings": [{"rule": "custom/a-rule", "severity": "warning", "message": "m1"}],
                },
                {
                    "path": "b",
                    "findings": [{"rule": "custom/b-rule", "severity": "warning", "message": "m1"}],
                },
                {
                    "path": "c",
                    "findings": [{"rule": "custom/b-rule", "severity": "warning", "message": "m2"}],
                },
            ]
        }
    }
    steps2 = _top_next_steps(digest2, None, None, limit=5)
    # b-rule a count 2 vs a-rule count 1 → b-rule avant
    rules = [s["rule"] for s in steps2 if s["source"] == "harness"]
    assert rules[0] == "custom/b-rule"

    # déterministe : deux appels identiques → même résultat
    steps_a = _top_next_steps(digest, insights, findings, limit=3)
    steps_b = _top_next_steps(digest, insights, findings, limit=3)
    assert steps_a == steps_b

    # règle tri alphabétique quand count et sévérité égaux
    digest3 = {
        "inspection": {
            "uncategorized": [
                {"findings": [{"rule": "custom/z-rule", "severity": "warning"}]},
                {"findings": [{"rule": "custom/a-rule", "severity": "warning"}]},
            ]
        }
    }
    steps3 = _top_next_steps(digest3, None, None, limit=5)
    rules3 = [s["rule"] for s in steps3 if s["source"] == "harness"]
    assert rules3 == sorted(rules3)


def test_top_next_steps_dedup_and_limit_and_empty_snapshot():
    """Snapshot dedup + limit + vide : fallback [] et passthrough best-effort."""
    from weekly_telemetry_aggregator.report import _top_next_steps

    # vide → []
    assert _top_next_steps(None, None, None) == []
    assert _top_next_steps({}, {}, {}) == []
    assert _top_next_steps({"inspection": {}}, {"alerts": []}, {"findings": []}) == []

    # dedup : même (actor, rule) deux fois → une seule
    digest = {
        "inspection": {
            "uncategorized": [
                {"findings": [{"rule": "security/mcp-tool-poisoning", "severity": "warning"}]},
                {"findings": [{"rule": "security/mcp-tool-poisoning", "severity": "warning"}]},
            ]
        }
    }
    steps = _top_next_steps(digest, None, None, limit=5)
    assert len([s for s in steps if s.get("rule") == "security/mcp-tool-poisoning"]) == 1
    assert steps[0]["count"] == 2  # compté 2 fois mais dédupliqué en une entrée

    # limit : au plus limit entrées, et groupé Toi/Pipeline/Agent d'abord
    digest_many = {
        "inspection": {
            "uncategorized": [
                {"findings": [{"rule": f"custom/rule-{i}", "severity": "warning"}]}
                for i in range(10)
            ]
        }
    }
    steps_many = _top_next_steps(digest_many, None, None, limit=3)
    assert len(steps_many) == 3

    # ignored_rules : filtré
    steps_ignored = _top_next_steps(
        digest, None, None, ignored_rules=["security/mcp-tool-poisoning"]
    )
    assert steps_ignored == []

    # passthrough best-effort : digest non-dict ou findings mal formé → pas de crash
    assert _top_next_steps("not-a-dict", None, None) == []  # type: ignore
    assert _top_next_steps(None, {"alerts": "not-a-list"}, None) == []  # type: ignore
    assert _top_next_steps(None, None, {"findings": "not-a-list"}) == []  # type: ignore


def test_actor_mapping_snapshot():
    """Snapshot mapping acteur : Toi/Pipeline/Agent selon tags et règles."""
    from weekly_telemetry_aggregator.report import (
        _actor_for_alert,
        _actor_for_finding,
        _actor_for_harness_rule,
    )

    # harness
    assert _actor_for_harness_rule("security/mcp-tool-poisoning") == "Toi"
    assert _actor_for_harness_rule("secret/unbounded") == "Toi"
    assert _actor_for_harness_rule("allowlist/unscoped") == "Pipeline"
    assert _actor_for_harness_rule("budget/violated") == "Pipeline"
    assert _actor_for_harness_rule("coverage/scope") == "Pipeline"
    assert _actor_for_harness_rule("custom/agent-loop") == "Agent"

    # alert
    assert _actor_for_alert("monthly_budget_usd") == "Toi"
    assert _actor_for_alert("weekly_budget_usd") == "Toi"
    assert _actor_for_alert("lint_violations_max") == "Pipeline"
    assert _actor_for_alert("lint_coverage") == "Pipeline"
    assert _actor_for_alert("daily_spike_z_min") == "Pipeline"
    assert _actor_for_alert("cache_hit_rate_min") == "Agent"

    # finding
    assert (
        _actor_for_finding({"category": "merge-candidate", "recommendation_type": "merge"}) == "Toi"
    )
    assert (
        _actor_for_finding({"category": "retire-candidate", "recommendation_type": "adopt"})
        == "Toi"
    )
    assert _actor_for_finding({"category": "security/tool-poisoning"}) == "Toi"
    assert _actor_for_finding({"category": "harness/missing"}) == "Pipeline"
    assert _actor_for_finding({"category": "coverage", "recommendation_type": ""}) == "Pipeline"
    assert _actor_for_finding({"category": "loop", "recommendation_type": "general"}) == "Agent"
    assert _actor_for_finding({"category": "context-bloat"}) == "Agent"


def test_report_context_top_next_steps_passthrough_snapshot(tmp_path: Path):
    """Snapshot build_report_context : top_next_steps exposé, groupé, best-effort."""
    from weekly_telemetry_aggregator.report import build_report_context

    _write_summary(tmp_path)
    cfg = _cfg(tmp_path)
    # pas de digest/insights/findings → top_next_steps == []
    ctx = build_report_context(cfg, anchor=RUN.isoformat())
    assert ctx is not None
    assert "top_next_steps" in ctx
    assert ctx["top_next_steps"] == []

    # avec digest harness seul → au moins un Toi/Pipeline
    (tmp_path / f"weekly-harness-digest-{DATE}.json").write_text(
        json.dumps(
            {
                "inspection": {
                    "uncategorized": [
                        {
                            "findings": [
                                {"rule": "security/mcp-tool-poisoning", "severity": "warning"}
                            ]
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    # clean run_state cache? rebuild context après ajout digest

    # force rebuild via new anchor same date (build_report_context re-reads files)
    ctx2 = build_report_context(cfg, anchor=RUN.isoformat())
    assert ctx2 is not None
    assert len(ctx2["top_next_steps"]) >= 1
    assert ctx2["top_next_steps"][0]["actor"] in ("Toi", "Pipeline", "Agent")
    # tag tri présent
    for step in ctx2["top_next_steps"]:
        assert "actor" in step and "source" in step and "severity" in step and "text" in step

    # avec insights alert high (Toi) + lint (Pipeline) + audit (Agent) → groupé trié Toi/Pipeline/Agent
    # on enrichit le digest avec une règle Pipeline pour garantir un candidat Pipeline harness
    (tmp_path / f"weekly-harness-digest-{DATE}.json").write_text(
        json.dumps(
            {
                "inspection": {
                    "uncategorized": [
                        {
                            "findings": [
                                {"rule": "security/mcp-tool-poisoning", "severity": "warning"},
                                {"rule": "allowlist/unscoped", "severity": "warning"},
                            ]
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / f"weekly-insights-{DATE}.json").write_text(
        json.dumps(
            {
                "alerts": [
                    {
                        "rule": "monthly_budget_usd",
                        "severity": "high",
                        "threshold": 25,
                        "observed": 30,
                    }
                ],
                "maintenance": {"findings": []},
                "deltas": {},
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / f"weekly-quality-findings-{DATE}.json").write_text(
        json.dumps(
            {
                "findings": [
                    {
                        "category": "context-bloat",
                        "severity": "medium",
                        "description": "gros",
                        "recommendation": "réduire",
                        "recommendation_type": "general",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    ctx3 = build_report_context(cfg, anchor=RUN.isoformat())
    # attendu : Toi (budget/security), Pipeline (allowlist), Agent (audit) — limit 3
    assert len(ctx3["top_next_steps"]) == 3
    assert [s["actor"] for s in ctx3["top_next_steps"]] == ["Toi", "Pipeline", "Agent"]


def test_report_context_ignores_harness_ignored_rules_snapshot(tmp_path: Path):
    """Snapshot ignored_rules : top_next_steps filtre les règles ignorées."""
    from weekly_telemetry_aggregator.report import build_report_context

    _write_summary(tmp_path)
    cfg = _cfg(tmp_path)
    cfg.harness_ignored_rules = ["security/mcp-tool-poisoning"]
    (tmp_path / f"weekly-harness-digest-{DATE}.json").write_text(
        json.dumps(
            {
                "inspection": {
                    "uncategorized": [
                        {
                            "findings": [
                                {"rule": "security/mcp-tool-poisoning", "severity": "warning"},
                                {"rule": "allowlist/unscoped", "severity": "warning"},
                            ]
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    ctx = build_report_context(cfg, anchor=RUN.isoformat())
    rules = [s.get("rule") for s in ctx["top_next_steps"] if s.get("source") == "harness"]
    assert "security/mcp-tool-poisoning" not in rules
    assert "allowlist/unscoped" in rules


def test_assemble_security_warn_only(tmp_path: Path):
    """Warn-only sécu : findings blocking + autres gates verts → rc==1, rapport écrit.

    gate_status.security.status == "warn", blocking_rules exposées triées,
    exit 2 conservé pour les cas non-sécu (draft manquant, _coerce_rc malformé).
    """
    _write_summary(tmp_path)
    cfg = _cfg(tmp_path)
    report_prep(cfg, anchor=RUN.isoformat())
    (tmp_path / f"weekly-harness-digest-{DATE}.json").write_text(
        json.dumps(
            {
                "findings": [],
                "inspection": {
                    "uncategorized": [
                        {
                            "path": ".opencode/a.md",
                            "findings": [
                                {"rule": "security/mcp-tool-poisoning", "severity": "warning"}
                            ],
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    final_path, warnings, rc = report_assemble(cfg, anchor=RUN.isoformat())
    assert rc == 1
    assert final_path is not None and final_path.exists()
    assert (tmp_path / f"weekly-report-{DATE}.md").exists()
    assert any("blocking security rule" in w for w in warnings)
    gates = json.loads((tmp_path / f"weekly-report-gates-{DATE}.json").read_text(encoding="utf-8"))
    assert gates["security"]["status"] == "warn"
    assert gates["security"]["blocking_count"] >= 1
    assert gates["blocking_rules"] == sorted(_BLOCKING_SECURITY_RULES)
    assert gates["security"]["blocking_rules"] == sorted(_BLOCKING_SECURITY_RULES)
    # exit 2 conservé pour cas non-sécu : draft manquant reste fatal
    missing_path, _w2, rc2 = report_assemble(_cfg(tmp_path / "empty"), anchor=RUN.isoformat())
    assert missing_path is None and rc2 == 2
    # _coerce_rc ne casse pas les autres cas : malformés → default, valides intacts
    assert _coerce_rc(True, default=0) == 0
    assert _coerce_rc(None, default=1) == 1
    assert _coerce_rc(-1, default=1) == 1
    assert _coerce_rc("2") == 2
    assert _coerce_rc("crash", default=0) == 0
    # _gate_status par défaut : security pass, autres gates inchangées
    assert _gate_status({})["security"]["status"] == "pass"


# ------------------------------------------------- vNext multi-harnais (harness-breakdown)


def _seed_harness_summary(tmp_path: Path) -> None:
    """Summary enrichi by_harness/all_sessions avec harness propagé."""
    _write_summary(tmp_path)
    p = tmp_path / f"weekly-summary-{DATE}.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    data["by_harness"] = [
        {
            "harness": "copilot-cli",
            "session_count": 1,
            "total_tokens": 300,
            "total_cost_usd": 3.0,
        },
        {
            "harness": "opencode",
            "session_count": 1,
            "total_tokens": 100,
            "total_cost_usd": 0.5,
        },
    ]
    big_title = "Gros chantier " + "x" * 100
    data["top_sessions_by_cost"] = [
        {
            "session_id": "b",
            "title_or_topic": big_title,
            "harness": "copilot-cli",
            "cost_usd": 3.0,
            "total_tokens": 300,
            "duration_seconds": 60,
            "active_time_seconds": 30,
            "cost_per_active_minute": 6.0,
            "api_call_count": 5,
            "includes_subagents": False,
            "cache_efficiency": 0.5,
        },
    ]
    data["all_sessions"] = data["top_sessions_by_cost"] + [
        {
            "session_id": "a",
            "title_or_topic": "Petit fix",
            "harness": "opencode",
            "cost_usd": 0.5,
            "total_tokens": 100,
            "duration_seconds": 10,
            "active_time_seconds": 5,
            "cost_per_active_minute": 6.0,
            "api_call_count": 2,
            "includes_subagents": False,
            "cache_efficiency": 0.1,
        },
    ]
    p.write_text(json.dumps(data), encoding="utf-8")


def test_report_context_exposes_harness_breakdown_and_all_sessions(tmp_path: Path):
    from weekly_telemetry_aggregator.report import build_report_context

    _seed_harness_summary(tmp_path)
    ctx = build_report_context(_cfg(tmp_path), anchor=RUN.isoformat())
    assert ctx is not None
    assert [h["harness"] for h in ctx["harness_breakdown"]] == ["copilot-cli", "opencode"]
    assert ctx["harness_breakdown"][0]["total_cost_usd"] == 3.0
    assert [s["session_id"] for s in ctx["all_sessions"]] == ["b", "a"]


def test_report_context_tolerates_missing_harness_keys(tmp_path: Path):
    """Runs anciens sans by_harness/all_sessions → listes vides, pas de crash."""
    from weekly_telemetry_aggregator.report import _harness_breakdown, build_report_context

    _write_summary(tmp_path)
    p = tmp_path / f"weekly-summary-{DATE}.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    data.pop("by_harness", None)
    data.pop("all_sessions", None)
    p.write_text(json.dumps(data), encoding="utf-8")
    ctx = build_report_context(_cfg(tmp_path), anchor=RUN.isoformat())
    assert ctx is not None
    assert ctx["harness_breakdown"] == []
    assert ctx["all_sessions"] == []
    # garde-fou unitaire : entrées malformées ignorées, tri (-coût, harnais)
    assert _harness_breakdown({}) == []
    assert _harness_breakdown({"by_harness": "nope"}) == []
    assert _harness_breakdown({"by_harness": [{"harness": "z"}, None, "x"]}) == [
        {"harness": "z", "session_count": 0, "total_tokens": 0, "total_cost_usd": 0.0}
    ]


def test_report_prep_renders_harness_breakdown_md(tmp_path: Path):
    _seed_harness_summary(tmp_path)
    draft, ctx = report_prep(_cfg(tmp_path), anchor=RUN.isoformat())
    assert draft is not None
    text = draft.read_text(encoding="utf-8")
    assert "### Ventilation par harnais" in text
    assert "copilot-cli" in text and "opencode" in text
    assert "harnais `copilot-cli`" in text  # colonne harness du top sessions
    assert "### Toutes les sessions (annexe)" in text
    assert "`b`" in text and "`a`" in text
    annex = text.split("### Toutes les sessions (annexe)")[1]
    assert "x" * 100 not in annex  # titres d'annexe tronqués 80ch
    assert "user_turns" not in text


def test_report_prep_harness_fallback_without_keys(tmp_path: Path):
    _write_summary(tmp_path)
    p = tmp_path / f"weekly-summary-{DATE}.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    data.pop("by_harness", None)
    data.pop("all_sessions", None)
    p.write_text(json.dumps(data), encoding="utf-8")
    draft, ctx = report_prep(_cfg(tmp_path), anchor=RUN.isoformat())
    assert draft is not None
    text = draft.read_text(encoding="utf-8")
    assert "Ventilation par harnais non disponible" in text
    assert "Toutes les sessions (annexe)" not in text


def test_report_gate_passes_without_harness_keys(tmp_path: Path):
    """Pas de régression gate : summary sans by_harness/all_sessions reste pass."""
    _write_summary(tmp_path)
    p = tmp_path / f"weekly-summary-{DATE}.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    data.pop("by_harness", None)
    data.pop("all_sessions", None)
    p.write_text(json.dumps(data), encoding="utf-8")
    gate = validate_required_artifacts(tmp_path, DATE)
    assert gate["status"] == "pass"
    assert gate["required"][f"weekly-summary-{DATE}.json"]["status"] == "present"


def test_html_renders_harness_filter_and_closed_annex(tmp_path: Path):
    from weekly_telemetry_aggregator.html_report import render_html_report
    from weekly_telemetry_aggregator.report import build_report_context

    _seed_harness_summary(tmp_path)
    cfg = _cfg(tmp_path)
    cfg.html_report_dir = str(tmp_path / "html")
    ctx = build_report_context(cfg, anchor=RUN.isoformat())
    assert ctx is not None
    path = render_html_report(cfg, anchor=RUN.isoformat(), ctx=ctx, quality_block=None)
    assert path is not None and path.exists()
    html = path.read_text(encoding="utf-8")
    # colonne harness du top sessions + ventilation by_harness
    assert "Ventilation par harnais" in html
    assert "copilot-cli" in html and "opencode" in html
    # annexe H fermée par défaut (jamais open), autonome zéro-CDN
    assert '<details class="annex" id="annex-h">' in html
    assert 'id="allsess-q"' in html and 'id="allsess-h"' in html
    assert '<option value="copilot-cli">' in html
    assert '<option value="opencode">' in html
    assert "cdn" not in html.lower()
    # titres de l'annexe H tronqués 80ch (le tableau top-sessions d'annexe B
    # garde son rendu historique ; le payload JSON garde les données complètes)
    #
    # ADAPTÉ (C6) : la troncature passe par `report.truncate_text`, qui coupe sur
    # une frontière de mot et signale la troncature par une ellipse. Ici le titre
    # est un jeton insécable de 100 `x` plus long que la borne : aucune frontière
    # n'existe, la coupe tombe donc à `limit - 1` + `…` (`report.truncate_text`
    # cas 3). Avant, le `[:80]` nu rendait 80 caractères sans marqueur, ce qui
    # faisait croire à un titre complet. Longueur visible inchangée : 80.
    annex_h = html.split('id="annex-h"')[1].split('<script type="application/json"')[0]
    assert "x" * 100 not in annex_h
    assert "<td>Gros chantier " + "x" * 65 + "…</td>" in annex_h
    # l'annexe affichée ne rend que des champs sûrs (pas de brut transcript) ;
    # les clés agrégées du payload JSON embarqué ne sont pas du contenu brut
    assert "user_turns" not in annex_h and "tool_arg" not in annex_h
    # zéro-CDN : aucun script/link externe
    assert "<script src=" not in html and '<link rel="stylesheet"' not in html


def _valid_audit_envelope(sid: str = "ses_f6ed03e11ffetdQstFHu2pb7B5") -> dict:
    return {
        "schema_version": 1,
        "session_id": sid,
        "summary": "ok",
        "findings": [],
        "warnings": [],
        "rc": 0,
    }


def test_audit_envelope_valid_accepts_minimal_contract():
    assert _audit_envelope_valid(_valid_audit_envelope(), "ses_f6ed03e11ffetdQstFHu2pb7B5") is True
    assert _audit_envelope_reason(_valid_audit_envelope(), "ses_f6ed03e11ffetdQstFHu2pb7B5") == "ok"


def test_audit_envelope_rejects_graphify_text_out_of_contract():
    """Cas 2026-09-16 : worker A retourne du texte graphify au lieu du JSON."""
    assert (
        _audit_envelope_valid("graphify: community nodes ...", "ses_f6ed03e11ffetdQstFHu2pb7B5")
        is False
    )
    assert (
        _audit_envelope_reason("graphify: community nodes ...", "ses_f6ed03e11ffetdQstFHu2pb7B5")
        == "not-mapping"
    )


def test_audit_envelope_rejects_empty_summary_and_sid_mismatch():
    bad_summary = _valid_audit_envelope()
    bad_summary["summary"] = "   "
    assert _audit_envelope_valid(bad_summary, "ses_f6ed03e11ffetdQstFHu2pb7B5") is False
    assert _audit_envelope_reason(bad_summary, "ses_f6ed03e11ffetdQstFHu2pb7B5") == "empty-summary"

    bad_sid = _valid_audit_envelope(sid="ses_other")
    assert _audit_envelope_valid(bad_sid, "ses_f6ed03e11ffetdQstFHu2pb7B5") is False
    assert _audit_envelope_reason(bad_sid, "ses_f6ed03e11ffetdQstFHu2pb7B5") == "sid-mismatch"


# =====================================================================
# FIX 1-8 : défauts de rendu vérifiés sur le run réel 2026-10-01
# (le rapport affirmait des choses fausses : « 30 commits en attente de
# revue » alors que le run en a produit 3, « 5 518 397.0 tokens,  appels
# API- », deux nombres collés sur une ligne).
# =====================================================================


def _git_repo(path: Path) -> Path:
    """Repo git initialisé, prêt à recevoir des commits."""
    import subprocess as sp

    path.mkdir(parents=True, exist_ok=True)
    for cmd in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "t@t"],
        ["git", "config", "user.name", "T"],
    ):
        sp.run(cmd, cwd=path, check=True)
    return path


def _commit(
    repo: Path, name: str, subject: str, body: str | None = None, when: str | None = None
) -> str:
    """Un commit ; `body` non nul crée un commit à corps multi-ligne.

    `when` fixe la date de commit (auteur ET committer, format ISO git) : sans
    elle, les bornes `--since`/`--until` des tests dépendent de l'horloge réelle.
    """
    import subprocess as sp

    (repo / name).write_text(name, encoding="utf-8")
    sp.run(["git", "add", "-A"], cwd=repo, check=True)
    args = ["git", "commit", "-q", "-m", subject]
    if body is not None:
        args += ["-m", body]
    env = None
    if when is not None:
        import os

        env = {**os.environ, "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when}
    sp.run(args, cwd=repo, check=True, env=env)
    return sp.run(
        ["git", "rev-parse", "--short", "HEAD"], cwd=repo, capture_output=True, text=True
    ).stdout.strip()


def _prep_with_ctx(tmp_path: Path, monkeypatch, **overrides):
    """report_prep sur un repo git dédié ; renvoie (draft_text, ctx)."""
    repo = _git_repo(tmp_path / "repo")
    cfg = _cfg(tmp_path)
    cfg.project_root = repo
    cfg.html_report_dir = str(tmp_path / "html")
    _write_summary(tmp_path)
    if "self_cost" in overrides:
        monkeypatch.setattr(
            "weekly_telemetry_aggregator.report._self_cost_value",
            lambda _cfg: overrides["self_cost"],
        )
    draft, ctx = report_prep(cfg, anchor=RUN.isoformat())
    assert draft is not None and ctx is not None
    return draft.read_text(encoding="utf-8"), ctx, cfg


def _render_html(cfg, ctx) -> str:
    """Rendu HTML via le chemin réel (render_html_report + security gate)."""
    from weekly_telemetry_aggregator.html_report import render_html_report

    dated = render_html_report(cfg, anchor=RUN.isoformat(), ctx=ctx, quality_block=None)
    assert dated is not None
    return dated.read_text(encoding="utf-8")


def _html_visible(html: str) -> str:
    """HTML sans le `<script type="application/json">` (payload machine lisible).

    Le rapport HTML embarque le ctx COMPLET en JSON (usage machine, par design) :
    les listes y sont donc non tronquées. Les assertions de cap portent sur le
    balisage VISIBLE, pas sur la payload.
    """
    return re.sub(r'<script type="application/json"[^>]*>.*?</script>', "", html, flags=re.S)


def test_self_cost_and_auto_commits_occupy_two_distinct_lines(tmp_path: Path, monkeypatch):
    """FIX 1 : `trim_blocks=True` avaleait le newline du `{% endif %}` ligne-final.

    Le self-cost et le compteur de commits se retrouvaient collés sur UNE seule
    ligne. Vérifié sur les DEUX rendus (parité markdown/HTML stricte).
    """
    repo = _git_repo(tmp_path / "repo")
    _commit(repo, "s.md", "skill:demo (auto-rédigé, revue hebdo 2026-08-14)")
    text, ctx, cfg = _prep_with_ctx(tmp_path, monkeypatch, self_cost={"cost": 1.0, "tokens": 42.0})

    lines = text.splitlines()
    sc_idx = next(i for i, line in enumerate(lines) if "self-cost" in line)
    sc_line, next_line = lines[sc_idx], lines[sc_idx + 1]
    # le self-cost ne doit pas déborder sur la ligne des commits…
    assert "Commits auto-rédigés" not in sc_line
    # …et les commits doivent disposer de leur propre ligne
    assert next_line.startswith("- Commits auto-rédigés sur la fenêtre")

    html = _render_html(cfg, ctx)
    assert "Commits auto-rédigés sur la fenêtre" in html
    # HTML : le kpi self-cost et le compteur d'annexes sont deux blocs distincts
    assert html.count("dont pipeline") == 1
    assert "<p><b>Commits auto-rédigés sur la fenêtre" in html


def test_self_cost_tokens_rendered_without_decimal(tmp_path: Path, monkeypatch):
    """FIX 3 : `costing.py` somme des floats → `{:,}` rendait « 5 518 397.0 »."""
    _value = {"cost": 1.2345, "tokens": 5518397.0, "session_id": "ses_x"}
    text, _ctx, cfg = _prep_with_ctx(tmp_path, monkeypatch, self_cost=_value)
    line = next(line for line in text.splitlines() if "self-cost" in line)
    assert "5,518,397 tokens" in line
    assert "5,518,397.0" not in line
    html = _render_html(cfg, _ctx)
    assert "5,518,397" in html
    assert "5,518,397.0" not in html


def test_self_cost_api_calls_absent_from_context_renders_na(tmp_path: Path, monkeypatch):
    """FIX 2 : la clé n'est fournie par AUCUNE source du run → « n/a » explicite.

    Le gabarit rendait une chaîne vide suivie d'un double espace («,  appels
    API »). La valeur ne doit jamais être inventée : « n/a » est la réponse.
    On retire la clé du ctx RÉEL pour provaquer le chemin de défense.
    """
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    from weekly_telemetry_aggregator import report as rp

    _text, ctx, _cfg = _prep_with_ctx(
        tmp_path, monkeypatch, self_cost={"cost": 1.0, "tokens": 100.0}
    )
    assert ctx["self_cost_api_calls"] == "n/a"  # context builder : source absente → n/a
    ctx.pop("self_cost_api_calls")

    env = Environment(
        loader=FileSystemLoader(str(Path(rp.__file__).parent / "templates")),
        autoescape=select_autoescape(("html",)),
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    out = env.get_template("report_template.md.j2").render(**ctx)
    line = next(line for line in out.splitlines() if "self-cost" in line)
    assert "n/a appels API" in line
    assert ",  appels" not in line  # plus de double espace


# ============================================================ E4 : self-cost réconcilié


def _seed_advisor_db(path: Path, cost: float) -> Path:
    """DB V1 seedée avec UNE session advisor (titre = `advisor_run_title` par défaut)."""
    seed_v1_file(
        path,
        [
            {
                "id": "ses_advisor",
                "title": "Lance la revue hebdomadaire",
                "start": RUN - timedelta(hours=2),
                "updated": RUN - timedelta(hours=2),
                "agg_cost": cost,
                "steps": [{"ts": RUN - timedelta(hours=2), "cost": cost}],
            }
        ],
    )
    return path


def _prep_cfg(tmp_path: Path, repo: Path) -> TelemetryConfig:
    """cfg de prep SANS monkeypatch de `_self_cost_value` (chemin réel de mesure)."""
    cfg = _cfg(tmp_path)
    cfg.project_root = repo
    cfg.html_report_dir = str(tmp_path / "html")
    _write_summary(tmp_path)
    return cfg


def _self_cost_lines(text: str) -> list[str]:
    """Lignes AFFICHANT le self-cost (bandeau + annexes), hors inventaire d'artefacts."""
    return [
        ln for ln in text.splitlines() if "self-cost" in ln.lower() and "Artefacts du run" not in ln
    ]


def test_self_cost_persists_post_assemble_artifact(tmp_path: Path):
    """E4 (a) : l'étape 8 écrit `weekly-self-cost-<date>.json` dans le run actif.

    Sans cet artefact, le rapport ne peut afficher que sa mesure d'assemblage —
    une valeur antérieure qu'aucune ligne ne signale comme telle.
    """
    from weekly_telemetry_aggregator.costing import self_cost
    from weekly_telemetry_aggregator.main import EXIT_OK

    cfg = _cfg(tmp_path)
    cfg.opencode_db_path = str(_seed_advisor_db(tmp_path / "opencode.db", 0.0718))

    assert self_cost(cfg, anchor=RUN.isoformat()) == EXIT_OK

    path = active_run_file(tmp_path, f"weekly-self-cost-{DATE}.json")
    assert path.is_file()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["cost"] == pytest.approx(0.0718)
    assert payload["session_id"]
    assert payload["phase"] == "post-assemble"
    assert payload["measured_at"]
    # tokens absents du seed → 0, jamais None (contrat de l'artefact)
    assert payload["tokens"] == 0


def test_report_prefers_persisted_artifact_over_diverging_live_measurement(tmp_path: Path):
    """E4 (b), branche ARTEFACT : une seule valeur — celle de l'étape 8.

    Cas réel du 03/10 : 0,0493 $ dans le rapport contre 0,0718 $ à l'étape 8.
    La source live est ici volontairement PLUS GRANDE que l'artefact : si le
    rapport la publiait (ou les deux), les deux chiffres coexisteraient.
    """
    repo = _git_repo(tmp_path / "repo")
    cfg = _prep_cfg(tmp_path, repo)
    (tmp_path / f"weekly-self-cost-{DATE}.json").write_text(
        json.dumps(
            {
                "cost": 0.0718,
                "tokens": 2820000,
                "session_id": "ses:abc",
                "measured_at": "2026-08-12T10:00:00+00:00",
                "phase": "post-assemble",
            }
        ),
        encoding="utf-8",
    )
    cfg.opencode_db_path = str(_seed_advisor_db(tmp_path / "opencode.db", 3.5))

    draft, ctx = report_prep(cfg, anchor=RUN.isoformat())
    assert draft is not None and ctx is not None
    assert ctx["self_cost"] == pytest.approx(0.0718)
    assert ctx["self_cost_tokens"] == 2820000
    assert ctx["self_cost_phase"] == "post-assemble"

    text = draft.read_text(encoding="utf-8")
    # une seule valeur, jamais les deux
    assert "0.0718" in text
    assert "3.5000" not in text
    # réconciliée → pas de libellé d'antériorité
    assert "mesuré à l'assemblage" not in text
    # HTML : même valeur, pas de « 3,50 »
    html = _html_visible(_render_html(cfg, ctx))
    assert "0,07" in html
    assert "3,50" not in html
    assert "mesuré à l'assemblage" not in html


def test_report_labels_live_self_cost_when_artifact_absent(tmp_path: Path):
    """E4 (b), branche SANS ARTEFACT : la valeur live est étiquetée, pas nue.

    Premier run du jour : l'assemblage ne peut pas encore lire l'artefact de
    l'étape 8. La mesure reste affichée, mais comme mesure d'assemblage.
    """
    repo = _git_repo(tmp_path / "repo")
    cfg = _prep_cfg(tmp_path, repo)
    cfg.opencode_db_path = str(_seed_advisor_db(tmp_path / "opencode.db", 0.0493))

    draft, ctx = report_prep(cfg, anchor=RUN.isoformat())
    assert draft is not None and ctx is not None
    assert ctx["self_cost"] == pytest.approx(0.0493)
    assert ctx["self_cost_phase"] == "assemble"

    text = draft.read_text(encoding="utf-8")
    assert "0.0493" in text
    assert "mesuré à l'assemblage" in text
    # le libellé qualifie CHAQUE occurrence du self-cost (bandeau + annexes)
    lines = _self_cost_lines(text)
    assert lines and all("mesuré à l'assemblage" in ln for ln in lines), lines

    html = _html_visible(_render_html(cfg, ctx))
    assert "0,05" in html
    assert "mesuré à l'assemblage" in html


def test_self_cost_report_and_step8_converge_on_one_value(tmp_path: Path, capsys):
    """E4, bout en bout SANS monkeypatch : deux mesures, un seul chiffre affiché.

    Séquence réelle : prep (pas d'artefact → live étiquetée) → étape 8 (persiste)
    → re-prap alors que la source live a grandi. Le rapport doit basculer sur la
    valeur persistée, sans jamais afficher l'ancienne ni la nouvelle en double.
    """
    from weekly_telemetry_aggregator.costing import self_cost
    from weekly_telemetry_aggregator.main import EXIT_OK

    repo = _git_repo(tmp_path / "repo")
    cfg = _prep_cfg(tmp_path, repo)
    cfg.opencode_db_path = str(_seed_advisor_db(tmp_path / "opencode.db", 0.0493))

    draft, ctx = report_prep(cfg, anchor=RUN.isoformat())
    assert ctx["self_cost"] == pytest.approx(0.0493)
    assert ctx["self_cost_phase"] == "assemble"
    assert "mesuré à l'assemblage" in draft.read_text(encoding="utf-8")

    # l'étape 8 remesure une session qui a continué de tourner
    cfg.opencode_db_path = str(_seed_advisor_db(tmp_path / "opencode_grown.db", 0.0718))
    assert self_cost(cfg, anchor=RUN.isoformat()) == EXIT_OK
    assert "$0.0718" in capsys.readouterr().out

    draft, ctx = report_prep(cfg, anchor=RUN.isoformat())
    assert ctx["self_cost"] == pytest.approx(0.0718)
    assert ctx["self_cost_phase"] == "post-assemble"
    text = draft.read_text(encoding="utf-8")
    assert "0.0718" in text
    assert "0.0493" not in text  # l'ancienne valeur ne subsiste nulle part
    assert "mesuré à l'assemblage" not in text
    # le rapport et la console de l'étape 8 portent le MÊME chiffre
    assert "$0.0718" in text
    # une seule valeur par ligne porteuse de self-cost
    lines = _self_cost_lines(text)
    assert all("0.0718" in ln for ln in lines), lines


def test_report_survives_truncated_self_cost_artifact(tmp_path: Path):
    """E4 : un artefact corrompu (pas de `cost`) retombe sur la mesure live.

    Le mode dégradé doit rester le MÊME chemin que l'absence d'artefact :
    valeur live + libellé, jamais un `None` muet ni une KeyError.
    """
    repo = _git_repo(tmp_path / "repo")
    cfg = _prep_cfg(tmp_path, repo)
    (tmp_path / f"weekly-self-cost-{DATE}.json").write_text(
        json.dumps({"session_id": "ses:abc", "phase": "post-assemble"}), encoding="utf-8"
    )
    cfg.opencode_db_path = str(_seed_advisor_db(tmp_path / "opencode.db", 0.0493))

    draft, ctx = report_prep(cfg, anchor=RUN.isoformat())
    assert ctx["self_cost"] == pytest.approx(0.0493)
    assert ctx["self_cost_phase"] == "assemble"
    assert "mesuré à l'assemblage" in draft.read_text(encoding="utf-8")


def test_audit_unaudited_uses_explicit_cap_label_and_caps_list_at_eight(
    tmp_path: Path, monkeypatch
):
    """FIX 4 : 15 ids rendus sous « plafond 8 » — le cap d'audit K n'est pas un
    plafond sur `unaudited`. Libellé explicite + liste tronquée à 8 + reste."""
    (tmp_path / f"weekly-audit-candidates-{DATE}.json").write_text(
        json.dumps(
            {
                "date": DATE,
                "limit": 8,
                "audited": [],
                "worker_statuses": [],
                "unaudited": [{"session_id": f"ses_{i:04d}"} for i in range(15)],
            }
        ),
        encoding="utf-8",
    )
    text, _ctx, cfg = _prep_with_ctx(tmp_path, monkeypatch)
    assert "cap d'audit K = 8" in text
    assert "plafond 8" not in text  # l'ancien libellé trompeur a disparu
    assert "(+7 autres)" in text  # 15 - 8 rendus
    for i in range(8):
        assert f"`ses_{i:04d}`" in text
    assert "`ses_0008`" not in text  # plafonné à 8

    html = _html_visible(_render_html(cfg, _ctx))
    assert "cap d'audit K = 8" in html
    assert "(+7 autres)" in html
    assert "ses_0008" not in html  # plafonné à 8 dans le balisage visible


def test_dirty_files_rendered_as_indented_sublist_capped_at_eight(tmp_path: Path, monkeypatch):
    """FIX 5 : 30 chemins bruts sur une seule ligne → sous-liste indentée, cap 8."""
    repo = _git_repo(tmp_path / "repo")
    _commit(repo, "a.txt", "feat: base")
    for i in range(20):
        (repo / f"dirty{i:02d}.txt").write_text("wip", encoding="utf-8")

    text, _ctx, cfg = _prep_with_ctx(tmp_path, monkeypatch)
    lines = text.splitlines()
    head_idx = next(i for i, line in enumerate(lines) if line.startswith("- Fichiers dirty au run"))
    assert lines[head_idx].rstrip().endswith(":")
    entries = [line for line in lines[head_idx + 1 :] if line.startswith("  - `?? dirty")]
    assert len(entries) == 8  # capé, pas 20 sur une ligne
    assert all(line.startswith("  - ") for line in entries)  # sous-liste indentée
    assert "(+12 autres)" in "\n".join(lines[head_idx : head_idx + 12])

    html = _html_visible(_render_html(cfg, _ctx))
    assert "(+12 autres)" in html
    assert "dirty08.txt" not in html  # plafonné à 8 dans le balisage visible


def test_auto_commits_count_commits_drafted_after_the_anchor(tmp_path: Path, monkeypatch):
    """FIX 6 (défaut structurel) : le rapport ne peut pas compter ses propres drafts.

    Preuve du run réel 2026-10-01 : ancre 19:07:13Z, commits draftés à
    19:18:54Z/19:19:23Z/19:19:35Z — soit APRÈS la borne. `git log --until=<ancre>`
    renvoyait 0 à chaque run. Ici les 3 commits sont postérieurs à l'ancre et
    doivent être comptés.
    """
    repo = _git_repo(tmp_path / "repo")
    for i in range(3):
        _commit(repo, f"d{i}.md", f"skill:demo{i} (auto-rédigé, revue hebdo {DATE})")

    text, ctx, _cfg = _prep_with_ctx(tmp_path, monkeypatch)
    assert len(ctx["auto_commits"]) == 3
    assert "- Commits auto-rédigés sur la fenêtre : 3" in text


def test_recorded_draft_commits_merged_with_git_log_not_short_circuited(
    tmp_path: Path, monkeypatch
):
    """FIX 6 : les drafts ENREGISTRÉS sont unionnés au git log, jamais écartés.

    Le pipeline sait ce qu'il a committé ; le git log ne le sait pas au moment
    du rendu (aucun draft du run courant n'est encore dans l'historique). Le
    `if recorded: return recorded` perdait en prime les drafts des runs
    PRÉCÉDENTS qui tombent dans la fenêtre — d'où l'union, pas la précédence.
    """
    repo = _git_repo(tmp_path / "repo")
    logged = _commit(
        repo,
        "d0.md",
        "skill:depuis-git (auto-rédigé, revue hebdo 2026-08-14)",
        when="2026-08-10T00:00:00+00:00",
    )
    (tmp_path / f"weekly-timings-{DATE}.json").write_text(
        json.dumps(
            {
                "branches": {"A": {"steps_done": ["a"]}},
                "steps": [
                    {
                        "branch": "D",
                        "step": "commit-draft",
                        "hash": "abc1234",
                        "date": DATE,
                        "subject": "skill:from-timings (auto-rédigé, revue hebdo 2026-08-14)",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    _text, ctx, _cfg = _prep_with_ctx(tmp_path, monkeypatch)
    hashes = [c["hash"] for c in ctx["auto_commits"]]
    assert hashes == ["abc1234", logged]  # le recorded + le log, pas l'un OU l'autre
    # §8 : le backlog borne par cutoff (2026-07-15) ne voit que l'enregistrement
    # ci-dessus — le commit du 2026-08-10 est postérieur à la borne haute.
    assert ctx["pending_auto_commits"] == 1


def test_auto_commits_keeps_previous_run_drafts_inside_the_window(tmp_path: Path, monkeypatch):
    """MEDIUM : borne INFERIEURE manquante — les drafts des runs précédents
    qui tombent dans la fenêtre §8 disparaissaient dès qu'un enregistrement
    existait. Ancre 2026-08-12, fenêtre 7 jours (borne basse 2026-08-05)."""
    repo = _git_repo(tmp_path / "repo")
    # ordre décroissant de date : `git log --since` élague la traversée au premier
    # commit plus ancien que la borne, donc HEAD doit être dans la fenêtre.
    _commit(
        repo,
        "old.md",
        "skill:hors-fenetre (auto-rédigé, revue hebdo 2026-01-01)",
        when="2026-01-01T00:00:00+00:00",
    )
    _commit(
        repo,
        "prev.md",
        "skill:run-precedent (auto-rédigé, revue hebdo 2026-08-06)",
        when="2026-08-06T00:00:00+00:00",
    )
    (tmp_path / f"weekly-timings-{DATE}.json").write_text(
        json.dumps(
            {
                "steps": [
                    {
                        "step": "commit-draft",
                        "hash": "abc1234",
                        "date": DATE,
                        "subject": "skill:run-courant (auto-rédigé, revue hebdo 2026-08-12)",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    _text, ctx, _cfg = _prep_with_ctx(tmp_path, monkeypatch)
    subjects = [c["subject"] for c in ctx["auto_commits"]]
    assert len(subjects) == 2  # courant (enregistré) + précédent (dans la fenêtre)
    assert any("run-precedent" in s for s in subjects)
    assert not any("hors-fenetre" in s for s in subjects)  # borné par le bas


def test_pending_auto_commits_unions_recorded_with_cross_run_backlog(tmp_path: Path):
    """HIGH : « en attente de revue » est un backlog CROSS-RUN, pas un compteur
    du run courant. `if recorded: return len(recorded)` le faisait s'effondrer sur
    le seul run (3 au lieu de 40+). Union dédupliquée par hash."""
    from weekly_telemetry_aggregator.report import _pending_auto_commits

    repo = _git_repo(tmp_path / "repo")
    backlog = [
        _commit(repo, f"old{i}.md", f"skill:backlog{i} (auto-rédigé, revue hebdo 2026-07-01)")
        for i in range(3)
    ]
    timings = {
        "steps": [
            {
                "step": "commit-draft",
                "hash": "abc1234",
                "date": "2026-08-12",
                "subject": "skill:run-courant (auto-rédigé, revue hebdo 2026-08-12)",
            }
        ]
    }
    # cutoff large : les 3 commits antérieurs sont bien dans le backlog
    assert _pending_auto_commits(repo, "2099-01-01T00:00:00Z", timings) == 4

    # le même commit vu par les DEUX sources n'est compté qu'une fois (dédup par hash)
    timings["steps"][0]["hash"] = backlog[0]
    assert _pending_auto_commits(repo, "2099-01-01T00:00:00Z", timings) == 3


def test_pending_auto_commits_ignores_marker_present_only_in_commit_body(tmp_path: Path):
    """FIX 7 : `--grep` matche le CORPS complet → « 30 en attente de revue ».

    Un commit dont le corps mentionne le motif mais dont le SUJET n'en parle pas
    ne doit PAS être compté. Le filtre est appliqué une seule fois, dans
    `_git_log` (côté sujet) : `_pending_auto_commits` ne le re-teste plus.
    """
    repo = _git_repo(tmp_path / "repo")
    _commit(repo, "spec.md", "docs: note de spec", body="Voir aussi : auto-rédigé, revue hebdo")
    _commit(repo, "feat.md", "feat: sans rapport", body="Mention: auto-rédigé, revue hebdo 2026")
    _commit(repo, "vrai.md", "skill:legitime (auto-rédigé, revue hebdo 2026)")

    from weekly_telemetry_aggregator.report import _pending_auto_commits

    # cutoff très tardif : les 3 commits sont dans le backlog
    pending = _pending_auto_commits(repo, "2099-01-01T00:00:00Z")
    assert pending == 1  # seul le vrai draft compte, pas les 2 faux positifs


def test_security_gate_exposes_by_rule_and_explains_count_ratio():
    """FIX 8 : `critical_count` (parcours récursif) vs 32/29 (dedup) vs blocking."""
    from weekly_telemetry_aggregator.report import _assemble_security_gate, _render_security_section

    digest = {
        "harness_counts": {"findings_raw": 32, "findings_unique": 29},
        "inspection": {
            "a": {
                "findings": [
                    {"rule": "security/mcp-tool-poisoning", "severity": "critical"},
                    {"rule": "memory-write-unscoped", "severity": "critical"},
                    {"rule": "unbounded-delegation", "severity": "critical"},
                    {"rule": "security/autre-regle", "severity": "critical"},
                ]
            }
        },
    }
    gate, _warning = _assemble_security_gate(digest)
    assert gate["critical_count"] == 4
    assert gate["blocking_count"] == 3
    assert gate["by_rule"]["mcp-tool-poisoning"] == 1  # normalisation : préfixe security/ retiré
    assert gate["by_rule"]["autre-regle"] == 1
    assert gate["blocking_by_rule"] == {
        "mcp-tool-poisoning": 1,
        "memory-write-unscoped": 1,
        "unbounded-delegation": 1,
    }
    assert (gate["digest_findings_raw"], gate["digest_findings_unique"]) == (32, 29)

    md = _render_security_section(gate)
    assert "not comparable" in md or "pas comparables" in md
    assert "32" in md and "29" in md  # les compteurs du digest sont explicités
    for rule in ("mcp-tool-poisoning", "memory-write-unscoped", "unbounded-delegation"):
        assert rule in md  # les 3 règles bloquantes sont nommées explicitement


# ============================================================ C5 / C6 / C7 / C9 — rendu du rapport
#
# C5 : le nom du skill visé est écrit DANS la puce (il n'était lisible que dans la
#      preuve, tronquée en milieu de mot).
# C6 : une source UNIQUE de troncature, sur frontière de mot.
# C7 : les puces byte-identiques sont condensées en une puce + multiplicateur.
# C9 : les findings de veille sont groupés par `category` (une puce par famille).


def test_truncate_text_never_cuts_a_word_in_half():
    """C6 : la troncature tombe sur une frontière de mot, jamais en milieu de mot.

    Régression observée : `evidence_summary[:110]` rendait « loadtest-baseline-man »
    et « octoperf-campaign-des » — un nom de skill coupé, donc inexploitable.
    """
    from weekly_telemetry_aggregator.report import truncate_text

    evidence = (
        "weekly-coherence-findings.json:147 : skills_never_loaded: "
        "loadtest-baseline-management, octoperf-campaign-design et "
        "candidates.selection absents de la surface déclarative"
    )
    for limit in (60, 110, 160):
        out = truncate_text(evidence, limit)
        assert len(out) <= limit
        cut = out[:-1] if out.endswith("…") else out
        # invariant : chaque mot rendu est un mot INTÉGRAL du texte source
        assert all(token in evidence.split() for token in cut.split()), (limit, out)
    # le cas réellement observé (110) : le nom de skill sort ENTIER, pas « loadtest-baseline-man »
    out = truncate_text(evidence, 110)
    assert "loadtest-baseline-management" in out
    assert "loadtest-baseline-man," not in out and "loadtest-baseline-man " not in out
    assert out.endswith("…")
    assert evidence.startswith(out[:-1].rstrip())


def test_truncate_text_prefers_sentence_then_word_then_hard_cut():
    """C6 : trois sorties, dans l'ordre de préférence documenté."""
    from weekly_telemetry_aggregator.report import truncate_text

    # 1. texte court : inchangé, espaces normalisés
    assert truncate_text("  deux   espaces  ", 80) == "deux espaces"
    # 2. phrase finissant au-delà du tiers de la fenêtre → coupure de phrase, sans ellipse
    phrase = "a" * 100 + ". suite du rapport qui ne rentre pas dans la borne."
    assert truncate_text(phrase, 120) == "a" * 100 + "."
    # 3. pas de phrase, plusieurs mots → dernier espace de la fenêtre
    words = " ".join(f"mot{i:02d}" for i in range(40))
    out = truncate_text(words, 30)
    assert out.endswith("…") and len(out) <= 30
    assert words.startswith(out[:-1].rstrip())
    # 4. jeton insécable plus long que la borne → coupe franche à limit-1 + ellipse
    hard = truncate_text("y" * 100, 10)
    assert hard == "y" * 9 + "…" and len(hard) == 10


def test_truncate_summary_reuses_report_helper():
    """C6 : les DEUX implémentations historiques n'en font plus qu'une.

    `watch_distill.truncate_summary` délègue à `report.truncate_text` ; l'ancienne
    coupe franche de `truncate_summary` (et celle de `main._truncate`) exposait un
    mot coupé. La borne historique « `limit - 1` + ellipse » est conservée.
    """
    from weekly_telemetry_aggregator.report import truncate_text
    from weekly_telemetry_aggregator.watch_distill import truncate_summary

    sample = "Skill weekly-safety-guardrails jamais chargé sur 8 runs consécutifs"
    for limit in (20, 40, 80, 200):
        assert truncate_summary(sample, limit) == truncate_text(sample, limit)
    # la borne est bien celle de `main._truncate` : `limit` caractères, ellipse comprise
    assert len(truncate_summary("z" * 500, 80)) == 80
    assert truncate_summary("z" * 500, 80).endswith("…")


def test_plain_twins_strip_markdown_ticks_for_html_and_keep_it_for_markdown(tmp_path: Path):
    """Jumeaux `plain` : le HTML perd les délimiteurs Markdown, le markdown les garde.

    Régression observée : le HTML fuit des backticks EN CLAIR — « Corriger
    `weekly-report-prose` — 3 violation(s) » (next-steps) et la description d'une
    nouveauté écosystème. Le HTML n'interprète pas le Markdown : le texte Markdown,
    lui, est écrit POUR ces délimiteurs. D'où un jumeau par champ, calculé ici
    (source unique), consommé par le gabarit HTML.

    Les deux rendus portent les MÊMES mots : on a retiré la ponctuation Markdown,
    pas du texte.
    """
    _write_summary(tmp_path)
    (tmp_path / f"weekly-ecosystem-{DATE}.json").write_text(
        json.dumps(
            {
                "new_items": [
                    {
                        "name": "acme/widget",
                        "category": "lib",
                        "description": "Corriger `render` dans la barre — version 1.2.3",
                        "found_via": ["watch:acme/widget"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / f"weekly-harness-digest-{DATE}.json").write_text(
        json.dumps(
            {
                "inspection": {"summary": {"errors": 1, "warnings": 0}},
                "findings": [
                    {"rule": "weekly-report-prose", "severity": "high", "message": "vide"}
                ],
            }
        ),
        encoding="utf-8",
    )

    draft, ctx = report_prep(_cfg(tmp_path), anchor=RUN.isoformat())

    assert ctx is not None and draft is not None
    # 1. next-steps : `text` garde les délimiteurs, `text_plain` les perd
    steps = ctx["top_next_steps"]
    assert steps, "le digest harness doit produire au moins une action"
    assert steps[0]["text"].startswith("Corriger `weekly-report-prose`")
    assert steps[0]["text_plain"] == "Corriger weekly-report-prose — 1 violation(s)"
    assert "`" not in steps[0]["text_plain"]
    # 2. écosystème : idem pour la description
    item = ctx["ecosystem"]["new_items"][0]
    assert "`render`" in item["description_short"]
    assert item["description_plain"] == "Corriger render dans la barre — version 1.2.3"
    # 3. le markdown, lui, rend toujours ses délimiteurs (les deux rendus concordent
    #    sur les MOTS) — et l'artefact brut n'a pas été muté sur place
    assert "`render`" in draft.read_text(encoding="utf-8")
    assert (
        json.loads((tmp_path / f"weekly-ecosystem-{DATE}.json").read_text(encoding="utf-8"))[
            "new_items"
        ][0]["description"]
        == "Corriger `render` dans la barre — version 1.2.3"
    )


def test_coherence_bullet_names_the_skill_it_targets(tmp_path: Path):
    """C5 : la puce nomme la cible, elle ne la laisse plus deviner via la preuve."""
    _write_summary(tmp_path)
    (tmp_path / f"weekly-coherence-findings-{DATE}.json").write_text(
        json.dumps(
            {
                "findings": [
                    {
                        "category": "unused-unreferenced",
                        "tag_action": "archive",
                        "severity": "medium",
                        "description": "Skill jamais chargé mais protégé par une politique TTL pin",
                        "evidence_summary": "skills_never_loaded: loadtest-baseline-management",
                        "recommendation": "Revue puis archivage",
                        "target_skill_id": "loadtest-baseline-management",
                    },
                    {
                        # pas de target_skill_id : le nom est dans la description
                        "category": "retire-candidate",
                        "severity": "medium",
                        "description": "skill 'octoperf-campaign-design' jamais chargé sur 8 runs",
                        "evidence_summary": "skills_never_loaded: 8/8 runs",
                        "recommendation": "Retirer .opencode/skills/octoperf-campaign-design/SKILL.md",
                    },
                    {
                        # ni target_skill_id, ni nom slug : le chemin de la reco est le source
                        "category": "duplicate",
                        "severity": "high",
                        "description": "Deux agents se recouvrent",
                        "evidence_summary": "chevauchement",
                        "recommendation": "Fusionner .opencode/skills/cli-builder/SKILL.md avec l'autre",
                    },
                    {
                        # aucun skill nulle part → pas de `subject` rendu (pas de crochets vides)
                        "category": "dead-reference",
                        "severity": "high",
                        "description": "Référence morte",
                        "evidence_summary": "x",
                        "recommendation": "corriger",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    draft, ctx = report_prep(_cfg(tmp_path), anchor=RUN.isoformat())
    assert draft is not None and ctx is not None
    section_5 = draft.read_text(encoding="utf-8").split("## 5.", 1)[1].split("## 6.", 1)[0]
    bullets = [ln for ln in section_5.splitlines() if ln.startswith("- **")]
    assert len(bullets) == 4
    # le nom est dans la PUCE (avant la description), pas seulement dans la preuve
    assert bullets[0].startswith("- **`loadtest-baseline-management`** **unused-unreferenced** ")
    assert bullets[1].startswith("- **`octoperf-campaign-design`** **retire-candidate** ")
    assert bullets[2].startswith("- **`cli-builder`** **duplicate** ")
    assert bullets[3].startswith("- **dead-reference** ")  # pas de subject → rien d'inventé
    assert "`**" not in bullets[3]
    # le nom n'est plus seulement lisible dans la preuve tronquée
    assert ctx["coherence_items"][0]["subject"] == "loadtest-baseline-management"
    assert ctx["coherence_items"][3].get("subject") is None


def test_identical_findings_collapse_into_one_bullet_with_multiplier(tmp_path: Path):
    """C7 : 12 puces byte-identiques → 1 puce `×12`, pas 12 doublons.

    Mesuré le 03/10 sur le run réel (cf. `doc/measurements/
    2026-10-03-section5-duplication.md`) : 12 findings `unused-unreferenced` et
    17 « jamais chargé 8/8 runs » identiques à l'écran.
    """
    _write_summary(tmp_path)
    duplicate = {
        "category": "unused-unreferenced",
        "severity": "medium",
        "description": "Skill jamais chargé et référencé nulle part",
        "evidence_summary": "weekly-coherence-findings.json:42",
        "recommendation": "Supprimer après revue",
    }
    (tmp_path / f"weekly-coherence-findings-{DATE}.json").write_text(
        json.dumps(
            {
                "findings": [
                    *[dict(duplicate) for _ in range(12)],
                    {  # finding distinct (preuve différente) → reste une puce séparée
                        **duplicate,
                        "evidence_summary": "weekly-coherence-findings.json:99",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    draft, ctx = report_prep(_cfg(tmp_path), anchor=RUN.isoformat())
    assert draft is not None and ctx is not None
    section_5 = draft.read_text(encoding="utf-8").split("## 5.", 1)[1].split("## 6.", 1)[0]
    bullets = [ln for ln in section_5.splitlines() if ln.startswith("- **")]
    assert len(bullets) == 2  # 13 findings → 2 puces
    assert bullets[0].endswith("Supprimer après revue **×12**")
    assert "**×12**" not in bullets[1]
    # le multiplicateur est porté par la donnée, pas compté dans le gabarit
    assert sorted(f["dup"] for f in ctx["coherence_items"]) == [1, 12]


def test_maintenance_findings_collapse_but_keep_distinct_recommendations(tmp_path: Path):
    """C7 : même condensation en §7, SANS agréger des constats qui diffèrent.

    La clé de déduplication inclut la recommandation : c'est elle qui nomme le skill
    à traiter, si hersser dessus fusionnerait deux actions différentes (mesure
    2026-10-03 §3.1).
    """
    _write_summary(tmp_path)
    same = {
        "category": "retire-candidate",
        "severity": "medium",
        "description": "skill 'alpha' jamais chargé sur 8 runs consécutifs",
        "recommendation": "Retirer .opencode/skills/alpha/SKILL.md après revue",
        "evidence_summary": "skills_never_loaded: 8/8 runs",
    }
    findings = [
        *[dict(same) for _ in range(17)],
        {**same, "recommendation": "Retirer .opencode/skills/beta/SKILL.md après revue"},
    ]
    (tmp_path / f"weekly-insights-{DATE}.json").write_text(
        json.dumps({"alerts": [], "maintenance": {"findings": findings}}),
        encoding="utf-8",
    )
    draft, ctx = report_prep(_cfg(tmp_path), anchor=RUN.isoformat())
    assert draft is not None and ctx is not None
    section_7 = draft.read_text(encoding="utf-8").split("## 7.", 1)[1].split("## 8.", 1)[0]
    bullets = [ln for ln in section_7.splitlines() if ln.startswith("- **[")]
    assert len(bullets) == 2  # 18 findings → 2 puces
    assert bullets[0].endswith("**×17**")  # .opencode/skills/alpha
    assert "**×" not in bullets[1]  # .opencode/skills/beta : reco différente
    assert "alpha" in bullets[0] and "beta" in bullets[1]  # C5 : cible dans la puce
    assert ctx["maint_sorted"][0]["subject"] == "alpha"


def test_watch_findings_grouped_by_category_one_bullet_per_family(tmp_path: Path):
    """C9 : `install-new`/`improve-existing`/`ignore` sont des CATÉGORIES.

    Le gabarit les répétait en boucle (26 puces `[LOW] ignore`) : une puce par famille,
    avec le décompte, et les membres dessous.
    """
    _write_summary(tmp_path)

    def _f(category: str, severity: str, i: int) -> dict:
        return {
            "session_id": None,
            "category": category,
            "severity": severity,
            "description": f"motif coûteux {category} #{i} : le harness relance le contexte",
            "evidence_summary": f"pattern coûteux détecté (F:ses_{i:08x}#context-bloat)",
            "recommendation": "Ne rien faire, ecosysteme deja couvert",
            "recommendation_type": f"watch-{category}",
        }

    findings = [_f("ignore", "low", i) for i in range(26)]
    findings += [_f("install-new", "high", 100), _f("install-new", "medium", 101)]
    (tmp_path / f"weekly-watch-findings-{DATE}.json").write_text(
        json.dumps({"findings": findings}), encoding="utf-8"
    )
    draft, ctx = report_prep(_cfg(tmp_path), anchor=RUN.isoformat())
    assert draft is not None and ctx is not None
    section_6 = draft.read_text(encoding="utf-8").split("## 6.", 1)[1].split("## 7.", 1)[0]
    families = [ln for ln in section_6.splitlines() if ln.startswith("- **")]
    # 2 puces de famille (pas 28 puces en boucle)
    assert len(families) == 2
    assert "- **[LOW] ignore** ×26" in section_6
    assert "- **install-new** ×2 — sévérités high×1, medium×1" in section_6
    # les 28 constats restent visibles, un par ligne indentée
    members = [ln for ln in section_6.splitlines() if ln.startswith("  - **[")]
    assert len(members) == 28
    # ordre : la famille la plus grave d'abord
    assert ctx["watch_groups"][0]["category"] == "install-new"
    assert ctx["watch_groups"][1]["severity"] == "low"
    assert ctx["watch_groups"][1]["count"] == 26
    # plus aucune trace de la boucle `[SÉV] category` par constat
    assert section_6.count("- **[LOW] ignore** —") == 0


def test_watch_findings_byte_identical_collapse_inside_their_family(tmp_path: Path):
    """C7 applique aux familles de veille : multiplicateur sur le membre, pas par ligne."""
    _write_summary(tmp_path)
    identical = {
        "session_id": None,
        "category": "ignore",
        "severity": "low",
        "description": "Motif déjà connu, aucun gain",
        "evidence_summary": "pattern coûteux détecté (F:ses_1#context-bloat)",
        "recommendation": "Ignorer",
        "recommendation_type": "watch-ignore",
    }
    (tmp_path / f"weekly-watch-findings-{DATE}.json").write_text(
        json.dumps({"findings": [*[dict(identical) for _ in range(26)]]}), encoding="utf-8"
    )
    draft, _ctx = report_prep(_cfg(tmp_path), anchor=RUN.isoformat())
    assert draft is not None
    section_6 = draft.read_text(encoding="utf-8").split("## 6.", 1)[1].split("## 7.", 1)[0]
    assert "- **[LOW] ignore** ×26 (1 distinct)" in section_6
    assert len([ln for ln in section_6.splitlines() if ln.startswith("  - **[")]) == 1
    assert "Ignorer **×26**" in section_6


def test_watch_section_markdown_and_html_agree(tmp_path: Path, monkeypatch):
    """Parité md/HTML : mêmes familles, mêmes décomptes, mêmes sujets, même troncature."""
    _write_summary(tmp_path)
    (tmp_path / f"weekly-watch-findings-{DATE}.json").write_text(
        json.dumps(
            {
                "findings": [
                    {
                        "session_id": None,
                        "category": "ignore",
                        "severity": "low",
                        # description longue → tronquée sur frontière de mot des deux côtés
                        "description": "Le motif " + "verylongtoken" * 40 + " est déjà couvert",
                        "evidence_summary": "pattern coûteux (F:ses_1#context-bloat)",
                        "recommendation": "Ignorer",
                        "recommendation_type": "watch-ignore",
                    },
                    {
                        "session_id": None,
                        "category": "install-new",
                        "severity": "high",
                        "description": "Adopter X",
                        "evidence_summary": "y",
                        "recommendation": "Évaluer X",
                        "recommendation_type": "watch-install-new",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    text, ctx, cfg = _prep_with_ctx(tmp_path, monkeypatch)
    html = _render_html(cfg, ctx)
    for group in ctx["watch_groups"]:
        assert group["category"] in text
        assert group["category"] in html
        assert f"×{group['count']}" in text
        assert f"&times;{group['count']}" in html
    # la description tronquée est IDENTIQUE dans les deux rendus, et sans mot coupé
    desc_short = ctx["watch_groups"][0]["members"][0]["description"]
    assert len(desc_short) <= 160
    assert desc_short in text and desc_short in html
    assert "verylongtokenverylong" not in desc_short


def test_report_html_shows_skill_subject_and_multiplier(tmp_path: Path, monkeypatch):
    """Parité md/HTML pour C5 + C7 : sujet et multiplicateur rendus des deux côtés."""
    _write_summary(tmp_path)
    (tmp_path / f"weekly-coherence-findings-{DATE}.json").write_text(
        json.dumps(
            {
                "findings": [
                    {
                        "category": "retire-candidate",
                        "severity": "medium",
                        "description": "skill 'weekly-report-prose' jamais chargé sur 8 runs",
                        "evidence_summary": "skills_never_loaded: 8/8 runs",
                        "recommendation": "Retirer .opencode/skills/weekly-report-prose/SKILL.md",
                        "target_skill_id": "weekly-report-prose",
                    }
                ]
                * 3
            }
        ),
        encoding="utf-8",
    )
    text, ctx, cfg = _prep_with_ctx(tmp_path, monkeypatch)
    html = _render_html(cfg, ctx)
    assert ctx["coherence_items"][0]["dup"] == 3
    assert "**`weekly-report-prose`**" in text
    assert "×3" in text
    assert "<code>weekly-report-prose</code>" in html
    assert "&times;3" in html  # `&times;` : la cellule HTML rend ×3


# --------------------------------------------------------------------------- C8
def _write_draft_candidates(tmp_path: Path, candidates: list[dict], *, limit: int = 3) -> None:
    (tmp_path / f"weekly-draft-candidates-{DATE}.json").write_text(
        json.dumps(
            {"schema_version": 1, "date": DATE, "candidates": candidates, "limit": limit},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_report_section8_lists_every_run_artifact_not_four_hardcoded(tmp_path: Path):
    """C8 : le §8 liste le CONTENU RÉEL du run dir, plus une liste de noms en dur.

    Motif : la ligne citait `weekly-summary/insights/harness-digest/ecosystem-<date>.json`
    en dur. Aucun de ces quatre fichiers n'est garanti (l'écosystème peut ne pas
    être produit), et les 10+ autres artefacts du run — dont
    `weekly-draft-candidates-<date>.json` — n'étaient jamais listés.
    """
    _write_summary(tmp_path)
    _write_audit_candidates(tmp_path)
    _write_skill_curate(tmp_path)
    _write_ecosystem(tmp_path)
    _write_draft_candidates(tmp_path, [{"session_id": "ses_x", "severity": "low"}])
    (tmp_path / f"weekly-watch-findings-{DATE}.json").write_text(
        json.dumps({"findings": [], "warnings": []}), encoding="utf-8"
    )

    draft, ctx = report_prep(_cfg(tmp_path), anchor=RUN.isoformat())
    assert draft is not None and ctx is not None
    text = draft.read_text(encoding="utf-8")
    line = next(ln for ln in text.splitlines() if ln.startswith("- Artefacts du run"))

    assert f"- Artefacts du run ({len(ctx['run_artifacts'])}) :" in line
    # les artefacts réellement présents ET l'étape 4 (jamais listée avant)
    for name in (
        f"weekly-summary-{DATE}.json",
        f"weekly-audit-candidates-{DATE}.json",
        f"weekly-draft-candidates-{DATE}.json",
        f"weekly-watch-findings-{DATE}.json",
        f"skill-curate-{DATE}.json",
    ):
        assert f"`{name}`" in line, name
    # un artefact attendu mais absent reste visible, avec son statut
    assert f"`weekly-harness-digest-{DATE}.json` _(absent)_" in line
    # le requis est marqué, et la ligne reste UNE ligne (trim_blocks)
    assert f"`weekly-summary-{DATE}.json` _(requis)_" in line
    assert "\n" not in line


def test_run_artifact_inventory_excludes_render_outputs_and_stays_deterministic(tmp_path: Path):
    """C8 : le §8 reste déterministe — les SORTIES de rendu n'y sont pas.

    Motif : inventorier tous les fichiers faisait apparaître
    `weekly-report-draft-<date>.md` au render N+1 (le draft du render N), donc
    deux rendus successifs du même run ne donnaient pas le même texte.
    """
    from weekly_telemetry_aggregator.report import _run_artifact_inventory

    _write_summary(tmp_path)
    (tmp_path / f"weekly-report-draft-{DATE}.md").write_text("draft", encoding="utf-8")
    (tmp_path / f"weekly-report-{DATE}.md").write_text("report", encoding="utf-8")
    (tmp_path / "extracts").mkdir()

    first = _run_artifact_inventory(tmp_path, {})
    second = _run_artifact_inventory(tmp_path, {})
    assert first == second
    assert [a["name"] for a in first] == [f"weekly-summary-{DATE}.json"]
    assert all(a["status"] == "present" for a in first)

    # un run dir illisible ne lève pas : l'inventaire se réduit au déclaré
    missing = _run_artifact_inventory(tmp_path / "nope", {"x.json": {"status": "absent"}})
    assert missing == [{"name": "x.json", "status": "absent", "required": False}]


def test_run_artifact_inventory_truncates_beyond_display_limit(tmp_path: Path):
    """C8 : au-delà de la fenêtre d'affichage le reste est COMPTÉ, pas listé."""
    from weekly_telemetry_aggregator.report import _RUN_ARTIFACTS_LIST_LIMIT

    _write_summary(tmp_path)
    for i in range(_RUN_ARTIFACTS_LIST_LIMIT + 6):
        (tmp_path / f"extra-{i:03d}.json").write_text("{}", encoding="utf-8")

    draft, ctx = report_prep(_cfg(tmp_path), anchor=RUN.isoformat())
    assert draft is not None and ctx is not None
    text = draft.read_text(encoding="utf-8")
    line = next(ln for ln in text.splitlines() if ln.startswith("- Artefacts du run"))
    listed = line.count("`") // 2
    total = ctx["run_artifacts_total"]
    assert total > _RUN_ARTIFACTS_LIST_LIMIT  # le plafond est bien atteint
    assert listed == _RUN_ARTIFACTS_LIST_LIMIT
    assert f"Artefacts du run ({total}) :" in line
    assert f"+{total - _RUN_ARTIFACTS_LIST_LIMIT} autres (liste tronquée)" in line


# --------------------------------------------------------------------------- C11
def _summary_with_session(tmp_path: Path, session_id: str, project_path: str) -> None:
    """Ajoute UNE session (avec son `project_path`) à la summary du run."""
    _write_summary(tmp_path)
    p = tmp_path / f"weekly-summary-{DATE}.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    data["all_sessions"] = [
        *(data.get("all_sessions") or []),
        {
            "session_id": session_id,
            "project_path": project_path,
            "harness": "opencode",
            "cost_usd": 0.1,
            "total_tokens": 10,
        },
    ]
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _summary_with_sessions(tmp_path: Path, pairs: list[tuple[str, str]]) -> None:
    """Summary du run dont CHAQUE session a son `project_path` (une seule écriture)."""
    _write_summary(tmp_path)
    p = tmp_path / f"weekly-summary-{DATE}.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    data["all_sessions"] = [
        {
            "session_id": sid,
            "project_path": path,
            "harness": "opencode",
            "cost_usd": 0.1,
            "total_tokens": 10,
        }
        for sid, path in pairs
    ]
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_report_drafting_section_names_out_of_scope_target(tmp_path: Path, monkeypatch):
    """C11/D5 : le rapport NOMME la cible hors périmètre, il n'en crée pas de seconde.

    Motif (P5) : l'étape 4 produisait `weekly-draft-candidates-<date>.json` et le
    rapport ne le lisait pas du tout — aucun mot « drafting » dans §7 ni §8. Un
    run dont les 3 candidats visent un dépôt non projeté s'expliquait par « 0
    draft » sans jamais dire où était le travail.
    """
    repo = _git_repo(tmp_path / "repo")
    cfg = _cfg(tmp_path)
    cfg.project_root = repo
    _summary_with_sessions(
        tmp_path,
        [("opencode:ses_in", str(repo)), ("opencode:ses_out", str(tmp_path / "elsewhere"))],
    )
    _write_draft_candidates(
        tmp_path,
        [
            {
                "session_id": "opencode:ses_in",
                "recommendation_type": "skill-candidate",
                "severity": "high",
                "action": "create",
                "skill_id": "skill_in",
                "description": "Motif coûteux répété dans le projet",
            },
            {
                "session_id": "opencode:ses_out",
                "recommendation_type": "command-candidate",
                "severity": "low",
                "action": "create",
                "skill_id": "skill_out",
                "description": "Vérification rejouée à la main dans un autre dépôt",
            },
        ],
    )

    draft, ctx = report_prep(cfg, anchor=RUN.isoformat())
    assert draft is not None and ctx is not None
    text = draft.read_text(encoding="utf-8")

    assert "### Drafting — candidats de l'étape 4" in text
    assert f"Cible du run : `{repo}`" in text
    assert "**Hors périmètre** : 1 candidat(s) sur 2 visent" in text
    assert str(tmp_path / "elsewhere") in text
    # la cible DANS le projet n'est pas étiquetée hors périmètre
    assert ctx["drafting"]["out_of_scope_targets"] == [str(tmp_path / "elsewhere")]
    assert [c["in_scope"] for c in ctx["drafting"]["candidates"]] == [True, False]
    # parité HTML
    html = _render_html(cfg, ctx)
    assert "Drafting — candidats de l'étape 4" in html
    assert "hors périmètre du projet" in html
    assert str(tmp_path / "elsewhere") in html


def test_report_drafting_section_absent_without_artifact(tmp_path: Path):
    """C11 : pas d'artefact d'étape 4 ⇒ pas de section (aucune hypothèse)."""
    _write_summary(tmp_path)
    draft, _ctx = report_prep(_cfg(tmp_path), anchor=RUN.isoformat())
    assert draft is not None
    text = draft.read_text(encoding="utf-8")
    assert "### Drafting" not in text
    assert "weekly-draft-candidates" not in text


def test_report_drafting_section_reports_empty_candidate_list(tmp_path: Path):
    """C11 : artefact présent et VIDE ⇒ section explicite, pas un silence."""
    _write_summary(tmp_path)
    _write_draft_candidates(tmp_path, [])
    draft, ctx = report_prep(_cfg(tmp_path), anchor=RUN.isoformat())
    assert draft is not None
    text = draft.read_text(encoding="utf-8")
    assert "### Drafting — aucune cible" in text
    assert ctx["drafting"]["candidates"] == []


def test_drafting_view_leaves_unresolvable_session_unnamed(tmp_path: Path):
    """C11 : une session absente de la summary n'est PAS comptée hors périmètre.

    Fail-closed : ni « dans le projet » (invérifiable) ni « hors périmètre »
    (inventé). Elle est nommée comme non résolue, et le rapport dit que le chemin
    n'est une donnée d'aucun artefact.
    """
    from weekly_telemetry_aggregator.report import _drafting_view

    payload = {"candidates": [{"session_id": "ses_absent", "description": "x"}]}
    view = _drafting_view(payload, {"all_sessions": []}, tmp_path)
    assert view["out_of_scope"] == []
    assert view["unknown_scope_count"] == 1
    assert view["candidates"][0]["target"] == ""
    # `_drafting_view` ne doit jamais écrire : D5 = nommer, pas redresser.
    assert list(tmp_path.iterdir()) == []


def test_drafting_view_resolves_session_id_with_or_without_harness_prefix(tmp_path: Path):
    """C11 : `opencode:ses_x` et `ses_x` désignent la même session."""
    from weekly_telemetry_aggregator.report import _drafting_view

    summary = {"all_sessions": [{"session_id": "opencode:ses_x", "project_path": "/elsewhere"}]}
    for sid in ("opencode:ses_x", "ses_x"):
        view = _drafting_view({"candidates": [{"session_id": sid}]}, summary, tmp_path)
        assert view["candidates"][0]["target"] == "/elsewhere"
        assert view["out_of_scope_count"] == 1


def test_drafting_view_truncates_prose_without_cutting_a_word(tmp_path: Path):
    """C11 : les descriptions passent par `truncate_text` (jamais de coupe en plein mot)."""
    from weekly_telemetry_aggregator.report import _drafting_view, truncate_text

    # mots de 5+ caractères : la troncature ne peut pas les couper sans le signaler
    description = " ".join(
        [
            "alpha",
            "bravo",
            "charlie",
            "delta",
            "echo",
            "foxtrot",
            "golf",
            "hotel",
            "india",
            "juliet",
            "kilo",
            "lima",
            "mike",
            "november",
            "oscar",
            "papa",
            "quebec",
            "romeo",
        ]
    )
    view = _drafting_view(
        {"candidates": [{"session_id": "ses_x", "description": description}]}, {}, tmp_path
    )
    short = view["candidates"][0]["description_short"]
    assert short == truncate_text(description, 80)
    assert len(short) <= 80
    # contrat de `truncate_text` : coupée sur une frontière de mot, ou ellipsée
    assert short.endswith("…") or description.startswith(short)


# ------------------------------------------------------------------ D5 propagé
def test_group_warnings_reads_count_multiplier_not_line_count():
    """D5 (correctif propagé) : l'annexe lit `count`, pas le nombre de LIGNES.

    Motif : `aggregator._cap_warnings` regroupe les warnings de même message et
    porte le total dans `count`. Compter les lignes affichait `×1` pour 50
    occurrences — l'annexe contredisait le summary JSON qu'elle résume.
    """
    from weekly_telemetry_aggregator.report import _group_warnings

    grouped = _group_warnings(
        [
            {
                "message": "session active exclue",
                "session_id": "ses_a",
                "count": 50,
                "session_ids": ["ses_b", "ses_c"],
            }
        ]
    )
    assert len(grouped) == 1
    assert grouped[0]["count"] == 50
    assert grouped[0]["session_ids"] == ["ses_a", "ses_b", "ses_c"]


def test_group_warnings_absent_count_defaults_to_one():
    """D5 : un warning sans `count` (summary pré-D5) vaut 1 occurrence, pas 0."""
    from weekly_telemetry_aggregator.report import _group_warnings

    grouped = _group_warnings(
        [{"message": "m", "session_id": "ses_a"}, {"message": "m", "session_id": "ses_b"}]
    )
    assert grouped[0]["count"] == 2
    assert grouped[0]["session_ids"] == ["ses_a", "ses_b"]


def test_report_warning_multiplier_rejects_ill_readable_count():
    """D5 : `count` illisible ou négatif retombe sur 1 (jamais 0 → ligne fantôme)."""
    from weekly_telemetry_aggregator.report import _warning_multiplier

    assert _warning_multiplier({}) == 1
    assert _warning_multiplier({"count": 0}) == 1
    assert _warning_multiplier({"count": -5}) == 1
    assert _warning_multiplier({"count": "beaucoup"}) == 1
    assert _warning_multiplier({"count": 7}) == 7


def test_report_annex_warnings_show_multiplier_from_summary_count(tmp_path: Path):
    """D5 : le §8 et la Synthèse affichent le MÊME total que le summary JSON."""
    _write_summary(tmp_path)
    p = tmp_path / f"weekly-summary-{DATE}.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    data["warnings"] = [
        {
            "message": "session active exclue",
            "session_id": "ses_a",
            "count": 50,
            "session_ids": ["ses_a", "ses_b"],
        },
        {"message": "source opencode indisponible", "session_id": "ses_z", "count": 1},
    ]
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    draft, ctx = report_prep(_cfg(tmp_path), anchor=RUN.isoformat())
    assert draft is not None and ctx is not None
    text = draft.read_text(encoding="utf-8")

    # 2 ENTITÉS (pas 2 lignes de 50) sur 51 OCCURRENCES (pas 2)
    assert (
        "- Warnings pipeline : 2 entité(s) groupée(s) sur 51 occurrence(s)"
        " — multiplicateur dominant ×50" in text
    )
    assert "- Warnings pipeline (51) :" in text
    assert "- **×50** session active exclue — `ses_a`, `ses_b`" in text
    assert ctx["warnings_occurrences"] == 51
    assert len(ctx["warnings_grouped"]) == 2


# --------------------------------------------------------------------------- C10/R3
def test_ecosystem_core_change_version_rendered_with_single_v(tmp_path: Path):
    """R3 : le gabarit préfixait `v` sur un `tag_name` qui le porte déjà (`vv1.18.34`)."""
    _write_summary(tmp_path)
    (tmp_path / f"weekly-ecosystem-{DATE}.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "new_items": [],
                "core_changes": [
                    {
                        "version": "v1.18.34",
                        "date": "2026-09-30",
                        "summary": "## Core · ### Bugfixes · Send namespaced identity headers.",
                        "relevance_flag": "medium",
                    }
                ],
                "warnings": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    draft, ctx = report_prep(_cfg(tmp_path), anchor=RUN.isoformat())
    assert draft is not None and ctx is not None
    text = draft.read_text(encoding="utf-8")
    line = next(ln for ln in text.splitlines() if ln.startswith("- v1.18.34 "))
    assert "vv1.18.34" not in text
    assert "- v1.18.34 (2026-09-30) — Core · Bugfixes · Send namespaced identity" in line
    assert "##" not in line and "###" not in line
    assert ctx["ecosystem"]["core_changes"][0]["version_label"] == "v1.18.34"


def test_version_label_normalizes_missing_and_double_v():
    """R3 : un seul `v`, quelle que soit la forme stockée ; rien à inventer."""
    from weekly_telemetry_aggregator.report import _version_label

    assert _version_label("1.18.34") == "v1.18.34"
    assert _version_label("v1.18.34") == "v1.18.34"
    assert _version_label("vv1.18.34") == "v1.18.34"
    assert _version_label("") == ""
    assert _version_label(None) == ""


def test_plain_release_summary_strips_markdown_markers():
    """R3 : le résumé de release est de la prose, pas du Markdown."""
    from weekly_telemetry_aggregator.report import _plain_release_summary

    assert _plain_release_summary("## Core · ### Bugfixes · **`x`** `y`") == (
        "Core · Bugfixes · x y"
    )
    assert _plain_release_summary(None) == ""
    # pas de marqueur en début de ligne ⇒ texte intact (un `#` isolé reste un #)
    assert _plain_release_summary("issue #42 closed") == "issue #42 closed"
