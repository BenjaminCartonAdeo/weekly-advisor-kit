"""Focused tests for the deterministic harness remediation gate."""

from __future__ import annotations

import json
from pathlib import Path

from weekly_telemetry_aggregator.config import TelemetryConfig
from weekly_telemetry_aggregator.harness_remediation import run
from weekly_telemetry_aggregator.main import EXIT_OK, EXIT_PARTIAL, EXIT_TOTAL_FAILURE

ANCHOR = "2026-08-12T00:00:00Z"
DATE = "2026-08-12"


def _config(tmp_path: Path, project: Path) -> TelemetryConfig:
    cfg = TelemetryConfig()
    cfg.output_dir = tmp_path / "reports"
    cfg.project_root = project
    cfg.output_dir.mkdir()
    return cfg


def _proposal(
    proposal_path: Path, *, rule: str = "quality/example-rule", **overrides: object
) -> Path:
    value: dict[str, object] = {
        "schema_version": 1,
        "date": DATE,
        "proposals": [
            {
                "rule": rule,
                "path": overrides.pop("path", ".opencode/commands/foo.md"),
                "line": 1,
                "decision": "apply",
                "confidence": "high",
                "description": "replace the example",
                "rationale": "the exact old text is unsafe",
                "old_text": "old",
                "new_text": "new",
                **overrides,
            }
        ],
    }
    proposal_path.write_text(json.dumps(value), encoding="utf-8")
    return proposal_path


def _project(tmp_path: Path, content: str = "old") -> tuple[Path, Path]:
    project = tmp_path / "project"
    target = project / ".opencode/commands/foo.md"
    target.parent.mkdir(parents=True)
    target.write_text(content, encoding="utf-8")
    return project, target


def _result(cfg: TelemetryConfig) -> dict:
    return json.loads(
        (cfg.output_dir / f"weekly-harness-remediation-{DATE}.json").read_text(encoding="utf-8")
    )


def _write_baseline(cfg: TelemetryConfig, *, rule: str = "quality/example-rule") -> None:
    (cfg.output_dir / f"weekly-harness-digest-{DATE}.json").write_text(
        json.dumps(
            {
                "inspection": {
                    "uncategorized": [
                        {
                            "path": ".opencode/commands/foo.md",
                            "findings": [{"rule": rule}],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )


def test_dry_run_never_changes_target_and_records_proposal(tmp_path: Path):
    project, target = _project(tmp_path)
    cfg = _config(tmp_path, project)
    cfg.harness_auto_fix_rules = ["quality/example-rule"]
    proposal = _proposal(cfg.output_dir / "proposal.json")
    _write_baseline(cfg)

    assert run(cfg, proposal_path=proposal, mode="dry-run", anchor=ANCHOR) == EXIT_OK

    assert target.read_text(encoding="utf-8") == "old"
    result = _result(cfg)
    assert result["summary"] == {
        "applied": 0,
        "proposed": 1,
        "manual": 0,
        "blocked": 0,
        "rolled_back": 0,
    }
    assert result["proposals"][0]["status"] == "proposed"
    assert result["postcheck"]["status"] == "not_run"


def test_apply_is_blocked_by_empty_default_allowlist(tmp_path: Path):
    project, target = _project(tmp_path)
    cfg = _config(tmp_path, project)
    proposal = _proposal(cfg.output_dir / "proposal.json")

    assert run(cfg, proposal_path=proposal, mode="apply", anchor=ANCHOR) == EXIT_OK

    assert target.read_text(encoding="utf-8") == "old"
    result = _result(cfg)
    assert result["summary"]["blocked"] == 1
    assert "harness_auto_fix_rules" in result["proposals"][0]["reason"]


def test_apply_requires_finding_in_current_digest(tmp_path: Path):
    project, target = _project(tmp_path)
    cfg = _config(tmp_path, project)
    cfg.harness_auto_fix_rules = ["quality/example-rule"]
    proposal = _proposal(cfg.output_dir / "proposal.json")

    assert run(cfg, proposal_path=proposal, mode="apply", anchor=ANCHOR) == EXIT_OK

    assert target.read_text(encoding="utf-8") == "old"
    result = _result(cfg)
    assert result["summary"]["blocked"] == 1
    assert "not present in the dated harness digest" in result["proposals"][0]["reason"]


def test_allowed_apply_replaces_text_and_passes_postcheck(tmp_path: Path):
    project, target = _project(tmp_path)
    cfg = _config(tmp_path, project)
    cfg.harness_auto_fix_rules = ["quality/example-rule"]
    proposal = _proposal(cfg.output_dir / "proposal.json")
    _write_baseline(cfg)

    def fake_harness(post_cfg: TelemetryConfig, *, anchor: str | None = None) -> int:
        _ = anchor
        digest = {
            "inspection": {"uncategorized": [{"path": ".opencode/commands/foo.md", "findings": []}]}
        }
        (post_cfg.output_dir / f"weekly-harness-digest-{DATE}.json").write_text(
            json.dumps(digest), encoding="utf-8"
        )
        return EXIT_OK

    assert (
        run(
            cfg,
            proposal_path=proposal,
            mode="apply",
            anchor=ANCHOR,
            harness_runner=fake_harness,
        )
        == EXIT_OK
    )

    assert target.read_text(encoding="utf-8") == "new"
    result = _result(cfg)
    assert result["summary"]["applied"] == 1
    assert result["proposals"][0]["status"] == "applied"
    assert result["postcheck"]["status"] == "passed"


def test_failed_postcheck_restores_every_target(tmp_path: Path):
    project, target = _project(tmp_path)
    cfg = _config(tmp_path, project)
    cfg.harness_auto_fix_rules = ["quality/example-rule"]
    proposal = _proposal(cfg.output_dir / "proposal.json")
    _write_baseline(cfg)

    def fake_harness(post_cfg: TelemetryConfig, *, anchor: str | None = None) -> int:
        _ = anchor
        digest = {
            "inspection": {
                "uncategorized": [
                    {
                        "path": ".opencode/commands/foo.md",
                        "findings": [{"rule": "quality/example-rule"}],
                    }
                ]
            }
        }
        (post_cfg.output_dir / f"weekly-harness-digest-{DATE}.json").write_text(
            json.dumps(digest), encoding="utf-8"
        )
        return EXIT_OK

    assert (
        run(
            cfg,
            proposal_path=proposal,
            mode="apply",
            anchor=ANCHOR,
            harness_runner=fake_harness,
        )
        == EXIT_PARTIAL
    )

    assert target.read_text(encoding="utf-8") == "old"
    result = _result(cfg)
    assert result["summary"]["rolled_back"] == 1
    assert result["proposals"][0]["status"] == "rolled_back"
    assert result["postcheck"]["status"] == "failed"


def test_security_rule_is_blocked_even_when_configured(tmp_path: Path):
    project, target = _project(tmp_path)
    cfg = _config(tmp_path, project)
    cfg.harness_auto_fix_rules = ["security/example-rule"]
    proposal = _proposal(cfg.output_dir / "proposal.json", rule="security/example-rule")

    assert run(cfg, proposal_path=proposal, mode="apply", anchor=ANCHOR) == EXIT_OK

    assert target.read_text(encoding="utf-8") == "old"
    result = _result(cfg)
    assert result["proposals"][0]["status"] == "blocked"
    assert "security/*" in result["proposals"][0]["reason"]


def test_traversal_and_global_paths_are_rejected(tmp_path: Path):
    project, _target = _project(tmp_path)
    cfg = _config(tmp_path, project)
    cfg.harness_auto_fix_rules = ["quality/example-rule"]
    proposal = _proposal(cfg.output_dir / "proposal.json", path="../../etc/passwd")

    assert run(cfg, proposal_path=proposal, mode="apply", anchor=ANCHOR) == EXIT_OK
    result = _result(cfg)
    assert result["summary"]["blocked"] == 1
    assert "invalid target path" in result["proposals"][0]["reason"]


def test_malformed_item_is_retained_in_result(tmp_path: Path):
    project, _target = _project(tmp_path)
    cfg = _config(tmp_path, project)
    proposal = cfg.output_dir / "proposal.json"
    proposal.write_text(
        json.dumps({"schema_version": 1, "date": DATE, "proposals": [{"rule": "x"}]}),
        encoding="utf-8",
    )

    assert run(cfg, proposal_path=proposal, mode="dry-run", anchor=ANCHOR) == EXIT_OK
    result = _result(cfg)
    assert result["summary"]["blocked"] == 1
    assert result["proposals"][0]["status"] == "blocked"
    assert "malformed proposal" in result["proposals"][0]["reason"]


def test_malformed_item_with_unhashable_decision_does_not_crash(tmp_path: Path):
    project, _target = _project(tmp_path)
    cfg = _config(tmp_path, project)
    proposal = cfg.output_dir / "proposal.json"
    item = {
        "rule": "quality/example-rule",
        "path": ".opencode/commands/foo.md",
        "line": 1,
        "decision": [],
        "confidence": "high",
        "description": "d",
        "rationale": "r",
        "old_text": "old",
        "new_text": "new",
    }
    proposal.write_text(
        json.dumps({"schema_version": 1, "date": DATE, "proposals": [item]}),
        encoding="utf-8",
    )

    assert run(cfg, proposal_path=proposal, mode="dry-run", anchor=ANCHOR) == EXIT_OK
    result = _result(cfg)
    assert result["proposals"][0]["status"] == "blocked"


def test_manual_proposal_can_explain_without_patch(tmp_path: Path):
    project, _target = _project(tmp_path)
    cfg = _config(tmp_path, project)
    proposal = cfg.output_dir / "proposal.json"
    proposal.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "date": DATE,
                "proposals": [
                    {
                        "rule": "security/unbounded-delegation",
                        "path": ".opencode/commands/foo.md",
                        "line": 1,
                        "decision": "manual",
                        "confidence": "low",
                        "description": "The delegation policy needs a human decision.",
                        "rationale": "No safe generic replacement exists.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    assert run(cfg, proposal_path=proposal, mode="dry-run", anchor=ANCHOR) == EXIT_OK
    result = _result(cfg)
    assert result["summary"]["manual"] == 1


def test_malformed_document_is_total_failure(tmp_path: Path):
    project, _target = _project(tmp_path)
    cfg = _config(tmp_path, project)
    proposal = cfg.output_dir / "proposal.json"
    proposal.write_text("[]", encoding="utf-8")

    assert run(cfg, proposal_path=proposal, mode="dry-run", anchor=ANCHOR) == EXIT_TOTAL_FAILURE


def test_cli_result_is_dated_without_overwriting_harness_digest(tmp_path: Path):
    project, _target = _project(tmp_path)
    cfg = _config(tmp_path, project)
    proposal = _proposal(cfg.output_dir / "proposal.json")
    digest = cfg.output_dir / f"weekly-harness-digest-{DATE}.json"
    digest.write_text('{"sentinel": true}', encoding="utf-8")
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "project_root": str(project),
                "output_dir": str(cfg.output_dir),
                "harness_auto_fix_rules": ["quality/example-rule"],
            }
        ),
        encoding="utf-8",
    )

    from weekly_telemetry_aggregator.cli import main

    assert (
        main(
            [
                "harness-remediate",
                "--proposal",
                str(proposal),
                "--config",
                str(config_path),
                "--anchor",
                ANCHOR,
            ]
        )
        == EXIT_OK
    )
    assert digest.read_text(encoding="utf-8") == '{"sentinel": true}'
    assert (cfg.output_dir / f"weekly-harness-remediation-{DATE}.json").is_file()


def test_cli_parser_defaults_to_dry_run():
    from weekly_telemetry_aggregator.cli import build_parser

    args = build_parser().parse_args(["harness-remediate", "--proposal", "proposal.json"])
    assert args.mode == "dry-run"
