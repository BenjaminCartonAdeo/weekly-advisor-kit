"""Provider Claude Code : arborescence `<projects_dir>/<cwd-mungé>/<sid>.jsonl` synthétique."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from tests.helpers import tzutc
from weekly_telemetry_aggregator.config import TelemetryConfig
from weekly_telemetry_aggregator.models import canonical_session_id
from weekly_telemetry_aggregator.providers import build_providers, discover_provider_factories
from weekly_telemetry_aggregator.providers.implementations.claude_code import (
    HARNESS_CLAUDE_CODE,
    PROVIDER_TYPE,
    ClaudeCodeSessionProvider,
    build_provider,
)
from weekly_telemetry_aggregator.sqlite_reader import SchemaError

T0 = tzutc(2026, 7, 30, 10, 0, 0)
MS0 = int(T0.timestamp() * 1000)

SID_POP = "aaaaaaaa-1111-4111-8111-000000000001"
SID_EMPTY = "bbbbbbbb-2222-4222-8222-000000000002"
SID_OLD = "cccccccc-3333-4333-8333-000000000003"

CWD_ALPHA = "/home/user/proj-alpha"
CWD_BETA = "/home/user/proj-beta"


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _user(sid: str, ts: datetime, cwd: str, content) -> dict:
    return {
        "type": "user",
        "uuid": f"u-{int(ts.timestamp() * 1000)}",
        "sessionId": sid,
        "timestamp": _iso(ts),
        "cwd": cwd,
        "message": {"role": "user", "content": content},
    }


def _assistant(
    sid: str, ts: datetime, cwd: str, model: str, blocks: list[dict], usage: dict | None
) -> dict:
    message: dict = {"role": "assistant", "model": model, "content": blocks}
    if usage is not None:
        message["usage"] = usage
    return {
        "type": "assistant",
        "uuid": f"a-{int(ts.timestamp() * 1000)}",
        "sessionId": sid,
        "timestamp": _iso(ts),
        "cwd": cwd,
        "message": message,
    }


def _write_jsonl(directory: Path, name: str, entries: list[dict], raw_lines: list[str] | None = None):
    directory.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(e) for e in entries] + (raw_lines or [])
    (directory / f"{name}.jsonl").write_text("\n".join(lines), encoding="utf-8")


def _populated_entries() -> list[dict]:
    """Session peuplée : 2 steps assistant (usage), tool round-trip, lignes parasites."""
    return [
        _user(SID_POP, T0, CWD_ALPHA, "Fix the flaky test"),
        _assistant(
            SID_POP,
            tzutc(2026, 7, 30, 10, 0, 5),
            CWD_ALPHA,
            "claude-sonnet-4-5",
            [
                {"type": "text", "text": "On it."},
                {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "pytest -q"}},
            ],
            {
                "input_tokens": 1200,
                "output_tokens": 340,
                "cache_read_input_tokens": 900,
                "cache_creation_input_tokens": 50,
            },
        ),
        # ligne non conversationnelle (type inconnu) : ignorée silencieusement
        {"type": "summary", "summary": "irrelevant"},
        _user(
            SID_POP,
            tzutc(2026, 7, 30, 10, 0, 6),
            "/tmp/tool-result-cwd",
            [{"type": "tool_result", "tool_use_id": "t1", "content": "3 passed"}],
        ),
        _assistant(
            SID_POP,
            tzutc(2026, 7, 30, 10, 1, 0),
            CWD_ALPHA,
            "claude-opus-4-1[slurm]",
            [
                {"type": "thinking", "thinking": "tests pass now"},
                {"type": "text", "text": "All green."},
            ],
            {"input_tokens": 800, "output_tokens": 150},
        ),
    ]


@pytest.fixture()
def projects_dir(tmp_path: Path) -> Path:
    root = tmp_path / "projects"
    _write_jsonl(root / "-home-user-proj-alpha", SID_POP, _populated_entries())
    _write_jsonl(
        root / "-home-user-proj-beta",
        SID_EMPTY,
        [_user(SID_EMPTY, tzutc(2026, 7, 29, 10, 0, 0), CWD_BETA, "Just looking")],
    )
    _write_jsonl(
        root / "-home-user-proj-beta",
        SID_OLD,
        [_user(SID_OLD, tzutc(2026, 7, 25, 10, 0, 0), CWD_BETA, "Old question")],
    )
    return root


@pytest.fixture()
def provider(projects_dir: Path) -> ClaudeCodeSessionProvider:
    built = build_provider(
        {"type": PROVIDER_TYPE, "projects_dir": str(projects_dir)}, TelemetryConfig()
    )
    assert built is not None
    return built


# --- factory / registry -------------------------------------------------------


def test_provider_type_discovered_by_registry():
    assert discover_provider_factories().get(PROVIDER_TYPE) is not None


def test_factory_returns_none_when_projects_dir_missing(tmp_path: Path):
    missing = tmp_path / "nope"
    assert (
        build_provider({"type": PROVIDER_TYPE, "projects_dir": str(missing)}, TelemetryConfig())
        is None
    )


def test_factory_uses_default_projects_dir_when_key_absent(monkeypatch, tmp_path: Path):
    import weekly_telemetry_aggregator.providers.implementations.claude_code as mod

    monkeypatch.setattr(mod, "_DEFAULT_PROJECTS_DIR", str(tmp_path / "claude"))
    assert build_provider({"type": PROVIDER_TYPE}, TelemetryConfig()) is None  # absent → None
    (tmp_path / "claude").mkdir()
    assert build_provider({"type": PROVIDER_TYPE}, TelemetryConfig()) is not None


def test_harness_constants(provider):
    assert provider.harness == HARNESS_CLAUDE_CODE == "claude-code"
    assert PROVIDER_TYPE == HARNESS_CLAUDE_CODE


def test_build_providers_integration_with_source_cfg(projects_dir: Path):
    cfg = TelemetryConfig()
    cfg.session_sources = [{"type": PROVIDER_TYPE, "projects_dir": str(projects_dir)}]
    providers = build_providers(cfg)
    assert [p.harness for p in providers] == [HARNESS_CLAUDE_CODE]
    providers[0].close()


# --- listing / fenêtrage / meta -----------------------------------------------


def test_list_sessions_namespaces_ids_and_majority_cwd(provider):
    sessions = {s.session_id: s for s in provider.list_sessions(0)}
    assert set(sessions) == {
        canonical_session_id(HARNESS_CLAUDE_CODE, sid) for sid in (SID_POP, SID_EMPTY, SID_OLD)
    }
    pop = sessions[canonical_session_id(HARNESS_CLAUDE_CODE, SID_POP)]
    # 3 lignes cwd proj-alpha vs 1 ligne /tmp → cwd majoritaire retenu
    assert pop.directory == CWD_ALPHA


def test_list_sessions_metadata_fields(provider):
    pop = next(s for s in provider.list_sessions(0) if s.session_id.endswith(SID_POP))
    assert pop.harness == HARNESS_CLAUDE_CODE
    assert pop.cost is None  # aucun prix journalisé par Claude Code
    assert pop.model_key == "anthropic/claude-sonnet-4-5"  # premier step avec usage
    assert pop.title == "Fix the flaky test"  # premier tour utilisateur
    assert pop.time_created == T0
    assert pop.time_updated == tzutc(2026, 7, 30, 10, 1, 0)
    # agrégation tokens multi-lignes assistant (1200+800 / 340+150)
    assert pop.tokens_input == 2000 and pop.tokens_output == 490
    assert pop.tokens_cache_read == 900 and pop.tokens_cache_write == 50


def test_list_sessions_window_filters_epoch_ms(provider):
    ids = [s.session_id for s in provider.list_sessions(MS0)]
    assert ids == [canonical_session_id(HARNESS_CLAUDE_CODE, SID_POP)]
    assert len(provider.list_sessions(0)) == 3


def test_title_truncated_to_max_chars(tmp_path: Path):
    long_text = "x" * 250
    root = tmp_path / "projects"
    _write_jsonl(
        root / "-tmp-x",
        SID_EMPTY,
        [_user(SID_EMPTY, T0, "/tmp/x", long_text)],
    )
    provider = ClaudeCodeSessionProvider(root)
    sessions = provider.list_sessions(0)
    assert sessions[0].title == "x" * 100


# --- sessions sans télémétrie ---------------------------------------------------


def test_empty_session_yields_clean_empty_results(provider):
    cid = canonical_session_id(HARNESS_CLAUDE_CODE, SID_EMPTY)
    assert provider.has_telemetry_rows(cid) is False
    assert provider.session_steps(cid, 0, MS0 * 2) == []
    assert provider.session_tools(cid, 0, MS0 * 2) == ({}, {}, {})
    assert provider.session_user_turns(cid, 0, MS0 * 2) == ["Just looking"]
    assert provider.session_context_chars(cid, 0, MS0 * 2) == {
        "file": 0,
        "tool_result": 0,
        # sémantique opencode : tout block texte (user comme assistant) compte dans "text"
        "text": len("Just looking"),
        "reasoning": 0,
    }
    assert [p.kind for p in provider.session_parts(cid)] == ["user"]
    assert provider.session_aggregates(cid) is None


def test_unknown_or_absent_session_ids_are_fail_soft(provider):
    ghost = canonical_session_id(HARNESS_CLAUDE_CODE, "no-such-session")
    assert provider.has_telemetry_rows(ghost) is False
    assert provider.session_steps(ghost, 0, MS0 * 2) == []
    assert provider.session_tools(ghost, 0, MS0 * 2) == ({}, {}, {})
    assert provider.session_user_turns(ghost, 0, MS0 * 2) == []
    assert provider.session_context_chars(ghost, 0, MS0 * 2) == {
        "file": 0,
        "tool_result": 0,
        "text": 0,
        "reasoning": 0,
    }
    assert provider.session_parts(ghost) == []
    assert provider.session_aggregates(ghost) is None


# --- session peuplée : mapping JSONL → Protocol ----------------------------------


def test_populated_session_has_telemetry_rows(provider):
    cid = canonical_session_id(HARNESS_CLAUDE_CODE, SID_POP)
    assert provider.has_telemetry_rows(cid) is True


def test_session_steps_window_mapping_and_harness_tag(provider):
    cid = canonical_session_id(HARNESS_CLAUDE_CODE, SID_POP)
    steps = provider.session_steps(cid, MS0, MS0 + 30_000)
    assert len(steps) == 1  # second assistant (MS0+60s) hors fenêtre
    step = steps[0]
    assert step.harness == HARNESS_CLAUDE_CODE
    assert step.session_id == cid
    assert step.model == "anthropic/claude-sonnet-4-5"
    assert step.tokens_input == 1200 and step.tokens_output == 340
    assert step.tokens_cache_read == 900 and step.tokens_cache_write == 50
    assert step.cost is None
    assert step.timestamp == tzutc(2026, 7, 30, 10, 0, 5)
    both = provider.session_steps(cid, 0, MS0 * 2)
    assert len(both) == 2
    assert both[1].model == "anthropic/claude-opus-4-1"  # suffixe variant "[slurm]" écarté


def test_session_steps_aggregate_per_assistant_line_not_lifetime(provider):
    cid = canonical_session_id(HARNESS_CLAUDE_CODE, SID_POP)
    steps = provider.session_steps(cid, 0, MS0 * 2)
    assert [s.tokens_input for s in steps] == [1200.0, 800.0]


def test_session_user_turns_exclude_tool_results(provider):
    cid = canonical_session_id(HARNESS_CLAUDE_CODE, SID_POP)
    turns = provider.session_user_turns(cid, 0, MS0 * 2)
    assert turns == ["Fix the flaky test"]  # tool_result seul ≠ tour utilisateur


def test_session_tools_counts_calls_and_arg_chars(provider):
    cid = canonical_session_id(HARNESS_CLAUDE_CODE, SID_POP)
    calls, arg_chars, skills = provider.session_tools(cid, 0, MS0 * 2)
    assert calls == {"Bash": 1}
    assert arg_chars["Bash"] == len(json.dumps({"command": "pytest -q"}))
    assert skills == {}  # skills_loaded sans objet hors CLI


def test_session_context_chars_categories(provider):
    cid = canonical_session_id(HARNESS_CLAUDE_CODE, SID_POP)
    counts = provider.session_context_chars(cid, 0, MS0 * 2)
    assert set(counts) == {"file", "tool_result", "text", "reasoning"}
    # sémantique opencode : tout block texte (user comme assistant) compte dans "text"
    assert counts["text"] == len("Fix the flaky test") + len("On it.") + len("All green.")
    assert counts["tool_result"] == len("3 passed")
    assert counts["reasoning"] == len("tests pass now")
    assert counts["file"] == 0


def test_session_parts_ordering_and_kinds(provider):
    cid = canonical_session_id(HARNESS_CLAUDE_CODE, SID_POP)
    parts = provider.session_parts(cid)
    kinds = [p.kind for p in parts]
    # L1 user ; L2 text+tool_use ; L3 tool_result ; L4 thinking+text
    assert kinds == ["user", "assistant", "tool", "tool", "reasoning", "assistant"]
    assert parts[0].text == "Fix the flaky test"
    assert parts[2].tool_name == "Bash"
    assert parts[2].tool_input == json.dumps({"command": "pytest -q"})
    assert parts[3].tool_name is None
    assert parts[3].tool_output == "3 passed"
    assert parts[4].text == "tests pass now"
    assert all(p.cost is None for p in parts)


def test_session_aggregates_always_none_cross_check_cost(provider):
    cid = canonical_session_id(HARNESS_CLAUDE_CODE, SID_POP)
    assert provider.session_aggregates(cid) is None


def test_find_session_by_title_exact_match(provider):
    hit = provider.find_session_by_title("Fix the flaky test")
    assert hit is not None
    assert hit.session_id == canonical_session_id(HARNESS_CLAUDE_CODE, SID_POP)
    assert provider.find_session_by_title("inconnu") is None


def test_raw_uuid_accepted_as_session_id(provider):
    assert provider.has_telemetry_rows(SID_POP) is True
    assert provider.find_session_by_title("Old question") is not None


# --- robustesse ------------------------------------------------------------------


def test_corrupt_lines_skipped_with_single_warning(projects_dir: Path):
    target = projects_dir / "-home-user-proj-alpha" / f"{SID_POP}.jsonl"
    target.write_text(target.read_text(encoding="utf-8") + "\n{broken json", encoding="utf-8")
    with pytest.warns(UserWarning, match="partiellement illisible"):
        built = build_provider(
            {"type": PROVIDER_TYPE, "projects_dir": str(projects_dir)}, TelemetryConfig()
        )
    assert built is not None  # ligne corrompue ignorée, le reste est chargé :
    assert built.find_session_by_title("Fix the flaky test") is not None


def test_session_id_falls_back_to_file_stem_when_lines_lack_it(tmp_path: Path):
    root = tmp_path / "projects"
    entry = _user(SID_OLD, T0, "/tmp/x", "hello")
    del entry["sessionId"]
    _write_jsonl(root / "-tmp-x", "deadbeef-1234", [entry])
    provider = ClaudeCodeSessionProvider(root)
    ids = [s.session_id for s in provider.list_sessions(0)]
    assert ids == [canonical_session_id(HARNESS_CLAUDE_CODE, "deadbeef-1234")]


def test_check_schema_raises_without_projects_dir(tmp_path: Path):
    provider = ClaudeCodeSessionProvider(tmp_path / "missing")
    with pytest.raises(SchemaError, match="introuvable"):
        provider.check_schema()


def test_check_schema_raises_without_parsable_jsonl(tmp_path: Path):
    root = tmp_path / "projects"
    root.mkdir()
    (root / "-empty-proj").mkdir()
    provider = ClaudeCodeSessionProvider(root)
    with pytest.raises(SchemaError, match="aucun transcript JSONL parsable"):
        provider.check_schema()


def test_close_is_idempotent_noop(provider):
    provider.close()
    provider.close()  # aucune ressource persistante → aucun effet, aucune erreur
