"""Report rendering — report_prep (deterministic draft) + report_assemble (blocks injection)."""

from __future__ import annotations

import json
from pathlib import Path

from helpers import make_step, make_usage, tzutc

from weekly_telemetry_aggregator.aggregator import aggregate
from weekly_telemetry_aggregator.config import TelemetryConfig
from weekly_telemetry_aggregator.models import Period
from weekly_telemetry_aggregator.report import report_assemble, report_blocks_draft, report_prep
from weekly_telemetry_aggregator.writer import summary_to_dict

RUN = tzutc(2026, 8, 12)
DATE = "2026-08-12"


def _cfg(tmp_path: Path) -> TelemetryConfig:
    cfg = TelemetryConfig()
    cfg.output_dir = tmp_path
    cfg.opencode_db_path = "/nonexistent/opencode.db"  # self-cost → None safely
    cfg.project_root = tmp_path
    cfg.open_browser = False  # ceinture : conftest force déjà WEEKLY_NO_BROWSER=1
    return cfg


def _write_summary(tmp_path: Path) -> None:
    period = Period(start=tzutc(2026, 8, 5), end=RUN)
    u = make_usage("r", [make_step("r", tzutc(2026, 8, 6, 10), cost=0.5)], title="S")
    data = summary_to_dict(aggregate([u], period=period, generated_at=RUN))
    (tmp_path / f"weekly-summary-{DATE}.json").write_text(
        __import__("json").dumps(data, ensure_ascii=False), encoding="utf-8"
    )


def test_report_prep_requires_summary(tmp_path: Path):
    draft, ctx = report_prep(_cfg(tmp_path), anchor=RUN.isoformat())
    assert draft is None
    assert ctx is None


def test_report_prep_renders_draft(tmp_path: Path):
    _write_summary(tmp_path)
    cfg = _cfg(tmp_path)
    draft, ctx = report_prep(cfg, anchor=RUN.isoformat())
    assert draft is not None and ctx is not None
    text = draft.read_text(encoding="utf-8")
    assert "<!-- QUALITY_BLOCK -->" in text
    assert "## 1. Vue d'ensemble" in text
    assert "## 4. Constats qualitatifs" in text
    assert f"weekly-summary-{DATE}.json" in text


def test_report_assemble_requires_draft(tmp_path: Path):
    path, warnings, rc = report_assemble(_cfg(tmp_path), anchor=RUN.isoformat())
    assert path is None
    assert rc == 2
    assert any("draft" in w for w in warnings)


def test_report_assemble_placeholder_without_blocks(tmp_path: Path):
    _write_summary(tmp_path)
    cfg = _cfg(tmp_path)
    # prep writes the draft; assemble runs but no blocks file → placeholder.
    report_prep(cfg, anchor=RUN.isoformat())
    final_path, warnings, rc = report_assemble(cfg, anchor=RUN.isoformat())
    assert rc == 0
    assert final_path is not None
    assert "<!-- QUALITY_BLOCK -->" not in final_path.read_text(encoding="utf-8")
    assert "non disponible" in final_path.read_text(encoding="utf-8")
    assert any("placeholder" in w for w in warnings)


def test_report_assemble_injects_blocks(tmp_path: Path):
    _write_summary(tmp_path)
    cfg = _cfg(tmp_path)
    (tmp_path / f"weekly-report-blocks-{DATE}.md").write_text(
        "*Insertion de test.*\n"
        "- constat : la régression de coût provient des sessions longues avec cache élevé.\n"
        "- constat : le skill X est redondant avec Y et doit être fusionné après revue.\n"
        "- constat : les violations harness mcp-tool-poisoning dominent le lint.\n"
        "- recommandation : corriger les violations en priorité puis relancer le lint.\n",
        encoding="utf-8",
    )
    report_prep(cfg, anchor=RUN.isoformat())
    final_path, warnings, rc = report_assemble(cfg, anchor=RUN.isoformat())
    assert rc == 0
    text = final_path.read_text(encoding="utf-8")
    assert "régression de coût" in text
    assert "<!-- QUALITY_BLOCK -->" not in text
    assert warnings == []
    assert not (tmp_path / f"weekly-report-draft-{DATE}.md").exists()  # draft removed


# ============================================================ v5.28 (P5.1/P5.2)


def test_report_blocks_draft_writes_blocks(tmp_path: Path):
    _write_summary(tmp_path)
    cfg = _cfg(tmp_path)
    path, warnings, rc = report_blocks_draft(cfg, anchor=RUN.isoformat())
    assert rc == 0
    assert path is not None
    assert path.name == f"weekly-report-blocks-auto-{DATE}.md"  # v5.29 : déterministe -> -auto-
    text = path.read_text(encoding="utf-8")
    assert "brouillon automatique" in text
    assert "Recommandations" in text


def test_report_assemble_rejects_too_short_blocks(tmp_path: Path):
    _write_summary(tmp_path)
    cfg = _cfg(tmp_path)
    (tmp_path / f"weekly-report-blocks-{DATE}.md").write_text("*court*", encoding="utf-8")
    report_prep(cfg, anchor=RUN.isoformat())
    path, warnings, rc = report_assemble(cfg, anchor=RUN.isoformat())
    assert rc == 2
    assert path is None
    assert any("trop court" in w for w in warnings)


def test_report_prep_renders_top_harness_rules(tmp_path: Path):
    _write_summary(tmp_path)
    cfg = _cfg(tmp_path)
    (tmp_path / f"weekly-harness-digest-{DATE}.json").write_text(
        __import__("json").dumps(
            {
                "harness_include": {
                    "unscoped_files": [".opencode/package.json"],
                },
                "harness_counts": {
                    "files_scanned": 4,
                    "components_scanned": 4,
                    "findings_raw": 3,
                    "findings_unique": 2,
                },
                "inspection": {
                    "summary": {"errors": 2, "warnings": 3},
                    "uncategorized": [
                        {
                            "path": "x",
                            "findings": [
                                {
                                    "rule": "security/mcp-tool-poisoning",
                                    "severity": "warning",
                                    "message": "m",
                                },
                                {
                                    "rule": "security/mcp-tool-poisoning",
                                    "severity": "warning",
                                    "message": "m2",
                                },
                                {
                                    "rule": "security/obfuscation",
                                    "severity": "error",
                                    "message": "o",
                                },
                            ],
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    draft, ctx = report_prep(cfg, anchor=RUN.isoformat())
    assert draft is not None and ctx is not None
    text = draft.read_text(encoding="utf-8")
    assert "mcp-tool-poisoning" in text
    assert "2 violation(s)" in text
    assert "4 fichier(s), 4 composant(s) inspecté(s)" in text
    assert "hors allowlist non scannées" in text


# ============================================================ v5.28 (synthèse + audit + watch)


def _write_ecosystem(tmp_path: Path) -> None:
    ecosystem = {
        "schema_version": 2,
        "new_items": [
            {
                "name": "adeo/ai-skills v0.2.0",
                "category": "repo",
                "repo_url": "https://github.com/adeo/ai-skills",
                "description": "Skills internes ADEO",
                "published_at": "2026-08-09T00:00:00Z",
                "found_via": ["github:watch-repos"],
                "new_repo": False,
            }
        ],
        "core_changes": [],
        "counts_by_source": {},
        "counts_by_category": {},
        "watch_repos": ["adeo/ai-skills"],
        "warnings": [],
    }
    (tmp_path / f"weekly-ecosystem-{DATE}.json").write_text(
        __import__("json").dumps(ecosystem, ensure_ascii=False), encoding="utf-8"
    )


def _seed_selection(tmp_path: Path) -> None:
    _write_summary(tmp_path)
    p = tmp_path / f"weekly-summary-{DATE}.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    data["selection"] = {
        "window_touched": 3,
        "counted": 1,
        "excluded_active": 1,
        "excluded_no_activity": 1,
        "excluded_advisor": 0,
        "excluded_error": 0,
        "recent": [
            {
                "session_id": "ses_a",
                "title": "En cours",
                "agent": None,
                "cost": 0.0,
                "updated": "2026-08-12T00:00:00Z",
                "status": "active",
            },
            {
                "session_id": "ses_b",
                "title": "Comptée",
                "agent": None,
                "cost": 0.5,
                "updated": "2026-08-11T00:00:00Z",
                "status": "included",
            },
        ],
    }
    p.write_text(__import__("json").dumps(data, ensure_ascii=False), encoding="utf-8")


def test_report_prep_renders_synthese_audit_and_watch(tmp_path: Path):
    _seed_selection(tmp_path)
    _write_ecosystem(tmp_path)
    cfg = _cfg(tmp_path)
    draft, ctx = report_prep(cfg, anchor=RUN.isoformat())
    assert draft is not None
    text = draft.read_text(encoding="utf-8")
    assert "## Synthèse" in text
    assert "### Audit de sélection" in text
    assert "`ses_a`" in text and "`ses_b`" in text
    assert "3 sessions touchées" in text
    assert "## 6. Veille — recommandations & nouveautés" in text
    assert "adeo/ai-skills" in text
    assert "github:watch-repos" in text


def test_report_prep_no_audit_when_empty_selection(tmp_path: Path):
    _write_summary(tmp_path)
    _write_ecosystem(tmp_path)
    draft, ctx = report_prep(_cfg(tmp_path), anchor=RUN.isoformat())
    assert draft is not None
    text = draft.read_text(encoding="utf-8")
    assert "### Audit de sélection" not in text  # pas de doublon/bruit sans trace


def test_report_prep_watch_source_failure_note(tmp_path: Path):
    _write_summary(tmp_path)
    eco = {
        "new_items": [],
        "core_changes": [],
        "counts_by_source": {},
        "counts_by_category": {},
        "watch_repos": ["adeo/ai-skills"],
        "warnings": [
            {
                "source": "github:watch-repos",
                "message": "API indisponible / rate-limitated; source ignorée pour ce run (...)",
            }
        ],
    }
    (tmp_path / f"weekly-ecosystem-{DATE}.json").write_text(
        json.dumps(eco, ensure_ascii=False), encoding="utf-8"
    )
    draft, ctx = report_prep(_cfg(tmp_path), anchor=RUN.isoformat())
    assert draft is not None
    text = draft.read_text(encoding="utf-8")
    assert "Sources suivies (watch) : adeo/ai-skills" in text
    assert "Source GitHub indisponible" in text
    assert "Aucune activité sur la fenêtre" not in text


def test_report_top_rules_filters_ignored(tmp_path: Path):
    """K5: les règles de harness_ignored_rules disparaissent du top §5."""
    _write_summary(tmp_path)
    cfg = _cfg(tmp_path)
    cfg.harness_ignored_rules = ["security/mcp-tool-poisoning"]
    (tmp_path / f"weekly-harness-digest-{DATE}.json").write_text(
        json.dumps(
            {
                "inspection": {
                    "summary": {"errors": 2, "warnings": 3},
                    "uncategorized": [
                        {
                            "findings": [
                                {
                                    "rule": "security/mcp-tool-poisoning",
                                    "severity": "high",
                                    "message": "x",
                                },
                                {
                                    "rule": "security/obfuscation",
                                    "severity": "high",
                                    "message": "y",
                                },
                            ]
                        }
                    ],
                }
            }
        ),
        encoding="utf-8",
    )
    draft, ctx = report_prep(cfg, anchor=RUN.isoformat())
    assert draft is not None
    text = draft.read_text(encoding="utf-8")
    assert "security/obfuscation" in text
    assert (
        "`security/mcp-tool-poisoning` —" not in text
    )  # absente du top règles (la note de config la liste, elle)
    assert "Règles exclues du top" in text


def test_report_blocks_draft_includes_part3_findings(tmp_path: Path):
    """v5.29 : les findings de l'audit qualitatif (Partie 3) sont rendus dans les blocs §4."""
    _write_summary(tmp_path)
    cfg = _cfg(tmp_path)
    (tmp_path / f"weekly-quality-findings-{DATE}.json").write_text(
        json.dumps(
            {
                "findings": [
                    {
                        "session_id": "s1",
                        "category": "loop",
                        "severity": "high",
                        "description": "retries répétés sur le même point",
                        "recommendation": "extraire une procédure",
                        "recommendation_type": "skill-candidate",
                        "evidence_summary": "x",
                        "impact_order_of_magnitude": "medium",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    path, warnings, rc = report_blocks_draft(cfg, anchor=RUN.isoformat())
    assert rc == 0
    text = path.read_text(encoding="utf-8")
    assert "Constats de l'audit qualitatif" in text
    assert "[HIGH] loop" in text
    assert "retries répétés" in text


def test_report_annex_lists_unaudited_candidates(tmp_path: Path):
    _write_summary(tmp_path)
    cfg = _cfg(tmp_path)
    (tmp_path / f"weekly-audit-candidates-{DATE}.json").write_text(
        json.dumps(
            {
                "limit": 8,
                "audited": [{"session_id": "s1", "reasons": ["top-cost"]}],
                "unaudited": [{"session_id": "s9", "reasons": ["cost-outlier"]}],
            }
        ),
        encoding="utf-8",
    )
    draft, ctx = report_prep(cfg, anchor=RUN.isoformat())
    assert draft is not None
    text = draft.read_text(encoding="utf-8")
    assert "Candidates d'audit non traitées" in text
    assert "s9" in text


# ============================================================ v5.29 (7b hybride — garde-fous anti-hallucination)


def _write_auto_blocks(tmp_path: Path) -> None:
    """report-blocks-draft a tourné : brouillon déterministe présent."""
    from weekly_telemetry_aggregator.report import report_blocks_draft

    report_blocks_draft(_cfg(tmp_path), anchor=RUN.isoformat())


def _write_findings(tmp_path: Path, findings: list[dict]) -> None:
    (tmp_path / f"weekly-quality-findings-{DATE}.json").write_text(
        json.dumps({"findings": findings}), encoding="utf-8"
    )


def test_assemble_uses_llm_block_when_valid(tmp_path: Path):
    _write_summary(tmp_path)
    _write_auto_blocks(tmp_path)
    cfg = _cfg(tmp_path)
    _write_findings(
        tmp_path,
        [
            {
                "session_id": "s1",
                "category": "loop",
                "severity": "high",
                "recommendation_type": "skill-candidate",
                "description": "d",
                "recommendation": "r",
            }
        ],
    )
    (tmp_path / f"weekly-report-blocks-{DATE}.md").write_text(
        "Semaine dominée par une session en boucle de travail intensif avec de nombreux appels "
        "répétés sur le même point [F:s1#loop]. La maintenance signale des skills probablement "
        "redondants et recommande une fusion manuelle après revue [M:merge-candidate]. "
        "Le budget hebdomadaire est dépassé, alerte à traiter en priorité [A:weekly_budget_usd].\n",
        encoding="utf-8",
    )
    # insights avec maintenance + alerte pour résoudre les balises M/A
    (tmp_path / f"weekly-insights-{DATE}.json").write_text(
        json.dumps(
            {
                "alerts": [
                    {
                        "rule": "weekly_budget_usd",
                        "threshold": 25.0,
                        "observed": 30.0,
                        "severity": "high",
                    }
                ],
                "maintenance": {
                    "findings": [{"category": "merge-candidate", "severity": "medium"}]
                },
            }
        ),
        encoding="utf-8",
    )
    report_prep(cfg, anchor=RUN.isoformat())
    final_path, warnings, rc = report_assemble(cfg, anchor=RUN.isoformat())
    assert rc == 0
    text = final_path.read_text(encoding="utf-8")
    assert "en boucle de travail intensif" in text
    assert "Statut section 4 : prose agent (7b LLM)" in text
    assert warnings == []


def test_assemble_rejects_llm_block_with_digits(tmp_path: Path):
    _write_summary(tmp_path)
    _write_auto_blocks(tmp_path)
    cfg = _cfg(tmp_path)
    (tmp_path / f"weekly-report-blocks-{DATE}.md").write_text(
        "Le budget hebdomadaire est dépassé de 3 fois cette semaine, ce qui constitue un "
        "signal fort de dérive des coûts qu'il convient d'investiguer avant la prochaine "
        "itération pour identifier les sessions responsables et corriger le tir rapidement, "
        "en commençant par les plus coûteuses puis en ajustant les seuils de déclenchement.\n",
        encoding="utf-8",
    )
    report_prep(cfg, anchor=RUN.isoformat())
    final_path, warnings, rc = report_assemble(cfg, anchor=RUN.isoformat())
    assert rc == 0
    text = final_path.read_text(encoding="utf-8")
    assert "chiffres interdits" in text  # statut de rejet
    assert "brouillon automatique" in text
    assert any("rejeté" in w for w in warnings)


def test_assemble_rejects_llm_block_with_unknown_tag(tmp_path: Path):
    _write_summary(tmp_path)
    _write_auto_blocks(tmp_path)
    cfg = _cfg(tmp_path)
    _write_findings(
        tmp_path,
        [
            {
                "session_id": "s1",
                "category": "loop",
                "severity": "medium",
                "recommendation_type": "prompting-habit",
                "description": "d",
                "recommendation": "r",
            }
        ],
    )
    (tmp_path / f"weekly-report-blocks-{DATE}.md").write_text(
        "Un constat inventé est cité dans le bloc avec une balise qui ne correspond à aucun "
        "finding de la semaine, ce qui doit déclencher le rejet automatique du bloc et le "
        "retour au brouillon déterministe pour garantir l'absence d'hallucination [F:ses_invente#loop].\n",
        encoding="utf-8",
    )
    report_prep(cfg, anchor=RUN.isoformat())
    final_path, warnings, rc = report_assemble(cfg, anchor=RUN.isoformat())
    assert rc == 0
    text = final_path.read_text(encoding="utf-8")
    assert "balise inconnue [F:ses_invente#loop]" in text
    assert "brouillon automatique" in text


def test_assemble_falls_back_to_auto_when_no_llm_block(tmp_path: Path):
    _write_summary(tmp_path)
    _write_auto_blocks(tmp_path)
    cfg = _cfg(tmp_path)
    report_prep(cfg, anchor=RUN.isoformat())
    final_path, warnings, rc = report_assemble(cfg, anchor=RUN.isoformat())
    assert rc == 0
    text = final_path.read_text(encoding="utf-8")
    assert "Statut section 4 : brouillon automatique (report-blocks-draft)" in text
    assert "brouillon automatique" in text


def test_validate_llm_blocks_high_coverage_warning():
    from weekly_telemetry_aggregator.report import validate_llm_blocks

    findings = {
        "findings": [
            {"session_id": "s1", "category": "loop", "severity": "high"},
            {"session_id": "s2", "category": "context-bloat", "severity": "high"},
        ]
    }
    text = "Seule la boucle est couverte [F:s1#loop].\n"
    violations, coverage = validate_llm_blocks(text, findings, None)
    assert violations == []
    assert any("s2#context-bloat" in c for c in coverage)  # high non cité -> warning, pas de rejet


def test_validate_llm_blocks_rejects_digits_and_unknown():
    from weekly_telemetry_aggregator.report import validate_llm_blocks

    violations, coverage = validate_llm_blocks(
        "Le coût a doublé : 39$ cette semaine.\n", None, None
    )
    assert any("chiffres" in v for v in violations)
    assert coverage == []

def test_validate_llm_blocks_allows_dates_percent_versions():
    from weekly_telemetry_aggregator.report import validate_llm_blocks

    # dates ISO, pourcentages et versions sémantiques ne sont pas des « chiffres libres »
    text = "Bogue corrigé en v6.0.l le 2026-08-26 (reprise à 12,5% de couverture).\n"
    violations, coverage = validate_llm_blocks(text, None, None)
    assert violations == []
    assert coverage == []

    # un coût libre reste interdit
    violations2, _ = validate_llm_blocks("coût de 39$ cette semaine.\n", None, None)
    assert any("chiffres" in v for v in violations2)


def test_report_annex_groups_identical_warnings(tmp_path: Path):
    """v5.30 (F) : les warnings identiques sont groupés avec compteur dans l'annexe."""
    _write_summary(tmp_path)
    cfg = _cfg(tmp_path)
    # injecter des warnings groupés dans le summary
    p = tmp_path / f"weekly-summary-{DATE}.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    data["warnings"] = [
        {
            "session_id": "s1",
            "message": "session sans télémétrie persistée en DB",
            "partial": False,
        },
        {
            "session_id": "s2",
            "message": "session sans télémétrie persistée en DB",
            "partial": False,
        },
        {"session_id": "s3", "message": "autre warning", "partial": False},
    ]
    p.write_text(json.dumps(data), encoding="utf-8")
    draft, ctx = report_prep(cfg, anchor=RUN.isoformat())
    assert draft is not None
    text = draft.read_text(encoding="utf-8")
    assert "×2" in text and "télémétrie persistée" in text
    assert "autre warning" in text


def test_report_synthese_lists_all_alerts(tmp_path: Path):
    _write_summary(tmp_path)
    cfg = _cfg(tmp_path)
    (tmp_path / f"weekly-insights-{DATE}.json").write_text(
        json.dumps(
            {
                "alerts": [
                    {
                        "rule": "weekly_budget_usd",
                        "threshold": 25.0,
                        "observed": 51.7,
                        "severity": "high",
                    },
                    {
                        "rule": "lint_violations_max",
                        "threshold": 10,
                        "observed": 2734,
                        "severity": "medium",
                    },
                ],
                "maintenance": {"findings": []},
            }
        ),
        encoding="utf-8",
    )
    draft, ctx = report_prep(cfg, anchor=RUN.isoformat())
    text = draft.read_text(encoding="utf-8")
    assert "weekly_budget_usd" in text and "lint_violations_max" in text


def test_report_outliers_state_computed_small_sample(tmp_path: Path):
    _write_summary(tmp_path)
    cfg = _cfg(tmp_path)
    p = tmp_path / f"weekly-summary-{DATE}.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    data["cost_outliers_state"] = "computed:small-sample"
    data["cost_outliers"] = [{"session_id": "s1", "cost_usd": 4.2, "z_score": 3.4}]
    p.write_text(json.dumps(data), encoding="utf-8")
    draft, ctx = report_prep(cfg, anchor=RUN.isoformat())
    text = draft.read_text(encoding="utf-8")
    assert "**calculés**" in text and "computed:small-sample" in text
    assert "non calculés" not in text


def test_report_daily_totals_complete_window(tmp_path: Path):
    _write_summary(tmp_path)
    cfg = _cfg(tmp_path)
    p = tmp_path / f"weekly-summary-{DATE}.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    data["period"] = {"start": "2026-08-05T00:00:00Z", "end": "2026-08-12T00:00:00Z"}
    data["daily_totals"] = [
        {"date": "2026-08-07", "cost_usd": 1.0, "total_tokens": 100, "cache_hit_rate": 0.9}
    ]
    p.write_text(json.dumps(data), encoding="utf-8")
    draft, ctx = report_prep(cfg, anchor=RUN.isoformat())
    text = draft.read_text(encoding="utf-8")
    for day in ("2026-08-05", "2026-08-06", "2026-08-07", "2026-08-08", "2026-08-12"):
        assert day in text
    assert "| 2026-08-05 | 0.0000 | 0 |" in text  # jour vide en zéro explicite


def test_report_harness_budget_rendered(tmp_path: Path):
    _write_summary(tmp_path)
    cfg = _cfg(tmp_path)
    (tmp_path / f"weekly-harness-digest-{DATE}.json").write_text(
        json.dumps(
            {
                "inspection": {"summary": {"errors": 2, "warnings": 3}},
                "budget": {
                    "total_tokens": 11884,
                    "always_loaded": 5575,
                    "on_demand": 6309,
                    "always_loaded_ratio": 0.47,
                    "heaviest": "claude_md/CLAUDE",
                },
                "triggers": {"skill_count": 0, "overlaps": []},
                "dependencies": {"total_edges": 3, "broken": []},
            }
        ),
        encoding="utf-8",
    )
    draft, ctx = report_prep(cfg, anchor=RUN.isoformat())
    text = draft.read_text(encoding="utf-8")
    assert "Budget tokens" in text and "11,884" in text
    assert "Dépendances : 3 arêtes" in text


def test_report_harness_remediation_status_rendered(tmp_path: Path):
    _write_summary(tmp_path)
    cfg = _cfg(tmp_path)
    (tmp_path / f"weekly-harness-digest-{DATE}.json").write_text(
        json.dumps({"inspection": {"summary": {"errors": 0, "warnings": 0}}}),
        encoding="utf-8",
    )
    (tmp_path / f"weekly-harness-remediation-{DATE}.json").write_text(
        json.dumps(
            {
                "summary": {
                    "applied": 0,
                    "proposed": 2,
                    "manual": 1,
                    "blocked": 3,
                    "rolled_back": 0,
                },
                "postcheck": {"status": "not_run", "reason": "no project changes were requested"},
            }
        ),
        encoding="utf-8",
    )
    draft, _ctx = report_prep(cfg, anchor=RUN.isoformat())
    text = draft.read_text(encoding="utf-8")
    assert "Remédiation harness" in text
    assert "2 proposée(s)" in text
    assert "1 manuelle(s)" in text


def test_report_section6_renders_watch_recommendations(tmp_path: Path):
    """v5.31 : les recommandations de la veille critique sont rendues en tête du §6."""
    _write_summary(tmp_path)
    cfg = _cfg(tmp_path)
    (tmp_path / f"weekly-watch-findings-{DATE}.json").write_text(
        json.dumps(
            {
                "findings": [
                    {
                        "session_id": None,
                        "category": "adopt",
                        "severity": "high",
                        "description": "Skill X du marché fait la vérification Jira efficacement",
                        "evidence_summary": "pattern coûteux détecté (F:ses_023b8f80#context-bloat)",
                        "recommendation": "Évaluer l'adoption de X avant la prochaine session de vérification",
                        "recommendation_type": "watch-adopt",
                        "impact_order_of_magnitude": "large",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    draft, ctx = report_prep(cfg, anchor=RUN.isoformat())
    assert draft is not None
    text = draft.read_text(encoding="utf-8")
    assert "### Recommandations (regard critique" in text
    assert "[HIGH] adopt" in text
    assert "jamais d'installation automatique" in text
    assert "Évaluer l'adoption de X" in text


def test_report_section5_renders_coherence_findings(tmp_path: Path):
    """v5.31 : les findings de cohérence de l'environnement sont rendus en tête du §5."""
    _write_summary(tmp_path)
    cfg = _cfg(tmp_path)
    (tmp_path / f"weekly-coherence-findings-{DATE}.json").write_text(
        json.dumps(
            {
                "findings": [
                    {
                        "category": "duplicate",
                        "tag": "merge",
                        "severity": "high",
                        "description": "Les agents frontend-developer et full-stack-developer chevauchent leurs rôles UI",
                        "evidence_summary": "références croisées + 60 % de rôles communs",
                        "recommendation": "Fusionner ou clarifier les frontières des rôles",
                        "recommendation_type": "coherence-merge",
                        "impact_order_of_magnitude": "large",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    draft, ctx = report_prep(cfg, anchor=RUN.isoformat())
    assert draft is not None
    text = draft.read_text(encoding="utf-8")
    assert "## 5. Santé de l'environnement (harness + cohérence)" in text
    assert "### Cohérence de l'environnement" in text
    assert "**merge**" in text and "Fusionner ou clarifier" in text


def test_git_log_filters_real_auto_commits(tmp_path: Path):
    """v5.31 (b) : seuls les vrais commits auto-rédigés (revue hebdo) sont comptés."""
    import subprocess as sp

    repo = tmp_path / "repo"
    repo.mkdir()
    sp.run(["git", "init", "-q"], cwd=repo, check=True)
    sp.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    sp.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    (repo / "a.txt").write_text("x")
    sp.run(["git", "add", "-A"], cwd=repo, check=True)
    sp.run(
        ["git", "commit", "-q", "-m", "skill:demo (auto-rédigé, revue hebdo 2026-08-14)"],
        cwd=repo,
        check=True,
    )
    (repo / "b.txt").write_text("y")
    sp.run(["git", "add", "-A"], cwd=repo, check=True)
    sp.run(
        ["git", "commit", "-q", "-m", "feat: skills auto-rédigés (pipeline)"], cwd=repo, check=True
    )

    from weekly_telemetry_aggregator.report import _git_log

    commits = _git_log(repo, "2026-08-01T00:00:00Z")
    assert len(commits) == 1  # le faux positif (feat mentionnant auto-rédigés) est filtré
    assert commits[0]["subject"].startswith("skill:demo")


def test_assemble_missing_draft_message_explicit(tmp_path: Path):
    """v5.31 (a) : le message draft inexistant rappelle la consommation par assemble."""
    _write_summary(tmp_path)
    cfg = _cfg(tmp_path)
    path, warnings, rc = report_assemble(cfg, anchor=RUN.isoformat())
    assert rc == 2
    assert any("consommé" in w and "report-prep" in w for w in warnings)


# ---------------------------------------------------------------- v6.0.k template


def test_template_maintenance_and_unscoped_lines_are_not_glued(tmp_path: Path):
    """v6.0.k : trim_blocks ne colle plus les bullets (§7) ni la ligne unscoped (§5)."""
    from weekly_telemetry_aggregator.run_state import activate_run

    active = activate_run(tmp_path, DATE, RUN)
    _write_summary(active.run_dir)
    # insights avec 2 constats de maintenance + digests enrichis
    (active.run_dir / f"weekly-insights-{DATE}.json").write_text(
        json.dumps(
            {
                "alerts": [],
                "maintenance": {
                    "findings": [
                        {
                            "severity": "HIGH",
                            "description": "first finding",
                            "recommendation": "fix it",
                            "evidence_summary": "file:line proof one",
                        },
                        {
                            "severity": "MEDIUM",
                            "description": "second finding",
                            "recommendation": "fix it too",
                            "evidence_summary": "file:line proof two",
                        },
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    (active.run_dir / f"weekly-harness-digest-{DATE}.json").write_text(
        json.dumps(
            {
                "inspection": {"summary": {}},
                "harness_scope": {
                    "unscoped_files": [".opencode/a.json", ".opencode/b.json"],
                },
            }
        ),
        encoding="utf-8",
    )
    draft, ctx = report_prep(_cfg(tmp_path), anchor=RUN.isoformat())
    assert draft is not None
    text = draft.read_text(encoding="utf-8")
    assert "- **[HIGH]** first finding → fix it — preuve : file:line proof one\n" in text
    assert "\n- **[MEDIUM]** second finding → fix it too — preuve : file:line proof two" in text
    assert "proof one- **[MEDIUM]" not in text  # plus de collage (trim_blocks)
    # ligne unscoped non collée à la section suivante (trim_blocks, v6.0.k)
    assert "b.json\n" in text
    assert "b.json## 6." not in text
    # annexe : répertoire du run
    assert f"`runs/{active.run_id}/`" in text


def test_assemble_renders_html_with_injected_quality_block(tmp_path: Path, monkeypatch):
    """report_assemble branche le renderer HTML avec le bloc effectivement injecté."""
    _write_summary(tmp_path)
    _write_auto_blocks(tmp_path)
    cfg = _cfg(tmp_path)
    report_prep(cfg, anchor=RUN.isoformat())
    seen: dict = {}

    def fake_render(cfg_, *, anchor, ctx, quality_block):
        seen.update(anchor=anchor, ctx=ctx, quality_block=quality_block)
        return tmp_path / "html" / f"weekly-report-{DATE}.html"

    monkeypatch.setattr("weekly_telemetry_aggregator.report.render_html_report", fake_render)
    opened: list[Path | None] = []
    monkeypatch.setattr(
        "weekly_telemetry_aggregator.report.open_html_report",
        lambda cfg_, path: opened.append(path),
    )
    final_path, warnings, rc = report_assemble(cfg, anchor=RUN.isoformat())
    assert rc == 0 and final_path is not None
    # le bloc passé au renderer est exactement celui injecté dans le MD final
    auto_text = (tmp_path / f"weekly-report-blocks-auto-{DATE}.md").read_text(encoding="utf-8")
    assert seen["quality_block"] == auto_text
    assert seen["ctx"] is not None and seen["ctx"]["date"] == DATE
    assert seen["anchor"] == RUN.isoformat()
    # auto-open branché avec le chemin retourné par le renderer
    assert opened == [tmp_path / "html" / f"weekly-report-{DATE}.html"]


def test_assemble_html_disabled_is_silent_noop(tmp_path: Path):
    """html_report_dir="" → aucun rendu HTML, assemble OK sans erreur."""
    _write_summary(tmp_path)
    cfg = _cfg(tmp_path)
    cfg.html_report_dir = ""  # génération HTML désactivée
    report_prep(cfg, anchor=RUN.isoformat())
    final_path, warnings, rc = report_assemble(cfg, anchor=RUN.isoformat())
    assert rc == 0 and final_path is not None
    assert not (tmp_path / "reports").exists()
