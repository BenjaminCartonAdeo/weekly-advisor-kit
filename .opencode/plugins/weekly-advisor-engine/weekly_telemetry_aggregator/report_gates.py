"""Gate d'artefacts pour le rapport hebdomadaire."""

import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path

from .artifact_contracts import (
    _artifact_contract_valid,
    _audit_envelope_reason,
    _canonical_audit_path,
    _declared_run_filename,
    _json_file_state,
    _valid_date,
)


def _check_required_json(
    out: Path,
    name: str,
    date: str,
    required_set: set[str],
    applicable: Mapping[str, bool],
) -> dict[str, object]:
    path = out / f"{name}-{date}.json"
    status = "absent"
    path_valid = (
        _valid_date(date)
        and isinstance(name, str)
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name) is not None
        and path.name == f"{name}-{date}.json"
    )
    data: object | None = None
    schema_valid = False
    if not path_valid:
        status = "ill_readable"
    elif path.is_file():
        try:
            text = path.read_text(encoding="utf-8")
            data = json.loads(text) if text.strip() else None
            parsed = isinstance(data, dict)
            strict = name in required_set
            schema_valid = parsed and _artifact_contract_valid(name, data, date=date)
            status = "present" if parsed and (schema_valid or not strict) else "ill_readable"
        except (OSError, UnicodeError, json.JSONDecodeError):
            status = "ill_readable"
    applicable_default = name in required_set or status != "absent"
    return {
        "path": str(path),
        "present": status == "present",
        "status": status,
        "required": name in required_set,
        "path_valid": path_valid,
        "schema_valid": schema_valid,
        "applicable": bool(applicable.get(name, applicable_default)),
    }


def _check_dynamic_audit_artifact(
    declaration: object, out: Path, taken: int
) -> tuple[str, dict[str, object]] | None:
    raw_declaration = str(declaration) if isinstance(declaration, str) else ""
    filename = _declared_run_filename(raw_declaration, out)
    match = re.fullmatch(r"audit-findings-(.+)\.json", filename) if filename is not None else None
    key = filename or f"audit-findings-declaration-{taken}"
    path = out / filename if filename is not None else out / raw_declaration
    sid = match.group(1) if match else ""
    data, state = _json_file_state(path) if match else (None, "ill_readable")
    path_ok = bool(match and _canonical_audit_path(out, sid) == path)
    envelope_reason = _audit_envelope_reason(data, sid) if path_ok and state == "present" else "ok"
    valid = bool(path_ok and state == "present" and envelope_reason == "ok")
    if not path_ok:
        reason = "path-mismatch"
    elif state == "absent":
        reason = "absent"
    elif state != "present":
        reason = "ill-readable"
    else:
        reason = envelope_reason
    return key, {
        "path": str(path),
        "present": valid,
        "status": "present" if valid else ("absent" if state == "absent" else "ill_readable"),
        "required": True,
        "path_valid": path_ok,
        "schema_valid": valid,
        "reason": reason,
        "applicable": True,
    }


def _check_html_artifact(path: Path | None) -> dict[str, object]:
    result: dict[str, object] = {"status": "absent", "path": str(path) if path else None}
    if path is None:
        return result
    try:
        if not path.is_file():
            result["status"] = "absent"
        elif not path.read_text(encoding="utf-8").strip():
            result["status"] = "ill_readable"
        else:
            result["status"] = "present"
    except (OSError, UnicodeError):
        result["status"] = "ill_readable"
    return result


def _html_gate_status(html_path: Path | None, html_enabled: bool) -> dict[str, object]:
    if html_path is not None:
        return _check_html_artifact(html_path)
    if html_enabled:
        return {"status": "absent", "path": None}
    return {"status": "disabled", "path": None}


def _summarize_artifact_gate(
    required: dict[str, dict[str, object]],
    optional: dict[str, dict[str, object]],
    html: dict[str, object],
) -> dict[str, object]:
    required_status = (
        "pass"
        if required and all(a["status"] == "present" for a in required.values())
        else "incomplete"
    )
    optional_missing = [
        entry["path"]
        for entry in optional.values()
        if entry["status"] == "absent" and entry.get("applicable", True)
    ]
    optional_ill_readable = [
        entry["path"]
        for entry in optional.values()
        if entry["status"] == "ill_readable" and entry.get("applicable", True)
    ]
    return {
        "required": required,
        "optional": optional,
        "artifacts": {**required, **optional},
        "status": required_status,
        "optional_missing": optional_missing,
        "optional_ill_readable": optional_ill_readable,
        "html": html,
    }


def validate_required_artifacts(
    out: Path,
    date: str,
    *,
    html_enabled: bool = False,
    html_path: Path | None = None,
    required_names: Iterable[str] | None = None,
    applicability: Mapping[str, bool] | None = None,
    dynamic_audit_artifacts: Iterable[object] = (),
) -> dict[str, object]:

    names = {
        "required": tuple(required_names or ("weekly-summary",)),
        "optional": (
            "weekly-insights",
            "weekly-harness-digest",
            "weekly-ecosystem",
            "weekly-quality-findings",
            "weekly-coherence-findings",
            "skill-curate",
            "weekly-audit-candidates",
            "weekly-watch-context",
            "weekly-watch-findings",
            "weekly-watch-findings-raw",
            "weekly-harness-remediation",
            "weekly-harness-remediation-proposals",
            "weekly-timings",
        ),
    }
    explicit_required = {str(name) for name in names["required"]}
    branch_required = {
        str(name) for name, enabled in (applicability or {}).items() if enabled is True
    }
    required_set = explicit_required | branch_required
    optional_names = tuple(name for name in names["optional"] if name not in required_set)
    names["optional"] = optional_names
    applicable = applicability or {}

    required = {
        f"{name}-{date}.json": _check_required_json(out, name, date, required_set, applicable)
        for name in sorted(required_set)
    }

    dynamic_required: dict[str, dict[str, object]] = {}
    for declaration in dynamic_audit_artifacts:
        checked = _check_dynamic_audit_artifact(declaration, out, len(dynamic_required))
        if checked is None:
            continue
        key, record = checked
        if key in dynamic_required:
            continue
        dynamic_required[key] = record
    required.update(dynamic_required)
    optional = {
        f"{name}-{date}.json": _check_required_json(out, name, date, required_set, applicable)
        for name in names["optional"]
    }

    html = _html_gate_status(html_path, html_enabled)
    return _summarize_artifact_gate(required, optional, html)
