"""Direct SQLite access to the local OpenCode storage (Part 1 §2, v5.16+).

Transport: no SDK, no server. The CLI reads `opencode.db` (V1 schema) or
`opencode-next.db` (V2 schema) directly, read-only.

Schemas (verified août 2026, spec §7.1):
- OpenCode V1 (`opencode`, `opencode.db`): session metadata in `session`;
  telemetry in `part`/`message`; events in `event` keyed by `aggregate_id`
  (joined to `event_sequence`). `migration` (Drizzle) tracks the schema version.
- OpenCode V2 (`opencode2` / OpenCode 2.0, `opencode-next.db`): session metadata
  in `session_v2`; the same `part`/`message`/`event`(aggregate_id) layout;
  `data_migration` tracks the schema version.
- Real event shape is `aggregate_id`/`seq`/`type`/`data` on BOTH versions
  (never `session_id`/`time_created`). The unified OpenCodeAdapter accepts
  either session table and reads telemetry from `part`/`message`.

All queries are windowed per session (indexed), never full scans.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .models import StepFinish

#: Minimal Drizzle migration count for the V1 schema (schema pin, v5.27).
MIGRATION_MIN_V1 = 42

#: session metadata columns shared by `session` (V1) and `session_v2` (V2).
_SESSION_COLUMNS = [
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

#: OpenCodeAdapter SELECT list (metadata union of session/session_v2).
_META_COLS = (
    "id, parent_id, title, model, agent, directory, cost, tokens_input, "
    "tokens_output, tokens_reasoning, tokens_cache_read, tokens_cache_write, "
    "time_created, time_updated"
)


class SchemaError(Exception):
    """Schema does not match the expected adapter (blocking, exit 2)."""


class DataSourceError(Exception):
    """No usable OpenCode database found (blocking, exit 2).

    Carries the offending ``path`` and detected ``family`` so callers (doctor)
    can render an actionable, user-facing message instead of a generic string.
    """

    def __init__(self, message: str, *, path: str | None = None, family: str | None = None):
        super().__init__(message)
        self.path = path
        self.family = family


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


def _fingerprint(value) -> str:
    """Hash a canonical JSON representation without exposing tool payloads."""
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _tally_tools(records) -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
    """Accumulate tool-call counts, arg chars and loaded-skill counts from (name, input) pairs."""
    tool_calls: dict[str, int] = {}
    tool_arg_chars: dict[str, int] = {}
    skills: dict[str, int] = {}
    for name, raw_input, _raw_output in records:
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


def _tool_name_and_io(data):
    """Extract (name, input, output) from a tool part in EITHER OpenCode shape.

    Old client / benjamin: {"type":"tool","tool":<name>,"state":{"status","input","output"}}
    v1.18.19+: {"type":"tool-invocation","toolInvocation":{"state","toolName","args","result"}}
    """
    inv = data.get("toolInvocation")
    if isinstance(inv, dict):
        name = str(inv.get("toolName") or data.get("tool") or "unknown")
        return name, inv.get("args"), inv.get("result")
    state = data.get("state") if isinstance(data.get("state"), dict) else data
    name = str(data.get("tool") or state.get("name") or data.get("name") or "unknown")
    raw_input = state.get("input") if "input" in state else data.get("input")
    output = state.get("output") or state.get("result") or data.get("output")
    return name, raw_input, output


def _file_path(data):
    """Canonical file path from a file part in EITHER OpenCode shape.

    Old: {"type":"file","filePath":...,"path":...}
    v1.18.19+: {"type":"file","filename":...,"url":"file://...","source":{"text":{"path":...}}}
    """
    pth = data.get("filePath") or data.get("path")
    if pth:
        return str(pth)
    src = data.get("source")
    if isinstance(src, dict):
        st = src.get("text") if isinstance(src.get("text"), dict) else None
        if isinstance(st, dict) and st.get("path"):
            return str(st["path"])
    return str(data.get("filename") or data.get("url") or "")


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


class OpenCodeAdapter(SchemaAdapter):
    """Unified reader for OpenCode V1 and V2 (v6.x).

    OpenCode V1 (`opencode`, `opencode.db`) stores session metadata in `session`;
    OpenCode V2 (`opencode2` / OpenCode 2.0, `opencode-next.db`) stores it in
    `session_v2`. Both keep telemetry in `part`/`message` and events in `event`
    keyed by `aggregate_id` (joined to `event_sequence`) — NEVER `session_id`.

    We accept whichever session table(s) exist (session, session_v2, or both)
    and union them, so both the CLI and server/daemon session worlds are read.
    The migration count is read as an advisory signal only (no hard floor).
    """

    name = "opencode"

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._session_tables: list[str] = []

    def check_schema(self) -> None:
        tables = {
            r[0]
            for r in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        # session metadata: session (V1) and/or session_v2 (V2) — at least one.
        session_tables = [t for t in ("session_v2", "session") if t in tables]
        if not session_tables:
            raise SchemaError("missing tables: session/session_v2")
        # Telemetry is read from part/message (shared by both OpenCode versions).
        _require_tables(self.conn, ["part", "message"])
        _require_columns(self.conn, "part", ["session_id", "data", "time_created"])
        # NB: the real schema stores part.type inside data JSON, not in a column.
        _require_columns(self.conn, "message", ["session_id", "data", "time_created"])
        # migration tracking present (advisory; no hard version floor). The
        # adapter never reads it, so either table is accepted.
        if not ({"migration", "data_migration"} & tables):
            raise SchemaError("no migration table (migration/data_migration)")
        for st in session_tables:
            _require_columns(self.conn, st, _SESSION_COLUMNS)
        self._session_tables = session_tables

    def latest_updated_ms(self) -> int:
        if not self._session_tables:
            return 0
        sub = " UNION ALL ".join(f"SELECT time_updated FROM {t}" for t in self._session_tables)
        row = self.conn.execute(f"SELECT MAX(time_updated) AS m FROM ({sub})").fetchone()
        return int(row["m"] or 0)

    def list_sessions(self, since_ms: int) -> list[SessionMeta]:
        if not self._session_tables:
            return []
        query = " UNION ALL ".join(
            f"SELECT {_META_COLS} FROM {t} WHERE time_updated >= ?" for t in self._session_tables
        )
        rows = self.conn.execute(query, tuple([since_ms] * len(self._session_tables))).fetchall()
        seen: set[str] = set()
        metas: list[SessionMeta] = []
        for row in rows:  # time_updated DESC in each leg; dedupe keeps first
            sid = str(row["id"])
            if sid in seen:
                continue
            seen.add(sid)
            metas.append(_row_to_meta(row))
        return metas

    # ---- telemetry: part/message readers shared by V1 + V2 ----

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

    def _parts_of_type_all(
        self, session_id: str, start_ms: int, end_ms: int
    ) -> list[tuple[int, dict]]:
        """Every part row in window, for context/lint walks.

        Canonicalises the v1.18.19+ `tool-invocation` part type to `tool` so the
        rest of the reader stays shape-agnostic.
        """
        out = []
        for row in self.conn.execute(
            "SELECT data, time_created FROM part WHERE session_id = ? "
            "AND time_created >= ? AND time_created <= ? ORDER BY time_created",
            (session_id, start_ms, end_ms),
        ):
            data = _json_obj(row["data"])
            if data.get("type") == "tool-invocation":
                data["type"] = "tool"
            out.append((int(row["time_created"]), data))
        return out

    def _parts_of_type(
        self, session_id: str, ptype: str, start_ms: int, end_ms: int
    ) -> list[tuple[int, dict]]:
        """part rows whose data.type == ptype (the real schema keeps type inside data)."""
        return [
            (ts, data)
            for ts, data in self._parts_of_type_all(session_id, start_ms, end_ms)
            if data.get("type") == ptype
        ]

    def _step_finish_parts(
        self, session_id: str, start_ms: int, end_ms: int
    ) -> list[tuple[int, dict]]:
        return self._parts_of_type(session_id, "step-finish", start_ms, end_ms)

    def _step_start_parts(
        self, session_id: str, start_ms: int, end_ms: int
    ) -> list[tuple[int, dict]]:
        return self._parts_of_type(session_id, "step-start", start_ms, end_ms)

    def session_steps(self, session_id: str, start_ms: int, end_ms: int) -> list:
        # v1.18.19+ drops the `step-finish` part; steps are marked by `step-start`.
        step_parts = self._step_finish_parts(session_id, start_ms, end_ms)
        if not step_parts:
            step_parts = self._step_start_parts(session_id, start_ms, end_ms)
        return _steps_from_records(
            session_id,
            step_parts,
            self._assistant_messages(session_id, start_ms, end_ms),
        )

    def _tool_parts(self, session_id: str, start_ms: int, end_ms: int) -> list[tuple[int, dict]]:
        return self._parts_of_type(session_id, "tool", start_ms, end_ms)

    def session_tools(self, session_id: str, start_ms: int, end_ms: int) -> tuple[dict, dict, dict]:
        records = []
        for _ts, data in self._tool_parts(session_id, start_ms, end_ms):
            name, raw_input, _output = _tool_name_and_io(data)
            records.append((name, raw_input, _output))
        return _tally_tools(records)

    def session_tool_fingerprints(
        self, session_id: str, start_ms: int, end_ms: int
    ) -> tuple[dict[str, dict[str, int]], dict[str, dict[str, int]]]:
        """Return deterministic argument/result fingerprints, grouped by tool."""
        args: dict[str, dict[str, int]] = {}
        results: dict[str, dict[str, int]] = {}
        for _ts, data in self._tool_parts(session_id, start_ms, end_ms):
            name, raw_input, raw_output = _tool_name_and_io(data)
            arg_fp = _fingerprint(raw_input)
            args.setdefault(name, {})[arg_fp] = args.setdefault(name, {}).get(arg_fp, 0) + 1
            if raw_output is not None:
                result_fp = _fingerprint(raw_output)
                results.setdefault(name, {})[result_fp] = (
                    results.setdefault(name, {}).get(result_fp, 0) + 1
                )
        return args, results

    def session_user_turns(self, session_id: str, start_ms: int, end_ms: int) -> list[str]:
        turns: list[str] = []
        for _ts, data in self._parts_of_type(session_id, "text", start_ms, end_ms):
            text = data.get("text")
            if isinstance(text, str) and text.strip():
                turns.append(text)
        return turns

    def session_context_chars(self, session_id: str, start_ms: int, end_ms: int) -> dict[str, int]:
        counts = {"file": 0, "tool_result": 0, "text": 0, "reasoning": 0}
        for _ts, data in self._parts_of_type_all(session_id, start_ms, end_ms):
            ptype = data.get("type") or "text"
            if ptype == "file":
                counts["file"] += len(_file_path(data))
            elif ptype == "tool":
                _name, _inp, output = _tool_name_and_io(data)
                counts["tool_result"] += len(str(output)) if output is not None else 0
            elif ptype == "text":
                text = data.get("text")
                counts["text"] += len(str(text)) if isinstance(text, str) else 0
            elif ptype == "reasoning":
                text = data.get("text") or data.get("summary") or ""
                counts["reasoning"] += len(str(text)) if text is not None else 0
        return counts

    def session_aggregates(self, session_id: str) -> dict | None:
        for tbl in self._session_tables:
            agg = _session_aggregates(self.conn, tbl, session_id)
            if agg is not None:
                return agg
        return None

    def session_parts(self, session_id: str) -> list[PartRecord]:
        records: list[PartRecord] = []
        for row in self.conn.execute(
            "SELECT data, time_created FROM part WHERE session_id = ? ORDER BY time_created",
            (session_id,),
        ):
            data = _json_obj(row["data"])
            if data.get("type") == "tool-invocation":
                data["type"] = "tool"
            ptype = data.get("type") or "text"
            ts = _iso_from_ms(int(row["time_created"]))
            if ptype == "step-finish":
                records.append(PartRecord(ts=ts, kind="step-finish", cost=data.get("cost")))
            elif ptype == "tool":
                name, raw_input, output = _tool_name_and_io(data)
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
                records.append(PartRecord(ts=ts, kind="file", text=_file_path(data)))
        return records

    def find_session_by_title(self, title: str) -> SessionMeta | None:
        if not self._session_tables:
            return None
        query = " UNION ALL ".join(
            f"SELECT {_META_COLS} FROM {t} WHERE title = ?" for t in self._session_tables
        )
        rows = self.conn.execute(query, tuple([title] * len(self._session_tables))).fetchall()
        return _row_to_meta(rows[0]) if rows else None


def open_database(path: Path) -> sqlite3.Connection:
    """Open read-only (WAL files present) or query_only (never write)."""
    suffix = path.suffix
    wal = path.with_suffix(suffix + "-wal")
    shm = path.with_suffix(suffix + "-shm")
    if not (wal.exists() or shm.exists()):
        try:
            conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        except sqlite3.OperationalError:
            conn = sqlite3.connect(str(path), timeout=5)
            conn.execute("PRAGMA query_only=ON")
    else:
        conn = sqlite3.connect(str(path), timeout=5)
        conn.execute("PRAGMA query_only=ON")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _classify_db(conn: sqlite3.Connection) -> dict:
    """Best-effort signature of an OpenCode DB for actionable diagnostics.

    Names the schema family (v1 `session`, v2 `session_v2`, or unknown) and the
    migration counters so the doctor can render a precise, user-facing message
    instead of a generic "no valid database" (v6.x).
    """
    try:
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
    except sqlite3.Error:
        return {
            "tables": [],
            "family": "inaccessible",
            "session_tables": [],
            "event_shape": "?",
            "migration": None,
            "data_migration": None,
        }

    info: dict[str, object] = {
        "tables": sorted(tables),
        "family": "inconnu",
        "session_tables": [t for t in ("session_v2", "session") if t in tables],
        "event_shape": "?",
        "migration": None,
        "data_migration": None,
    }
    for tbl in ("migration", "data_migration"):
        if tbl in tables:
            try:
                info[tbl] = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
            except sqlite3.Error:
                info[tbl] = "?"

    if "event" in tables:
        ev = {r[1] for r in conn.execute("PRAGMA table_info(event)").fetchall()}
        if {"aggregate_id", "seq"} <= ev:
            info["event_shape"] = "aggregate_id/seq (courant)"
        elif "session_id" in ev:
            info["event_shape"] = "session_id (legacy?)"
        else:
            info["event_shape"] = "inconnu"

    if "session_v2" in tables and "session" in tables:
        info["family"] = "v1+v2 (session + session_v2)"
    elif "session_v2" in tables:
        info["family"] = "v2 (session_v2)"
    elif "session" in tables:
        info["family"] = "v1 (session)"
    elif tables:
        info["family"] = "schéma OpenCode non reconnu"
    return info


def detect_db(value: str) -> tuple[Path, SchemaAdapter]:
    """Auto-detect (or pin) the OpenCode DB and its schema adapter.

    `value == "auto"` → `<XDG_DATA_HOME>/opencode/` then `%LOCALAPPDATA%/opencode/`
    (Windows), each probed for `opencode.db` then `opencode-next.db`; candidates
    are deduplicated in stable order, and among schema-valid ones the one with
    the most recent `MAX(time_updated)` wins. No valid candidate →
    DataSourceError with an actionable, schema-aware message (v6.x).
    """
    if value != "auto":
        candidates = [Path(value).expanduser()]
    else:
        xdg = Path(os.environ.get("XDG_DATA_HOME", "~/.local/share")).expanduser()
        bases = [xdg / "opencode"]
        localappdata = os.environ.get("LOCALAPPDATA")
        if localappdata:
            bases.append(Path(localappdata) / "opencode")
        candidates = []
        seen: set[str] = set()
        for base in bases:
            for name in ("opencode.db", "opencode-next.db"):
                candidate = base / name
                if str(candidate) not in seen:
                    seen.add(str(candidate))
                    candidates.append(candidate)

    matches: list[tuple[int, Path, SchemaAdapter]] = []
    existing: list[Path] = []
    for path in candidates:
        if not path.is_file():
            continue
        existing.append(path)
        for adapter_cls in (OpenCodeAdapter,):
            conn = None
            try:
                conn = open_database(path)
                adapter = adapter_cls(conn)
                adapter.check_schema()
                matches.append((adapter.latest_updated_ms(), path, adapter))
            except (SchemaError, sqlite3.Error, OSError):
                if conn is not None:
                    conn.close()
    if matches:
        matches.sort(key=lambda m: -m[0])
        _latest, path, adapter = matches[0]
        for _ms, other_path, other in matches[1:]:
            if other_path != path:
                other.conn.close()
        return path, adapter

    # --- No usable candidate: build an actionable, schema-aware error. ---
    searched = ", ".join(str(p) for p in candidates)
    if not existing:
        raise DataSourceError(
            "Aucune base OpenCode trouvée aux emplacements testés "
            f"({searched}). Lancez 'opencode' ou 'opencode2' une fois pour "
            "initialiser la base, ou renseignez opencode_db_path dans "
            "weekly-telemetry-config.json.",
            path=None,
            family=None,
        )
    path = existing[0]
    try:
        probe = open_database(path)
        sig = _classify_db(probe)
        probe.close()
    except (sqlite3.Error, OSError) as exc:
        raise DataSourceError(
            f"Base trouvée en {path} mais illisible ({exc}). Vérifiez les "
            "permissions ou qu'OpenCode ne verrouille pas la base.",
            path=str(path),
            family=None,
        ) from exc
    sess = ", ".join(sig["session_tables"]) or "aucune"
    raise DataSourceError(
        f"Base OpenCode trouvée en {path} mais schéma non reconnu par le kit. "
        f"Famille: {sig['family']}. Tables session: {sess}. event: "
        f"{sig['event_shape']}. Migrations: migration={sig['migration']}, "
        f"data_migration={sig['data_migration']}. Le kit lit "
        f"session/session_v2 + part/message. Vérifiez que c'est une base OpenCode "
        f"(opencode.db V1 ou opencode-next.db V2) ; sinon pointez opencode_db_path "
        f"dessus.",
        path=str(path),
        family=sig["family"],
    )
