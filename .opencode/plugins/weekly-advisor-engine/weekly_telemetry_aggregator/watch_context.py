"""Deterministic, worktree-only context for the weekly ecosystem review.

The ecosystem collector deliberately knows nothing about the project in which
the weekly advisor is installed.  This module is the small deterministic join
between the collector output and that project state.  It only reads paths
under ``project_root`` and never consults OpenCode's global configuration,
global skills, telemetry, or a network service.

The public builder returns ordinary JSON-compatible dictionaries so the module
can be used by the CLI and by the LLM-facing report stages without introducing
another persistence format or a cross-run state file.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote, urlsplit

from .util import iso

ExistingState = Literal["absent", "declared", "observed", "unknown"]
PluginSource = Literal["config", "local_file"]

SCHEMA_VERSION = 1
_CONFIG_NAMES = ("opencode.json", "opencode.jsonc")
_PLUGIN_FILE_SUFFIXES = {".cjs", ".cts", ".js", ".mjs", ".mts", ".ts", ".tsx"}
_NPM_PACKAGE_RE = re.compile(
    r"^(?:@[a-z0-9._~-]+/)?[a-z0-9._~-]+$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class PluginRecord:
    """One declared or locally observed project plugin."""

    name: str
    source: PluginSource
    path: str
    raw: str | None = None
    npm_package: str | None = None
    repo_url: str | None = None
    identities: tuple[str, ...] = ()
    declared: bool = False


@dataclass(frozen=True, slots=True)
class FileRecord:
    """One project-local skill, command, or agent identity."""

    name: str
    path: str
    identities: tuple[str, ...] = ()


@dataclass(slots=True)
class EnvironmentInventory:
    """Inventory collected exclusively from a project worktree."""

    plugins: list[PluginRecord] = field(default_factory=list)
    skills: list[FileRecord] = field(default_factory=list)
    commands: list[FileRecord] = field(default_factory=list)
    agents: list[FileRecord] = field(default_factory=list)
    config_files: list[str] = field(default_factory=list)
    config_available: bool = False
    config_valid: bool = False
    directories: dict[str, bool] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def _casefold(value: str) -> str:
    return value.strip().casefold()


def normalize_npm_package(value: str | None) -> str | None:
    """Return a normalized package identity without its version/specifier.

    ``@scope/name@latest`` and ``@scope/name@1.2.3`` therefore both normalize
    to ``@scope/name``.  Git, file, and URL specifications do not themselves
    identify an npm package and return ``None``.  Package names are compared
    case-insensitively because npm package identities are effectively
    lower-case, while the returned value remains a normal package identity.
    """

    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.lower().startswith("npm:"):
        text = text[4:].strip()
    if not text or text.startswith((".", "/", "~", "git+", "git:", "ssh:")):
        return None
    if text.lower().startswith(
        ("http://", "https://", "ssh://", "git://", "github:", "gitlab:", "bitbucket:")
    ):
        return None

    if text.startswith("@"):
        slash = text.find("/")
        if slash <= 1:
            return None
        separator = text.find("@", slash + 1)
    else:
        separator = text.find("@")
    identity = text if separator < 0 else text[:separator]
    identity = identity.strip()
    if not _NPM_PACKAGE_RE.fullmatch(identity):
        return None
    return identity.casefold()


def _package_spec_parts(value: str) -> tuple[str, str | None]:
    """Split a package spec into its package portion and optional suffix."""

    text = value.strip()
    if text.lower().startswith("npm:"):
        text = text[4:].strip()
    if text.startswith("@"):
        slash = text.find("/")
        separator = text.find("@", slash + 1) if slash >= 0 else -1
    else:
        separator = text.find("@")
    if separator < 0:
        return text, None
    return text[:separator], text[separator + 1 :].strip() or None


def normalize_repo_url(value: str | None) -> str | None:
    """Canonicalize a repository URL for deterministic exact matching.

    The canonical form uses HTTPS, lower-cases the host (and GitHub path),
    removes ``git+``/SSH transport decoration, query/fragment data, trailing
    slashes, and a terminal ``.git`` suffix.  No URL is fetched or validated
    against a remote service.
    """

    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.lower().startswith("git+"):
        text = text[4:]
    if text.lower().startswith("github:"):
        text = "https://github.com/" + text[7:].lstrip("/")
    elif text.startswith("git@"):  # scp-style git@github.com:owner/repo.git
        host_path = text[4:]
        if ":" not in host_path:
            return None
        host, path = host_path.split(":", 1)
        text = f"https://{host}/{path}"
    elif "://" not in text and not text.startswith("/"):
        # Useful for GitHub full names found in a few ecosystem sources.
        if re.fullmatch(r"[^/\s]+/[^/\s]+", text):
            text = "https://github.com/" + text
        else:
            return None

    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    if parts.scheme.lower() not in {"http", "https", "git", "ssh"} or not parts.hostname:
        return None
    host = parts.hostname.casefold()
    path = unquote(parts.path or "")
    path = "/" + "/".join(part for part in path.split("/") if part)
    if path == "/":
        return None
    path = path.rstrip("/")
    if path.lower().endswith(".git"):
        path = path[:-4].rstrip("/")
    if not path or path == "/":
        return None
    if host == "github.com":
        path = path.casefold()
    return f"https://{host}{path}"


# Explicit aliases make the normalization contract easy to discover for
# callers that use the terminology from the watch-context report.
canonical_repo_url = normalize_repo_url
normalize_repo = normalize_repo_url
canonicalize_repo_url = normalize_repo_url
canonicalize_npm_package = normalize_npm_package
normalize_npm_package_name = normalize_npm_package


def parse_jsonc(text: str) -> Any:
    """Parse JSON or JSONC text safely, supporting comments and trailing commas.

    A small state-machine parser is used instead of regular expressions so
    ``//`` in URLs and comment-like characters inside JSON strings are kept
    intact.  The function raises :class:`json.JSONDecodeError` for malformed
    input, just like :func:`json.loads`.
    """

    return json.loads(_remove_jsonc_syntax(text))


def load_jsonc(path: Path) -> Any:
    """Read and parse a JSON/JSONC file without consulting any other path."""

    return parse_jsonc(path.read_text(encoding="utf-8"))


def _remove_jsonc_syntax(text: str) -> str:
    without_comments: list[str] = []
    in_string = False
    escaped = False
    line_comment = False
    block_comment = False
    index = 0
    while index < len(text):
        char = text[index]
        next_char = text[index + 1] if index + 1 < len(text) else ""
        if line_comment:
            if char in "\r\n":
                line_comment = False
                without_comments.append(char)
            else:
                without_comments.append(" ")
            index += 1
            continue
        if block_comment:
            if char == "*" and next_char == "/":
                block_comment = False
                without_comments.extend((" ", " "))
                index += 2
            else:
                without_comments.append(char if char in "\r\n" else " ")
                index += 1
            continue
        if in_string:
            without_comments.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            without_comments.append(char)
            index += 1
        elif char == "/" and next_char == "/":
            line_comment = True
            without_comments.extend((" ", " "))
            index += 2
        elif char == "/" and next_char == "*":
            block_comment = True
            without_comments.extend((" ", " "))
            index += 2
        else:
            without_comments.append(char)
            index += 1

    cleaned = "".join(without_comments)
    result: list[str] = []
    in_string = False
    escaped = False
    for index, char in enumerate(cleaned):
        if in_string:
            result.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            result.append(char)
            continue
        if char == ",":
            lookahead = index + 1
            while lookahead < len(cleaned) and cleaned[lookahead].isspace():
                lookahead += 1
            if lookahead < len(cleaned) and cleaned[lookahead] in "}]":
                result.append(" ")
                continue
        result.append(char)
    return "".join(result)


loads_jsonc = parse_jsonc
read_jsonc = load_jsonc


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _repo_slug(repo_url: str | None) -> str | None:
    if not repo_url:
        return None
    slug = repo_url.rsplit("/", 1)[-1].strip()
    return slug or None


def _unique_identities(values: Sequence[str | None]) -> tuple[str, ...]:
    identities: set[str] = set()
    for value in values:
        if isinstance(value, str) and value.strip():
            identities.add(value.strip())
    return tuple(sorted(identities, key=str.casefold))


def parse_plugin_spec(value: str, *, path: str) -> PluginRecord:
    """Parse one project ``plugin`` declaration into normalized identities."""

    raw = value.strip()
    npm_package = normalize_npm_package(raw)
    _package, suffix = _package_spec_parts(raw) if npm_package else (raw, None)
    if suffix:
        repo_url = normalize_repo_url(suffix)
    elif npm_package:
        repo_url = None
    else:
        repo_url = normalize_repo_url(raw)

    local_name: str | None = None
    if raw.lower().startswith("file:"):
        local_name = Path(raw[5:].split("#", 1)[0]).stem
    elif raw.startswith((".", "/", "~")):
        local_name = Path(raw.split("#", 1)[0]).stem

    name = npm_package or _repo_slug(repo_url) or local_name or raw
    identities = _unique_identities(
        (
            name,
            npm_package,
            repo_url,
            _repo_slug(repo_url),
            local_name,
        )
    )
    return PluginRecord(
        name=name,
        source="config",
        path=path,
        raw=raw,
        npm_package=npm_package,
        repo_url=repo_url,
        identities=identities,
        declared=True,
    )


def _record_to_dict(record: PluginRecord | FileRecord) -> dict[str, Any]:
    data = asdict(record)
    if isinstance(record, (PluginRecord, FileRecord)):
        data["identities"] = list(record.identities)
    return data


def _read_plugin_config(
    project_root: Path,
) -> tuple[list[PluginRecord], list[str], bool, bool, list[str]]:
    records: list[PluginRecord] = []
    config_files: list[str] = []
    warnings: list[str] = []
    available = False
    valid = False
    opencode_dir = project_root / ".opencode"
    for name in _CONFIG_NAMES:
        path = opencode_dir / name
        try:
            exists = path.is_file()
        except OSError as exc:
            warnings.append(f"cannot inspect {_relative(path, project_root)}: {exc}")
            continue
        if not exists:
            continue
        available = True
        config_files.append(_relative(path, project_root))
        try:
            payload = load_jsonc(path)
        except (OSError, json.JSONDecodeError) as exc:
            warnings.append(f"invalid {_relative(path, project_root)}: {exc}")
            continue
        if not isinstance(payload, Mapping):
            warnings.append(f"invalid {_relative(path, project_root)}: root must be an object")
            continue
        valid = True
        declarations = payload.get("plugin", [])
        if isinstance(declarations, str):
            declarations = [declarations]
        if not isinstance(declarations, Sequence) or isinstance(declarations, (bytes, bytearray)):
            valid = False
            warnings.append(
                f"invalid {_relative(path, project_root)}: plugin must be an array of strings"
            )
            continue
        for declaration in declarations:
            if isinstance(declaration, str) and declaration.strip():
                records.append(parse_plugin_spec(declaration, path=_relative(path, project_root)))
            elif isinstance(declaration, Mapping):
                # Be permissive for hand-written configs while keeping the
                # string-array contract as the primary supported shape.
                raw = (
                    declaration.get("name") or declaration.get("package") or declaration.get("spec")
                )
                if isinstance(raw, str) and raw.strip():
                    record = parse_plugin_spec(raw, path=_relative(path, project_root))
                    repository = declaration.get("repository") or declaration.get("repo")
                    explicit_repo = normalize_repo_url(repository)
                    if explicit_repo and explicit_repo != record.repo_url:
                        record = PluginRecord(
                            **{
                                **asdict(record),
                                "repo_url": explicit_repo,
                                "identities": _unique_identities(
                                    (*record.identities, explicit_repo, _repo_slug(explicit_repo))
                                ),
                            }
                        )
                    records.append(record)
            else:
                warnings.append(
                    f"ignored non-string plugin declaration in {_relative(path, project_root)}"
                )
    if not available:
        warnings.append("plugin config not found under .opencode/ (opencode.json/opencode.jsonc)")
    return records, sorted(config_files), available, valid, warnings


def _local_plugin_records(project_root: Path) -> tuple[list[PluginRecord], list[str], bool]:
    directory = project_root / ".opencode" / "plugins"
    warnings: list[str] = []
    try:
        directory_exists = directory.is_dir()
    except OSError as exc:
        return [], [f"cannot inspect .opencode/plugins: {exc}"], False
    if not directory_exists:
        return [], [], False
    try:
        paths = sorted(path for path in directory.rglob("*") if path.is_file())
    except OSError as exc:
        return [], [f"cannot scan .opencode/plugins: {exc}"], True

    records: list[PluginRecord] = []
    for path in paths:
        # Direct files are accepted even without an extension (a useful escape
        # hatch for executable plugin shims); nested package directories are
        # restricted to JS/TS plugin extensions so the Python engine package
        # itself is not mistaken for dozens of plugins.
        direct_file = path.parent == directory
        if not direct_file and path.suffix.casefold() not in _PLUGIN_FILE_SUFFIXES:
            continue
        if path.name.startswith("."):
            continue
        basename = path.name
        parent_name = path.parent.name if path.parent != directory else None
        file_stem = path.stem or basename
        name = parent_name or file_stem
        identities = _unique_identities((name, file_stem, basename, parent_name))
        records.append(
            PluginRecord(
                name=name,
                source="local_file",
                path=_relative(path, project_root),
                identities=identities,
                declared=False,
            )
        )
    return records, warnings, True


def _markdown_records(
    project_root: Path,
    relative_directory: str,
    *,
    skill: bool = False,
    parent_identity: bool = False,
) -> tuple[list[FileRecord], bool, list[str]]:
    directory = project_root / relative_directory
    warnings: list[str] = []
    try:
        directory_exists = directory.is_dir()
    except OSError as exc:
        return [], False, [f"cannot inspect {relative_directory}: {exc}"]
    if not directory_exists:
        return [], False, []
    try:
        paths = sorted(path for path in directory.rglob("*.md") if path.is_file())
    except OSError as exc:
        return [], True, [f"cannot scan {relative_directory}: {exc}"]

    records: list[FileRecord] = []
    for path in paths:
        if skill and path.name != "SKILL.md":
            continue
        stem = path.stem
        parent_name = path.parent.name if path.parent != directory else None
        name = parent_name if (skill or parent_identity) and parent_name else stem
        identities = _unique_identities((name,) if skill else (name, stem, path.name, parent_name))
        records.append(
            FileRecord(name=name, path=_relative(path, project_root), identities=identities)
        )
    return records, True, warnings


def inventory_environment(project_root: Path) -> EnvironmentInventory:
    """Inventory project plugins, skills, commands, and agents.

    Only ``project_root/.opencode`` is inspected.  Missing files and malformed
    JSONC are represented as warnings and do not make the deterministic
    inventory crash.
    """

    root = Path(project_root)
    warnings: list[str] = []
    try:
        root_exists = root.is_dir()
    except OSError as exc:
        root_exists = False
        warnings.append(f"cannot inspect project_root: {exc}")
    if not root_exists:
        warnings.append(f"project_root does not exist: {root}")

    declared, config_files, config_available, config_valid, config_warnings = _read_plugin_config(
        root
    )
    local_plugins, local_warnings, plugins_dir_exists = _local_plugin_records(root)
    skills, skills_dir_exists, skill_warnings = _markdown_records(
        root, ".opencode/skills", skill=True
    )
    commands, commands_dir_exists, command_warnings = _markdown_records(root, ".opencode/commands")
    agents, agents_dir_exists, agent_warnings = _markdown_records(
        root, ".opencode/agents", parent_identity=True
    )
    warnings.extend(config_warnings)
    warnings.extend(local_warnings)
    warnings.extend(skill_warnings)
    warnings.extend(command_warnings)
    warnings.extend(agent_warnings)

    plugins = sorted(
        [*declared, *local_plugins],
        key=lambda record: (
            0 if record.declared else 1,
            record.name.casefold(),
            record.path.casefold(),
            record.raw or "",
        ),
    )
    return EnvironmentInventory(
        plugins=plugins,
        skills=skills,
        commands=commands,
        agents=agents,
        config_files=config_files,
        config_available=config_available,
        config_valid=config_valid,
        directories={
            "plugins": plugins_dir_exists,
            "skills": skills_dir_exists,
            "commands": commands_dir_exists,
            "agents": agents_dir_exists,
        },
        warnings=warnings,
    )


scan_environment = inventory_environment
scan_project_environment = inventory_environment


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return iso(value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC))
    if isinstance(value, Mapping):
        return {str(key): _json_safe(inner) for key, inner in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(inner) for inner in value]
    return value


def _environment_items(inventory: EnvironmentInventory) -> dict[str, list[dict[str, Any]]]:
    return {
        "plugins": [_record_to_dict(record) for record in inventory.plugins],
        "skills": [_record_to_dict(record) for record in inventory.skills],
        "commands": [_record_to_dict(record) for record in inventory.commands],
        "agents": [_record_to_dict(record) for record in inventory.agents],
    }


def _market_identifiers(item: Mapping[str, Any]) -> tuple[str | None, str | None, set[str]]:
    npm_package = normalize_npm_package(item.get("npm_package"))
    found_via = item.get("found_via")
    if (
        npm_package is None
        and isinstance(found_via, Sequence)
        and not isinstance(found_via, (str, bytes, bytearray))
        and any(str(source).startswith("npm:") for source in found_via)
    ):
        npm_package = normalize_npm_package(str(item.get("name") or ""))

    repo_url = normalize_repo_url(item.get("repo_url"))
    if (
        repo_url is None
        and isinstance(found_via, Sequence)
        and not isinstance(found_via, (str, bytes, bytearray))
        and any(str(source).startswith("github:") for source in found_via)
    ):
        repo_url = normalize_repo_url(str(item.get("name") or ""))

    identities: set[str] = set()
    name = item.get("name")
    if isinstance(name, str) and name.strip():
        identities.add(_casefold(name))
    if npm_package:
        identities.add(npm_package)
    if repo_url:
        identities.add(repo_url.casefold())
        slug = _repo_slug(repo_url)
        if slug:
            identities.add(_casefold(slug))
    return npm_package, repo_url, identities


def _config_evidence(
    item_npm: str | None,
    item_repo: str | None,
    item_identities: set[str],
    records: Sequence[PluginRecord],
) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for record in records:
        if not record.declared:
            continue
        if item_npm and record.npm_package == item_npm:
            evidence.append(
                {
                    "type": "npm_package",
                    "value": item_npm,
                    "source": "config",
                    "path": record.path,
                    "raw": record.raw,
                }
            )
        if item_repo and record.repo_url == item_repo:
            evidence.append(
                {
                    "type": "repo_url",
                    "value": item_repo,
                    "source": "config",
                    "path": record.path,
                    "raw": record.raw,
                }
            )
        if (
            not item_npm
            and not item_repo
            and any(_casefold(identity) in item_identities for identity in record.identities)
        ):
            evidence.append(
                {
                    "type": "plugin_identity",
                    "value": record.name,
                    "source": "config",
                    "path": record.path,
                    "raw": record.raw,
                }
            )
    return evidence


def _observed_evidence(
    item: Mapping[str, Any],
    item_identities: set[str],
    inventory: EnvironmentInventory,
) -> list[dict[str, Any]]:
    category = str(item.get("category") or "").casefold()
    if category in {"skill", "skills"}:
        groups: Sequence[tuple[str, Sequence[FileRecord]]] = (("skill", inventory.skills),)
    elif category in {"agent", "agents"}:
        groups = (("agent", inventory.agents),)
    elif category in {"command", "commands"}:
        groups = (("command", inventory.commands),)
    elif category in {"plugin", "mcp-server", "repo", ""}:
        groups = (("plugin", [record for record in inventory.plugins if not record.declared]),)
    else:
        groups = (
            ("plugin", [record for record in inventory.plugins if not record.declared]),
            ("skill", inventory.skills),
            ("command", inventory.commands),
            ("agent", inventory.agents),
        )

    evidence: list[dict[str, Any]] = []
    for kind, records in groups:
        for record in records:
            matched = next(
                (
                    identity
                    for identity in record.identities
                    if _casefold(identity) in item_identities
                ),
                None,
            )
            if matched is not None:
                evidence.append(
                    {
                        "type": "basename" if kind == "plugin" else kind,
                        "value": matched,
                        "source": "worktree",
                        "path": record.path,
                        "name": record.name,
                    }
                )
    return evidence


def _match_summary(evidence: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not evidence:
        return None
    first = dict(evidence[0])
    first["evidence"] = evidence
    return first


def _category_is_observable(item: Mapping[str, Any], inventory: EnvironmentInventory) -> bool:
    category = str(item.get("category") or "").casefold()
    if category in {"skill", "skills"}:
        return inventory.directories.get("skills", False)
    if category in {"command", "commands"}:
        return inventory.directories.get("commands", False)
    if category in {"agent", "agents"}:
        return inventory.directories.get("agents", False)
    if category in {"plugin", "mcp-server", "repo", ""}:
        item_npm, item_repo, _identities = _market_identifiers(item)
        if item_npm or item_repo:
            # A local plugin directory cannot prove that a package/repository
            # is absent when the project plugin config is unavailable.
            return inventory.config_available and inventory.config_valid
        return inventory.config_available or inventory.directories.get("plugins", False)
    return any(inventory.directories.values())


def _match_market_item(item: Mapping[str, Any], inventory: EnvironmentInventory) -> dict[str, Any]:
    item_npm, item_repo, item_identities = _market_identifiers(item)
    declared_evidence = _config_evidence(item_npm, item_repo, item_identities, inventory.plugins)
    observed_evidence = _observed_evidence(item, item_identities, inventory)
    if declared_evidence:
        state: ExistingState = "declared"
        evidence = declared_evidence
    elif observed_evidence:
        state = "observed"
        evidence = observed_evidence
    elif not item_npm and not item_repo and not item_identities:
        state = "unknown"
        evidence = []
    elif _category_is_observable(item, inventory):
        state = "absent"
        evidence = []
    else:
        # A missing/invalid plugin config cannot prove that a package or repo
        # is absent.  Keeping it unknown prevents a false adopt candidate.
        state = "unknown"
        evidence = []

    result = _json_safe(dict(item))
    result["existing_state"] = state
    # Identity proves that the capability is represented by the project, but
    # absence never proves that no equivalent capability exists.  The LLM may
    # therefore investigate `unknown`; it must not turn it into `adopt` without
    # evidence from the quality findings.
    result["capability_state"] = "covered" if state in {"declared", "observed"} else "unknown"
    result["match"] = _match_summary(evidence)
    result["normalized"] = {
        "npm_package": item_npm,
        "repo_url": item_repo,
    }
    return result


def _ecosystem_items(ecosystem: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    for key in ("new_items", "items"):
        values = ecosystem.get(key)
        if isinstance(values, Sequence) and not isinstance(values, (str, bytes, bytearray)):
            return [value for value in values if isinstance(value, Mapping)]
    return []


def build_watch_context(
    project_root: Path,
    ecosystem: Mapping[str, Any],
    *,
    generated_at: datetime | str | None = None,
    ecosystem_path: Path | None = None,
) -> dict[str, Any]:
    """Build a deterministic watch context from one ecosystem report.

    ``ecosystem`` is treated as an input snapshot.  The function does not
    fetch, mutate, deduplicate across runs, or write lifecycle state.  The
    caller controls the timestamp so repeated runs with the same anchor and
    worktree produce stable context content apart from filesystem changes.
    """

    inventory = inventory_environment(Path(project_root))
    if generated_at is None:
        run_time = datetime.now(UTC)
        generated = iso(run_time)
    elif isinstance(generated_at, datetime):
        run_time = (
            generated_at.astimezone(UTC)
            if generated_at.tzinfo
            else generated_at.replace(tzinfo=UTC)
        )
        generated = iso(run_time)
    else:
        generated = str(generated_at)
        try:
            parsed = datetime.fromisoformat(generated.replace("Z", "+00:00"))
            run_time = parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            run_time = datetime.now(UTC)
    environment = _environment_items(inventory)
    market_matches = [_match_market_item(item, inventory) for item in _ecosystem_items(ecosystem)]
    market_matches.sort(
        key=lambda item: (
            str(item.get("name") or "").casefold(),
            str(item.get("npm_package") or "").casefold(),
            str(item.get("repo_url") or "").casefold(),
        )
    )
    counts = {name: len(items) for name, items in environment.items()}
    counts["declared_plugins"] = sum(1 for record in inventory.plugins if record.declared)
    counts["local_plugins"] = sum(1 for record in inventory.plugins if not record.declared)

    context: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated,
        "date": run_time.strftime("%Y-%m-%d"),
        "project_root": ".",
        "plugin_config": {
            "files": inventory.config_files,
            "available": inventory.config_available,
            "valid": inventory.config_valid,
        },
        "plugins": environment["plugins"],
        "declared_plugins": [
            item for item in environment["plugins"] if item.get("declared") is True
        ],
        "local_plugins": [
            item for item in environment["plugins"] if item.get("declared") is not True
        ],
        "skills": environment["skills"],
        "commands": environment["commands"],
        "agents": environment["agents"],
        "counts": counts,
        "market_matches": market_matches,
        "warnings": sorted(set(inventory.warnings)),
    }
    if ecosystem_path is not None:
        context["ecosystem_file"] = ecosystem_path.name
    if isinstance(ecosystem.get("generated_at"), str):
        context["ecosystem_generated_at"] = ecosystem["generated_at"]
    return context


build_context = build_watch_context


def load_ecosystem_report(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """Load one local ecosystem JSON report for the CLI.

    The tuple contains ``(payload, error)``; no global or fallback path is
    consulted.  Ecosystem reports are regular JSON, but JSONC is accepted for
    a forgiving hand-edited fixture and to share the safe parser.
    """

    try:
        payload = load_jsonc(path)
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"cannot read ecosystem report {path}: {exc}"
    if not isinstance(payload, dict):
        return None, f"ecosystem report root must be an object: {path}"
    return dict(payload), None
