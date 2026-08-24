"""JSON serialization + atomic writes (Part 1 §4, Part 2, Part 6, artefacts).

Serialization is deterministic: dict insertion order follows the spec schemas,
lists are already sorted by their stable key upstream.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path

from .models import WeeklySummary
from .util import iso as _iso


def summary_to_dict(summary: WeeklySummary) -> dict:
    """Convert WeeklySummary to the spec §4 JSON structure (schema_version 2).

    Built on dataclasses.asdict (field names/order already match the spec
    schemas); only the spec's deviations are adjusted afterwards.
    """
    data = {"schema_version": 2, **asdict(summary)}
    data.pop("skill_catalog_source", None)  # internal field, not part of spec §4
    if data.get("cost_estimates") is None:
        data.pop("cost_estimates", None)  # optional field: key absent when nothing to estimate
    for item in data["tool_usage"]:
        item["estimated_input_tokens"] = item.pop("estimated_tokens")  # spec key name
    data["period"]["start"] = _iso(summary.period.start)
    data["period"]["end"] = _iso(summary.period.end)
    data["generated_at"] = _iso(summary.generated_at)
    return data


def write_json_atomic(path: Path, data: dict, *, indent: int | None = 2) -> None:
    """Write JSON atomically: temp file in the same directory + os.replace.

    The spec imposes this recipe: an `open(cible, "w")` writes directly to the
    target — a crash mid-write leaves a truncated JSON that would break the
    next `insights` run. os.replace is atomic on the same filesystem, hence the
    .tmp in the same directory (never /tmp).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=indent, ensure_ascii=False) + "\n"
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
