"""v6.0.k (F1) : per-run UUID directories — collision-free by design."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from weekly_telemetry_aggregator.run_state import (
    RUN_STATE_FILE,
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
    link = tmp_path / "current"
    assert link.is_symlink()
    assert link.resolve() == active.run_dir.resolve()


def test_resolve_uses_state_and_falls_back_to_legacy_root(tmp_path):
    active = activate_run(tmp_path, RUN_DATE, RUN_TIME)
    assert resolve_active_run_dir(tmp_path, RUN_DATE) == active.run_dir
    # autre date : pas de run actif → legacy root (tests / debug CLI)
    assert resolve_active_run_dir(tmp_path, "2026-08-17") == tmp_path
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
