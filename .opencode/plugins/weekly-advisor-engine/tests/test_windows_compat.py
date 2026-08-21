"""Windows compatibility: %LOCALAPPDATA% detection + read-only URI open regression."""

from __future__ import annotations

from pathlib import Path

from helpers import seed_v1_file, tzutc

from weekly_telemetry_aggregator.sqlite_reader import detect_db, open_database

RUN_TIME = tzutc(2026, 8, 12)


def _seed(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    seed_v1_file(
        path,
        [{"id": "ses_001", "title": "Session 1", "start": RUN_TIME, "updated": RUN_TIME}],
    )
    return path


def test_detect_db_localappdata_candidate(tmp_path: Path, monkeypatch) -> None:
    """Windows : une base sous %LOCALAPPDATA%/opencode est détectée en mode auto."""
    db = _seed(tmp_path / "localappdata" / "opencode" / "opencode.db")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))  # inexistant
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    path, adapter = detect_db("auto")
    assert path == db
    assert adapter.name == "opencode"
    adapter.conn.close()


def test_open_database_readonly_regression(tmp_path: Path) -> None:
    """Le mode ro passe par as_uri() : la connexion reste lisible (SELECT ok)."""
    db = _seed(tmp_path / "opencode.db")
    conn = open_database(db)
    count = conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0]
    assert count >= 1
    conn.close()


def test_detect_db_same_dir_no_duplicate(tmp_path: Path, monkeypatch) -> None:
    """XDG_DATA_HOME == LOCALAPPDATA : le doublon de base est dédupliqué sans bloquer."""
    shared = tmp_path / "shared"
    db = _seed(shared / "opencode" / "opencode.db")
    monkeypatch.setenv("XDG_DATA_HOME", str(shared))
    monkeypatch.setenv("LOCALAPPDATA", str(shared))
    path, adapter = detect_db("auto")
    assert path == db
    assert adapter.name == "opencode"
    adapter.conn.close()
