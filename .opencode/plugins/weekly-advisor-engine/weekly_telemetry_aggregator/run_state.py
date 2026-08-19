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

import json
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .util import load_json

RUNS_DIR = "runs"
RUN_STATE_FILE = "run_state.json"
CURRENT_SYMLINK = "current"
STATE_SCHEMA_VERSION = 1

_LAYOUT_README = """# reports/ — layout du pipeline `weekly-advisor`

- `runs/<date>-<uuid8>/` — artefacts complets de chaque run (source de vérité)
- `runs/current/` — alias symlink vers le run actif (signal cron/CI)
- `~/weekly-reports/weekly-report-latest.md` — **copie du rapport pour l'utilisateur**
  (chemin par défaut, config `report_dir` pour le changer, `""` pour désactiver)
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
    _update_current_link(output_dir, run_dir)
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
    """State dict of the active run when it matches ``date``; None otherwise."""
    state = load_json(Path(output_dir) / RUN_STATE_FILE)
    if not isinstance(state, dict) or state.get("run_date") != date:
        return None
    run_dir = state.get("run_dir")
    if not isinstance(run_dir, str) or not run_dir:
        return None
    if not (Path(output_dir) / run_dir).is_dir():
        return None
    return state


def _write_ok(path: Path, data: dict) -> None:
    """Atomic-ish state write — state corruption must never kill a run."""
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _update_current_link(output_dir: Path, run_dir: Path) -> None:
    """Refresh ``<output_dir>/current`` → run_dir; best-effort (no error raised)."""
    link = output_dir / CURRENT_SYMLINK
    try:
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(run_dir, target_is_directory=True)
    except OSError:
        pass  # filesystems without symlink support still resolve via run_state.json


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
