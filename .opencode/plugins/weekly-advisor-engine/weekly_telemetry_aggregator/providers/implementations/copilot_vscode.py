"""Provider Copilot (VS Code) — chatSessions JSONL/JSON + index + transcripts (voir spec).

Fail-soft partout ; tokens=0/cost=None ; détails dans la spec.
HORS PÉRIMÈTRE : JetBrains/Nitrite et auth Copilot jamais lus.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import warnings
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import unquote, urlparse

from ...models import StepFinish, canonical_session_id
from ...sqlite_reader import PartRecord, SchemaError, open_database
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

#: Clé d'index des sessions dans state.vscdb (table ItemTable).
_INDEX_KEY = "chat.ChatSessionStore.index"

#: Troncature des textes exposés dans les parts (parité sqlite_reader).
_PART_TRUNCATE = 2000


@dataclass(slots=True)
class _ChatSession:
    """Vue plate d'une session chat (legacy .json ou .jsonl + index + transcripts)."""

    session_id: str
    creation_ms: int | None = None
    last_ms: int | None = None
    directory: str | None = None
    title: str | None = None
    requests: list[dict] = field(default_factory=list)
    custom_title: str | None = None
    archived: bool = False
    empty_stub: bool = False
    transcript_user: list[tuple[str, int | None]] = field(default_factory=list)
    transcript_assistant: list[tuple[str, int | None]] = field(default_factory=list)
    transcript_tools: list[tuple[dict, int | None]] = field(default_factory=list)
    files: list[str] = field(default_factory=list)

    @property
    def active_ms(self) -> int:
        """Horodatage d'activité retenu pour le fenêtrage `list_sessions`."""
        return self.last_ms or self.creation_ms or 0

    def has_transcript(self) -> bool:
        """Transcript de repli non-vide (tours ou outils)."""
        return bool(self.transcript_user or self.transcript_assistant or self.transcript_tools)


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
            if key in ("inputTokens", "outputTokens", "tokenCount")
            and isinstance(value, (int, float))
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


# --- chemins user multi-plateforme --------------------------------------------


def _candidate_user_dirs() -> list[Path]:
    """Candidats plateforme (existants retenus par l'appelant), dédupliqués."""
    home = Path.home()
    candidates: list[Path] = []
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            candidates.append(Path(appdata) / "Code" / "User")
    else:
        candidates.append(Path(_DEFAULT_USER_DIR).expanduser())
    candidates.extend(
        [
            home / ".vscode-server" / "data" / "User",
        ]
    )
    seen: set[str] = set()
    unique: list[Path] = []
    for candidate in candidates:
        key = str(candidate)
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def _resolve_user_dirs(source_cfg: dict | None) -> list[Path]:
    """Chaîne de résolution : user_dirs > user_dir > $VSCODE_USER_DIR > candidats."""
    if isinstance(source_cfg, dict):
        raw_list = source_cfg.get("user_dirs")
        if isinstance(raw_list, list):
            explicit = [
                Path(item).expanduser()
                for item in raw_list
                if isinstance(item, str) and item.strip()
            ]
            if explicit:
                return explicit
        raw_dir = source_cfg.get("user_dir")
        if isinstance(raw_dir, str) and raw_dir.strip():
            return [Path(raw_dir).expanduser()]
    env_dir = os.environ.get("VSCODE_USER_DIR")
    if env_dir and env_dir.strip():
        return [Path(env_dir).expanduser()]
    return _candidate_user_dirs()


# --- parsing .jsonl (streaming) ------------------------------------------------


def _apply_jsonl_op(requests: list[dict], op: object) -> None:
    """Applique une op ``splice``/``push`` sur ``path: ["requests", ...]`` ; tolérant."""
    if not isinstance(op, dict):
        return
    kind = op.get("op")
    path = op.get("path")
    if not isinstance(kind, str) or not isinstance(path, list) or not path:
        return
    if path[0] != "requests":
        return
    if kind == "push":
        value = op.get("value")
        if isinstance(value, dict):
            requests.append(value)
        elif isinstance(value, list):
            requests.extend(item for item in value if isinstance(item, dict))
    elif kind == "splice":
        start = op.get("start", len(requests))
        delete = op.get("deleteCount", 0)
        items = op.get("items", [])
        if not isinstance(start, (int, float)) or not isinstance(delete, (int, float)):
            return
        start_i = max(0, min(int(start), len(requests)))
        del requests[start_i : start_i + max(0, int(delete))]
        if isinstance(items, dict):
            items = [items]
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    requests.insert(start_i, item)
                    start_i += 1


def _parse_session_jsonl(path: Path) -> dict | None:
    """Parse un ``.jsonl`` en streaming : snapshot puis ops ; None si inexploitable."""
    session_id: str | None = None
    creation_ms: int | None = None
    custom_title: str | None = None
    requests: list[dict] = []
    seen_snapshot = False
    valid_lines = 0
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if not isinstance(obj, dict):
                continue
            valid_lines += 1
            if "op" in obj:
                _apply_jsonl_op(requests, obj)
            elif "sessionId" in obj or "creationDate" in obj or "version" in obj:
                if not seen_snapshot:
                    seen_snapshot = True
                    raw_id = obj.get("sessionId")
                    session_id = str(raw_id) if raw_id else None
                    creation_ms = _positive_ms(obj.get("creationDate"))
                    title = obj.get("customTitle")
                    custom_title = title.strip() if isinstance(title, str) and title.strip() else None
                    raw_requests = obj.get("requests")
                    if isinstance(raw_requests, list):
                        requests.extend(r for r in raw_requests if isinstance(r, dict))
                else:
                    raw_requests = obj.get("requests")
                    if isinstance(raw_requests, list):
                        requests.extend(r for r in raw_requests if isinstance(r, dict))
            # kind1 {key} et lignes inconnues : ignorées (références d'index).
    if valid_lines == 0:
        return None
    return {
        "session_id": session_id or path.stem,
        "creation_ms": creation_ms,
        "custom_title": custom_title,
        "requests": requests,
    }


# --- index state.vscdb ---------------------------------------------------------


def _open_ro_db(db_path: Path) -> sqlite3.Connection | None:
    """Ouvre une base sqlite en lecture seule ; None si impossible (délègue)."""
    try:
        return open_database(db_path)
    except Exception:  # noqa: BLE001 — fail-soft
        return None


def _read_session_index(user_dir: Path) -> dict[str, dict]:
    """Entrées ``chat.ChatSessionStore.index`` ; {} si absent/illisible (fail-soft)."""
    conn = _open_ro_db(user_dir / "globalStorage" / "state.vscdb")
    if conn is None:
        return {}
    try:
        row = conn.execute(
            "SELECT value FROM ItemTable WHERE key=?", (_INDEX_KEY,)
        ).fetchone()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    if not row:
        return {}
    try:
        payload = json.loads(row[0]) if isinstance(row[0], (str, bytes)) else None
    except ValueError:
        return {}
    entries = payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(entries, dict):
        return {}
    return {str(sid): info for sid, info in entries.items() if isinstance(info, dict)}


def _archived_ids_from_obj(obj: object, acc: set[str]) -> None:
    """Collecte tolérante d'ids archivés depuis des formes JSON variées."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(key, str) and "archiv" in key.lower():
                if isinstance(value, list):
                    for item in value:
                        if isinstance(item, str):
                            acc.add(item)
                        elif isinstance(item, dict) and isinstance(item.get("id"), str):
                            acc.add(item["id"])
                elif isinstance(value, dict):
                    for sub_id, sub_val in value.items():
                        if sub_val is True and isinstance(sub_id, str):
                            acc.add(sub_id)
                        elif isinstance(sub_val, dict) and sub_val.get("archived") is True:
                            acc.add(str(sub_id))
            elif isinstance(value, dict):
                if value.get("archived") is True and isinstance(key, str):
                    acc.add(key)
                else:
                    _archived_ids_from_obj(value, acc)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        item_id = item.get("id") or item.get("sessionId")
                        if item.get("archived") is True and isinstance(item_id, str):
                            acc.add(item_id)
                        else:
                            _archived_ids_from_obj(item, acc)
    elif isinstance(obj, list):
        for item in obj:
            _archived_ids_from_obj(item, acc)


def _read_archived_ids(user_dir: Path) -> set[str]:
    """Ids archivés depuis les ``agentSessions.state.cache`` lisibles ; fail-soft."""
    acc: set[str] = set()
    candidates = [user_dir / "globalStorage" / "agentSessions.state.cache"]
    ws_root = user_dir / "workspaceStorage"
    if ws_root.is_dir():
        try:
            ws_dirs = [p for p in ws_root.iterdir() if p.is_dir()]
        except OSError:
            ws_dirs = []
        candidates.extend(wd / "agentSessions.state.cache" for wd in ws_dirs)
    for cache_path in candidates:
        if not cache_path.is_file():
            continue
        try:
            _archived_ids_from_obj(json.loads(cache_path.read_text(encoding="utf-8")), acc)
        except (OSError, ValueError):
            continue
    return acc


# --- transcripts de repli ------------------------------------------------------


def _event_ts_ms(event: dict) -> int | None:
    for key in ("timestamp", "time", "createdAt", "startTime", "date"):
        value = event.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return int(value)
    return None


def _event_text(event: dict) -> str:
    for key in ("text", "message", "content", "prompt", "value", "response"):
        value = event.get(key)
        text = value if isinstance(value, str) else _message_text(value)
        if isinstance(text, str) and text.strip():
            return text
    return ""


def _tool_name_of(event: dict) -> str:
    for key in ("toolName", "name", "tool", "function"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "unknown"


def _load_transcript(
    ws_dir: Path, session_id: str
) -> tuple[list[tuple[str, int | None]], list[tuple[str, int | None]], list[tuple[dict, int | None]]]:
    """Tours/outils depuis ``GitHub.copilot-chat/transcripts/<id>.jsonl`` ; vide si absent."""
    transcript = ws_dir / "GitHub.copilot-chat" / "transcripts" / f"{session_id}.jsonl"
    users: list[tuple[str, int | None]] = []
    assistants: list[tuple[str, int | None]] = []
    tools: list[tuple[dict, int | None]] = []
    if not transcript.is_file():
        return users, assistants, tools
    try:
        lines = transcript.read_text(encoding="utf-8").splitlines()
    except OSError:
        return users, assistants, tools
    pending: list[int] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = str(event.get("type") or "")
        ts_ms = _event_ts_ms(event)
        if kind in ("user.message", "user", "prompt"):
            text = _event_text(event)
            if text.strip():
                users.append((text, ts_ms))
        elif kind in ("assistant.message", "assistant", "response"):
            text = _event_text(event)
            if text.strip():
                assistants.append((text, ts_ms))
        elif kind.endswith("execution_start") or kind in ("tool_start", "tool_call"):
            tool = {
                "name": _tool_name_of(event),
                "input": _tool_field(event, "input", "parameters", "arguments", "args"),
            }
            tools.append((tool, ts_ms))
            pending.append(len(tools) - 1)
        elif kind.endswith("complete") or kind.endswith("execution_end") or kind in (
            "tool_end",
            "tool_result",
        ):
            output = _event_text(event) or _tool_field(event, "result", "output")
            if pending:
                idx = pending.pop(0)
                if output:
                    tools[idx][0]["result"] = output
            elif output.strip():
                tools.append(({"name": _tool_name_of(event), "result": output}, ts_ms))
    return users, assistants, tools


def _collect_paths(obj: object, acc: list[str], depth: int = 0) -> None:
    """Collecte tolérante de chemins (clés uri/path/file/...) ; profondeur bornée."""
    if depth > 6:
        return
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(key, str) and any(
                token in key.lower() for token in ("uri", "path", "filename", "filepath", "resource")
            ):
                if isinstance(value, str) and value.strip():
                    converted = _folder_uri_to_path(value.strip())
                    if converted and converted not in acc and len(acc) < 50:
                        acc.append(converted)
            else:
                _collect_paths(value, acc, depth + 1)
    elif isinstance(obj, list):
        for item in obj:
            _collect_paths(item, acc, depth + 1)


def _load_editing_files(ws_dir: Path, session_id: str) -> list[str]:
    """Fichiers depuis ``chatEditingSessions/<id>/state.json`` ; [] si absent."""
    state_path = ws_dir / "chatEditingSessions" / session_id / "state.json"
    if not state_path.is_file():
        return []
    try:
        raw = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    acc: list[str] = []
    _collect_paths(raw, acc)
    return acc


class CopilotVSCodeSessionProvider:
    """Sessions Copilot de VS Code, ids canoniques ``copilot-vscode:<sessionId>``."""

    harness = HARNESS_COPILOT_VSCODE

    def __init__(
        self,
        user_dir: Path,
        user_dirs: list[Path] | None = None,
        include_orphans: bool = True,
    ) -> None:
        self.user_dir = user_dir
        merged: list[Path] = [user_dir]
        for extra in user_dirs or []:
            if extra not in merged:
                merged.append(extra)
        self.user_dirs = merged
        self.include_orphans = include_orphans
        self._sessions: dict[str, _ChatSession] = {}
        self._orphan_count = 0
        for root in self.user_dirs:
            self._load_workspace_sessions(root)
            self._load_empty_window_sessions(root)
            self._merge_session_index(root)
        self._finalize_titles()
        if self._orphan_count:
            warnings.warn(
                f"vscode-orphan : {self._orphan_count} session(s) sans workspace.json "
                "exploitable (directory=None)",
                stacklevel=2,
            )
        #: Comptage informatif des clés chat de state.vscdb ; None = source absente.
        self.global_state_key_count: int | None = self._count_global_state_chat_keys()

    # --- chargement ---------------------------------------------------------

    def _register(
        self,
        session_id: str,
        creation_ms: int | None,
        last_ms: int | None,
        directory: str | None,
        title: str | None,
        custom_title: str | None,
        requests: list[dict],
        users: list[tuple[str, int | None]] | None = None,
        assistants: list[tuple[str, int | None]] | None = None,
        tools: list[tuple[dict, int | None]] | None = None,
        files: list[str] | None = None,
    ) -> None:
        if session_id in self._sessions:
            return  # .jsonl prioritaire / premier user_dir gagne
        self._sessions[session_id] = _ChatSession(
            session_id=session_id,
            creation_ms=creation_ms,
            last_ms=last_ms,
            directory=directory,
            title=title or custom_title,
            requests=requests,
            custom_title=custom_title,
            transcript_user=users or [],
            transcript_assistant=assistants or [],
            transcript_tools=tools or [],
            files=files or [],
        )

    def _load_chat_dir(
        self, ws_dir: Path, chat_dir: Path, directory: str | None, is_orphan: bool
    ) -> None:
        loaded_from_jsonl: set[str] = set()
        jsonl_files = sorted(chat_dir.glob("*.jsonl"))
        legacy_files = sorted(chat_dir.glob("*.json"))
        for session_file in jsonl_files:
            try:
                parsed = _parse_session_jsonl(session_file)
            except OSError:
                warnings.warn(f"chatSessions illisible, ignoré : {session_file}", stacklevel=2)
                continue
            if parsed is None:
                warnings.warn(f"chatSessions illisible, ignoré : {session_file}", stacklevel=2)
                continue
            session_id = parsed["session_id"]
            loaded_from_jsonl.add(session_file.stem)
            if is_orphan and not self.include_orphans:
                continue
            users, assistants, tools = _load_transcript(ws_dir, session_id)
            self._register(
                session_id,
                parsed["creation_ms"],
                None,  # dates affinées ensuite par l'index state.vscdb
                None if is_orphan else directory,
                parsed["custom_title"],
                parsed["custom_title"],
                parsed["requests"],
                users,
                assistants,
                tools,
                _load_editing_files(ws_dir, session_id),
            )
            if is_orphan:
                self._orphan_count += 1
        for session_file in legacy_files:
            if session_file.stem in loaded_from_jsonl:
                continue  # .jsonl prioritaire sur le legacy
            try:
                raw = json.loads(session_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                warnings.warn(f"chatSessions illisible, ignoré : {session_file}", stacklevel=2)
                continue
            if not isinstance(raw, dict):
                continue
            if is_orphan and not self.include_orphans:
                continue
            raw_requests = raw.get("requests")
            requests = (
                [r for r in raw_requests if isinstance(r, dict)]
                if isinstance(raw_requests, list)
                else []
            )
            session_id = str(raw.get("sessionId") or session_file.stem)
            title = raw.get("customTitle")
            custom_title = title.strip() if isinstance(title, str) and title.strip() else None
            users, assistants, tools = _load_transcript(ws_dir, session_id)
            self._register(
                session_id,
                _positive_ms(raw.get("creationDate")),
                _positive_ms(raw.get("lastMessageDate")),
                None if is_orphan else directory,
                custom_title,
                custom_title,
                requests,
                users,
                assistants,
                tools,
                _load_editing_files(ws_dir, session_id),
            )
            if is_orphan:
                self._orphan_count += 1

    def _load_workspace_sessions(self, user_dir: Path) -> None:
        ws_root = user_dir / "workspaceStorage"
        if not ws_root.is_dir():
            return
        try:
            ws_dirs = sorted(p for p in ws_root.iterdir() if p.is_dir())
        except OSError:
            return
        for ws_dir in ws_dirs:
            workspace_json = ws_dir / "workspace.json"
            directory = _project_folder(workspace_json)
            is_orphan = not workspace_json.is_file() or directory is None
            chat_dir = ws_dir / "chatSessions"
            if not chat_dir.is_dir():
                continue
            self._load_chat_dir(ws_dir, chat_dir, directory, is_orphan)

    def _load_empty_window_sessions(self, user_dir: Path) -> None:
        """``globalStorage/emptyWindowChatSessions/*`` (.jsonl puis .json), directory=None."""
        empty_dir = user_dir / "globalStorage" / "emptyWindowChatSessions"
        if not empty_dir.is_dir():
            return
        try:
            jsonl_files = sorted(empty_dir.glob("*.jsonl"))
            legacy_files = sorted(empty_dir.glob("*.json"))
        except OSError:
            return
        loaded: set[str] = set()
        for session_file in jsonl_files:
            try:
                parsed = _parse_session_jsonl(session_file)
            except OSError:
                warnings.warn(f"chatSessions illisible, ignoré : {session_file}", stacklevel=2)
                continue
            if parsed is None:
                warnings.warn(f"chatSessions illisible, ignoré : {session_file}", stacklevel=2)
                continue
            loaded.add(session_file.stem)
            self._register(
                parsed["session_id"],
                parsed["creation_ms"],
                None,
                None,
                parsed["custom_title"],
                parsed["custom_title"],
                parsed["requests"],
            )
        for session_file in legacy_files:
            if session_file.stem in loaded:
                continue
            try:
                raw = json.loads(session_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                warnings.warn(f"chatSessions illisible, ignoré : {session_file}", stacklevel=2)
                continue
            if not isinstance(raw, dict):
                continue
            raw_requests = raw.get("requests")
            requests = (
                [r for r in raw_requests if isinstance(r, dict)]
                if isinstance(raw_requests, list)
                else []
            )
            session_id = str(raw.get("sessionId") or session_file.stem)
            title = raw.get("customTitle")
            custom_title = title.strip() if isinstance(title, str) and title.strip() else None
            self._register(
                session_id,
                _positive_ms(raw.get("creationDate")),
                _positive_ms(raw.get("lastMessageDate")),
                None,
                custom_title,
                custom_title,
                requests,
            )

    def _merge_session_index(self, user_dir: Path) -> None:
        index = _read_session_index(user_dir)
        if not index:
            return
        archived = _read_archived_ids(user_dir)
        for session_id, info in index.items():
            idx_title = info.get("title")
            idx_title = (
                idx_title.strip()
                if isinstance(idx_title, str) and idx_title.strip()
                else None
            )
            last_msg = _positive_ms(info.get("lastMessageDate"))
            timing = info.get("timing")
            last_started = (
                _positive_ms(timing.get("lastRequestStarted"))
                if isinstance(timing, dict)
                else None
            )
            best_last = max(
                [ms for ms in (last_msg, last_started) if ms is not None], default=None
            )
            is_empty = info.get("isEmpty") is True
            entry = self._sessions.get(session_id)
            if entry is None:
                if is_empty:
                    continue  # stub vide sans transcript : rien à conserver
                working = info.get("workingDirectory")
                self._sessions[session_id] = _ChatSession(
                    session_id=session_id,
                    creation_ms=None,
                    last_ms=best_last,
                    directory=working if isinstance(working, str) and working else None,
                    title=idx_title,
                    requests=[],
                    archived=session_id in archived,
                )
                continue
            if entry.custom_title is None and idx_title is not None:
                entry.title = idx_title
            if best_last is not None and (entry.last_ms is None or best_last > entry.last_ms):
                entry.last_ms = best_last
            if session_id in archived:
                entry.archived = True
            if is_empty and (entry.requests or entry.has_transcript()):
                entry.empty_stub = True
                warnings.warn(
                    f"vscode-empty-stub : session {session_id} marquée vide par "
                    "l'index mais transcript non-vide — conservée",
                    stacklevel=2,
                )

    def _finalize_titles(self) -> None:
        """Repli ``premier user_text[:80]`` (requests puis transcripts) + tag archived."""
        for entry in self._sessions.values():
            if entry.title is None:
                first_text = next(
                    (
                        text
                        for text in (
                            _message_text(req.get("message")).strip() for req in entry.requests
                        )
                        if text
                    ),
                    "",
                )
                if not first_text and entry.transcript_user:
                    first_text = entry.transcript_user[0][0].strip()
                entry.title = first_text[:80] or None
            if entry.archived and entry.title is not None and "[archived]" not in entry.title:
                entry.title = f"{entry.title} [archived]"

    def _count_global_state_chat_keys(self) -> int | None:
        """Compte readonly des clés chat/interactive.session ; None si indisponible."""
        db_path = self.user_dir / "globalStorage" / "state.vscdb"
        conn = _open_ro_db(db_path)
        if conn is None:
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

    @staticmethod
    def _in_window(ts_ms: int | None, start_ms: int, end_ms: int) -> bool:
        return ts_ms is None or start_ms <= ts_ms <= end_ms

    def _to_harness_session(self, entry: _ChatSession) -> HarnessSession:
        tokens = [_request_tokens(req) for req in entry.requests]
        model_key = next(
            (
                _request_model_key(req)
                for req in entry.requests
                if _request_model_key(req) != "unknown/unknown"
            ),
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
        for root in self.user_dirs:
            if (root / "workspaceStorage").is_dir():
                return
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
        return bool(entry and (entry.requests or entry.has_transcript()))

    def session_steps(self, session_id: str, start_ms: int, end_ms: int) -> list[StepFinish]:
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

        def _accumulate(name: str, arg_chars: int) -> None:
            tool_calls[name] = tool_calls.get(name, 0) + 1
            tool_arg_chars[name] = tool_arg_chars.get(name, 0) + arg_chars

        if entry.requests:
            for req, _ts_ms in self._windowed_requests(entry, start_ms, end_ms):
                for tool in _request_tools(req):
                    name = str(tool.get("name") or tool.get("toolName") or "unknown")
                    _accumulate(name, len(_tool_field(tool, "input", "parameters", "arguments")))
        else:
            for tool, ts_ms in entry.transcript_tools:
                if not self._in_window(ts_ms, start_ms, end_ms):
                    continue
                name = str(tool.get("name") or "unknown")
                _accumulate(name, len(_tool_field(tool, "input", "parameters", "arguments")))
        return tool_calls, tool_arg_chars, {}  # skills_loaded : sans objet hors CLI

    def session_user_turns(self, session_id: str, start_ms: int, end_ms: int) -> list[str]:
        entry = self._get(session_id)
        if entry is None:
            return []
        if entry.requests:
            turns = []
            for req, _ts_ms in self._windowed_requests(entry, start_ms, end_ms):
                text = _message_text(req.get("message"))
                if text.strip():
                    turns.append(text)
            return turns
        return [
            text
            for text, ts_ms in entry.transcript_user
            if text.strip() and self._in_window(ts_ms, start_ms, end_ms)
        ]

    def session_context_chars(self, session_id: str, start_ms: int, end_ms: int) -> dict[str, int]:
        entry = self._get(session_id)
        if entry is None:
            return dict.fromkeys(_CONTEXT_KEYS, 0)
        counts = dict.fromkeys(_CONTEXT_KEYS, 0)
        if entry.requests:
            for req, _ts_ms in self._windowed_requests(entry, start_ms, end_ms):
                counts["text"] += len(_response_text(req))
                for tool in _request_tools(req):
                    counts["tool_result"] += len(_tool_field(tool, "result", "output"))
        else:
            for text, ts_ms in entry.transcript_assistant:
                if self._in_window(ts_ms, start_ms, end_ms):
                    counts["text"] += len(text)
            for tool, ts_ms in entry.transcript_tools:
                if self._in_window(ts_ms, start_ms, end_ms):
                    counts["tool_result"] += len(_tool_field(tool, "result", "output"))
        counts["file"] += sum(len(path) for path in entry.files)
        return counts

    def session_aggregates(self, session_id: str) -> dict | None:
        self._get(session_id)  # tolérance id brut/canonique
        return None  # VS Code ne persiste aucun agrégat vie-telle-enregistrée

    def session_parts(self, session_id: str) -> list[PartRecord]:
        entry = self._get(session_id)
        if entry is None:
            return []
        parts: list[PartRecord] = []
        if entry.requests:
            for req in entry.requests:
                ts_ms = _request_ts_ms(req, entry.last_ms or entry.creation_ms) or 0
                ts = datetime.fromtimestamp(ts_ms / 1000, tz=UTC) if ts_ms else None
                user_text = _message_text(req.get("message"))
                if user_text.strip() and ts is not None:
                    parts.append(PartRecord(ts=ts, kind="user", text=user_text[:_PART_TRUNCATE]))
                for tool in _request_tools(req):
                    if ts is None:
                        break
                    name = str(tool.get("name") or tool.get("toolName") or "unknown")
                    parts.append(
                        PartRecord(
                            ts=ts,
                            kind="tool",
                            tool_name=name,
                            tool_input=_tool_field(tool, "input", "parameters", "arguments")[
                                :_PART_TRUNCATE
                            ],
                            tool_output=_tool_field(tool, "result", "output")[:_PART_TRUNCATE]
                            or None,
                        )
                    )
                response = _response_text(req)
                if response.strip() and ts is not None:
                    parts.append(
                        PartRecord(ts=ts, kind="assistant", text=response[:_PART_TRUNCATE])
                    )
            return parts
        fallback_ts = entry.last_ms or entry.creation_ms
        fallback_dt = (
            datetime.fromtimestamp(fallback_ts / 1000, tz=UTC) if fallback_ts else None
        )
        for text, ts_ms in entry.transcript_user:
            ts = (
                datetime.fromtimestamp(ts_ms / 1000, tz=UTC)
                if ts_ms
                else fallback_dt
            )
            if text.strip() and ts is not None:
                parts.append(PartRecord(ts=ts, kind="user", text=text[:_PART_TRUNCATE]))
        for tool, ts_ms in entry.transcript_tools:
            ts = (
                datetime.fromtimestamp(ts_ms / 1000, tz=UTC)
                if ts_ms
                else fallback_dt
            )
            if ts is None:
                continue
            parts.append(
                PartRecord(
                    ts=ts,
                    kind="tool",
                    tool_name=str(tool.get("name") or "unknown"),
                    tool_input=_tool_field(tool, "input", "parameters", "arguments")[
                        :_PART_TRUNCATE
                    ],
                    tool_output=_tool_field(tool, "result", "output")[:_PART_TRUNCATE] or None,
                )
            )
        for text, ts_ms in entry.transcript_assistant:
            ts = (
                datetime.fromtimestamp(ts_ms / 1000, tz=UTC)
                if ts_ms
                else fallback_dt
            )
            if text.strip() and ts is not None:
                parts.append(PartRecord(ts=ts, kind="assistant", text=text[:_PART_TRUNCATE]))
        if fallback_dt is not None:
            for path in entry.files:
                parts.append(PartRecord(ts=fallback_dt, kind="file", text=path[:_PART_TRUNCATE]))
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
    """Factory registry : None si aucun `user_dir` exploitable (source indisponible propre)."""
    raw_cfg = source_cfg if isinstance(source_cfg, dict) else {}
    candidates = _resolve_user_dirs(raw_cfg)
    seen: set[str] = set()
    existing: list[Path] = []
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        if candidate.is_dir():
            existing.append(candidate)
    if not existing:
        return None
    include_orphans = raw_cfg.get("include_orphans", True)
    return CopilotVSCodeSessionProvider(
        existing[0],
        user_dirs=existing[1:],
        include_orphans=bool(include_orphans),
    )
