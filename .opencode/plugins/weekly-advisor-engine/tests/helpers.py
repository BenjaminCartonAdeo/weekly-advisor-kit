"""Shared builders for weekly-advisor tests (V1 sqlite seed + usage factories)."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from weekly_telemetry_aggregator.models import SessionUsage, StepFinish


def tzutc(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def make_step(
    session_id: str,
    ts: datetime,
    model: str = "anthropic/claude-x",
    *,
    cost: float | None = None,
    cache_read: float = 0,
    cache_write: float = 0,
    fresh: int = 100,
    out: int = 10,
    reason: int = 0,
) -> StepFinish:
    return StepFinish(
        session_id=session_id,
        timestamp=ts,
        model=model,
        tokens_input=float(fresh),
        tokens_output=float(out),
        tokens_reasoning=float(reason),
        tokens_cache_read=float(cache_read),
        tokens_cache_write=float(cache_write),
        cost=cost,
    )


def make_usage(
    sid: str,
    steps,
    *,
    parent: str | None = None,
    title: str | None = None,
    agent: str | None = None,
    tools: dict | None = None,
    skills: dict | None = None,
    user_turns: list[str] | None = None,
    context_chars: dict | None = None,
    first_user_text: str | None = None,
    partial: bool = False,
) -> SessionUsage:
    return SessionUsage(
        session_id=sid,
        title=title,
        parent_id=parent,
        agent_type=agent,
        steps=list(steps),
        tool_calls=tools or {},
        tool_arg_chars={t: 40 * c for t, c in (tools or {}).items()},
        skills_loaded=skills or {},
        user_turns=list(user_turns or []),
        context_chars=dict(context_chars or {}),
        first_user_text=first_user_text,
        partial=partial,
    )


def seed_v1_file(path, sessions, *, migrations: int = 42) -> sqlite3.Connection:
    """Create a V1 opencode.db with realistic sessions (V1Adapter schema, >= 42 migrations).

    Each session dict:
      id, parent?, title?, agent?, directory?, start, updated,
      steps: [{ts, cost?, input?, output?, reason?, cache_read?, cache_write?}],
      tools: [{ts, name, input, output}],
      texts: [{ts, text}],
      agg_cost?,                     # session_v2 aggregate for the cross-check
    Returns the (open) connection.
    """
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE session_v2 (
            id TEXT PRIMARY KEY, parent_id TEXT, title TEXT, model TEXT, agent TEXT,
            directory TEXT, cost REAL, tokens_input REAL, tokens_output REAL,
            tokens_reasoning REAL, tokens_cache_read REAL, tokens_cache_write REAL,
            time_created INTEGER, time_updated INTEGER
        );
        CREATE TABLE message (session_id TEXT, data TEXT, time_created INTEGER);
        CREATE TABLE part (session_id TEXT, type TEXT, data TEXT, time_created INTEGER);
        CREATE TABLE migration (id INTEGER PRIMARY KEY);
        """
    )
    conn.executemany("INSERT INTO migration (id) VALUES (?)", [(i,) for i in range(migrations)])
    model_json = json.dumps({"id": "claude-x", "providerID": "anthropic"})
    for s in sessions:
        sid = s["id"]
        cost = float(s.get("agg_cost", 0.0))
        conn.execute(
            "INSERT INTO session_v2 (id, parent_id, title, model, agent, directory, cost, tokens_input, "
            "tokens_output, tokens_reasoning, tokens_cache_read, tokens_cache_write, time_created, time_updated) "
            "VALUES (?,?,?,?,?,?,?,0,0,0,0,0,?,?)",
            (
                sid,
                s.get("parent"),
                s.get("title"),
                model_json,
                s.get("agent"),
                s.get("directory"),
                cost,
                _ms(s["start"]),
                _ms(s["updated"]),
            ),
        )
        for st in s.get("steps", []):
            conn.execute(
                "INSERT INTO part (session_id, data, time_created) VALUES (?, ?, ?)",
                (
                    sid,
                    json.dumps(
                        {
                            "type": "step-finish",
                            "cost": st.get("cost"),
                            "tokens": {
                                "input": st.get("input", 100),
                                "output": st.get("output", 10),
                                "reasoning": st.get("reason", 0),
                                "cache": {
                                    "read": st.get("cache_read", 0),
                                    "write": st.get("cache_write", 0),
                                },
                            },
                        }
                    ),
                    _ms(st["ts"]),
                ),
            )
        for t in s.get("tools", []):
            inp = t.get("input")
            conn.execute(
                "INSERT INTO part (session_id, data, time_created) VALUES (?, ?, ?)",
                (
                    sid,
                    json.dumps(
                        {
                            "type": "tool",
                            "state": {
                                "name": t["name"],
                                "input": inp,
                                "output": t.get("output", ""),
                            },
                        }
                    ),
                    _ms(t["ts"]),
                ),
            )
        for txt in s.get("texts", []):
            conn.execute(
                "INSERT INTO part (session_id, data, time_created) VALUES (?, ?, ?)",
                (sid, json.dumps({"type": "text", "text": txt["text"]}), _ms(txt["ts"])),
            )
        if s.get("assistant_msgs"):
            for a in s["assistant_msgs"]:
                data = {
                    "role": "assistant",
                    "modelID": "claude-x",
                    "providerID": "anthropic",
                    "tokens": {
                        "input": a.get("input", 100),
                        "output": a.get("output", 10),
                        "reasoning": a.get("reason", 0),
                        "cache": {"read": a.get("cache_read", 0), "write": a.get("cache_write", 0)},
                    },
                    "cost": a.get("cost"),
                }
                conn.execute(
                    "INSERT INTO message (session_id, data, time_created) VALUES (?, ?, ?)",
                    (sid, json.dumps(data), _ms(a["ts"])),
                )
    conn.commit()
    return conn


def seed_hybrid_file(path, sessions, *, migrations: int = 42) -> sqlite3.Connection:
    """Post-migration opencode.db: metadata in `session`, telemetry in `part`/`message`.

    Same session dict format as :func:`seed_v1_file`; also mirrors a stale
    `session_v2` row per session so detect_db has to prefer the live table.
    """
    conn = seed_v1_file(path, sessions, migrations=migrations)
    conn.executescript(
        """
        CREATE TABLE session (
            id TEXT PRIMARY KEY, parent_id TEXT, title TEXT, model TEXT, agent TEXT,
            directory TEXT, cost REAL, tokens_input REAL, tokens_output REAL,
            tokens_reasoning REAL, tokens_cache_read REAL, tokens_cache_write REAL,
            time_created INTEGER, time_updated INTEGER
        );
        """
    )
    model_json = json.dumps({"id": "claude-x", "providerID": "anthropic"})
    for s in sessions:
        conn.execute(
            "INSERT INTO session (id, parent_id, title, model, agent, directory, cost, tokens_input, "
            "tokens_output, tokens_reasoning, tokens_cache_read, tokens_cache_write, time_created, time_updated) "
            "VALUES (?,?,?,?,?,?,?,0,0,0,0,0,?,?)",
            (
                s["id"],
                s.get("parent"),
                s.get("title"),
                model_json,
                s.get("agent"),
                s.get("directory"),
                float(s.get("agg_cost", 0.0)),
                _ms(s["start"]),
                _ms(s["updated"]),
            ),
        )
    # Miroir V1 figé : le client ne met plus session_v2 à jour après migration (root cause).
    conn.execute("UPDATE session_v2 SET time_updated = time_updated - 86400000")
    conn.commit()
    return conn
