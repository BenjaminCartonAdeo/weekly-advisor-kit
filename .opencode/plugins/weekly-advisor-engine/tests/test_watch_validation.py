"""Deterministic validation of raw weekly watch findings."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from weekly_telemetry_aggregator.cli import build_parser, main
from weekly_telemetry_aggregator.watch_memory import week_of
from weekly_telemetry_aggregator.watch_validation import (
    match_subject_to_market,
    validate_findings,
)

ANCHOR = datetime(2026, 8, 12, 6, 0, tzinfo=UTC)


def _finding(category: str = "install-new", **values: object) -> dict[str, object]:
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


def _project_with_skills(root: Path, *names: str) -> Path:
    for name in names:
        directory = root / ".opencode" / "skills" / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "SKILL.md").write_text(
            "---\ndescription: capacite locale\n---\ncorps\n", encoding="utf-8"
        )
    return root


def _fiche(cid: str, verdict: str = "clean", reason: str | None = None) -> dict[str, object]:
    return {
        "id": cid,
        "name": cid.split(":", 1)[-1],
        "sources": [],
        "score": {"total": 1, "breakdown": {}},
        "security": {"verdict": verdict, "reason": reason},
        "summary": "une fiche",
        "signature": {"version": "1.0.0", "published_at": None},
        "local_relevance_hints": [],
    }


def _candidates_payload(
    *fiches: dict[str, object],
    annex: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "mode": "distill",
        "candidates": list(fiches),
    }
    if annex is not None:
        payload["security_annex"] = annex
    return payload


def _write_candidates(tmp_path: Path, payload: dict[str, object]) -> Path:
    path = tmp_path / "watch-candidates-2026-08-12.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_absent_market_install_new_is_valid() -> None:
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
    assert finding["category"] == "install-new"
    assert finding["existing_state"] == "absent"
    assert finding["decision"] == "install-new"
    assert finding["evidence_summary"] == "Evidence must survive validation"
    assert result["validation"]["counts"]["accepted"] == 1


def test_declared_item_install_new_is_downgraded_to_verify_existing() -> None:
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


def test_observed_market_item_install_new_becomes_improve_existing() -> None:
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
        {"category": "install-new", "description": "missing recommendation", "evidence": "keep"},
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
    assert data["findings"][0]["decision"] == "install-new"
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


# ------------------------------------------------------- coercition cible locale


def test_improve_existing_unknown_or_missing_target_coerced_to_install_new(
    tmp_path: Path,
) -> None:
    root = _project_with_skills(tmp_path, "known-target")
    raw = {
        "findings": [
            _finding(
                "improve-existing",
                target_local="ghost-capability",
                subject={"name": "tool-a", "npm_package": "tool-a"},
            ),
            _finding(
                "improve-existing",
                target_local="known-target",
                subject={"name": "tool-b", "npm_package": "tool-b"},
            ),
            _finding(
                "improve-existing",
                target_local=None,
                subject={"name": "tool-c", "npm_package": "tool-c"},
            ),
        ]
    }
    context = _context(
        {"name": "tool-a", "npm_package": "tool-a", "existing_state": "absent"},
        {"name": "tool-b", "npm_package": "tool-b", "existing_state": "absent"},
        {"name": "tool-c", "npm_package": "tool-c", "existing_state": "absent"},
    )

    result = validate_findings(raw, context, project_root=root)

    first, second, third = result["findings"]
    assert first["category"] == "install-new"
    assert first["decision"] == "install-new"
    assert second["category"] == "improve-existing"
    assert second["decision"] == "improve-existing"
    assert third["category"] == "install-new"
    assert third["decision"] == "install-new"
    assert result["validation"]["counts"]["downgraded"] == 2
    assert result["validation"]["counts"]["rejected"] == 0


def test_target_coercion_is_neutral_without_project_root() -> None:
    raw = {
        "findings": [
            _finding(
                "improve-existing",
                subject={"name": "tool-a", "npm_package": "tool-a"},
            )
        ]
    }
    context = _context({"name": "tool-a", "npm_package": "tool-a", "existing_state": "absent"})

    result = validate_findings(raw, context)

    finding = result["findings"][0]
    assert finding["category"] == "improve-existing"
    assert finding["decision"] == "improve-existing"


# -------------------------------------------------- sévérité fiche suspicious


def test_suspicious_fiche_without_risk_mention_raises_severity_high(tmp_path: Path) -> None:
    candidates_path = _write_candidates(
        tmp_path,
        _candidates_payload(
            _fiche("npm:sketchy-tool", verdict="suspicious", reason="recent-no-traction")
        ),
    )
    raw = {
        "findings": [
            _finding(subject={"name": "sketchy-tool", "npm_package": "sketchy-tool"}),
            _finding(
                evidence_summary="risk flagged: recent-no-traction observed upstream",
                subject={"name": "sketchy-tool", "npm_package": "sketchy-tool"},
            ),
        ]
    }
    context = _context(
        {"name": "sketchy-tool", "npm_package": "sketchy-tool", "existing_state": "absent"}
    )

    result = validate_findings(raw, context, candidates_path=candidates_path)

    first, second = result["findings"]
    assert first["severity"] == "high"
    assert second["severity"] == "medium"


def test_suspicious_fiche_without_reason_key_raises_severity_high(tmp_path: Path) -> None:
    sketchy = _fiche("npm:sketchy-tool", verdict="suspicious")
    del sketchy["security"]["reason"]  # clé absente, pas seulement None
    candidates_path = _write_candidates(tmp_path, _candidates_payload(sketchy))
    raw = {"findings": [_finding(subject={"name": "sketchy-tool", "npm_package": "sketchy-tool"})]}
    context = _context(
        {"name": "sketchy-tool", "npm_package": "sketchy-tool", "existing_state": "absent"}
    )

    result = validate_findings(raw, context, candidates_path=candidates_path)

    assert result["findings"][0]["severity"] == "high"


# ---------------------------------------------------------- annexe sécurité


def test_security_annex_from_candidates_snapshot(tmp_path: Path) -> None:
    candidates_path = _write_candidates(
        tmp_path,
        _candidates_payload(
            _fiche("npm:clean-tool"),
            annex=[{"id": "npm:blocked-one", "name": "blocked-one", "reason": "env-exfiltration"}],
        ),
    )
    raw = {"findings": [_finding(subject={"name": "clean-tool", "npm_package": "clean-tool"})]}
    context = _context(
        {"name": "clean-tool", "npm_package": "clean-tool", "existing_state": "absent"}
    )

    result = validate_findings(raw, context, candidates_path=candidates_path)

    assert result["security_annex"] == {"blocked_count": 1, "ids": ["npm:blocked-one"]}
    assert result["validation"]["counts"]["accepted"] == 1


def test_no_security_annex_without_candidates_path() -> None:
    raw = {"findings": [_finding(subject={"name": "clean-tool", "npm_package": "clean-tool"})]}
    context = _context(
        {"name": "clean-tool", "npm_package": "clean-tool", "existing_state": "absent"}
    )

    result = validate_findings(raw, context)

    assert "security_annex" not in result


def test_filet_metadata_passes_through_to_final_findings() -> None:
    raw = {
        "schema_version": 1,
        "filet": {"kept": ["npm:extra"], "dropped_reasons": {"url:qux": "hors périmètre"}},
        "findings": [_finding(subject={"name": "clean-tool", "npm_package": "clean-tool"})],
    }
    context = _context(
        {"name": "clean-tool", "npm_package": "clean-tool", "existing_state": "absent"}
    )

    result = validate_findings(raw, context)

    assert result["filet"] == {
        "kept": ["npm:extra"],
        "dropped_reasons": {"url:qux": "hors périmètre"},
    }


# ------------------------------------------------------------ writer mémoire


def _memory_entries(path: Path) -> dict[str, dict]:
    lines = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    return {entry["id"]: entry for entry in lines}


def test_memory_writer_records_final_statuses(tmp_path: Path) -> None:
    candidates_path = _write_candidates(
        tmp_path,
        _candidates_payload(
            _fiche("npm:new-tool"),
            _fiche("gh:acme/unreviewed"),
            _fiche("npm:noisy"),
        ),
    )
    raw = {
        "findings": [
            _finding(subject={"name": "new-tool", "npm_package": "new-tool"}),
            _finding("ignore", subject={"name": "noisy", "npm_package": "noisy"}),
            _finding(
                "bogus-category",
                description="mauvaise catégorie",
                recommendation="rejeter",
                subject={"name": "rejected-tool", "npm_package": "rejected-tool"},
            ),
            "not an object",
        ]
    }
    context = _context(
        {"name": "new-tool", "npm_package": "new-tool", "existing_state": "absent"},
        {"name": "noisy", "npm_package": "noisy", "existing_state": "absent"},
    )
    memory_path = tmp_path / "watch-memory.jsonl"

    result = validate_findings(
        raw,
        context,
        date="2026-08-12",
        memory_path=memory_path,
        candidates_path=candidates_path,
    )

    week = week_of(datetime(2026, 8, 12, tzinfo=UTC))
    entries = _memory_entries(memory_path)
    assert entries["npm:new-tool"]["history"][-1] == {"week": week, "status": "recommended"}
    assert entries["npm:new-tool"]["last_signature"] == {"version": "1.0.0", "published_at": None}
    assert entries["gh:acme/unreviewed"]["history"][-1] == {"week": week, "status": "seen"}
    assert entries["npm:noisy"]["history"][-1] == {"week": week, "status": "ignored"}
    assert entries["npm:rejected-tool"]["history"][-1] == {"week": week, "status": "seen"}
    # Rejet enregistré SANS la raison (jamais de note empoisonnante).
    assert entries["npm:rejected-tool"]["note"] == ""
    assert result["validation"]["counts"]["rejected"] == 2


def test_memory_not_written_without_path_or_date(tmp_path: Path) -> None:
    memory_path = tmp_path / "watch-memory.jsonl"
    raw = {"findings": [_finding(subject={"name": "clean-tool", "npm_package": "clean-tool"})]}
    context = _context(
        {"name": "clean-tool", "npm_package": "clean-tool", "existing_state": "absent"}
    )

    validate_findings(raw, context, memory_path=memory_path)
    assert not memory_path.exists()

    validate_findings(raw, context, date="2026-08-12")
    assert not memory_path.exists()
