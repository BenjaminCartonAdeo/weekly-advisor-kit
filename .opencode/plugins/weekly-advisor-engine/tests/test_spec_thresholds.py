"""D4 rendu exécutable — les seuils du § C9 sont *câblés*, pas seulement déclarés.

D4 (`doc/spec/08-criteres-acceptation.md:51-52`) énumère des seuils. Un critère
écrit que rien n'exécute est le même défaut que « 1212 tests » : vérifiable par
une personne, invérifiable par une machine. Ce fichier est la machine.

## Pourquoi ce test n'est PAS un `hasattr`

Le plan 02 demandait d'asserter la « présence » de chaque seuil dans
`InsightsConfig`. Une assertion de présence est **vide de sens** ici, et elle
mentirait activement sur deux points :

1. Tout champ de dataclass porte un défaut Python. `hasattr(InsightsConfig(),
   "weekly_budget_usd")` est vrai que la configuration livrée câblle la clé ou
   non — l'assertion ne distingue jamais du code câblé du code mort.
2. `exclusion_rate_max` n'est pas un champ du tout. Il est lu par
   `getattr(insights_cfg, "exclusion_rate_max", EXCLUSION_RATE_MAX_DEFAULT)`
   (`insights.py:739`) alors que `InsightsConfig` est `frozen=True, slots=True` :
   l'attribut ne peut pas être posé, la branche `getattr` est inatteignable. Une
   assertion de présence « verte » sur cette clé fabriquerait la preuve d'un
   câblage inexistant.

Ce que « câblé » doit signifier ici : **la valeur configurée est lue par le
chemin de code qui décide de l'alerte**. On le prouve par le comportement —
perturber le seuil et exiger que l'alerte suive, puis qu'elle se taise. Un
seuil recodé en dur dans l'émetteur (`"threshold": 0.70` au lieu de
`insights_cfg.lint_coverage_min`) fait rougir ce test ; un `hasattr`, jamais.

## Ce que ce test ne fait pas

Il n'aligne pas la spec sur le code. Les nombres de D4 ne sont pas recopiés
ici : ce serait faire du test une seconde copie du document, donc circulaire (il
passerait aussi bien quand doc et code ont dérivé ensemble). Les seuils sont
référencés par **nom de champ** ; la table du C9 reste le document d'autorité.

Sources : `doc/spec/08-criteres-acceptation.md:51-52` (D4),
`doc/spec/03-capacites.md:351-358` (C9), `weekly_telemetry_aggregator/config.py`
(`InsightsConfig`), `weekly_telemetry_aggregator/insights.py` (émetteurs).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest

from weekly_telemetry_aggregator.config import InsightsConfig
from weekly_telemetry_aggregator.insights import (
    EXCLUSION_RATE_MAX_DEFAULT,
    compute,
)

RUN = datetime(2026, 8, 12, tzinfo=UTC)

#: Deux seuils « silencing », dans les deux directions de comparaison.
#:
#: Les alertes ne comparent pas toutes de la même façon : celles notées
#: « plancher » sonnent quand l'observé passe *sous* le seuil, on les étouffe
#: donc en poussant le seuil sous zéro ; les autres sonnent quand l'observé
#: dépasse le seuil, on les étouffe en poussant le seuil à l'infini. Un sentinel
#: unique « vers le haut » n'éteindrait jamais les premières — il les
#: déclencherait au contraire.
SILENT_ABOVE = 1e9
SILENT_BELOW = -1.0

#: Seuils de **perturbation** : valeurs synthétiques, délibérément distinctes des
#: défauts Python, mais encore du bon côté de l'observé pour que l'alerte parte.
#:
#: C'est ce qui distingue une assertion qui prouve le câblage d'une assertion
#: qui le simule. Comparer l'alerte au défaut (`threshold == 35.0`) ne prouve
#: rien : un émetteur qui écrit `"threshold": 35.0` en dur passe le test. Il faut
#: donc **déplacer** le seuil et exiger que l'alerte suive le déplacement. Ces
#: valeurs ne viennent pas de D4 et ne doivent pas y être lues.
PERTURBED_ABOVE = 9_000.0
PERTURBED_BELOW = 0.05


# --------------------------------------------------------------------------
# Harnais
# --------------------------------------------------------------------------


def _summary(period, *, cost=1.0, cache=0.9, cache_write=1000, generated_at=None):
    """Résumé hebdomadaire minimal — même forme que `_summary` de test_insights."""
    return {
        "period": {"start": period[0], "end": period[1]},
        "totals": {
            "total_cost_usd": cost,
            "cache_hit_rate": cache,
            "total_tokens": 1000,
            "cache_write_tokens": cache_write,
        },
        "skill_usage": [],
        "skills_never_loaded": [],
        "skill_similar_pairs": [],
        "daily_totals": [],
        "skill_catalog_count": 0,
        "generated_at": generated_at or (period[1] + "T00:00:00Z"),
    }


def _run(kwargs: dict[str, Any], **cfg_over) -> list[dict]:
    """`compute()` sur un jeu d'arguments, avec un InsightsConfig paramétrable."""
    return compute(
        insights_cfg=replace(InsightsConfig(), **cfg_over),
        ignored_findings=[],
        harness_ignored_rules=[],
        **kwargs,
    )["alerts"]


def _alert(alerts, rule):
    return next((a for a in alerts if a["rule"] == rule), None)


# --------------------------------------------------------------------------
# Un jeu de données par famille de seuil. Chaque entrée renvoie les arguments
# de `compute()` *sans* `insights_cfg` : le seuil est injecté par `_run`, ce qui
# permet de rejouer exactement la même observation avec une config différente.
# --------------------------------------------------------------------------


def _case_weekly_budget():
    cur = _summary(("2026-08-05", "2026-08-12"), cost=10_000.0)
    return dict(
        run_time=RUN,
        current_summary=cur,
        previous_summary=None,
        current_digest=None,
        previous_digest=None,
        recent_summaries=[cur],
    )


def _case_monthly_budget():
    """Le run courant est calme ; c'est l'historique 30 jours qui dépasse.

    `_month_cost` (insights.py:133-141) ne somme que les résumés dont
    `generated_at` tombe dans les 30 jours précédant le run : le résumé « vieux »
    doit donc être récent, sinon il est ignoré et l'alerte ne part jamais.
    """
    cur = _summary(("2026-08-05", "2026-08-12"))
    old = _summary(("2026-07-14", "2026-07-20"), cost=10_000.0, generated_at="2026-07-20T00:00:00Z")
    return dict(
        run_time=RUN,
        current_summary=cur,
        previous_summary=old,
        current_digest=None,
        previous_digest=None,
        recent_summaries=[cur, old],
    )


def _case_daily_spike():
    """z robuste astronomique : baseline plate, un jour à 10 000 $."""
    cur = _summary(("2026-08-05", "2026-08-12"), cost=10_000.0)
    cur["daily_totals"] = [{"date": "2026-08-07", "cost_usd": 10_000.0}]
    prev = _summary(("2026-07-29", "2026-08-05"))
    prev["daily_totals"] = [{"date": f"2026-07-{d:02d}", "cost_usd": 1.0} for d in range(25, 32)]
    return dict(
        run_time=RUN,
        current_summary=cur,
        previous_summary=prev,
        current_digest=None,
        previous_digest=None,
        recent_summaries=[cur, prev],
    )


def _case_cache():
    cur = _summary(("2026-08-05", "2026-08-12"), cache=0.0)
    return dict(
        run_time=RUN,
        current_summary=cur,
        previous_summary=None,
        current_digest=None,
        previous_digest=None,
        recent_summaries=[cur],
    )


def _case_wow():
    """Hausse de coût d'un run à l'autre (le seuil porte sur le pourcentage)."""
    cur = _summary(("2026-08-05", "2026-08-12"), cost=100.0)
    prev = _summary(("2026-07-29", "2026-08-05"), cost=1.0)
    return dict(
        run_time=RUN,
        current_summary=cur,
        previous_summary=prev,
        current_digest=None,
        previous_digest=None,
        recent_summaries=[cur, prev],
    )


def _digest(findings: int = 0, unscoped: int = 0) -> dict:
    """Digest harness-eval : N findings sous `inspection.command`."""
    return {
        "findings": [],
        "inspection": {
            "summary": {"total": 10},
            "command": [
                {"path": f"f{i}.sh", "findings": [{"rule": "E501", "message": "m"}]}
                for i in range(findings)
            ],
            "claude_md": [],
            "uncategorized": [],
        },
        "harness_scope": {"unscoped_file_count": unscoped},
    }


def _case_lint_violations():
    cur = _summary(("2026-08-05", "2026-08-12"))
    return dict(
        run_time=RUN,
        current_summary=cur,
        previous_summary=None,
        current_digest=_digest(findings=50),
        previous_digest=None,
        recent_summaries=[cur],
    )


def _case_lint_coverage():
    """Majorité des surfaces .opencode/ hors allowlist."""
    cur = _summary(("2026-08-05", "2026-08-12"))
    return dict(
        run_time=RUN,
        current_summary=cur,
        previous_summary=None,
        current_digest=_digest(unscoped=1_000),
        previous_digest=None,
        recent_summaries=[cur],
    )


def _case_cache_write_zero():
    """Deux runs consécutifs sans écriture de cache (courant + précédent)."""
    cur = _summary(("2026-08-05", "2026-08-12"), cache_write=0)
    prev = _summary(("2026-07-29", "2026-08-05"), cache_write=0)
    return dict(
        run_time=RUN,
        current_summary=cur,
        previous_summary=prev,
        current_digest=None,
        previous_digest=None,
        recent_summaries=[cur, prev],
    )


#: (champ `InsightsConfig`, nom d'alerte, cas qui la déclenche,
#:  seuil qui éteint, seuil qui déplace l'alerte).
#: Le champ est un *nom*, jamais un nombre de D4 : le test ne doit pas recopier
#: le document, sinon il validerait une copie au lieu du code.
D4_THRESHOLDS = [
    ("weekly_budget_usd", "weekly_budget_usd", _case_weekly_budget, SILENT_ABOVE, PERTURBED_ABOVE),
    (
        "monthly_budget_usd",
        "monthly_budget_usd",
        _case_monthly_budget,
        SILENT_ABOVE,
        PERTURBED_ABOVE,
    ),
    ("daily_spike_z_min", "daily_spike", _case_daily_spike, SILENT_ABOVE, 2.0),
    ("cache_hit_rate_min", "cache_hit_rate_min", _case_cache, SILENT_BELOW, PERTURBED_BELOW),
    ("cost_wow_pct_max", "cost_wow_pct_max", _case_wow, SILENT_ABOVE, 9_000.0),
    ("lint_violations_max", "lint_violations_max", _case_lint_violations, SILENT_ABOVE, 45.0),
    ("lint_coverage_min", "lint_coverage", _case_lint_coverage, SILENT_BELOW, PERTURBED_BELOW),
    ("cache_write_zero_runs", "cache_write_zero_runs", _case_cache_write_zero, SILENT_ABOVE, 1.0),
]

_D4_IDS = [t[0] for t in D4_THRESHOLDS]


# --------------------------------------------------------------------------
# D4 — le seuil existe, il est publié, il décide
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "rule", "case", "silent", "perturbed"), D4_THRESHOLDS, ids=_D4_IDS
)
def test_d4_threshold_is_a_declared_field(field, rule, case, silent, perturbed):
    """Le seuil cité par D4 est bien un champ de `InsightsConfig`.

    Cette assertion est la plus faible des trois — tout champ porte un défaut
    Python, elle ne prouve donc aucun câblage. Elle est conservée parce qu'elle
    donne un message d'échec lisible si le champ disparaît, au lieu d'une
    `AttributeError` au fond d'un émetteur.
    """
    assert field in InsightsConfig.__dataclass_fields__, (
        f"{field}, cité par D4, n'est pas un champ de InsightsConfig "
        f"(champs: {sorted(InsightsConfig.__dataclass_fields__)})"
    )


@pytest.mark.parametrize(
    ("field", "rule", "case", "silent", "perturbed"), D4_THRESHOLDS, ids=_D4_IDS
)
def test_d4_threshold_is_published_from_config(field, rule, case, silent, perturbed):
    """D4 : l'alerte publie le seuil *configuré*, y compris déplacé.

    Preuve comportementale — le seuil est **perturbé** vers une valeur qui n'est
    pas le défaut, et l'alerte doit publier cette valeur. Comparer au défaut ne
    prouverait rien : un émetteur écrivant `"threshold": 35.0` en dur passerait
    le test. C'est le déplacement qui le fait rougir.
    """
    alert = _alert(_run(case(), **{field: perturbed}), rule)
    assert alert is not None, (
        f"l'alerte {rule!r} ne part pas avec InsightsConfig.{field}={perturbed!r} "
        "alors que l'observé est franchement du bon côté — jeu de données périmé ?"
    )
    assert alert["threshold"] == pytest.approx(perturbed), (
        f"{rule}: le seuil publié ({alert['threshold']!r}) ne suit pas "
        f"InsightsConfig.{field}={perturbed!r} — il est recodé en dur dans "
        "l'émetteur, la configuration livrée ne pilote rien"
    )


@pytest.mark.parametrize(
    ("field", "rule", "case", "silent", "perturbed"), D4_THRESHOLDS, ids=_D4_IDS
)
def test_d4_threshold_decides_its_alert(field, rule, case, silent, perturbed):
    """D4, deuxième moitié : la *décision* lit la config, pas l'affichage.

    Pousser le seuil hors d'atteinte de l'observé fait disparaître l'alerte.
    C'est la preuve que l'émetteur compare à `InsightsConfig.<champ>` : si le
    seuil était figé dans l'émetteur, l'alerte resterait malgré la config.
    """
    assert _alert(_run(case(), **{field: silent}), rule) is None, (
        f"{rule}: l'alerte survit à un seuil de {silent} — le seuil est recodé "
        f"en dur dans l'émetteur, InsightsConfig.{field} n'est pas lu"
    )


# --------------------------------------------------------------------------
# D4 cite aussi « R1 ≥ 4 runs » : le seuil de R1 ne produit pas une alerte mais
# un constat de maintenance. Canal différent, donc test séparé — les fusionner
# dans la table masquerait la différence de nature.
# --------------------------------------------------------------------------


def _retire_findings(**cfg_over):
    """Findings de maintenance pour une série saturée de 4 runs."""
    cur = _summary(("2026-08-05", "2026-08-12"))
    cur["skills_never_loaded"] = ["dead-skill"]
    cur["skill_catalog_entries"] = [{"skill_id": "dead-skill"}]
    hist = [cur] + [_summary(("X", "Y")) for _ in range(3)]
    for s in hist[1:]:
        s["skills_never_loaded"] = ["dead-skill"]
    return compute(
        run_time=RUN,
        current_summary=cur,
        previous_summary=None,
        current_digest=None,
        previous_digest=None,
        recent_summaries=hist,
        insights_cfg=replace(InsightsConfig(), **cfg_over),
        ignored_findings=[],
    )["maintenance"]["findings"]


def test_d4_r1_evidence_floor_is_wired_to_config():
    """D4 (« R1 ≥ 4 runs ») : le plancher de preuve lit `never_loaded_runs_threshold`.

    La série est ici volontairement complète et saturée (le skill est absent des
    4 runs observées, et figure au catalogue) — tous les critères de R1 sont donc
    réunis. Le seul levier restant est le seuil : il doit decidesque le constat
    sort ou non. Un plancher figé en dur dans `_retire_candidates` ferait rougir
    ce test.
    """
    above = [
        f
        for f in _retire_findings(never_loaded_runs_threshold=99)
        if f["category"] == "retire-candidate"
    ]
    assert above == [], (
        "le constat sort encore avec un plancher de 99 runs pour une série de 4 — "
        "InsightsConfig.never_loaded_runs_threshold n'est pas lu"
    )

    at_floor = [
        f
        for f in _retire_findings(never_loaded_runs_threshold=4)
        if f["category"] == "retire-candidate"
    ]
    assert at_floor, (
        "le constat ne sort pas à un plancher de 4 runs pour une série de 4 — "
        "la comparaison n'est plus `len(runs) >= never_loaded_runs_threshold`"
    )


# --------------------------------------------------------------------------
# Le piège `exclusion_rate_max`
# --------------------------------------------------------------------------


def test_c9_exclusion_rate_is_a_module_constant_not_a_config_field():
    """Verrou anti-2.8 : `exclusion_rate_max` n'est pas câblable en l'état.

    La table C9 cite cette alerte. Son seuil est `EXCLUSION_RATE_MAX_DEFAULT`
    (`insights.py:68`), relu par un `getattr` sur `InsightsConfig` — or
    `InsightsConfig` est `frozen=True, slots=True` : l'attribut ne peut pas être
    posé, la branche `getattr` est donc inatteignable et la valeur effective est
    toujours la constante de module.

    Conséquence : ajouter `exclusion_rate_max` à `weekly-telemetry-config.json`
    créerait une clé *paraissant* configurable que rien n'exécute — exactement
    le défaut que ce fichier existe pour empêcher. Le test est ici pour rendre
    ce « correctif » impossible à rejouer par ignorance.
    """
    assert "exclusion_rate_max" not in InsightsConfig.__dataclass_fields__, (
        "InsightsConfig expose désormais exclusion_rate_max : la constante de "
        "module insights.py:68 serait devenue une constante de configuration. "
        "Mettre à jour la table C9 et cet avertissement."
    )
    assert pytest.approx(0.20) == EXCLUSION_RATE_MAX_DEFAULT, (
        f"EXCLUSION_RATE_MAX_DEFAULT vaut {EXCLUSION_RATE_MAX_DEFAULT!r} "
        f"(insights.py:68) ; la table C9 doit être resynchronisée"
    )


def test_exclusion_rate_alert_reads_the_module_constant():
    """Le seuil effectif de `exclusion_rate` est bien la constante (0,20).

    `InsightsConfig()` nu ne porte pas l'attribut : la valeur qui décide de
    l'alerte vient de `insights.py:68`. On le prouve en passant une
    `InsightsConfig` réelle (pas un `SimpleNamespace` coiffé de l'attribut,
    comme le fait `test_insights.py`) et en observant le seuil publié.
    """
    summary = {
        **_summary(("2026-08-05", "2026-08-12")),
        "selection": {
            "window_touched": 100,
            "excluded_active": 50,
            "excluded_no_activity": 0,
            "excluded_unflushed": 0,
        },
    }
    alerts = _run(
        dict(
            run_time=RUN,
            current_summary=summary,
            previous_summary=None,
            current_digest=None,
            previous_digest=None,
            recent_summaries=[summary],
        )
    )
    alert = _alert(alerts, "exclusion_rate")
    assert alert is not None, "50 % de sessions exclues doit dépasser 0,20"
    assert alert["threshold"] == pytest.approx(EXCLUSION_RATE_MAX_DEFAULT)
