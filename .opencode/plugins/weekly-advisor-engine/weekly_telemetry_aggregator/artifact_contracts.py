"""Centralised artifact contracts for weekly-advisor reports."""

import json
import re
from collections.abc import Callable, Mapping
from pathlib import Path


def _coerce_rc(value: object, *, default: int | None = None) -> int | None:
    """Return a bounded process status, rejecting malformed values.

    JSON manifests are agent-facing inputs.  Treat booleans, empty strings and
    arbitrary text as malformed instead of accidentally turning them into a
    successful status with ``int(...)``.
    """
    if isinstance(value, bool) or value is None:
        return default
    if isinstance(value, int):
        return value if value >= 0 else default
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return default


def _json_file_state(path: Path) -> tuple[object | None, str]:
    """Read one JSON artifact and distinguish absent from malformed input."""
    if not path.is_file():
        return None, "absent"
    try:
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            return None, "ill_readable"
        value = json.loads(text)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, "ill_readable"
    return (value, "present") if isinstance(value, dict) else (None, "ill_readable")


def _valid_schema_version(value: object, expected: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value == expected


def _nonempty_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _valid_date(date: object) -> bool:
    return isinstance(date, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) is not None


def _valid_weekly_summary(value: Mapping) -> bool:
    period = value.get("period")
    return (
        _valid_schema_version(value.get("schema_version"), 2)
        and isinstance(period, Mapping)
        and _nonempty_text(period.get("start"))
        and _nonempty_text(period.get("end"))
        and _nonempty_text(value.get("generated_at"))
        and isinstance(value.get("totals"), Mapping)
    )


def _valid_weekly_insights(value: Mapping) -> bool:
    return (
        _valid_schema_version(value.get("schema_version"), 1)
        and isinstance(value.get("period"), Mapping)
        and _nonempty_text(value.get("generated_at"))
        and isinstance(value.get("deltas"), Mapping)
        and isinstance(value.get("alerts"), list)
        and isinstance(value.get("maintenance"), Mapping)
    )


def _valid_harness_digest(value: Mapping) -> bool:
    inspection = value.get("inspection")
    return (
        (isinstance(inspection, Mapping) and bool(inspection))
        or (isinstance(value.get("rules"), list) and bool(value["rules"]))
        or (isinstance(value.get("findings"), list) and bool(value["findings"]))
    )


def _valid_ecosystem(value: Mapping) -> bool:
    return (
        _valid_schema_version(value.get("schema_version"), 2)
        and isinstance(value.get("new_items"), list)
        and isinstance(value.get("core_changes"), list)
        and isinstance(value.get("warnings"), list)
    )


def _has_findings_list(value: Mapping) -> bool:
    return isinstance(value.get("findings"), list)


def _valid_coherence(value: Mapping) -> bool:
    return isinstance(value.get("findings"), list) or isinstance(
        value.get("curation_signal"), (list, Mapping)
    )


def _valid_audit_candidates(value: Mapping) -> bool:
    return (
        _valid_schema_version(value.get("schema_version"), 1)
        and isinstance(value.get("audited"), list)
        and isinstance(value.get("unaudited"), list)
        and isinstance(value.get("limit"), int)
        and not isinstance(value.get("limit"), bool)
    )


def _valid_watch_context(value: Mapping) -> bool:
    return _valid_schema_version(value.get("schema_version"), 1) and isinstance(
        value.get("market_matches"), list
    )


def _valid_watch_findings(value: Mapping) -> bool:
    return (
        _valid_schema_version(value.get("schema_version"), 2)
        and isinstance(value.get("findings"), list)
        and isinstance(value.get("validation"), Mapping)
    )


def _valid_watch_candidates(value: Mapping) -> bool:
    return (
        _valid_schema_version(value.get("schema_version"), 1)
        and isinstance(value.get("candidates"), list)
        and isinstance(value.get("security_annex"), list)
    )


def _valid_remediation(value: Mapping) -> bool:
    return isinstance(value.get("summary"), Mapping) and isinstance(value.get("postcheck"), Mapping)


def _valid_timings(value: Mapping) -> bool:
    return isinstance(value.get("branches"), Mapping) or isinstance(
        value.get("steps"), (list, Mapping)
    )


def _valid_remediation_proposals(value: Mapping, date: str | None) -> bool:
    envelope = (
        _valid_date(value.get("date"))
        and (date is None or value.get("date") == date)
        and isinstance(value.get("proposals"), list)
    )
    # The remediation producer currently emits v1. Keep v1 as the
    # canonical proposal contract until the producer is versioned to v2.
    return envelope and (
        _valid_schema_version(value.get("schema_version"), 1)
        or _valid_schema_version(value.get("schema_version"), 2)
    )


def _valid_skill_curate(value: Mapping, date: str | None, allow_legacy_v1: bool) -> bool:
    mode = value.get("mode")
    common = (
        mode in {"dry-run", "dry_run", "apply"}
        and isinstance(value.get("dry_run"), bool)
        and isinstance(value.get("decisions"), list)
        and _coerce_rc(value.get("rc"), default=None) is not None
        and _valid_date(value.get("date"))
        and (date is None or value.get("date") == date)
    )
    if not common:
        return False
    if _valid_schema_version(value.get("schema_version"), 2):
        return (
            _nonempty_text(value.get("generated_at"))
            and _nonempty_text(value.get("anchor"))
            and isinstance(value.get("summary"), Mapping)
            and isinstance(value.get("skipped_details"), list)
        )
    return allow_legacy_v1 and _valid_schema_version(value.get("schema_version"), 1)


#: Artifact name → shape validator (date/legacy variants handled separately).
_ARTIFACT_CONTRACTS: dict[str, Callable[[Mapping], bool]] = {
    "weekly-summary": _valid_weekly_summary,
    "weekly-insights": _valid_weekly_insights,
    "weekly-harness-digest": _valid_harness_digest,
    "weekly-ecosystem": _valid_ecosystem,
    "weekly-quality-findings": _has_findings_list,
    "weekly-watch-findings-raw": _has_findings_list,
    "weekly-coherence-findings": _valid_coherence,
    "weekly-audit-candidates": _valid_audit_candidates,
    "weekly-watch-context": _valid_watch_context,
    "weekly-watch-findings": _valid_watch_findings,
    "watch-candidates": _valid_watch_candidates,
    "weekly-harness-remediation": _valid_remediation,
    "weekly-timings": _valid_timings,
}
def _artifact_contract_valid(
    name: str,
    value: object,
    *,
    date: str | None = None,
    allow_legacy_v1: bool = False,
) -> bool:
    """Validate the exact JSON shape of a required/recovered artifact.

    The report gate must not treat ``{}`` as a successful producer output.  A
    compact contract is kept here rather than importing producer modules so the
    report remains usable when an optional branch is not installed.
    """
    if not isinstance(value, Mapping) or not value:
        return False
    if name == "weekly-harness-remediation-proposals":
        return _valid_remediation_proposals(value, date)
    if name == "skill-curate":
        return _valid_skill_curate(value, date=date, allow_legacy_v1=allow_legacy_v1)
    validator = _ARTIFACT_CONTRACTS.get(name)
    if validator is not None:
        return validator(value)
    # Unknown required names still need a non-empty object.  The path and file
    # name are validated by ``validate_required_artifacts`` below.
    return True

