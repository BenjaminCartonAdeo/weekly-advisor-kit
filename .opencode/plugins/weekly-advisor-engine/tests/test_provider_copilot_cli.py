"""Provider Copilot CLI : session-store.db synthétique (tmp_path)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tests.helpers import tzutc
from weekly_telemetry_aggregator.config import TelemetryConfig
from weekly_telemetry_aggregator.models import canonical_session_id
from weekly_telemetry_aggregator.providers import discover_provider_factories
from weekly_telemetry_aggregator.providers.implementations.copilot_cli import (
    HARNESS_COPILOT_CLI,
    PROVIDER_TYPE,
    CopilotCliSessionProvider,
    _event_cost,
    _parse_nano_cost,
    build_provider,
    resolve_copilot_home,
)
from weekly_telemetry_aggregator.sqlite_reader import SchemaError

T0 = tzutc(2026, 7, 30, 10, 0, 0)
T1 = tzutc(2026, 7, 30, 10, 1, 0)
T2 = tzutc(2026, 7, 30, 10, 5, 0)
T_OLD = tzutc(2026, 7, 20, 10, 0, 0)


def _fmt(dt) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


SID_FULL = "aaaaaaaa-1111-4111-8111-000000000001"
SID_EMPTY = "bbbbbbbb-2222-4222-8222-000000000002"
SID_OLD = "cccccccc-3333-4333-8333-000000000003"

SUMMARY_FULL = "Fix the flaky test"
USER_MSG_1 = "Fix the flaky test in the login module"
USER_MSG_2 = "Also update the docs"
ASSISTANT_1 = "Done, fixed."
ASSISTANT_2 = "Docs updated."


def _seed_db(db: Path, *, with_optionals: bool = True) -> None:
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, cwd TEXT, repository TEXT, branch TEXT,
            summary TEXT, created_at TEXT, updated_at TEXT
        );
        CREATE TABLE turns (
            session_id TEXT, turn_index INTEGER, user_message TEXT,
            assistant_response TEXT, timestamp TEXT UNIQUE
        );
        CREATE TABLE assistant_usage_events (
            session_id TEXT, model TEXT NOT NULL, input_tokens REAL,
            output_tokens REAL, cache_read_tokens REAL, cache_write_tokens REAL,
            reasoning_tokens REAL, created_at TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?,?)",
        (
            SID_FULL,
            "/home/user/proj",
            "https://github.com/u/proj",
            "main",
            SUMMARY_FULL,
            _fmt(T0),
            _fmt(T2),
        ),
    )
    conn.execute(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?,?)",
        (SID_EMPTY, "/home/user/empty", None, "main", None, _fmt(T0), _fmt(T0)),
    )
    conn.execute(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?,?)",
        (SID_OLD, "/home/user/old", None, "main", "Old stuff", _fmt(T_OLD), _fmt(T_OLD)),
    )
    conn.execute(
        "INSERT INTO turns VALUES (?,?,?,?,?)",
        (SID_FULL, 0, USER_MSG_1, ASSISTANT_1, _fmt(T1)),
    )
    conn.execute(
        "INSERT INTO turns VALUES (?,?,?,?,?)",
        (SID_FULL, 1, USER_MSG_2, ASSISTANT_2, _fmt(T2)),
    )
    conn.execute(
        "INSERT INTO assistant_usage_events VALUES (?,?,?,?,?,?,?,?)",
        (SID_FULL, "gpt-4o", 1200, 340, 100, 20, 50, _fmt(T1)),
    )
    conn.execute(
        "INSERT INTO assistant_usage_events VALUES (?,?,?,?,?,?,?,?)",
        (SID_FULL, "gpt-4o", 500, 100, 0, 0, 10, _fmt(T2)),
    )
    if with_optionals:
        conn.executescript(
            """
            CREATE TABLE forge_trajectory_events (
                session_id TEXT, event_type TEXT, command TEXT,
                output TEXT, created_at TEXT
            );
            CREATE TABLE session_files (
                session_id TEXT, file_path TEXT, tool_name TEXT
            );
            CREATE TABLE schema_version (version TEXT);
            """
        )
        conn.execute(
            "INSERT INTO forge_trajectory_events VALUES (?,?,?,?,?)",
            (SID_FULL, "command", "runTests", "ok output", _fmt(T1)),
        )
        conn.execute(
            "INSERT INTO forge_trajectory_events VALUES (?,?,?,?,?)",
            (SID_FULL, "note", "ignored-type", "zzz", _fmt(T2)),
        )
        conn.execute(
            "INSERT INTO session_files VALUES (?,?,?)",
            (SID_FULL, "/home/user/proj/a.py", "read"),
        )
        conn.execute("INSERT INTO schema_version VALUES (?)", ("7",))
    conn.commit()
    conn.close()


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    root = tmp_path / ".copilot"
    root.mkdir()
    _seed_db(root / "session-store.db")
    return root


@pytest.fixture()
def provider(home: Path) -> CopilotCliSessionProvider:
    built = build_provider({"type": PROVIDER_TYPE, "copilot_home": str(home)}, TelemetryConfig())
    assert built is not None
    return built


# --- factory / registry -------------------------------------------------------


def test_provider_type_discovered_by_registry():
    assert discover_provider_factories().get(PROVIDER_TYPE) is not None


def test_factory_returns_none_when_db_missing(tmp_path: Path):
    home = tmp_path / ".copilot"
    home.mkdir()
    assert (
        build_provider({"type": PROVIDER_TYPE, "copilot_home": str(home)}, TelemetryConfig())
        is None
    )


def test_factory_resolves_env_and_default(monkeypatch, tmp_path: Path):
    home = tmp_path / "cfgdir"
    home.mkdir()
    _seed_db(home / "session-store.db")
    monkeypatch.setenv("COPILOT_CONFIG_DIR", str(home))
    assert build_provider({"type": PROVIDER_TYPE}, TelemetryConfig()) is not None
    monkeypatch.delenv("COPILOT_CONFIG_DIR")
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "nope")
    assert build_provider({"type": PROVIDER_TYPE}, TelemetryConfig()) is None


def test_resolve_copilot_home_priority(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("COPILOT_CONFIG_DIR", str(tmp_path / "env"))
    assert resolve_copilot_home({"copilot_home": str(tmp_path / "cfg")}) == tmp_path / "cfg"
    assert resolve_copilot_home({}) == tmp_path / "env"


def test_resolve_copilot_home_auto_uses_default(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("COPILOT_CONFIG_DIR", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # Windows lit %USERPROFILE% avant Path.home() : le neutraliser aussi.
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert resolve_copilot_home({"copilot_home": "auto"}) == tmp_path / ".copilot"


def test_harness_constants(provider):
    assert provider.harness == HARNESS_COPILOT_CLI == "copilot-cli"
    assert PROVIDER_TYPE == HARNESS_COPILOT_CLI


# --- énumération ---------------------------------------------------------------


def test_list_sessions_namespaces_ids_and_fields(provider):
    sessions = {s.session_id: s for s in provider.list_sessions(0)}
    assert set(sessions) == {
        canonical_session_id(HARNESS_COPILOT_CLI, sid) for sid in (SID_FULL, SID_EMPTY, SID_OLD)
    }
    full = sessions[canonical_session_id(HARNESS_COPILOT_CLI, SID_FULL)]
    assert full.harness == HARNESS_COPILOT_CLI
    assert full.directory == "/home/user/proj"
    assert full.title == SUMMARY_FULL
    assert full.model_key == "github-copilot/gpt-4o"
    assert full.cost is None
    assert full.tokens_input == 1700 and full.tokens_output == 440
    assert full.tokens_reasoning == 60
    assert full.time_created == T0 and full.time_updated == T2
    empty = sessions[canonical_session_id(HARNESS_COPILOT_CLI, SID_EMPTY)]
    assert empty.title is None and empty.model_key == "unknown/unknown"


def test_list_sessions_ordered_desc_and_windowed(provider):
    since_ms = int(T0.timestamp() * 1000) + 1
    ids = [s.session_id for s in provider.list_sessions(since_ms)]
    assert ids == [canonical_session_id(HARNESS_COPILOT_CLI, SID_FULL)]


# --- sessions vides / fail-soft -------------------------------------------------


def test_empty_session_yields_clean_empty_results(provider):
    cid = canonical_session_id(HARNESS_COPILOT_CLI, SID_EMPTY)
    assert provider.has_telemetry_rows(cid) is False
    assert provider.session_steps(cid, 0, 2**63 - 1) == []
    assert provider.session_tools(cid, 0, 2**63 - 1) == ({}, {}, {})
    assert provider.session_user_turns(cid, 0, 2**63 - 1) == []
    assert provider.session_context_chars(cid, 0, 2**63 - 1) == {
        "file": 0,
        "tool_result": 0,
        "text": 0,
        "reasoning": 0,
    }
    assert provider.session_parts(cid) == []
    assert provider.session_aggregates(cid) is None


def test_unknown_session_ids_are_fail_soft(provider):
    ghost = canonical_session_id(HARNESS_COPILOT_CLI, "no-such-session")
    assert provider.has_telemetry_rows(ghost) is False
    assert provider.session_steps(ghost, 0, 2**63 - 1) == []
    assert provider.session_parts(ghost) == []
    assert provider.session_aggregates(ghost) is None


def test_raw_id_accepted(provider):
    assert provider.has_telemetry_rows(SID_FULL) is True
    assert provider.session_parts(SID_EMPTY) == []


# --- session peuplée ------------------------------------------------------------


def test_populated_session_has_telemetry_rows(provider):
    assert provider.has_telemetry_rows(canonical_session_id(HARNESS_COPILOT_CLI, SID_FULL)) is True


def test_session_steps_window_mapping_and_harness_tag(provider):
    cid = canonical_session_id(HARNESS_COPILOT_CLI, SID_FULL)
    end = int(T1.timestamp() * 1000) + 60_000
    steps = provider.session_steps(cid, 0, end)
    assert len(steps) == 1
    step = steps[0]
    assert step.harness == HARNESS_COPILOT_CLI
    assert step.session_id == cid
    assert step.model == "github-copilot/gpt-4o"
    assert step.tokens_input == 1200 and step.tokens_output == 340
    assert step.tokens_reasoning == 50
    assert step.cost is None
    assert step.timestamp == T1
    assert len(provider.session_steps(cid, 0, 2**63 - 1)) == 2


def test_session_steps_keeps_slashed_model_raw(tmp_path: Path):
    home = tmp_path / ".copilot"
    home.mkdir()
    _seed_db(home / "session-store.db")
    conn = sqlite3.connect(str(home / "session-store.db"))
    conn.execute(
        "INSERT INTO assistant_usage_events VALUES (?,?,?,?,?,?,?,?)",
        (SID_FULL, "openai/gpt-5", 10, 5, 0, 0, 0, _fmt(T2)),
    )
    conn.commit()
    conn.close()
    built = build_provider({"type": PROVIDER_TYPE, "copilot_home": str(home)}, TelemetryConfig())
    assert built is not None
    cid = canonical_session_id(HARNESS_COPILOT_CLI, SID_FULL)
    models = {s.model for s in built.session_steps(cid, 0, 2**63 - 1)}
    assert "openai/gpt-5" in models
    built.close()


def test_session_user_turns_in_window(provider):
    cid = canonical_session_id(HARNESS_COPILOT_CLI, SID_FULL)
    end = int(T1.timestamp() * 1000) + 60_000
    assert provider.session_user_turns(cid, 0, end) == [USER_MSG_1]
    assert provider.session_user_turns(cid, 0, 2**63 - 1) == [USER_MSG_1, USER_MSG_2]


def test_session_tools_trajectory_command_only(provider):
    cid = canonical_session_id(HARNESS_COPILOT_CLI, SID_FULL)
    calls, arg_chars, skills = provider.session_tools(cid, 0, 2**63 - 1)
    assert calls == {"runTests": 1}  # event_type 'note' exclu
    assert arg_chars == {"runTests": len("runTests")}
    assert skills == {}


def test_session_tools_fallback_session_files(tmp_path: Path):
    home = tmp_path / ".copilot"
    home.mkdir()
    _seed_db(home / "session-store.db", with_optionals=False)
    conn = sqlite3.connect(str(home / "session-store.db"))
    conn.execute("CREATE TABLE session_files (session_id TEXT, file_path TEXT, tool_name TEXT)")
    conn.execute("INSERT INTO session_files VALUES (?,?,?)", (SID_FULL, "/x/y.py", "read"))
    conn.commit()
    conn.close()
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        built = build_provider(
            {"type": PROVIDER_TYPE, "copilot_home": str(home)}, TelemetryConfig()
        )
    assert built is not None
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        calls, arg_chars, skills = built.session_tools(
            canonical_session_id(HARNESS_COPILOT_CLI, SID_FULL), 0, 2**63 - 1
        )
    assert calls == {"read": 1}
    assert arg_chars == {"read": len("/x/y.py")}
    assert skills == {}
    built.close()


def test_session_context_chars_categories_and_reasoning_factor(provider):
    cid = canonical_session_id(HARNESS_COPILOT_CLI, SID_FULL)
    counts = provider.session_context_chars(cid, 0, 2**63 - 1)
    assert set(counts) == {"file", "tool_result", "text", "reasoning"}
    assert counts["text"] == len(USER_MSG_1) + len(ASSISTANT_1) + len(USER_MSG_2) + len(ASSISTANT_2)
    assert counts["tool_result"] == len("ok output") + len("zzz")
    assert counts["file"] == len("/home/user/proj/a.py")
    assert counts["reasoning"] == 60 * 4


def test_session_aggregates_lifetime_sums(provider):
    agg = provider.session_aggregates(canonical_session_id(HARNESS_COPILOT_CLI, SID_FULL))
    assert agg is not None
    assert agg["tokens_input"] == 1700 and agg["tokens_output"] == 440
    assert agg["tokens_reasoning"] == 60


def test_session_parts_ordering_and_truncation(provider):
    cid = canonical_session_id(HARNESS_COPILOT_CLI, SID_FULL)
    parts = provider.session_parts(cid)
    kinds = [p.kind for p in parts]
    assert kinds[:2] == ["user", "assistant"]
    assert parts[0].text == USER_MSG_1
    tools = [p for p in parts if p.kind == "tool"]
    assert len(tools) == 2
    assert tools[0].tool_name == "runTests"
    assert all(p.ts is not None for p in parts)
    assert all(len(p.tool_output or "") <= 2000 for p in tools)


def test_find_session_by_title_summary_then_turn_prefix(provider):
    hit = provider.find_session_by_title(SUMMARY_FULL)
    assert hit is not None
    assert hit.session_id == canonical_session_id(HARNESS_COPILOT_CLI, SID_FULL)
    hit2 = provider.find_session_by_title(USER_MSG_1[:80])
    assert hit2 is not None
    assert hit2.session_id == canonical_session_id(HARNESS_COPILOT_CLI, SID_FULL)
    assert provider.find_session_by_title("inconnu") is None


def test_search_content_fts(tmp_path: Path):
    home = tmp_path / ".copilot"
    home.mkdir()
    _seed_db(home / "session-store.db")
    conn = sqlite3.connect(str(home / "session-store.db"))
    try:
        conn.execute("CREATE VIRTUAL TABLE search_index USING fts5(content, session_id UNINDEXED)")
    except sqlite3.OperationalError:
        conn.close()
        pytest.skip("FTS5 indisponible")
    conn.execute("INSERT INTO search_index VALUES (?,?)", ("flaky login failure", SID_FULL))
    conn.commit()
    conn.close()
    built = build_provider({"type": PROVIDER_TYPE, "copilot_home": str(home)}, TelemetryConfig())
    assert built is not None
    assert built.search_content("flaky") == [SID_FULL]
    assert built.search_content('fla"ky') == []  # échappement quotes, sans crash
    built.close()


def test_search_content_absent_table_returns_empty(tmp_path: Path):
    home = tmp_path / ".copilot"
    home.mkdir()
    _seed_db(home / "session-store.db", with_optionals=False)
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        built = build_provider(
            {"type": PROVIDER_TYPE, "copilot_home": str(home)}, TelemetryConfig()
        )
    assert built is not None
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        assert built.search_content("x") == []
    built.close()


# --- robustesse ------------------------------------------------------------------


def test_state_enrichment_fills_missing_cwd(tmp_path: Path):
    home = tmp_path / ".copilot"
    home.mkdir()
    _seed_db(home / "session-store.db")
    conn = sqlite3.connect(str(home / "session-store.db"))
    conn.execute("UPDATE sessions SET cwd = NULL WHERE id = ?", (SID_EMPTY,))
    conn.commit()
    conn.close()
    state = home / "session-state" / SID_EMPTY
    (state / "workspace.yaml").parent.mkdir(parents=True)
    (state / "workspace.yaml").write_text("cwd: /ws/from-yaml\n", encoding="utf-8")
    (state / "checkpoints").mkdir()
    (state / "checkpoints" / "index.md").write_text("# Yaml Title\n", encoding="utf-8")
    built = build_provider({"type": PROVIDER_TYPE, "copilot_home": str(home)}, TelemetryConfig())
    assert built is not None
    sessions = {s.session_id: s for s in built.list_sessions(0)}
    empty = sessions[canonical_session_id(HARNESS_COPILOT_CLI, SID_EMPTY)]
    assert empty.directory == "/ws/from-yaml"
    assert empty.title == "Yaml Title"
    built.close()


def test_auth_and_config_files_never_read(tmp_path: Path):
    home = tmp_path / ".copilot"
    home.mkdir()
    _seed_db(home / "session-store.db")
    (home / "config.json").write_text("{invalid", encoding="utf-8")
    (home / "mcp-config.json").write_text("{invalid", encoding="utf-8")
    built = build_provider({"type": PROVIDER_TYPE, "copilot_home": str(home)}, TelemetryConfig())
    assert built is not None  # fichiers sensibles/illlisibles ignorés sans crash
    assert len(built.list_sessions(0)) == 3
    built.close()


def test_check_schema_ok_and_schema_version(provider):
    provider.check_schema()  # cœur présent → aucune erreur
    assert getattr(provider, "schema_version", None) == "7"


def test_check_schema_warns_on_missing_optionals(tmp_path: Path):
    home = tmp_path / ".copilot"
    home.mkdir()
    _seed_db(home / "session-store.db", with_optionals=False)
    built = build_provider({"type": PROVIDER_TYPE, "copilot_home": str(home)}, TelemetryConfig())
    assert built is not None
    with pytest.warns(UserWarning, match="optionnelle"):
        built.check_schema()
    built.close()


def test_check_schema_raises_on_missing_core_table(tmp_path: Path):
    home = tmp_path / ".copilot"
    home.mkdir()
    _seed_db(home / "session-store.db")
    conn = sqlite3.connect(str(home / "session-store.db"))
    conn.execute("DROP TABLE turns")
    conn.commit()
    conn.close()
    built = build_provider({"type": PROVIDER_TYPE, "copilot_home": str(home)}, TelemetryConfig())
    assert built is not None
    with pytest.raises(SchemaError, match="turns"):
        built.check_schema()
    built.close()


def test_close_is_idempotent(provider):
    provider.close()
    provider.close()


def test_corrupt_db_returns_none(tmp_path: Path):
    home = tmp_path / ".copilot"
    home.mkdir()
    (home / "session-store.db").write_text("not a database", encoding="utf-8")
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        built = build_provider(
            {"type": PROVIDER_TYPE, "copilot_home": str(home)}, TelemetryConfig()
        )
    # DB corrompue : None (fail-soft) ou provider vide qui lève au check_schema.
    if built is not None:
        with pytest.raises(SchemaError):
            built.check_schema()
        built.close()


# --- coûts nano (E1+E2+E3) --------------------------------------------------------


def _seed_nano_db(db: Path) -> None:
    """DB avec `token_details_json` + `request_multiplier` (branche nano couverte)."""
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, cwd TEXT, repository TEXT, branch TEXT,
            summary TEXT, created_at TEXT, updated_at TEXT
        );
        CREATE TABLE turns (
            session_id TEXT, turn_index INTEGER, user_message TEXT,
            assistant_response TEXT, timestamp TEXT UNIQUE
        );
        CREATE TABLE assistant_usage_events (
            session_id TEXT, model TEXT NOT NULL, input_tokens REAL,
            output_tokens REAL, cache_read_tokens REAL, cache_write_tokens REAL,
            reasoning_tokens REAL, created_at TEXT,
            token_details_json TEXT, request_multiplier REAL
        );
        """
    )
    conn.execute(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?,?)",
        (SID_FULL, "/home/user/proj", None, "main", SUMMARY_FULL, _fmt(T0), _fmt(T2)),
    )
    conn.execute(
        "INSERT INTO turns VALUES (?,?,?,?,?)",
        (SID_FULL, 0, USER_MSG_1, ASSISTANT_1, _fmt(T1)),
    )
    conn.execute(
        "INSERT INTO assistant_usage_events "
        "(session_id, model, input_tokens, output_tokens, cache_read_tokens, "
        "cache_write_tokens, reasoning_tokens, created_at, "
        "token_details_json, request_multiplier) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (SID_FULL, "gpt-4o", 1200, 340, 100, 20, 50, _fmt(T1), '{"total_nano_aiu": 1e11}', 1.0),
    )
    conn.execute(
        "INSERT INTO assistant_usage_events "
        "(session_id, model, input_tokens, output_tokens, cache_read_tokens, "
        "cache_write_tokens, reasoning_tokens, created_at, "
        "token_details_json, request_multiplier) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            SID_FULL,
            "gpt-4o",
            500,
            100,
            0,
            0,
            10,
            _fmt(T2),
            '{"total_nano_aiu": 2.26095e15}',
            1.0,
        ),
    )
    conn.commit()
    conn.close()


@pytest.fixture()
def nano_provider(tmp_path: Path) -> CopilotCliSessionProvider:
    home = tmp_path / ".copilot"
    home.mkdir()
    _seed_nano_db(home / "session-store.db")
    built = build_provider({"type": PROVIDER_TYPE, "copilot_home": str(home)}, TelemetryConfig())
    assert built is not None
    return built


def test_parse_nano_cost_one_aiu_cent_is_one_dollar():
    assert _parse_nano_cost('{"total_nano_aiu": 1e11}', 1.0) == pytest.approx(1.0)


def test_parse_nano_cost_large_value():
    # 2.26095e15 nano / 1e11 = $22609.50 (1 AIU = 1 crédit = $0.01).
    assert _parse_nano_cost('{"total_nano_aiu": 2.26095e15}', 1.0) == pytest.approx(22609.5)


def test_parse_nano_cost_applies_multiplier():
    assert _parse_nano_cost('{"total_nano_aiu": 1e11}', 2.0) == pytest.approx(2.0)


def test_parse_nano_cost_fail_soft():
    import warnings

    assert _parse_nano_cost(None, None) == 0.0
    assert _parse_nano_cost("", 1.0) == 0.0
    assert _parse_nano_cost("{}", 1.0) == 0.0
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        assert _parse_nano_cost("{invalid", 1.0) == 0.0
        assert _parse_nano_cost('{"total_nano_aiu": "abc"}', 1.0) == 0.0
        assert _parse_nano_cost('{"total_nano_aiu": 1e11}', "abc") == pytest.approx(1.0)


def test_nano_steps_carry_cost_and_match_aggregates(nano_provider):
    cid = canonical_session_id(HARNESS_COPILOT_CLI, SID_FULL)
    steps = nano_provider.session_steps(cid, 0, 2**63 - 1)
    assert len(steps) == 2
    assert steps[0].cost == pytest.approx(1.0)
    assert steps[1].cost == pytest.approx(22609.5)
    agg = nano_provider.session_aggregates(cid)
    assert agg is not None
    assert agg["cost"] == pytest.approx(22610.5)
    assert sum(s.cost for s in steps if s.cost is not None) == pytest.approx(agg["cost"])


def test_nano_parts_contain_step_finish_with_cost(nano_provider):
    cid = canonical_session_id(HARNESS_COPILOT_CLI, SID_FULL)
    parts = nano_provider.session_parts(cid)
    finishes = [p for p in parts if p.kind == "step-finish"]
    assert len(finishes) == 2
    assert finishes[0].cost == pytest.approx(1.0)
    assert finishes[1].cost == pytest.approx(22609.5)
    agg = nano_provider.session_aggregates(cid)
    assert agg is not None
    assert sum(p.cost for p in finishes if p.cost is not None) == pytest.approx(agg["cost"])


def test_no_nano_columns_keeps_legacy_behavior(provider):
    """Sans colonnes nano : steps à None, aucun part step-finish (inchangé)."""
    cid = canonical_session_id(HARNESS_COPILOT_CLI, SID_FULL)
    assert all(s.cost is None for s in provider.session_steps(cid, 0, 2**63 - 1))
    assert [p for p in provider.session_parts(cid) if p.kind == "step-finish"] == []


# --- coûts nano v2 : colonne total_nano_aiu + fallback tableau ------------------

#: Batches de référence : 500M + 332338000 = 832338000 nano (ligne réelle 1).
_ARRAY_REF = (
    '[{"batchSize": 1000, "costPerBatch": 500000000, "tokenCount": 1000,'
    ' "tokenType": "input", "model": "gpt-4o"},'
    ' {"batchSize": 500, "costPerBatch": 332338000, "tokenCount": 500,'
    ' "tokenType": "output", "model": "gpt-4o"}]'
)
#: Leurre à $1.0 pour prouver la priorité de la colonne sur l'array.
_ARRAY_DECOY_1USD = (
    '[{"batchSize": 1, "costPerBatch": 100000000000, "tokenCount": 1,'
    ' "tokenType": "input", "model": "gpt-4o"}]'
)
_NANO_REF_USD = 832338000 / 1e11  # 0.00832338


def _seed_mixed_nano_db(db: Path) -> None:
    """DB 3 formes : colonne peuplée / NULL+array / dict legacy."""
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, cwd TEXT, repository TEXT, branch TEXT,
            summary TEXT, created_at TEXT, updated_at TEXT
        );
        CREATE TABLE turns (
            session_id TEXT, turn_index INTEGER, user_message TEXT,
            assistant_response TEXT, timestamp TEXT UNIQUE
        );
        CREATE TABLE assistant_usage_events (
            session_id TEXT, model TEXT NOT NULL, input_tokens REAL,
            output_tokens REAL, cache_read_tokens REAL, cache_write_tokens REAL,
            reasoning_tokens REAL, created_at TEXT,
            total_nano_aiu INTEGER, token_details_json TEXT, request_multiplier REAL
        );
        """
    )
    conn.execute(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?,?)",
        (SID_FULL, "/home/user/proj", None, "main", SUMMARY_FULL, _fmt(T0), _fmt(T2)),
    )
    conn.execute(
        "INSERT INTO turns VALUES (?,?,?,?,?)",
        (SID_FULL, 0, USER_MSG_1, ASSISTANT_1, _fmt(T1)),
    )
    conn.execute(
        "INSERT INTO assistant_usage_events "
        "(session_id, model, input_tokens, output_tokens, cache_read_tokens, "
        "cache_write_tokens, reasoning_tokens, created_at, "
        "total_nano_aiu, token_details_json, request_multiplier) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (SID_FULL, "gpt-4o", 1200, 340, 100, 20, 50, _fmt(T0), 832338000, _ARRAY_DECOY_1USD, 1.0),
    )
    conn.execute(
        "INSERT INTO assistant_usage_events "
        "(session_id, model, input_tokens, output_tokens, cache_read_tokens, "
        "cache_write_tokens, reasoning_tokens, created_at, "
        "total_nano_aiu, token_details_json, request_multiplier) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (SID_FULL, "gpt-4o", 500, 100, 0, 0, 10, _fmt(T1), None, _ARRAY_REF, 1.0),
    )
    conn.execute(
        "INSERT INTO assistant_usage_events "
        "(session_id, model, input_tokens, output_tokens, cache_read_tokens, "
        "cache_write_tokens, reasoning_tokens, created_at, "
        "total_nano_aiu, token_details_json, request_multiplier) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (SID_FULL, "gpt-4o", 100, 50, 0, 0, 0, _fmt(T2), None, '{"total_nano_aiu": 1e11}', 1.0),
    )
    conn.commit()
    conn.close()


@pytest.fixture()
def mixed_nano_provider(tmp_path: Path) -> CopilotCliSessionProvider:
    home = tmp_path / ".copilot"
    home.mkdir()
    _seed_mixed_nano_db(home / "session-store.db")
    built = build_provider({"type": PROVIDER_TYPE, "copilot_home": str(home)}, TelemetryConfig())
    assert built is not None
    return built


def test_parse_nano_cost_array_batches():
    # Σ batches = 832338000 nano → $0.00832338.
    assert _parse_nano_cost(_ARRAY_REF, 1.0) == pytest.approx(_NANO_REF_USD)
    assert _parse_nano_cost(_ARRAY_REF, 2.0) == pytest.approx(_NANO_REF_USD * 2.0)


def test_parse_nano_cost_array_fail_soft():
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        # Entrées invalides ignorées : batchSize 0, clés manquantes, non-dict.
        assert (
            _parse_nano_cost('[{"batchSize": 0, "costPerBatch": 5, "tokenCount": 5}]', 1.0) == 0.0
        )
        assert _parse_nano_cost('[{"nope": 1}, 42, null]', 1.0) == 0.0
        assert _parse_nano_cost("[]", 1.0) == 0.0
        assert _parse_nano_cost("[1, 2]", 1.0) == 0.0


def test_event_cost_column_priority_over_array():
    # Colonne 832338000 prioritaire sur l'array leurre à $1.0.
    assert _event_cost(832338000, _ARRAY_DECOY_1USD, 1.0) == pytest.approx(_NANO_REF_USD)
    assert _event_cost(1e11, None, 2.0) == pytest.approx(2.0)
    assert _event_cost(None, _ARRAY_REF, 1.0) == pytest.approx(_NANO_REF_USD)
    assert _event_cost(None, '{"total_nano_aiu": 1e11}', 1.0) == pytest.approx(1.0)
    assert _event_cost(None, None, 1.0) == 0.0


def test_mixed_nano_steps_priority_and_fallbacks(mixed_nano_provider):
    cid = canonical_session_id(HARNESS_COPILOT_CLI, SID_FULL)
    steps = mixed_nano_provider.session_steps(cid, 0, 2**63 - 1)
    assert len(steps) == 3
    assert steps[0].cost == pytest.approx(_NANO_REF_USD)  # colonne > array leurre
    assert steps[1].cost == pytest.approx(_NANO_REF_USD)  # array → 832338000 nano
    assert steps[2].cost == pytest.approx(1.0)  # dict legacy inchangé
    agg = mixed_nano_provider.session_aggregates(cid)
    assert agg is not None
    assert agg["cost"] == pytest.approx(_NANO_REF_USD * 2 + 1.0)
    assert sum(s.cost for s in steps if s.cost is not None) == pytest.approx(agg["cost"])


def test_mixed_nano_parts_match_aggregates(mixed_nano_provider):
    cid = canonical_session_id(HARNESS_COPILOT_CLI, SID_FULL)
    parts = mixed_nano_provider.session_parts(cid)
    finishes = [p for p in parts if p.kind == "step-finish"]
    assert len(finishes) == 3
    assert finishes[0].cost == pytest.approx(_NANO_REF_USD)
    assert finishes[1].cost == pytest.approx(_NANO_REF_USD)
    assert finishes[2].cost == pytest.approx(1.0)
    agg = mixed_nano_provider.session_aggregates(cid)
    assert agg is not None
    assert sum(p.cost for p in finishes if p.cost is not None) == pytest.approx(agg["cost"])


def test_total_only_db_without_details_column(tmp_path: Path):
    """Colonne total_nano_aiu seule (sans token_details_json) : coût porté."""
    home = tmp_path / ".copilot"
    home.mkdir()
    conn = sqlite3.connect(str(home / "session-store.db"))
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, cwd TEXT, repository TEXT, branch TEXT,
            summary TEXT, created_at TEXT, updated_at TEXT
        );
        CREATE TABLE turns (
            session_id TEXT, turn_index INTEGER, user_message TEXT,
            assistant_response TEXT, timestamp TEXT UNIQUE
        );
        CREATE TABLE assistant_usage_events (
            session_id TEXT, model TEXT NOT NULL, input_tokens REAL,
            output_tokens REAL, cache_read_tokens REAL, cache_write_tokens REAL,
            reasoning_tokens REAL, created_at TEXT,
            total_nano_aiu INTEGER, request_multiplier REAL
        );
        """
    )
    conn.execute(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?,?)",
        (SID_FULL, "/home/user/proj", None, "main", SUMMARY_FULL, _fmt(T0), _fmt(T2)),
    )
    conn.execute(
        "INSERT INTO assistant_usage_events VALUES (?,?,?,?,?,?,?,?,?,?)",
        (SID_FULL, "gpt-4o", 10, 5, 0, 0, 0, _fmt(T1), 832338000, 1.0),
    )
    conn.commit()
    conn.close()
    built = build_provider({"type": PROVIDER_TYPE, "copilot_home": str(home)}, TelemetryConfig())
    assert built is not None
    cid = canonical_session_id(HARNESS_COPILOT_CLI, SID_FULL)
    steps = built.session_steps(cid, 0, 2**63 - 1)
    assert len(steps) == 1
    assert steps[0].cost == pytest.approx(_NANO_REF_USD)
    agg = built.session_aggregates(cid)
    assert agg is not None
    assert agg["cost"] == pytest.approx(_NANO_REF_USD)
    built.close()
