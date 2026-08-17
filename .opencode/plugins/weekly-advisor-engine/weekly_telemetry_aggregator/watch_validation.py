"""Deterministic validation of the weekly watch findings hand-off.

The watch review is an LLM step, so its raw JSON is not treated as a trusted
final artefact.  This module validates one raw findings snapshot against the
dated watch context produced for the same run.  It intentionally reads no
OpenCode configuration, telemetry, logs, network resource, or previous final
findings file: the two arguments are the complete input to the validation.

The functions return JSON-compatible values and do not keep process or
cross-run state.  This makes the validator useful both from the CLI and from
unit tests with hand-written snapshots.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .watch_context import normalize_npm_package, normalize_repo_url

INPUT_CATEGORIES = frozenset({"adopt", "improve-existing", "token-saver", "ignore"})
EXISTING_STATES = frozenset({"absent", "declared", "observed", "unknown"})

_DECISION_BY_STATE = {
    "absent": "adopt",
    "declared": "verify-existing",
    "observed": "improve-existing",
    "unknown": "verify-existing",
}
_STATE_PRIORITY = {"declared": 0, "observed": 1, "unknown": 2, "absent": 3}
_RESERVED_TOP_LEVEL_KEYS = {
    "schema_version",
    "findings",
    "rejected_findings",
    "validation",
}


def _casefold(value: str) -> str:
    return value.strip().casefold()


def _non_empty_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _is_values(value: object) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))


def _subject_identities(subject: object) -> set[tuple[str, str]]:
    """Extract exact, typed identities from a finding subject.

    A string is intentionally accepted for backwards-compatible hand-written
    findings.  It is tried as an npm package, repository URL/full name, and
    plain name.  Object subjects are restricted to the three documented
    identity fields; extra descriptive fields are retained in the finding but
    do not affect matching.
    """

    values: list[tuple[str, object]]
    if isinstance(subject, str):
        values = [("npm_package", subject), ("repo_url", subject), ("name", subject)]
    elif isinstance(subject, Mapping):
        values = [
            (key, subject.get(key)) for key in ("npm_package", "repo_url", "name") if key in subject
        ]
    else:
        return set()

    identities: set[tuple[str, str]] = set()
    for key, raw_value in values:
        text = _non_empty_text(raw_value)
        if text is None:
            continue
        if key == "npm_package":
            normalized = normalize_npm_package(text)
            if normalized is not None:
                identities.add((key, normalized))
        elif key == "repo_url":
            normalized = normalize_repo_url(text)
            if normalized is not None:
                identities.add((key, normalized))
        else:
            identities.add((key, _casefold(text)))
    return identities


def _market_item_identities(item: Mapping[str, Any]) -> set[tuple[str, str]]:
    """Extract the same exact identity set from one context market item."""

    identities: set[tuple[str, str]] = set()
    normalized = item.get("normalized")
    normalized_values = normalized if isinstance(normalized, Mapping) else {}

    npm_candidates = (item.get("npm_package"), normalized_values.get("npm_package"))
    for value in npm_candidates:
        normalized_npm = normalize_npm_package(value if isinstance(value, str) else None)
        if normalized_npm is not None:
            identities.add(("npm_package", normalized_npm))

    repo_candidates = (item.get("repo_url"), normalized_values.get("repo_url"))
    for value in repo_candidates:
        normalized_repo = normalize_repo_url(value if isinstance(value, str) else None)
        if normalized_repo is not None:
            identities.add(("repo_url", normalized_repo))

    name = _non_empty_text(item.get("name"))
    if name is not None:
        identities.add(("name", _casefold(name)))
        if not any(identity[0] == "npm_package" for identity in identities):
            found_via = item.get("found_via")
            if _is_values(found_via) and any(
                str(source).startswith("npm:") for source in found_via
            ):
                npm_package = normalize_npm_package(name)
                if npm_package is not None:
                    identities.add(("npm_package", npm_package))
        if not any(identity[0] == "repo_url" for identity in identities):
            found_via = item.get("found_via")
            if _is_values(found_via) and any(
                str(source).startswith("github:") for source in found_via
            ):
                repo_url = normalize_repo_url(name)
                if repo_url is not None:
                    identities.add(("repo_url", repo_url))
    return identities


def _market_sort_key(item: Mapping[str, Any]) -> tuple[str, str, str, str]:
    state = item.get("existing_state")
    state_text = state if isinstance(state, str) and state in _STATE_PRIORITY else "unknown"
    return (
        str(_STATE_PRIORITY[state_text]),
        _casefold(str(item.get("name") or "")),
        _casefold(str(item.get("npm_package") or "")),
        _casefold(str(item.get("repo_url") or "")),
    )


def match_subject_to_market(
    subject: object,
    market_matches: Sequence[object] | None,
) -> dict[str, Any] | None:
    """Return the exact context item identified by ``subject``.

    Matching is local and deterministic.  npm package names and repository
    URLs use the same canonicalization as :mod:`watch_context`; names are
    compared case-insensitively.  The helper never performs substring or
    network matching.  If duplicate context entries identify the same subject,
    an existing declaration wins over an observation, which wins over unknown
    and absent states.
    """

    identities = _subject_identities(subject)
    if not identities or not _is_values(market_matches):
        return None

    candidates: list[dict[str, Any]] = []
    for item in market_matches:
        if not isinstance(item, Mapping):
            continue
        if identities.isdisjoint(_market_item_identities(item)):
            continue
        candidates.append(dict(item))
    if not candidates:
        return None
    candidates.sort(key=_market_sort_key)
    return copy.deepcopy(candidates[0])


def _rejection(index: int | None, finding: object, reason: str) -> dict[str, Any]:
    """Build one deterministic rejection while retaining the original value."""

    return {
        "index": index,
        "reason": reason,
        "finding": copy.deepcopy(finding),
    }


def _raw_items(raw_findings: object) -> tuple[dict[str, Any], list[object], list[dict[str, Any]]]:
    """Split a raw report into metadata, candidate values, and root rejections."""

    if isinstance(raw_findings, Mapping):
        metadata = {
            str(key): copy.deepcopy(value)
            for key, value in raw_findings.items()
            if str(key) not in _RESERVED_TOP_LEVEL_KEYS
        }
        raw_values = raw_findings.get("findings", [])
        if raw_values is None:
            return metadata, [], []
        if _is_values(raw_values):
            return metadata, list(raw_values), []
        return metadata, [], [_rejection(None, raw_values, "findings must be an array")]

    return {}, [], [_rejection(None, raw_findings, "findings root must be an object")]


def _context_matches(context: Mapping[str, Any] | None) -> Sequence[object]:
    if not isinstance(context, Mapping):
        return ()
    matches = context.get("market_matches")
    return matches if _is_values(matches) else ()


def _existing_state(market_match: Mapping[str, Any] | None) -> str:
    if market_match is None:
        return "unknown"
    state = market_match.get("existing_state")
    return state if isinstance(state, str) and state in EXISTING_STATES else "unknown"


def _finding_error(finding: Mapping[str, Any]) -> str | None:
    category = finding.get("category")
    errors: list[str] = []
    if not isinstance(category, str) or category not in INPUT_CATEGORIES:
        errors.append("invalid category")
    for field in ("description", "recommendation"):
        if _non_empty_text(finding.get(field)) is None:
            errors.append(f"missing required field: {field}")

    subject = finding.get("subject")
    has_subject = "subject" in finding
    subject_is_valid = bool(_subject_identities(subject))
    # Market recommendations need an explicit subject so the deterministic
    # validator can prove whether the item already exists.  `ignore` remains
    # subject-less for noise findings and backwards compatibility.
    if category in {"adopt", "improve-existing", "token-saver"} and not subject_is_valid:
        errors.append("market finding requires subject identity")
    elif has_subject and not subject_is_valid:
        errors.append("invalid subject identity")
    return "; ".join(errors) if errors else None


def _validate_one_finding(
    finding: object,
    index: int,
    market_matches: Sequence[object],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, bool, bool]:
    """Validate and enrich one raw value.

    Returns ``(accepted, rejected, was_downgraded, was_matched)``.  The return
    shape keeps the aggregation in :func:`validate_findings` simple and makes
    all decisions local to one input finding.
    """

    if not isinstance(finding, Mapping):
        return None, _rejection(index, finding, "finding must be an object"), False, False

    error = _finding_error(finding)
    if error is not None:
        return None, _rejection(index, finding, error), False, False

    validated = copy.deepcopy(dict(finding))
    subject = validated.get("subject")
    subject_identities = _subject_identities(subject)
    if not subject_identities:
        return validated, None, False, False

    market_match = match_subject_to_market(subject, market_matches)
    state = _existing_state(market_match)
    category = str(validated["category"])
    validated["existing_state"] = state
    decision = category
    downgraded = False
    if category == "adopt":
        decision = _DECISION_BY_STATE[state]
    elif category == "token-saver":
        decision = "token-saver" if state == "absent" else _DECISION_BY_STATE[state]
    if category in {"adopt", "token-saver"} and decision != category:
        validated["category"] = decision
        downgraded = True
    validated["decision"] = decision
    if market_match is not None:
        validated["capability_state"] = market_match.get("capability_state", "unknown")
        validated["match"] = copy.deepcopy(market_match.get("match"))
        match = market_match.get("match")
        if isinstance(match, Mapping):
            validated["current_location"] = match.get("path")
        validated["confidence"] = "high"
    else:
        validated["confidence"] = "low" if state == "unknown" else "medium"
    return validated, None, downgraded, market_match is not None


def validate_findings(
    raw_findings: Mapping[str, Any] | None,
    context: Mapping[str, Any] | None,
    *,
    date: str | None = None,
) -> dict[str, Any]:
    """Validate one raw findings snapshot against one watch context.

    Args:
        raw_findings: Raw report object (or legacy bare findings array).
        context: The dated ``weekly-watch-context`` object.  Only its
            ``market_matches`` list is consulted.
        date: Optional anchor date to preserve/override the output date.

    Returns:
        A schema-version-2 JSON-compatible final findings report.  Invalid
        individual values are retained under ``rejected_findings`` rather than
        raising an exception.  No timestamp is generated here, so identical
        inputs produce identical output.
    """

    metadata, raw_items, rejected = _raw_items(raw_findings)
    market_matches = _context_matches(context)
    accepted: list[dict[str, Any]] = []
    downgraded_count = 0
    matched_count = 0
    unknown_count = 0

    for index, raw_item in enumerate(raw_items):
        valid, invalid, downgraded, matched = _validate_one_finding(raw_item, index, market_matches)
        if invalid is not None:
            rejected.append(invalid)
            continue
        if valid is None:  # Defensive guard; _validate_one_finding always returns one branch.
            rejected.append(_rejection(index, raw_item, "finding could not be validated"))
            continue
        accepted.append(valid)
        downgraded_count += int(downgraded)
        matched_count += int(matched)
        if "existing_state" in valid and valid["existing_state"] == "unknown":
            unknown_count += 1

    counts = {
        "input": len(raw_items),
        "accepted": len(accepted),
        "rejected": len(rejected),
        "downgraded": downgraded_count,
        "market_matched": matched_count,
        "market_unknown": unknown_count,
    }
    validation = {
        "status": "ok" if not rejected else "degraded",
        "summary": (
            f"{counts['accepted']} accepted, {counts['rejected']} rejected, "
            f"{counts['downgraded']} downgraded"
        ),
        "counts": counts,
    }

    result: dict[str, Any] = {"schema_version": 2, **metadata}
    if date is not None:
        result["date"] = date
    result["findings"] = accepted
    result["rejected_findings"] = rejected
    result["validation"] = validation
    return result


def load_watch_json(path: Path) -> tuple[object | None, str | None]:
    """Load one local JSON report without consulting fallback/global paths."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, f"missing input: {path}"
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, f"cannot read input {path}: {exc}"
    return payload, None


def load_watch_context(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """Load a context object from exactly ``path``."""

    payload, error = load_watch_json(path)
    if error is not None:
        return None, error
    if not isinstance(payload, Mapping):
        return None, f"context root must be an object: {path}"
    return dict(payload), None


def load_raw_findings(path: Path) -> tuple[Mapping[str, Any] | None, str | None]:
    """Load the object-shaped raw findings contract from exactly ``path``."""

    payload, error = load_watch_json(path)
    if error is not None:
        return None, error
    if isinstance(payload, Mapping):
        return dict(payload), None
    return None, f"raw findings root must be an object: {path}"
