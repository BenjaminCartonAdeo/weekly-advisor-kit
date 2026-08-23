"""Suite de conformance transversale du contrat `SessionProvider` (#4/#15 audit).

Chaque implémentation constructible en local (opencode, claude-code,
copilot-vscode) et le double de test `FakeSessionProvider` subissent les MÊMES
assertions sémantiques : jamais de None là où le protocol promet une liste,
ids canoniques préfixés ``"<harness>:"``, close() idempotent, check_schema qui
détecte une source absente, factory fail-soft → None, passage du validateur
structurel. Un provider dérivant du contrat échoue ICI avant de contaminer
l'agrégation.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.helpers import FakeSessionProvider, fake_meta, seed_v1_file
from weekly_telemetry_aggregator.config import TelemetryConfig
from weekly_telemetry_aggregator.providers import build_providers
from weekly_telemetry_aggregator.providers.base import HarnessSession, validate_provider
from weekly_telemetry_aggregator.providers.implementations.claude_code import (
    ClaudeCodeSessionProvider,
)
from weekly_telemetry_aggregator.providers.implementations.claude_code import (
    build_provider as build_claude_code,
)
from weekly_telemetry_aggregator.providers.implementations.copilot_vscode import (
    CopilotVSCodeSessionProvider,
)
from weekly_telemetry_aggregator.providers.implementations.copilot_vscode import (
    build_provider as build_copilot_vscode,
)
from weekly_telemetry_aggregator.providers.implementations.opencode import (
    OpenCodeSessionProvider,
)
from weekly_telemetry_aggregator.providers.implementations.opencode import (
    build_provider as build_opencode,
)
from weekly_telemetry_aggregator.sqlite_reader import SchemaError

T0 = datetime(2026, 7, 30, 10, 0, 0, tzinfo=UTC)
MS0 = int(T0.timestamp() * 1000)
TITLE = "Titre Conforme"


# --- builders : un provider peuplé par implémentation (source = tmp_path) -----


def _opencode_provider(tmp_path: Path) -> OpenCodeSessionProvider:
    db = tmp_path / "opencode.db"
    seed_v1_file(db, [{"id": "s-op", "title": TITLE, "start": T0, "updated": T0}]).close()
    cfg = TelemetryConfig()
    cfg.opencode_db_path = str(db)
    built = build_opencode({"type": "opencode"}, cfg)
    assert built is not None
    return built


def _claude_code_provider(tmp_path: Path) -> ClaudeCodeSessionProvider:
    root = tmp_path / "projects"
    entries = [
        {
            "type": "user",
            "uuid": "u1",
            "sessionId": "s-cc",
            "timestamp": T0.isoformat().replace("+00:00", "Z"),
            "cwd": "/home/user/proj",
            "message": {"role": "user", "content": TITLE},
        },
        {
            "type": "assistant",
            "uuid": "a1",
            "sessionId": "s-cc",
            "timestamp": T0.isoformat().replace("+00:00", "Z"),
            "cwd": "/home/user/proj",
            "message": {
                "role": "assistant",
                "model": "claude-x",
                "content": [{"type": "text", "text": "ok"}],
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        },
    ]
    d = root / "-home-user-proj"
    d.mkdir(parents=True)
    (d / "s-cc.jsonl").write_text("\n".join(json.dumps(e) for e in entries), encoding="utf-8")
    built = build_claude_code({"type": "claude-code", "projects_dir": str(root)}, TelemetryConfig())
    assert built is not None
    return built


def _copilot_vscode_provider(tmp_path: Path) -> CopilotVSCodeSessionProvider:
    root = tmp_path / "User"
    entry = {
        "version": 3,
        "sessionId": "s-cp",
        "creationDate": MS0,
        "lastMessageDate": MS0 + 1_000,
        "requests": [
            {
                "requestId": "req-1",
                "timestamp": MS0 + 500,
                "message": {"role": "user", "text": TITLE},
                "modelId": "gpt-4o",
                "tokenCounts": {"inputTokens": 10, "outputTokens": 5},
                "responseText": "ok",
            }
        ],
    }
    chat_dir = root / "workspaceStorage" / ("c" * 32) / "chatSessions"
    chat_dir.mkdir(parents=True)
    (chat_dir / "s-cp.json").write_text(json.dumps(entry), encoding="utf-8")
    built = build_copilot_vscode(
        {"type": "copilot-vscode", "user_dir": str(root)}, TelemetryConfig()
    )
    assert built is not None
    return built


def _fake_provider(tmp_path: Path) -> FakeSessionProvider:
    del tmp_path  # source en mémoire — signature uniforme avec les autres builders
    return FakeSessionProvider("fake-harness", [fake_meta("fake-harness", "s-fk", title=TITLE)])


_BUILDERS = {
    "opencode": _opencode_provider,
    "claude-code": _claude_code_provider,
    "copilot-vscode": _copilot_vscode_provider,
    "fake": _fake_provider,
}


@pytest.fixture(params=list(_BUILDERS))
def provider(request: pytest.FixtureRequest, tmp_path: Path):
    """Un provider conforme par implémentation constructible en local."""
    return _BUILDERS[request.param](tmp_path)


# --- conformance sémantique commune -------------------------------------------


def test_validate_structurelle_ok(provider):
    assert validate_provider(provider) == []


def test_list_sessions_retourne_liste_ids_prefixes(provider):
    sessions = provider.list_sessions(0)
    assert isinstance(sessions, list)
    prefix = f"{provider.harness}:"
    for session in sessions:
        assert isinstance(session, HarnessSession)
        assert session.session_id.startswith(prefix)
        assert session.harness == provider.harness


def test_accessors_fenetrees_formes_contractuelles(provider):
    sid = provider.list_sessions(0)[0].session_id
    start_ms, end_ms = MS0 - 3_600_000, MS0 + 3_600_000
    steps = provider.session_steps(sid, start_ms, end_ms)
    assert isinstance(steps, list)
    tools = provider.session_tools(sid, start_ms, end_ms)
    assert isinstance(tools, tuple) and len(tools) == 3
    assert all(isinstance(part, dict) for part in tools)
    turns = provider.session_user_turns(sid, start_ms, end_ms)
    assert isinstance(turns, list)
    ctx = provider.session_context_chars(sid, start_ms, end_ms)
    assert isinstance(ctx, dict)
    parts = provider.session_parts(sid)
    assert isinstance(parts, list)


def test_accesseurs_scalaires_formes_contractuelles(provider):
    sid = provider.list_sessions(0)[0].session_id
    telemetry = provider.has_telemetry_rows(sid)
    assert isinstance(telemetry, bool)
    aggregates = provider.session_aggregates(sid)
    assert aggregates is None or isinstance(aggregates, dict)
    found = provider.find_session_by_title(TITLE)
    if found is not None:
        assert isinstance(found, HarnessSession)
        assert found.session_id.startswith(f"{provider.harness}:")
    assert provider.find_session_by_title("titre-absent-improbable") is None


def test_close_idempotent_deux_fois(provider):
    provider.close()
    provider.close()  # aucun crash au second appel


# --- check_schema détecte une source absente/degradée -------------------------


def test_check_schema_leve_schemaerror_source_absente(provider, tmp_path: Path):
    if isinstance(provider, FakeSessionProvider):
        pytest.skip("source en mémoire : ne peut pas disparaître")
    if isinstance(provider, OpenCodeSessionProvider):
        # Dégrader la base sous-jacente : plus aucune table de sessions.
        wreck = sqlite3.connect(str(provider.db_path))
        wreck.executescript("DROP TABLE IF EXISTS session_v2; DROP TABLE IF EXISTS session;")
        wreck.commit()
        wreck.close()
    elif isinstance(provider, CopilotVSCodeSessionProvider):
        shutil.rmtree(provider.user_dir)
    else:
        shutil.rmtree(provider.db_path)
    with pytest.raises(SchemaError):
        provider.check_schema()


# --- factory fail-soft → None si la source est absente ------------------------


def test_factory_none_si_source_absente(tmp_path: Path):
    missing = tmp_path / "absent"
    assert (
        build_claude_code({"type": "claude-code", "projects_dir": str(missing)}, TelemetryConfig())
        is None
    )
    assert (
        build_copilot_vscode(
            {"type": "copilot-vscode", "user_dir": str(missing)}, TelemetryConfig()
        )
        is None
    )
    cfg = TelemetryConfig()
    cfg.opencode_db_path = str(missing)
    assert build_opencode({"type": "opencode"}, cfg) is None


# --- garde-fou registry : provider non conforme rejeté ------------------------


class _NonConforme:
    """Dérive du contrat : harness vide, méthodes manquantes, une non appelable."""

    harness = ""
    list_sessions = "pas-une-methode"

    def close(self) -> None: ...


def test_registry_rejette_provider_non_conforme():
    cfg = TelemetryConfig()
    cfg.session_sources = [{"type": "broken"}]
    with pytest.warns(UserWarning, match="non conforme"):
        providers = build_providers(cfg, factories={"broken": lambda src, c: _NonConforme()})
    assert providers == []


def test_registry_accepte_provider_conforme():
    cfg = TelemetryConfig()
    cfg.session_sources = [{"type": "fake"}]
    factories = {"fake": lambda src, c: FakeSessionProvider("fake", [fake_meta("fake", "s1")])}
    assert [p.harness for p in build_providers(cfg, factories=factories)] == ["fake"]


# --- validate_provider : unitaire ---------------------------------------------


def test_validate_provider_signale_ecarts_precis():
    issues = validate_provider(_NonConforme())
    assert any("'harness'" in i for i in issues)
    assert any("check_schema" in i and "manquante" in i for i in issues)
    assert any("list_sessions" in i and "non appelable" in i for i in issues)
    assert not any("close()" in i for i in issues)  # présente et appelable

    class _SansRien:
        pass

    # 1 attribut harness + 11 méthodes du contrat.
    assert len(validate_provider(_SansRien())) == 12
    assert validate_provider(FakeSessionProvider("x", [])) == []
