"""Characterisation des cœurs partagés entre `insights` et `rule_context`.

Ces six noms (3 fonctions + 3 constantes) étaient atteignables uniquement via
`insights`, et `rule_context` les importait *en différé* pour éviter un cycle
avec `insights`. Le déplacement vers `rule_context` n'est protégé que si leur
comportement est d'abord figé ici — d'où ce fichier, écrit AVANT le déplacement.

Chaque test asserte un comportement réel (sortie exacte sur entrée dégénérée,
classement d'un run, valeur documentée d'un défaut), pas une tautologie.
"""

from __future__ import annotations

import pytest

from weekly_telemetry_aggregator.insights import (
    AGENT_LOOP_MIN_REPEATS_DEFAULT,
    AGENT_LOOP_TASK_MIN_REPEATS_DEFAULT,
    ARCHITECTURE_DRIFT_RUNS_DEFAULT,
    _architecture_drift,
    _architecture_observation,
    _robust_z_scores,
)


# --------------------------------------------------------------------------- #
# _robust_z_scores — z robuste (médiane + MAD), arrondi à 2 décimales
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("values", "expected"),
    [
        # liste vide : aucune observation, pas de division
        ([], []),
        # un seul point : la médiane EST le point, donc z = 0 (pas de NaN)
        ([5.0], [0.0]),
        # série plate : MAD = 0 puis MAD moyen = 0 ⇒ tous zéros, jamais 1/0
        ([1.0, 1.0, 1.0], [0.0, 0.0, 0.0]),
        # pic unique : c'est le fallback MAD-moyen qui rend le pic détectable
        ([1.0, 1.0, 1.0, 99.0], [0.0, 0.0, 0.0, 2.7]),
        # série régulière : symétrie autour de la médiane, somme nulle
        ([1.0, 2.0, 3.0, 4.0], [-1.01, -0.34, 0.34, 1.01]),
        # valeurs négatives : le signe suit l'écart à la médiane
        ([-1.0, -2.0, -3.0], [0.67, 0.0, -0.67]),
    ],
)
def test_robust_z_scores_on_degenerate_and_regular_inputs(values, expected):
    assert _robust_z_scores(values) == expected


def test_robust_z_scores_is_rounded_to_two_decimals():
    """L'arrondi à 2 décimales est un contrat de sortie (JSON de finding)."""
    raw = _robust_z_scores([1.0, 2.0, 3.0, 4.0, 5.0, 100.0])

    assert raw == [-1.12, -0.67, -0.22, 0.22, 0.67, 43.39]
    assert all(value == round(value, 2) for value in raw)


def test_robust_z_scores_never_divides_by_zero():
    """Régression : MAD=0 doit produire des 0.0, pas une ZeroDivisionError/inf."""
    assert _robust_z_scores([2.0, 2.0, 2.0, 2.0]) == [0.0, 0.0, 0.0, 0.0]


def test_robust_z_scores_preserves_length_and_sign_order():
    values = [3.0, 1.0, 2.0, 10.0]

    scored = _robust_z_scores(values)

    assert len(scored) == len(values)
    # le plus grand écart garde le plus grand z, le plus petit le plus petit
    assert scored[3] == max(scored)
    assert scored[1] == min(scored)


# --------------------------------------------------------------------------- #
# _architecture_observation — projection architecture, 2 emplacements
# --------------------------------------------------------------------------- #
def test_architecture_observation_reads_the_top_level_key():
    assert _architecture_observation({"architecture_observations": {"state_counts": 1}}) == {
        "state_counts": 1
    }


def test_architecture_observation_falls_back_to_the_watch_context_mirror():
    """Le miroir `watch_context` existe pour les runs sans clé de premier niveau."""
    summary = {"watch_context": {"architecture_observations": {"state_counts": 1}}}

    assert _architecture_observation(summary) == {"state_counts": 1}


def test_architecture_observation_top_level_wins_over_the_mirror():
    summary = {
        "architecture_observations": {"origin": "top"},
        "watch_context": {"architecture_observations": {"origin": "mirror"}},
    }

    assert _architecture_observation(summary) == {"origin": "top"}


@pytest.mark.parametrize(
    "summary",
    [
        None,
        {},
        {"architecture_observations": None},
        {"architecture_observations": 5},
        {"watch_context": "not-a-dict"},
        {"watch_context": {"architecture_observations": "not-a-dict"}},
    ],
)
def test_architecture_observation_is_none_when_absent_or_malformed(summary):
    """Un snapshot absent ou malformé ne doit jamais lever : il vaut « inconnu »."""
    assert _architecture_observation(summary) is None


# --------------------------------------------------------------------------- #
# _architecture_drift — 4 champs stables comparés, ordre déterministe
# --------------------------------------------------------------------------- #
def test_architecture_drift_reports_a_changed_stable_field():
    assert _architecture_drift({"state_counts": 1}, {"state_counts": 2}) == ["state_counts"]


def test_architecture_drift_covers_exactly_the_four_stable_fields():
    """Un champ hors liste (ex. une timestamp) ne doit jamais être signalé."""
    current = {"state_counts": 1, "config": {"a": 1}, "inventory_counts": 2, "harness_scope": 3}
    previous = {**current, "anchor": "2026-10-01", "generated_at": "2026-10-01T00:00:00Z"}

    assert _architecture_drift(current, previous) == []


def test_architecture_drift_lists_every_changed_field_in_fixed_order():
    """L'ordre est celui de la tuple d_constants : la sortie est déterministe."""
    current = {"harness_scope": {"a": 1}, "config": {"a": 1}, "state_counts": 1}
    previous = {"harness_scope": {"a": 2}, "config": {"a": 2}, "state_counts": 1}

    assert _architecture_drift(current, previous) == ["config", "harness_scope"]


def test_architecture_drift_is_empty_without_both_snapshots():
    """Un snapshot manquant = « inconnu », jamais « drift » (pas de faux positif)."""
    assert _architecture_drift(None, None) == []
    assert _architecture_drift({"state_counts": 1}, None) == []
    assert _architecture_drift(None, {"state_counts": 1}) == []
    assert _architecture_drift({}, {}) == []


def test_architecture_drift_ignores_a_field_missing_on_one_side_only():
    """`dict.get` symmetric : une clé absente des deux côtés ne compte pas."""
    assert _architecture_drift({"config": {"a": 1}}, {}) == []


# --------------------------------------------------------------------------- #
# Les 3 défauts — seuils documentés, surchargeables par config
# --------------------------------------------------------------------------- #
def test_documented_defaults_hold_their_published_values():
    assert AGENT_LOOP_MIN_REPEATS_DEFAULT == 8
    assert AGENT_LOOP_TASK_MIN_REPEATS_DEFAULT == 3
    assert ARCHITECTURE_DRIFT_RUNS_DEFAULT == 2


def test_task_threshold_stays_strictly_below_the_global_threshold():
    """Invariant de la règle agent-loop : le seuil par tâche est le plus bas."""
    assert AGENT_LOOP_TASK_MIN_REPEATS_DEFAULT < AGENT_LOOP_MIN_REPEATS_DEFAULT


def test_drift_needs_more_than_one_run_to_be_stable():
    """Un drift d'un seul run est du bruit : le seuil est à 2 runs."""
    assert ARCHITECTURE_DRIFT_RUNS_DEFAULT > 1
