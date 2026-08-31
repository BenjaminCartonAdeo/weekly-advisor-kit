"""Tests du parsing config (_parse) — validation fail-soft des chemins."""

from __future__ import annotations

import json
import warnings
from pathlib import Path

import pytest

from weekly_telemetry_aggregator.config import (
    AuditConfig,
    DraftTargetsConfig,
    HarnessIncludeConfig,
    InsightsConfig,
    WatchDistillConfig,
    _parse,
)


def test_focused_config_sections_are_frozen():
    sections = (
        (AuditConfig(), "cost_per_active_minute_min"),
        (InsightsConfig(), "weekly_budget_usd"),
        (DraftTargetsConfig(), "mode"),
        (HarnessIncludeConfig(), "default_profile"),
        (WatchDistillConfig(), "top_n"),
    )
    for section, field_name in sections:
        with pytest.raises(AttributeError):
            setattr(section, field_name, None)


def _config_file(tmp_path: Path, payload: dict) -> Path:
    p = tmp_path / "weekly-telemetry-config.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def test_parse_warns_on_nonexistent_project_root(tmp_path: Path):
    """#11 : project_root fourni mais inexistant/non-répertoire → UserWarning
    explicite ; le comportement fail-soft (pas d'exception) est conservé."""
    missing = tmp_path / "absente"
    cfg_path = _config_file(tmp_path, {"project_root": str(missing)})
    with pytest.warns(UserWarning, match="project_root.*inexistant"):
        cfg = _parse(cfg_path)
    assert cfg.project_root == missing  # valeur conservée, jamais d'exception


def test_parse_no_warning_on_existing_project_root(tmp_path: Path):
    """Régression zéro : project_root existant → aucun warning nouveau."""
    root = tmp_path / "repo"
    root.mkdir()
    cfg_path = _config_file(tmp_path, {"project_root": str(root)})
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # tout warning → échec
        cfg = _parse(cfg_path)
    assert cfg.project_root == root


def test_parse_no_warning_when_project_root_absent(tmp_path: Path):
    """Clé absente → défaut None conservé, aucun warning."""
    cfg_path = _config_file(tmp_path, {"lookback_days": 7})
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        cfg = _parse(cfg_path)
    assert cfg.project_root is None
