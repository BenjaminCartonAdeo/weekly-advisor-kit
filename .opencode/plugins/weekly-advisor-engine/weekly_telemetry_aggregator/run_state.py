"""Per-run artifact directories (v6.0.k, F1).

Each run writes every artifact under ``<output_dir>/runs/<run_id>/`` with
``run_id = <anchor-date>-<uuid-hex-8>`` — two runs can never overwrite each
other, even when they share the same anchor date.  A small state file
(``<output_dir>/run_state.json``) records the active run; ``runs/current`` is a
best-effort symlink to it so agents and docs keep a stable path.  Commands that
resolve a date with no run state fall back to ``output_dir`` itself (legacy
mode: tests, manual CLI debugging) — production always activates a run first.
"""

from __future__ import annotations

import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .util import load_json
from .writer import write_json_atomic

RUNS_DIR = "runs"
RUN_STATE_FILE = "run_state.json"
CURRENT_SYMLINK = "current"
STATE_SCHEMA_VERSION = 1
_WARNED_REAL_CURRENT = False  # warning "current réel" : une seule fois par process

_LAYOUT_README = """# reports/ — layout du pipeline `weekly-advisor`

- `runs/<date>-<uuid8>/` — artefacts complets de chaque run (source de vérité)
- `runs/current/` — alias symlink vers le run actif (signal cron/CI)
- `<project_root>/reports/html/` — **rapport HTML pour l'utilisateur**
  (chemin par défaut, config `html_report_dir` pour le changer, `""` pour désactiver)
- `run_state.json` / `previous_run.json` / `anchor-last.txt` — état du pipeline (ne pas éditer)
- `legacy/` (dans le run dir) — artefacts racine pré-v6.0.k migrés automatiquement
"""


@dataclass(frozen=True, slots=True)
class ActiveRun:
    """Identity and directory of the currently activated run."""

    run_id: str
    run_date: str
    run_dir: Path


def activate_run(output_dir: Path, date: str, run_time: datetime) -> ActiveRun:
    """Create a fresh per-run directory, persist state, refresh the ``current`` link.

    The directory is brand new (UUID suffix) so a second run on the same anchor
    date never collides — previous artifacts stay untouched (v6.0.k, F1).
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    runs_dir = output_dir / RUNS_DIR
    runs_dir.mkdir(parents=True, exist_ok=True)
    run_id = f"{date}-{uuid.uuid4().hex[:8]}"
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=False)  # uuid ⇒ never pre-exists
    #: alias matérialisé IMMÉDIatement à la naissance du run dir (v6.2) —
    #: les artefacts intermédiaires résolvent `runs/current` dès la première écriture.
    _update_current_link(output_dir, run_dir)
    state = {
        "schema_version": STATE_SCHEMA_VERSION,
        "run_id": run_id,
        "run_date": date,
        "run_dir": f"{RUNS_DIR}/{run_id}",
        "created_at": run_time.isoformat(),
        #: heure réelle de création du run dir (v6.0.l) — created_at reste l'ancre.
        "activated_at": datetime.now(UTC).isoformat(),
    }
    _write_ok(output_dir / RUN_STATE_FILE, state)
    _migrate_legacy_root(output_dir, run_dir)
    _ensure_layout_readme(output_dir)
    return ActiveRun(run_id=run_id, run_date=date, run_dir=run_dir)


def resolve_active_run_dir(output_dir: Path, date: str) -> Path:
    """Active run directory for ``date``, or ``output_dir`` (legacy mode).

    Legacy mode keeps pre-v6.0.k behavior for tests and direct CLI debugging:
    artifacts live at the ``output_dir`` root.  Production runs always activate
    a run first, so real pipelines are collision-free by design.
    """
    meta = active_run_meta(output_dir, date)
    if meta is None:
        return Path(output_dir)
    name = meta.get("run_dir")
    if not isinstance(name, str) or not name:
        return Path(output_dir)
    run_dir = Path(output_dir) / name
    return run_dir if run_dir.is_dir() else Path(output_dir)


def active_run_meta(output_dir: Path, date: str) -> dict | None:
    """State dict of the active run for ``date`` (± tolérance minuit); None otherwise.

    v6.2 : un run franchissant minuit UTC rafraîchit l'ancre en B alors que le
    state actif porte la date A — le voisin -1 jour reste actif (pas de fallback
    legacy). Au-delà d'un jour d'écart, comportement historique.
    """
    state = load_json(Path(output_dir) / RUN_STATE_FILE)
    if not isinstance(state, dict) or not _state_date_matches(state.get("run_date"), date):
        return None
    run_dir = state.get("run_dir")
    if not isinstance(run_dir, str) or not run_dir:
        return None
    if not (Path(output_dir) / run_dir).is_dir():
        return None
    return state


def _state_date_matches(state_run_date: object, date: str) -> bool:
    """Exact match, ou run daté du voisin -1 jour (tolérance minuit UTC)."""
    if state_run_date == date:
        return True
    try:
        yesterday = datetime.strptime(date, "%Y-%m-%d").date() - timedelta(days=1)
    except ValueError:  # date malformée → pas de tolérance
        return False
    return state_run_date == yesterday.isoformat()


def _write_ok(path: Path, data: dict) -> None:
    """Atomic state write (recette writer.py : temp même répertoire + os.replace).

    Un crash mi-écriture ne doit jamais laisser un ``run_state.json`` tronqué —
    l'état corrompu basculerait tous les artefacts suivants en fallback legacy.
    """
    write_json_atomic(path, data)


def _update_current_link(output_dir: Path, run_dir: Path) -> None:
    """Refresh ``<output_dir>/runs/current`` → run_dir; best-effort (no error raised).

    Mécanique atomique (recette writer.py) : symlink temporaire + ``os.replace`` —
    jamais de fenêtre sans alias, et un alias préexistant (cassé ou pointant
    ailleurs) est remplacé en une opération. Si ``current`` existe comme entrée
    réelle (répertoire/fichier), warning + intact. Filesystems sans support
    symlink (Windows non privilégié) : OSError silencieux, la résolution passe
    toujours par ``run_state.json``.
    """
    link = output_dir / RUNS_DIR / CURRENT_SYMLINK
    tmp_link = output_dir / RUNS_DIR / f".{CURRENT_SYMLINK}.tmp-{uuid.uuid4().hex[:8]}"
    global _WARNED_REAL_CURRENT
    try:
        if link.exists() and not link.is_symlink():
            if not _WARNED_REAL_CURRENT:
                print(
                    f"run_state: {RUNS_DIR}/{CURRENT_SYMLINK} existe déjà comme "
                    "répertoire réel — alias non remplacé",
                    flush=True,
                )
                _WARNED_REAL_CURRENT = True
            return
        with suppress(OSError):
            tmp_link.unlink()  # résidu d'un crash précédent
        tmp_link.symlink_to(run_dir, target_is_directory=True)
        tmp_link.replace(link)
    except OSError:
        return  # symlink unsupported → comportement historique (state file only)


def _migrate_legacy_root(output_dir: Path, run_dir: Path) -> None:
    """One-shot v6.0.l (E4) : artefacts racine pré-v6.0.k → ``runs/<id>/legacy/``.

    Seuls les **fichiers réels** `weekly-*` sont déplacés — jamais les symlinks
    (liens stables du rapport) ni les fichiers d'état vivants (`run_state.json`,
    `previous_run.json`, `anchor-last.txt`). Best-effort : un échec de déplacement
    n'empêche pas le run.
    """
    legacy = run_dir / "legacy"
    moved = 0
    for p in sorted(output_dir.glob("weekly-*")):
        if p.is_symlink() or not p.is_file():
            continue
        try:
            legacy.mkdir(parents=True, exist_ok=True)
            p.rename(legacy / p.name)
            moved += 1
        except OSError:
            continue
    if moved:
        print(
            f"run_state: migration legacy — {moved} artefact(s) racine -> "
            f"{legacy.relative_to(output_dir)}",
            flush=True,
        )


def _ensure_layout_readme(output_dir: Path) -> None:
    """Write `<output_dir>/README.md` once (v6.0.l) — l'utilisateur sait où chercher."""
    readme = output_dir / "README.md"
    if readme.exists():
        return
    with suppress(OSError):
        readme.write_text(_LAYOUT_README, encoding="utf-8")
