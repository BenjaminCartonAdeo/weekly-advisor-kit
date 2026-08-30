"""Cellule 2.1 : détection mono-cible du harnais + parsing config + doctor."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from weekly_telemetry_aggregator.config import (
    DraftTargetsConfig,
    TelemetryConfig,
    load_config,
)
from weekly_telemetry_aggregator.draft_targets import (
    DEFAULT_DRAFT_HARNESS,
    DRAFT_HARNESS_MARKERS,
    DRAFT_HARNESS_TARGETS,
    DRAFT_TARGET_PRIORITY,
    HARNESS_CLAUDE_CODE,
    HARNESS_CODEX,
    HARNESS_COPILOT_VSCODE,
    HARNESS_OPENCODE,
    describe_draft_target,
    detect_draft_target,
    resolve_draft_targets,
)

# ================================================== détection par marqueurs


def test_detect_each_marker_alone(tmp_path: Path):
    """Chaque marqueur seul désigne son harnais (copilot : prompts OU skills)."""
    expected = {
        HARNESS_CLAUDE_CODE: (".claude/",),
        HARNESS_OPENCODE: (".opencode/",),
        HARNESS_COPILOT_VSCODE: (".github/prompts/", ".github/skills/"),
        HARNESS_CODEX: (".agents/",),
    }
    for harness, markers in expected.items():
        assert expected[harness] == DRAFT_HARNESS_MARKERS[harness]
        for marker in markers:
            root = tmp_path / f"{harness}_{marker.strip('/').replace('/', '_')}"
            (root / marker).mkdir(parents=True)
            assert detect_draft_target(root) == harness


def test_detect_priority_multi_markers(tmp_path: Path):
    """Priorité actée §2.1 : claude > opencode > copilot > codex."""
    assert DRAFT_TARGET_PRIORITY == (
        HARNESS_CLAUDE_CODE,
        HARNESS_OPENCODE,
        HARNESS_COPILOT_VSCODE,
        HARNESS_CODEX,
    )
    all_markers = tmp_path / "all"
    for marker in (
        ".claude",
        ".opencode",
        ".github/prompts",
        ".github/skills",
        ".agents",
    ):
        _ = (all_markers / marker).mkdir(parents=True)
    assert detect_draft_target(all_markers) == HARNESS_CLAUDE_CODE

    no_claude = tmp_path / "no_claude"
    for marker in (".opencode", ".github/prompts", ".agents"):
        _ = (no_claude / marker).mkdir(parents=True)
    assert detect_draft_target(no_claude) == HARNESS_OPENCODE

    copilot_codex = tmp_path / "copilot_codex"
    _ = (copilot_codex / ".github/skills").mkdir(parents=True)
    _ = (copilot_codex / ".agents").mkdir(parents=True)
    assert detect_draft_target(copilot_codex) == HARNESS_COPILOT_VSCODE

    codex_only = tmp_path / "codex_only"
    _ = (codex_only / ".agents").mkdir(parents=True)
    assert detect_draft_target(codex_only) == HARNESS_CODEX


def test_detect_none_when_no_marker_or_root(tmp_path: Path):
    """Aucun marqueur → None ; project_root None → None (le défaut est porté
    par la résolution, pas par la détection)."""
    empty = tmp_path / "empty"
    empty.mkdir()
    assert detect_draft_target(empty) is None
    assert detect_draft_target(None) is None


# ================================================== résolution effective


def test_resolve_detected_single_target(tmp_path: Path):
    _ = (tmp_path / ".opencode").mkdir()
    resolved = resolve_draft_targets(tmp_path, DraftTargetsConfig())
    assert resolved.mode == "detected"
    assert resolved.harnesses == (HARNESS_OPENCODE,)
    assert resolved.warning is None


def test_resolve_default_opencode_with_warning_when_no_marker(tmp_path: Path):
    """Rien trouvé → défaut opencode + warning explicite (affiché par le doctor)."""
    resolved = resolve_draft_targets(tmp_path, DraftTargetsConfig())
    assert resolved.mode == "default"
    assert resolved.harnesses == (DEFAULT_DRAFT_HARNESS,)
    assert resolved.warning is not None
    assert DEFAULT_DRAFT_HARNESS in resolved.warning


def test_resolve_none_root_falls_back_to_default():
    resolved = resolve_draft_targets(None, DraftTargetsConfig())
    assert resolved.mode == "default"
    assert resolved.harnesses == (DEFAULT_DRAFT_HARNESS,)


def test_resolve_override_wins_over_detection(tmp_path: Path):
    """Override config > détection : .claude présent mais override codex → codex."""
    _ = (tmp_path / ".claude").mkdir()
    cfg_dt = DraftTargetsConfig(mode="override", targets=[HARNESS_CODEX])
    resolved = resolve_draft_targets(tmp_path, cfg_dt)
    assert resolved.mode == "override"
    assert resolved.harnesses == (HARNESS_CODEX,)
    assert resolved.warning is None


def test_resolve_legacy_returns_all_targets(tmp_path: Path):
    """[] = mode legacy : toutes les cibles connues, comportement historique."""
    resolved = resolve_draft_targets(tmp_path, DraftTargetsConfig(mode="legacy"))
    assert resolved.mode == "legacy"
    assert resolved.harnesses == DRAFT_TARGET_PRIORITY
    assert len(resolved.harnesses) == 4


# ================================================== mapping cibles (contrat 2.2)


def test_mapping_covers_every_harness_with_relative_dirs():
    """Structure exportée pour 2.2 : chaque harnais prioritaire a ses cibles ;
    chemins relatifs au project_root, zéro symlink à ce stade (décision seule)."""
    assert set(DRAFT_HARNESS_TARGETS) == set(DRAFT_TARGET_PRIORITY)
    assert set(DRAFT_HARNESS_MARKERS) == set(DRAFT_TARGET_PRIORITY)
    assert DRAFT_HARNESS_TARGETS == {
        HARNESS_CLAUDE_CODE: (".claude/skills",),
        HARNESS_OPENCODE: (".opencode/skills",),
        HARNESS_COPILOT_VSCODE: (".github/prompts", ".github/skills"),
        HARNESS_CODEX: (".agents",),
    }
    for dirs in DRAFT_HARNESS_TARGETS.values():
        for d in dirs:
            assert not Path(d).is_absolute()


# ================================================== affichage doctor (describe)


def test_describe_draft_target_modes():
    detected = resolve_draft_targets(Path("/x"), DraftTargetsConfig())
    _ = detected  # describe est pur : construit directement
    assert (
        describe_draft_target(resolve_draft_targets(Path("/nonexistent_zz"), DraftTargetsConfig()))
        == "opencode (défaut)"
    )
    assert (
        describe_draft_target(
            resolve_draft_targets(None, DraftTargetsConfig(mode="override", targets=["codex"]))
        )
        == "codex (config)"
    )
    assert (
        describe_draft_target(
            resolve_draft_targets(
                None, DraftTargetsConfig(mode="override", targets=["opencode", "codex"])
            )
        )
        == "opencode, codex (config)"
    )
    assert (
        describe_draft_target(resolve_draft_targets(None, DraftTargetsConfig(mode="legacy")))
        == "toutes cibles (legacy)"
    )


# ================================================== parsing config draft_targets


def _write_conf(tmp_path: Path, payload: dict) -> Path:
    conf = tmp_path / "weekly-telemetry-config.json"
    conf.write_text(json.dumps(payload), encoding="utf-8")
    return conf


def test_config_key_absent_means_auto(tmp_path: Path):
    cfg = load_config(_write_conf(tmp_path, {"lookback_days": 3}))
    assert cfg.draft_targets.mode == "auto"
    assert cfg.draft_targets.targets == []


def test_config_default_dataclass_is_auto():
    cfg = TelemetryConfig()
    assert cfg.draft_targets.mode == "auto"
    assert cfg.draft_targets.targets == []
    assert cfg.curation.draft_targets == cfg.draft_targets


def test_config_non_empty_list_is_override(tmp_path: Path):
    cfg = load_config(_write_conf(tmp_path, {"draft_targets": ["codex", "opencode"]}))
    assert cfg.draft_targets.mode == "override"
    assert cfg.draft_targets.targets == ["codex", "opencode"]


def test_config_override_dedupes_preserving_order(tmp_path: Path):
    cfg = load_config(_write_conf(tmp_path, {"draft_targets": ["opencode", "codex", "opencode"]}))
    assert cfg.draft_targets.mode == "override"
    assert cfg.draft_targets.targets == ["opencode", "codex"]


def test_config_empty_list_is_legacy(tmp_path: Path):
    cfg = load_config(_write_conf(tmp_path, {"draft_targets": []}))
    assert cfg.draft_targets.mode == "legacy"
    assert cfg.draft_targets.targets == []


def test_config_unknown_value_warns_and_is_dropped(tmp_path: Path):
    """Valeur inconnue → warning + entrée ignorée ; les valides restent override."""
    with pytest.warns(UserWarning, match="draft_targets"):
        cfg = load_config(_write_conf(tmp_path, {"draft_targets": ["bogus", "codex"]}))
    assert cfg.draft_targets.mode == "override"
    assert cfg.draft_targets.targets == ["codex"]


def test_config_all_unknown_values_fallback_auto(tmp_path: Path):
    """Toutes les valeurs inconnues → warning + fallback détection auto."""
    with pytest.warns(UserWarning, match="draft_targets"):
        cfg = load_config(_write_conf(tmp_path, {"draft_targets": ["bogus", "nope"]}))
    assert cfg.draft_targets.mode == "auto"
    assert cfg.draft_targets.targets == []


def test_config_malformed_type_warns_and_fallback_auto(tmp_path: Path):
    """Type non-liste → warning + auto (style fail-soft de session_sources)."""
    with pytest.warns(UserWarning, match="draft_targets"):
        cfg = load_config(_write_conf(tmp_path, {"draft_targets": "opencode"}))
    assert cfg.draft_targets.mode == "auto"
    assert cfg.draft_targets.targets == []
