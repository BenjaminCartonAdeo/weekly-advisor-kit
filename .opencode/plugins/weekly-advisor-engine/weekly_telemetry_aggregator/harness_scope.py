"""Allowlist and temporary projection helpers for ``harness-eval``.

``harness-eval`` accepts one root but does not currently expose an include
option.  The weekly advisor therefore materialises a small, disposable root
containing only the configured policy/documentation surfaces.  Files retain
their project-relative paths (for example ``.opencode/skills/foo/SKILL.md``),
so the resulting digest remains useful to the report and to insights.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .config import DEFAULT_HARNESS_EXCLUDE_PATTERNS, HarnessIncludeConfig
from .util import relative_path


@dataclass(slots=True)
class HarnessScope:
    """Resolved harness scope and its audit information."""

    profile: str
    include_patterns: list[str]
    exclude_patterns: list[str]
    included_files: list[str]
    included_counts_by_pattern: dict[str, int]
    excluded_file_count: int
    excluded_counts_by_pattern: dict[str, int]
    unscoped_files: list[str]
    warnings: list[str]

    def to_dict(self) -> dict[str, Any]:
        """Return the stable JSON representation stored in the digest."""
        return {
            "profile": self.profile,
            "include_patterns": list(self.include_patterns),
            "exclude_patterns": list(self.exclude_patterns),
            "included_file_count": len(self.included_files),
            "included_files": list(self.included_files),
            "included_counts_by_pattern": dict(self.included_counts_by_pattern),
            "excluded_file_count": self.excluded_file_count,
            "excluded_counts_by_pattern": dict(self.excluded_counts_by_pattern),
            "unscoped_file_count": len(self.unscoped_files),
            "unscoped_files": list(self.unscoped_files),
            "warnings": list(self.warnings),
            "projection": "temporary-project-relative",
        }


def _expand_braces(pattern: str) -> list[str]:
    """Expand the small ``{js,ts}`` style syntax used by the config profiles."""
    match = re.search(r"\{([^{}]+)\}", pattern)
    if match is None:
        return [pattern]
    values = match.group(1).split(",")
    expanded: list[str] = []
    for value in values:
        expanded.extend(_expand_braces(pattern[: match.start()] + value + pattern[match.end() :]))
    return expanded


def _normalise_pattern(pattern: str) -> str:
    """Normalise a config glob to the project-relative POSIX form."""
    return pattern.replace("\\", "/").rstrip("/")


def _safe_pattern(pattern: str) -> bool:
    """Reject absolute and parent-traversing patterns before using ``Path.glob``."""
    pure = PurePosixPath(pattern)
    return not pure.is_absolute() and ".." not in pure.parts


def _glob_regex(pattern: str) -> re.Pattern[str]:
    """Compile a slash-aware glob regex.

    ``fnmatch`` treats ``*`` as matching ``/``.  That is unsafe for an
    allowlist because ``plugins/*.ts`` could otherwise select nested files.
    This small compiler gives ``*`` segment semantics and ``**`` recursive
    semantics.
    """
    pattern = _normalise_pattern(pattern)
    parts: list[str] = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "*":
            if index + 1 < len(pattern) and pattern[index + 1] == "*":
                index += 2
                if index < len(pattern) and pattern[index] == "/":
                    parts.append("(?:.*/)?")
                    index += 1
                else:
                    parts.append(".*")
                continue
            parts.append("[^/]*")
        elif char == "?":
            parts.append("[^/]")
        else:
            parts.append(re.escape(char))
        index += 1
    return re.compile("^" + "".join(parts) + "$")


def _matches(rel_path: str, pattern: str) -> bool:
    """Return whether a project-relative path matches a glob pattern."""
    return any(_glob_regex(expanded).fullmatch(rel_path) for expanded in _expand_braces(pattern))


def _iter_regular_files(root: Path) -> Iterator[Path]:
    """Yield regular, non-symlink files below ``root`` (rglob ne suit pas les symlinks de dossiers)."""
    if not root.is_dir():
        return
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        yield path


def resolve_harness_scope(project_root: Path, config: HarnessIncludeConfig) -> HarnessScope:
    """Resolve configured files and audit what was deliberately left out.

    The walk is used only for scope accounting.  The harness itself receives
    files from ``included_files`` through :func:`copy_scope_to_projection`, so
    excluded and unscoped content is never made visible to the subprocess.
    """
    root = project_root.expanduser().resolve()
    profile = config.default_profile
    profile_patterns = config.profiles.get(profile)
    profile_known = profile_patterns is not None
    include_patterns = list(profile_patterns or [])
    # These exclusions are mandatory even when a caller supplies custom
    # patterns: a broad custom include must not be able to re-expose vendor,
    # generated, or the engine's own source tree.
    exclude_patterns: list[str] = []
    for pattern in [*DEFAULT_HARNESS_EXCLUDE_PATTERNS, *config.exclude_patterns]:
        if pattern not in exclude_patterns:
            exclude_patterns.append(pattern)

    # The allowlist is deliberately rooted in `.opencode/`.  Walking the whole
    # application repository only to count files that can never be included
    # defeats the purpose of the projection and made the Adeo run spend minutes
    # traversing source trees and build artefacts.
    opencode_root = root / ".opencode"
    all_files = sorted(
        (
            f".opencode/{relative_path(path, opencode_root)}"
            for path in _iter_regular_files(opencode_root)
        ),
        key=str,
    )

    excluded_counts = {pattern: 0 for pattern in exclude_patterns}
    excluded_files: set[str] = set()
    unexcluded_files: list[str] = []
    for rel_path in all_files:
        matching_excludes = [pattern for pattern in exclude_patterns if _matches(rel_path, pattern)]
        if not matching_excludes:
            unexcluded_files.append(rel_path)
            continue
        excluded_files.add(rel_path)
        for pattern in matching_excludes:
            excluded_counts[pattern] += 1

    included: set[str] = set()
    included_counts = {pattern: 0 for pattern in include_patterns}
    for pattern in include_patterns:
        normalised = _normalise_pattern(pattern)
        if not _safe_pattern(normalised):
            continue
        for rel_path in all_files:
            if rel_path in excluded_files or rel_path in included:
                continue
            if not _matches(rel_path, normalised):
                continue
            included.add(rel_path)
            included_counts[pattern] += 1

    unscoped = sorted(
        rel_path
        for rel_path in unexcluded_files
        if rel_path == ".opencode" or rel_path.startswith(".opencode/")
        if rel_path not in included
    )
    warnings: list[str] = []
    if not profile_known:
        warnings.append(
            f"unknown harness include profile '{profile}' — no files selected; "
            "configure harness_include.default_profile explicitly"
        )
    if unscoped:
        preview = ", ".join(unscoped[:10])
        suffix = f" (+{len(unscoped) - 10} more)" if len(unscoped) > 10 else ""
        warnings.append(f"unscoped .opencode surface(s) not scanned: {preview}{suffix}")

    return HarnessScope(
        profile=profile,
        include_patterns=include_patterns,
        exclude_patterns=exclude_patterns,
        included_files=sorted(included),
        included_counts_by_pattern=included_counts,
        excluded_file_count=len(excluded_files),
        excluded_counts_by_pattern=excluded_counts,
        unscoped_files=unscoped,
        warnings=warnings,
    )


def copy_scope_to_projection(
    project_root: Path, scope: HarnessScope, projection_root: Path
) -> None:
    """Copy selected files under a temporary root, preserving relative paths."""
    root = project_root.expanduser().resolve()
    destination = projection_root.resolve()
    for rel_path in scope.included_files:
        source = root / Path(rel_path)
        if source.is_symlink() or not source.is_file():
            raise OSError(f"scoped file disappeared or is not regular: {rel_path}")
        target = destination / Path(rel_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _remap_string(value: str, projection_root: Path) -> str:
    """Remap absolute projection paths to project-relative POSIX paths."""
    projection = projection_root.resolve()
    try:
        candidate = Path(value)
        if candidate.is_absolute():
            return candidate.relative_to(projection).as_posix()
    except ValueError:
        pass

    prefix = str(projection) + os.sep
    if value == str(projection):
        return "."
    if value.startswith(prefix):
        return Path(value[len(prefix) :]).as_posix()
    return value


def remap_digest_paths(value: Any, projection_root: Path) -> Any:
    """Recursively remap paths emitted by harness-eval from the projection root."""
    if isinstance(value, dict):
        return {key: remap_digest_paths(item, projection_root) for key, item in value.items()}
    if isinstance(value, list):
        return [remap_digest_paths(item, projection_root) for item in value]
    if isinstance(value, str):
        return _remap_string(value, projection_root)
    return value


def attach_component_paths(digest: dict[str, Any], scope: HarnessScope) -> None:
    """Attach project-relative paths when harness-eval identifies only names.

    Version 7.9.0 emits ``uncategorized_files`` in the same order as
    ``inspection.uncategorized`` but does not repeat the path on each component.
    Commands can be resolved from their basename.  Adding the path here keeps
    findings actionable and lets downstream aggregation deduplicate by file.
    """
    inspection = digest.get("inspection")
    if not isinstance(inspection, dict):
        return

    uncategorized = inspection.get("uncategorized")
    unclassified_paths = digest.get("uncategorized_files")
    if isinstance(uncategorized, list) and isinstance(unclassified_paths, list):
        for component, path in zip(uncategorized, unclassified_paths, strict=False):
            if isinstance(component, dict) and "path" not in component and isinstance(path, str):
                component["path"] = path

    for section, directory in (("command", ".opencode/commands/"), ("claude_md", ".opencode/")):
        components = inspection.get(section)
        if not isinstance(components, list):
            continue
        for component in components:
            if not isinstance(component, dict) or component.get("path"):
                continue
            name = component.get("name")
            if not isinstance(name, str) or not name:
                continue
            candidates = [
                path
                for path in scope.included_files
                if path.startswith(directory)
                and Path(path).stem.casefold() == Path(name).stem.casefold()
            ]
            if len(candidates) == 1:
                component["path"] = candidates[0]


def _as_nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, float) and value.is_integer() and value >= 0:
        return int(value)
    return None


def _first_count(data: Mapping[str, object], *keys: str) -> int | None:
    for key in keys:
        count = _as_nonnegative_int(data.get(key))
        if count is not None:
            return count
    return None


def harness_digest_counts(digest: Mapping[str, object]) -> dict[str, int | None]:
    """Normalize scan metrics without confusing components with violations.

    ``components_scanned`` describes scanner work, not lint failures.  Raw
    findings count detailed finding records before de-duplication; unique
    findings de-duplicate exact records while retaining their component/path
    identity.  Failing summary ``rules`` with no detailed finding are counted
    as unique fallback findings, matching the insights normalizer's intent.
    """
    metadata = digest.get("metadata")
    metadata_map = metadata if isinstance(metadata, Mapping) else {}
    inspection = digest.get("inspection")
    inspection_map = inspection if isinstance(inspection, Mapping) else {}

    components: list[tuple[str, Mapping[str, object]]] = []
    for section in ("command", "claude_md", "uncategorized"):
        values = inspection_map.get(section)
        if not isinstance(values, list):
            continue
        for index, component in enumerate(values):
            if isinstance(component, Mapping):
                path = str(component.get("path") or f"{section}[{index}]")
                components.append((path, component))

    files = _first_count(digest, "files_scanned")
    if files is None:
        files = _first_count(metadata_map, "files_scanned")
    if files is None:
        paths = {
            path
            for path, _component in components
            if not path.startswith(("command[", "claude_md[", "uncategorized["))
        }
        files = len(paths) if paths else None

    components_scanned = _first_count(digest, "components_scanned")
    if components_scanned is None:
        components_scanned = _first_count(metadata_map, "components_scanned")
    if components_scanned is None:
        components_scanned = _first_count(inspection_map, "components_scanned")
    if components_scanned is None:
        components_scanned = len(components)

    raw_count = 0
    unique_records: set[tuple[str, str, str, str]] = set()

    top_findings = digest.get("findings")
    if isinstance(top_findings, list):
        for index, finding in enumerate(top_findings):
            if not isinstance(finding, Mapping):
                continue
            raw_count += 1
            unique_records.add(
                (
                    str(finding.get("path") or f"top[{index}]"),
                    str(finding.get("rule") or finding.get("id") or "unknown"),
                    str(finding.get("message") or finding.get("detail") or ""),
                    str(finding.get("severity") or ""),
                )
            )

    for path, component in components:
        detailed_rules: set[str] = set()
        findings = component.get("findings")
        if isinstance(findings, list):
            for finding in findings:
                if not isinstance(finding, Mapping):
                    continue
                raw_count += 1
                rule = str(finding.get("rule") or finding.get("id") or "unknown")
                detailed_rules.add(rule)
                unique_records.add(
                    (
                        path,
                        rule,
                        str(finding.get("message") or finding.get("detail") or ""),
                        str(finding.get("severity") or ""),
                    )
                )
        rules = component.get("rules")
        if isinstance(rules, list):
            for rule_entry in rules:
                if not isinstance(rule_entry, Mapping) or rule_entry.get("result") in (
                    None,
                    "pass",
                ):
                    continue
                rule = str(rule_entry.get("rule") or "unknown")
                if rule not in detailed_rules:
                    unique_records.add((path, rule, "", ""))

    findings_raw = _first_count(digest, "findings_raw")
    if findings_raw is None:
        findings_raw = _first_count(metadata_map, "findings_raw")
    if findings_raw is None:
        findings_raw = raw_count

    findings_unique = _first_count(digest, "findings_unique")
    if findings_unique is None:
        findings_unique = _first_count(metadata_map, "findings_unique")
    if findings_unique is None:
        findings_unique = len(unique_records)

    return {
        "files_scanned": files,
        "components_scanned": components_scanned,
        "findings_raw": findings_raw,
        "findings_unique": findings_unique,
    }


def enrich_harness_digest(
    digest: Mapping[str, object], scope: HarnessScope, projection_root: Path
) -> dict[str, Any]:
    """Remap paths and attach scope/normalized scan metrics to a digest."""
    enriched = remap_digest_paths(dict(digest), projection_root)
    if not isinstance(enriched, dict):  # pragma: no cover - defensive for typed recursion
        enriched = json.loads(json.dumps(digest))
    attach_component_paths(enriched, scope)
    scope_data = scope.to_dict()
    enriched["harness_include"] = scope_data
    # v6.0.k (F4): explicit `harness_scope` alias — the template and docs read
    # `harness_scope.unscoped_files`; `harness_include` stays for compatibility.
    enriched["harness_scope"] = scope_data
    counts = harness_digest_counts(enriched)
    if counts["files_scanned"] is None:
        counts["files_scanned"] = len(scope.included_files)
    enriched["harness_counts"] = counts
    normalized = enriched.get("normalized")
    normalized_data = dict(normalized) if isinstance(normalized, Mapping) else {}
    normalized_data.update(counts)
    normalized_data["harness_include"] = scope_data
    normalized_data["harness_scope"] = scope_data
    enriched["normalized"] = normalized_data
    return enriched
