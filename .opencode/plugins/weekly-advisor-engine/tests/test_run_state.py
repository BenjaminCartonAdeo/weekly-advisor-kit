"""v6.0.k (F1) : per-run UUID directories — collision-free by design.

v6.2 : écriture d'état atomique (recette writer.py) + tolérance minuit UTC
(run actif daté du voisin -1 jour reste résolu, pas de fallback legacy).
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime

import pytest

from weekly_telemetry_aggregator.run_state import (
    RUN_STATE_FILE,
    _write_ok,
    activate_run,
    active_run_meta,
    resolve_active_run_dir,
)

RUN_DATE = "2026-08-16"
RUN_TIME = datetime(2026, 8, 18, 10, 0, tzinfo=UTC)


def test_activate_creates_uuid_run_dir_and_state(tmp_path):
    active = activate_run(tmp_path, RUN_DATE, RUN_TIME)
    assert active.run_id.startswith(RUN_DATE + "-")
    assert active.run_dir.parent.name == "runs"
    state = json.loads((tmp_path / RUN_STATE_FILE).read_text(encoding="utf-8"))
    assert state["run_id"] == active.run_id
    assert state["run_date"] == RUN_DATE
    assert state["run_dir"] == f"runs/{active.run_id}"


def test_current_symlink_points_to_active_run(tmp_path):
    active = activate_run(tmp_path, RUN_DATE, RUN_TIME)
    link = tmp_path / "runs" / "current"
    assert link.is_symlink()
    assert link.resolve() == active.run_dir.resolve()


def test_alias_repoints_when_new_run_created(tmp_path):
    """Nouveau run dir → l'alias bascule atomiquement ; aucun résidu .tmp."""
    first = activate_run(tmp_path, RUN_DATE, RUN_TIME)
    second = activate_run(tmp_path, RUN_DATE, RUN_TIME.replace(hour=11))
    link = tmp_path / "runs" / "current"
    assert link.resolve() == second.run_dir.resolve()
    assert link.resolve() != first.run_dir.resolve()
    assert [p.name for p in (tmp_path / "runs").iterdir() if ".tmp" in p.name] == []


def test_alias_repairs_broken_preexisting_symlink(tmp_path):
    """Alias cassé préexistant (cible supprimée) → réparé au prochain activate."""
    runs = tmp_path / "runs"
    runs.mkdir()
    ghost = runs / "ghost-run"
    ghost.mkdir()
    link = runs / "current"
    link.symlink_to(ghost)
    ghost.rmdir()  # symlink désormais cassé
    assert link.is_symlink() and not link.exists()

    active = activate_run(tmp_path, RUN_DATE, RUN_TIME)
    assert link.resolve() == active.run_dir.resolve()


def test_alias_replaces_stale_symlink_pointing_elsewhere(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    stale = tmp_path / "stale-target"
    stale.mkdir()
    (runs / "current").symlink_to(stale)

    active = activate_run(tmp_path, RUN_DATE, RUN_TIME)
    assert (runs / "current").resolve() == active.run_dir.resolve()


def test_real_directory_at_current_left_untouched_with_warning(tmp_path, capsys):
    """`runs/current` répertoire réel : warning + intact, jamais écrasé."""
    runs = tmp_path / "runs"
    runs.mkdir(parents=True)
    link = runs / "current"
    link.mkdir()
    sentinel = link / "keep.txt"
    sentinel.write_text("data", encoding="utf-8")

    active = activate_run(tmp_path, RUN_DATE, RUN_TIME)
    assert link.is_dir() and not link.is_symlink()
    assert sentinel.read_text(encoding="utf-8") == "data"
    out = capsys.readouterr().out
    assert "current" in out
    assert active.run_id.startswith(RUN_DATE + "-")  # le run continue malgré tout


def test_resolve_uses_state_and_falls_back_to_legacy_root(tmp_path):
    active = activate_run(tmp_path, RUN_DATE, RUN_TIME)
    assert resolve_active_run_dir(tmp_path, RUN_DATE) == active.run_dir
    # date hors tolérance minuit (> 1 jour) : pas de run actif → legacy root
    assert resolve_active_run_dir(tmp_path, "2026-08-18") == tmp_path
    # état corrompu → legacy root sans erreur
    (tmp_path / RUN_STATE_FILE).write_text("{broken", encoding="utf-8")
    assert resolve_active_run_dir(tmp_path, RUN_DATE) == tmp_path


def test_same_date_rerun_never_collides(tmp_path):
    first = activate_run(tmp_path, RUN_DATE, RUN_TIME)
    second = activate_run(tmp_path, RUN_DATE, RUN_TIME.replace(hour=11))
    assert second.run_id != first.run_id
    assert first.run_dir.is_dir()  # premier run intact
    assert second.run_dir.is_dir()
    assert resolve_active_run_dir(tmp_path, RUN_DATE) == second.run_dir
    assert active_run_meta(tmp_path, RUN_DATE)["run_id"] == second.run_id
    assert active_run_meta(tmp_path, "2026-08-15") is None


def test_write_ok_atomic_no_truncated_state_on_crash(tmp_path, monkeypatch):
    """Crash mi-écriture : l'état final reste l'ancien JSON valide, aucun .tmp.

    Recette writer.py : temporaire même répertoire + os.replace — un crash
    pendant l'écriture ne doit jamais laisser un run_state.json tronqué.
    """
    target = tmp_path / RUN_STATE_FILE
    target.write_text('{"old": true}', encoding="utf-8")

    real_fdopen = os.fdopen

    class Boom(Exception):
        pass

    def fake_fdopen(fd, *args, **kwargs):
        fh = real_fdopen(fd, *args, **kwargs)
        orig_write = fh.write

        def partial_then_crash(data):
            orig_write(data[: max(len(data) // 2, 1)])  # moitié écrite puis crash
            raise Boom

        fh.write = partial_then_crash
        return fh

    monkeypatch.setattr(os, "fdopen", fake_fdopen)
    with pytest.raises(Boom):
        _write_ok(target, {"new": "state"})
    monkeypatch.undo()
    # fichier final intact (ancien contenu), aucun résidu .tmp
    assert json.loads(target.read_text(encoding="utf-8")) == {"old": True}
    assert [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []


def test_resolve_accepts_active_run_from_previous_day(tmp_path):
    """Run franchissant minuit UTC : state daté J reste actif pour date J+1."""
    active = activate_run(tmp_path, RUN_DATE, RUN_TIME)
    # le plugin rafraîchit l'ancre après minuit → date courante B ≠ run_date A
    assert resolve_active_run_dir(tmp_path, "2026-08-17") == active.run_dir
    assert active_run_meta(tmp_path, "2026-08-17")["run_id"] == active.run_id


def test_resolve_legacy_beyond_midnight_tolerance(tmp_path):
    activate_run(tmp_path, RUN_DATE, RUN_TIME)
    # au-delà du voisin -1 jour : comportement historique (fallback legacy)
    assert resolve_active_run_dir(tmp_path, "2026-08-18") == tmp_path
    # date antérieure au run : jamais active
    assert resolve_active_run_dir(tmp_path, "2026-08-15") == tmp_path
    assert active_run_meta(tmp_path, "2026-08-18") is None
