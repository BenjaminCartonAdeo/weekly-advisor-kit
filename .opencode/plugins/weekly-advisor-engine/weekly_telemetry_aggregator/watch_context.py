"""Deterministic, worktree-only context for the weekly ecosystem review.

The ecosystem collector deliberately knows nothing about the project in which
the weekly advisor is installed.  This module is the small deterministic join
between the collector output and that project state.  It only reads paths
under ``project_root`` and never consults OpenCode's global configuration,
global skills, telemetry, or a network service.

The public builder returns ordinary JSON-compatible dictionaries so the module
can be used by the CLI and by the LLM-facing report stages without introducing
another persistence format or a cross-run state file.

The worktree inventory itself (plugins, skills, commands, agents and the
identity normalisers) lives in :mod:`watch_inventory`; the names it used to
export are re-exported here for backwards compatibility.  What stays here is
the evidence side: matching an ecosystem item against the inventory, scoping
the market matches, and the residual band, which needs the distill scorer.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from .identities import normalize_npm_package
from .util import casefold, iso, load_jsonc, parse_iso_ts
from .watch_distill import DEFAULT_WEIGHTS, score_item, truncate_summary
from .watch_inventory import (
    HINTS_CAP,
    EnvironmentInventory,
    FileRecord,
    PluginRecord,
    _environment_items,
    _repo_slug,
    build_local_inventory,
    hints_for,
    inventory_environment,
    normalize_repo_url,
)
from .watch_memory import normalize_id

ExistingState = Literal["absent", "declared", "observed", "unknown"]
EXISTING_STATES = frozenset(("absent", "declared", "observed", "unknown"))

SCHEMA_VERSION = 1

#: Schéma du fichier ``watch-candidates-enriched-<date>.json`` (T6).
ENRICHED_SCHEMA_VERSION = 1
#: Plafond d'entrées dans la bande résiduelle sous cutoff (T6).
RESIDUAL_CAP = 50
#: Longueur maximale d'une description compacte (fiche/résiduel, T6).
ENRICHED_DESCRIPTION_MAX_CHARS = 200

#: Surface publique. Les réexports ``watch_inventory`` sont listés pour que les
#: importateurs historiques continuent de résoudre ces noms ici.
__all__ = [
    "EXISTING_STATES",
    "ENRICHED_DESCRIPTION_MAX_CHARS",
    "ENRICHED_SCHEMA_VERSION",
    "HINTS_CAP",
    "RESIDUAL_CAP",
    "SCHEMA_VERSION",
    "EnvironmentInventory",
    "ExistingState",
    "FileRecord",
    "PluginRecord",
    "build_local_inventory",
    "build_watch_context",
    "enrich_candidates",
    "hints_for",
    "inventory_environment",
    "load_ecosystem_report",
    "normalize_npm_package",
    "normalize_repo_url",
]


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return iso(value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC))
    if isinstance(value, Mapping):
        return {str(key): _json_safe(inner) for key, inner in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(inner) for inner in value]
    return value


def _market_identifiers(item: Mapping[str, Any]) -> tuple[str | None, str | None, set[str]]:
    npm_package = normalize_npm_package(item.get("npm_package"))
    found_via = item.get("found_via")
    if (
        npm_package is None
        and isinstance(found_via, Sequence)
        and not isinstance(found_via, str | bytes | bytearray)
        and any(str(source).startswith("npm:") for source in found_via)
    ):
        npm_package = normalize_npm_package(str(item.get("name") or ""))

    repo_url = normalize_repo_url(item.get("repo_url"))
    if (
        repo_url is None
        and isinstance(found_via, Sequence)
        and not isinstance(found_via, str | bytes | bytearray)
        and any(str(source).startswith("github:") for source in found_via)
    ):
        repo_url = normalize_repo_url(str(item.get("name") or ""))

    identities: set[str] = set()
    name = item.get("name")
    if isinstance(name, str) and name.strip():
        identities.add(casefold(name))
    if npm_package:
        identities.add(npm_package)
    if repo_url:
        identities.add(repo_url.casefold())
        slug = _repo_slug(repo_url)
        if slug:
            identities.add(casefold(slug))
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
            and any(casefold(identity) in item_identities for identity in record.identities)
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
                    if casefold(identity) in item_identities
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


def _architecture_observations(
    inventory: EnvironmentInventory,
    market_matches: Sequence[Mapping[str, Any]],
    harness_scope: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a read-only snapshot useful for detecting configuration drift.

    This is deliberately a projection of facts already collected by the
    crosswalk.  It does not infer intent and never proposes or applies a
    change.  Keeping the three existing states (declared/observed/absent)
    explicit also prevents an unavailable config from being reported as absent.
    """
    states = {state: 0 for state in ("declared", "observed", "absent", "unknown")}
    for match in market_matches:
        state = match.get("existing_state")
        if state in states:
            states[state] += 1
    return {
        "state_counts": states,
        "config": {
            "files": list(inventory.config_files),
            "available": inventory.config_available,
            "valid": inventory.config_valid,
        },
        "inventory_counts": {
            "plugins": len(inventory.plugins),
            "skills": len(inventory.skills),
            "commands": len(inventory.commands),
            "agents": len(inventory.agents),
        },
        "harness_scope": dict(harness_scope) if isinstance(harness_scope, Mapping) else None,
    }


def _ecosystem_items(ecosystem: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    for key in ("new_items", "items"):
        values = ecosystem.get(key)
        if isinstance(values, Sequence) and not isinstance(values, str | bytes | bytearray):
            return [value for value in values if isinstance(value, Mapping)]
    return []


def _validate_candidates(payload: object) -> str | None:
    """Contrat minimal d'un snapshot watch-candidates ; message d'erreur sinon."""

    if not isinstance(payload, Mapping):
        return "watch-candidates invalide : racine non objet"
    candidates = payload.get("candidates")
    is_list = isinstance(candidates, Sequence) and not isinstance(
        candidates, str | bytes | bytearray
    )
    if payload.get("mode") != "distill" or not is_list:
        return "watch-candidates invalide : mode/candidats inattendus"
    if not all(isinstance(row, Mapping) for row in candidates):
        return "watch-candidates invalide : fiche non objet"
    return None


def _load_valid_candidates(path: Path | None) -> tuple[Mapping[str, Any] | None, str | None]:
    """Charge un snapshot watch-candidates ; ``(payload, erreur)``."""

    if path is None:
        return None, None
    payload = load_jsonc(path)
    if payload is None:
        return None, f"watch-candidates illisible ({Path(path).name}) : flux legacy conservé"
    error = _validate_candidates(payload)
    if error is not None:
        return None, f"{error} ({Path(path).name}) : flux legacy conservé"
    return payload, None


def _candidate_ids(payload: Mapping[str, Any]) -> tuple[set[str], set[str]]:
    """Ids des fiches retenues + ids bloqués sécurité (annexe), ensemblistes."""

    fiches = payload.get("candidates")
    kept = {
        str(row.get("id"))
        for row in (fiches if isinstance(fiches, Sequence) else [])
        if isinstance(row, Mapping) and row.get("id")
    }
    annex = payload.get("security_annex")
    blocked: set[str] = set()
    if isinstance(annex, Sequence) and not isinstance(annex, str | bytes | bytearray):
        blocked = {
            str(row.get("id")) for row in annex if isinstance(row, Mapping) and row.get("id")
        }
    return kept, blocked


def _item_identity(item: Mapping[str, Any]) -> str:
    """Id canonique d'un item écosystème (même calcul que le distill)."""

    return normalize_id(str(item.get("name") or ""), item.get("npm_package"), item.get("repo_url"))


def _residual_entries(
    ecosystem: Mapping[str, Any],
    *,
    exclude_ids: set[str],
    now: datetime,
    extra_keywords: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """Bande sous cutoff : entrées compactes scorées, triées, plafonnées à 50.

    Réutilise le scoring du distill avec les poids par défaut ; tri
    ``(-score, id)`` pour un plafonnage reproductible. Les ids exclus
    (candidats retenus + bloqués sécurité) n'y figurent jamais.

    L'import est au niveau module : le cycle qui le motivait (``watch_distill``
    → ``watch_context`` pour l'identité npm) est rompu, l'identité vit désormais
    dans ``identities``.
    """
    keywords = tuple(extra_keywords)
    rows: list[tuple[float, str, dict[str, Any]]] = []
    for item in _ecosystem_items(ecosystem):
        identity = _item_identity(item)
        if identity in exclude_ids:
            continue
        total = float(
            score_item(item, weights=dict(DEFAULT_WEIGHTS), now=now, extra_keywords=keywords)[
                "total"
            ]
        )
        rows.append(
            (
                total,
                identity,
                {
                    "id": identity,
                    "name": str(item.get("name") or ""),
                    "description": truncate_summary(
                        item.get("description"), ENRICHED_DESCRIPTION_MAX_CHARS
                    ),
                    "score_total": round(total, 3),
                },
            )
        )
    rows.sort(key=lambda row: (-row[0], row[1]))
    return [row[2] for row in rows[:RESIDUAL_CAP]]


def build_watch_context(
    project_root: Path,
    ecosystem: Mapping[str, Any],
    *,
    generated_at: datetime | str | None = None,
    ecosystem_path: Path | None = None,
    candidates_path: Path | None = None,
    extra_keywords: Sequence[str] = (),
    harness_scope: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a deterministic watch context from one ecosystem report.

    ``ecosystem`` is treated as an input snapshot.  The function does not
    fetch, mutate, deduplicate across runs, or write lifecycle state.  The
    caller controls the timestamp so repeated runs with the same anchor and
    worktree produce stable context content apart from filesystem changes.

    Crosswalk candidats (T6) : si ``candidates_path`` pointe un snapshot
    ``watch-candidates-<date>.json`` valide (mode ``distill``), les
    ``market_matches`` sont restreints aux fiches retenues ET aux items
    résiduels sous cutoff (plafonnés, hors annexe sécurité) afin que leurs
    findings passent la validation 3.6. Snapshot absent → comportement legacy
    inchangé ; corrompu/invalide → legacy + warning.
    """

    inventory = inventory_environment(Path(project_root))
    candidates, candidates_error = _load_valid_candidates(candidates_path)
    if candidates_error is not None:
        inventory.warnings.append(candidates_error)
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
        parsed = parse_iso_ts(str(generated_at))
        run_time = parsed.astimezone(UTC) if parsed is not None else datetime.now(UTC)
        generated = iso(run_time)
    environment = _environment_items(inventory)
    eco_items = _ecosystem_items(ecosystem)
    if candidates is not None:
        # Scope appliqué dès que le snapshot est valide, même si aucune fiche
        # n'est retenue : les bloqués sécurité doivent être exclus des deux
        # artefacts (contexte ET enrichi), jamais seulement de l'un.
        kept_ids, blocked_ids = _candidate_ids(candidates)
        residual = _residual_entries(
            ecosystem,
            exclude_ids=kept_ids | blocked_ids,
            now=run_time,
            extra_keywords=extra_keywords,
        )
        scope_ids = kept_ids | {row["id"] for row in residual}
        eco_items = [item for item in eco_items if _item_identity(item) in scope_ids]
    market_matches = [_match_market_item(item, inventory) for item in eco_items]
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
    context["architecture_observations"] = _architecture_observations(
        inventory, market_matches, harness_scope
    )
    if ecosystem_path is not None:
        context["ecosystem_file"] = ecosystem_path.name
    if isinstance(ecosystem.get("generated_at"), str):
        context["ecosystem_generated_at"] = ecosystem["generated_at"]
    return context


def enrich_candidates(
    candidates: Mapping[str, Any] | None,
    context: Mapping[str, Any],
    ecosystem: Mapping[str, Any],
    inventory_items: Sequence[Mapping[str, Any]],
    *,
    now: datetime,
    extra_keywords: Sequence[str] = (),
) -> dict[str, Any] | None:
    """Fusion fiches × contexte local → ``watch-candidates-enriched-<date>.json``.

    Chaque fiche reçoit ``existing_state``, ``market_match`` (preuve issue du
    contexte scoppé) et ``local_relevance_hints`` remplies ; les items éco-
    système sous cutoff forment ``residual`` (entrées compactes scorées,
    plafonnées, hors fiches retenues et bloqués sécurité). Retourne ``None``
    si le snapshot candidats est invalide : l'appelant retombe alors sur le
    flux legacy sans fichier enrichi. Aucune écriture ici — l'appelant persiste.
    """

    if _validate_candidates(candidates) is not None:
        return None
    kept_ids, blocked_ids = _candidate_ids(candidates)
    matches_by_id: dict[str, Mapping[str, Any]] = {}
    for match in context.get("market_matches") or []:
        if isinstance(match, Mapping):
            matches_by_id[_item_identity(match)] = match

    enriched_fiches: list[dict[str, Any]] = []
    for fiche in candidates["candidates"]:
        out = dict(fiche)
        match = matches_by_id.get(str(fiche.get("id")))
        state = match.get("existing_state") if match is not None else None
        out["existing_state"] = (
            str(state) if isinstance(state, str) and state in EXISTING_STATES else "unknown"
        )
        out["market_match"] = match.get("match") if match is not None else None
        out["local_relevance_hints"] = hints_for(fiche, inventory_items)
        enriched_fiches.append(out)

    return {
        "schema_version": ENRICHED_SCHEMA_VERSION,
        "mode": "enriched",
        "date": now.strftime("%Y-%m-%d"),
        "candidates": enriched_fiches,
        "residual": _residual_entries(
            ecosystem,
            exclude_ids=kept_ids | blocked_ids,
            now=now,
            extra_keywords=extra_keywords,
        ),
        "warnings": [],
    }


def load_ecosystem_report(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """Load one local ecosystem JSON report for the CLI.

    The tuple contains ``(payload, error)``; no global or fallback path is
    consulted.  Ecosystem reports are regular JSON, but JSONC is accepted for
    a forgiving hand-edited fixture and to share the safe parser.
    """

    payload = load_jsonc(path)
    if payload is None:
        return None, f"cannot read ecosystem report {path}: JSON illisible"
    return dict(payload), None
