"""Provider Copilot CLI — ``session-store.db`` + ``session-state/`` de ``~/.copilot``.

Source primaire : ``<copilot_home>/session-store.db`` (SQLite, lecture seule),
tables cœur ``sessions`` / ``turns`` / ``assistant_usage_events`` ; tables
optionnelles (``forge_trajectory_events``, ``session_files``, ``session_refs``,
``checkpoints``, ``search_index`` FTS5, ``schema_version``) exploitées en
fail-soft (avertissement + fallback). Tables explicitement ignorées :
``dynamic_context_items``, ``forge_skill_proposals``.

Source secondaire : ``<copilot_home>/session-state/<uuid>/`` —
``workspace.yaml`` (``cwd``/``name|title|summary→title``) et
``checkpoints/index.md`` servent uniquement à combler un ``cwd``/``summary``
absent de la table ``sessions`` ; illisibles → ignorés.

HORS PÉRIMÈTRE : ``config.json``, ``mcp-config.json`` et tout fichier
d'authentification ne sont JAMAIS lus ni listés. Aucune écriture.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sqlite3
import warnings
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from ...models import StepFinish, canonical_session_id
from ...sqlite_reader import PartRecord, SchemaError, open_database
from ..base import HarnessSession

if TYPE_CHECKING:
    from ...config import TelemetryConfig

#: Identifiant de harnais (préfixe des ids canoniques).
HARNESS_COPILOT_CLI = "copilot-cli"

PROVIDER_TYPE = HARNESS_COPILOT_CLI

#: Nom du fichier SQLite sous COPILOT_HOME.
_DB_NAME = "session-store.db"

#: Tables cœur exigées par `check_schema` (table → colonnes requises).
_CORE_TABLES: dict[str, tuple[str, ...]] = {
    "sessions": ("id", "cwd", "repository", "branch", "summary", "created_at", "updated_at"),
    "turns": ("session_id", "turn_index", "user_message", "assistant_response", "timestamp"),
    "assistant_usage_events": (
        "session_id",
        "model",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "reasoning_tokens",
        "created_at",
    ),
}

#: Tables optionnelles : absentes → avertissement + fallback, jamais bloquant.
_OPTIONAL_TABLES = (
    "forge_trajectory_events",
    "session_files",
    "session_refs",
    "checkpoints",
    "search_index",
    "schema_version",
)

#: Clés de contexte alignées sur les autres providers.
_CONTEXT_KEYS = ("file", "tool_result", "text", "reasoning")

#: Fenêtre `list_sessions` : borne + taille des lots IN().
_LIST_LIMIT = 500
_BATCH_SIZE = 50

_WS_YAML_KEYS = re.compile(r"^\s*(?:cwd|workspace|directory|path)\s*:\s*(.+?)\s*$")
_WS_YAML_TITLE_KEYS = re.compile(r"^\s*(?:name|title|summary)\s*:\s*(.+?)\s*$")
_MD_HEADING = re.compile(r"^\s*#\s+(.+?)\s*$")


def _default_copilot_home() -> Path:
    """``~/.copilot`` (Linux/mac) ou ``%USERPROFILE%/.copilot`` (Windows)."""
    if os.name == "nt":
        base = os.environ.get("USERPROFILE") or str(Path.home())
        return Path(base) / ".copilot"
    return Path.home() / ".copilot"


def resolve_copilot_home(source_cfg: dict) -> Path:
    """Résout COPILOT_HOME : ``copilot_home`` > ``COPILOT_CONFIG_DIR`` > défaut OS."""
    if isinstance(source_cfg, dict):
        raw = source_cfg.get("copilot_home")
        if isinstance(raw, str) and raw.strip():
            return Path(raw).expanduser()
    env = os.environ.get("COPILOT_CONFIG_DIR")
    if env and env.strip():
        return Path(env).expanduser()
    return _default_copilot_home()


def _parse_ts(value: object) -> datetime | None:
    """Horodatage tolérant : epoch (s/ms), ISO, ``YYYY-MM-DD HH:MM:SS`` UTC."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        num = float(value)
        if num <= 0:
            return None
        if num > 1e10:  # epoch ms (ou µs : garde-fou, traite comme ms)
            num /= 1000.0
        return datetime.fromtimestamp(num, tz=UTC)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            if text.endswith(("Z", "z")):
                text = text[:-1] + "+00:00"
            dt = datetime.fromisoformat(text)
            return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
        except ValueError:
            pass
        try:
            return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
        except ValueError:
            return None
    return None


def _format_ts(dt: datetime) -> str:
    """Formate un instant en ``YYYY-MM-DD HH:MM:SS`` UTC (écriture des tests)."""
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _model_key(raw: object) -> str:
    """Modèle brut avec ``/`` gardé, sinon préfixé ``github-copilot/``."""
    model = str(raw or "").strip()
    if not model:
        return "unknown/unknown"
    return model if "/" in model else f"github-copilot/{model}"


def _num(value: object) -> float:
    return float(value) if isinstance(value, (int, float)) else 0.0


def _parse_nano_cost(raw_json: object, multiplier: object | None) -> float:
    """Parse `token_details_json.total_nano_aiu * request_multiplier / 1e9` → USD.

    Fail-soft : JSON illisible ou champ manquant → warn + 0.0.
    """
    if raw_json is None:
        return 0.0
    text = raw_json if isinstance(raw_json, str) else str(raw_json)
    text = text.strip()
    if not text:
        return 0.0
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError, TypeError):
        warnings.warn(
            f"copilot-cli : token_details_json illisible ({text[:60]!r})",
            stacklevel=4,
        )
        return 0.0
    if not isinstance(data, dict):
        return 0.0
    raw_val = data.get("total_nano_aiu")
    if raw_val is None:
        return 0.0
    try:
        nano = float(raw_val)
    except (TypeError, ValueError):
        warnings.warn(
            f"copilot-cli : total_nano_aiu non numérique ({raw_val!r})",
            stacklevel=4,
        )
        return 0.0
    mult = 1.0
    if multiplier is not None:
        try:
            mult = float(multiplier)
        except (TypeError, ValueError):
            warnings.warn(
                f"copilot-cli : request_multiplier illisible ({multiplier!r})",
                stacklevel=4,
            )
            mult = 1.0
    return nano * mult / 1e9


@dataclass(slots=True)
class _CliSession:
    """Vue plate d'une ligne ``sessions`` enrichie (tokens + modèle + télémétrie)."""

    session_id: str
    cwd: str | None = None
    repository: str | None = None
    branch: str | None = None
    summary: str | None = None
    created: datetime | None = None
    updated: datetime | None = None
    model_key: str = "unknown/unknown"
    tokens: dict[str, float] = field(default_factory=dict)
    turn_count: int = 0
    event_count: int = 0
    cost_estimate: float | None = None

    @property
    def active_ms(self) -> int:
        """Horodatage d'activité retenu pour le fenêtrage `list_sessions`."""
        dt = self.updated or self.created
        return int(dt.timestamp() * 1000) if dt else 0

    @property
    def directory(self) -> str | None:
        return self.cwd or None

    @property
    def has_telemetry(self) -> bool:
        return self.turn_count > 0 or self.event_count > 0


class CopilotCliSessionProvider:
    """Sessions Copilot CLI, ids canoniques ``copilot-cli:<id>``."""

    harness = HARNESS_COPILOT_CLI

    def __init__(self, home: Path, conn: sqlite3.Connection) -> None:
        self.home = home
        self.db_path = home / _DB_NAME
        self._conn = conn
        self._closed = False
        self._tables: set[str] = self._list_tables()
        self._warned_optionals: set[str] = set()
        self._sessions: dict[str, _CliSession] = {}
        self._load_sessions()

    # --- chargement ---------------------------------------------------------

    def _list_tables(self) -> set[str]:
        try:
            rows = self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        except sqlite3.Error:
            return set()
        return {str(r[0]) for r in rows}

    def _warn_optional(self, table: str) -> None:
        if table not in self._warned_optionals:
            self._warned_optionals.add(table)
            warnings.warn(
                f"copilot-cli : table optionnelle {table!r} absente — fallback",
                stacklevel=3,
            )

    def _has_table(self, table: str, *, optional: bool = False) -> bool:
        present = table in self._tables
        if not present and optional:
            self._warn_optional(table)
        return present

    def _load_sessions(self) -> None:
        try:
            rows = self._conn.execute(
                "SELECT id, cwd, repository, branch, summary, created_at, updated_at "
                "FROM sessions ORDER BY updated_at DESC"
            ).fetchall()
        except sqlite3.Error as exc:
            warnings.warn(f"copilot-cli : sessions illisibles ({exc})", stacklevel=2)
            return
        for row in rows:
            try:
                sid = str(row["id"])
            except (KeyError, TypeError, IndexError):
                continue
            created = _parse_ts(row["created_at"])
            updated = _parse_ts(row["updated_at"])
            session = _CliSession(
                session_id=sid,
                cwd=str(row["cwd"]) if row["cwd"] else None,
                repository=str(row["repository"]) if row["repository"] else None,
                branch=str(row["branch"]) if row["branch"] else None,
                summary=str(row["summary"]) if row["summary"] else None,
                created=created,
                updated=updated,
            )
            extra = self._state_enrichment(sid)
            if session.cwd is None and extra.get("directory"):
                session.cwd = extra["directory"]
            if session.summary is None and extra.get("title"):
                session.summary = extra["title"]
            self._sessions[sid] = session
        self._batch_enrich_telemetry()

    def _batch_enrich_telemetry(self) -> None:
        """Lots IN(50) : SUM tokens + modèle récent + COUNT events/turns."""
        ids = list(self._sessions)
        latest_ts: dict[str, datetime] = {}
        for i in range(0, len(ids), _BATCH_SIZE):
            chunk = ids[i : i + _BATCH_SIZE]
            placeholders = ",".join("?" for _ in chunk)
            try:
                for row in self._conn.execute(
                    "SELECT session_id, model, "
                    "SUM(input_tokens), SUM(output_tokens), SUM(reasoning_tokens), "
                    "SUM(cache_read_tokens), SUM(cache_write_tokens), "
                    "COUNT(*), MAX(created_at) "
                    f"FROM assistant_usage_events WHERE session_id IN ({placeholders}) "
                    "GROUP BY session_id, model",
                    chunk,
                ).fetchall():
                    session = self._sessions.get(str(row[0]))
                    if session is None:
                        continue
                    session.event_count += int(row[7] or 0)
                    latest = _parse_ts(row[8])
                    seen = latest_ts.get(session.session_id)
                    if latest is not None and (seen is None or latest >= seen):
                        latest_ts[session.session_id] = latest
                        session.model_key = _model_key(row[1])
                    for key, val in zip(
                        ("input", "output", "reasoning", "cache_read", "cache_write"),
                        row[2:7],
                        strict=True,
                    ):
                        session.tokens[key] = session.tokens.get(key, 0.0) + _num(val)
            except sqlite3.Error as exc:
                warnings.warn(f"copilot-cli : usage illisible ({exc})", stacklevel=2)
            try:
                for row in self._conn.execute(
                    f"SELECT session_id, COUNT(*) FROM turns "
                    f"WHERE session_id IN ({placeholders}) GROUP BY session_id",
                    chunk,
                ).fetchall():
                    session = self._sessions.get(str(row[0]))
                    if session is not None:
                        session.turn_count += int(row[1] or 0)
            except sqlite3.Error as exc:
                warnings.warn(f"copilot-cli : turns illisibles ({exc})", stacklevel=2)
        # --- coût nano (token_details_json.total_nano_aiu * request_multiplier / 1e9) ---
        try:
            cols = {
                r[1]
                for r in self._conn.execute("PRAGMA table_info(assistant_usage_events)").fetchall()
            }
        except sqlite3.Error:
            cols = set()
        if "token_details_json" in cols:
            has_multiplier = "request_multiplier" in cols
            for i in range(0, len(ids), _BATCH_SIZE):
                chunk = ids[i : i + _BATCH_SIZE]
                placeholders = ",".join("?" for _ in chunk)
                col_list = "token_details_json" + (", request_multiplier" if has_multiplier else "")
                try:
                    for row in self._conn.execute(
                        f"SELECT session_id, {col_list} FROM assistant_usage_events "
                        f"WHERE session_id IN ({placeholders})",
                        chunk,
                    ).fetchall():
                        sid = str(row[0])
                        session = self._sessions.get(sid)
                        if session is None:
                            continue
                        raw_json = row[1]
                        mult = row[2] if has_multiplier else None
                        cost = _parse_nano_cost(raw_json, mult)
                        if cost:
                            session.tokens["nano_cost"] = (
                                session.tokens.get("nano_cost", 0.0) + cost
                            )
                            session.cost_estimate = (session.cost_estimate or 0.0) + cost
                except sqlite3.Error as exc:
                    warnings.warn(f"copilot-cli : nano cost illisible ({exc})", stacklevel=2)
                    break

    def _state_enrichment(self, session_id: str) -> dict[str, str]:
        """Enrichissement léger depuis ``session-state/<uuid>/`` ; fail-soft."""
        out: dict[str, str] = {}
        state_dir = self.home / "session-state" / session_id
        ws_yaml = state_dir / "workspace.yaml"
        if ws_yaml.is_file():
            try:
                for line in ws_yaml.read_text(encoding="utf-8").splitlines():
                    if "directory" not in out:
                        m_dir = _WS_YAML_KEYS.match(line)
                        if m_dir:
                            out["directory"] = m_dir.group(1).strip().strip("'\"")
                    if "title" not in out:
                        m_title = _WS_YAML_TITLE_KEYS.match(line)
                        if m_title:
                            out["title"] = m_title.group(1).strip().strip("'\"")[:80]
                    if "directory" in out and "title" in out:
                        break
            except (OSError, ValueError):
                warnings.warn(f"session-state illisible, ignoré : {ws_yaml}", stacklevel=2)
        index_md = state_dir / "checkpoints" / "index.md"
        if "title" not in out and index_md.is_file():
            try:
                for line in index_md.read_text(encoding="utf-8").splitlines():
                    match = _MD_HEADING.match(line)
                    if match and match.group(1).strip():
                        out["title"] = match.group(1).strip()[:80]
                        break
            except (OSError, ValueError):
                warnings.warn(f"session-state illisible, ignoré : {index_md}", stacklevel=2)
        return out

    # --- helpers ------------------------------------------------------------

    def _unwrap(self, session_id: str) -> str:
        prefix = f"{self.harness}:"
        return session_id[len(prefix) :] if session_id.startswith(prefix) else session_id

    def _get(self, session_id: str) -> _CliSession | None:
        return self._sessions.get(self._unwrap(session_id))

    def _to_harness_session(self, entry: _CliSession) -> HarnessSession:
        return HarnessSession(
            harness=self.harness,
            session_id=canonical_session_id(self.harness, entry.session_id),
            title=entry.summary,
            parent_id=None,
            model_key=entry.model_key,
            agent=None,
            directory=entry.directory,
            cost=entry.cost_estimate,
            tokens_input=entry.tokens.get("input", 0.0),
            tokens_output=entry.tokens.get("output", 0.0),
            tokens_reasoning=entry.tokens.get("reasoning", 0.0),
            tokens_cache_read=entry.tokens.get("cache_read", 0.0),
            tokens_cache_write=entry.tokens.get("cache_write", 0.0),
            time_created=entry.created,
            time_updated=entry.updated,
        )

    def _windowed_events(self, session_id: str, start_ms: int, end_ms: int) -> list[sqlite3.Row]:
        try:
            rows = self._conn.execute(
                "SELECT model, input_tokens, output_tokens, cache_read_tokens, "
                "cache_write_tokens, reasoning_tokens, created_at "
                "FROM assistant_usage_events WHERE session_id = ? ORDER BY created_at",
                (session_id,),
            ).fetchall()
        except sqlite3.Error:
            return []
        out = []
        for row in rows:
            ts = _parse_ts(row["created_at"])
            ms = int(ts.timestamp() * 1000) if ts else None
            if ms is not None and start_ms <= ms <= end_ms:
                out.append(row)
        return out

    def _windowed_turns(self, session_id: str, start_ms: int, end_ms: int) -> list[sqlite3.Row]:
        try:
            rows = self._conn.execute(
                "SELECT turn_index, user_message, assistant_response, timestamp "
                "FROM turns WHERE session_id = ? ORDER BY turn_index",
                (session_id,),
            ).fetchall()
        except sqlite3.Error:
            return []
        out = []
        for row in rows:
            ts = _parse_ts(row["timestamp"])
            ms = int(ts.timestamp() * 1000) if ts else None
            if ms is None or start_ms <= ms <= end_ms:
                out.append(row)
        return out

    def _windowed_trajectory(self, session_id: str, start_ms: int, end_ms: int) -> list[dict]:
        if not self._has_table("forge_trajectory_events", optional=True):
            return []
        try:
            cols = {r[1] for r in self._conn.execute("PRAGMA table_info(forge_trajectory_events)")}
        except sqlite3.Error:
            return []
        wanted = [c for c in ("event_type", "command", "output", "created_at") if c in cols]
        if not wanted:
            return []
        order = "ORDER BY created_at" if "created_at" in cols else "ORDER BY rowid"
        try:
            rows = self._conn.execute(
                f"SELECT {', '.join(wanted)} FROM forge_trajectory_events "
                f"WHERE session_id = ? {order}",
                (session_id,),
            ).fetchall()
        except sqlite3.Error:
            return []
        out = []
        for row in rows:
            item = {c: row[c] for c in wanted}
            if "created_at" in item:
                ts = _parse_ts(item["created_at"])
                ms = int(ts.timestamp() * 1000) if ts else None
                if ms is not None and not (start_ms <= ms <= end_ms):
                    continue
            out.append(item)
        return out

    # --- Protocol SessionProvider -------------------------------------------

    def check_schema(self) -> None:
        try:
            tables = {
                row[0]
                for row in self._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        except sqlite3.Error as exc:
            raise SchemaError(f"session-store.db illisible : {exc}") from exc
        missing = [t for t in _CORE_TABLES if t not in tables]
        if missing:
            raise SchemaError(f"tables cœur manquantes : {', '.join(missing)}")
        for table, columns in _CORE_TABLES.items():
            try:
                present = {row[1] for row in self._conn.execute(f"PRAGMA table_info({table})")}
            except sqlite3.Error as exc:
                raise SchemaError(f"table {table} illisible : {exc}") from exc
            absent = [c for c in columns if c not in present]
            if absent:
                raise SchemaError(f"table {table} : colonnes manquantes : {', '.join(absent)}")
        for table in _OPTIONAL_TABLES:
            if table not in tables:
                warnings.warn(
                    f"copilot-cli : table optionnelle {table!r} absente",
                    stacklevel=2,
                )
        if "schema_version" in tables:
            try:
                row = self._conn.execute(
                    "SELECT version FROM schema_version ORDER BY rowid DESC LIMIT 1"
                ).fetchone()
                if row is not None:
                    self.schema_version = str(row[0])
            except sqlite3.Error:
                pass

    def list_sessions(self, since_ms: int) -> list[HarnessSession]:
        sessions = [
            self._to_harness_session(entry)
            for entry in self._sessions.values()
            if entry.active_ms >= since_ms
        ]
        sessions.sort(
            key=lambda s: s.time_updated or s.time_created or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )
        return sessions[:_LIST_LIMIT]

    def has_telemetry_rows(self, session_id: str) -> bool:
        entry = self._get(session_id)
        return bool(entry and entry.has_telemetry)

    def session_steps(self, session_id: str, start_ms: int, end_ms: int) -> list[StepFinish]:
        entry = self._get(session_id)
        if entry is None:
            return []
        canonical = canonical_session_id(self.harness, entry.session_id)
        steps = []
        for row in self._windowed_events(entry.session_id, start_ms, end_ms):
            ts = _parse_ts(row["created_at"])
            if ts is None:
                continue
            steps.append(
                StepFinish(
                    session_id=canonical,
                    timestamp=ts,
                    model=_model_key(row["model"]),
                    tokens_input=_num(row["input_tokens"]),
                    tokens_output=_num(row["output_tokens"]),
                    tokens_reasoning=_num(row["reasoning_tokens"]),
                    tokens_cache_read=_num(row["cache_read_tokens"]),
                    tokens_cache_write=_num(row["cache_write_tokens"]),
                    cost=None,  # pas de coût persisté par le CLI
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
        trajectory = self._windowed_trajectory(entry.session_id, start_ms, end_ms)
        for item in trajectory:
            event_type = str(item.get("event_type") or "")
            if event_type and event_type != "command":
                continue
            name = str(item.get("command") or event_type or "unknown")
            tool_calls[name] = tool_calls.get(name, 0) + 1
            tool_arg_chars[name] = tool_arg_chars.get(name, 0) + len(str(item.get("command") or ""))
        if not tool_calls and self._has_table("session_files", optional=True):
            try:
                cols = {r[1] for r in self._conn.execute("PRAGMA table_info(session_files)")}
                if "tool_name" in cols:
                    rows = self._conn.execute(
                        "SELECT tool_name, file_path FROM session_files WHERE session_id = ?",
                        (entry.session_id,),
                    ).fetchall()
                    for row in rows:
                        name = str(row["tool_name"] or "unknown")
                        tool_calls[name] = tool_calls.get(name, 0) + 1
                        tool_arg_chars[name] = tool_arg_chars.get(name, 0) + len(
                            str(row["file_path"] or "")
                        )
            except sqlite3.Error:
                pass
        return tool_calls, tool_arg_chars, {}  # skills : sans objet pour ce harnais

    def session_user_turns(self, session_id: str, start_ms: int, end_ms: int) -> list[str]:
        entry = self._get(session_id)
        if entry is None:
            return []
        turns = []
        for row in self._windowed_turns(entry.session_id, start_ms, end_ms):
            text = str(row["user_message"] or "")
            if text.strip():
                turns.append(text)
        return turns

    def session_context_chars(self, session_id: str, start_ms: int, end_ms: int) -> dict[str, int]:
        counts = dict.fromkeys(_CONTEXT_KEYS, 0)
        entry = self._get(session_id)
        if entry is None:
            return counts
        reasoning_tokens = 0.0
        for row in self._windowed_events(entry.session_id, start_ms, end_ms):
            reasoning_tokens += _num(row["reasoning_tokens"])
        counts["reasoning"] = int(reasoning_tokens * 4)
        for row in self._windowed_turns(entry.session_id, start_ms, end_ms):
            counts["text"] += len(str(row["user_message"] or ""))
            counts["text"] += len(str(row["assistant_response"] or ""))
        for item in self._windowed_trajectory(entry.session_id, start_ms, end_ms):
            counts["tool_result"] += len(str(item.get("output") or ""))
        if self._has_table("session_files", optional=True):
            try:
                cols = {r[1] for r in self._conn.execute("PRAGMA table_info(session_files)")}
                if "file_path" in cols:
                    rows = self._conn.execute(
                        "SELECT file_path FROM session_files WHERE session_id = ?",
                        (entry.session_id,),
                    ).fetchall()
                    counts["file"] += sum(len(str(r[0] or "")) for r in rows)
            except sqlite3.Error:
                pass
        return counts

    def session_aggregates(self, session_id: str) -> dict | None:
        entry = self._get(session_id)
        if entry is None:
            return None
        try:
            row = self._conn.execute(
                "SELECT SUM(input_tokens), SUM(output_tokens), SUM(reasoning_tokens), "
                "SUM(cache_read_tokens), SUM(cache_write_tokens) "
                "FROM assistant_usage_events WHERE session_id = ?",
                (entry.session_id,),
            ).fetchone()
        except sqlite3.Error:
            return None
        if row is None:
            return None
        totals = [_num(v) for v in row]
        cost = entry.cost_estimate
        if sum(totals) <= 0 and cost is None:
            return None
        return {
            "cost": cost,
            "tokens_input": totals[0],
            "tokens_output": totals[1],
            "tokens_reasoning": totals[2],
            "tokens_cache_read": totals[3],
            "tokens_cache_write": totals[4],
        }

    def session_parts(self, session_id: str) -> list[PartRecord]:
        entry = self._get(session_id)
        if entry is None:
            return []
        parts: list[PartRecord] = []
        for row in self._windowed_turns(entry.session_id, 0, 2**63 - 1):
            ts = _parse_ts(row["timestamp"])
            if ts is None:
                continue
            user_text = str(row["user_message"] or "")
            if user_text.strip():
                parts.append(PartRecord(ts=ts, kind="user", text=user_text))
            assistant_text = str(row["assistant_response"] or "")
            if assistant_text.strip():
                parts.append(PartRecord(ts=ts, kind="assistant", text=assistant_text))
        for item in self._windowed_trajectory(entry.session_id, 0, 2**63 - 1):
            ts = _parse_ts(item.get("created_at")) if "created_at" in item else None
            if ts is None:
                ts = entry.updated or entry.created
            if ts is None:
                continue
            name = str(item.get("command") or item.get("event_type") or "unknown")
            parts.append(
                PartRecord(
                    ts=ts,
                    kind="tool",
                    tool_name=name,
                    tool_input=str(item.get("command") or "")[:2000],
                    tool_output=str(item.get("output") or "")[:2000] or None,
                )
            )
        parts.sort(key=lambda p: p.ts)
        return parts

    def find_session_by_title(self, title: str) -> HarnessSession | None:
        for entry in self._sessions.values():
            if entry.summary == title:
                return self._to_harness_session(entry)
        try:
            rows = self._conn.execute("SELECT session_id, user_message FROM turns").fetchall()
        except sqlite3.Error:
            return None
        for row in rows:
            if str(row["user_message"] or "")[:80] == title:
                entry = self._sessions.get(str(row["session_id"]))
                if entry is not None:
                    return self._to_harness_session(entry)
        return None

    def search_content(self, query: str, session_id: str | None = None) -> list[str]:
        """Recherche FTS interne sur ``search_index`` ; [] si indisponible."""
        if not self._has_table("search_index", optional=True):
            return []
        escaped = f'"{query.replace(chr(34), chr(34) * 2)}"'
        try:
            if session_id is not None:
                rows = self._conn.execute(
                    "SELECT session_id FROM search_index WHERE search_index MATCH ? "
                    "AND session_id = ?",
                    (escaped, self._unwrap(session_id)),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT session_id FROM search_index WHERE search_index MATCH ?",
                    (escaped,),
                ).fetchall()
        except sqlite3.Error:
            return []
        return [str(r[0]) for r in rows]

    def close(self) -> None:
        """Ferme la connexion RO (idempotent)."""
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(sqlite3.Error):
            self._conn.close()


def build_provider(source_cfg: dict, _cfg: TelemetryConfig) -> CopilotCliSessionProvider | None:
    """Factory registry : None si ``session-store.db`` absente (fail-soft)."""
    home = resolve_copilot_home(source_cfg)
    db_path = home / _DB_NAME
    if not db_path.is_file():
        return None
    try:
        conn = open_database(db_path)
    except (sqlite3.Error, OSError):
        return None
    try:
        return CopilotCliSessionProvider(home, conn)
    except Exception:
        with contextlib.suppress(sqlite3.Error):
            conn.close()
        return None
