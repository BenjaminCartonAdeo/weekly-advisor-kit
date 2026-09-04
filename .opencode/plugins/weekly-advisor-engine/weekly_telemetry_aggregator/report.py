"""Deterministic report rendering — report-prep + report-assemble (Partie 7).

`report-prep` renders every section that comes from JSON artefacts + git log
(deterministic, no LLM) into a draft; section 4 (qualitative findings) is left
as `<!-- QUALITY_BLOCK -->` for the agent. `report-assemble` injects the LLM
block file (`weekly-report-blocks-<date>.md`) into the draft and produces the
final `weekly-report-<date>.md`. Missing blocks => explicit placeholder, never
a silent gap.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import timedelta
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import __version__
from .config import TelemetryConfig
from .harness_scope import harness_digest_problems
from .html_report import open_html_report, render_html_report
from .insights import flatten_harness_findings
from .run_state import active_run_meta, resolve_active_run_dir
from .util import iso as _iso
from .util import load_json as _load_json
from .util import parse_anchor as _parse_anchor
from .util import parse_iso_ts
from .util import read_text as _load_text


def _git_log_raw(project_root: Path, *args: str) -> list[str]:
    """Lines of `git log --grep=auto-rédigé, revue hebdo <args>`; [] on any failure."""
    if project_root is None or not (project_root / ".git").exists():
        return []
    try:
        proc = subprocess.run(
            ["git", "-C", str(project_root), "log", "--grep=auto-rédigé, revue hebdo", *args],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    return proc.stdout.splitlines()


def _git_log(project_root: Path, since_iso: str, until_iso: str | None = None) -> list[dict]:
    """Auto-redige commits inside the window: [{hash, date, subject}] (v6.0.l, E1).

    Two v6.0.l fixes: the window is now bounded by ``--until`` (previously the
    log ran from window start to *now*, so commits of later runs — even the
    current run's own draft — were counted "sur la fenêtre"); and the match is
    enforced on the **subject** in Python because ``--grep`` matches full commit
    messages (a spec/doc commit whose body mentions the phrase was counted).
    """
    args = ["--since=" + since_iso, "--format=%h|%ad|%s", "--date=short"]
    if until_iso:
        args.append("--until=" + until_iso)
    rows = []
    for line in _git_log_raw(project_root, *args):
        parts = line.split("|", 2)
        if len(parts) == 3 and "auto-rédigé, revue hebdo" in parts[2]:
            rows.append({"hash": parts[0], "date": parts[1], "subject": parts[2]})
    return rows


def _pending_auto_commits(project_root: Path, cutoff_iso: str) -> int:
    return len(
        [
            line
            for line in _git_log_raw(project_root, "--before=" + cutoff_iso, "--format=%H")
            if line.strip()
        ]
    )


def _self_cost_value(cfg: TelemetryConfig) -> dict | None:
    """Advisor session info {cost, tokens} for the report; None when undetectable."""
    from .costing import advisor_cost
    from .sqlite_reader import DataSourceError

    try:
        return advisor_cost(cfg)
    except DataSourceError:
        return None


def _top_models(summary: dict, limit: int = 3) -> list[dict]:
    """Top-N by cost with <5% models fused into 'autres' (spec: by code, not LLM)."""
    # v6.0.l (E9) : les lignes 0 token ET 0 coût sont des fantômes de sélection
    # (sessions aux steps vides) — elles n'apportent aucun signal au top modèles.
    models = [
        m
        for m in summary.get("by_model", [])
        if m.get("total_tokens", 0) > 0 or m.get("total_cost_usd", 0.0) > 0
    ]
    models.sort(key=lambda m: (-m.get("total_cost_usd", 0.0), m.get("model", "")))
    total_cost = sum(m.get("total_cost_usd", 0.0) for m in models)
    if total_cost <= 0:
        return models
    top, others_cost = [], 0.0
    others_tokens, others_sessions = 0, 0
    for m in models:
        share = m.get("total_cost_usd", 0.0) / total_cost
        if len(top) < limit or share >= 0.05:
            top.append(m)
        else:
            others_cost += m.get("total_cost_usd", 0.0)
            others_tokens += m.get("total_tokens", 0)
            others_sessions += m.get("session_count", 0)
    if len(models) > len(top):
        top.append(
            {
                "model": "autres",
                "total_cost_usd": round(others_cost, 6),
                "total_tokens": others_tokens,
                "session_count": others_sessions,
            }
        )
    return top


def _complete_daily(period: dict, daily: list[dict]) -> list[dict]:
    """Tous les jours de la fenêtre, zéro explicite (v5.30, 10).

    Le bucketing ne produit que les jours avec activité — le lecteur pouvait croire
    à des trous de données. On complète la période avec des entrées à zéro.
    """
    start = parse_iso_ts(period.get("start"))
    end = parse_iso_ts(period.get("end"))
    if start is None or end is None:
        return daily
    start = start.date()
    end = end.date()
    by_date = {d.get("date"): d for d in daily}
    out: list[dict] = []
    cur = start
    while cur <= end:
        out.append(
            by_date.get(
                cur.isoformat(),
                {
                    "date": cur.isoformat(),
                    "cost_usd": 0.0,
                    "total_tokens": 0,
                    "cache_hit_rate": None,
                },
            )
        )
        cur += timedelta(days=1)
    return out


def _group_warnings(warnings: list[dict]) -> list[dict]:
    """Regroupe les warnings identiques (message) — annexe lisible (v5.30, F)."""
    grouped: dict[str, dict] = {}
    for w in warnings:
        msg = w.get("message", "")
        entry = grouped.setdefault(msg, {"message": msg, "count": 0, "session_ids": []})
        entry["count"] += 1
        sid = w.get("session_id") or "global"
        if sid not in entry["session_ids"]:
            entry["session_ids"].append(sid)
    return list(grouped.values())


def _top_harness_rules(
    digest: dict | None, ignored_rules: list[str], n: int = 5
) -> list[tuple[str, int]]:
    """Top-N most-violated harness rules, ignored rules excluded."""
    ignored = set(ignored_rules)
    return Counter(
        f["rule"] for f in flatten_harness_findings(digest) if f.get("rule") not in ignored
    ).most_common(n)


def _critical_security_findings(digest: object) -> list[dict]:
    """Return critical security findings, preserving deterministic provenance."""
    if not isinstance(digest, dict):
        return []

    # Do not let a present (possibly empty) top-level ``findings`` array hide
    # findings attached to inspection components.  harness-eval has emitted
    # both shapes over time.  The recursive walk is deliberately conservative:
    # a mapping is a finding only when it carries a rule/id and at least one
    # finding field, so metadata such as ``rules`` is not misclassified.
    findings: list[dict] = []
    seen: set[int] = set()

    def walk(value: object) -> None:
        if isinstance(value, Mapping):
            identity = id(value)
            if identity in seen:
                return
            seen.add(identity)
            rule = value.get("rule") or value.get("id")
            if rule is not None and any(
                key in value for key in ("severity", "message", "detail", "result")
            ):
                findings.append(dict(value))
            for nested in value.values():
                walk(nested)
        elif isinstance(value, list):
            for nested in value:
                walk(nested)

    walk(digest)
    if not findings:
        # Keep compatibility with the normalized harness shape if a future
        # producer uses a finding record without one of the marker fields.
        findings = flatten_harness_findings(digest)
    return [
        finding
        for finding in findings
        if (
            str(finding.get("severity") or "").lower() == "critical"
            and str(finding.get("rule") or finding.get("id") or "")
            .lower()
            .startswith("security/")
        )
        or _is_blocking_security_rule(finding.get("rule") or finding.get("id"))
    ]


_NONBLOCKING_WARNING_MARKERS = (
    "transcript-truncated:",
    "transcript_truncated:",
    "recovered:",
    "recovered ",
    "récupéré:",
    "récupérée:",
    "récupération:",
)
_RECOVERABLE_INPUT_MARKERS = {
    "watch",
    "harness",
    "proposal",
    "proposals",
    "ecosystem",
}
_REFUSAL_STATUSES = {"error", "rejected", "ambiguous", "unverified", "refused", "failure"}


_RECOVERABLE_ARTIFACT_STEMS: dict[str, tuple[str, ...]] = {
    "watch": (
        "weekly-watch-findings",
        "weekly-watch-context",
        "weekly-ecosystem",
        "watch-candidates",
    ),
    "ecosystem": ("weekly-ecosystem",),
    "harness": (
        "weekly-harness-digest",
        "weekly-harness-remediation",
        "weekly-harness-remediation-proposals",
    ),
    "proposal": (
        "weekly-harness-remediation-proposals",
        "weekly-harness-remediation",
    ),
}


def _path_is_outside_worktree(target: object, worktree: object) -> bool:
    """Prove that an absolute target is outside a worktree.

    Permission refusals are report-only only after this check succeeds.  A
    missing, relative or unresolvable path is deliberately *not* considered
    external: fail closed rather than turning an unverified refusal into a
    successful run.
    """
    if not isinstance(target, (str, Path)) or not isinstance(worktree, (str, Path)):
        return False
    target_text = str(target).strip()
    worktree_text = str(worktree).strip()
    if not target_text or not worktree_text:
        return False
    try:
        target_path = Path(target_text).expanduser()
        worktree_path = Path(worktree_text).expanduser()
        if not target_path.is_absolute() or not worktree_path.is_absolute():
            return False
        target_path = target_path.resolve(strict=False)
        worktree_path = worktree_path.resolve(strict=False)
    except (OSError, RuntimeError):
        return False
    try:
        target_path.relative_to(worktree_path)
    except ValueError:
        return True
    return False


def _record_field(record: Mapping[str, object], *keys: str) -> object | None:
    for key in keys:
        value = record.get(key)
        if value is not None:
            if isinstance(value, Mapping):
                nested = value.get("path") or value.get("target")
                if nested is not None:
                    return nested
            return value
    return None


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
    if name == "weekly-summary":
        period = value.get("period")
        return (
            _valid_schema_version(value.get("schema_version"), 2)
            and isinstance(period, Mapping)
            and _nonempty_text(period.get("start"))
            and _nonempty_text(period.get("end"))
            and _nonempty_text(value.get("generated_at"))
            and isinstance(value.get("totals"), Mapping)
        )
    if name == "weekly-insights":
        return (
            _valid_schema_version(value.get("schema_version"), 1)
            and isinstance(value.get("period"), Mapping)
            and _nonempty_text(value.get("generated_at"))
            and isinstance(value.get("deltas"), Mapping)
            and isinstance(value.get("alerts"), list)
            and isinstance(value.get("maintenance"), Mapping)
        )
    if name == "weekly-harness-digest":
        inspection = value.get("inspection")
        return (
            isinstance(inspection, Mapping) and bool(inspection)
        ) or (
            isinstance(value.get("rules"), list) and bool(value["rules"])
        ) or (
            isinstance(value.get("findings"), list) and bool(value["findings"])
        )
    if name == "weekly-ecosystem":
        return (
            _valid_schema_version(value.get("schema_version"), 2)
            and isinstance(value.get("new_items"), list)
            and isinstance(value.get("core_changes"), list)
            and isinstance(value.get("warnings"), list)
        )
    if name in {"weekly-quality-findings", "weekly-watch-findings-raw"}:
        return isinstance(value.get("findings"), list)
    if name == "weekly-coherence-findings":
        return isinstance(value.get("findings"), list) or isinstance(
            value.get("curation_signal"), (list, Mapping)
        )
    if name == "weekly-audit-candidates":
        return (
            _valid_schema_version(value.get("schema_version"), 1)
            and isinstance(value.get("audited"), list)
            and isinstance(value.get("unaudited"), list)
            and isinstance(value.get("limit"), int)
            and not isinstance(value.get("limit"), bool)
        )
    if name == "weekly-watch-context":
        return _valid_schema_version(value.get("schema_version"), 1) and isinstance(
            value.get("market_matches"), list
        )
    if name == "weekly-watch-findings":
        return (
            _valid_schema_version(value.get("schema_version"), 2)
            and isinstance(value.get("findings"), list)
            and isinstance(value.get("validation"), Mapping)
        )
    if name == "watch-candidates":
        return (
            _valid_schema_version(value.get("schema_version"), 1)
            and isinstance(value.get("candidates"), list)
            and isinstance(value.get("security_annex"), list)
        )
    if name == "weekly-harness-remediation":
        return isinstance(value.get("summary"), Mapping) and isinstance(
            value.get("postcheck"), Mapping
        )
    if name == "weekly-harness-remediation-proposals":
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
    if name == "skill-curate":
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
    if name == "weekly-timings":
        return isinstance(value.get("branches"), Mapping) or isinstance(
            value.get("steps"), (list, Mapping)
        )
    # Unknown required names still need a non-empty object.  The path and file
    # name are validated by ``validate_required_artifacts`` below.
    return True


def _text_values(value: object) -> Iterable[str]:
    """Yield searchable text from one warning/manifest record."""
    if isinstance(value, str):
        yield value
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(item, (str, int, float, bool)):
                yield f"{key}={item}"
            elif isinstance(item, (Mapping, list)):
                yield from _text_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from _text_values(item)


def _record_text(record: object) -> str:
    return " ".join(_text_values(record)).casefold()


def _is_report_only_record(
    record: object, *, project_root: Path | str | None = None
) -> bool:
    """Recognize external, report-only permission outcomes.

    A bare ``permission denied`` or a producer-supplied ``report_only`` flag is
    intentionally *not* enough.  The record must identify a permission refusal
    and an absolute target that is provably outside the worktree.
    """
    if not isinstance(record, Mapping) or project_root is None:
        return False
    if record.get("report_only") is not True:
        return False
    if str(record.get("status") or "").casefold() != "report-only":
        return False
    if str(record.get("category") or "").casefold() != "external-permission-refusal":
        return False
    refusal_marker = _record_field(
        record,
        "permission_refused",
        "permission_denied",
        "permission_error",
        "external_permission_refusal",
    )
    if refusal_marker is not True and str(refusal_marker or "").casefold() not in {
        "permission denied",
        "permission refused",
        "external permission refusal",
    }:
        return False
    target = _record_field(
        record,
        "target",
        "target_path",
        "path",
        "destination",
        "output_path",
        "html_report_dir",
    )
    # The configured project root is trusted; worker-supplied roots are not.
    return _path_is_outside_worktree(target, project_root)


def _is_external_permission_failure(cfg: TelemetryConfig, exc: BaseException) -> bool:
    """Whether an HTML/report permission error targets outside the worktree."""
    if not isinstance(exc, PermissionError):
        return False
    project_root = getattr(cfg, "project_root", None)
    configured = getattr(cfg, "html_report_dir", None)
    if not project_root or not configured:
        return False
    try:
        root = Path(project_root).expanduser().resolve(strict=False)
        target = Path(configured).expanduser()
        if not target.is_absolute():
            target = root / target
        target = target.resolve(strict=False)
    except (OSError, RuntimeError):
        return False
    return _path_is_outside_worktree(target, root)


def _is_recovered_record(record: object) -> bool:
    """Return whether a warning describes a successfully recovered input."""
    if not isinstance(record, Mapping):
        return False
    if record.get("recovered") is True or record.get("recovery") in (True, "ok", "success"):
        return True
    status = str(record.get("status") or "").casefold()
    if status in {"recovered", "recovered-input", "fallback-recovered"}:
        return True
    text = _record_text(record)
    return (
        any(marker in text for marker in _NONBLOCKING_WARNING_MARKERS[2:])
        or "recovered" in text
        or "fallback" in text
        or "récupér" in text
    ) and any(
        marker in text for marker in _RECOVERABLE_INPUT_MARKERS
    )


def _artifact_entry_valid(entry: object) -> bool:
    """Check the permissive artifact-input shapes used by joins and reports."""
    if not isinstance(entry, Mapping):
        return False
    status = str(entry.get("status") or "").casefold()
    if status in {"present", "valid", "ok", "recovered"}:
        return entry.get("valid", True) is not False
    return entry.get("present") is True or entry.get("valid") is True


def _summary_artifact_inputs(summary: object) -> Mapping[str, object]:
    if not isinstance(summary, Mapping):
        return {}
    inputs = summary.get("artifact_inputs")
    return inputs if isinstance(inputs, Mapping) else {}


def _audit_envelope_valid(value: object, session_id: str) -> bool:
    """Validate one worker audit envelope at its canonical run-local path."""
    if not isinstance(value, Mapping):
        return False
    rc = _coerce_rc(value.get("rc"), default=None)
    return (
        _valid_schema_version(value.get("schema_version"), 1)
        and value.get("session_id") == session_id
        and _nonempty_text(value.get("summary"))
        and isinstance(value.get("findings"), list)
        and rc in {0, 1}
        and isinstance(value.get("warnings"), list)
    )


def _audit_declaration_values(record: object) -> list[str]:
    """Return structured audit artifact declarations without parsing text."""
    if not isinstance(record, Mapping):
        return []
    values: list[str] = []

    def collect(value: object) -> None:
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, Mapping):
            for key in ("path", "artifact", "artifact_path", "audit_artifact", "audit_artifact_path"):
                if key in value:
                    collect(value[key])
        elif isinstance(value, list):
            for item in value:
                collect(item)

    for key in (
        "audit_artifact",
        "audit_artifact_path",
        "artifact",
        "artifact_path",
        "artifacts",
        "required_artifact",
        "required_artifacts",
    ):
        if key in record:
            collect(record[key])
    return values


def _canonical_audit_path(out: Path, session_id: str) -> Path | None:
    """Build the only accepted audit path for one session id."""
    if not session_id or "/" in session_id or "\\" in session_id or session_id in {".", ".."}:
        return None
    path = out / f"audit-findings-{session_id}.json"
    try:
        if path.resolve(strict=False).parent != out.resolve(strict=False):
            return None
    except (OSError, RuntimeError):
        return None
    return path


def _audit_declaration_matches(out: Path, session_id: str, record: object) -> bool:
    """Require a declaration equal to the exact canonical run-local filename."""
    canonical = _canonical_audit_path(out, session_id)
    if canonical is None:
        return False
    expected_name = canonical.name
    try:
        expected_path = str(canonical.resolve(strict=False))
    except (OSError, RuntimeError):
        return False
    return any(
        declaration in (expected_name, expected_path)
        for declaration in _audit_declaration_values(record)
    )


def _audit_artifact_valid(
    out: Path | None,
    date: str | None,
    session_id: object,
    record: object | None = None,
) -> bool:
    """Validate the declared audit envelope at its exact run-local path."""
    del date  # audit filenames are session-scoped, not date-scoped
    if out is None or not isinstance(session_id, str):
        return False
    path = _canonical_audit_path(out, session_id)
    if path is None or not _audit_declaration_matches(out, session_id, record):
        return False
    data, state = _json_file_state(path)
    return state == "present" and _audit_envelope_valid(data, session_id)


def _recoverable_artifact_valid(
    out: Path | None,
    date: str | None,
    source_text: str,
    artifact: object | None = None,
) -> bool:
    """Validate the named recovered artifact on disk, using its exact schema."""
    if out is None or not _valid_date(date):
        return False
    source = source_text.casefold()
    source_kind = next(
        (kind for kind in ("ecosystem", "watch", "proposal", "harness") if kind in source),
        None,
    )
    if source_kind is None:
        return False
    allowed = set(_RECOVERABLE_ARTIFACT_STEMS[source_kind])
    if isinstance(artifact, list):
        return any(
            _recoverable_artifact_valid(out, date, source_text, candidate)
            for candidate in artifact
        )
    if not isinstance(artifact, str) or not artifact.strip():
        # Recovery must identify the replacement.  Guessing from any valid
        # artifact in the run lets unrelated branch output mask a missing one.
        return False
    reference = artifact.strip().replace("\\", "/")
    reference_path = Path(reference)
    if reference_path.is_absolute():
        try:
            reference_path = reference_path.resolve(strict=False)
            run_root = out.resolve(strict=False)
            reference_path.relative_to(run_root)
            if reference_path.parent != run_root:
                return False
        except (OSError, RuntimeError, ValueError):
            return False
        filename = reference_path.name
    else:
        if "/" in reference or reference.startswith("."):
            return False
        filename = reference
    if filename.endswith(".json"):
        filename = filename[:-5]
    suffix = f"-{date}"
    if filename.endswith(suffix):
        filename = filename[: -len(suffix)]
    if filename not in allowed:
        return False
    path = out / f"{filename}-{date}.json"
    try:
        if path.resolve(strict=False).parent != out.resolve(strict=False):
            return False
    except (OSError, RuntimeError):
        return False
    value, state = _json_file_state(path)
    return state == "present" and _artifact_contract_valid(filename, value, date=date)


def _truncated_record_valid(
    record: object, summary: object, out: Path | None, date: str | None
) -> bool:
    """Validate a truncated audit via disk or a declared artifact input."""
    del summary
    session_id = record.get("session_id") if isinstance(record, Mapping) else None
    return _audit_artifact_valid(out, date, session_id, record)


def _join_status_records(payload: object) -> list[object]:
    """Extract worker/join status records without imposing one schema version."""
    if not isinstance(payload, Mapping):
        return []
    records: list[object] = []
    for key in ("warnings", "worker_statuses", "statuses", "contracts"):
        value = payload.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, str) and payload.get("artifacts"):
                    # Keep the exact artifact declaration attached to a string
                    # warning; otherwise recovery validation would have to
                    # guess which branch output was recovered.
                    records.append({"message": item, "artifacts": payload["artifacts"]})
                else:
                    records.append(item)
        elif isinstance(value, Mapping):
            records.append(value)
    for key in ("branches", "workers", "join"):
        value = payload.get(key)
        if isinstance(value, Mapping):
            for record in value.values():
                if isinstance(record, Mapping):
                    records.append(record)
                    records.extend(_join_status_records(record))
    return records


def _audit_artifact_declarations(payload: object) -> list[str]:
    """Collect dynamic audit filenames from structured JOIN declarations."""
    declarations: list[str] = []

    def visit(value: object) -> None:
        if isinstance(value, Mapping):
            for key in (
                "audit_artifact",
                "audit_artifact_path",
                "artifact",
                "artifact_path",
                "artifacts",
                "required_artifact",
                "required_artifacts",
            ):
                item = value.get(key)
                if isinstance(item, str):
                    if item.startswith("audit-findings-") or "audit-findings-" in item:
                        declarations.append(item)
                elif isinstance(item, list):
                    for candidate in item:
                        if isinstance(candidate, str) and (
                            candidate.startswith("audit-findings-")
                            or "audit-findings-" in candidate
                        ):
                            declarations.append(candidate)
            for nested in value.values():
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    visit(payload)
    return declarations


def _declared_run_filename(item: str, out: Path | None = None) -> str | None:
    """Return a declaration filename only when its run-local path is exact."""
    if not item or "\\" in item:
        return None
    path = Path(item)
    if path.is_absolute():
        if out is None:
            return None
        try:
            resolved = path.resolve(strict=False)
            run_root = out.resolve(strict=False)
            if resolved.parent != run_root:
                return None
            return resolved.name
        except (OSError, RuntimeError):
            return None
    if "/" in item or item.startswith("."):
        return None
    return item


def _branch_applicability(
    payload: object, date: str, *, out: Path | None = None
) -> dict[str, bool]:
    """Extract exact branch artifact declarations from a timings/join payload."""
    applicable: dict[str, bool] = {}
    optional_names = {
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
    }

    def visit(value: object) -> None:
        if isinstance(value, Mapping):
            declared: list[object] = []
            for key in ("artifact", "artifacts", "required_artifact", "required_artifacts"):
                item = value.get(key)
                if isinstance(item, list):
                    declared.extend(item)
                elif item is not None:
                    declared.append(item)
            for item in declared:
                if not isinstance(item, str):
                    continue
                filename = _declared_run_filename(item, out)
                if filename is None:
                    continue
                match = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9_-]*)-(\d{4}-\d{2}-\d{2})\.json", filename)
                if match and match.group(2) == date and match.group(1) in optional_names:
                    applicable[match.group(1)] = True
            for nested in value.values():
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    visit(payload)
    return applicable


def _warning_is_nonblocking(
    warning: object,
    *,
    summary: object,
    out: Path | None = None,
    date: str | None = None,
    project_root: Path | str | None = None,
) -> bool:
    """Whether a warning is informational under the final JOIN contract."""
    if isinstance(warning, str):
        # A serialized warning has no trusted structure.  In particular, never
        # downgrade a run by substring-matching report-only fields in text.
        return False
    if not isinstance(warning, Mapping):
        return False
    report_only = _is_report_only_record(warning, project_root=project_root)
    if report_only or _is_recovered_record(warning):
        # Recovered/report-only inputs are only harmless when the replacement
        # input is observable and valid.  Explicit producers can point to it;
        # otherwise require a valid watch/harness/proposal artifact in the run.
        if report_only:
            return True
        source = _record_text(warning)
        artifact = _record_field(warning, "artifact", "artifact_path", "artifacts")
        return _recoverable_artifact_valid(out, date, source, artifact)

    text = _record_text(warning)
    transcript_prefix = next(
        (marker for marker in _NONBLOCKING_WARNING_MARKERS[:2] if marker in text), None
    )
    if transcript_prefix:
        match = text.split(transcript_prefix, 1)
        sid = match[1].split()[0].strip(" ,;)]") if len(match) == 2 else None
        transcript_record = dict(warning)
        transcript_record["session_id"] = sid
        return _truncated_record_valid(transcript_record, summary, out, date)

    # Optional inputs are observable but never required to make a report.  A
    # producer can mark the warning directly or identify the optional artifact.
    if warning.get("optional") is True or warning.get("applicable") is False:
        return True
    for key, value in _summary_artifact_inputs(summary).items():
        if not isinstance(value, Mapping) or value.get("required") is not False:
            continue
        if (
            (_artifact_entry_valid(value) or value.get("status") in {"absent", "not_applicable"})
            and (
                str(key).casefold() in text
                or str(value.get("path", "")).casefold() in text
            )
        ):
            return True
    return False


def applicable_summary_rc(
    summary: object,
    *,
    out: Path | None = None,
    date: str | None = None,
    additional_records: Iterable[object] = (),
    project_root: Path | str | None = None,
    fallback_rc: int | None = None,
) -> int:
    """Compute the summary process status without counting report-only facts.

    ``weekly_run`` and external joins historically surfaced every partial worker
    warning as ``1``.  The final contract treats valid transcript-truncated,
    recovered optional inputs and external report-only permissions as facts, not
    failures.  Fatal ``2`` is never downgraded.
    """
    if not isinstance(summary, Mapping):
        return 2
    raw_value = summary.get("rc")
    if raw_value is None:
        raw_value = summary.get("exit")
    if raw_value is None:
        # Generated summaries before the JOIN contract have no rc/exit field.
        # Callers that know the process result must pass it explicitly; direct
        # use without a fallback is partial, never an accidental success.
        raw = _coerce_rc(fallback_rc, default=None)
        if raw is None:
            raw = 1
    else:
        raw = _coerce_rc(raw_value, default=None)
    if raw is None:
        raw = 1

    warnings = summary.get("warnings")
    if not isinstance(warnings, list):
        warnings = []
    warnings = [
        warning
        for warning in warnings
        if not (isinstance(warning, Mapping) and warning.get("partial") is False)
    ]
    recovered_inputs = summary.get("recovered_inputs")
    if not isinstance(recovered_inputs, list):
        recovered_inputs = []
    for key in ("recovered_watch_inputs", "recovered_proposal_inputs"):
        value = summary.get(key)
        if isinstance(value, list):
            recovered_inputs.extend(value)
    report_only_permissions = summary.get("report_only_permissions")
    if not isinstance(report_only_permissions, list):
        report_only_permissions = []
    worker_statuses: list[object] = []
    status_sources = [summary.get("worker_statuses")]
    selection = summary.get("selection")
    if isinstance(selection, Mapping):
        status_sources.append(selection.get("worker_statuses"))
    for source in status_sources:
        if isinstance(source, list):
            worker_statuses.extend(
                item
                for item in source
                if not isinstance(item, Mapping)
                or _coerce_rc(item.get("rc"), default=0) not in (None, 0)
                or item.get("truncated") is True
                or item.get("worker_status") == "truncated"
                or item.get("status") in {"truncated", "error", "missing", "timeout"}
            )
    join_records = [
        record
        for record in additional_records
        if not (
            isinstance(record, Mapping)
            and _coerce_rc(record.get("rc"), default=0) == 0
            and not any(
                key in record
                for key in (
                    "warnings",
                    "truncated",
                    "worker_status",
                    "status",
                    "recovered",
                    "report_only",
                )
            )
        )
    ]
    records = [
        *warnings,
        *worker_statuses,
        *recovered_inputs,
        *report_only_permissions,
        *join_records,
    ]
    if raw >= 2 or any(
        isinstance(record, Mapping) and _coerce_rc(record.get("rc"), default=0) >= 2
        for record in records
    ):
        return 2
    if not records:
        inputs = _summary_artifact_inputs(summary)
        valid_input_statuses = {"absent", "not_applicable", "present", "valid", "recovered"}
        optional_entries = [
            value
            for value in inputs.values()
            if isinstance(value, Mapping) and value.get("required") is False
        ]
        blocking_inputs = [
            value
            for value in inputs.values()
            if isinstance(value, Mapping)
            and value.get("required") is True
            and str(value.get("status") or "").casefold() not in {"present", "valid", "ok"}
        ]
        optional_only = bool(optional_entries) and not blocking_inputs and all(
            str(value.get("status") or "").casefold() in valid_input_statuses
            for value in inputs.values()
            if isinstance(value, Mapping)
        )
        if blocking_inputs:
            return 1
        if optional_only:
            return 0
        return 0 if raw == 0 else 1
    all_nonblocking = all(
        _warning_is_nonblocking(
            item,
            summary=summary,
            out=out,
            date=date,
            project_root=project_root,
        )
        or (
            isinstance(item, Mapping)
            and (
                item.get("truncated") is True
                or item.get("worker_status") == "truncated"
                or item.get("status") == "truncated"
            )
            and _truncated_record_valid(item, summary, out, date)
        )
        for item in records
    )
    return 0 if all_nonblocking else 1


def _artifact_provenance(out: Path, date: str) -> dict[str, dict[str, object]]:
    """Describe report inputs without relying on mutable process state."""
    return validate_required_artifacts(out, date)["artifacts"]


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
    """Validate report inputs once, with deterministic required/optional gates.

    JSON inputs are read exactly once each.  Optional upstream artefacts remain
    visible when absent, but cannot make an otherwise usable report fail.  HTML
    is a conditional artefact: callers can pass ``html_path`` after rendering
    to distinguish a renderer failure from a missing or unreadable output.
    """
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
    # A branch explicitly marked applicable is required even when its producer
    # is normally optional.  Disabled branches stay visible in ``optional`` and
    # cannot fail the gate merely because they have no output.
    branch_required = {
        str(name) for name, enabled in (applicability or {}).items() if enabled is True
    }
    required_set = explicit_required | branch_required
    optional_names = tuple(name for name in names["optional"] if name not in required_set)
    names["optional"] = optional_names
    applicable = applicability or {}

    def check_json(name: str) -> dict[str, object]:
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
            # An absent optional producer is not evidence that its branch was
            # enabled and failed.  Existing files are applicable by definition;
            # callers can override this for explicitly enabled/disabled waves.
            "applicable": bool(applicable.get(name, applicable_default)),
        }

    required = {f"{name}-{date}.json": check_json(name) for name in sorted(required_set)}

    dynamic_required: dict[str, dict[str, object]] = {}
    for declaration in dynamic_audit_artifacts:
        raw_declaration = str(declaration) if isinstance(declaration, str) else ""
        filename = _declared_run_filename(raw_declaration, out)
        match = (
            re.fullmatch(r"audit-findings-(.+)\.json", filename)
            if filename is not None
            else None
        )
        key = filename or f"audit-findings-declaration-{len(dynamic_required)}"
        if key in dynamic_required:
            continue
        path = out / filename if filename is not None else out / raw_declaration
        sid = match.group(1) if match else ""
        data, state = _json_file_state(path) if match else (None, "ill_readable")
        valid = bool(match and _canonical_audit_path(out, sid) == path and _audit_envelope_valid(data, sid))
        dynamic_required[key] = {
            "path": str(path),
            "present": valid,
            "status": "present" if valid else ("absent" if state == "absent" else "ill_readable"),
            "required": True,
            "path_valid": bool(match and _canonical_audit_path(out, sid) == path),
            "schema_valid": valid,
            "applicable": True,
        }
    required.update(dynamic_required)
    optional = {f"{name}-{date}.json": check_json(name) for name in names["optional"]}
    def _check_html(path: Path | None) -> dict[str, object]:
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

    # P0 : un fichier HTML réellement produit prime sur le flag de config —
    # `disabled` uniquement si rendu off ET aucun fichier produit.
    if html_path is not None:
        html = _check_html(html_path)
    elif html_enabled:
        html = {"status": "absent", "path": None}
    else:
        html = {"status": "disabled", "path": None}
    required_status = (
        "pass" if required and all(a["status"] == "present" for a in required.values()) else "incomplete"
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


_BLOCKING_SECURITY_RULES = {
    "mcp-tool-poisoning",
    "unbounded-delegation",
    "memory-write-unscoped",
}


def _is_blocking_security_rule(rule: object) -> bool:
    """Match only the documented critical rule identifiers."""
    normalized = str(rule or "").strip().lower()
    return normalized in _BLOCKING_SECURITY_RULES


def _blocking_security_findings(digest: object) -> list[dict]:
    """Return only the exact security rules that block final status."""
    return [
        finding
        for finding in _critical_security_findings(digest)
        if _is_blocking_security_rule(finding.get("rule") or finding.get("id"))
    ]


def _gate_status(provenance: dict[str, dict[str, object]]) -> dict[str, object]:
    """Machine-readable artifact gate; missing optional inputs remain explicit."""
    artifacts = list(provenance.values())
    required = {
        name: artifact
        for name, artifact in provenance.items()
        if artifact.get("required", name.startswith("weekly-summary-"))
    }
    optional = {name: artifact for name, artifact in provenance.items() if name not in required}
    missing = [
        a["path"]
        for a in artifacts
        if a["status"] == "absent" and (a.get("required") or a.get("applicable", True))
    ]
    ill_readable = [
        a["path"]
        for a in artifacts
        if a["status"] == "ill_readable" and (a.get("required") or a.get("applicable", True))
    ]
    return {
        "required": required,
        "optional": optional,
        "artifacts": {
            "status": "pass" if all(a["status"] == "present" for a in required.values()) else "incomplete",
            "missing": missing,
            "ill_readable": ill_readable,
            "optional_missing": [
                a["path"]
                for a in artifacts
                if a["status"] == "absent"
                and not a.get("required")
                and a.get("applicable", True)
            ],
            "optional_ill_readable": [
                a["path"]
                for a in artifacts
                if a["status"] == "ill_readable"
                and not a.get("required")
                and a.get("applicable", True)
            ],
        },
        "prose": {"status": "not_validated"},
        "html": {"status": "not_run"},
        "blocking_rules": sorted(_BLOCKING_SECURITY_RULES),
    }


_CURATION_TAG_ACTIONS = {"archive", "merge", "pin", "reference", "delete", "recalibrate"}


def _coherence_has_curation_signal(coherence: object) -> bool:
    """Vrai si les findings de cohérence portent ≥1 action de curation.

    Accepte un dict (champ ``curation_signal`` ou ``findings[]``) ou une liste
    (findings bruts). Défensif : toute entrée illisible est ignorée.
    """
    if not coherence:
        return False
    if isinstance(coherence, dict):
        sig = coherence.get("curation_signal")
        if isinstance(sig, list) and sig:
            return True
        # R4 emits a mapping (rather than a list) for archive candidates.  Any
        # non-empty mapping is an actionable signal; do not require a specific
        # producer shape here so report gating remains forward-compatible.
        if isinstance(sig, dict) and sig:
            return True
        findings = coherence.get("findings") or []
    elif isinstance(coherence, list):
        findings = coherence
    else:
        return False
    return any(
        isinstance(f, dict) and f.get("tag_action") in _CURATION_TAG_ACTIONS for f in findings
    )


def _coherence_findings(coherence: object) -> list[dict]:
    """Return coherence findings in one deterministic shape for all reports."""
    if isinstance(coherence, list):
        return [finding for finding in coherence if isinstance(finding, dict)]
    if isinstance(coherence, dict):
        findings = coherence.get("findings")
        if isinstance(findings, list):
            return [finding for finding in findings if isinstance(finding, dict)]
    return []


def _curation_manifest_detail(manifest: object) -> dict:
    """Normalize curation v1/v2 manifests without changing their contracts."""
    if not isinstance(manifest, dict):
        return {"decisions": [], "skipped_details": [], "by_action": {}, "mode": None}
    decisions = manifest.get("decisions")
    skipped = manifest.get("skipped_details")
    summary = manifest.get("summary")
    normalized_decisions = (
        [item for item in decisions if isinstance(item, dict)]
        if isinstance(decisions, list)
        else []
    )
    decision_skips = [item for item in normalized_decisions if item.get("status") == "skipped"]
    raw_skips = (
        [item for item in skipped if isinstance(item, dict)]
        if isinstance(skipped, list)
        else decision_skips
    )
    # v2 carries skipped decisions in both arrays. Keep one rendered row.
    seen_skips: set[tuple[object, ...]] = set()
    skipped_details = []
    for item in raw_skips:
        key = tuple(
            item.get(field) for field in ("skill_id", "action", "source", "reason", "status")
        )
        if key not in seen_skips:
            seen_skips.add(key)
            skipped_details.append(item)
    by_action = summary.get("by_action") if isinstance(summary, dict) else None
    if not isinstance(by_action, dict):
        by_action = dict(
            sorted(Counter(str(item.get("action") or "") for item in normalized_decisions).items())
        )
    return {
        "decisions": normalized_decisions,
        "skipped_details": skipped_details,
        "by_action": by_action,
        "mode": manifest.get("mode"),
        "dry_run": manifest.get("dry_run"),
    }


def _curation_manifest_gate(
    manifest: object,
    *,
    date: str | None = None,
    project_root: Path | str | None = None,
) -> tuple[int, str | None]:
    """Return ``(rc, reason)`` for one curation manifest.

    Dry-run is a report-only proposal phase.  Its skipped/proposed decisions and
    even a non-fatal producer ``rc=1`` must not turn an otherwise valid report
    partial.  Apply refusals remain counted, except an explicitly external,
    report-only permission refusal.  A malformed manifest is always nonzero.
    """
    if not _artifact_contract_valid(
        "skill-curate", manifest, date=date, allow_legacy_v1=True
    ):
        return 2, "manifeste de curation malformé (schéma attendu absent ou invalide)"
    if not isinstance(manifest, Mapping):
        return 2, "manifeste de curation malformé (objet JSON attendu)"

    mode_raw = manifest.get("mode")
    mode = str(mode_raw).casefold() if mode_raw is not None else None
    if mode not in {"dry-run", "dry_run", "apply"}:
        return 2, f"manifeste de curation malformé (mode={mode_raw!r})"
    dry_run = manifest.get("dry_run")
    if not isinstance(dry_run, bool):
        return 2, "manifeste de curation malformé (dry_run non booléen)"
    if mode in {"dry-run", "dry_run"} and dry_run is False:
        return 2, "manifeste de curation malformé (dry-run incohérent)"
    if mode == "apply" and dry_run is True:
        return 2, "manifeste de curation malformé (apply avec dry_run=true)"

    decisions = manifest.get("decisions")
    if not isinstance(decisions, list) or any(not isinstance(item, Mapping) for item in decisions):
        return 2, "manifeste de curation malformé (decisions invalides)"
    summary = manifest.get("summary")
    if summary is not None and not isinstance(summary, Mapping):
        return 2, "manifeste de curation malformé (summary invalide)"
    raw_rc_value = manifest.get("rc", 0)
    raw_rc = _coerce_rc(raw_rc_value, default=None)
    if raw_rc is None:
        return 2, "manifeste de curation malformé (rc invalide)"
    if raw_rc >= 2:
        return 2, "manifeste de curation fatal (rc=2)"

    is_dry_run = mode in {"dry-run", "dry_run"} or dry_run is True
    if is_dry_run:
        return 0, None

    refusal_records: list[Mapping[str, object]] = []
    for decision in decisions or []:
        statuses = {
            str(decision.get("status") or "").casefold(),
            str(decision.get("move_status") or "").casefold(),
        }
        if statuses & _REFUSAL_STATUSES:
            refusal_records.append(decision)
    if raw_rc == 1 and refusal_records and all(
        _is_report_only_record(decision, project_root=project_root)
    ):
        return 0, None
    if raw_rc == 1:
        return 1, "manifeste de curation signale un refus d'application"
    if refusal_records:
        if all(
            _is_report_only_record(decision, project_root=project_root)
            for decision in refusal_records
        ):
            return 0, None
        return 1, "manifeste de curation contient un refus d'application"
    return 0, None


def build_report_context(cfg: TelemetryConfig, *, anchor: str | None = None) -> dict | None:
    """Construit le ctx Jinja du rapport (v6.1) — partagé par prep et assemble.

    Reconstruit intégralement depuis les artefacts JSON du run actif à chaque
    appel : prep et assemble tournent comme sous-commandes CLI séparées, il n'y
    a donc aucune persistance inter-process. Retourne None si la summary du run
    est absente (le rapport HTML est alors silencieusement ignoré).
    """
    run_time = _parse_anchor(anchor)
    date = run_time.strftime("%Y-%m-%d")
    out = resolve_active_run_dir(cfg.output_dir, date)

    summary = _load_json(out / f"weekly-summary-{date}.json")
    if not _artifact_contract_valid("weekly-summary", summary, date=date):
        return None

    insights = _load_json(out / f"weekly-insights-{date}.json")
    digest = _load_json(out / f"weekly-harness-digest-{date}.json")
    for digest_problem in harness_digest_problems(digest):
        print(f"report: WARNING: {digest_problem}", file=sys.stderr, flush=True)
        digest = None
    ecosystem = _load_json(out / f"weekly-ecosystem-{date}.json")
    findings = _load_json(out / f"weekly-quality-findings-{date}.json")
    coherence_findings = _load_json(out / f"weekly-coherence-findings-{date}.json")
    skill_curate = _load_json(out / f"skill-curate-{date}.json")

    git_commits = _git_log(
        cfg.project_root,
        _iso(run_time - timedelta(hours=cfg.window_hours())),
        _iso(run_time),
    )
    pending = _pending_auto_commits(
        cfg.project_root,
        _iso(run_time - timedelta(weeks=cfg.review_window_weeks)),
    )
    # v6.0.l (E11) : delta par règle vs run précédent (null en first-run).
    lint_delta = ((insights or {}).get("deltas") or {}).get("lint_violations_delta_by_rule") or {}

    provenance = {
        "anchor": run_time.isoformat(),
        "artifact_inputs": _artifact_provenance(out, date),
        "run_provenance": summary.get("run_provenance"),
    }
    ctx = {
        "date": date,
        "engine_version": __version__,
        "period": summary.get("period", {}),
        "summary": summary,
        "insights": insights,
        "digest": digest,
        "ecosystem": ecosystem,
        "findings": findings,
        "models_top": _top_models(summary),
        "top_sessions": summary.get("top_sessions_by_cost", []),
        "harness_ignored_rules": list(cfg.harness_ignored_rules),
        "harness_top_rules": [
            {
                "rule": rule,
                "count": count,
                "delta": lint_delta.get(rule),
            }
            for rule, count in _top_harness_rules(digest, cfg.harness_ignored_rules)
        ],
        "cost_outliers_state": summary.get("cost_outliers_state", "computed"),
        "outliers": {o["session_id"] for o in summary.get("cost_outliers", [])},
        "audit_candidates": _load_json(out / f"weekly-audit-candidates-{date}.json"),
        "audit_worker_statuses": (
            (_load_json(out / f"weekly-audit-candidates-{date}.json") or {}).get("worker_statuses", [])
        ),
        "watch_findings": _load_json(out / f"weekly-watch-findings-{date}.json"),
        "coherence_findings": coherence_findings,
        "coherence_items": _coherence_findings(coherence_findings),
        "skill_curate": skill_curate,
        "curation_detail": _curation_manifest_detail(skill_curate),
        "coherence_curation_signal": _coherence_has_curation_signal(coherence_findings),
        "graphify_state": _load_json(out / f"weekly-graphify-state-{date}.json"),
        "harness_budget": (digest or {}).get("budget"),
        "harness_triggers": (digest or {}).get("triggers"),
        "harness_dependencies": (digest or {}).get("dependencies"),
        "harness_scope": (digest or {}).get("harness_scope")
        or (digest or {}).get("harness_include"),
        "harness_counts": (digest or {}).get("harness_counts"),
        "run_dir": (active_run_meta(cfg.output_dir, date) or {}).get("run_dir"),
        "provenance": provenance,
        "gate_status": _gate_status(provenance["artifact_inputs"]),
        "harness_remediation": _load_json(out / f"weekly-harness-remediation-{date}.json"),
        "warnings_grouped": _group_warnings(summary.get("warnings", [])),
        "watch_warned": any(
            w.get("source") == "github:watch-repos" for w in (ecosystem or {}).get("warnings", [])
        ),
        "watch_items": [
            i
            for i in (ecosystem or {}).get("new_items", [])
            if any(
                fv == "github:watch-repos" or fv.startswith("watch:")
                for fv in (i.get("found_via") or [])
            )
        ],
        "daily_totals": _complete_daily(summary.get("period", {}), summary.get("daily_totals", [])),
        "auto_commits": git_commits,
        "pending_auto_commits": pending,
        "self_cost": info["cost"] if (info := _self_cost_value(cfg)) else None,
        "self_cost_tokens": (info or {}).get("tokens"),
    }
    return ctx


def report_prep(
    cfg: TelemetryConfig, *, anchor: str | None = None
) -> tuple[Path | None, dict | None]:
    """Render deterministic sections into `weekly-report-draft-<date>.md`."""
    ctx = build_report_context(cfg, anchor=anchor)
    if ctx is None:
        return None, None

    date = ctx["date"]
    out = resolve_active_run_dir(cfg.output_dir, date)
    env = Environment(
        loader=FileSystemLoader(str(Path(__file__).parent / "templates")),
        autoescape=select_autoescape(("html",)),
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    template = env.get_template("report_template.md.j2")
    rendered = template.render(**ctx)

    draft = out / f"weekly-report-draft-{date}.md"
    draft.write_text(rendered, encoding="utf-8")
    return draft, ctx


def report_blocks_draft(
    cfg: TelemetryConfig, *, anchor: str | None = None
) -> tuple[Path | None, list[str], int]:
    """Deterministic draft of the section-4 blocks (v5.28, P5.1) — zero LLM.

    Writes `weekly-report-blocks-<date>.md` from alerts, maintenance findings,
    notable sessions and top harness rules. Explicitly flagged as an automatic
    draft requiring human review; `report-assemble` validates it like any block.
    """
    run_time = _parse_anchor(anchor)
    date = run_time.strftime("%Y-%m-%d")
    out = resolve_active_run_dir(cfg.output_dir, date)

    summary = _load_json(out / f"weekly-summary-{date}.json")
    if not _artifact_contract_valid("weekly-summary", summary, date=date):
        return None, ["summary inexistante — lancer run d'abord"], 2
    insights = _load_json(out / f"weekly-insights-{date}.json")
    digest = _load_json(out / f"weekly-harness-digest-{date}.json")
    for digest_problem in harness_digest_problems(digest):
        print(f"report: WARNING: {digest_problem}", file=sys.stderr, flush=True)
        digest = None
    quality_findings = _load_json(out / f"weekly-quality-findings-{date}.json")

    lines = [
        "# Constats qualitatifs (brouillon automatique — revue humaine requise)",
        "",
        f"*Généré le {date} par `report-blocks-draft` (déterministe, zéro LLM). "
        "Éditer puis relancer `report-assemble`, ou conserver tel quel : "
        "la section 4 du rapport restera marquée comme brouillon automatique.*",
        "",
    ]
    if insights and insights.get("alerts"):
        lines += ["## Alertes", ""]
        for a in insights["alerts"]:
            lines += [
                f"- **`{a['rule']}`** ({a['severity']}) : observé {a.get('observed')} vs seuil {a.get('threshold')}"
                f"{(' ' + str(a.get('unit'))) if a.get('unit') else ''}"
                f"{(' — ' + str(a.get('note'))) if a.get('note') else ''}",
                "",
            ]
    findings = (insights or {}).get("maintenance", {}).get("findings", [])
    if findings:
        lines += ["## Constats de maintenance", ""]
        for f in findings:
            lines += [
                f"- [{f.get('severity')}] {f.get('category')} — {f.get('description')} "
                f"{f.get('recommendation')}",
                "",
            ]
    for t in summary.get("top_sessions_by_cost", [])[:2]:
        lines += [
            f"- **{t.get('title_or_topic') or t['session_id']}** — ${t.get('cost_usd', 0.0):.4f}, "
            f"{t.get('total_tokens', 0):,} tokens, cache eff. {t.get('cache_efficiency')}, "
            f"{t.get('api_call_count')} appels API",
            "",
        ]
    if quality_findings and quality_findings.get("findings"):
        lines += ["## Constats de l'audit qualitatif (Partie 3)", ""]
        for f in quality_findings["findings"]:
            lines += [
                f"- [{f.get('severity', 'low').upper()}] {f.get('category', '?')} — "
                f"{f.get('description', '')} → {f.get('recommendation', '')}"
                f"{(' *(repris de ' + str(f.get('carried_from')) + ')*') if f.get('source') == 'carried' else ''}",
                "",
            ]
    flat = flatten_harness_findings(digest)
    if flat:
        lines += ["## Règles harness les plus violées", ""]
        for rule, count in _top_harness_rules(digest, cfg.harness_ignored_rules):
            lines += [f"- `{rule}` : {count} violation(s)", ""]
    lines += ["## Recommandations", ""]
    lines += [
        "- Revoir les alertes et constats ci-dessus ; corriger les violations harness en priorité (R4).",
        "",
    ]

    # v5.29/7b-hybride : le brouillon déterministe vit dans -auto- (filet de sécurité) ;
    # le fichier weekly-report-blocks-<date>.md est réservé à la prose LLM de l'agent.
    blocks_path = out / f"weekly-report-blocks-auto-{date}.md"
    blocks_path.write_text("\n".join(lines), encoding="utf-8")
    return (
        blocks_path,
        [f"brouillon de blocs généré ({len(lines)} lignes) — à éditer si besoin"],
        applicable_summary_rc(summary, out=out, date=date, fallback_rc=0),
    )


_BLOC_DETERMINISTE_SIGNATURE = "# Constats qualitatifs (brouillon automatique"


def validate_llm_blocks(text: str, findings: dict | None, insights: dict | None):
    """Garde-fous anti-hallucination du bloc 7b LLM (v5.29, hybride).

    Retourne (violations, coverage_warnings) :
    - violations → le bloc est REJETÉ, report-assemble bascule sur le brouillon
      déterministe (fallback) ;
    - coverage_warnings → le bloc est accepté mais l'annexe signale les constats
      high non cités (omission ≠ hallucination).
    Checks : zéro chiffre (spec : le bloc ne cite que catégories/sévérités) ;
    balises de source [F:ses_xxx#cat] / [M:cat] / [A:rule] résolues dans les
    entrées ; taille ≤ 60 lignes ; tout finding high doit être cité.
    """
    import re

    violations: list[str] = []
    coverage: list[str] = []

    # les balises [F:...]/[M:...]/[A:...] portent des ids/session_ids (chiffres) —
    # le check chiffres ne s'applique qu'au texte visible (hors balises).
    text_no_tags = re.sub(r"\[[FMA]:[^\]]+\]", "", text)
    # chiffres autorisés hors balise : dates ISO, pourcentages, numéros de version.
    # la spec interdit toujours les chiffres « libres » (coûts, décomptes, durées).
    _ALLOWED_NUM = re.compile(
        r"\b\d{4}-\d{2}-\d{2}\b"  # date ISO
        r"|\b\d{1,3}([.,]\d+)?%"  # pourcentage
        r"|\bv?\d+\.\d+(\.\d+)?\b"  # version sémantique
    )
    text_no_allowed = _ALLOWED_NUM.sub(" ", text_no_tags)
    illegal_digits = re.findall(r"\d+", text_no_allowed)
    if illegal_digits:
        snippet = ", ".join(illegal_digits[:5])
        first_digit = re.search(r"\d+", text_no_allowed)
        line = text_no_allowed.count("\n", 0, first_digit.start()) + 1 if first_digit else 1
        violations.append(
            "chiffres interdits dans le bloc LLM (spec : catégories/sévérités "
            f"uniquement) — ligne {line}, chiffres hors date/pourcentage/version : {snippet}"
        )

    n_lines = len(text.splitlines())
    if n_lines > 60:
        violations.append(f"bloc trop long (ligne {n_lines}, {n_lines} lignes > 60)")

    findings_list = (findings or {}).get("findings", []) if findings else []
    alerts = (insights or {}).get("alerts", []) if insights else []
    maint = (insights or {}).get("maintenance", {}).get("findings", []) if insights else []

    f_refs = {f"{f.get('session_id')}#{f.get('category')}" for f in findings_list}
    m_refs = {f.get("category") for f in maint}
    a_refs = {a.get("rule") for a in alerts}
    seen_f: set[str] = set()

    # Empty source references are not merely unknown: report them as malformed so
    # the author can repair the exact traceability marker instead of guessing.
    for match in re.finditer(r"\[([FMA]):([^\]]*)\]", text):
        kind, ref = match.group(1), match.group(2)
        line = text.count("\n", 0, match.start()) + 1
        if not ref.strip():
            violations.append(f"balise de source mal formée [{kind}:] — ligne {line}")
            continue
        if kind == "F":
            if ref not in f_refs:
                violations.append(
                    f"balise inconnue [F:{ref}] — ligne {line}, aucun finding correspondant"
                )
            else:
                seen_f.add(ref)
        elif kind == "M":
            if ref not in m_refs:
                violations.append(
                    f"balise inconnue [M:{ref}] — ligne {line}, aucun constat de maintenance"
                )
        elif kind == "A" and ref not in a_refs:
            violations.append(f"balise inconnue [A:{ref}] — ligne {line}, aucune alerte")

    for f in findings_list:
        if f.get("severity") == "high":
            ref = f"{f.get('session_id')}#{f.get('category')}"
            if ref not in seen_f:
                coverage.append(f"constat high non couvert par le bloc : [F:{ref}]")

    return violations, coverage


def report_assemble(
    cfg: TelemetryConfig, *, anchor: str | None = None
) -> tuple[Path | None, list[str], int]:
    """Inject the LLM blocks file into the draft → final report."""
    run_time = _parse_anchor(anchor)
    date = run_time.strftime("%Y-%m-%d")
    out = resolve_active_run_dir(cfg.output_dir, date)
    warnings: list[str] = []
    rc = 0

    draft = out / f"weekly-report-draft-{date}.md"
    text = _load_text(draft)
    if text is None:
        return (
            None,
            [
                f"draft inexistant {draft} — un assemble précédent l'a consommé/supprimé : "
                "relancer report-prep d'abord"
            ],
            2,
        )

    # Required-input validation happens after the draft existence check so the
    # historical "assemble consumed the draft" diagnostic remains actionable.
    timings, timings_state = _json_file_state(out / f"weekly-timings-{date}.json")
    artifact_gate = validate_required_artifacts(
        out,
        date,
        applicability=_branch_applicability(timings, date, out=out),
        dynamic_audit_artifacts=_audit_artifact_declarations(timings),
    )
    if artifact_gate["status"] != "pass":
        missing = [
            entry["path"]
            for entry in artifact_gate["required"].values()
            if entry["status"] != "present"
        ]
        return (
            None,
            [
                "artefact requis manquant ou illisible — "
                + ", ".join(missing)
                + " (relancer weekly_run)"
            ],
            2,
        )

    summary_for_rc = _load_json(out / f"weekly-summary-{date}.json")
    rc = applicable_summary_rc(
        summary_for_rc,
        out=out,
        date=date,
        additional_records=_join_status_records(timings) if timings_state == "present" else (),
        project_root=cfg.project_root,
        fallback_rc=0,
    )

    # Phase 4 (gate déterministe) : WAVE 2.5 REQUIRED. Si les findings de cohérence
    # portent des actions de curation mais le manifeste skill-curate est absent ->
    # alerte P0 + rc=1 (partiel, jamais fatal). Le détail P0 est rendu dans le
    # rapport via le contexte (coherence_curation_signal + skill_curate).
    coherence = _load_json(out / f"weekly-coherence-findings-{date}.json")
    curation_path = out / f"skill-curate-{date}.json"
    curation_manifest, curation_state = _json_file_state(curation_path)
    if curation_state == "ill_readable":
        warning = f"manifeste de curation malformé ou illisible : {curation_path.name}"
        warnings.append(warning)
        return None, warnings, 2
    elif curation_state == "present":
        curation_rc, curation_warning = _curation_manifest_gate(
            curation_manifest,
            date=date,
            project_root=cfg.project_root,
        )
        if curation_warning:
            warnings.append(curation_warning)
        if curation_rc >= 2:
            return None, warnings, 2
        rc = max(rc, curation_rc)

    harness_digest = _load_json(out / f"weekly-harness-digest-{date}.json")
    critical_security = _critical_security_findings(harness_digest)
    blocking = _blocking_security_findings(harness_digest)
    if critical_security:
        warnings.append(
            "⚠ findings security/critical présents — rapport marqué en échec déterministe"
            + (" (blocking security rule)" if blocking else "")
        )
        if blocking:
            # Exact critical rules are a hard stop.  Do not consume the draft or
            # write Markdown/HTML/gate artifacts after a security block.
            return None, warnings, 2
        rc = max(rc, 1)
    if _coherence_has_curation_signal(coherence) and curation_state == "absent":
        warnings.append(
            f"⚠ WAVE 2.5 (curation) REQUIRED : findings de cohérence porte(nt) des "
            f"actions de curation mais skill-curate-{date}.json est absent — "
            f"exécuter `weekly_skill_curate --apply` puis regénérer le rapport (P0)."
        )
        rc = max(rc, 1)

    marker = "<!-- QUALITY_BLOCK -->"
    if marker not in text:
        return None, ["marqueur QUALITY_BLOCK absent du draft — gabarit incohérent"], 2

    # v5.29 hybride : brouillon déterministe (-auto-) toujours disponible ;
    # le fichier weekly-report-blocks-<date>.md est la prose LLM (7b), validée.
    auto_path = out / f"weekly-report-blocks-auto-{date}.md"
    llm_path = out / f"weekly-report-blocks-{date}.md"
    auto_text = _load_text(auto_path)
    llm_text = _load_text(llm_path)

    replacement: str | None = None
    status = "non disponible (placeholder)"
    if llm_text is not None:
        word_count = len(llm_text.split())
        if word_count < cfg.blocks_min_words:
            violation = (
                f"bloc LLM trop court ({word_count} mots < {cfg.blocks_min_words}) — "
                f"revoir {llm_path.name}"
            )
            status = f"brouillon automatique (bloc LLM rejeté : {violation}) — auto_draft_fallback; never validated"
            warnings.append(f"bloc LLM rejeté — fallback brouillon automatique : {violation}")
            replacement = (
                auto_text
                if auto_text is not None
                else (
                    "*Section 4 non disponible (bloc LLM rejeté et brouillon automatique absent — "
                    "lancer report-blocks-draft).*\n"
                )
            )
        else:
            findings = _load_json(out / f"weekly-quality-findings-{date}.json")
            insights = _load_json(out / f"weekly-insights-{date}.json")
            violations, coverage = validate_llm_blocks(llm_text, findings, insights)
            warnings.extend(f"bloc LLM : {v}" for v in coverage)
            if violations:
                status = f"brouillon automatique (bloc LLM rejeté : {' ; '.join(violations)}) — auto_draft_fallback; never validated"
                warnings.append(
                    f"bloc LLM rejeté — fallback brouillon automatique ({len(violations)} violation(s))"
                )
                replacement = (
                    auto_text
                    if auto_text is not None
                    else (
                        "*Section 4 non disponible (bloc LLM rejeté et brouillon automatique absent — "
                        "lancer report-blocks-draft).*\n"
                    )
                )
            else:
                status = "prose agent (7b LLM)"
                replacement = llm_text
    elif auto_text is not None:
        status = "brouillon automatique (report-blocks-draft) — auto_draft_fallback; prose absente; never validated"
        replacement = auto_text
    else:
        replacement = (
            "*Section 4 non disponible (bloc de constats absent — ni l'agent (7b) ni "
            "report-blocks-draft n'ont produit de bloc).*\n\n"
            "Lancer report-blocks-draft, ou coller les constats dans "
            "`weekly-report-blocks-<date>.md` puis relancer report-assemble."
        )
        warnings.append("bloc de constats absent — section 4 remplacée par un placeholder")

    final_text = text.replace(marker, replacement) + f"\n---\n*Statut section 4 : {status}*\n"
    final_path = out / f"weekly-report-{date}.md"
    final_path.write_text(final_text, encoding="utf-8")
    if replacement is not None:
        final_path.with_name(f"weekly-report-draft-{date}.md").unlink(missing_ok=True)

    # v6.1 : rapport HTML autonome, best-effort (échec → warning + None, jamais
    # fatal). Le ctx est reconstruit depuis les artefacts — prep et assemble
    # tournent comme sous-commandes CLI séparées — et le bloc qualité injecté
    # ci-dessus (prose LLM validée ou fallback auto) alimente la section 4.
    ctx = build_report_context(cfg, anchor=anchor)
    if ctx is not None:
        html_enabled = bool(cfg.html_report_dir)
        render_error: Exception | None = None
        try:
            html_path = render_html_report(cfg, anchor=anchor, ctx=ctx, quality_block=replacement)
        except Exception as exc:  # renderer is best-effort; gate remains deterministic
            html_path = None
            render_error = exc
        artifact_gate = validate_required_artifacts(
            out, date, html_enabled=html_enabled, html_path=html_path
        )
        ctx["gate_status"]["required"] = artifact_gate["required"]
        ctx["gate_status"]["optional"] = artifact_gate["optional"]
        ctx["gate_status"]["artifacts"] = {
            "status": artifact_gate["status"],
            "missing": [
                artifact["path"]
                for artifact in artifact_gate["artifacts"].values()
                if artifact["status"] == "absent"
                and (artifact.get("required") or artifact.get("applicable", True))
            ],
            "ill_readable": [
                artifact["path"]
                for artifact in artifact_gate["artifacts"].values()
                if artifact["status"] == "ill_readable"
                and (artifact.get("required") or artifact.get("applicable", True))
            ],
            "optional_missing": artifact_gate["optional_missing"],
            "optional_ill_readable": artifact_gate["optional_ill_readable"],
        }
        ctx["gate_status"]["html"] = artifact_gate["html"]
        if render_error is not None:
            report_only_permission = _is_external_permission_failure(cfg, render_error)
            ctx["gate_status"]["html"] = {
                "status": "report-only" if report_only_permission else "failure",
                "path": None,
                "error": type(render_error).__name__,
            }
            if report_only_permission:
                ctx["gate_status"]["html"].update(
                    {
                        "report_only": True,
                        "category": "external-permission-refusal",
                        "target": str(cfg.html_report_dir),
                        "project_root": str(cfg.project_root),
                    }
                )
            warnings.append(
                "HTML renderer permission refused outside worktree; report-only"
                if report_only_permission
                else "HTML renderer failed; report artifact unavailable"
            )
            if not report_only_permission:
                rc = max(rc, 1)
        elif html_enabled and artifact_gate["html"]["status"] != "present":
            warnings.append(
                "HTML enabled but report artifact "
                f"{artifact_gate['html']['status']}"
            )
            rc = max(rc, 1)
        if html_path:
            try:
                open_html_report(cfg, html_path)
            except Exception as exc:  # best effort; external permission is report-only
                if _is_external_permission_failure(cfg, exc):
                    warnings.append("HTML auto-open permission refused outside worktree; report-only")
                else:
                    warnings.append(f"HTML auto-open failed: {type(exc).__name__}")
                    rc = max(rc, 1)

    # Persist machine-readable gate state alongside final report metadata.
    if ctx is not None:
        ctx["gate_status"]["prose"] = {
            "status": "validated" if status == "prose agent (7b LLM)" else "auto_draft_fallback",
            "validated": status == "prose agent (7b LLM)",
        }
        ctx["gate_status"]["summary_rc"] = rc
        ctx["gate_status"]["blocking_rules"] = sorted(_BLOCKING_SECURITY_RULES)
        (out / f"weekly-report-gates-{date}.json").write_text(
            json.dumps(ctx["gate_status"], ensure_ascii=False, indent=2), encoding="utf-8"
        )

    return final_path, warnings, rc
