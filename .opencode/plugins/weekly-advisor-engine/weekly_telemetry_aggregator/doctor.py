"""Diagnostic d'installation — `doctor()` et ses checks.

Bloc extrait de `main` (le fichier le plus dégradé du dépôt). Sens des imports
volontairement unidirectionnel : ce module importe de `main`, jamais l'inverse.

- `EXIT_OK` / `EXIT_PARTIAL` / `EXIT_TOTAL_FAILURE` restent dans `main` (12
  modules les importent) — ils sont importés d'ici.
- `_version_tuple`, `_placeholder_fields`, `_placeholder_message` et
  `build_providers` sont partagés avec `run()`/`harness()` : ils restent dans
  `main` et sont résolus via l'attribut de module `_main.<nom>`, jamais liés à
  l'import. Un `from .main import build_providers` figerait la référence et
  casserait les `monkeypatch.setattr(main_mod, "build_providers", ...)` des
  tests ; l'attribut de module garde ce point d'injection observable.
- `main` ré-exporte cette surface via `__getattr__` (PEP 562) : `from .main
  import doctor` reste valide sans qu'aucune arête `main` -> `doctor` n'existe
  au niveau module.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from . import main as _main
from .config import TelemetryConfig
from .draft_targets import describe_draft_target, resolve_draft_targets
from .harness_scope import resolve_remediation_surface
from .main import EXIT_OK, EXIT_PARTIAL, EXIT_TOTAL_FAILURE
from .sqlite_reader import MIGRATION_MIN_V1
from .util import _abs


def _check_migrations(adapter) -> int | None:
    for table in ("migration", "data_migration"):
        try:
            row = adapter.conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()
            if row is not None:
                return int(row["n"])
        except Exception:  # noqa: BLE001
            continue
    return None


def _copilot_doctor_details(provider) -> list[str]:
    """Détails doctor Copilot — best-effort, jamais de raise."""
    try:
        harness = getattr(provider, "harness", "")
        sessions = getattr(provider, "_sessions", None)
        n = len(sessions) if isinstance(sessions, dict) else None
        if harness == "copilot-cli":
            home = getattr(provider, "home", None)
            version = getattr(provider, "schema_version", None)
            tables = getattr(provider, "_tables", None)
            out = [f"home={home}" if home is not None else "home=?"]
            out.append(f"schema_version={version if version is not None else '?'}")
            if n is not None:
                out.append(f"sessions={n}")
            if isinstance(tables, (set, frozenset)):
                out.append(f"fts={'oui' if 'search_index' in tables else 'non'}")
            return out
    except Exception:  # noqa: BLE001 — diagnostic best-effort uniquement
        return []
    return []


def _unknown_session_source_types(cfg: TelemetryConfig) -> tuple[list[str], list[str]]:
    """Types session_sources inconnus du registre — ([], []) si registre illisible."""
    try:
        from .providers.registry import discover_provider_factories as _discover_factories

        supported = set(_discover_factories().keys())
        unknown: list[str] = []
        for src in cfg.session_sources:
            if not isinstance(src, dict):
                unknown.append(repr(src))
                continue
            if src.get("enabled", True) is False:
                continue
            t = src.get("type")
            if not isinstance(t, str) or t not in supported:
                unknown.append(repr(t))
        if unknown:
            return sorted(set(unknown)), sorted(supported)
        return [], sorted(supported)
    except Exception:  # pragma: no cover - diagnostic best-effort
        return [], []


def _doctor_project_root(
    cfg: TelemetryConfig,
    cwd: Path,
    *,
    config_loaded: bool,
    problems: list[str],
    warnings: list[str],
) -> None:
    """Contrôles project_root : présence .opencode/ + config localisable."""
    if cfg.project_root is None:
        problems.append("project_root manquant dans la config")
        return
    if not (cfg.project_root / ".opencode").is_dir():
        problems.append(
            f"project_root {cfg.project_root} ne contient pas .opencode/ — "
            "adapter la config (clone : project_root = chemin absolu de votre repo)"
        )
    if not config_loaded:
        try:
            # layout kit : cwd = moteur (config lue au cwd) ≠ project_root (repo audité) —
            # le vrai défaut est une config introuvable (cron lancé d'un dossier quelconque)
            _cfg_nearby = (cwd / "weekly-telemetry-config.json").is_file()
            _cfg_at_root = (cfg.project_root / "weekly-telemetry-config.json").is_file()
            if not _cfg_nearby and not _cfg_at_root:
                warnings.append(
                    f"config introuvable au cwd ({cwd}) ni au project_root — vérifier --dir du cron"
                )
        except OSError:
            warnings.append("project_root non résoluble — chemins à vérifier")


def _doctor_output_dir_guard(cfg: TelemetryConfig, warnings: list[str]) -> None:
    """Garde-fou : output_dir sous .opencode/plugins/ = run lancé depuis le moteur."""
    resolved_out = Path(_abs(cfg.output_dir)).resolve()
    parts = resolved_out.parts
    if (
        ".opencode" in parts
        and "plugins" in parts
        and parts.index(".opencode") + 1 == parts.index("plugins")
    ):
        warnings.append(
            f"output_dir résout sous l'arbre plugins ({resolved_out}) — run "
            "probablement lancé depuis le dossier du moteur ; déplacer reports/ "
            "hors du plugin et relancer les étapes depuis la racine du projet"
        )


def _doctor_opencode_version(
    cfg: TelemetryConfig, opencode_bin: str, problems: list[str], warnings: list[str]
) -> None:
    """Binaire opencode : présence PATH + épinglage version minimale."""
    try:
        # Windows : shutil.which résout "opencode" → "opencode.cmd"/".exe" (un
        # argv nu n'est pas exécutable tel quel via subprocess sans shell).
        resolved = shutil.which(opencode_bin) or opencode_bin
        proc = subprocess.run(
            [resolved, "--version"], capture_output=True, encoding="utf-8", timeout=15
        )
        version = (proc.stdout or proc.stderr).strip()
    except (OSError, subprocess.TimeoutExpired):
        # Non fatal (v6.0.f) : le run est lancé PAR opencode (binaire absolu du cron) et
        # le pipeline lit opencode.db directement — un PATH étroit (cron) rend le binaire
        # invisible au sous-processus sans casser la revue. Le version-pin devient une
        # note, comme harness-eval.
        warnings.append(f"opencode introuvable ou non exécutable ({opencode_bin})")
        version = "?"

    if version and version != "?":
        cur = _main._version_tuple(version)
        minv = _main._version_tuple(cfg.opencode_version_min or "0")
        if cur is not None and minv is not None and cur < minv:
            problems.append(
                f"opencode {version} < {cfg.opencode_version_min} — épinglage du schéma non garanti"
            )


def _doctor_session_providers(
    cfg: TelemetryConfig, problems: list[str], warnings: list[str]
) -> bool:
    """Itération générique sur les providers actifs — aucun harnais en dur.

    Retourne True si dégradation partielle (≥1 source OK et ≥1 KO, #13).
    """
    providers = _main.build_providers(cfg)
    usable = 0
    for provider in providers:
        name = getattr(provider, "harness", type(provider).__name__)
        try:
            try:
                provider.check_schema()
            except Exception as exc:  # noqa: BLE001 — diagnostic fail-soft par source
                warnings.append(f"[{name}] schéma illisible ({exc})")
                print(f"doctor: [{name}] KO ({exc})")
                continue
            usable += 1
            details: list[str] = []
            src = getattr(provider, "db_path", None)
            if src is not None:
                details.append(str(src))
            adapter = getattr(provider, "_adapter", None)
            if adapter is not None:
                # Check migrations conservé pour les providers SQLite qui exposent
                # leur adapter ; les autres (non-SQLite) sautent proprement.
                migrations = _check_migrations(adapter)
                n = migrations if migrations is not None else 0
                if migrations is None:
                    warnings.append(
                        f"[{name}] compteur de migrations introuvable — schéma non standard"
                    )
                elif n < MIGRATION_MIN_V1:
                    warnings.append(
                        f"[{name}] compteur de migrations faible ({n}) — vérifier la version du harnais"
                    )
                details.append(f"migrations={n}")
            # Détails Copilot (home/schema_version/sessions/events/fts,
            # user_dirs/workspaces/orphans/index) — best-effort, fail-soft.
            details.extend(_copilot_doctor_details(provider))
            suffix = f" ({', '.join(details)})" if details else ""
            print(f"doctor: [{name}] OK{suffix}")
        finally:
            provider.close()
    if not usable:
        problems.append(
            "aucune source de sessions disponible — vérifier session_sources / bases locales"
        )
        print("doctor: sources de sessions: aucune disponible")
    # #13 : ≥1 source utilisable ET ≥1 source KO → dégradation partielle réelle,
    # signalée par EXIT_PARTIAL au lieu d'un 0 muet.
    return bool(providers) and 0 < usable < len(providers)


def _doctor_draft_targets(cfg: TelemetryConfig, warnings: list[str]) -> None:
    """Cibles de drafting (cellule 2.1) : override > marqueurs > défaut ; [] = legacy."""
    resolved = resolve_draft_targets(cfg.project_root, cfg.draft_targets)
    print(f"doctor: cibles de drafting: {describe_draft_target(resolved)}")
    if resolved.warning:
        warnings.append(resolved.warning)
    # Matrice de décision 5.5 (cellule 2.2) : surface de remédiation déduite
    # du harnais résolu — affichée seule ; la règle portability.yaml = cellule 3.1.
    surface = resolve_remediation_surface(resolved.harnesses, resolved.mode)
    print(f"doctor: surface de remédiation 5.5: {surface.decision} — {surface.reason}")


def _doctor_output_probe(cfg: TelemetryConfig, problems: list[str]) -> None:
    """Probe d'écriture output_dir (seule écriture du doctor)."""
    try:
        cfg.output_dir.mkdir(parents=True, exist_ok=True)
        probe = cfg.output_dir / ".doctor-write-probe"
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
        print(f"doctor: output_dir accessible en écriture: {cfg.output_dir}")
    except OSError as exc:
        problems.append(f"output_dir non accessible en écriture: {exc}")


def _doctor_tool_presence(warnings: list[str]) -> None:
    """Présence harness-eval/git au PATH (étapes dégradées si absents)."""
    for tool in ("harness-eval", "git"):
        if shutil.which(tool) is None:
            warnings.append(
                f"{tool} absent du PATH (rien n'est lancé, mais l'étape correspondante sera dégradée)"
            )


def _doctor_harness_eval_version(cfg: TelemetryConfig, warnings: list[str]) -> None:
    """Version minimum harness-eval (v6.1.a — plancher acceptant les versions supérieures)."""
    if shutil.which("harness-eval") is not None and cfg.harness_eval_version:
        try:
            proc = subprocess.run(
                ["harness-eval", "--version"], capture_output=True, encoding="utf-8", timeout=15
            )
            version = (proc.stdout or proc.stderr).strip()
            installed = _main._version_tuple(version)
            required = _main._version_tuple(cfg.harness_eval_version)
            if installed is not None and required is not None:
                if installed < required:
                    warnings.append(
                        f"harness-eval {version} < minimum requis {cfg.harness_eval_version}"
                        " — mettre à jour : uv tool install --upgrade harness-eval"
                    )
            elif version and cfg.harness_eval_version not in version:
                warnings.append(
                    f"harness-eval --version illisible ({version!r})"
                    f" — attendu ≥ {cfg.harness_eval_version}"
                )
        except (OSError, subprocess.TimeoutExpired):
            warnings.append("harness-eval --version indisponible")


def _doctor_watch_repos(cfg: TelemetryConfig, warnings: list[str]) -> None:
    """watch_repos : gh présent au PATH et authentifié."""
    if not cfg.watch_repos:
        return
    if shutil.which("gh") is None:
        warnings.append(
            "watch_repos configuré mais gh absent du PATH — repos privés/renommés non suivis"
        )
        return
    try:
        proc = subprocess.run(
            ["gh", "auth", "status", "--active"],
            capture_output=True,
            encoding="utf-8",
            timeout=10,
        )
        if proc.returncode != 0:
            warnings.append(
                "watch_repos configuré mais gh non authentifié — repos privés indisponibles (gh auth login)"
            )
    except (OSError, subprocess.TimeoutExpired):
        warnings.append("gh auth status indisponible — vérifier l'authentification gh")


def doctor(
    cfg: TelemetryConfig,
    *,
    cwd: Path | None = None,
    opencode_bin: str = "opencode",
    config_loaded: bool = False,
) -> int:
    """Diagnose the installation — reads/writes nothing but a probe file in output_dir."""
    cwd = Path(cwd) if cwd is not None else Path.cwd()
    problems: list[str] = []
    warnings: list[str] = []

    _doctor_project_root(
        cfg, cwd, config_loaded=config_loaded, problems=problems, warnings=warnings
    )

    # Sentinelle d'installation : placeholders « /path/to/... » jamais substitués
    # dans weekly-telemetry-config.json — le fatal générique ci-dessus n'est pas
    # actionnable, on nomme le vrai défaut et les champs exacts à corriger.
    _fields = _main._placeholder_fields(cfg)
    if _fields:
        problems.append(_main._placeholder_message(_fields))

    # ses_f55 : session_sources avec type inconnu (ex. copilot-app) passait en
    # warning fail-soft côté registry → coût 0.0 malgré tokens → alertes fausses.
    # Doctor doit être strict : type inconnu = PROBLEM rc2, pas un warning muet.
    _unknown, _supported = _unknown_session_source_types(cfg)
    if _unknown:
        problems.append(
            f"session_sources contient des types inconnus {_unknown}"
            f" — types supportés: {_supported} — corriger weekly-telemetry-config.json"
        )

    _doctor_output_dir_guard(cfg, warnings)

    _doctor_opencode_version(cfg, opencode_bin, problems, warnings)

    # Sources de sessions : itération générique sur les providers actifs du
    # registre — aucun harnais connu en dur du doctor (un nouveau provider
    # s'affiche ici sans modification de ce bloc). close() est garanti pour
    # chaque provider (try/finally), même si check_schema() lève (#9).
    partial_sources = _doctor_session_providers(cfg, problems, warnings)

    _doctor_draft_targets(cfg, warnings)

    _doctor_output_probe(cfg, problems)

    _doctor_tool_presence(warnings)

    _doctor_harness_eval_version(cfg, warnings)

    _doctor_watch_repos(cfg, warnings)

    for msg in warnings:
        print(f"doctor: WARNING: {msg}")
    for msg in problems:
        print(f"doctor: PROBLEM: {msg}")

    if problems:
        return EXIT_TOTAL_FAILURE
    if partial_sources:
        # #13 : sources mixtes OK/KO — setup dégradé, pas un échec total (2)
        # ni un setup sain (0).
        return EXIT_PARTIAL
    # Warnings (harness-eval absent, cwd hint, migrations bas) sont des notes
    # d'opération — un setup sain retourne 0 avec les notes imprimées.
    return EXIT_OK
