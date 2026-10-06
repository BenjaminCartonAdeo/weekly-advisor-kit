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


def _hermetic_cfg(tmp_path: Path) -> TelemetryConfig:
    """Cfg sans dépendance ambiante : base OpenCode seedée dans tmp_path."""
    db = tmp_path / "opencode.db"
    seed_v1_file(db, []).close()
    cfg = TelemetryConfig()
    cfg.opencode_db_path = str(db)
    return cfg


def test_unknown_type_warns_and_skips(tmp_path: Path):
    cfg = _hermetic_cfg(tmp_path)
    # 1.3 : "claude-code" est désormais un provider réel — inconnu générique utilisé.
    cfg.session_sources = [{"type": "harnais-inexistant"}, {"type": "opencode"}]
    with pytest.warns(UserWarning, match="type inconnu 'harnais-inexistant'"):
        providers = build_providers(cfg)
    assert [p.harness for p in providers] == [HARNESS_OPENCODE]


def test_disabled_source_skipped_silently(tmp_path: Path):
    cfg = _hermetic_cfg(tmp_path)
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


# --- B2 / B3 : canal résultat et tours user (schéma part lié à message) -------


def _linkable_db(path: Path, *, texts: list[tuple[str, str]], tools: list[tuple[str, str]]):
    """opencode.db avec le lien `part.message_id` → `message.id` (schéma réel).

    `seed_v1_file` omet `id`/`message_id` : sans ce jointure, le rôle du message
    parent d'une part est indécidable et B3 ne peut pas s'exécuter. Ce helper crée
    le lien explicite pour exercer le chemin nominal.
    """
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE session_v2 (
            id TEXT PRIMARY KEY, parent_id TEXT, title TEXT, model TEXT, agent TEXT,
            directory TEXT, cost REAL, tokens_input REAL, tokens_output REAL,
            tokens_reasoning REAL, tokens_cache_read REAL, tokens_cache_write REAL,
            time_created INTEGER, time_updated INTEGER
        );
        CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, data TEXT, time_created INTEGER);
        CREATE TABLE part (
            id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
            data TEXT, time_created INTEGER
        );
        CREATE TABLE migration (id INTEGER PRIMARY KEY);
        """
    )
    conn.execute("INSERT INTO migration (id) VALUES (1)")
    conn.execute(
        "INSERT INTO session_v2 (id, parent_id, title, model, cost, tokens_input, "
        "tokens_output, tokens_reasoning, tokens_cache_read, tokens_cache_write, "
        "time_created, time_updated) VALUES ('ses_1', NULL, 't', NULL, 0, 0, 0, 0, 0, 0, 0, ?)",
        (int(RUN_TIME.timestamp() * 1000),),
    )
    base = int(RUN_TIME.timestamp() * 1000) - 60_000
    for i, (role, text) in enumerate(texts):
        mid = f"msg_{i}"
        conn.execute(
            "INSERT INTO message (id, session_id, data, time_created) VALUES (?,?,?,?)",
            (mid, "ses_1", json.dumps({"role": role}), base + i),
        )
        conn.execute(
            "INSERT INTO part (id, message_id, session_id, data, time_created) VALUES (?,?,?,?,?)",
            (
                f"prt_{i}",
                mid,
                "ses_1",
                json.dumps({"type": "text", "text": text}),
                base + i,
            ),
        )
    for j, (tool, output) in enumerate(tools):
        conn.execute(
            "INSERT INTO part (id, message_id, session_id, data, time_created) VALUES (?,?,?,?,?)",
            (
                f"prt_t{j}",
                None,
                "ses_1",
                json.dumps(
                    {
                        "type": "tool",
                        "state": {"name": tool, "input": {"n": j}, "output": output},
                    }
                ),
                base + 100 + j,
            ),
        )
    conn.commit()
    conn.close()
    return path


def test_user_turns_exclude_assistant_narration_and_json_payloads(tmp_path: Path):
    """B3 : seuls les tours `role == "user"` et non-JSON survivent.

    Mesuré sur le run 2026-10-03 : `data.type == "text"` couvre 852 parts assistant
    et 425 parts user — lire le type seul comptait la narration comme prompt humain.
    """
    db = _linkable_db(
        tmp_path / "opencode.db",
        texts=[
            ("user", "Lance la revue hebdomadaire"),
            ("assistant", "J'utilise `graphify` pour situer les composants"),
            ("assistant", '{"verdict":"block","reason":"contexte json invalide"}'),
            ("user", '{"verdict":"safe","reason":"patch update"}'),
            ("user", "   "),
            ("user", "explique moi ce que ça changerait"),
        ],
        tools=[],
    )
    provider = _provider_for(db)
    try:
        assert provider.session_user_turns("ses_1", 0, WINDOW_END_MS) == [
            "Lance la revue hebdomadaire",
            "explique moi ce que ça changerait",
        ]
    finally:
        provider.close()


def test_user_turns_keep_json_quoted_but_not_object_payload(tmp_path: Path):
    """B3 : un tour JSON-sérialisé STRING est une intention (commande client), pas un blob.

    Le client sérialise `/swarmx test: …` en `"/swarmx test: …"` : le retirer sur la
    seule présence de guillemets ferait perdre la seule commande slash du corpus.
    """
    db = _linkable_db(
        tmp_path / "opencode.db",
        texts=[
            ("user", "\"/swarmx test: réponds JUSTE 'ok'\""),
            ("user", '["a", "b"]'),
            ("user", "mets à jour le schema et ajoute {" + '"x": 1' + "} dedans"),
        ],
        tools=[],
    )
    provider = _provider_for(db)
    try:
        turns = provider.session_user_turns("ses_1", 0, WINDOW_END_MS)
    finally:
        provider.close()
    assert turns == [
        "\"/swarmx test: réponds JUSTE 'ok'\"",
        'mets à jour le schema et ajoute {"x": 1} dedans',
    ]


def test_user_turns_fallback_without_part_message_link(seeded_db: Path):
    """B3 fail-open : sans `part.message_id`, le rôle est indécidable → tours bruts.

    On ne perd pas tous les tours en silence ; le canal reste simplement non filtré,
    et `command_usage_state` (B5.2) distingue alors « non mesuré » de « mesuré vide ».
    """
    provider = _provider_for(seeded_db)
    try:
        assert provider.session_user_turns("ses_1", 0, WINDOW_END_MS) == ["Lance la revue"]
        assert provider._adapter._part_has_message_id is False  # noqa: SLF001 — branche de repli
    finally:
        provider.close()


def test_tool_fingerprints_drop_status_boilerplate_from_result_channel(tmp_path: Path):
    """B2 : 'Edit applied successfully.' ne crée AUCUN bucket résultat.

    Le canal arguments garde un bucket par appel (344 buckets pour 344 appels sur le
    run 2026-10-03) ; le canal résultat n'en garde qu'un, de cardinal = nombre
    d'appels. Une sortie constante ne prouve aucune répétition.
    """
    db = _linkable_db(
        tmp_path / "opencode.db",
        texts=[],
        tools=[
            ("edit", "Edit applied successfully."),
            ("edit", "Edit applied successfully."),
            ("edit", "Edit applied successfully."),
            ("write", "Wrote file successfully."),
            ("compress", "Compressed 2 messages into [Compressed conversation section]."),
            ("glob", "No files found"),
            ("read", "<path>/home/benjamin/dev/a.py</path>"),
            ("todowrite", '[{"content": "un todo"}]'),
        ],
    )
    provider = _provider_for(db)
    try:
        args, results = provider.session_tool_fingerprints("ses_1", 0, WINDOW_END_MS)
    finally:
        provider.close()
    # aucun bucket pour les statuts constants / à cardinal 1
    for tool in ("edit", "write", "compress", "glob"):
        assert tool not in results, tool
    # les payloads réels survivent
    assert len(results["read"]) == 1
    assert len(results["todowrite"]) == 1
    # le canal arguments est INTACT : 3 appels edit = 3 empreintes distinctes
    assert len(args["edit"]) == 3
    assert list(args["edit"].values()) == [1, 1, 1]


def test_tool_fingerprints_keep_structured_outputs(tmp_path: Path):
    """B2 : une sortie structurée n'est jamais un statut, même courte."""
    db = _linkable_db(
        tmp_path / "opencode.db",
        texts=[],
        tools=[
            ("edit", {"file": "a.py", "edits": 1}),
            ("bash", {"stdout": "", "exit": 0}),
        ],
    )
    provider = _provider_for(db)
    try:
        _args, results = provider.session_tool_fingerprints("ses_1", 0, WINDOW_END_MS)
    finally:
        provider.close()
    assert len(results["edit"]) == 1
    assert len(results["bash"]) == 1
