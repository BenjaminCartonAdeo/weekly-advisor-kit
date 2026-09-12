"""Report rendering — report_prep (deterministic draft) + report_assemble (blocks injection)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from helpers import make_step, make_usage, tzutc

from weekly_telemetry_aggregator.aggregator import aggregate
from weekly_telemetry_aggregator.config import TelemetryConfig
from weekly_telemetry_aggregator.main import RunProvenance
from weekly_telemetry_aggregator.models import Period
from weekly_telemetry_aggregator.report import (
    _coherence_has_curation_signal,
    _critical_security_findings,
    applicable_summary_rc,
    report_assemble,
    report_blocks_draft,
    report_prep,
    validate_required_artifacts,
)
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
    _write_summary(tmp_path)
    cfg = _cfg(tmp_path)
    report_prep(cfg, anchor=RUN.isoformat())
    (tmp_path / f"weekly-harness-digest-{DATE}.json").write_text(
        json.dumps({"findings": [{"rule": "security/tool-poisoning", "severity": "critical"}]}),
        encoding="utf-8",
    )
    final_path, warnings, rc = report_assemble(cfg, anchor=RUN.isoformat())
    assert final_path is not None
    assert rc != 0
    assert any("security/critical" in warning for warning in warnings)


@pytest.mark.parametrize(
    "rule",
    ["mcp-tool-poisoning", "unbounded-delegation", "memory-write-unscoped"],
)
def test_report_assemble_nested_blocking_security_rule_is_rc_two(tmp_path: Path, rule: str):
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
    assert final_path is None
    assert rc == 2
    assert not (tmp_path / f"weekly-report-{DATE}.md").exists()
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
    assert ctx["coherence_items"] == [{"tag": "drift", "description": "x"}]
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


def test_report_harness_budget_rendered(tmp_path: Path):
    _write_summary(tmp_path)
    cfg = _cfg(tmp_path)
    (tmp_path / f"weekly-harness-digest-{DATE}.json").write_text(
        json.dumps(
            {
                "inspection": {"summary": {"errors": 2, "warnings": 3}},
                "budget": {
                    "total_tokens": 11884,
                    "always_loaded": 5575,
                    "on_demand": 6309,
                    "always_loaded_ratio": 0.47,
                    "heaviest": "claude_md/CLAUDE",
                },
                "triggers": {"skill_count": 0, "overlaps": []},
                "dependencies": {"total_edges": 3, "broken": []},
            }
        ),
        encoding="utf-8",
    )
    draft, ctx = report_prep(cfg, anchor=RUN.isoformat())
    text = draft.read_text(encoding="utf-8")
    assert "Budget tokens" in text and "11,884" in text
    assert "Dépendances : 3 arêtes" in text


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
                            "description": "first finding",
                            "recommendation": "fix it",
                            "evidence_summary": "file:line proof one",
                        },
                        {
                            "severity": "MEDIUM",
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
    assert "- **[HIGH]** first finding → fix it — preuve : file:line proof one\n" in text
    assert "\n- **[MEDIUM]** second finding → fix it too — preuve : file:line proof two" in text
    assert "proof one- **[MEDIUM]" not in text  # plus de collage (trim_blocks)
    # ligne unscoped non collée à la section suivante (trim_blocks, v6.0.k)
    assert "b.json\n" in text
    assert "b.json## 6." not in text
    # annexe : répertoire du run
    assert f"`runs/{active.run_id}/`" in text


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
    _write_summary(tmp_path)
    cfg = _cfg(tmp_path)
    report_prep(cfg, anchor=RUN.isoformat())
    (tmp_path / f"weekly-harness-digest-{DATE}.json").write_text(
        json.dumps({"findings": [{"rule": "mcp-tool-poisoning", "severity": "high"}]}),
        encoding="utf-8",
    )
    final_path, warnings, rc = report_assemble(cfg, anchor=RUN.isoformat())
    assert final_path is None and rc == 2
    assert not (tmp_path / f"weekly-report-{DATE}.md").exists()


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
