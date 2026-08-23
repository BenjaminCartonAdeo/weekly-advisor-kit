"""Provider Copilot VS Code : arborescence workspaceStorage synthétique (tmp_path)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.helpers import tzutc
from weekly_telemetry_aggregator.config import TelemetryConfig
from weekly_telemetry_aggregator.models import canonical_session_id
from weekly_telemetry_aggregator.providers import discover_provider_factories
from weekly_telemetry_aggregator.providers.implementations.copilot_vscode import (
    HARNESS_COPILOT_VSCODE,
    PROVIDER_TYPE,
    CopilotVSCodeSessionProvider,
    build_provider,
)
from weekly_telemetry_aggregator.sqlite_reader import SchemaError

T0 = tzutc(2026, 7, 30, 10, 0, 0)
MS0 = int(T0.timestamp() * 1000)

UUID_EMPTY_ALPHA = "11111111-aaaa-4bbb-8ccc-000000000001"
UUID_POPULATED = "22222222-bbbb-4ccc-8ddd-000000000002"
UUID_OLD = "33333333-cccc-4ddd-8eee-000000000003"
UUID_EMPTY_BETA = "44444444-dddd-4eee-8fff-000000000004"

REQ_IN_WINDOW_MS = MS0 + 1_000
REQ_OUT_WINDOW_MS = MS0 + 90_000

POPULATED_REQUESTS = [
    {
        "requestId": "req-1",
        "timestamp": REQ_IN_WINDOW_MS,
        "message": {"role": "user", "text": "Fix the flaky test"},
        "modelId": "gpt-4o",
        "tokenCounts": {"inputTokens": 1200, "outputTokens": 340},
        "toolInvocations": [{"name": "runTests", "parameters": {"cmd": "pytest -q"}}],
        "responseText": "Done.",
        "result": {"errorDetails": None},
    },
    {
        "requestId": "req-2",
        "timestamp": REQ_OUT_WINDOW_MS,
        "message": "Second question?",
        "modelId": "gpt-4o",
        "tokenCounts": {"inputTokens": 500, "outputTokens": 100},
        "responseText": "Answer.",
    },
]


def _session(session_id: str, creation_ms: int, last_ms: int, requests: list[dict]) -> dict:
    """Entrée chatSessions version 3 telle qu'observée sur machine réelle."""
    return {
        "version": 3,
        "sessionId": session_id,
        "creationDate": creation_ms,
        "lastMessageDate": last_ms,
        "initialLocation": "panel",
        "isImported": False,
        "requests": requests,
    }


def _write_workspace(
    root: Path, ws_hash: str, folder_uri: str | None, sessions: list[dict]
) -> Path:
    ws_dir = root / "workspaceStorage" / ws_hash
    chat_dir = ws_dir / "chatSessions"
    chat_dir.mkdir(parents=True)
    for entry in sessions:
        (chat_dir / f"{entry['sessionId']}.json").write_text(
            json.dumps(entry), encoding="utf-8"
        )
    if folder_uri is not None:
        (ws_dir / "workspace.json").write_text(
            json.dumps({"folder": folder_uri}), encoding="utf-8"
        )
    return ws_dir


@pytest.fixture()
def user_dir(tmp_path: Path) -> Path:
    root = tmp_path / "User"
    _write_workspace(
        root,
        "a" * 32,
        "file:///home/user/proj-alpha",
        [
            _session(UUID_EMPTY_ALPHA, MS0 - 3_600_000, MS0 - 3_600_000, []),
            _session(UUID_POPULATED, MS0, MS0 + 120_000, POPULATED_REQUESTS),
            _session(UUID_OLD, MS0 - 86_400_000 * 10, MS0 - 86_400_000 * 5, []),
        ],
    )
    _write_workspace(
        root,
        "b" * 32,
        "file:///home/user/proj-beta",
        [_session(UUID_EMPTY_BETA, MS0 - 60_000, MS0 - 30_000, [])],
    )
    return root


@pytest.fixture()
def provider(user_dir: Path) -> CopilotVSCodeSessionProvider:
    built = build_provider({"type": PROVIDER_TYPE, "user_dir": str(user_dir)}, TelemetryConfig())
    assert built is not None
    return built


# --- factory / registry -------------------------------------------------------


def test_provider_type_discovered_by_registry():
    assert discover_provider_factories().get(PROVIDER_TYPE) is not None


def test_factory_returns_none_when_user_dir_missing(tmp_path: Path):
    missing = tmp_path / "nope"
    assert build_provider({"type": PROVIDER_TYPE, "user_dir": str(missing)}, TelemetryConfig()) is None


def test_factory_uses_default_linux_user_dir_when_key_absent(monkeypatch, tmp_path: Path):
    import weekly_telemetry_aggregator.providers.implementations.copilot_vscode as mod

    monkeypatch.setattr(mod, "_DEFAULT_USER_DIR", str(tmp_path / "Code"))
    assert build_provider({"type": PROVIDER_TYPE}, TelemetryConfig()) is None  # absent → None
    (tmp_path / "Code").mkdir()
    assert build_provider({"type": PROVIDER_TYPE}, TelemetryConfig()) is not None


def test_harness_constants_and_state_vscdb_absent_silent(provider, user_dir: Path):
    assert provider.harness == HARNESS_COPILOT_VSCODE == "copilot-vscode"
    assert PROVIDER_TYPE == HARNESS_COPILOT_VSCODE
    # state.vscdb absent → source secondaire ignorée silencieusement, pas d'erreur.
    assert not (user_dir / "globalStorage" / "state.vscdb").exists()
    assert provider.global_state_key_count is None


# --- énumération / mapping projet ---------------------------------------------


def test_list_sessions_namespaces_ids_and_maps_project_directory(provider):
    sessions = {s.session_id: s for s in provider.list_sessions(0)}
    assert set(sessions) == {
        canonical_session_id(HARNESS_COPILOT_VSCODE, uid)
        for uid in (UUID_EMPTY_ALPHA, UUID_POPULATED, UUID_OLD, UUID_EMPTY_BETA)
    }
    alpha = sessions[canonical_session_id(HARNESS_COPILOT_VSCODE, UUID_POPULATED)]
    assert alpha.directory == "/home/user/proj-alpha"
    beta = sessions[canonical_session_id(HARNESS_COPILOT_VSCODE, UUID_EMPTY_BETA)]
    assert beta.directory == "/home/user/proj-beta"


def test_list_sessions_metadata_fields(provider):
    pop = next(
        s
        for s in provider.list_sessions(0)
        if s.session_id.endswith(UUID_POPULATED)
    )
    assert pop.harness == HARNESS_COPILOT_VSCODE
    assert pop.cost is None  # jamais de coût exploitable hors IDE
    assert pop.model_key == "github-copilot/gpt-4o"  # dérivé des requests peuplées
    assert pop.title == "Fix the flaky test"  # premier tour utilisateur
    assert pop.time_created == tzutc(2026, 7, 30, 10, 0, 0)
    assert pop.time_updated == tzutc(2026, 7, 30, 10, 2, 0)
    assert pop.tokens_input == 1700 and pop.tokens_output == 440
    empty = next(s for s in provider.list_sessions(0) if s.session_id.endswith(UUID_EMPTY_ALPHA))
    assert empty.title is None
    assert empty.model_key == "unknown/unknown"


def test_list_sessions_window_filters_epoch_ms(provider):
    ids = [s.session_id for s in provider.list_sessions(MS0)]
    # Seule la session peuplée a lastMessageDate >= MS0 ; les vides et l'ancienne sont exclues.
    assert ids == [canonical_session_id(HARNESS_COPILOT_VSCODE, UUID_POPULATED)]
    assert UUID_OLD not in [s.session_id for s in provider.list_sessions(0)]


# --- sessions vides : transcripts/agrégats vides sans erreur -------------------


def test_empty_session_yields_clean_empty_results(provider):
    cid = canonical_session_id(HARNESS_COPILOT_VSCODE, UUID_EMPTY_ALPHA)
    assert provider.has_telemetry_rows(cid) is False
    assert provider.session_steps(cid, 0, MS0 * 2) == []
    assert provider.session_tools(cid, 0, MS0 * 2) == ({}, {}, {})
    assert provider.session_user_turns(cid, 0, MS0 * 2) == []
    assert provider.session_context_chars(cid, 0, MS0 * 2) == {
        "file": 0,
        "tool_result": 0,
        "text": 0,
        "reasoning": 0,
    }
    assert provider.session_parts(cid) == []
    assert provider.session_aggregates(cid) is None


def test_unknown_or_absent_session_ids_are_fail_soft(provider):
    ghost = canonical_session_id(HARNESS_COPILOT_VSCODE, "no-such-session")
    assert provider.has_telemetry_rows(ghost) is False
    assert provider.session_parts(ghost) == []
    assert provider.session_aggregates(ghost) is None


# --- session peuplée : mapping requests → Protocol ------------------------------


def test_populated_session_has_telemetry_rows(provider):
    cid = canonical_session_id(HARNESS_COPILOT_VSCODE, UUID_POPULATED)
    assert provider.has_telemetry_rows(cid) is True


def test_session_steps_window_mapping_and_harness_tag(provider):
    cid = canonical_session_id(HARNESS_COPILOT_VSCODE, UUID_POPULATED)
    end_exclusive_second = MS0 + 60_000
    steps = provider.session_steps(cid, MS0, end_exclusive_second)
    assert len(steps) == 1  # req-2 hors fenêtre exclu
    step = steps[0]
    assert step.harness == HARNESS_COPILOT_VSCODE
    assert step.session_id == cid
    assert step.model == "github-copilot/gpt-4o"
    assert step.tokens_input == 1200 and step.tokens_output == 340
    assert step.cost is None
    assert step.timestamp == tzutc(2026, 7, 30, 10, 0, 1)
    both = provider.session_steps(cid, 0, MS0 * 2)
    assert len(both) == 2


def test_session_user_turns_in_window(provider):
    cid = canonical_session_id(HARNESS_COPILOT_VSCODE, UUID_POPULATED)
    turns = provider.session_user_turns(cid, MS0, MS0 + 60_000)
    assert turns == ["Fix the flaky test"]


def test_session_tools_counts_calls_and_arg_chars(provider):
    cid = canonical_session_id(HARNESS_COPILOT_VSCODE, UUID_POPULATED)
    calls, arg_chars, skills = provider.session_tools(cid, 0, MS0 * 2)
    assert calls == {"runTests": 1}
    assert arg_chars["runTests"] == len(json.dumps({"cmd": "pytest -q"}))
    assert skills == {}  # skills_loaded sans objet hors CLI


def test_session_context_chars_categories(provider):
    cid = canonical_session_id(HARNESS_COPILOT_VSCODE, UUID_POPULATED)
    counts = provider.session_context_chars(cid, 0, MS0 * 2)
    assert set(counts) == {"file", "tool_result", "text", "reasoning"}
    assert counts["text"] == len("Done.") + len("Answer.")
    assert counts["file"] == 0 and counts["reasoning"] == 0


def test_session_parts_ordering_and_kinds(provider):
    cid = canonical_session_id(HARNESS_COPILOT_VSCODE, UUID_POPULATED)
    parts = provider.session_parts(cid)
    kinds = [p.kind for p in parts]
    # req-1 : user → tool → assistant ; req-2 : user → assistant
    assert kinds == ["user", "tool", "assistant", "user", "assistant"]
    assert parts[0].text == "Fix the flaky test"
    assert parts[1].tool_name == "runTests"
    assert parts[2].text == "Done."
    assert all(p.cost is None for p in parts)


def test_session_aggregates_always_none_cross_check_cost(provider):
    cid = canonical_session_id(HARNESS_COPILOT_VSCODE, UUID_POPULATED)
    assert provider.session_aggregates(cid) is None


def test_find_session_by_title_exact_match(provider):
    hit = provider.find_session_by_title("Fix the flaky test")
    assert hit is not None
    assert hit.session_id == canonical_session_id(HARNESS_COPILOT_VSCODE, UUID_POPULATED)
    assert provider.find_session_by_title("inconnu") is None


def test_raw_uuid_accepted_as_session_id(provider):
    assert provider.has_telemetry_rows(UUID_POPULATED) is True
    assert provider.session_parts(UUID_EMPTY_ALPHA) == []


# --- robustesse ------------------------------------------------------------------


def test_corrupt_session_file_skipped_with_warning(user_dir: Path):
    broken_ws = user_dir / "workspaceStorage" / ("c" * 32)
    (broken_ws / "chatSessions").mkdir(parents=True)
    (broken_ws / "chatSessions" / "broken.json").write_text("{not json", encoding="utf-8")
    with pytest.warns(UserWarning, match="chatSessions illisible"):
        built = build_provider({"type": PROVIDER_TYPE, "user_dir": str(user_dir)}, TelemetryConfig())
    assert built is not None  # fichier corrompu ignoré, le reste est chargé :
    assert built.find_session_by_title("Fix the flaky test") is not None


def test_missing_workspace_json_leaves_directory_unset(tmp_path: Path):
    ws_dir = tmp_path / "workspaceStorage" / ("d" * 32)
    (ws_dir / "chatSessions").mkdir(parents=True)
    (ws_dir / "chatSessions" / f"{UUID_EMPTY_ALPHA}.json").write_text(
        json.dumps(_session(UUID_EMPTY_ALPHA, MS0, MS0, [])), encoding="utf-8"
    )
    provider = CopilotVSCodeSessionProvider(tmp_path)
    sessions = provider.list_sessions(0)
    assert len(sessions) == 1 and sessions[0].directory is None


def test_check_schema_raises_without_workspace_storage(tmp_path: Path):
    bare = tmp_path / "User"
    bare.mkdir()
    provider = CopilotVSCodeSessionProvider(bare)
    with pytest.raises(SchemaError, match="workspaceStorage introuvable"):
        provider.check_schema()


def test_close_is_idempotent_noop(provider):
    provider.close()
    provider.close()  # aucune ressource persistante → aucun effet, aucune erreur
