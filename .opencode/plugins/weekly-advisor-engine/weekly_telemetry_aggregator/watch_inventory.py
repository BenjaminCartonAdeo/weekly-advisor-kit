"""Deterministic, worktree-only inventory of the project's local surface.

Isolated from :mod:`watch_context` because the two concerns are independent:
this module only *describes what the project already has* (plugins declared in
``.opencode`` or found on disk, skills, commands, agents, plus the identity
normalisers that turn raw text into comparable keys), while ``watch_context``
*matches that inventory against the ecosystem collector output*.

The split keeps this module a leaf: stdlib plus ``identities``/``util`` only.
It must never import ``watch_distill`` or ``report`` — ``watch_distill`` needs
the npm identity from ``identities``, and the residual band that does use the
distill scorer lives on the ``watch_context`` side.

Only ``project_root/.opencode`` is inspected; nothing global and no network.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote, urlsplit

from .identities import _package_spec_parts, normalize_npm_package
from .util import casefold, load_jsonc, relative_path

PluginSource = Literal["config", "local_file"]

_CONFIG_NAMES = ("opencode.json", "opencode.jsonc")
_PLUGIN_FILE_SUFFIXES = {".cjs", ".cts", ".js", ".mjs", ".mts", ".ts", ".tsx"}
#: Plafond de noms locaux pertinents par fiche (T6).
HINTS_CAP = 5
_TOKEN_SPLIT_RE = re.compile(r"[^a-z0-9]+")


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
    if isinstance(record, PluginRecord | FileRecord):
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
            warnings.append(f"cannot inspect {relative_path(path, project_root)}: {exc}")
            continue
        if not exists:
            continue
        available = True
        config_files.append(relative_path(path, project_root))
        payload = load_jsonc(path)
        if payload is None:
            warnings.append(f"invalid {relative_path(path, project_root)}: JSON illisible")
            continue
        if not isinstance(payload, Mapping):
            warnings.append(f"invalid {relative_path(path, project_root)}: root must be an object")
            continue
        valid = True
        declarations = payload.get("plugin", [])
        if isinstance(declarations, str):
            declarations = [declarations]
        if not isinstance(declarations, Sequence) or isinstance(declarations, (bytes, bytearray)):
            valid = False
            warnings.append(
                f"invalid {relative_path(path, project_root)}: plugin must be an array of strings"
            )
            continue
        for declaration in declarations:
            if isinstance(declaration, str) and declaration.strip():
                records.append(
                    parse_plugin_spec(declaration, path=relative_path(path, project_root))
                )
            else:
                warnings.append(
                    f"ignored non-string plugin declaration in {relative_path(path, project_root)}"
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
                path=relative_path(path, project_root),
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
            FileRecord(name=name, path=relative_path(path, project_root), identities=identities)
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


def _environment_items(inventory: EnvironmentInventory) -> dict[str, list[dict[str, Any]]]:
    return {
        "plugins": [_record_to_dict(record) for record in inventory.plugins],
        "skills": [_record_to_dict(record) for record in inventory.skills],
        "commands": [_record_to_dict(record) for record in inventory.commands],
        "agents": [_record_to_dict(record) for record in inventory.agents],
    }


# ------------------------------------------- T6 : inventaire local + crosswalk


def _markdown_description(path: Path) -> str:
    """Description d'un markdown local : frontmatter ``description:`` sinon
    première ligne utile (titres ``#`` décapités) ; ``""`` si illisible."""

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return ""
    body = lines
    if lines and lines[0].strip() == "---":
        body = lines[1:]
        for index, line in enumerate(lines[1:], start=1):
            stripped = line.strip()
            if stripped == "---":
                body = lines[index + 1 :]
                break
            if stripped.startswith("description:"):
                value = stripped[len("description:") :].strip().strip("\"'")
                if value:
                    return value
    for line in body:
        text = line.strip()
        if text:
            return text.lstrip("#").strip()
    return ""


def build_local_inventory(project_root: Path) -> dict[str, Any]:
    """Inventaire structuré des capacités locales du projet (zéro LLM/réseau).

    Agrège sous ``project_root/.opencode`` les skills (SKILL.md), commands,
    agents et plugins (locaux + déclarés) en entrées compactes
    ``{name, kind, path, description}``. Réutilise les scanners déterministes
    existants ; toute anomalie de lecture devient un warning, jamais un crash.
    """

    root = Path(project_root)
    skills, _, skill_warnings = _markdown_records(root, ".opencode/skills", skill=True)
    commands, _, command_warnings = _markdown_records(root, ".opencode/commands")
    agents, _, agent_warnings = _markdown_records(root, ".opencode/agents", parent_identity=True)
    local_plugins, plugin_warnings, _ = _local_plugin_records(root)
    declared_plugins, _, _, _, config_warnings = _read_plugin_config(root)

    items: list[dict[str, Any]] = []
    for records, kind in ((skills, "skill"), (commands, "command"), (agents, "agent")):
        for record in records:
            items.append(
                {
                    "name": record.name,
                    "kind": kind,
                    "path": record.path,
                    "description": _markdown_description(root / record.path),
                }
            )
    # Plugin : aucune description locale fiable (spec brute ou fichier vide).
    for record in [*local_plugins, *declared_plugins]:
        items.append(
            {"name": record.name, "kind": "plugin", "path": record.path, "description": ""}
        )
    items.sort(key=lambda item: (item["kind"], casefold(item["name"]), item["path"]))
    warnings = [
        *skill_warnings,
        *command_warnings,
        *agent_warnings,
        *plugin_warnings,
        *config_warnings,
    ]
    return {"items": items, "warnings": sorted(set(warnings))}


def _tokens(text: object) -> frozenset[str]:
    """Jetons normalisés d'un texte : minuscules, split non-alphanum, ≥3 chars."""

    return frozenset(
        token for token in _TOKEN_SPLIT_RE.split(str(text or "").casefold()) if len(token) >= 3
    )


def hints_for(fiche: Mapping[str, Any], inventory_items: Sequence[Mapping[str, Any]]) -> list[str]:
    """Noms locaux pertinents pour une fiche, par intersection de jetons (cap 5).

    Jetons de ``name+summary`` (côté fiche) croisés avec ceux de la seule
    ``description`` locale ; un seul jeton commun suffit. Déterministe : ordre
    de l'inventaire, noms dédoublonnés.
    """

    fiche_tokens = _tokens(f"{fiche.get('name') or ''} {fiche.get('summary') or ''}")
    if not fiche_tokens:
        return []
    hints: list[str] = []
    for item in inventory_items:
        name = str(item.get("name") or "")
        if not name or name in hints or len(hints) >= HINTS_CAP:
            continue
        if fiche_tokens & _tokens(item.get("description")):
            hints.append(name)
    return hints
