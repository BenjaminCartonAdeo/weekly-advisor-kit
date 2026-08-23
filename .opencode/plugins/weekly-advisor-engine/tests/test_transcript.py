"""show-session rendering + compaction (Partie 0 §3) + routage multi-harnais."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from helpers import seed_v1_file, tzutc

from weekly_telemetry_aggregator.providers.implementations.opencode import (
    OpenCodeSessionProvider,
)
from weekly_telemetry_aggregator.sqlite_reader import detect_db
from weekly_telemetry_aggregator.transcript import render_session

RUN_TIME = tzutc(2026, 8, 12)


def _provider_for(db: Path) -> OpenCodeSessionProvider:
    _path, adapter = detect_db(str(db))
    return OpenCodeSessionProvider(_path, adapter)


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
    provider = _provider_for(db)
    try:
        text = render_session(provider, "ses_a")
    finally:
        provider.close()
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
    provider = _provider_for(db)
    try:
        text = render_session(provider, "ses_r")
    finally:
        provider.close()
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
    provider = _provider_for(db)
    try:
        alone = render_session(provider, "root")
        with_child = render_session(provider, "root", include_children=True)
    finally:
        provider.close()
    assert "child turn" not in alone
    assert "child turn" in with_child


# ============================================================ routage multi-harnais


def _fake_source(harness: str, ts):
    from helpers import FakeSessionProvider, fake_meta, fake_part

    return FakeSessionProvider(
        harness,
        [fake_meta(harness, "s1", title=f"{harness} session", updated=ts)],
        parts_by_session={
            "s1": [
                fake_part("user", ts, text=f"tour {harness}"),
                fake_part("text", ts, text=f"réponse {harness}"),
            ]
        },
    )


def test_render_session_routes_provider_by_harness_prefix():
    from helpers import tzutc

    alpha = _fake_source("alpha", tzutc(2026, 8, 11, 22))
    beta = _fake_source("beta", tzutc(2026, 8, 11, 22))
    try:
        out_alpha = render_session([alpha, beta], "alpha:s1")
        out_beta = render_session([alpha, beta], "beta:s1")
    finally:
        alpha.close()
        beta.close()
    assert "tour alpha" in out_alpha and "réponse alpha" in out_alpha
    assert "beta" not in out_alpha
    assert "tour beta" in out_beta and "réponse beta" in out_beta


def test_render_session_raw_id_falls_back_to_first_source():
    from helpers import tzutc

    alpha = _fake_source("alpha", tzutc(2026, 8, 11, 22))
    beta = _fake_source("beta", tzutc(2026, 8, 11, 22))
    try:
        out = render_session([alpha, beta], "s1")  # id brut → première source
    finally:
        alpha.close()
        beta.close()
    assert "tour alpha" in out


def test_render_session_include_children_canonical_parents(tmp_path: Path):
    """parent_id brut des metas re-préfixé → enfants trouvés via l'id canonique."""
    from helpers import FakeSessionProvider, fake_meta, fake_part, tzutc

    ts = tzutc(2026, 8, 11, 22)
    provider = FakeSessionProvider(
        "gamma",
        [
            fake_meta("gamma", "root", title="Root", updated=ts),
            fake_meta("gamma", "child", title="Child", parent="root", updated=ts),
        ],
        parts_by_session={
            "root": [fake_part("user", ts, text="root turn")],
            "child": [fake_part("user", ts, text="child turn")],
        },
    )
    try:
        alone = render_session(provider, "gamma:root")
        with_child = render_session(provider, "gamma:root", include_children=True)
    finally:
        provider.close()
    assert "child turn" not in alone
    assert "child turn" in with_child
