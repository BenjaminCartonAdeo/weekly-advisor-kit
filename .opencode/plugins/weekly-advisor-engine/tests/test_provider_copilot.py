"""Provider Copilot VS Code : arborescence workspaceStorage synthétique (tmp_path)."""

from __future__ import annotations

import json
import sqlite3
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
        (chat_dir / f"{entry['sessionId']}.json").write_text(json.dumps(entry), encoding="utf-8")
    if folder_uri is not None:
        (ws_dir / "workspace.json").write_text(json.dumps({"folder": folder_uri}), encoding="utf-8")
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
    assert (
        build_provider({"type": PROVIDER_TYPE, "user_dir": str(missing)}, TelemetryConfig()) is None
    )


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
    pop = next(s for s in provider.list_sessions(0) if s.session_id.endswith(UUID_POPULATED))
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
        built = build_provider(
            {"type": PROVIDER_TYPE, "user_dir": str(user_dir)}, TelemetryConfig()
        )
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


# --- JSONL (prioritaire sur legacy) ---------------------------------------------

UUID_JSONL = "55555555-eeee-4fff-9000-000000000005"
UUID_DUP = "66666666-ffff-4000-9111-000000000006"


def _write_jsonl(chat_dir: Path, session_id: str, lines: list[dict]) -> Path:
    path = chat_dir / f"{session_id}.jsonl"
    path.write_text("\n".join(json.dumps(line) for line in lines), encoding="utf-8")
    return path


def test_jsonl_snapshot_push_ops_and_custom_title(tmp_path: Path):
    ws_dir = tmp_path / "workspaceStorage" / ("e" * 32)
    chat_dir = ws_dir / "chatSessions"
    chat_dir.mkdir(parents=True)
    (ws_dir / "workspace.json").write_text(
        json.dumps({"folder": "file:///home/user/proj-jsonl"}), encoding="utf-8"
    )
    req1 = {
        "requestId": "req-1",
        "timestamp": REQ_IN_WINDOW_MS,
        "message": {"role": "user", "text": "Question JSONL"},
        "modelId": "gpt-4o",
        "responseText": "Réponse JSONL.",
    }
    req2 = {
        "requestId": "req-2",
        "timestamp": REQ_IN_WINDOW_MS + 1_000,
        "message": "Suite ?",
        "modelId": "gpt-4o",
        "responseText": "Suite.",
    }
    _write_jsonl(
        chat_dir,
        UUID_JSONL,
        [
            {
                "version": 1,
                "sessionId": UUID_JSONL,
                "creationDate": MS0,
                "customTitle": "Titre JSONL",
                "requests": [req1],
            },
            {"key": f"chatSessions/{UUID_JSONL}"},  # kind1 : référence ignorée
            {"op": "push", "path": ["requests"], "value": req2},
        ],
    )
    provider = CopilotVSCodeSessionProvider(tmp_path)
    cid = canonical_session_id(HARNESS_COPILOT_VSCODE, UUID_JSONL)
    assert provider.has_telemetry_rows(cid) is True
    session = next(s for s in provider.list_sessions(0) if s.session_id == cid)
    assert session.title == "Titre JSONL"  # customTitle prioritaire
    assert session.directory == "/home/user/proj-jsonl"
    assert provider.session_user_turns(cid, 0, MS0 * 2) == [
        "Question JSONL",
        "Suite ?",
    ]


def test_jsonl_splice_op_and_priority_over_legacy(tmp_path: Path):
    ws_dir = tmp_path / "workspaceStorage" / ("f" * 32)
    chat_dir = ws_dir / "chatSessions"
    chat_dir.mkdir(parents=True)
    (ws_dir / "workspace.json").write_text(
        json.dumps({"folder": "file:///home/user/proj-dup"}), encoding="utf-8"
    )
    req = {
        "requestId": "req-1",
        "timestamp": REQ_IN_WINDOW_MS,
        "message": "Depuis JSONL",
        "modelId": "gpt-4o",
        "responseText": "ok",
    }
    _write_jsonl(
        chat_dir,
        UUID_DUP,
        [
            {"sessionId": UUID_DUP, "creationDate": MS0, "customTitle": "Depuis JSONL"},
            {"op": "splice", "path": ["requests"], "start": 0, "deleteCount": 0, "items": [req]},
        ],
    )
    (chat_dir / f"{UUID_DUP}.json").write_text(
        json.dumps(_session(UUID_DUP, MS0, MS0, POPULATED_REQUESTS)), encoding="utf-8"
    )
    provider = CopilotVSCodeSessionProvider(tmp_path)
    cid = canonical_session_id(HARNESS_COPILOT_VSCODE, UUID_DUP)
    assert provider.find_session_by_title("Depuis JSONL") is not None
    assert provider.session_user_turns(cid, 0, MS0 * 2) == ["Depuis JSONL"]


def test_corrupt_jsonl_skipped_with_warning(tmp_path: Path):
    ws_dir = tmp_path / "workspaceStorage" / ("g" * 32)
    chat_dir = ws_dir / "chatSessions"
    chat_dir.mkdir(parents=True)
    (chat_dir / "broken.jsonl").write_text("{not json\n{{{{", encoding="utf-8")
    with pytest.warns(UserWarning, match="chatSessions illisible"):
        CopilotVSCodeSessionProvider(tmp_path)


# --- index state.vscdb ------------------------------------------------------------


def _write_state_vscdb(user_dir: Path, index_payload: dict) -> Path:
    db_path = user_dir / "globalStorage" / "state.vscdb"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute(
        "INSERT INTO ItemTable (key, value) VALUES (?, ?)",
        ("chat.ChatSessionStore.index", json.dumps(index_payload)),
    )
    conn.commit()
    conn.close()
    return db_path


UUID_INDEXED = "77777777-0000-4000-8222-000000000007"


def test_index_merges_title_and_max_dates(tmp_path: Path):
    ws_dir = tmp_path / "workspaceStorage" / ("h" * 32)
    chat_dir = ws_dir / "chatSessions"
    chat_dir.mkdir(parents=True)
    (ws_dir / "workspace.json").write_text(
        json.dumps({"folder": "file:///home/user/proj-idx"}), encoding="utf-8"
    )
    (chat_dir / f"{UUID_INDEXED}.json").write_text(
        json.dumps(_session(UUID_INDEXED, MS0 - 3_600_000, MS0 - 3_600_000, [])),
        encoding="utf-8",
    )
    _write_state_vscdb(
        tmp_path,
        {
            "entries": {
                UUID_INDEXED: {
                    "title": "Titre index",
                    "lastMessageDate": MS0,
                    "timing": {"lastRequestStarted": MS0 + 5_000},
                    "isEmpty": False,
                    "isExternal": False,
                    "workingDirectory": "/home/user/proj-idx",
                }
            }
        },
    )
    provider = CopilotVSCodeSessionProvider(tmp_path)
    assert provider.global_state_key_count == 1
    session = next(s for s in provider.list_sessions(0))
    assert session.title == "Titre index"  # index.title > repli (pas de texte ici)
    assert session.time_updated == tzutc(2026, 7, 30, 10, 0, 5)  # max(index, timing)


def test_index_only_stub_created_when_not_empty(tmp_path: Path):
    _write_state_vscdb(
        tmp_path,
        {
            "entries": {
                "stub-only": {
                    "title": "Stub index seul",
                    "lastMessageDate": MS0,
                    "isEmpty": False,
                    "workingDirectory": "/home/user/proj-stub",
                },
                "stub-empty": {"title": "Vide", "lastMessageDate": MS0, "isEmpty": True},
            }
        },
    )
    (tmp_path / "workspaceStorage").mkdir(parents=True)
    provider = CopilotVSCodeSessionProvider(tmp_path)
    ids = [s.session_id for s in provider.list_sessions(0)]
    assert canonical_session_id(HARNESS_COPILOT_VSCODE, "stub-only") in ids
    assert canonical_session_id(HARNESS_COPILOT_VSCODE, "stub-empty") not in ids


def test_index_empty_stub_with_transcript_conserved_and_warns(tmp_path: Path):
    ws_dir = tmp_path / "workspaceStorage" / ("i" * 32)
    chat_dir = ws_dir / "chatSessions"
    chat_dir.mkdir(parents=True)
    (ws_dir / "workspace.json").write_text(
        json.dumps({"folder": "file:///home/user/proj-stub"}), encoding="utf-8"
    )
    sid = "88888888-1111-4000-8333-000000000008"
    (chat_dir / f"{sid}.json").write_text(json.dumps(_session(sid, MS0, MS0, [])), encoding="utf-8")
    transcript_dir = ws_dir / "GitHub.copilot-chat" / "transcripts"
    transcript_dir.mkdir(parents=True)
    (transcript_dir / f"{sid}.jsonl").write_text(
        json.dumps({"type": "user.message", "text": "Question transcript", "timestamp": MS0 + 1_000}),
        encoding="utf-8",
    )
    _write_state_vscdb(tmp_path, {"entries": {sid: {"isEmpty": True, "lastMessageDate": MS0}}})
    with pytest.warns(UserWarning, match="vscode-empty-stub"):
        provider = CopilotVSCodeSessionProvider(tmp_path)
    cid = canonical_session_id(HARNESS_COPILOT_VSCODE, sid)
    assert provider.has_telemetry_rows(cid) is True  # conservée, jamais exclue
    assert provider.session_user_turns(cid, 0, MS0 * 2) == ["Question transcript"]


def test_archived_session_conserved_and_tagged(tmp_path: Path):
    ws_dir = tmp_path / "workspaceStorage" / ("j" * 32)
    chat_dir = ws_dir / "chatSessions"
    chat_dir.mkdir(parents=True)
    (ws_dir / "workspace.json").write_text(
        json.dumps({"folder": "file:///home/user/proj-arch"}), encoding="utf-8"
    )
    sid = "99999999-2222-4000-8444-000000000009"
    (chat_dir / f"{sid}.json").write_text(
        json.dumps(_session(sid, MS0, MS0, POPULATED_REQUESTS)), encoding="utf-8"
    )
    (ws_dir / "agentSessions.state.cache").write_text(
        json.dumps({"archivedSessions": [sid]}), encoding="utf-8"
    )
    _write_state_vscdb(tmp_path, {"entries": {sid: {"isEmpty": False, "lastMessageDate": MS0}}})
    provider = CopilotVSCodeSessionProvider(tmp_path)
    session = next(s for s in provider.list_sessions(0))
    assert "[archived]" in (session.title or "")
    assert provider.has_telemetry_rows(canonical_session_id(HARNESS_COPILOT_VSCODE, sid)) is True


# --- transcripts de repli -----------------------------------------------------------


def test_transcript_fallback_tools_turns_parts_and_files(tmp_path: Path):
    ws_dir = tmp_path / "workspaceStorage" / ("k" * 32)
    chat_dir = ws_dir / "chatSessions"
    chat_dir.mkdir(parents=True)
    (ws_dir / "workspace.json").write_text(
        json.dumps({"folder": "file:///home/user/proj-tr"}), encoding="utf-8"
    )
    sid = "aaaaaaaa-3333-4000-8555-000000000010"
    (chat_dir / f"{sid}.json").write_text(json.dumps(_session(sid, MS0, MS0, [])), encoding="utf-8")
    transcript_dir = ws_dir / "GitHub.copilot-chat" / "transcripts"
    transcript_dir.mkdir(parents=True)
    (transcript_dir / f"{sid}.jsonl").write_text(
        "\n".join(
            json.dumps(line)
            for line in [
                {"type": "user.message", "text": "Hello transcript", "timestamp": MS0 + 1_000},
                {
                    "type": "tool.execution_start",
                    "toolName": "readFile",
                    "input": {"path": "a.py"},
                    "timestamp": MS0 + 2_000,
                },
                {
                    "type": "tool.execution_complete",
                    "toolName": "readFile",
                    "result": "contenu",
                    "timestamp": MS0 + 3_000,
                },
                {"type": "assistant.message", "text": "Salut", "timestamp": MS0 + 4_000},
            ]
        ),
        encoding="utf-8",
    )
    editing_dir = ws_dir / "chatEditingSessions" / sid
    editing_dir.mkdir(parents=True)
    (editing_dir / "state.json").write_text(
        json.dumps({"editors": [{"uri": "file:///home/user/proj-tr/a.py"}]}), encoding="utf-8"
    )
    provider = CopilotVSCodeSessionProvider(tmp_path)
    cid = canonical_session_id(HARNESS_COPILOT_VSCODE, sid)
    assert provider.has_telemetry_rows(cid) is True
    assert provider.session_user_turns(cid, 0, MS0 * 2) == ["Hello transcript"]
    calls, _arg_chars, skills = provider.session_tools(cid, 0, MS0 * 2)
    assert calls == {"readFile": 1}
    assert skills == {}
    counts = provider.session_context_chars(cid, 0, MS0 * 2)
    assert set(counts) == {"file", "tool_result", "text", "reasoning"}
    assert counts["text"] == len("Salut")
    assert counts["tool_result"] == len("contenu")
    assert counts["file"] > 0
    parts = provider.session_parts(cid)
    assert [p.kind for p in parts] == ["user", "tool", "assistant", "file"]
    assert parts[1].tool_name == "readFile"
    assert parts[1].tool_output == "contenu"


def test_empty_window_chat_sessions_included(tmp_path: Path):
    empty_dir = tmp_path / "globalStorage" / "emptyWindowChatSessions"
    empty_dir.mkdir(parents=True)
    (empty_dir / "ew1.json").write_text(
        json.dumps(_session("ew1", MS0, MS0, [])), encoding="utf-8"
    )
    (tmp_path / "workspaceStorage").mkdir(parents=True)
    provider = CopilotVSCodeSessionProvider(tmp_path)
    sessions = {s.session_id: s for s in provider.list_sessions(0)}
    assert canonical_session_id(HARNESS_COPILOT_VSCODE, "ew1") in sessions
    assert sessions[canonical_session_id(HARNESS_COPILOT_VSCODE, "ew1")].directory is None


# --- orphelins ------------------------------------------------------------------------


def test_orphan_sessions_warn_once_and_leave_directory_unset(tmp_path: Path):
    import warnings as _warnings

    for digest in ("m" * 32, "n" * 32):
        chat_dir = tmp_path / "workspaceStorage" / digest / "chatSessions"
        chat_dir.mkdir(parents=True)
        (chat_dir / f"orphan-{digest[:4]}.json").write_text(
            json.dumps(_session(f"orphan-{digest[:4]}", MS0, MS0, [])), encoding="utf-8"
        )
    with _warnings.catch_warnings(record=True) as caught:
        _warnings.simplefilter("always")
        provider = CopilotVSCodeSessionProvider(tmp_path)
    orphan_warnings = [w for w in caught if "vscode-orphan" in str(w.message)]
    assert len(orphan_warnings) == 1  # récap unique, pas par session
    for session in provider.list_sessions(0):
        assert session.directory is None


def test_include_orphans_false_excludes_workspace_less_sessions(tmp_path: Path):
    chat_dir = tmp_path / "workspaceStorage" / ("o" * 32) / "chatSessions"
    chat_dir.mkdir(parents=True)
    (chat_dir / "orphan.json").write_text(
        json.dumps(_session("orphan", MS0, MS0, [])), encoding="utf-8"
    )
    built = build_provider(
        {"type": PROVIDER_TYPE, "user_dir": str(tmp_path), "include_orphans": False},
        TelemetryConfig(),
    )
    assert built is not None
    assert built.list_sessions(0) == []


# --- user_dirs multi-plateforme ----------------------------------------------------------


def test_build_provider_accepts_user_dirs_list(tmp_path: Path):
    roots = []
    for digest, name in (("p" * 32, "r1"), ("q" * 32, "r2")):
        root = tmp_path / name
        ws_dir = root / "workspaceStorage" / digest
        chat_dir = ws_dir / "chatSessions"
        chat_dir.mkdir(parents=True)
        (ws_dir / "workspace.json").write_text(
            json.dumps({"folder": f"file:///home/user/{name}"}), encoding="utf-8"
        )
        sid = f"sid-{name}"
        (chat_dir / f"{sid}.json").write_text(
            json.dumps(_session(sid, MS0, MS0, [])), encoding="utf-8"
        )
        roots.append(root)
    built = build_provider(
        {"type": PROVIDER_TYPE, "user_dirs": [str(roots[0]), str(roots[1])]}, TelemetryConfig()
    )
    assert built is not None
    assert built.user_dir == roots[0]
    assert {s.directory for s in built.list_sessions(0)} == {"/home/user/r1", "/home/user/r2"}


def test_build_provider_uses_vscode_user_dir_env(monkeypatch, tmp_path: Path):
    ws_dir = tmp_path / "workspaceStorage" / ("r" * 32)
    chat_dir = ws_dir / "chatSessions"
    chat_dir.mkdir(parents=True)
    (chat_dir / "env-sid.json").write_text(
        json.dumps(_session("env-sid", MS0, MS0, [])), encoding="utf-8"
    )
    monkeypatch.setenv("VSCODE_USER_DIR", str(tmp_path))
    built = build_provider({"type": PROVIDER_TYPE}, TelemetryConfig())
    assert built is not None
    assert canonical_session_id(HARNESS_COPILOT_VSCODE, "env-sid") in [
        s.session_id for s in built.list_sessions(0)
    ]


def test_candidate_user_dirs_deduplicated():
    import sys as _sys

    import weekly_telemetry_aggregator.providers.implementations.copilot_vscode as mod

    candidates = mod._candidate_user_dirs()
    assert len(candidates) == len({str(p) for p in candidates})
    if _sys.platform != "win32":
        assert str(mod.Path(mod._DEFAULT_USER_DIR).expanduser()) in [str(p) for p in candidates]

