"""Self-cost of the pipeline run (Part 1 §12) — extracted from main (C14, v6.0.p).

``advisor_cost`` is shared by the CLI self-cost command and the report's
self-cost line: title lookup first, then the most recent weekly-advisor agent
session (v5.30 E), toutes sources actives confondues.  Raises
:class:`DataSourceError` when no source is usable (no DB exists).

Multi-harnais : la recherche traverse tous les providers actifs
(`build_providers`) ; l'id retourné est canonique ``"<harness>:<id>"``.
Repli rétrocompatible : aucune source active → base OpenCode locale via
`detect_db` (le contrat DataSourceError est conservé).

Exit codes are imported from main so the CLI contract stays in one place
(main never imports this module — no cycle).
"""

from __future__ import annotations

import sys
import warnings
from datetime import UTC, datetime

from .config import TelemetryConfig
from .main import EXIT_OK, EXIT_PARTIAL, EXIT_TOTAL_FAILURE
from .providers import build_providers
from .sqlite_reader import DataSourceError, detect_db

_EPOCH = datetime.min.replace(tzinfo=UTC)


def advisor_cost(cfg: TelemetryConfig) -> dict | None:
    """Advisor session info: cost, session_id (canonique), tokens; None sinon."""
    providers = build_providers(cfg)
    if not providers:
        # Repli historique : DataSourceError préservée quand la base manque
        # (contrat consommé par self_cost et report._self_cost_value).
        warnings.warn(
            "aucune source de sessions active — repli sur la base OpenCode locale "
            f"({cfg.opencode_db_path})",
            stacklevel=2,
        )
        _path, adapter = detect_db(cfg.opencode_db_path)
        from .providers.implementations.opencode import OpenCodeSessionProvider

        providers = [OpenCodeSessionProvider(_path, adapter)]
    try:
        found = None
        for provider in providers:
            meta = provider.find_session_by_title(cfg.advisor_run_title)
            if meta is not None:
                found = meta
                break
        if found is None:
            # v5.30 (E) : fallback — session la plus récente de l'agent weekly-advisor,
            # toutes sources confondues (le titre du run peut différer du
            # advisor_run_title si le prompt cron change).
            for provider in providers:
                for m in provider.list_sessions(0):
                    if not (m.agent and "weekly-advisor" in m.agent):
                        continue
                    if found is None or (m.time_updated or _EPOCH) > (
                        found.time_updated or _EPOCH
                    ):
                        found = m
        if found is None:
            return None
        owner = next(p for p in providers if p.harness == found.harness)
        agg = owner.session_aggregates(found.session_id)
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
        return {"cost": agg["cost"], "session_id": found.session_id, "tokens": tokens}
    finally:
        for provider in providers:
            provider.close()


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
