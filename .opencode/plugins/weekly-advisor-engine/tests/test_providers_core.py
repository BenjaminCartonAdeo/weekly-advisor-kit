"""Cœur multi-harnais : parsing `session_sources`, registre, délégation opencode."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from tests.helpers import seed_v1_file, tzutc
from weekly_telemetry_aggregator.config import TelemetryConfig, load_config
from weekly_telemetry_aggregator.models import split_canonical_session_id
from weekly_telemetry_aggregator.providers import (
    HARNESS_OPENCODE,
    build_providers,
    discover_provider_factories,
)
from weekly_telemetry_aggregator.providers.implementations.opencode import (
    OpenCodeSessionProvider,
)

RUN_TIME = tzutc(2026, 8, 20, 12, 0, 0)
WINDOW_END_MS = int(RUN_TIME.timestamp() * 1000) + 60_000


# --- config.session_sources -------------------------------------------------


def test_default_sources_is_opencode_only():
    cfg = TelemetryConfig()
    assert cfg.session_sources == [{"type": "opencode"}]


def test_absent_key_keeps_retrocompatible_default(tmp_path: Path):
    conf = tmp_path / "weekly-telemetry-config.json"
    conf.write_text(json.dumps({"lookback_days": 3}), encoding="utf-8")
    assert load_config(conf).session_sources == [{"type": "opencode"}]


def test_explicit_sources_parsed_with_extra_keys(tmp_path: Path):
    conf = tmp_path / "weekly-telemetry-config.json"
    conf.write_text(
        json.dumps(
            {
                "session_sources": [
                    {"type": "opencode"},
                    {"type": "claude-code", "path": "/tmp/cc.jsonl", "enabled": False},
                ]
            }
        ),
        encoding="utf-8",
    )
    sources = load_config(conf).session_sources
    assert sources[0] == {"type": "opencode", "enabled": True}
    assert sources[1] == {"type": "claude-code", "enabled": False, "path": "/tmp/cc.jsonl"}


def test_invalid_entries_dropped_tolerantly(tmp_path: Path):
    conf = tmp_path / "weekly-telemetry-config.json"
    conf.write_text(
        json.dumps(
            {
                "session_sources": [
                    42,
                    "opencode",
                    {},
                    {"type": "   "},
                    {"type": "  opencode  "},
                ]
            }
        ),
        encoding="utf-8",
    )
    assert load_config(conf).session_sources == [{"type": "opencode", "enabled": True}]


# --- registry ---------------------------------------------------------------


def test_registry_discovers_opencode_factory():
    factories = discover_provider_factories()
    assert HARNESS_OPENCODE in factories
    assert callable(factories[HARNESS_OPENCODE])


def test_unknown_type_warns_and_skips():
    cfg = TelemetryConfig()
    # 1.3 : "claude-code" est désormais un provider réel — inconnu générique utilisé.
    cfg.session_sources = [{"type": "harnais-inexistant"}, {"type": "opencode"}]
    with pytest.warns(UserWarning, match="type inconnu 'harnais-inexistant'"):
        providers = build_providers(cfg)
    assert [p.harness for p in providers] == [HARNESS_OPENCODE]


def test_disabled_source_skipped_silently():
    cfg = TelemetryConfig()
    cfg.session_sources = [
        {"type": "opencode", "enabled": False},
        {"type": "opencode"},
    ]
    import warnings as _w

    with _w.catch_warnings():
        _w.simplefilter("error")  # source désactivée = silence strict
        providers = build_providers(cfg)
    assert len(providers) == 1
    assert providers[0].harness == HARNESS_OPENCODE


def test_unavailable_source_warns_and_skips(tmp_path: Path):
    cfg = TelemetryConfig()
    cfg.opencode_db_path = str(tmp_path / "missing.db")
    with pytest.warns(UserWarning, match="indisponible"):
        assert build_providers(cfg) == []


def test_factory_failure_warns_and_skips():
    def _boom(_source_cfg, _cfg):  # noqa: ARG001 — signature factory registry
        raise RuntimeError("boom")

    cfg = TelemetryConfig()
    with pytest.warns(UserWarning, match="échec d'initialisation"):
        assert (
            build_providers(
                cfg,
                factories={HARNESS_OPENCODE: _boom},  # type: ignore[dict-item]
            )
            == []
        )


# --- délégation opencode (fixture SQLite temporaire) ------------------------


@pytest.fixture()
def seeded_db(tmp_path: Path) -> Path:
    db = tmp_path / "opencode.db"
    conn = seed_v1_file(
        db,
        [
            {
                "id": "ses_1",
                "title": "Lance la revue hebdomadaire",
                "start": tzutc(2026, 8, 20, 11, 0, 0),
                "updated": RUN_TIME,
                "agg_cost": 0.3,
                "steps": [{"ts": tzutc(2026, 8, 20, 11, 5, 0), "cost": 0.25, "input": 100}],
                "texts": [{"ts": tzutc(2026, 8, 20, 11, 1, 0), "text": "Lance la revue"}],
            }
        ],
    )
    conn.close()
    return db


def _provider_for(db: Path) -> OpenCodeSessionProvider:
    cfg = TelemetryConfig()
    cfg.opencode_db_path = str(db)
    built = build_providers(cfg)
    assert len(built) == 1
    provider = built[0]
    assert isinstance(provider, OpenCodeSessionProvider)
    return provider


def test_opencode_delegation_roundtrip(seeded_db: Path):
    provider = _provider_for(seeded_db)
    try:
        assert provider.harness == HARNESS_OPENCODE
        provider.check_schema()

        sessions = provider.list_sessions(0)
        assert [s.session_id for s in sessions] == ["opencode:ses_1"]
        meta = sessions[0]
        assert meta.harness == HARNESS_OPENCODE
        assert meta.title == "Lance la revue hebdomadaire"
        assert meta.model_key == "anthropic/claude-x"
        # helpers de namespacing cohérents avec les ids exposés
        assert split_canonical_session_id(meta.session_id) == ("opencode", "ses_1")

        found = provider.find_session_by_title("Lance la revue hebdomadaire")
        assert found is not None and found.session_id == "opencode:ses_1"
        assert provider.find_session_by_title("inexistant") is None

        steps = provider.session_steps("opencode:ses_1", 0, WINDOW_END_MS)
        assert len(steps) == 1
        assert steps[0].session_id == "opencode:ses_1"  # ids canoniques aussi sur les steps
        assert steps[0].harness == HARNESS_OPENCODE
        assert steps[0].cost == pytest.approx(0.25)

        assert provider.has_telemetry_rows("opencode:ses_1") is True
        assert provider.session_user_turns("opencode:ses_1", 0, WINDOW_END_MS) == ["Lance la revue"]
        assert provider.session_tools("opencode:ses_1", 0, WINDOW_END_MS) == ({}, {}, {})
        assert isinstance(provider.session_context_chars("opencode:ses_1", 0, WINDOW_END_MS), dict)

        parts = provider.session_parts("opencode:ses_1")
        assert any(p.kind == "step-finish" for p in parts)

        agg = provider.session_aggregates("opencode:ses_1")
        assert agg is not None and agg.get("cost") == pytest.approx(0.3)
    finally:
        provider.close()


def test_opencode_tolerates_raw_ids(seeded_db: Path):
    provider = _provider_for(seeded_db)
    try:
        steps = provider.session_steps("ses_1", 0, WINDOW_END_MS)
        assert [s.session_id for s in steps] == ["opencode:ses_1"]
        assert provider.has_telemetry_rows("ses_1") is True
    finally:
        provider.close()


def test_close_releases_source(seeded_db: Path):
    provider = _provider_for(seeded_db)
    provider.close()
    with pytest.raises(sqlite3.ProgrammingError):  # connexion fermée → interdit
        provider._adapter.conn.execute("SELECT 1")  # noqa: SLF001 — assertion de cycle de vie
