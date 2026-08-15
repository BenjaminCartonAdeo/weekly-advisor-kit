"""show-session rendering + compaction (Partie 0 §3)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from helpers import seed_v1_file, tzutc

from weekly_telemetry_aggregator.sqlite_reader import detect_db
from weekly_telemetry_aggregator.transcript import render_session

RUN_TIME = tzutc(2026, 8, 12)


def test_render_includes_turns_and_tools(tmp_path: Path):
    db = tmp_path / "opencode.db"
    ts = RUN_TIME - timedelta(hours=2)
    seed_v1_file(
        db,
        [
            {
                "id": "ses_a",
                "title": "A",
                "start": ts,
                "updated": ts,
                "steps": [{"ts": ts, "cost": 0.1}],
                "texts": [{"ts": ts, "text": "fais une revue"}],
                "tools": [
                    {
                        "ts": ts + timedelta(seconds=2),
                        "name": "bash",
                        "input": {"cmd": "ls"},
                        "output": "ok",
                    }
                ],
            }
        ],
    )
    _path, adapter = detect_db(str(db))
    try:
        text = render_session(adapter, "ses_a")
    finally:
        adapter.conn.close()
    assert "user: fais une revue" in text
    assert "tool: bash" in text


def test_compaction_of_repeated_similar_tool_calls(tmp_path: Path):
    db = tmp_path / "opencode.db"
    ts = RUN_TIME - timedelta(hours=2)
    tools = [
        {
            "ts": ts + timedelta(seconds=i),
            "name": "bash",
            "input": {"cmd": "make test"},
            "output": f"error: fail {i}",
        }
        for i in range(5)
    ]
    seed_v1_file(
        db, [{"id": "ses_r", "title": "R", "start": ts, "updated": ts, "steps": [], "tools": tools}]
    )
    _path, adapter = detect_db(str(db))
    try:
        text = render_session(adapter, "ses_r")
    finally:
        adapter.conn.close()
    assert "appels similaires" in text
    assert "masqués (compaction)" in text
    # first + last present, not all 5
    assert text.count("make test") == 2


def test_include_children_expands_transcript(tmp_path: Path):
    db = tmp_path / "opencode.db"
    ts = RUN_TIME - timedelta(hours=2)
    seed_v1_file(
        db,
        [
            {
                "id": "root",
                "title": "Root",
                "start": ts,
                "updated": ts,
                "texts": [{"ts": ts, "text": "root turn"}],
            },
            {
                "id": "child",
                "parent": "root",
                "title": "Child",
                "start": ts,
                "updated": ts,
                "texts": [{"ts": ts + timedelta(seconds=5), "text": "child turn"}],
            },
        ],
    )
    _path, adapter = detect_db(str(db))
    try:
        alone = render_session(adapter, "root")
        with_child = render_session(adapter, "root", include_children=True)
    finally:
        adapter.conn.close()
    assert "child turn" not in alone
    assert "child turn" in with_child
