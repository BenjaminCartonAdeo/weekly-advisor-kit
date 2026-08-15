"""Direct SQLite access to the local OpenCode storage (Part 1 §2, v5.16+).

Transport: no SDK, no server. The CLI reads `opencode.db` (V1 schema, living) or
`opencode-next.db` (V2 schema) directly, read-only.

Schemas (verified août 2026, spec §7.1):
- V1: session_v2 (metadata + aggregates), message (data JSON per message),
  part (data JSON: step-finish with cost + tokens.cache{read,write}; tool with
  name + state; text with data.text), migration (Drizzle, >= 42 rows).
- V2: session, session_message (typed: user -> data.text, assistant -> content[]),
  event (session.step.ended.1 -> cost + tokens.cache{read,write},
  session.tool.called.1 -> {name, input}), data_migration.

All queries are windowed per session (indexed), never full scans.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .models import StepFinish

#: Minimal Drizzle migration count for the V1 schema (schema pin, v5.27).
MIGRATION_MIN_V1 = 42

#: session_v2 metadata columns (V1 + dual schema checks).
_SESSION_V2_COLUMNS = [
    "id",
    "parent_id",
    "title",
    "model",
    "agent",
    "cost",
    "tokens_input",
    "tokens_output",
    "tokens_reasoning",
    "tokens_cache_read",
    "tokens_cache_write",
    "time_created",
    "time_updated",
]

#: DualAdapter SELECT list (metadata union of session/session_v2).
_META_COLS = (
    "id, parent_id, title, model, agent, directory, cost, tokens_input, "
    "tokens_output, tokens_reasoning, tokens_cache_read, tokens_cache_write, "
    "time_created, time_updated"
)


class SchemaError(Exception):
    """Schema does not match the expected adapter (blocking, exit 2)."""


class DataSourceError(Exception):
    """No usable OpenCode database found (blocking, exit 2)."""


def _iso_from_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, tz=UTC)


def _to_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _json_obj(raw) -> dict:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def model_key(model_raw) -> str:
    """Normalize a model JSON {id, providerID, variant} (or str/None) to `provider/model`.

    NULL or missing → "unknown/unknown" (spec §2).
    """
    obj = model_raw
    if isinstance(obj, str):
        stripped = obj.strip()
        if stripped.startswith("{"):  # session_v2.model stores a JSON string
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, dict):
                    obj = parsed
            except (ValueError, TypeError):
                pass
        if isinstance(obj, str):
            obj = {"id": obj}
    if not isinstance(obj, dict):
        return "unknown/unknown"
    model = str(obj.get("modelID") or obj.get("id") or obj.get("model") or "").strip()
    provider = str(obj.get("providerID") or obj.get("provider") or "").strip()
    if not model:
        return "unknown/unknown"
    if not provider:
        return f"unknown/{model}"
    return f"{provider}/{model}"


@dataclass(slots=True)
class SessionMeta:
    session_id: str
    title: str | None = None
    parent_id: str | None = None
    model_key: str = "unknown/unknown"
    agent: str | None = None
    directory: str | None = None
    cost: float | None = None
    tokens_input: float = 0.0
    tokens_output: float = 0.0
    tokens_reasoning: float = 0.0
    tokens_cache_read: float = 0.0
    tokens_cache_write: float = 0.0
    time_created: datetime | None = None
    time_updated: datetime | None = None


@dataclass(slots=True)
class PartRecord:
    """One normalized part of a session transcript (show-session, Partie 0 §3)."""

    ts: datetime
    kind: str  # user | assistant | tool | reasoning | file | step-finish
    text: str = ""
    tool_name: str | None = None
    tool_input: str | None = None
    tool_output: str | None = None
    cost: float | None = None


class SchemaAdapter:
    """Interface shared by V1/V2 adapters (all timestamps in ms)."""

    name: str = ""

    def check_schema(self) -> None:
        raise NotImplementedError

    def latest_updated_ms(self) -> int:
        raise NotImplementedError

    def list_sessions(self, since_ms: int) -> list[SessionMeta]:
        raise NotImplementedError

    def has_telemetry_rows(self, session_id: str) -> bool:
        raise NotImplementedError

    def session_steps(self, session_id: str, start_ms: int, end_ms: int) -> list:
        raise NotImplementedError

    def session_tools(self, session_id: str, start_ms: int, end_ms: int) -> tuple[dict, dict, dict]:
        raise NotImplementedError

    def session_user_turns(self, session_id: str, start_ms: int, end_ms: int) -> list[str]:
        raise NotImplementedError

    def session_context_chars(self, session_id: str, start_ms: int, end_ms: int) -> dict[str, int]:
        raise NotImplementedError

    def session_aggregates(self, session_id: str) -> dict | None:
        raise NotImplementedError

    def session_parts(self, session_id: str) -> list[PartRecord]:
        raise NotImplementedError

    def find_session_by_title(self, title: str) -> SessionMeta | None:
        raise NotImplementedError


def _require_tables(conn: sqlite3.Connection, tables: list[str]) -> None:
    found = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    missing = [t for t in tables if t not in found]
    if missing:
        raise SchemaError(f"missing tables: {', '.join(missing)}")


def _require_columns(conn: sqlite3.Connection, table: str, columns: list[str]) -> None:
    present = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    missing = [c for c in columns if c not in present]
    if missing:
        raise SchemaError(f"table {table} missing columns: {', '.join(missing)}")


def _row_to_meta(row: sqlite3.Row) -> SessionMeta:
    directory = row["directory"] or None
    if not directory and "cwd" in row.keys():  # noqa: SIM118 (in row testerait les valeurs)
        directory = row["cwd"]  # V2 rows expose cwd instead of directory
    return SessionMeta(
        session_id=str(row["id"]),
        title=row["title"] or None,
        parent_id=row["parent_id"] or None,
        model_key=model_key(row["model"]),
        agent=row["agent"] or None,
        directory=directory,
        cost=row["cost"] if row["cost"] is not None else None,
        tokens_input=float(row["tokens_input"] or 0.0),
        tokens_output=float(row["tokens_output"] or 0.0),
        tokens_reasoning=float(row["tokens_reasoning"] or 0.0),
        tokens_cache_read=float(row["tokens_cache_read"] or 0.0),
        tokens_cache_write=float(row["tokens_cache_write"] or 0.0),
        time_created=_iso_from_ms(row["time_created"]) if row["time_created"] else None,
        time_updated=_iso_from_ms(row["time_updated"]) if row["time_updated"] else None,
    )


def _tokens_of(data: dict) -> dict:
    t = data.get("tokens") or {}
    if not isinstance(t, dict):
        return {}
    cache = t.get("cache") or {}
    return {
        "input": float(t.get("input") or 0.0),
        "output": float(t.get("output") or 0.0),
        "reasoning": float(t.get("reasoning") or 0.0),
        "cache_read": float((cache.get("read") if isinstance(cache, dict) else 0) or 0.0),
        "cache_write": float((cache.get("write") if isinstance(cache, dict) else 0) or 0.0),
    }


def _tally_tools(records) -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
    """Accumulate tool-call counts, arg chars and loaded-skill counts from (name, input) pairs."""
    tool_calls: dict[str, int] = {}
    tool_arg_chars: dict[str, int] = {}
    skills: dict[str, int] = {}
    for name, raw_input in records:
        arg_chars = (
            len(json.dumps(raw_input, ensure_ascii=False, default=str))
            if raw_input is not None
            else 0
        )
        tool_calls[name] = tool_calls.get(name, 0) + 1
        tool_arg_chars[name] = tool_arg_chars.get(name, 0) + arg_chars
        if name in {"skill", "skills", "load-skill"}:
            inp = raw_input if isinstance(raw_input, dict) else {}
            skill = str(inp.get("name") or inp.get("skill") or "unknown")
            skills[skill] = skills.get(skill, 0) + 1
    return tool_calls, tool_arg_chars, skills


def _merge_step_records(records, messages) -> list[tuple[int, dict, str, float | None]]:
    """[(ts, tokens, model, cost)] — each step record merged with its closest assistant
    message (two-pointer, tie → earlier); untouched when no message is near."""
    merged: list[tuple[int, dict, str, float | None]] = []
    mi = 0
    for ts, data in records:
        tokens = _tokens_of(data)
        cost = data.get("cost")
        model = "unknown/unknown"
        while mi < len(messages) and messages[mi][0] < ts:
            mi += 1
        candidate = None
        if mi > 0:
            candidate = messages[mi - 1]
        if mi < len(messages):
            nxt = messages[mi]
            if candidate is None or (nxt[0] - ts) < (ts - candidate[0]):
                candidate = nxt
        if candidate is not None:
            mdata = candidate[1]
            mtokens = _tokens_of(mdata)
            if mdata.get("modelID"):
                model = model_key(mdata)
            elif mdata.get("model") is not None:
                model = model_key(mdata.get("model"))
            tokens = {
                "input": mtokens["input"] or tokens["input"],
                "output": mtokens["output"] or tokens["output"],
                "reasoning": mtokens["reasoning"] or tokens["reasoning"],
                "cache_read": tokens["cache_read"] or mtokens["cache_read"],
                "cache_write": tokens["cache_write"] or mtokens["cache_write"],
            }
            if cost is None:
                cost = mdata.get("cost")
        merged.append((ts, tokens, model, cost))
    return merged


def _steps_from_records(session_id: str, records, messages) -> list[StepFinish]:
    """StepFinish list from step-finish records + assistant messages (V1/V2 adapters).

    Records present → per-record two-pointer merge with the closest message;
    absent (older rows) → assistant messages are the steps.
    """
    if records:
        merged = _merge_step_records(records, messages)
    else:
        merged = [
            (ts, _tokens_of(data), model_key(data), data.get("cost")) for ts, data in messages
        ]
    return [
        StepFinish(
            session_id=session_id,
            timestamp=_iso_from_ms(ts),
            model=model,
            tokens_input=tokens["input"],
            tokens_output=tokens["output"],
            tokens_reasoning=tokens["reasoning"],
            tokens_cache_read=tokens["cache_read"],
            tokens_cache_write=tokens["cache_write"],
            cost=cost,
        )
        for ts, tokens, model, cost in merged
    ]


def _session_aggregates(conn: sqlite3.Connection, table: str, session_id: str) -> dict | None:
    """Session aggregate totals from one metadata table (cost-null → None)."""
    row = conn.execute(
        f"SELECT cost, tokens_input, tokens_output, tokens_reasoning, "
        f"tokens_cache_read, tokens_cache_write FROM {table} WHERE id = ?",
        (session_id,),
    ).fetchone()
    if row is None or row["cost"] is None:
        return None
    return {
        "cost": float(row["cost"]),
        "tokens_input": float(row["tokens_input"] or 0.0),
        "tokens_output": float(row["tokens_output"] or 0.0),
        "tokens_reasoning": float(row["tokens_reasoning"] or 0.0),
        "tokens_cache_read": float(row["tokens_cache_read"] or 0.0),
        "tokens_cache_write": float(row["tokens_cache_write"] or 0.0),
    }


class V1Adapter(SchemaAdapter):
    """opencode.db — session_v2 / message / part / migration (Drizzle, >= 42)."""

    name = "v1"

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def check_schema(self) -> None:
        _require_tables(self.conn, ["session_v2", "message", "part", "migration"])
        _require_columns(self.conn, "session_v2", _SESSION_V2_COLUMNS)
        _require_columns(self.conn, "part", ["session_id", "data", "time_created"])
        # NB: the real schema stores part.type inside data JSON, not in a column.
        _require_columns(self.conn, "message", ["session_id", "data", "time_created"])
        count = self.conn.execute("SELECT COUNT(*) FROM migration").fetchone()[0]
        if count < MIGRATION_MIN_V1:
            raise SchemaError(f"migration count {count} < {MIGRATION_MIN_V1} (V1 schema pin)")

    def latest_updated_ms(self) -> int:
        row = self.conn.execute("SELECT MAX(time_updated) AS t FROM session_v2").fetchone()
        return int(row["t"] or 0)

    def list_sessions(self, since_ms: int) -> list[SessionMeta]:
        rows = self.conn.execute(
            "SELECT * FROM session_v2 WHERE time_updated >= ? ORDER BY time_updated DESC",
            (since_ms,),
        ).fetchall()
        return [_row_to_meta(r) for r in rows]

    def has_telemetry_rows(self, session_id: str) -> bool:
        row = self.conn.execute(
            "SELECT EXISTS(SELECT 1 FROM part WHERE session_id=? LIMIT 1) AS p, "
            "EXISTS(SELECT 1 FROM message WHERE session_id=? LIMIT 1) AS m",
            (session_id, session_id),
        ).fetchone()
        return bool(row and (row["p"] or row["m"]))

    def _assistant_messages(
        self, session_id: str, start_ms: int, end_ms: int
    ) -> list[tuple[int, dict]]:
        """[(time_created_ms, data)] for assistant messages in window (data JSON)."""
        out: list[tuple[int, dict]] = []
        for row in self.conn.execute(
            "SELECT data, time_created FROM message WHERE session_id = ? AND time_created >= ? "
            "AND time_created <= ? ORDER BY time_created",
            (session_id, start_ms, end_ms),
        ):
            data = _json_obj(row["data"])
            if str(data.get("role") or "assistant") == "assistant":
                out.append((int(row["time_created"]), data))
        return out

    def _parts_of_type(
        self, session_id: str, ptype: str, start_ms: int, end_ms: int
    ) -> list[tuple[int, dict]]:
        """part rows whose data.type == ptype (the real V1 schema keeps type inside data)."""
        return [
            (ts, data)
            for ts, data in self._parts_of_type_all(session_id, start_ms, end_ms)
            if data.get("type") == ptype
        ]

    def _step_finish_parts(
        self, session_id: str, start_ms: int, end_ms: int
    ) -> list[tuple[int, dict]]:
        return self._parts_of_type(session_id, "step-finish", start_ms, end_ms)

    def session_steps(self, session_id: str, start_ms: int, end_ms: int) -> list:
        return _steps_from_records(
            session_id,
            self._step_finish_parts(session_id, start_ms, end_ms),
            self._assistant_messages(session_id, start_ms, end_ms),
        )

    def _tool_parts(self, session_id: str, start_ms: int, end_ms: int) -> list[tuple[int, dict]]:
        return self._parts_of_type(session_id, "tool", start_ms, end_ms)

    def session_tools(self, session_id: str, start_ms: int, end_ms: int) -> tuple[dict, dict, dict]:
        records = []
        for _ts, data in self._tool_parts(session_id, start_ms, end_ms):
            state = data.get("state") if isinstance(data.get("state"), dict) else data
            name = str(data.get("tool") or state.get("name") or data.get("name") or "unknown")
            raw_input = state.get("input") if "input" in state else data.get("input")
            records.append((name, raw_input))
        return _tally_tools(records)

    def session_user_turns(self, session_id: str, start_ms: int, end_ms: int) -> list[str]:
        turns: list[str] = []
        for _ts, data in self._parts_of_type(session_id, "text", start_ms, end_ms):
            text = data.get("text")
            if isinstance(text, str) and text.strip():
                turns.append(text)
        return turns

    def _parts_of_type_all(
        self, session_id: str, start_ms: int, end_ms: int
    ) -> list[tuple[int, dict]]:
        """Every part row in window (data.type kept), for context/lint walks."""
        return [
            (int(row["time_created"]), _json_obj(row["data"]))
            for row in self.conn.execute(
                "SELECT data, time_created FROM part WHERE session_id = ? "
                "AND time_created >= ? AND time_created <= ? ORDER BY time_created",
                (session_id, start_ms, end_ms),
            )
        ]

    def session_context_chars(self, session_id: str, start_ms: int, end_ms: int) -> dict[str, int]:
        counts = {"file": 0, "tool_result": 0, "text": 0, "reasoning": 0}
        for _ts, data in self._parts_of_type_all(session_id, start_ms, end_ms):
            ptype = data.get("type") or "text"
            if ptype == "file":
                path = data.get("filePath") or data.get("path") or data.get("file") or ""
                counts["file"] += len(str(path))
            elif ptype == "tool":
                state = data.get("state") if isinstance(data.get("state"), dict) else data
                output = (
                    state.get("output")
                    or state.get("result")
                    or data.get("output")
                    or data.get("result")
                    or ""
                )
                counts["tool_result"] += len(str(output)) if output is not None else 0
            elif ptype == "text":
                text = data.get("text")
                counts["text"] += len(str(text)) if isinstance(text, str) else 0
            elif ptype == "reasoning":
                text = data.get("text") or data.get("summary") or ""
                counts["reasoning"] += len(str(text)) if text is not None else 0
        return counts

    def session_aggregates(self, session_id: str) -> dict | None:
        return _session_aggregates(self.conn, "session_v2", session_id)

    def session_parts(self, session_id: str) -> list[PartRecord]:
        records: list[PartRecord] = []
        for row in self.conn.execute(
            "SELECT data, time_created FROM part WHERE session_id = ? ORDER BY time_created",
            (session_id,),
        ):
            data = _json_obj(row["data"])
            ptype = data.get("type") or "text"
            ts = _iso_from_ms(int(row["time_created"]))
            if ptype == "step-finish":
                records.append(PartRecord(ts=ts, kind="step-finish", cost=data.get("cost")))
            elif ptype == "tool":
                state = data.get("state") if isinstance(data.get("state"), dict) else data
                name = str(data.get("tool") or state.get("name") or data.get("name") or "unknown")
                raw_input = state.get("input") if "input" in state else data.get("input")
                output = state.get("output") or state.get("result") or data.get("output") or ""
                records.append(
                    PartRecord(
                        ts=ts,
                        kind="tool",
                        tool_name=name,
                        tool_input=json.dumps(raw_input, ensure_ascii=False, default=str)[:2000]
                        if raw_input is not None
                        else "",
                        tool_output=str(output)[:2000] if output is not None else "",
                    )
                )
            elif ptype == "text":
                records.append(PartRecord(ts=ts, kind="user", text=str(data.get("text") or "")))
            elif ptype == "reasoning":
                records.append(
                    PartRecord(
                        ts=ts,
                        kind="reasoning",
                        text=str(data.get("text") or data.get("summary") or ""),
                    )
                )
            elif ptype == "file":
                path = data.get("filePath") or data.get("path") or ""
                records.append(PartRecord(ts=ts, kind="file", text=str(path)))
        return records

    def find_session_by_title(self, title: str) -> SessionMeta | None:
        row = self.conn.execute(
            "SELECT * FROM session_v2 WHERE title = ? ORDER BY time_updated DESC LIMIT 1",
            (title,),
        ).fetchone()
        return _row_to_meta(row) if row else None


class DualAdapter(V1Adapter):
    """Metadata from `session` ∪ `session_v2`, telemetry from `part`/`message` (v5.29).

    Root cause fix: the client keeps TWO live metadata stores — CLI sessions
    (`opencode run`/TUI) live in `session` (+ `part`/`message` telemetry),
    server/demon sessions live in `session_v2` (no persisted parts → `unflushed`).
    Picking either table alone hides the other world's week. This adapter unions
    both (dedup by id, freshest row wins) and reuses V1 telemetry readers.
    """

    name = "v1-dual"

    def check_schema(self) -> None:
        _require_tables(self.conn, ["session", "session_v2", "part", "message", "migration"])
        _require_columns(self.conn, "session_v2", _SESSION_V2_COLUMNS)
        _require_columns(
            self.conn,
            "session",
            ["id", "parent_id", "title", "model", "agent", "cost", "time_created", "time_updated"],
        )
        _require_columns(self.conn, "part", ["session_id", "data", "time_created"])
        _require_columns(self.conn, "message", ["session_id", "data", "time_created"])
        count = self.conn.execute("SELECT COUNT(*) FROM migration").fetchone()[0]
        if count < MIGRATION_MIN_V1:
            raise SchemaError(f"migration count {count} < {MIGRATION_MIN_V1} (v1-dual pin)")

    def latest_updated_ms(self) -> int:
        row = self.conn.execute(
            "SELECT MAX(t) AS m FROM ("
            "SELECT MAX(time_updated) AS t FROM session "
            "UNION ALL SELECT MAX(time_updated) FROM session_v2)"
        ).fetchone()
        return int(row["m"] or 0)

    def list_sessions(self, since_ms: int) -> list[SessionMeta]:
        cols = _META_COLS
        rows = self.conn.execute(
            f"SELECT {cols} FROM ("
            f"SELECT {cols} FROM session WHERE time_updated >= ? "
            f"UNION ALL "
            f"SELECT {cols} FROM session_v2 WHERE time_updated >= ?) "
            "ORDER BY time_updated DESC",
            (since_ms, since_ms),
        ).fetchall()
        seen: set[str] = set()
        metas: list[SessionMeta] = []
        for row in rows:  # time_updated DESC → première occurrence = la plus fraîche
            sid = str(row["id"])
            if sid in seen:
                continue
            seen.add(sid)
            metas.append(_row_to_meta(row))
        return metas

    def session_aggregates(self, session_id: str) -> dict | None:
        for tbl in ("session", "session_v2"):
            agg = _session_aggregates(self.conn, tbl, session_id)
            if agg is not None:
                return agg
        return None

    def find_session_by_title(self, title: str) -> SessionMeta | None:
        cols = _META_COLS
        rows = self.conn.execute(
            f"SELECT {cols} FROM ("
            f"SELECT {cols} FROM session WHERE title = ? "
            f"UNION ALL "
            f"SELECT {cols} FROM session_v2 WHERE title = ?) "
            "ORDER BY time_updated DESC LIMIT 1",
            (title, title),
        ).fetchall()
        return _row_to_meta(rows[0]) if rows else None


class V2Adapter(SchemaAdapter):
    """opencode-next.db — session / session_message / event / data_migration."""

    name = "v2"

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def check_schema(self) -> None:
        _require_tables(self.conn, ["session", "session_message", "event", "data_migration"])
        _require_columns(
            self.conn,
            "session",
            [
                "id",
                "parent_id",
                "title",
                "model",
                "agent",
                "cost",
                "tokens_input",
                "tokens_output",
                "tokens_reasoning",
                "tokens_cache_read",
                "tokens_cache_write",
                "time_created",
                "time_updated",
            ],
        )
        _require_columns(
            self.conn, "session_message", ["session_id", "type", "data", "time_created"]
        )
        _require_columns(self.conn, "event", ["session_id", "type", "data", "time_created"])

    def latest_updated_ms(self) -> int:
        row = self.conn.execute("SELECT MAX(time_updated) AS t FROM session").fetchone()
        return int(row["t"] or 0)

    def list_sessions(self, since_ms: int) -> list[SessionMeta]:
        rows = self.conn.execute(
            "SELECT * FROM session WHERE time_updated >= ? ORDER BY time_updated DESC",
            (since_ms,),
        ).fetchall()
        return [_row_to_meta(r) for r in rows]

    def has_telemetry_rows(self, session_id: str) -> bool:
        row = self.conn.execute(
            "SELECT EXISTS(SELECT 1 FROM session_message WHERE session_id=? LIMIT 1) AS p, "
            "EXISTS(SELECT 1 FROM event WHERE session_id=? LIMIT 1) AS m",
            (session_id, session_id),
        ).fetchone()
        return bool(row and (row["p"] or row["m"]))

    def _assistant_messages(
        self, session_id: str, start_ms: int, end_ms: int
    ) -> list[tuple[int, dict]]:
        out: list[tuple[int, dict]] = []
        for row in self.conn.execute(
            "SELECT data, time_created FROM session_message WHERE session_id = ? AND type = 'assistant' "
            "AND time_created >= ? AND time_created <= ? ORDER BY time_created",
            (session_id, start_ms, end_ms),
        ):
            out.append((int(row["time_created"]), _json_obj(row["data"])))
        return out

    def _step_events(self, session_id: str, start_ms: int, end_ms: int) -> list[tuple[int, dict]]:
        out: list[tuple[int, dict]] = []
        for row in self.conn.execute(
            "SELECT data, time_created FROM event WHERE session_id = ? AND type = 'session.step.ended.1' "
            "AND time_created >= ? AND time_created <= ? ORDER BY time_created",
            (session_id, start_ms, end_ms),
        ):
            out.append((int(row["time_created"]), _json_obj(row["data"])))
        return out

    def session_steps(self, session_id: str, start_ms: int, end_ms: int) -> list:
        return _steps_from_records(
            session_id,
            self._step_events(session_id, start_ms, end_ms),
            self._assistant_messages(session_id, start_ms, end_ms),
        )

    def _tool_events(self, session_id: str, start_ms: int, end_ms: int) -> list[tuple[int, dict]]:
        out: list[tuple[int, dict]] = []
        for row in self.conn.execute(
            "SELECT data, time_created FROM event WHERE session_id = ? AND type = 'session.tool.called.1' "
            "AND time_created >= ? AND time_created <= ? ORDER BY time_created",
            (session_id, start_ms, end_ms),
        ):
            out.append((int(row["time_created"]), _json_obj(row["data"])))
        return out

    def session_tools(self, session_id: str, start_ms: int, end_ms: int) -> tuple[dict, dict, dict]:
        return _tally_tools(
            (str(data.get("name") or "unknown"), data.get("input"))
            for _ts, data in self._tool_events(session_id, start_ms, end_ms)
        )

    def session_user_turns(self, session_id: str, start_ms: int, end_ms: int) -> list[str]:
        turns: list[str] = []
        for row in self.conn.execute(
            "SELECT data FROM session_message WHERE session_id = ? AND type = 'user' "
            "AND time_created >= ? AND time_created <= ? ORDER BY time_created",
            (session_id, start_ms, end_ms),
        ):
            data = _json_obj(row["data"])
            text = data.get("text") if isinstance(data.get("text"), str) else None
            if text is None and isinstance(data.get("text"), dict):
                text = data["text"].get("text")
            if isinstance(text, str) and text.strip():
                turns.append(text)
        return turns

    def session_context_chars(self, session_id: str, start_ms: int, end_ms: int) -> dict[str, int]:
        counts = {"file": 0, "tool_result": 0, "text": 0, "reasoning": 0}
        for _ts, data in self._assistant_messages(session_id, start_ms, end_ms):
            for block in data.get("content") or []:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "file":
                    path = block.get("filePath") or block.get("path") or ""
                    counts["file"] += len(str(path))
                elif btype == "tool":
                    output = block.get("output") or block.get("result") or ""
                    counts["tool_result"] += len(str(output)) if output is not None else 0
                elif btype == "text":
                    counts["text"] += len(str(block.get("text") or ""))
                elif btype == "reasoning":
                    counts["reasoning"] += len(str(block.get("text") or ""))
        for _ts, data in self._tool_events(session_id, start_ms, end_ms):
            raw_input = data.get("input")
            counts["tool_result"] += (
                len(json.dumps(raw_input, ensure_ascii=False, default=str))
                if raw_input is not None
                else 0
            )
        return counts

    def session_aggregates(self, session_id: str) -> dict | None:
        return _session_aggregates(self.conn, "session", session_id)

    def session_parts(self, session_id: str) -> list[PartRecord]:
        records: list[PartRecord] = []
        for row in self.conn.execute(
            "SELECT type, data, time_created FROM session_message WHERE session_id = ? ORDER BY time_created",
            (session_id,),
        ):
            mtype = row["type"]
            data = _json_obj(row["data"])
            ts = _iso_from_ms(int(row["time_created"]))
            if mtype == "user":
                text = data.get("text") if isinstance(data.get("text"), str) else None
                if text is None and isinstance(data.get("text"), dict):
                    text = data["text"].get("text")
                records.append(PartRecord(ts=ts, kind="user", text=str(text or "")))
            elif mtype == "assistant":
                for block in data.get("content") or []:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "text":
                        records.append(
                            PartRecord(ts=ts, kind="assistant", text=str(block.get("text") or ""))
                        )
                    elif btype == "file":
                        records.append(
                            PartRecord(ts=ts, kind="file", text=str(block.get("filePath") or ""))
                        )
                    elif btype == "tool":
                        records.append(
                            PartRecord(
                                ts=ts,
                                kind="tool",
                                tool_name=str(block.get("name") or "unknown"),
                                tool_input=json.dumps(
                                    block.get("input"), ensure_ascii=False, default=str
                                )[:2000],
                                tool_output=str(block.get("output") or "")[:2000],
                            )
                        )
                    elif btype == "reasoning":
                        records.append(
                            PartRecord(ts=ts, kind="reasoning", text=str(block.get("text") or ""))
                        )
        for row in self.conn.execute(
            "SELECT type, data, time_created FROM event WHERE session_id = ? ORDER BY time_created",
            (session_id,),
        ):
            etype = row["type"]
            data = _json_obj(row["data"])
            ts = _iso_from_ms(int(row["time_created"]))
            if etype == "session.step.ended.1":
                records.append(PartRecord(ts=ts, kind="step-finish", cost=data.get("cost")))
            elif etype == "session.tool.called.1":
                records.append(
                    PartRecord(
                        ts=ts,
                        kind="tool",
                        tool_name=str(data.get("name") or "unknown"),
                        tool_input=json.dumps(data.get("input"), ensure_ascii=False, default=str)[
                            :2000
                        ],
                    )
                )
        records.sort(key=lambda r: r.ts)
        return records

    def find_session_by_title(self, title: str) -> SessionMeta | None:
        row = self.conn.execute(
            "SELECT * FROM session WHERE title = ? ORDER BY time_updated DESC LIMIT 1",
            (title,),
        ).fetchone()
        return _row_to_meta(row) if row else None


def open_database(path: Path) -> sqlite3.Connection:
    """Open read-only (WAL files present) or query_only (never write)."""
    suffix = path.suffix
    wal = path.with_suffix(suffix + "-wal")
    shm = path.with_suffix(suffix + "-shm")
    if not (wal.exists() or shm.exists()):
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        except sqlite3.OperationalError:
            conn = sqlite3.connect(str(path), timeout=5)
            conn.execute("PRAGMA query_only=ON")
    else:
        conn = sqlite3.connect(str(path), timeout=5)
        conn.execute("PRAGMA query_only=ON")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


#: Tie-break priority at equal MAX(time_updated): live/event-sourced adapters
#: beat the stale mirror. The client updates `session_v2` AND `session` in sync
#: for active sessions → naive sort would keep V1 (first) and read the mirror,
#: hiding every session that only exists in `session` (v5.29, root cause of
#: "0 session sur une semaine").
_ADAPTER_PRIORITY = {"v2": 0, "v1-dual": 1, "v1": 2}


def detect_db(value: str) -> tuple[Path, SchemaAdapter]:
    """Auto-detect (or pin) the OpenCode DB and its schema adapter (v5.24).

    `value == "auto"` → `<XDG_DATA_HOME>/opencode/opencode.db` then
    `opencode-next.db`; among schema-valid candidates the one with the most
    recent `MAX(time_updated)` wins, ties broken by live-adapter priority
    (v2 > v1-dual > v1). No valid candidate → DataSourceError.
    """
    if value != "auto":
        candidates = [Path(value).expanduser()]
    else:
        xdg = Path(os.environ.get("XDG_DATA_HOME", "~/.local/share")).expanduser()
        base = xdg / "opencode"
        candidates = [base / "opencode.db", base / "opencode-next.db"]

    matches: list[tuple[int, Path, SchemaAdapter]] = []
    for path in candidates:
        if not path.is_file():
            continue
        for adapter_cls in (V1Adapter, V2Adapter, DualAdapter):
            conn = None
            try:
                conn = open_database(path)
                adapter = adapter_cls(conn)
                adapter.check_schema()
                matches.append((adapter.latest_updated_ms(), path, adapter))
            except (SchemaError, sqlite3.Error, OSError):
                if conn is not None:
                    conn.close()
    if not matches:
        searched = ", ".join(str(p) for p in candidates)
        raise DataSourceError(f"no valid OpenCode database found (searched: {searched})")
    # latest DESC, puis priorité ASC (v2 > v1-dual > v1-live > v1) — un reverse=True
    # inverserait aussi la priorité et laisserait le miroir gagner l'égalité.
    matches.sort(key=lambda m: (-m[0], _ADAPTER_PRIORITY.get(m[2].name, 3)))
    _latest, path, adapter = matches[0]
    for _ms, other_path, other in matches[1:]:
        if other_path != path:
            other.conn.close()
    return path, adapter
