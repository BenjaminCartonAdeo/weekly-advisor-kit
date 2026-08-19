"""Self-cost of the pipeline run (Part 1 §12) — extracted from main (C14, v6.0.p).

``advisor_cost`` is shared by the CLI self-cost command and the report's
self-cost line: title lookup first, then the most recent weekly-advisor
agent session (v5.30 E).  Raises :class:`DataSourceError` when no DB exists.

Exit codes are imported from main so the CLI contract stays in one place
(main never imports this module — no cycle).
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime

from .config import TelemetryConfig
from .main import EXIT_OK, EXIT_PARTIAL, EXIT_TOTAL_FAILURE
from .sqlite_reader import DataSourceError, detect_db


def advisor_cost(cfg: TelemetryConfig) -> dict | None:
    """Advisor session info: cost, session_id, tokens; None when undetectable."""
    _path, adapter = detect_db(cfg.opencode_db_path)
    try:
        meta = adapter.find_session_by_title(cfg.advisor_run_title)
        if meta is None:
            # v5.30 (E) : fallback — session la plus récente de l'agent weekly-advisor
            # (le titre du run peut différer du advisor_run_title si le prompt cron change).
            best = None
            for m in adapter.list_sessions(0):
                if (
                    m.agent
                    and "weekly-advisor" in m.agent
                    and (
                        best is None
                        or (m.time_updated or datetime.min.replace(tzinfo=UTC))
                        > (best.time_updated or datetime.min.replace(tzinfo=UTC))
                    )
                ):
                    best = m
            meta = best
        if meta is None:
            return None
        agg = adapter.session_aggregates(meta.session_id)
        if agg is None:
            return None
        tokens = sum(
            float(agg.get(key) or 0.0)
            for key in (
                "tokens_input",
                "tokens_output",
                "tokens_reasoning",
                "tokens_cache_read",
                "tokens_cache_write",
            )
        )
        return {"cost": agg["cost"], "session_id": meta.session_id, "tokens": tokens}
    finally:
        adapter.conn.close()


def self_cost(cfg: TelemetryConfig, *, anchor: str | None = None) -> int:  # noqa: ARG001
    """Cost of the pipeline's own run session (Part 1 §12) — the one place the pipeline sees itself."""
    try:
        found = advisor_cost(cfg)
    except DataSourceError as exc:
        print(f"self-cost: FATAL: {exc}", file=sys.stderr, flush=True)
        return EXIT_TOTAL_FAILURE
    if found is None:
        print("self-cost: session du pipeline introuvable — coût propre non mesurable (0 $)")
        return EXIT_PARTIAL
    cost = float(found["cost"])
    session_id = str(found["session_id"])
    tokens = int(found.get("tokens") or 0)
    detail = f"session {session_id[:12]}"
    if tokens:
        detail += f", {tokens:,} tokens"
    print(f"self-cost: coût propre du pipeline: ${cost:.4f} ({detail})")
    return EXIT_OK
