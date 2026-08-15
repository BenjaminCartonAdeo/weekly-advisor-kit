"""JSON serialization + atomic writes (Part 1 §4, Part 2, Part 6, artefacts).

Serialization is deterministic: dict insertion order follows the spec schemas,
lists are already sorted by their stable key upstream.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .models import WeeklySummary


def _iso(dt) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def summary_to_dict(summary: WeeklySummary) -> dict:
    """Convert WeeklySummary to the spec §4 JSON structure (schema_version 2)."""
    t = summary.totals
    return {
        "schema_version": 2,
        "period": {"start": _iso(summary.period.start), "end": _iso(summary.period.end)},
        "generated_at": _iso(summary.generated_at),
        "totals": {
            "session_count": t.session_count,
            "total_tokens": t.total_tokens,
            "total_cost_usd": t.total_cost_usd,
            "cache_read_tokens": t.cache_read_tokens,
            "cache_write_tokens": t.cache_write_tokens,
            "fresh_input_tokens": t.fresh_input_tokens,
            "output_tokens": t.output_tokens,
            "reasoning_tokens": t.reasoning_tokens,
            "cache_hit_rate": t.cache_hit_rate,
        },
        "daily_totals": [
            {
                "date": d.date,
                "cost_usd": d.cost_usd,
                "total_tokens": d.total_tokens,
                "cache_hit_rate": d.cache_hit_rate,
            }
            for d in summary.daily_totals
        ],
        "by_model": [
            {
                "model": m.model,
                "session_count": m.session_count,
                "total_tokens": m.total_tokens,
                "total_cost_usd": m.total_cost_usd,
                "cache_hit_rate": m.cache_hit_rate,
            }
            for m in summary.by_model
        ],
        "top_sessions_by_cost": [
            {
                "session_id": s.session_id,
                "title_or_topic": s.title_or_topic,
                "cost_usd": s.cost_usd,
                "reported_cost_usd_lifetime": s.reported_cost_usd_lifetime,
                "total_tokens": s.total_tokens,
                "project_path": s.project_path,
                "duration_seconds": s.duration_seconds,
                "active_time_seconds": s.active_time_seconds,
                "cost_per_active_minute": s.cost_per_active_minute,
                "api_call_count": s.api_call_count,
                "includes_subagents": s.includes_subagents,
                "cache_read_tokens": s.cache_read_tokens,
                "cache_write_tokens": s.cache_write_tokens,
                "cache_efficiency": s.cache_efficiency,
                "context_composition": dict(s.context_composition),
            }
            for s in summary.top_sessions_by_cost
        ],
        "cost_outliers": [
            {"session_id": o.session_id, "cost_usd": o.cost_usd, "z_score": o.z_score}
            for o in summary.cost_outliers
        ],
        "cost_outliers_state": summary.cost_outliers_state,
        "tool_usage": [
            {
                "tool": x.tool,
                "call_count": x.call_count,
                "estimated_input_tokens": x.estimated_tokens,
            }
            for x in summary.tool_usage
        ],
        "skill_usage": [
            {"skill": x.skill, "load_count": x.load_count, "sessions_used_in": x.sessions_used_in}
            for x in summary.skill_usage
        ],
        "command_usage": [
            {
                "command": x.command,
                "call_count": x.call_count,
                "sessions_used_in": x.sessions_used_in,
            }
            for x in summary.command_usage
        ],
        "skill_similar_pairs": [
            {"skills": list(p.skills), "similarity": p.similarity}
            for p in summary.skill_similar_pairs
        ],
        "skill_catalog_count": summary.skill_catalog_count,
        "skills_never_loaded": list(summary.skills_never_loaded),
        "skills_targets": summary.skills_targets,
        "user_prompt_repeats": [
            {
                "normalized_preview": r.normalized_preview,
                "count": r.count,
                "session_id": r.session_id,
                "avg_chars": r.avg_chars,
            }
            for r in summary.user_prompt_repeats
        ],
        "subagent_totals": {
            "child_session_count": summary.subagent_totals.child_session_count,
            "total_cost_usd": summary.subagent_totals.total_cost_usd,
            "by_agent_type": [
                {
                    "agent_type": a.agent_type,
                    "session_count": a.session_count,
                    "cost_usd": a.cost_usd,
                }
                for a in summary.subagent_totals.by_agent_type
            ],
        },
        "selection": summary.selection,
        "warnings": [
            {
                "session_id": w.session_id,
                "message": w.message,
                "partial": w.partial,
                "parts_cost": w.parts_cost,
                "session_v2_cost": w.session_v2_cost,
            }
            for w in summary.warnings
        ],
    }


def write_json_atomic(path: Path, data: dict) -> None:
    """Write JSON atomically: temp file in the same directory + os.replace.

    The spec imposes this recipe: an `open(cible, "w")` writes directly to the
    target — a crash mid-write leaves a truncated JSON that would break the
    next `insights` run. os.replace is atomic on the same filesystem, hence the
    .tmp in the same directory (never /tmp).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        Path(tmp).replace(path)
    finally:
        if Path(tmp).exists():
            Path(tmp).unlink()


def write_summary(path: Path, summary: WeeklySummary) -> None:
    """Write the summary file (final `weekly-summary-<date>.json`)."""
    write_json_atomic(path, summary_to_dict(summary))
