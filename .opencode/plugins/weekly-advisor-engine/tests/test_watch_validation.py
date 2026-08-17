"""Deterministic validation of raw weekly watch findings."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from weekly_telemetry_aggregator.cli import build_parser, main
from weekly_telemetry_aggregator.watch_validation import (
    match_subject_to_market,
    validate_findings,
)

ANCHOR = datetime(2026, 8, 12, 6, 0, tzinfo=UTC)


def _finding(category: str = "adopt", **values: object) -> dict[str, object]:
    return {
        "category": category,
        "severity": "medium",
        "description": "A useful market item",
        "evidence_summary": "Evidence must survive validation",
        "recommendation": "Evaluate the item with a human",
        **values,
    }


def _context(*items: dict[str, object]) -> dict[str, object]:
    return {"schema_version": 1, "market_matches": list(items)}


def test_absent_market_adopt_is_valid() -> None:
    raw = {"schema_version": 1, "findings": [_finding(subject="new-package")]}
    context = _context(
        {
            "name": "new-package",
            "npm_package": "new-package",
            "existing_state": "absent",
        }
    )

    result = validate_findings(raw, context, date="2026-08-12")

    finding = result["findings"][0]
    assert result["schema_version"] == 2
    assert finding["category"] == "adopt"
    assert finding["existing_state"] == "absent"
    assert finding["decision"] == "adopt"
    assert finding["evidence_summary"] == "Evidence must survive validation"
    assert result["validation"]["counts"]["accepted"] == 1


def test_declared_dcp_adopt_is_downgraded_to_verify_existing() -> None:
    raw = {
        "findings": [
            _finding(
                subject={"npm_package": "@tarquinen/opencode-dcp"},
                evidence={"source": "ecosystem"},
            )
        ]
    }
    context = _context(
        {
            "name": "dcp",
            "npm_package": "@tarquinen/opencode-dcp",
            "existing_state": "declared",
            "capability_state": "covered",
            "match": {"type": "npm_package", "value": "@tarquinen/opencode-dcp"},
        }
    )

    result = validate_findings(raw, context)

    finding = result["findings"][0]
    assert finding["existing_state"] == "declared"
    assert finding["category"] == "verify-existing"
    assert finding["decision"] == "verify-existing"
    assert finding["capability_state"] == "covered"
    assert finding["evidence"] == {"source": "ecosystem"}
    assert result["validation"]["counts"]["downgraded"] == 1


def test_observed_market_item_adopt_becomes_improve_existing() -> None:
    raw = {"findings": [_finding(subject="https://github.com/acme/tool.git")]}
    context = _context(
        {
            "name": "tool",
            "repo_url": "https://github.com/acme/tool",
            "existing_state": "observed",
        }
    )

    result = validate_findings(raw, context)

    finding = result["findings"][0]
    assert finding["existing_state"] == "observed"
    assert finding["category"] == "improve-existing"
    assert finding["decision"] == "improve-existing"


def test_malformed_findings_are_rejected_without_losing_evidence() -> None:
    malformed = [
        "not an object",
        {"category": "adopt", "description": "missing recommendation", "evidence": "keep"},
        {
            "category": "not-a-category",
            "description": "bad category",
            "recommendation": "do something",
        },
    ]

    result = validate_findings({"findings": malformed}, _context())

    assert result["findings"] == []
    assert len(result["rejected_findings"]) == 3
    assert result["rejected_findings"][0]["reason"] == "finding must be an object"
    assert "recommendation" in result["rejected_findings"][1]["reason"]
    assert result["rejected_findings"][1]["finding"]["evidence"] == "keep"
    assert "invalid category" in result["rejected_findings"][2]["reason"]
    assert result["validation"]["counts"]["rejected"] == 3


def test_missing_context_or_raw_is_a_fatal_local_cli_input_error(tmp_path: Path, capsys) -> None:
    reports = tmp_path / "reports"
    reports.mkdir()
    config = tmp_path / "weekly-config.json"
    config.write_text(json.dumps({"output_dir": str(reports)}), encoding="utf-8")

    assert (
        main(
            [
                "watch-validate",
                "--config",
                str(config),
                "--anchor",
                "2026-08-12T06:00:00Z",
            ]
        )
        == 2
    )
    assert "missing input" in capsys.readouterr().err

    context = reports / "weekly-watch-context-2026-08-12.json"
    context.write_text(json.dumps(_context()), encoding="utf-8")
    assert (
        main(
            [
                "watch-validate",
                "--config",
                str(config),
                "--anchor",
                "2026-08-12T06:00:00Z",
            ]
        )
        == 2
    )
    assert "missing input" in capsys.readouterr().err
    assert not (reports / "weekly-watch-findings-2026-08-12.json").exists()


def test_cli_writes_anchor_dated_final_findings(tmp_path: Path, capsys) -> None:
    reports = tmp_path / "reports"
    reports.mkdir()
    config = tmp_path / "weekly-config.json"
    config.write_text(json.dumps({"output_dir": str(reports)}), encoding="utf-8")
    (reports / "weekly-watch-context-2026-08-12.json").write_text(
        json.dumps(
            _context(
                {
                    "name": "new-package",
                    "npm_package": "new-package",
                    "existing_state": "absent",
                }
            )
        ),
        encoding="utf-8",
    )
    (reports / "weekly-watch-findings-raw-2026-08-12.json").write_text(
        json.dumps({"schema_version": 1, "findings": [_finding(subject="new-package")]}),
        encoding="utf-8",
    )

    parser = build_parser()
    assert callable(parser.parse_args(["watch-validate"]).func)
    assert (
        main(
            [
                "watch-validate",
                "--config",
                str(config),
                "--anchor",
                "2026-08-12T06:00:00Z",
            ]
        )
        == 0
    )

    output = reports / "weekly-watch-findings-2026-08-12.json"
    data = json.loads(output.read_text(encoding="utf-8"))
    assert data["schema_version"] == 2
    assert data["findings"][0]["decision"] == "adopt"
    assert data["rejected_findings"] == []
    assert "watch-validate:" in capsys.readouterr().out


def test_subject_helper_matches_normalized_repository_identity() -> None:
    match = match_subject_to_market(
        {"repo_url": "git+https://github.com/Acme/Tool.git"},
        [
            {
                "name": "tool",
                "repo_url": "https://github.com/acme/tool/",
                "existing_state": "observed",
            }
        ],
    )

    assert match is not None
    assert match["existing_state"] == "observed"
