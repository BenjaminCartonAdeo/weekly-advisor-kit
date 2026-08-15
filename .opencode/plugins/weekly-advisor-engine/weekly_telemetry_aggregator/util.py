"""Shared tiny helpers — ISO/UTC formatting, anchor parsing, JSON reads, robust z.

Single home for the one-liners previously copy-pasted across main/releases/
insights/report/writer (audit v6.0): behavior is byte-identical to the former
per-module definitions.
"""

from __future__ import annotations

import json
import statistics
from datetime import UTC, datetime
from pathlib import Path


def iso(dt: datetime) -> str:
    """UTC ISO-8601 with a Z suffix ("%Y-%m-%dT%H:%M:%SZ")."""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_anchor(value: str | None) -> datetime:
    """now(UTC) or ISO with Z→+00:00, UTC if naive."""
    if value is None:
        return datetime.now(UTC)
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def load_json(path: Path) -> dict | None:
    """Read a JSON dict; None on missing file or garbage (never raises)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def read_text(path: Path) -> str | None:
    """Read a text file; None on missing/unreadable (never raises)."""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def period_hours(start_iso: str, end_iso: str) -> float | None:
    """Window duration in hours from two ISO strings; None on garbage/absent."""
    try:
        return (
            datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
            - datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
        ).total_seconds() / 3600
    except (ValueError, TypeError):
        return None


def robust_z(values: list[float]) -> list[float]:
    """Robust z-scores (median + MAD, 0.6745 scale).

    MAD==0 → fall back to mean absolute deviation so a single spike among an
    otherwise-identical baseline is still detectable; still 0 → all zeros.
    """
    if not values:
        return []
    median = statistics.median(values)
    mad = statistics.median(sorted(abs(v - median) for v in values))
    if mad == 0:
        mad = sum(abs(v - median) for v in values) / len(values)
    if mad == 0:
        return [0.0] * len(values)
    return [0.6745 * (v - median) / mad for v in values]
