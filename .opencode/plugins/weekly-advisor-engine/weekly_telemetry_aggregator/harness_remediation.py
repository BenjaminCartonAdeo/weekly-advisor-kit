"""Deterministic, transaction-safe remediation of harness proposals.

The harness scanner is intentionally read-only with respect to the project.  A
proposal can only mutate a narrowly allowlisted ``.opencode`` skill or command
when every gate is satisfied.  Applied changes are kept in memory until a
temporary-output post-check proves that the proposed finding disappeared; a
failed post-check restores every changed file.
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import date as calendar_date
from pathlib import Path, PurePosixPath
from typing import Any

from . import main as main_module
from .config import TelemetryConfig
from .main import EXIT_OK, EXIT_PARTIAL, EXIT_TOTAL_FAILURE, _parse_anchor
from .writer import write_json_atomic

MAX_PATCH_LINES = 120
PROPOSAL_SCHEMA_VERSION = 1
VALID_DECISIONS = {"apply", "propose", "manual", "dismiss"}
VALID_CONFIDENCES = {"high", "medium", "low"}
REQUIRED_PROPOSAL_FIELDS = (
    "rule",
    "path",
    "line",
    "decision",
    "confidence",
    "description",
    "rationale",
)

# This name is deliberately module-level so tests and callers can inject a
# hermetic harness implementation without starting a real subprocess.
harness = main_module.harness
_INITIAL_HARNESS = harness


class ProposalInputError(ValueError):
    """Raised when the proposal document itself is not a supported object."""


@dataclass(slots=True)
class _ValidatedProposal:
    """Validated proposal data kept out of the JSON result until serialization."""

    index: int
    raw: dict[str, Any]
    rule: str
    relative_path: str
    target_path: Path
    old_text: str
    new_text: str
    current_text: str | None


@dataclass(slots=True)
class _Postcheck:
    """Post-check result and its serializable representation."""

    data: dict[str, Any]
    passed: bool


def _record(raw: object, index: int, status: str, reason: str) -> dict[str, Any]:
    """Build one result record while preserving the caller's proposal fields."""
    if isinstance(raw, dict):
        result: dict[str, Any] = dict(raw)
    else:
        result = {"proposal": raw}
    result.update({"index": index, "status": status, "reason": reason})
    return result


def _summary(records: list[dict[str, Any]]) -> dict[str, int]:
    """Count proposal dispositions using the stable remediation vocabulary."""
    return {
        status: sum(1 for record in records if record.get("status") == status)
        for status in ("applied", "proposed", "manual", "blocked", "rolled_back")
    }


def _line_count(text: str) -> int:
    """Return a useful patch-line count, including a non-empty final line."""
    return len(text.splitlines()) if text else 0


def _read_text_exact(path: Path) -> str:
    """Read UTF-8 text without normalizing CRLF into LF before patching."""
    with path.open("r", encoding="utf-8", newline="") as handle:
        return handle.read()


def _write_text_exact(path: Path, content: str) -> None:
    """Write UTF-8 text without applying platform newline conversion."""
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(content)


def _valid_date(value: object) -> bool:
    """Return whether ``value`` is an exact ISO calendar date."""
    if not isinstance(value, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
        return False
    try:
        calendar_date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _resolve_under(path: Path, base: Path, label: str) -> Path:
    """Resolve ``path`` and require it to remain below ``base``."""
    try:
        resolved_base = base.expanduser().resolve()
        resolved_path = path.expanduser().resolve()
    except OSError as exc:
        raise ProposalInputError(f"{label} cannot be resolved: {exc}") from exc
    if not resolved_path.is_relative_to(resolved_base):
        raise ProposalInputError(f"{label} must be under {resolved_base}")
    return resolved_path


def _load_proposal_document(path: Path) -> tuple[str, list[object]]:
    """Load and validate the proposal document envelope."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProposalInputError(f"proposal JSON is unreadable: {exc}") from exc

    if not isinstance(raw, dict):
        raise ProposalInputError("proposal document must be a JSON object")
    schema_version = raw.get("schema_version")
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != PROPOSAL_SCHEMA_VERSION
    ):
        raise ProposalInputError(f"schema_version must be {PROPOSAL_SCHEMA_VERSION}")
    proposal_date = raw.get("date")
    if not _valid_date(proposal_date):
        raise ProposalInputError("date must be YYYY-MM-DD")
    proposals = raw.get("proposals")
    if not isinstance(proposals, list):
        raise ProposalInputError("proposals must be a JSON array")
    return str(proposal_date), proposals


def _contains_symlink(root: Path, path: Path) -> bool:
    """Return whether a target or one of its path components is a symlink."""
    try:
        relative = path.relative_to(root)
    except ValueError:
        return True
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            return True
    return False


def _resolve_target(project_root: Path, value: object) -> tuple[Path, str]:
    """Validate and resolve one project-relative remediation target."""
    if not isinstance(value, str) or not value:
        raise ValueError("path must be a non-empty project-relative string")
    if "\x00" in value:
        raise ValueError("path contains a NUL byte")

    normalized = value.replace("\\", "/")
    pure = PurePosixPath(normalized)
    if pure.is_absolute() or ".." in pure.parts or "." in pure.parts:
        raise ValueError("path must not be absolute or traverse parent directories")
    relative_path = pure.as_posix()
    if not (
        relative_path.startswith(".opencode/skills/")
        or relative_path.startswith(".opencode/commands/")
    ):
        raise ValueError("path must be under .opencode/skills/ or .opencode/commands/")
    if relative_path.endswith("/"):
        raise ValueError("path must identify a file")

    root = project_root.expanduser().resolve()
    target = root.joinpath(*pure.parts)
    try:
        resolved_target = target.resolve()
    except OSError as exc:
        raise ValueError(f"path cannot be resolved: {exc}") from exc
    if not resolved_target.is_relative_to(root):
        raise ValueError("path resolves outside project_root")
    if _contains_symlink(root, target):
        raise ValueError("symlink targets are not eligible for remediation")
    if not target.is_file():
        raise ValueError("target file does not exist")
    return target, relative_path


def _validate_proposal(
    raw: object,
    index: int,
    project_root: Path,
) -> tuple[_ValidatedProposal | None, str | None]:
    """Validate proposal shape, safe target, and apply-only patch constraints."""
    if not isinstance(raw, dict):
        return None, "malformed proposal: each proposal must be a JSON object"
    missing = [key for key in REQUIRED_PROPOSAL_FIELDS if key not in raw]
    if missing:
        return None, f"malformed proposal: missing field(s): {', '.join(missing)}"

    rule = raw["rule"]
    path_value = raw["path"]
    line = raw["line"]
    decision = raw["decision"]
    confidence = raw["confidence"]
    description = raw["description"]
    rationale = raw["rationale"]
    old_text = raw.get("old_text", "")
    new_text = raw.get("new_text", "")
    if not isinstance(rule, str) or not rule:
        return None, "malformed proposal: rule must be a non-empty string"
    if not isinstance(line, int) or isinstance(line, bool) or line < 1:
        return None, "malformed proposal: line must be a positive integer"
    if not isinstance(decision, str) or decision not in VALID_DECISIONS:
        return None, f"malformed proposal: decision must be one of {sorted(VALID_DECISIONS)}"
    if not isinstance(confidence, str) or confidence not in VALID_CONFIDENCES:
        return None, f"malformed proposal: confidence must be one of {sorted(VALID_CONFIDENCES)}"
    if not all(isinstance(value, str) for value in (description, rationale, old_text, new_text)):
        return (
            None,
            "malformed proposal: description, rationale, old_text, and new_text must be strings",
        )
    if decision == "apply" and not old_text:
        return None, "malformed proposal: old_text must not be empty for apply"

    try:
        target_path, relative_path = _resolve_target(project_root, path_value)
    except ValueError as exc:
        return None, f"blocked: invalid target path: {exc}"

    current_text: str | None = None
    if decision == "apply":
        if rule.lower().startswith("security/"):
            return None, "blocked: security/* rules are never auto-applied"
        if confidence != "high":
            return None, "blocked: automatic application requires confidence=high"
        if _line_count(old_text) > MAX_PATCH_LINES or _line_count(new_text) > MAX_PATCH_LINES:
            return None, f"blocked: old_text and new_text are limited to {MAX_PATCH_LINES} lines"
        try:
            current_text = _read_text_exact(target_path)
        except (OSError, UnicodeError) as exc:
            return None, f"blocked: target cannot be read as UTF-8: {exc}"
        occurrences = current_text.count(old_text)
        if occurrences != 1:
            return None, f"blocked: old_text occurs {occurrences} times; exactly once is required"

    return (
        _ValidatedProposal(
            index=index,
            raw=raw,
            rule=rule,
            relative_path=relative_path,
            target_path=target_path,
            old_text=old_text,
            new_text=new_text,
            current_text=current_text,
        ),
        None,
    )


def _apply_gate(
    proposal: _ValidatedProposal,
    cfg: TelemetryConfig,
) -> str | None:
    """Return a deterministic gate reason, or ``None`` when eligible."""
    if proposal.rule.lower().startswith("security/"):
        return "blocked: security/* rules are never auto-applied"
    if proposal.raw["confidence"] != "high":
        return "blocked: automatic application requires confidence=high"
    if proposal.rule not in {str(rule) for rule in cfg.harness_auto_fix_rules}:
        return "blocked: rule is not listed in harness_auto_fix_rules"
    return None


def _status_for_non_apply(decision: str) -> tuple[str, str]:
    """Translate an explicit human decision to the result vocabulary."""
    if decision == "propose":
        return "proposed", "proposal requires review; automatic application was not requested"
    if decision == "manual":
        return "manual", "proposal is explicitly marked for manual handling"
    return "dismissed", "proposal is explicitly dismissed"


def _normalise_digest_path(value: object) -> str | None:
    """Convert harness output paths to project-relative POSIX paths."""
    if not isinstance(value, str) or not value:
        return None
    normalized = value.replace("\\", "/")
    marker = normalized.find(".opencode/")
    if marker >= 0:
        return normalized[marker:]
    if normalized == ".opencode":
        return normalized
    return normalized.removeprefix("./")


def _digest_findings(digest: Mapping[str, Any]) -> list[dict[str, str | None]]:
    """Extract rule/path records from top-level and component harness findings."""
    findings: list[dict[str, str | None]] = []
    uncategorized_files = digest.get("uncategorized_files")

    def add_finding(value: Mapping[str, Any], component_path: str | None) -> None:
        rule_value = value.get("rule") or value.get("id")
        if not isinstance(rule_value, str) or not rule_value:
            rule_value = "unknown"
        path = _normalise_digest_path(value.get("path") or component_path)
        findings.append({"rule": rule_value, "path": path})

    top_level = digest.get("findings")
    if isinstance(top_level, list):
        for value in top_level:
            if isinstance(value, Mapping):
                add_finding(value, None)

    inspection = digest.get("inspection")
    if not isinstance(inspection, Mapping):
        return findings
    for section in ("command", "claude_md", "uncategorized"):
        components = inspection.get(section)
        if not isinstance(components, list):
            continue
        for index, component in enumerate(components):
            if not isinstance(component, Mapping):
                continue
            component_path = _normalise_digest_path(component.get("path"))
            if (
                component_path is None
                and section == "uncategorized"
                and isinstance(uncategorized_files, list)
                and index < len(uncategorized_files)
            ):
                component_path = _normalise_digest_path(uncategorized_files[index])
            detailed_rules: set[str] = set()
            component_findings = component.get("findings")
            if isinstance(component_findings, list):
                for value in component_findings:
                    if isinstance(value, Mapping):
                        add_finding(value, component_path)
                        rule_value = value.get("rule") or value.get("id")
                        if isinstance(rule_value, str):
                            detailed_rules.add(rule_value)
            rules = component.get("rules")
            if isinstance(rules, list):
                for value in rules:
                    if not isinstance(value, Mapping) or value.get("result") in (None, "pass"):
                        continue
                    rule_value = value.get("rule")
                    rule = str(rule_value) if rule_value else "unknown"
                    if rule not in detailed_rules:
                        findings.append({"rule": rule, "path": component_path})
    return findings


def _postcheck_counts(
    digest: Mapping[str, Any], findings: list[dict[str, str | None]]
) -> dict[str, int | None]:
    """Return compact, stable counts for the remediation report."""
    paths = {value["path"] for value in findings if value.get("path")}
    rules = {value["rule"] for value in findings if value.get("rule")}
    counts: dict[str, int | None] = {
        "findings": len(findings),
        "files": len(paths),
        "rules": len(rules),
    }
    harness_counts = digest.get("harness_counts")
    if isinstance(harness_counts, Mapping):
        for key in ("files_scanned", "components_scanned", "findings_raw", "findings_unique"):
            value = harness_counts.get(key)
            counts[key] = value if isinstance(value, int) else None
    return counts


def _load_digest(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """Load a digest, returning a user-facing error instead of raising."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, f"digest JSON is unreadable: {exc}"
    if not isinstance(value, dict):
        return None, "digest JSON must contain an object"
    return value, None


def _harness_runner(
    runner: Callable[..., int] | None,
) -> Callable[..., int]:
    """Select an injected runner while supporting patches on either module."""
    if runner is not None:
        return runner
    if harness is not _INITIAL_HARNESS:
        return harness
    return main_module.harness


def _run_postcheck(
    cfg: TelemetryConfig,
    *,
    date: str,
    anchor: str | None,
    applied: list[_ValidatedProposal],
    baseline_findings: list[dict[str, str | None]],
    runner: Callable[..., int] | None,
) -> _Postcheck:
    """Run harness in a disposable output directory and verify target findings."""
    expected = {(proposal.rule, proposal.relative_path) for proposal in applied}
    expected_paths = {path for _rule, path in expected}
    baseline_keys = {
        (str(value.get("rule")), str(value.get("path")))
        for value in baseline_findings
        if value.get("path") is not None
    }
    with tempfile.TemporaryDirectory(prefix="weekly-harness-remediation-") as temporary:
        temporary_cfg = replace(cfg, output_dir=Path(temporary))
        try:
            rc = _harness_runner(runner)(temporary_cfg, anchor=anchor)
        except Exception as exc:  # noqa: BLE001 - failed post-check must trigger rollback
            return _Postcheck(
                {
                    "status": "failed",
                    "counts": {"findings": 0, "files": 0, "rules": 0},
                    "reason": f"post-check harness raised an exception: {exc}",
                },
                False,
            )

        digest_path = temporary_cfg.output_dir / f"weekly-harness-digest-{date}.json"
        digest, error = _load_digest(digest_path)
        if rc != EXIT_OK:
            reason = f"post-check harness failed with exit code {rc}"
            if error:
                reason += f"; {error}"
            return _Postcheck(
                {
                    "status": "failed",
                    "counts": {"findings": 0, "files": 0, "rules": 0},
                    "reason": reason,
                },
                False,
            )
        if digest is None:
            return _Postcheck(
                {
                    "status": "failed",
                    "counts": {"findings": 0, "files": 0, "rules": 0},
                    "reason": f"post-check digest is missing or invalid: {error}",
                },
                False,
            )

        findings = _digest_findings(digest)
        expected_rules = {rule for rule, _path in expected}
        post_keys = {
            (str(value.get("rule")), str(value.get("path")))
            for value in findings
            if value.get("path") is not None
        }
        missing = sorted(expected - post_keys)
        remaining = sorted(expected & post_keys)
        unlocated_expected = sorted(
            {
                str(value.get("rule"))
                for value in findings
                if value.get("path") is None and value.get("rule") in expected_rules
            }
        )
        added_on_targets = sorted(
            {
                (str(value.get("rule")), str(value.get("path")))
                for value in findings
                if value.get("path") in expected_paths
                and (str(value.get("rule")), str(value.get("path"))) not in baseline_keys
                and (str(value.get("rule")), str(value.get("path"))) not in expected
            }
        )
        reasons: list[str] = []
        if remaining:
            reasons.append(f"applied finding(s) remain: {remaining}")
        if unlocated_expected:
            reasons.append(
                "post-check could not prove disappearance for rule(s) without a project path: "
                f"{unlocated_expected}"
            )
        if added_on_targets:
            reasons.append(f"new finding(s) appeared on target(s): {added_on_targets}")
        counts = _postcheck_counts(digest, findings)
        if reasons:
            return _Postcheck(
                {
                    "status": "failed",
                    "counts": counts,
                    "reason": "; ".join(reasons),
                    "missing_targets": missing,
                    "remaining_targets": remaining,
                    "unlocated_expected_rules": unlocated_expected,
                    "new_findings_on_targets": added_on_targets,
                },
                False,
            )
        return _Postcheck(
            {
                "status": "passed",
                "counts": counts,
                "reason": "all applied rule/path findings disappeared",
                "missing_targets": missing,
                "remaining_targets": remaining,
                "unlocated_expected_rules": unlocated_expected,
                "new_findings_on_targets": added_on_targets,
            },
            True,
        )


def _restore_snapshots(snapshots: Mapping[Path, str], project_root: Path) -> list[str]:
    """Restore all snapshots, reporting every restoration failure."""
    errors: list[str] = []
    root = project_root.expanduser().resolve()
    for path, content in snapshots.items():
        if _contains_symlink(root, path):
            errors.append(f"{path}: target became a symlink during remediation")
            continue
        try:
            _write_text_exact(path, content)
        except (OSError, UnicodeError) as exc:
            errors.append(f"{path}: restore failed: {exc}")
    return errors


def _apply_changes(
    proposals: list[_ValidatedProposal],
) -> tuple[dict[Path, str], set[Path], str | None]:
    """Apply selected replacements after a final exact-content check."""
    snapshots = {proposal.target_path: proposal.current_text or "" for proposal in proposals}
    changed: set[Path] = set()
    try:
        for proposal in proposals:
            current = _read_text_exact(proposal.target_path)
            if current != snapshots[proposal.target_path]:
                raise OSError(f"target changed during detection: {proposal.relative_path}")
            if current.count(proposal.old_text) != 1:
                raise OSError(f"old_text is no longer unique: {proposal.relative_path}")
            changed.add(proposal.target_path)
            _write_text_exact(
                proposal.target_path, current.replace(proposal.old_text, proposal.new_text)
            )
    except (OSError, UnicodeError) as exc:
        return snapshots, changed, str(exc)
    return snapshots, changed, None


def _write_result(path: Path, result: dict[str, Any]) -> bool:
    """Write one result artefact without exposing writer exceptions to callers."""
    try:
        write_json_atomic(path, result)
    except OSError as exc:
        print(f"harness-remediate: FATAL: result write failed: {exc}", file=sys.stderr, flush=True)
        return False
    return True


def run(
    cfg: TelemetryConfig,
    *,
    proposal_path: Path | str,
    mode: str = "dry-run",
    anchor: str | None = None,
    harness_runner: Callable[..., int] | None = None,
) -> int:
    """Evaluate or apply a dated proposal document.

    ``dry-run`` performs every validation gate but never writes a project file.
    ``apply`` writes only fully eligible proposals, then rolls back all changes
    when the isolated harness post-check cannot prove the requested remediation.
    """
    if mode not in {"dry-run", "apply"}:
        print(f"harness-remediate: FATAL: unsupported mode: {mode}", file=sys.stderr, flush=True)
        return EXIT_TOTAL_FAILURE
    try:
        run_time = _parse_anchor(anchor)
    except (TypeError, ValueError) as exc:
        print(f"harness-remediate: FATAL: invalid anchor: {exc}", file=sys.stderr, flush=True)
        return EXIT_TOTAL_FAILURE
    date = run_time.strftime("%Y-%m-%d")

    try:
        proposal_path = Path(proposal_path)
        output_root = cfg.output_dir.expanduser().resolve()
        proposal = _resolve_under(proposal_path, output_root, "proposal path")
        if not proposal.is_file():
            raise ProposalInputError(f"proposal file does not exist: {proposal}")
        proposal_date, raw_proposals = _load_proposal_document(proposal)
        if proposal_date != date:
            raise ProposalInputError(
                f"proposal date {proposal_date} does not match anchor date {date}"
            )
    except (OSError, TypeError, ProposalInputError) as exc:
        print(f"harness-remediate: FATAL: {exc}", file=sys.stderr, flush=True)
        return EXIT_TOTAL_FAILURE

    if cfg.project_root is None:
        print("harness-remediate: FATAL: project_root manquant dans la config", file=sys.stderr)
        return EXIT_TOTAL_FAILURE
    try:
        project_root = cfg.project_root.expanduser().resolve()
    except OSError as exc:
        print(f"harness-remediate: FATAL: project_root cannot be resolved: {exc}", file=sys.stderr)
        return EXIT_TOTAL_FAILURE

    baseline_path = cfg.output_dir / f"weekly-harness-digest-{date}.json"
    baseline_digest, _baseline_error = _load_digest(baseline_path)
    baseline_findings = _digest_findings(baseline_digest) if baseline_digest is not None else []
    baseline_keys = {
        (str(value.get("rule")), str(value.get("path")))
        for value in baseline_findings
        if value.get("path") is not None
    }

    records: list[dict[str, Any]] = []
    eligible: list[_ValidatedProposal] = []
    for index, raw in enumerate(raw_proposals):
        proposal_data, validation_error = _validate_proposal(raw, index, project_root)
        if validation_error is not None:
            records.append(_record(raw, index, "blocked", validation_error))
            continue
        if proposal_data is None:  # pragma: no cover - defensive for type narrowing
            records.append(_record(raw, index, "blocked", "malformed proposal"))
            continue

        decision = str(proposal_data.raw["decision"])
        if decision != "apply":
            status, reason = _status_for_non_apply(decision)
            records.append(_record(raw, index, status, reason))
            continue
        gate_error = _apply_gate(proposal_data, cfg)
        if gate_error is not None:
            records.append(_record(raw, index, "blocked", gate_error))
            continue
        if (proposal_data.rule, proposal_data.relative_path) not in baseline_keys:
            records.append(
                _record(
                    raw,
                    index,
                    "blocked",
                    "blocked: rule/path is not present in the dated harness digest",
                )
            )
            continue
        records.append(_record(raw, index, "blocked", "pending deterministic gate evaluation"))
        eligible.append(proposal_data)

    record_by_index = {int(record["index"]): record for record in records}
    selected: list[_ValidatedProposal] = []
    selected_paths: set[str] = set()
    max_files = max(0, int(cfg.harness_auto_fix_max_files))
    for proposal_data in eligible:
        record = record_by_index[proposal_data.index]
        if proposal_data.relative_path in selected_paths:
            record["reason"] = "blocked: more than one proposal targets the same file"
            continue
        if len(selected_paths) >= max_files:
            record["reason"] = f"blocked: harness_auto_fix_max_files limit is {max_files}"
            continue
        selected.append(proposal_data)
        selected_paths.add(proposal_data.relative_path)
        if mode == "dry-run":
            record["status"] = "proposed"
            record["reason"] = "eligible for apply; dry-run made no project changes"

    postcheck: dict[str, Any] = {
        "status": "not_run",
        "counts": {"findings": 0, "files": 0, "rules": 0},
        "reason": "no project changes were requested",
    }
    result_path = cfg.output_dir / f"weekly-harness-remediation-{date}.json"
    result: dict[str, Any] = {
        "schema_version": PROPOSAL_SCHEMA_VERSION,
        "date": date,
        "mode": mode,
        "proposal_file": proposal.relative_to(output_root).as_posix(),
        "summary": _summary(records),
        "proposals": records,
        "postcheck": postcheck,
    }

    if mode == "dry-run" or not selected:
        if mode == "apply" and not selected:
            postcheck["reason"] = "no proposal passed the deterministic apply gates"
        result["summary"] = _summary(records)
        result["postcheck"] = postcheck
        if not _write_result(result_path, result):
            return EXIT_TOTAL_FAILURE
        print(f"harness-remediate: result {result_path}", flush=True)
        return EXIT_OK

    snapshots, changed_paths, apply_error = _apply_changes(selected)
    if apply_error is not None:
        restore_errors = _restore_snapshots(
            {path: snapshots[path] for path in changed_paths}, project_root
        )
        reason = f"rolled back: {apply_error}"
        if restore_errors:
            reason += "; " + "; ".join(restore_errors)
        for proposal_data in selected:
            record_by_index[proposal_data.index]["status"] = "rolled_back"
            record_by_index[proposal_data.index]["reason"] = reason
        postcheck = {
            "status": "failed",
            "counts": {"findings": 0, "files": 0, "rules": 0},
            "reason": reason,
        }
        result["summary"] = _summary(records)
        result["proposals"] = records
        result["postcheck"] = postcheck
        if not _write_result(result_path, result):
            return EXIT_TOTAL_FAILURE
        print(f"harness-remediate: result {result_path}", flush=True)
        return EXIT_PARTIAL

    for proposal_data in selected:
        record_by_index[proposal_data.index]["status"] = "applied"
        record_by_index[proposal_data.index]["reason"] = "replacement applied; awaiting post-check"

    postcheck_result = _run_postcheck(
        cfg,
        date=date,
        anchor=anchor,
        applied=selected,
        baseline_findings=baseline_findings,
        runner=harness_runner,
    )
    postcheck = postcheck_result.data
    if not postcheck_result.passed:
        restore_errors = _restore_snapshots(snapshots, project_root)
        rollback_reason = f"rolled_back: {postcheck.get('reason', 'post-check failed')}"
        if restore_errors:
            rollback_reason += "; " + "; ".join(restore_errors)
        for proposal_data in selected:
            record_by_index[proposal_data.index]["status"] = "rolled_back"
            record_by_index[proposal_data.index]["reason"] = rollback_reason
        postcheck["status"] = "failed"
        postcheck["reason"] = rollback_reason

    result["summary"] = _summary(records)
    result["proposals"] = records
    result["postcheck"] = postcheck
    if not _write_result(result_path, result):
        if postcheck_result.passed:
            restore_errors = _restore_snapshots(
                {path: snapshots[path] for path in changed_paths}, project_root
            )
            for proposal_data in selected:
                record_by_index[proposal_data.index]["status"] = "rolled_back"
                record_by_index[proposal_data.index]["reason"] = (
                    "rolled_back: remediation result could not be written"
                )
            if restore_errors:
                for proposal_data in selected:
                    record_by_index[proposal_data.index]["reason"] += "; " + "; ".join(
                        restore_errors
                    )
        return EXIT_TOTAL_FAILURE
    print(f"harness-remediate: result {result_path}", flush=True)
    return EXIT_OK if postcheck_result.passed else EXIT_PARTIAL


def harness_remediate(
    cfg: TelemetryConfig,
    *,
    proposal_path: Path | str,
    mode: str = "dry-run",
    anchor: str | None = None,
    harness_runner: Callable[..., int] | None = None,
) -> int:
    """Compatibility-named wrapper for callers that prefer the command name."""
    return run(
        cfg,
        proposal_path=proposal_path,
        mode=mode,
        anchor=anchor,
        harness_runner=harness_runner,
    )


def remediate(
    cfg: TelemetryConfig,
    *,
    proposal_path: Path | str,
    mode: str = "dry-run",
    anchor: str | None = None,
    harness_runner: Callable[..., int] | None = None,
) -> int:
    """Short-name wrapper for programmatic callers of the remediation gate."""
    return run(
        cfg,
        proposal_path=proposal_path,
        mode=mode,
        anchor=anchor,
        harness_runner=harness_runner,
    )
