"""Cellule 2.1 : résolution mono-cible du harnais + parsing config + doctor."""

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
    DRAFT_HARNESS_COMMAND_TARGETS,
    DRAFT_HARNESS_LAYOUTS,
    DRAFT_HARNESS_MARKERS,
    DRAFT_HARNESS_TARGETS,
    DRAFT_TARGET_PRIORITY,
    HARNESS_CATEGORIES,
    HARNESS_CATEGORY_AGENTS,
    HARNESS_CATEGORY_COMMANDS,
    HARNESS_CATEGORY_SKILLS,
    HARNESS_CLAUDE_CODE,
    HARNESS_CODEX,
    HARNESS_COPILOT_CLI,
    HARNESS_OPENCODE,
    MODE_DEFAULT,
    MODE_OVERRIDE,
    HarnessLayout,
    ResolvedDraftTarget,
    describe_draft_target,
    marker_warnings,
    resolve_draft_targets,
)
from weekly_telemetry_aggregator.harness_scope import harness_extra_roots
from weekly_telemetry_aggregator.safe_git_write import _MESSAGE_PREFIX

# ================================================== résolution effective


def test_resolve_ignores_markers_returns_default_opencode(tmp_path: Path):
    """A2 : les marqueurs ne décident plus rien — `.opencode` présent comme absent,
    la cible résolue est le défaut opencode, en mode default avec warning."""
    with_marker = tmp_path / "with_marker"
    _ = (with_marker / ".opencode").mkdir(parents=True)
    resolved = resolve_draft_targets(with_marker, DraftTargetsConfig())
    assert resolved.mode == "default"
    assert resolved.harnesses == (DEFAULT_DRAFT_HARNESS,)
    assert resolved.warning is not None

    without_marker = tmp_path / "without_marker"
    without_marker.mkdir()
    bare = resolve_draft_targets(without_marker, DraftTargetsConfig())
    assert bare.mode == resolved.mode
    assert bare.harnesses == resolved.harnesses
    assert bare.warning is not None
    assert DEFAULT_DRAFT_HARNESS in bare.warning


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
    """Override config > marqueurs : `.claude` présent mais override codex → codex.

    A3 : le warning n'est plus `None` — `.claude/` est un marqueur *étranger* à la
    cible résolue et `.agents/` est absent. Ni `mode` ni `harnesses` n'ont bougé
    (asserts ci-dessus) : c'est exactement la garantie « warn-only ».
    """
    _ = (tmp_path / ".claude").mkdir()
    cfg_dt = DraftTargetsConfig(mode="override", targets=[HARNESS_CODEX])
    resolved = resolve_draft_targets(tmp_path, cfg_dt)
    assert resolved.mode == "override"
    assert resolved.harnesses == (HARNESS_CODEX,)
    assert resolved.warning is not None
    assert "hors cible résolue : .claude/ → claude-code" in resolved.warning
    assert "aucun marqueur pour la cible résolue (codex) : .agents/" in resolved.warning


def test_resolve_legacy_returns_all_targets(tmp_path: Path):
    """[] = mode legacy : toutes les cibles connues, comportement historique."""
    resolved = resolve_draft_targets(tmp_path, DraftTargetsConfig(mode="legacy"))
    assert resolved.mode == "legacy"
    assert resolved.harnesses == DRAFT_TARGET_PRIORITY
    assert len(resolved.harnesses) == 4


# ================================================== mapping cibles (contrat 2.2)


# ================================================== A1 : table unique 3 catégories


def test_mapping_covers_every_harness_with_relative_dirs():
    """Table unique : les 4 harnais × 3 catégories, chemins relatifs au project_root.

    `DRAFT_HARNESS_TARGETS` reste la vue « skills » consommée par la projection
    (`harness_extra_roots`, `inject_engine_content`) — donc strictement identique
    à l'ex-littéral. `DRAFT_HARNESS_COMMAND_TARGETS` reproduit exactement
    l'ex-`ENGINE_COMMAND_TARGETS` codé en dur dans `harness_scope`.
    """
    assert set(DRAFT_HARNESS_LAYOUTS) == set(DRAFT_TARGET_PRIORITY)
    assert set(DRAFT_HARNESS_MARKERS) == set(DRAFT_TARGET_PRIORITY)
    assert DRAFT_HARNESS_TARGETS == {
        HARNESS_CLAUDE_CODE: (".claude/skills",),
        HARNESS_OPENCODE: (".opencode/skills",),
        HARNESS_COPILOT_CLI: (".github/prompts", ".github/skills"),
        HARNESS_CODEX: (".agents",),
    }
    assert DRAFT_HARNESS_COMMAND_TARGETS == {
        ".opencode/skills": ".opencode/commands",
        ".claude/skills": ".claude/commands",
        ".github/skills": ".github/prompts",
        ".github/prompts": ".github/prompts",
        ".agents": ".agents/commands",
    }
    for layout in DRAFT_HARNESS_LAYOUTS.values():
        for category in HARNESS_CATEGORIES:
            for directory in getattr(layout, category):
                assert not Path(directory).is_absolute()
    assert {
        harness: layout.skills for harness, layout in DRAFT_HARNESS_LAYOUTS.items()
    } == DRAFT_HARNESS_TARGETS


def test_agents_category_matches_commit_draft_agents_prefix():
    """A1 : la catégorie `agents` décrit la destination que `commit-draft` écrit.

    `safe_git_write._MESSAGE_PREFIX["agent"]` est le préfixe des drafts d'agents ;
    la destination opencode correspondante est `.opencode/agents/` (inclus par
    les profils harness de `config`). Sans entrée `agents` dans la table, cette
    surface restait invisible — le drift que A1 supprime.
    """
    assert _MESSAGE_PREFIX["agent"] == "agent:"
    assert ".opencode/agents" in DRAFT_HARNESS_LAYOUTS[HARNESS_OPENCODE].agents
    assert ".claude/agents" in DRAFT_HARNESS_LAYOUTS[HARNESS_CLAUDE_CODE].agents
    assert ".github/agents" in DRAFT_HARNESS_LAYOUTS[HARNESS_COPILOT_CLI].agents
    # codex : aucun profil d'agent déclaré — case présente mais vide
    assert DRAFT_HARNESS_LAYOUTS[HARNESS_CODEX].agents == ()
    # chaque harnais déclare les 3 catégories (structurelle, pas de dict à part)
    for harness, layout in DRAFT_HARNESS_LAYOUTS.items():
        assert isinstance(layout, HarnessLayout)
        assert isinstance(getattr(layout, HARNESS_CATEGORY_SKILLS), tuple)
        assert isinstance(getattr(layout, HARNESS_CATEGORY_COMMANDS), tuple)
        assert isinstance(getattr(layout, HARNESS_CATEGORY_AGENTS), tuple)
        assert harness in DRAFT_TARGET_PRIORITY


def test_agents_category_is_not_a_projection_root():
    """Parité de projection : `.opencode/agents` n'est PAS une extra root.

    Le walk natif `.opencode` couvre déjà ces fichiers ; les réintégrer
    dupliquerait la surface (`harness_extra_roots(opencode) == ()`).
    """
    assert harness_extra_roots(ResolvedDraftTarget(MODE_DEFAULT, (HARNESS_OPENCODE,))) == ()


# ================================================== A3 : marqueurs warn-only


def test_marker_warnings_foreign_marker(tmp_path: Path):
    """Cas réel du 03/10 : `.claude/` résiduel alors que la cible est opencode."""
    _ = (tmp_path / ".opencode").mkdir()
    _ = (tmp_path / ".claude").mkdir()

    assert marker_warnings(tmp_path, (HARNESS_OPENCODE,)) == (
        "marqueur(s) de harnais hors cible résolue : .claude/ → claude-code — "
        "ajouter draft_targets: [claude-code] pour auditer cette surface",
    )


def test_marker_warnings_absent_marker(tmp_path: Path):
    """Aucun marqueur pour la cible résolue → surface absente signalée."""
    tmp_path.mkdir(exist_ok=True)

    assert marker_warnings(tmp_path, (HARNESS_OPENCODE,)) == (
        "aucun marqueur pour la cible résolue (opencode) : .opencode/ — "
        "surface de projection absente du projet",
    )


def test_marker_warnings_silent_when_target_marker_present(tmp_path: Path):
    """Marqueur présent ET cible → aucun warning (cas nominal)."""
    _ = (tmp_path / ".opencode").mkdir()

    assert marker_warnings(tmp_path, (HARNESS_OPENCODE,)) == ()


def test_marker_warnings_never_decide_mode_or_harnesses(tmp_path: Path):
    """D3 : un warning ne modifie ni `mode` ni `harnesses` — quelle que soit la
    présence de marqueurs, la config seule décide."""
    foreign = tmp_path / "foreign"
    _ = (foreign / ".opencode").mkdir(parents=True)
    _ = (foreign / ".claude").mkdir()
    _ = (foreign / ".agents").mkdir()
    bare = tmp_path / "bare"
    bare.mkdir()

    cfg_auto = DraftTargetsConfig()
    with_markers = resolve_draft_targets(foreign, cfg_auto)
    without_markers = resolve_draft_targets(bare, cfg_auto)
    assert with_markers.mode == without_markers.mode == MODE_DEFAULT
    assert with_markers.harnesses == without_markers.harnesses == (DEFAULT_DRAFT_HARNESS,)
    # ... seul le warning diffère
    assert with_markers.warning != without_markers.warning

    # même config, marqueurs très différents → même cible en override
    cfg_override = DraftTargetsConfig(mode="override", targets=[HARNESS_OPENCODE])
    foreign_override = resolve_draft_targets(foreign, cfg_override)
    bare_override = resolve_draft_targets(bare, cfg_override)
    assert foreign_override.harnesses == bare_override.harnesses == (HARNESS_OPENCODE,)
    assert foreign_override.mode == bare_override.mode == MODE_OVERRIDE
    # ... et le warning, lui, capte bien l'écart de marqueurs
    assert "hors cible résolue : .agents/ → codex" in foreign_override.warning
    assert "hors cible résolue" not in (bare_override.warning or "")

    # le warning du mode default (A2) reste présent, jamais dupliqué
    assert without_markers.warning is not None
    assert without_markers.warning.count("aucun draft_targets explicite en config") == 1


def test_marker_warnings_compose_with_default_warning(tmp_path: Path):
    """Le défaut de config (A2) et l'absence de marqueur (A3) coexistent, chacun
    une fois, sans se masquer."""
    tmp_path.mkdir(exist_ok=True)

    resolved = resolve_draft_targets(tmp_path, DraftTargetsConfig())

    assert resolved.warning is not None
    assert resolved.warning.count("aucun draft_targets explicite en config") == 1
    assert resolved.warning.count("aucun marqueur pour la cible résolue") == 1
    assert DEFAULT_DRAFT_HARNESS in resolved.warning


def test_marker_warnings_coprilot_partial_markers_is_present(tmp_path: Path):
    """`.github/prompts` seul suffit : la surface copilot existe, pas d'alerte."""
    _ = (tmp_path / ".github/prompts").mkdir(parents=True)

    assert marker_warnings(tmp_path, (HARNESS_COPILOT_CLI,)) == ()


def test_marker_warnings_without_root_or_harness_is_silent():
    """Ni racine ni cible comparable → rien à dire (pas de faux positif)."""
    assert marker_warnings(None, (HARNESS_OPENCODE,)) == ()
    assert marker_warnings(Path("/nonexistent_zz"), (HARNESS_OPENCODE,)) != ()
    assert marker_warnings(Path("/nonexistent_zz"), ()) == ()
    # harnais inconnu (typo de config) : pas de marqueur à comparer
    assert marker_warnings(Path("/nonexistent_zz"), ("bogus",)) == ()


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
