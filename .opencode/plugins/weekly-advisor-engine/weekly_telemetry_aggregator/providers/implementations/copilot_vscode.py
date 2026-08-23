"""Provider Copilot (VS Code) — chatSessions du workspaceStorage + state.vscdb optionnel.

Sources primaires : ``<user_dir>/workspaceStorage/<hash>/chatSessions/<uuid>.json``
(schéma version 3 : ``sessionId``, ``creationDate``/``lastMessageDate`` en epoch
ms, ``requests``) avec le sibling ``workspace.json`` (``{"folder":
"file:///chemin/projet"}``) qui fiabilise le mapping session → projet.

Sur beaucoup de machines les fichiers portent ``requests: []`` (sessions
vides) : le provider énumère quand même les métadonnées et retourne des
transcripts vides proprement (`has_telemetry_rows` faux, parts vides).

Source secondaire optionnelle : ``<user_dir>/globalStorage/state.vscdb`` —
comptage readonly des clés ``%interactive.session%`` / ``%chat%`` de la table
``ItemTable``, purement informatif ; absente/vide/illisible → ignorée
silencieusement, jamais bloquante.

HORS PÉRIMÈTRE : JetBrains/Nitrite (``~/.config/github-copilot/**``) et tout
fichier d'authentification Copilot (tokens OAuth) ne sont JAMAIS lus ni listés.
"""

from __future__ import annotations

import json
import sqlite3
import warnings
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import unquote, urlparse

from ...models import StepFinish, canonical_session_id
from ...sqlite_reader import PartRecord, SchemaError
from ..base import HarnessSession

if TYPE_CHECKING:
    from ...config import TelemetryConfig

#: Identifiant de harnais (préfixe des ids canoniques).
HARNESS_COPILOT_VSCODE = "copilot-vscode"

PROVIDER_TYPE = HARNESS_COPILOT_VSCODE

#: Répertoire utilisateur VS Code par défaut (Linux) si `user_dir` non fourni.
_DEFAULT_USER_DIR = "~/.config/Code/User"

#: Clés de contexte alignées sur l'adaptateur OpenCode existant.
_CONTEXT_KEYS = ("file", "tool_result", "text", "reasoning")


@dataclass(slots=True)
class _ChatSession:
    """Vue plate d'un fichier chatSessions/<uuid>.json (déjà normalisé)."""

    session_id: str
    creation_ms: int | None = None
    last_ms: int | None = None
    directory: str | None = None
    title: str | None = None
    requests: list[dict] = field(default_factory=list)

    @property
    def active_ms(self) -> int:
        """Horodatage d'activité retenu pour le fenêtrage `list_sessions`."""
        return self.last_ms or self.creation_ms or 0


def _epoch_ms_to_dt(ms: int | None) -> datetime | None:
    return datetime.fromtimestamp(ms / 1000, tz=UTC) if ms else None


def _folder_uri_to_path(uri: object) -> str | None:
    """Convertit ``file:///chemin`` en chemin ; autre schéma renvoyé tel quel."""
    if not isinstance(uri, str) or not uri.strip():
        return None
    parsed = urlparse(uri)
    if parsed.scheme == "file":
        return unquote(parsed.path)
    return uri


def _project_folder(workspace_json: Path) -> str | None:
    """Lit le sibling ``workspace.json`` (``{"folder": "file://..."}``) ; fail-soft."""
    try:
        raw = json.loads(workspace_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(raw, dict):
        return _folder_uri_to_path(raw.get("folder"))
    return None


def _message_text(message: object) -> str:
    """Texte d'un message utilisateur : chaîne ou dict structuré, tolérant."""
    if isinstance(message, str):
        return message
    if isinstance(message, dict):
        for key in ("text", "value", "message"):
            value = message.get(key)
            if isinstance(value, str):
                return value
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and isinstance(part.get("text"), str)
            )
    return ""


def _response_text(request: dict) -> str:
    """Texte de réponse assistant : ``responseText`` ou parties de ``response``."""
    direct = request.get("responseText")
    if isinstance(direct, str):
        return direct
    response = request.get("response")
    if isinstance(response, str):
        return response
    if isinstance(response, list):
        chunks = [
            part.get("text", "")
            for part in response
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        ]
        return "\n".join(chunk for chunk in chunks if chunk)
    return ""


def _request_tokens(request: dict) -> tuple[float, float, float, float, float]:
    """Tokens (in, out, reasoning, cache_read, cache_write) d'une requête, 0 par défaut."""
    counts = request.get("tokenCounts")
    if not isinstance(counts, dict):
        counts = {
            key: value
            for key, value in request.items()
            if key in ("inputTokens", "outputTokens", "tokenCount") and isinstance(value, (int, float))
        }
    total = counts.get("tokenCount")

    def _num(*keys: str) -> float:
        for key in keys:
            value = counts.get(key)
            if isinstance(value, (int, float)):
                return float(value)
        return 0.0

    input_tok = _num("inputTokens", "input", "promptTokens")
    output_tok = _num("outputTokens", "output", "completionTokens")
    reasoning_tok = _num("reasoningTokens", "reasoning")
    cache_read = _num("cacheReadTokens", "cachedTokens", "cachedInputTokens")
    cache_write = _num("cacheWriteTokens")
    if total and not (input_tok or output_tok):
        return total, 0.0, 0.0, cache_read, cache_write
    return input_tok, output_tok, reasoning_tok, cache_read, cache_write


def _request_model_key(request: dict) -> str:
    model = str(request.get("modelId") or request.get("model") or "").strip()
    return f"github-copilot/{model}" if model else "unknown/unknown"


def _request_tools(request: dict) -> list[dict]:
    tools = request.get("toolInvocations")
    if not isinstance(tools, list):
        return []
    return [tool for tool in tools if isinstance(tool, dict)]


def _tool_field(tool: dict, *keys: str) -> str:
    for key in keys:
        value = tool.get(key)
        if isinstance(value, str):
            return value
        if isinstance(value, (dict, list)):
            return json.dumps(value)
    return ""


def _request_ts_ms(request: dict, fallback_ms: int | None) -> int | None:
    ts = request.get("timestamp")
    if isinstance(ts, (int, float)) and ts > 0:
        return int(ts)
    return fallback_ms


class CopilotVSCodeSessionProvider:
    """Sessions Copilot de VS Code, ids canoniques ``copilot-vscode:<sessionId>``."""

    harness = HARNESS_COPILOT_VSCODE

    def __init__(self, user_dir: Path) -> None:
        self.user_dir = user_dir
        self._sessions: dict[str, _ChatSession] = {}
        self._load_workspace_sessions()
        #: Comptage informatif des clés chat de state.vscdb ; None = source absente.
        self.global_state_key_count: int | None = self._count_global_state_chat_keys()

    # --- chargement ---------------------------------------------------------

    def _load_workspace_sessions(self) -> None:
        ws_root = self.user_dir / "workspaceStorage"
        if not ws_root.is_dir():
            return
        for ws_dir in sorted(p for p in ws_root.iterdir() if p.is_dir()):
            directory = _project_folder(ws_dir / "workspace.json")
            chat_dir = ws_dir / "chatSessions"
            if not chat_dir.is_dir():
                continue
            for session_file in sorted(chat_dir.glob("*.json")):
                try:
                    raw = json.loads(session_file.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    warnings.warn(
                        f"chatSessions illisible, ignoré : {session_file}", stacklevel=2
                    )
                    continue
                if not isinstance(raw, dict):
                    continue
                raw_requests = raw.get("requests")
                requests = [r for r in raw_requests if isinstance(r, dict)] if isinstance(
                    raw_requests, list
                ) else []
                session_id = str(raw.get("sessionId") or session_file.stem)
                first_user_text = next(
                    (
                        text
                        for text in (
                            _message_text(req.get("message")).strip() for req in requests
                        )
                        if text
                    ),
                    "",
                )
                self._sessions[session_id] = _ChatSession(
                    session_id=session_id,
                    creation_ms=_positive_ms(raw.get("creationDate")),
                    last_ms=_positive_ms(raw.get("lastMessageDate")),
                    directory=directory,
                    title=(first_user_text[:80] or None),
                    requests=requests,
                )

    def _count_global_state_chat_keys(self) -> int | None:
        """Compte readonly des clés chat/interactive.session ; None si indisponible."""
        db_path = self.user_dir / "globalStorage" / "state.vscdb"
        if not db_path.is_file():
            return None
        try:
            conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
        except sqlite3.Error:
            return None
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM ItemTable "
                "WHERE key LIKE '%interactive.session%' OR key LIKE '%chat%'"
            ).fetchone()
        except sqlite3.Error:
            return None  # table absente / base vide : source secondaire ignorée
        finally:
            conn.close()
        return int(row[0]) if row else None

    # --- helpers ------------------------------------------------------------

    def _unwrap(self, session_id: str) -> str:
        prefix = f"{self.harness}:"
        return session_id[len(prefix) :] if session_id.startswith(prefix) else session_id

    def _get(self, session_id: str) -> _ChatSession | None:
        return self._sessions.get(self._unwrap(session_id))

    def _to_harness_session(self, entry: _ChatSession) -> HarnessSession:
        tokens = [_request_tokens(req) for req in entry.requests]
        model_key = next(
            (_request_model_key(req) for req in entry.requests if _request_model_key(req) != "unknown/unknown"),
            "unknown/unknown",
        )
        return HarnessSession(
            harness=self.harness,
            session_id=canonical_session_id(self.harness, entry.session_id),
            title=entry.title,
            parent_id=None,
            model_key=model_key,
            agent=None,
            directory=entry.directory,
            cost=None,  # pas de coût exploitable hors IDE
            tokens_input=sum(t[0] for t in tokens),
            tokens_output=sum(t[1] for t in tokens),
            tokens_reasoning=sum(t[2] for t in tokens),
            tokens_cache_read=sum(t[3] for t in tokens),
            tokens_cache_write=sum(t[4] for t in tokens),
            time_created=_epoch_ms_to_dt(entry.creation_ms),
            time_updated=_epoch_ms_to_dt(entry.last_ms),
        )

    def _windowed_requests(
        self, entry: _ChatSession, start_ms: int, end_ms: int
    ) -> list[tuple[dict, int]]:
        out: list[tuple[dict, int]] = []
        for req in entry.requests:
            ts_ms = _request_ts_ms(req, entry.last_ms or entry.creation_ms)
            if ts_ms is not None and start_ms <= ts_ms <= end_ms:
                out.append((req, ts_ms))
        return out

    # --- Protocol SessionProvider -------------------------------------------

    def check_schema(self) -> None:
        ws_root = self.user_dir / "workspaceStorage"
        if not ws_root.is_dir():
            raise SchemaError(f"workspaceStorage introuvable sous {self.user_dir}")

    def list_sessions(self, since_ms: int) -> list[HarnessSession]:
        sessions = [
            self._to_harness_session(entry)
            for entry in self._sessions.values()
            if entry.active_ms >= since_ms
        ]
        return sorted(
            sessions,
            key=lambda s: s.time_updated or s.time_created or datetime.min.replace(tzinfo=UTC),
        )

    def has_telemetry_rows(self, session_id: str) -> bool:
        entry = self._get(session_id)
        return bool(entry and entry.requests)

    def session_steps(
        self, session_id: str, start_ms: int, end_ms: int
    ) -> list[StepFinish]:
        entry = self._get(session_id)
        if entry is None:
            return []
        steps: list[StepFinish] = []
        for req, ts_ms in self._windowed_requests(entry, start_ms, end_ms):
            tin, tout, treas, tread, twrite = _request_tokens(req)
            steps.append(
                StepFinish(
                    session_id=canonical_session_id(self.harness, entry.session_id),
                    timestamp=datetime.fromtimestamp(ts_ms / 1000, tz=UTC),
                    model=_request_model_key(req),
                    tokens_input=tin,
                    tokens_output=tout,
                    tokens_reasoning=treas,
                    tokens_cache_read=tread,
                    tokens_cache_write=twrite,
                    cost=None,  # VS Code ne persiste pas de coût par requête
                    harness=self.harness,
                )
            )
        return steps

    def session_tools(
        self, session_id: str, start_ms: int, end_ms: int
    ) -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
        entry = self._get(session_id)
        if entry is None:
            return {}, {}, {}
        tool_calls: dict[str, int] = {}
        tool_arg_chars: dict[str, int] = {}
        for req, _ts_ms in self._windowed_requests(entry, start_ms, end_ms):
            for tool in _request_tools(req):
                name = str(tool.get("name") or tool.get("toolName") or "unknown")
                arg_chars = len(_tool_field(tool, "input", "parameters", "arguments"))
                tool_calls[name] = tool_calls.get(name, 0) + 1
                tool_arg_chars[name] = tool_arg_chars.get(name, 0) + arg_chars
        return tool_calls, tool_arg_chars, {}  # skills_loaded : sans objet hors CLI

    def session_user_turns(self, session_id: str, start_ms: int, end_ms: int) -> list[str]:
        entry = self._get(session_id)
        if entry is None:
            return []
        turns = []
        for req, _ts_ms in self._windowed_requests(entry, start_ms, end_ms):
            text = _message_text(req.get("message"))
            if text.strip():
                turns.append(text)
        return turns

    def session_context_chars(
        self, session_id: str, start_ms: int, end_ms: int
    ) -> dict[str, int]:
        entry = self._get(session_id)
        if entry is None:
            return dict.fromkeys(_CONTEXT_KEYS, 0)
        counts = dict.fromkeys(_CONTEXT_KEYS, 0)
        for req, _ts_ms in self._windowed_requests(entry, start_ms, end_ms):
            counts["text"] += len(_response_text(req))
            for tool in _request_tools(req):
                counts["tool_result"] += len(_tool_field(tool, "result", "output"))
        return counts

    def session_aggregates(self, session_id: str) -> dict | None:
        self._get(session_id)  # tolérance id brut/canonique
        return None  # VS Code ne persiste aucun agrégat vie-telle-enregistrée

    def session_parts(self, session_id: str) -> list[PartRecord]:
        entry = self._get(session_id)
        if entry is None:
            return []
        parts: list[PartRecord] = []
        for req in entry.requests:
            ts_ms = _request_ts_ms(req, entry.last_ms or entry.creation_ms) or 0
            ts = datetime.fromtimestamp(ts_ms / 1000, tz=UTC) if ts_ms else None
            user_text = _message_text(req.get("message"))
            if user_text.strip() and ts is not None:
                parts.append(PartRecord(ts=ts, kind="user", text=user_text))
            for tool in _request_tools(req):
                if ts is None:
                    break
                name = str(tool.get("name") or tool.get("toolName") or "unknown")
                parts.append(
                    PartRecord(
                        ts=ts,
                        kind="tool",
                        tool_name=name,
                        tool_input=_tool_field(tool, "input", "parameters", "arguments"),
                        tool_output=_tool_field(tool, "result", "output") or None,
                    )
                )
            response = _response_text(req)
            if response.strip() and ts is not None:
                parts.append(PartRecord(ts=ts, kind="assistant", text=response))
        return parts

    def find_session_by_title(self, title: str) -> HarnessSession | None:
        for entry in self._sessions.values():
            if entry.title == title:
                return self._to_harness_session(entry)
        return None

    def close(self) -> None:
        """Aucune ressource persistante (lectures ponctuelles) — no-op idempotent."""


def _positive_ms(value: object) -> int | None:
    return int(value) if isinstance(value, (int, float)) and value > 0 else None


def build_provider(source_cfg: dict, _cfg: TelemetryConfig) -> CopilotVSCodeSessionProvider | None:
    """Factory registry : None si `user_dir` absent (source indisponible propre)."""
    raw_user_dir = source_cfg.get("user_dir") if isinstance(source_cfg, dict) else None
    user_dir = Path(
        raw_user_dir if isinstance(raw_user_dir, str) and raw_user_dir else _DEFAULT_USER_DIR
    ).expanduser()
    if not user_dir.is_dir():
        return None
    return CopilotVSCodeSessionProvider(user_dir)
