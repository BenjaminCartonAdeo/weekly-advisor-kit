"""Orchestration of Part 1 (spec flow steps 1-6) + doctor & self-cost (Part 1 §12).

The pipeline reads OpenCode telemetry directly from the local SQLite DB via the
SchemaAdapter (sqlite_reader.py, v5.16/v5.24) — no SDK, no server, no pricing chain:
`step-finish.cost` is read as-is (spec §2, "une seule règle").
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import re
import shutil
import subprocess
import sys
import tempfile
import warnings
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path
from warnings import warn as _warn_user

from .aggregator import _cap_warnings, aggregate, dedup_resumed_usages
from .config import TelemetryConfig, apply_lookback_override
from .draft_targets import DRAFT_HARNESS_TARGETS, resolve_draft_targets
from .harness_scope import (
    copy_scope_to_projection,
    enrich_harness_digest,
    harness_extra_roots,
    harness_rules_fingerprint,
    inject_engine_content,
    resolve_harness_scope,
    resolve_remediation_surface,
)
from .models import (
    Period,
    SessionUsage,
    WarningEntry,
    canonical_session_id,
)
from .providers import SessionProvider, build_providers
from .run_state import RUNS_DIR, activate_run, resolve_active_run_dir

# La surface skills (résolution + scan, A4/A5/A6) vit dans `skill_surface` :
# `curation` en dépend, et la garder ici imposait un import différé dans les deux
# sens. Ré-exportée pour préserver l'API `main.resolve_skill_surface` etc.
from .skill_surface import SKILL_ARCHIVE_DIR_NAME as SKILL_ARCHIVE_DIR_NAME
from .skill_surface import SkillRecord as SkillRecord
from .skill_surface import SkillSurface as SkillSurface
from .skill_surface import _is_archived_skill as _is_archived_skill
from .skill_surface import _parse_skill_md as _parse_skill_md
from .skill_surface import resolve_skill_surface as resolve_skill_surface
from .skill_surface import scan_skill_catalog as scan_skill_catalog
from .skill_surface import scan_skill_records as scan_skill_records
from .sqlite_reader import DataSourceError, _to_ms, detect_db

# La construction des `SessionUsage` (fenêtre, exclusions, cross-checks de coût) vit
# dans `usage` : c'est le bloc le plus volumineux de `main` et il ne dépend que de
# `models`/`config`/`providers` — le garder ici imposait un import différé dès que
# `usage` aurait besoin d'un nom de `main`. Ré-exporté pour préserver l'API
# `main.build_usage`, `main._truncate`, `main.ACTIVE_CUTOFF_MINUTES`, etc.
from .usage import ACTIVE_CUTOFF_MINUTES as ACTIVE_CUTOFF_MINUTES
from .usage import CROSS_CHECK_ABS as CROSS_CHECK_ABS
from .usage import DEFAULT_HARNESS_COST_RATE_USD_PER_MTOK as DEFAULT_HARNESS_COST_RATE_USD_PER_MTOK
from .usage import HARNESS_COST_RATES_USD_PER_MTOK as HARNESS_COST_RATES_USD_PER_MTOK
from .usage import _audit_record as _audit_record
from .usage import _fetch_session_reads as _fetch_session_reads
from .usage import _harness_cost_rates as _harness_cost_rates
from .usage import _session_part_timestamps as _session_part_timestamps
from .usage import _SessionReads as _SessionReads
from .usage import _truncate as _truncate
from .usage import _usage_active_excluded as _usage_active_excluded
from .usage import _usage_advisor_excluded as _usage_advisor_excluded
from .usage import _usage_cost_warnings as _usage_cost_warnings
from .usage import _usage_no_steps_status as _usage_no_steps_status
from .usage import build_usage as build_usage
from .usage import estimate_costs as estimate_costs
from .util import (
    HARNESS_BASELINE_FILE,
    _abs,
    iter_digest_findings,
    load_json,
    parse_iso_ts,
    root_and_orphan_ids,
)
from .util import parse_anchor as _parse_anchor
from .writer import write_json_atomic, write_summary

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_TOTAL_FAILURE = 2

# Surface déplacée dans `doctor` (doctor(), ses checks, `_check_migrations`).
# `doctor` importe de `main`, donc `main` ne peut pas l'importer au niveau module
# sans créer un cycle — d'où PEP 562 : `from .main import doctor` reste valide,
# l'attribut est résolu à la première utilisation, jamais au chargement.
_DOCTOR_SURFACE = frozenset(
    {
        "_check_migrations",
        "_copilot_doctor_details",
        "_doctor_draft_targets",
        "_doctor_harness_eval_version",
        "_doctor_opencode_version",
        "_doctor_output_dir_guard",
        "_doctor_output_probe",
        "_doctor_project_root",
        "_doctor_session_providers",
        "_doctor_tool_presence",
        "_doctor_watch_repos",
        "_unknown_session_source_types",
        "doctor",
    }
)


def __getattr__(name: str):
    if name in _DOCTOR_SURFACE:
        from . import doctor as _doctor

        return getattr(_doctor, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


@dataclasses.dataclass(frozen=True, slots=True)
class RunProvenance:
    """Stable identity attached to every run artefact.

    ``start_time`` is the canonical wall-clock field.  ``run_started_at`` is
    retained as a compatibility alias for consumers of the previous payload.
    """

    repository_path: str | None
    branch: str | None
    commit_sha: str | None
    working_tree_dirty: bool | None
    start_time: str
    pipeline_version: str

    def as_dict(self) -> dict[str, object]:
        payload = dataclasses.asdict(self)
        payload["run_started_at"] = self.start_time
        return payload


def _run_provenance(
    project_root: Path | None, run_started_at: datetime | None = None
) -> dict[str, object]:
    """Collect stable run identity without making git a pipeline dependency."""
    root = Path(project_root) if project_root else None
    provenance = RunProvenance(
        repository_path=str(root.resolve()) if root else None,
        branch=None,
        commit_sha=None,
        working_tree_dirty=None,
        start_time=(run_started_at or datetime.now().astimezone()).isoformat(),
        pipeline_version=__import__("weekly_telemetry_aggregator").__version__,
    )
    result = provenance.as_dict()
    if root is None or not (root / ".git").exists():
        return result
    try:

        def git(*args: str) -> str:
            return subprocess.run(
                ["git", "-C", str(root), *args],
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            ).stdout.strip()

        result["branch"] = git("branch", "--show-current") or None
        result["commit_sha"] = git("rev-parse", "HEAD") or None
        result["working_tree_dirty"] = bool(git("status", "--porcelain"))
    except (OSError, subprocess.SubprocessError):
        pass
    return result


# NOTE: `SKILL_ARCHIVE_DIR_NAME` (A6) vit dans `skill_surface` avec le scan qui
# l'utilise, et est ré-exporté par le bloc d'imports ci-dessus.


def _build_selection(
    audit: list[dict],
    limit: int,
    known_parent_ids: set[str] | None = None,
    period: dict | None = None,
) -> dict:
    """Aggregate the per-session disposition trace into the summary's selection audit.

    v6.0.l (E2) : ``counted`` = **racines comptées**, même convention que
    ``aggregate()`` — les enfants dont le parent est dans la fenêtre sont
    fusionnés dans leur racine, jamais comptés individuellement (fini le
    28 vs 29 : le rapport compte 28 racines et expose les enfants fusionnés
    séparément). ``counted_all`` garde le décompte brut des enregistrements
    inclus, ``merged_children`` la différence.
    """
    counts: dict[str, int] = {}
    for rec in audit:
        counts[rec["status"]] = counts.get(rec["status"], 0) + 1
    included = [rec for rec in audit if rec["status"] == "included"]
    # Même logique d'orphelins que aggregate() (util.root_and_orphan_ids): un
    # enfant dont le parent n'est ni dans la fenêtre ni connu en base n'est
    # fusionné nulle part.
    orphan_ids, root_ids = root_and_orphan_ids(
        ((r["session_id"], r.get("parent_id")) for r in included),
        known_parent_ids=known_parent_ids,
    )
    cores = [rec for rec in included if rec["session_id"] in root_ids]
    recent = sorted(audit, key=lambda r: r["updated"], reverse=True)[: max(0, limit)]
    # v6.0.n : marquage fenêtre — les sessions actives post-fenêtre (runs récents
    # hors période) restent listées mais séparées dans le rapport (§1).
    # period peut être un dict (tests/JSON) ou un objet Period du summary.
    if isinstance(period, dict):
        period_start, period_end = period.get("start"), period.get("end")
    else:
        period_start, period_end = getattr(period, "start", None), getattr(period, "end", None)
    window_start = parse_iso_ts(period_start)
    window_end = parse_iso_ts(period_end)
    for rec in recent:
        ts = parse_iso_ts(rec.get("updated"))
        rec["in_window"] = bool(
            ts is not None
            and (window_start is None or ts >= window_start)
            and (window_end is None or ts <= window_end)
        )
    return {
        "window_touched": len(audit),
        "counted": len(cores),
        "counted_all": len(included),
        "merged_children": len(included) - len(cores),
        "excluded_active": counts.get("active", 0),
        "excluded_advisor": counts.get("advisor", 0),
        "excluded_no_activity": counts.get("no-activity", 0),
        "excluded_unflushed": counts.get("unflushed", 0),
        "excluded_error": counts.get("error", 0),
        "resumed_duplicates": counts.get("resumed-duplicate", 0),
        # Keep the complete worker/session disposition index separate from the
        # bounded ``recent`` view; report consumers must not lose rc/truncation
        # state when the display limit is small.
        "worker_statuses": [
            {
                key: rec[key]
                for key in ("session_id", "status", "rc", "truncated", "worker_status")
                if key in rec
            }
            for rec in audit
            if any(key in rec for key in ("rc", "truncated", "worker_status"))
        ],
        "recent": recent,
    }


def _existing_period(path: Path) -> tuple[str | None, str | None]:
    """(start, end) ISO strings of an existing summary JSON; (None, None) on garbage."""
    data = load_json(path) or {}
    period = data.get("period")
    if not isinstance(period, dict):
        return None, None
    return period.get("start"), period.get("end")


# --------------------------------------------------------------------------- run


def _warn_fallback_local_db(cfg: TelemetryConfig) -> None:
    """Warning de repli (module-level : run() shadowe `warnings` avec sa liste)."""
    warnings.warn(
        "aucune source de sessions active — repli sur la base OpenCode locale "
        f"({cfg.opencode_db_path})",
        stacklevel=3,
    )


def _run_placeholder_guard(cfg: TelemetryConfig, warnings: list[WarningEntry]) -> None:
    """Sentinelle placeholders #12 — warning visible, non fatal, run continue."""
    placeholder_fields = _placeholder_fields(cfg)
    if placeholder_fields:
        message = _placeholder_message(placeholder_fields)
        _warn_user(message, stacklevel=2)
        warnings.append(WarningEntry(session_id=None, message=message))


def _run_fallback_providers(cfg: TelemetryConfig) -> list[SessionProvider] | None:
    """Repli rétrocompatible sur la base OpenCode locale — None si base illisible."""
    _warn_fallback_local_db(cfg)
    try:
        _path, adapter = detect_db(cfg.opencode_db_path)
    except DataSourceError as exc:
        print(f"telemetry-aggregator: FATAL: {exc} — lancer doctor", file=sys.stderr, flush=True)
        return None
    from .providers.implementations.opencode import OpenCodeSessionProvider

    return [OpenCodeSessionProvider(_path, adapter)]


def _run_list_windowed(
    providers: list[SessionProvider], period: Period, all_ids: set[str]
) -> list[tuple[SessionProvider, list]] | None:
    """Listing fenêtré + univers d'ids — None si listing FATAL."""
    try:
        # Fusion multi-sources : listing fenêtré + univers d'ids par provider.
        windowed: list[tuple[SessionProvider, list]] = []
        for provider in providers:
            windowed.append((provider, provider.list_sessions(_to_ms(period.start))))
            all_ids.update(m.session_id for m in provider.list_sessions(0))
        return windowed
    except Exception as exc:  # noqa: BLE001
        print(
            f"telemetry-aggregator: FATAL: session listing failed: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return None


def _run_dedup_windowed(
    windowed: list[tuple[SessionProvider, list]], warnings: list[WarningEntry]
) -> None:
    """Dédup déterministe des ids canoniques — la PREMIÈRE source gagne."""
    dup_by_source: dict[int, int] = {}
    seen_canonical: set[str] = set()
    for idx, (provider, metas) in enumerate(windowed):
        kept: list = []
        for meta in metas:
            if meta.session_id in seen_canonical:
                dup_by_source[idx] = dup_by_source.get(idx, 0) + 1
                continue
            seen_canonical.add(meta.session_id)
            kept.append(meta)
        windowed[idx] = (provider, kept)
    if dup_by_source:
        message = " ; ".join(
            f"{n} session(s) en doublon ignorée(s) depuis {windowed[i][0].harness} source #{i + 1}"
            for i, n in sorted(dup_by_source.items())
        )
        _warn_user(message, stacklevel=2)
        warnings.append(WarningEntry(session_id=None, message=message))


def _run_read_usages(
    windowed: list[tuple[SessionProvider, list]],
    period: Period,
    run_time: object,
    cfg: TelemetryConfig,
    warnings: list[WarningEntry],
    audit: list[dict],
) -> tuple[list[SessionUsage], bool]:
    """Boucle de lecture par meta — parent canonique + build_usage."""
    usages: list[SessionUsage] = []
    read_failed = False
    for provider, metas in windowed:
        for meta in metas:
            if meta.time_updated is not None and meta.time_updated < period.start:
                continue
            if meta.parent_id:
                # parent_id brut → canonique sur une COPIE (dataclasses.replace) :
                # les metas du provider restent intactes (#10). La fusion
                # racine/enfants (aggregate + selection audit) reste valable
                # multi-source ; tolérant à un parent déjà préfixé (jamais de
                # double préfixe).
                raw_parent = str(meta.parent_id)
                prefix = f"{provider.harness}:"
                canonical_parent = (
                    raw_parent
                    if raw_parent.startswith(prefix)
                    else canonical_session_id(provider.harness, raw_parent)
                )
                meta = dataclasses.replace(meta, parent_id=canonical_parent)
            usage, failed = build_usage(
                meta,
                provider,
                period=period,
                run_time=run_time,
                cfg=cfg,
                warnings=warnings,
                audit=audit,
            )
            if failed:
                read_failed = True
            if usage is not None:
                usages.append(usage)
    return usages, read_failed


def _run_merge_resumed(
    usages: list[SessionUsage], audit: list[dict], warnings: list[WarningEntry]
) -> list[SessionUsage]:
    """R3 (v6.1) : dédup resume-fork — marque audit + warnings."""
    usages, resumed_merges = dedup_resumed_usages(usages)
    for merge in resumed_merges:
        dropped_id = merge["dropped_session_id"]
        for rec in audit:
            if rec["session_id"] == dropped_id and rec["status"] == "included":
                rec["status"] = "resumed-duplicate"
                rec["merged_into"] = merge["kept_session_id"]
        warnings.append(
            WarningEntry(
                session_id=dropped_id,
                message=f"session reprise fusionnée dans {merge['kept_session_id']} (dédup resume)",
            )
        )
    return usages


def _run_selection_warnings(summary: object) -> None:
    """Warnings fenêtre vide / 0 comptée — fusion capée dans summary."""
    extra_warnings: list[WarningEntry] = []
    if summary.selection["window_touched"] == 0:
        extra_warnings.append(
            WarningEntry(
                session_id=None,
                message="aucune session mise à jour dans la fenêtre — vérifier la période/ancre",
            )
        )
    elif summary.selection["counted"] == 0:
        extra_warnings.append(
            WarningEntry(
                session_id=None,
                message=(
                    f"0 session comptée sur {summary.selection['window_touched']} touchée(s) — "
                    "télémétrie absente ou fenêtre vide (audit §1)"
                ),
            )
        )
    if extra_warnings:
        summary.warnings = _cap_warnings([*summary.warnings, *extra_warnings])


def _run_cost_estimates(summary: object, usages: list[SessionUsage], cfg: TelemetryConfig) -> None:
    """Coûts estimés first-class — None si rien à estimer."""
    estimates = estimate_costs(usages, rates=_harness_cost_rates(cfg))
    if estimates:
        summary.cost_estimates = estimates


def _run_write_summary(cfg: TelemetryConfig, run_time: object, summary: object) -> Path:
    """Activation run UUID + écriture summary + provenance — retourne out_path."""
    date = run_time.strftime("%Y-%m-%d")
    active = activate_run(cfg.output_dir, date, run_time)
    same_date_runs = sorted(
        d.name for d in (cfg.output_dir / "runs").glob(f"{date}-*") if d.is_dir()
    )
    if len(same_date_runs) > 1:
        print(
            f"telemetry-aggregator: re-run fenêtre {date} — nouveaux artefacts isolés "
            f"dans runs/{active.run_id} (le run précédent est conservé)",
            flush=True,
        )
    # X4 : chemin ABSOLU dans la réponse — l'agent enchaîne run → étapes
    # suivantes en recopiant ce chemin (un relatif dépend du cwd : incident 07:41).
    out_path = _abs(active.run_dir / f"weekly-summary-{date}.json")
    print("telemetry-aggregator: écriture du summary…", flush=True)
    write_summary(out_path, summary)
    # Additive metadata keeps the established summary schema intact while making
    # report/manifest provenance available to downstream steps.
    summary_data = load_json(out_path) or {}
    provenance = _run_provenance(cfg.project_root, run_time)
    # Normalize legacy/provider payloads to canonical ``start_time`` while
    # retaining the historical alias for downstream readers.
    if "start_time" not in provenance and "run_started_at" in provenance:
        provenance["start_time"] = provenance["run_started_at"]
    provenance.setdefault("run_started_at", provenance.get("start_time"))
    summary_data["run_provenance"] = provenance
    write_json_atomic(out_path, summary_data)
    return out_path


def run(
    cfg: TelemetryConfig,
    *,
    anchor: str | None = None,
    top_sessions_limit: int | None = None,
    include_subagents: bool | None = None,
    fail_on_missing_telemetry: bool = False,
    lookback_days: int | None = None,
) -> int:
    """Run the aggregation pipeline. Returns the process exit code (0/1/2)."""
    run_time = _parse_anchor(anchor)
    if top_sessions_limit is not None:
        cfg.top_sessions_limit = max(0, top_sessions_limit)
    if include_subagents is not None:
        cfg.include_subagents = include_subagents
    if fail_on_missing_telemetry:
        cfg.fail_on_missing_telemetry = True
    apply_lookback_override(cfg, lookback_days)

    period = Period(start=run_time - timedelta(hours=cfg.window_hours()), end=run_time)
    warnings: list[WarningEntry] = []
    audit: list[dict] = []

    _run_placeholder_guard(cfg, warnings)

    print(
        f"telemetry-aggregator: fenêtre {cfg.lookback_days} j [{period.start.isoformat()} → {period.end.isoformat()}]",
        flush=True,
    )

    providers = build_providers(cfg)
    if not providers:
        fallback = _run_fallback_providers(cfg)
        if fallback is None:
            return EXIT_TOTAL_FAILURE
        providers = fallback

    read_failed = False
    usages: list[SessionUsage] = []
    all_ids: set[str] = set()
    try:
        windowed = _run_list_windowed(providers, period, all_ids)
        if windowed is None:
            return EXIT_TOTAL_FAILURE
        # Dédup déterministe des ids canoniques entre sources du même harnais :
        # deux entrées session_sources pointant la même base listent les mêmes
        # ids → sinon double comptage sessions/coûts/tokens. La PREMIÈRE source
        # (ordre cfg.session_sources) gagne ; `all_ids` étant un set, l'univers
        # ne peut de toute façon pas doubler.
        _run_dedup_windowed(windowed, warnings)
        touched = sum(len(metas) for _, metas in windowed)
        print(
            f"telemetry-aggregator: {touched} session(s) touchée(s) — lecture télémétrie…",
            flush=True,
        )
        usages, read_failed = _run_read_usages(windowed, period, run_time, cfg, warnings, audit)
    finally:
        for provider in providers:
            provider.close()

    if read_failed and cfg.fail_on_missing_telemetry:
        return EXIT_PARTIAL

    # R3 (v6.1) : dédup des sessions reprises — une resume-fork copie le transcript
    # sous un nouvel id avec timestamps d'origine → sinon double comptage sessions/
    # coûts/tokens et 2 candidats d'audit pour une seule session logique.
    usages = _run_merge_resumed(usages, audit, warnings)

    print("telemetry-aggregator: agrégation…", flush=True)
    # A5 — UN scan alimente les deux anciens scanners : plus de dédup par nom
    # d'un côté et pas de dédup de l'autre. `records` est la source unique.
    skill_records = scan_skill_records(
        resolve_skill_surface(
            cfg.project_root,
            resolve_draft_targets(cfg.project_root, cfg.draft_targets),
            cfg.global_roots,
        )
    )
    catalog_names, catalog_count, catalog_entries = scan_skill_catalog(skill_records)
    summary = aggregate(
        usages,
        period=period,
        generated_at=run_time,
        top_sessions_limit=cfg.top_sessions_limit,
        include_subagents=cfg.include_subagents,
        skill_catalog=catalog_names,
        skill_catalog_entries=catalog_entries,
        skill_catalog_snapshot=[record.as_protection_entry() for record in skill_records],
        warnings=warnings,
        known_parent_ids=all_ids,
        session_outlier_z=cfg.session_outlier_z,
        session_outlier_min_cost_usd=cfg.session_outlier_min_cost_usd,
        outlier_min_sessions=cfg.outlier_min_sessions,
        user_prompt_repeat_min=cfg.user_prompt_repeat_min,
        user_prompt_repeat_similarity=cfg.user_prompt_repeat_similarity,
        user_prompt_repeat_min_chars=cfg.user_prompt_repeat_min_chars,
        skill_similarity_min=cfg.skill_similarity_min,
    )
    summary.selection = _build_selection(
        audit, cfg.audit_max_sessions, all_ids, period=getattr(summary, "period", None)
    )
    _run_selection_warnings(summary)

    # coûts estimés (champ first-class) : sessions sans aucun coût enregistré →
    # estimation tokens × taux du harnais ; champ laissé à None (clé absente
    # à la sérialisation) si rien à estimer.
    _run_cost_estimates(summary, usages, cfg)

    # v6.0.k (F1): every run gets its own UUID-scoped directory — artifacts of
    # different runs (same anchor or not) can never collide or overwrite each
    # other; the legacy --force flag (v6.0.p D2) had no meaning anymore.
    out_path = _run_write_summary(cfg, run_time, summary)

    print(
        f"telemetry-aggregator: sessions={summary.totals.session_count} "
        f"tokens={summary.totals.total_tokens} cost=${summary.totals.total_cost_usd:.6f} "
        f"warnings={len(summary.warnings)} run_dir={_abs(out_path.parent)} file={out_path}",
        flush=True,
    )
    return EXIT_PARTIAL if any(w.partial for w in summary.warnings) else EXIT_OK


# ------------------------------------- doctor : helpers partagés + harness / self-cost
#
# Le diagnostic lui-même vit dans `doctor` ; seuls les noms utilisés par
# `run()`/`harness()` restent ici (voir l'en-tête de `doctor` pour le sens des
# imports).


def _version_tuple(version: str) -> tuple[int, ...] | None:
    nums = [int(x) for x in re.findall(r"\d+", version)]
    if not nums:
        return None
    return tuple(nums[:3]) + (0,) * (3 - len(nums[:3]))


def _placeholder_fields(cfg: TelemetryConfig) -> list[str]:
    """Champs de config restés en placeholder « /path/to/... » jamais substitué."""
    return [
        field
        for field in ("project_root", "output_dir")
        # Normalisation séparateurs : str(Path) sous Windows rend des « \ » —
        # sans elle, « path/to » ne matche plus et la garde devient muette (CI).
        if "path/to" in str(getattr(cfg, field, "") or "").replace("\\", "/")
    ]


def _placeholder_message(fields: list[str]) -> str:
    """Message d'installation partagé doctor/run pour les champs placeholder."""
    return (
        "config jamais adaptée à cette installation — substituer "
        + "/".join(fields)
        + " dans weekly-telemetry-config.json (placeholders /path/to/ détectés)"
    )


# ---- cellule 2.2 : kit root + baseline findings -------------------------------

# v2 (faiblesse #14) : ajout de `rules_version` — empreinte du jeu de règles
# (fichiers .harness-eval/rules + version harness-eval). Une baseline sans
# empreinte (v1) est rafraîchie une fois puis réutilisée.
HARNESS_BASELINE_SCHEMA_VERSION = 2


def _engine_kit_root(cfg: TelemetryConfig) -> Path | None:
    """Racine du kit portant le contenu engine (`.opencode/{skills,commands}`).

    ``cfg.kit_root`` d'abord (distribution), puis dérivation depuis le paquet
    moteur (`<kit>/.opencode/plugins/weekly-advisor-engine/...`). None si
    aucune racine valide — l'injection engine est alors silencieusement absente.
    """
    candidates: list[Path] = []
    if cfg.kit_root is not None:
        candidates.append(Path(cfg.kit_root))
    # main.py = <kit>/.opencode/plugins/weekly-advisor-engine/weekly_telemetry_aggregator/
    candidates.append(Path(__file__).resolve().parents[4])
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve()
        except OSError:  # pragma: no cover - chemin illisible
            continue
        if (resolved / ".opencode" / "skills").is_dir():
            return resolved
    return None


def _baseline_finding_keys(digest: Mapping) -> list[tuple[str, str]]:
    """Paires (rule, path) localisées d'un digest, dédupliquées et triées."""
    return sorted(
        {
            (str(record["rule"]), str(record["path"]))
            for record in iter_digest_findings(digest)
            if record.get("path") is not None
        }
    )


def _valid_baseline(payload: object) -> dict | None:
    """Forme attendue d'une baseline stockée : dict + findings list + captured_on str."""
    if (
        isinstance(payload, dict)
        and isinstance(payload.get("findings"), list)
        and isinstance(payload.get("captured_on"), str)
    ):
        return payload
    return None


def _capture_or_reuse_baseline(
    output_dir: Path,
    date: str,
    enriched_digest: Mapping,
    *,
    project_root: Path,
    tool_version: str | None,
) -> dict:
    """Baseline findings : capturée au premier run, réutilisée à empreinte égale.

    Ancrage : racine ``output_dir`` (stable entre runs, comme run_state.json).
    Premier run → le snapshot courant devient la baseline (`status=created`).
    Runs suivants → réutilisation (`reused`) uniquement si l'empreinte du jeu
    de règles (``rules_version``, faiblesse #14) est identique ; sinon la
    baseline est recapturée (`refreshed`) avec note stdout + WarningEntry —
    un upgrade harness-eval/portability.yaml ne produit plus des faux
    ``new_findings`` éternels. Une baseline illisible est remplacée
    (auto-réparation conservée) mais désormais tracée (warning + note), plus
    aucun reset silencieux.
    """
    current_keys = _baseline_finding_keys(enriched_digest)
    rules_version = harness_rules_fingerprint(project_root, tool_version)
    path = output_dir / HARNESS_BASELINE_FILE
    stored: dict | None = None
    corrupted = False
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            loaded = None
        stored = _valid_baseline(loaded)
        if stored is None:
            # Faiblesse #14 : JSON cassé OU forme invalide = corruption — la
            # recapture reste automatique mais n'est plus silencieuse.
            corrupted = True

    # Restauration best-effort depuis les copies legacy (bug de migration
    # v6.0.l→v6.0.p ayant déplacé la baseline racine vers runs/<id>/legacy/).
    if stored is None and output_dir.is_dir():
        for legacy_path in sorted(
            output_dir.glob(f"{RUNS_DIR}/*/legacy/{HARNESS_BASELINE_FILE}"),
            reverse=True,
        ):
            try:
                candidate = json.loads(legacy_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            stored = _valid_baseline(candidate)
            if stored is None:
                continue
            corrupted = False
            print(
                f"harness: baseline restaurée depuis {legacy_path.relative_to(output_dir)}",
                flush=True,
            )
            with contextlib.suppress(OSError):
                # logique intacte ce run ; réécriture retentée au suivant
                write_json_atomic(path, stored)
            break
    baseline_keys = sorted(
        {
            (str(item.get("rule")), str(item.get("path")))
            for item in (stored or {}).get("findings", [])
            if isinstance(item, Mapping) and item.get("path") is not None
        }
    )
    new_keys = sorted(set(current_keys) - set(baseline_keys)) if stored else []
    if (
        stored is not None
        and stored.get("rules_version") == rules_version
        and rules_version != "unknown"
    ):
        return {
            "schema_version": HARNESS_BASELINE_SCHEMA_VERSION,
            "status": "reused",
            "captured_on": str(stored["captured_on"]),
            "finding_count": len(baseline_keys),
            "new_findings": [{"rule": rule, "path": path_} for rule, path_ in new_keys],
            "rules_version": rules_version,
        }

    # Faiblesse #14 : toute capture non-réutilisée est tracée (jamais muette).
    entries: list[WarningEntry] = []
    status = "created"
    if stored is not None:
        # Empreinte absente (baseline legacy) ou différente (règles/outil
        # changés) → recapture unique : le prochain run retrouvera l'empreinte.
        status = "refreshed"
        entries.append(
            WarningEntry(session_id=None, message="baseline harness rafraîchie : règles changées")
        )
    elif corrupted:
        entries.append(WarningEntry(session_id=None, message="baseline illisible — recapture"))
    for entry in entries:
        print(f"harness: WARNING: {entry.message}", flush=True)

    snapshot = [
        {"rule": rule, "path": path_}
        for rule, path_ in (current_keys if stored is None else baseline_keys)
    ]
    payload = {
        "schema_version": HARNESS_BASELINE_SCHEMA_VERSION,
        "captured_on": date,
        "finding_count": len(snapshot),
        "findings": snapshot,
        "rules_version": rules_version,
    }
    try:
        write_json_atomic(path, payload)
    except OSError as exc:
        print(f"harness: WARNING: baseline non écrite ({exc})", flush=True)
        return {
            "schema_version": HARNESS_BASELINE_SCHEMA_VERSION,
            "status": status,
            "captured_on": date,
            "finding_count": len(snapshot),
            "new_findings": [],
            "error": str(exc),
            "rules_version": rules_version,
            "warnings": [dataclasses.asdict(entry) for entry in entries],
        }
    verb = "capturée" if status == "created" else "rafraîchie"
    print(f"harness: baseline findings {verb} ({len(snapshot)} finding(s))", flush=True)
    return {
        "schema_version": HARNESS_BASELINE_SCHEMA_VERSION,
        "status": status,
        "captured_on": date,
        "finding_count": len(snapshot),
        "new_findings": [],
        "rules_version": rules_version,
        "warnings": [dataclasses.asdict(entry) for entry in entries],
    }


def harness(cfg: TelemetryConfig, *, anchor: str | None = None, timeout: int = 900) -> int:
    """Run ``harness-eval`` against the configured temporary projection.

    Exit 0/1 of the tool = success (violations live in the digest, spec Partie 0 §4);
    any other code or a missing binary = real step failure.  The worktree is
    never modified by projection creation; when the tool emits a digest, its
    temporary paths are remapped before the scope and normalized counts are
    merged into the output.
    """
    run_time = _parse_anchor(anchor)
    date = run_time.strftime("%Y-%m-%d")
    binary = shutil.which("harness-eval")
    if binary is None:
        print(
            "harness: FATAL: harness-eval absent du PATH — lancer doctor",
            file=sys.stderr,
            flush=True,
        )
        return EXIT_TOTAL_FAILURE
    if cfg.project_root is None:
        print("harness: FATAL: project_root manquant dans la config", file=sys.stderr, flush=True)
        return EXIT_TOTAL_FAILURE
    tool_version: str | None = None
    try:
        vp = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=15)
        version = (vp.stdout or vp.stderr).strip()
        if version:
            tool_version = version
        installed = _version_tuple(version)
        required = _version_tuple(cfg.harness_eval_version) if cfg.harness_eval_version else None
        if installed is not None and required is not None and installed < required:
            print(
                f"harness: WARNING: harness-eval {version} < minimum requis "
                f"{cfg.harness_eval_version} — résultat non garanti",
                flush=True,
            )
    except (OSError, subprocess.TimeoutExpired):
        print("harness: WARNING: --version indisponible", flush=True)
    # X4 : chemin ABSOLU dès la résolution — l'écriture du digest, l'argument
    # --output et la réponse finale portent le même chemin non ambigu.
    out_path = _abs(
        resolve_active_run_dir(cfg.output_dir, date) / f"weekly-harness-digest-{date}.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Cellule 2.2 : le harnais résolu étend la projection au-delà de
        # `.opencode/` (DRAFT_HARNESS_TARGETS) et reçoit le contenu engine.
        resolved_draft = resolve_draft_targets(cfg.project_root, cfg.draft_targets)
        extra_roots = harness_extra_roots(resolved_draft)
        inject_dirs = tuple(
            sorted(
                {
                    target
                    for harness in resolved_draft.harnesses
                    for target in DRAFT_HARNESS_TARGETS.get(harness, ())
                }
            )
        )
        scope = resolve_harness_scope(
            cfg.project_root, cfg.harness_include, extra_roots=extra_roots
        )
        for warning in scope.warnings:
            print(f"harness: WARNING: {warning}", flush=True)

        with tempfile.TemporaryDirectory(prefix="weekly-harness-") as temporary:
            projection_root = Path(temporary)
            copy_scope_to_projection(cfg.project_root, scope, projection_root)
            orphans = inject_engine_content(
                cfg.project_root,
                inject_dirs,
                projection_root,
                kit_root=_engine_kit_root(cfg),
            )
            if orphans:
                print(
                    f"harness: contenu engine projeté ({len(orphans)} fichier(s) orphelin(s))",
                    flush=True,
                )
            proc = subprocess.run(
                [
                    binary,
                    "harness-lint",
                    str(projection_root),
                    "--format",
                    "json",
                    "--output",
                    str(out_path),
                ],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if proc.returncode not in (0, 1):
                print(
                    f"harness: FATAL: rc={proc.returncode} inattendu — échec réel d'étape",
                    file=sys.stderr,
                    flush=True,
                )
                return EXIT_TOTAL_FAILURE

            # Keep compatibility with mocked/older wrappers that do not leave a
            # digest behind.  A real harness-eval run normally takes this path.
            if out_path.is_file():
                try:
                    digest = json.loads(out_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                    print(
                        f"harness: FATAL: digest JSON illisible: {exc}",
                        file=sys.stderr,
                        flush=True,
                    )
                    return EXIT_TOTAL_FAILURE
                if not isinstance(digest, dict):
                    print(
                        "harness: FATAL: digest JSON invalide (objet attendu)",
                        file=sys.stderr,
                        flush=True,
                    )
                    return EXIT_TOTAL_FAILURE
                enriched = enrich_harness_digest(digest, scope, projection_root)
                # Cellule 2.2 : signal de projection (harnais, orphelins) +
                # baseline findings capturée au premier run, réutilisée ensuite.
                enriched["draft_targets"] = {
                    "mode": resolved_draft.mode,
                    "harnesses": list(resolved_draft.harnesses),
                    "warning": resolved_draft.warning,
                    "extra_projection_roots": list(extra_roots),
                    "injected_engine_files": len(orphans),
                    "orphan_files": sorted(orphans),
                    "surface_decision": resolve_remediation_surface(
                        resolved_draft.harnesses, resolved_draft.mode
                    ).to_dict(),
                }
                # Cellule 2.2 : baseline ancrée à la racine output_dir (comme
                # run_state.json) — un run dir UUID neuf ne doit pas casser
                # la réutilisation aux runs suivants. Faiblesse #14 : réutilisation
                # conditionnée à l'empreinte du jeu de règles (rules_version).
                baseline = _capture_or_reuse_baseline(
                    cfg.output_dir,
                    date,
                    enriched,
                    project_root=cfg.project_root,
                    tool_version=tool_version,
                )
                enriched["harness_baseline"] = baseline
                write_json_atomic(out_path, enriched)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"harness: FATAL: exécution impossible: {exc}", file=sys.stderr, flush=True)
        return EXIT_TOTAL_FAILURE
    print(f"harness: digest {out_path} (rc={proc.returncode})", flush=True)
    return EXIT_OK
