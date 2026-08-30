"""Tests du module curation (R4 curation/GC + R8 TTL/décroissance)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from weekly_telemetry_aggregator import curation


def _iso(days_ago: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()


# ----------------------------------------------------------------- user-origin


def test_curation_protects_user_origin():
    """Un skill d'origin='user' ne génère jamais archive/merge/delete."""
    findings = [
        {"tag_action": "merge", "target_skill_id": "my/user-skill"},
        {"tag_action": "delete", "target_skill_id": "my/user-skill"},
        {"tag_action": "reference", "target_skill_id": "my/user-skill"},
    ]
    catalog = [
        {
            "skill_id": "my/user-skill",
            "metadata": {"origin": "user", "ttl_policy": None},
        }
    ]

    decisions = curation.decide_actions(findings, catalog)

    assert decisions, "au moins une décision attendue"
    for d in decisions:
        assert d["target_skill_id"] == "my/user-skill"
        assert d["action"] == "skip", d
        assert d["reason"] == "user-origin protected"
    # aucune action destructive ne doit fuiter
    assert all(d["action"] not in {"merge", "delete", "archive"} for d in decisions)


def test_curation_user_explicit_pin_allowed():
    """Un pin explicitement levé sur un skill user est toléré (action='pin')."""
    findings = [{"tag_action": "pin", "target_skill_id": "my/user-skill"}]
    catalog = [{"skill_id": "my/user-skill", "metadata": {"origin": "user", "ttl_policy": None}}]

    decisions = curation.decide_actions(findings, catalog)

    assert len(decisions) == 1
    assert decisions[0]["action"] == "pin"


def test_curation_pinned_never_archived_or_deleted():
    """ttl_policy=='pin' force action='pin', jamais delete/archive."""
    findings = [{"tag_action": "delete", "target_skill_id": "pinned/skill"}]
    catalog = [
        {
            "skill_id": "pinned/skill",
            "metadata": {"origin": "weekly-background", "ttl_policy": "pin"},
        }
    ]

    decisions = curation.decide_actions(findings, catalog)

    assert len(decisions) == 1
    assert decisions[0]["action"] == "pin"


def test_curation_normal_emit_action():
    """Un skill non-user non-pinned émet l'action du finding."""
    findings = [{"tag_action": "merge", "target_skill_id": "dup/skill"}]
    catalog = [
        {"skill_id": "dup/skill", "metadata": {"origin": "weekly-background", "ttl_policy": None}}
    ]

    decisions = curation.decide_actions(findings, catalog)

    assert decisions[0]["action"] == "merge"


def test_curation_ignores_irrelevant_tag_action():
    """Les findings hors ensemble d'action ne produisent aucune décision."""
    findings = [{"tag_action": "summary", "target_skill_id": "x/skill"}]
    catalog = [{"skill_id": "x/skill", "metadata": {"origin": "user"}}]

    assert curation.decide_actions(findings, catalog) == []


# --------------------------------------------------------------- ttl archive


def test_ttl_archive_stale():
    """last_loaded > 90j -> archivé ; pin -> NON ; load_count==0 sur 3 runs -> archivé."""
    usage = [
        # vieux de 100j, charge normal -> doit être archivé (stale)
        {
            "skill_id": "stale/skill",
            "ttl_policy": None,
            "usage": {"last_loaded": _iso(100), "load_count": 5},
        },
        # vieux de 100j mais épinglé -> exempté
        {
            "skill_id": "pinned/old",
            "ttl_policy": "pin",
            "usage": {"last_loaded": _iso(100), "load_count": 5},
        },
        # chargé récemment, jamais utilisé sur 3 runs -> archivé (zero_three)
        {
            "skill_id": "unused/skill",
            "ttl_policy": None,
            "usage": {"last_loaded": _iso(1), "load_count": 0},
        },
    ]

    archived = curation.ttl_archive_candidates(usage, runs_seen=3, stale_days=90)

    assert "stale/skill" in archived
    assert "unused/skill" in archived
    assert "pinned/old" not in archived


def test_ttl_archive_not_stale_when_recent():
    """Un skill chargé récemment avec load_count>0 n'est pas archivé."""
    usage = [
        {
            "skill_id": "active/skill",
            "ttl_policy": None,
            "usage": {"last_loaded": _iso(2), "load_count": 12},
        }
    ]

    assert curation.ttl_archive_candidates(usage, runs_seen=5) == []


def test_ttl_archive_zero_load_needs_three_runs():
    """load_count==0 n'archive qu'après runs_seen>=3."""
    usage = [
        {
            "skill_id": "cold/skill",
            "ttl_policy": None,
            "usage": {"last_loaded": _iso(1), "load_count": 0},
        }
    ]

    assert curation.ttl_archive_candidates(usage, runs_seen=2) == []
    assert curation.ttl_archive_candidates(usage, runs_seen=3) == ["cold/skill"]


def test_ttl_archive_exempts_pin_even_zero():
    """Un skill pin même à load_count==0 n'est jamais archivé."""
    usage = [
        {
            "skill_id": "pinned/cold",
            "ttl_policy": "pin",
            "usage": {"last_loaded": _iso(1), "load_count": 0},
        }
    ]

    assert curation.ttl_archive_candidates(usage, runs_seen=10) == []


def test_ttl_archive_exempts_user_origin_even_without_catalog():
    """TTL must honor origin=user protection before curation has a catalog."""
    usage = [
        {
            "skill_id": "user/skill",
            "origin": "user",
            "usage": {"last_loaded": _iso(120), "load_count": 0},
        }
    ]

    assert curation.ttl_archive_candidates(usage, runs_seen=3) == []


# ----------------------------------------------------- WAVE 2.5 wiring (Phases 1-2)


def _ns(**kw):
    from argparse import Namespace

    return Namespace(**kw)


def _cfg(output_dir, project_root):
    from weekly_telemetry_aggregator.config import TelemetryConfig

    return TelemetryConfig(output_dir=output_dir, project_root=project_root)


def test_skill_curate_autoload_and_decisions(tmp_path):
    """Phase 1 : sans --coherence/--catalog, lit le run actif (auto-load).

    Décisions NON vides (archive-from-coherence + merge) sans piping LLM.
    """
    from weekly_telemetry_aggregator.cli import _cmd_skill_curate

    date = "2026-08-28"
    run = tmp_path / "reports"
    run.mkdir()
    (run / f"weekly-coherence-findings-{date}.json").write_text(
        json.dumps(
            [
                {"tag_action": "merge", "target_skill_id": "dup/skill", "reason": "redondant"},
                {
                    "tag_action": "archive",
                    "target_skill_id": "old/skill",
                    "recommendation": "stale 120j",
                },
            ]
        ),
        encoding="utf-8",
    )
    (run / f"weekly-summary-{date}.json").write_text(
        json.dumps(
            {
                "skill_catalog": [
                    {
                        "skill_id": "dup/skill",
                        "metadata": {"origin": "weekly-background", "ttl_policy": None},
                    },
                    {
                        "skill_id": "old/skill",
                        "metadata": {"origin": "weekly-background", "ttl_policy": None},
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    args = _ns(
        anchor=date,
        coherence=None,
        catalog=None,
        usage=None,
        runs_seen=0,
        stale_days=90,
        apply=False,
    )
    rc = _cmd_skill_curate(args, _cfg(run, tmp_path))
    assert rc == 0
    # Le dry-run publie un rapport d'exécution, sans déplacer de skill.
    manifest = json.loads((run / f"skill-curate-{date}.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 1
    assert manifest["mode"] == "dry-run"
    assert manifest["dry_run"] is True
    assert manifest["decisions"]


def test_skill_curate_apply_archive_moves_and_idempotent(tmp_path):
    """Phase 2 : --apply déplace le skill vers _archive/<date>/ (idempotent, jamais delete)."""
    from weekly_telemetry_aggregator.cli import _cmd_skill_curate

    date = "2026-08-28"
    proj = tmp_path / "proj"
    skill = proj / ".opencode" / "skills" / "old-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: old-skill\ndescription: stale skill\nmetadata:\n  origin: weekly-background\n  verification: unit test\n  ttl_policy: null\n---\n## How\nbody\n",
        encoding="utf-8",
    )
    run = tmp_path / "reports"
    run.mkdir()
    (run / f"weekly-coherence-findings-{date}.json").write_text(
        json.dumps(
            [{"tag_action": "archive", "target_skill_id": "old-skill", "recommendation": "stale"}]
        ),
        encoding="utf-8",
    )
    (run / f"weekly-summary-{date}.json").write_text(
        json.dumps(
            {
                "skill_catalog": [
                    {
                        "skill_id": "old-skill",
                        "metadata": {"origin": "weekly-background", "ttl_policy": None},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    args = _ns(
        anchor=date,
        coherence=None,
        catalog=None,
        usage=None,
        runs_seen=0,
        stale_days=90,
        apply=True,
    )
    assert _cmd_skill_curate(args, _cfg(run, proj)) == 0
    archived = proj / ".opencode" / "skills" / "_archive" / date / "old-skill"
    assert archived.is_dir(), "skill déplacé vers _archive"
    assert not skill.exists(), "source retirée (déplacée, pas supprimée)"

    # 2e run -> idempotent : aucune erreur, une seule copie
    assert _cmd_skill_curate(args, _cfg(run, proj)) == 0
    assert archived.is_dir()
    assert not skill.exists()


def test_skill_curate_merge_pin_stay_proposition(tmp_path):
    """Phase 2/E : merge/reference/pin -> proposition (aucune op fs), compté proposed."""
    from weekly_telemetry_aggregator.cli import _cmd_skill_curate

    date = "2026-08-28"
    proj = tmp_path / "proj"
    skill = proj / ".opencode" / "skills" / "dup-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: dup-skill\nmetadata:\n  origin: weekly-background\n---\nbody\n",
        encoding="utf-8",
    )
    run = tmp_path / "reports"
    run.mkdir()
    (run / f"weekly-coherence-findings-{date}.json").write_text(
        json.dumps(
            [{"tag_action": "merge", "target_skill_id": "dup-skill", "reason": "redondant"}]
        ),
        encoding="utf-8",
    )
    (run / f"weekly-summary-{date}.json").write_text(
        json.dumps(
            {
                "skill_catalog": [
                    {
                        "skill_id": "dup-skill",
                        "metadata": {"origin": "weekly-background", "ttl_policy": None},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    args = _ns(
        anchor=date,
        coherence=None,
        catalog=None,
        usage=None,
        runs_seen=0,
        stale_days=90,
        apply=True,
    )
    assert _cmd_skill_curate(args, _cfg(run, proj)) == 0
    assert skill.exists(), "merge = proposition, aucun déplacement fs"
    manifest = json.loads((run / f"skill-curate-{date}.json").read_text(encoding="utf-8"))
    assert manifest["applied"] == 0
    assert manifest["proposed"] == 1
    assert manifest["decisions"][0]["action"] == "merge"
    assert manifest["decisions"][0]["status"] == "proposed"


def test_safe_git_move_idempotent(tmp_path):
    """safe_git_move : déplace, puis 'exists'/'missing' (idempotent), jamais delete."""
    from weekly_telemetry_aggregator.safe_git_write import safe_git_move

    src = tmp_path / "a.txt"
    src.write_text("x", encoding="utf-8")
    dst = tmp_path / "arch" / "a.txt"
    assert safe_git_move(src, dst)[0] == "moved"
    assert dst.exists() and not src.exists()

    # src absent -> missing (idempotent, pas d'erreur)
    assert safe_git_move(tmp_path / "a.txt", dst)[0] == "missing"

    # dst déjà présent -> exists (idempotent)
    dup = tmp_path / "b.txt"
    dup.write_text("y", encoding="utf-8")
    assert safe_git_move(dup, dst)[0] == "exists"
    assert dup.exists(), "src non détruit quand dst existe"


def test_coherence_has_curation_signal():
    """Phase 4 : détection du signal de curation (champ ou findings)."""
    from weekly_telemetry_aggregator.report import _coherence_has_curation_signal

    assert _coherence_has_curation_signal({"curation_signal": [{"x": 1}]})
    assert _coherence_has_curation_signal({"findings": [{"tag_action": "merge"}]})
    assert _coherence_has_curation_signal([{"tag_action": "archive"}])
    assert not _coherence_has_curation_signal({})
    assert not _coherence_has_curation_signal(None)
    assert not _coherence_has_curation_signal({"findings": [{"tag_action": "summary"}]})


def test_report_template_renders_curation_section():
    """Phase 3 : le bloc Curation réel rendu (appliquée vs P0 non exécutée)."""
    from pathlib import Path as _P

    from jinja2 import Environment

    tpl_dir = _P(__file__).resolve().parents[1] / "weekly_telemetry_aggregator" / "templates"
    tpl_path = tpl_dir / "report_template.md.j2"
    full = tpl_path.read_text(encoding="utf-8")
    # On isole le bloc Curation (du `{% if skill_curate %}` ouvrant jusqu'à
    # `{% if digest %}`), évitant le reste du gabarit qui exige un contexte complet.
    start = full.index("{% if skill_curate %}")
    end = full.index("{% if digest %}", start)
    block = full[start:end]
    env = Environment(autoescape=False, trim_blocks=True, lstrip_blocks=True)

    ctx = {
        "date": "2026-08-28",
        "skill_curate": {
            "applied": 1,
            "proposed": 2,
            "skipped": 0,
            "decisions": [
                {
                    "skill_id": "old/skill",
                    "action": "archive",
                    "reason": "stale",
                    "source": "coherence",
                    "status": "moved",
                },
                {
                    "skill_id": "dup/skill",
                    "action": "merge",
                    "reason": "redondant",
                    "source": "coherence",
                    "status": "proposed",
                },
            ],
        },
        "coherence_curation_signal": False,
    }
    rendered = env.from_string(block).render(**ctx)
    assert "Curation (WAVE 2.5 — appliquée" in rendered
    assert "appliquées" in rendered.lower()
    assert "old/skill" in rendered and "moved" in rendered

    # manifest absent + signal -> avertissement P0
    ctx2 = {"date": "2026-08-28", "skill_curate": None, "coherence_curation_signal": True}
    rendered2 = env.from_string(block).render(**ctx2)
    assert "REQUIRED, non exécutée" in rendered2


# ----------------------------------------------------- R4 archive shape fix (étape 6.6)


def test_skill_curate_split_concatenated_archive_finding(tmp_path, capsys):
    """Finding tag_action=archive avec target_skill_id concaténé (virgules) ->
    N décisions archive INDIVIDUELLES (ids non concaténés), pas une chaîne.

    Reproduit le run réel 2026-08-28 : 1 finding archive dont target_skill_id est
    une chaîne de N ids séparés par virgules.
    """
    from weekly_telemetry_aggregator.cli import _cmd_skill_curate

    date = "2026-08-28"
    run = tmp_path / "reports"
    run.mkdir()
    strong = ["adeo-loadtest-conventions", "backend-migration-standards", "xlsx"]
    concat = ",".join(strong)
    (run / f"weekly-coherence-findings-{date}.json").write_text(
        json.dumps(
            {
                "findings": [
                    {
                        "tag_action": "archive",
                        "target_skill_id": concat,
                        "recommendation": "R4 8/8 (load_count==0)",
                    }
                ],
                "curation_signal": {"R4_archive_candidates": {"strong_8of8": strong}},
            }
        ),
        encoding="utf-8",
    )
    (run / f"weekly-summary-{date}.json").write_text(
        json.dumps({"skill_catalog": []}), encoding="utf-8"
    )

    args = _ns(
        anchor=date,
        coherence=None,
        catalog=None,
        usage=None,
        runs_seen=0,
        stale_days=90,
        apply=False,
    )
    rc = _cmd_skill_curate(args, _cfg(run, tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    for sid in strong:
        assert f"archive {sid}" in out, out
    # la chaîne concaténée ne doit PAS apparaître comme décision
    assert f"archive {concat}" not in out, out


def test_skill_curate_r4_strong_signal_enumerates(tmp_path, capsys):
    """curation_signal.R4_archive_candidates.strong_8of8 (liste propre) énumère les
    ids individuellement même sans finding archive."""
    from weekly_telemetry_aggregator.cli import _cmd_skill_curate

    date = "2026-08-28"
    run = tmp_path / "reports"
    run.mkdir()
    strong = ["a-skill", "b-skill", "c-skill"]
    (run / f"weekly-coherence-findings-{date}.json").write_text(
        json.dumps(
            {
                "findings": [
                    {"tag_action": "merge", "target_skill_id": "dup/skill", "reason": "redondant"}
                ],
                "curation_signal": {"R4_archive_candidates": {"strong_8of8": strong}},
            }
        ),
        encoding="utf-8",
    )
    (run / f"weekly-summary-{date}.json").write_text(
        json.dumps(
            {
                "skill_catalog": [
                    {
                        "skill_id": "dup/skill",
                        "metadata": {"origin": "weekly-background", "ttl_policy": None},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    args = _ns(
        anchor=date,
        coherence=None,
        catalog=None,
        usage=None,
        runs_seen=0,
        stale_days=90,
        apply=False,
    )
    rc = _cmd_skill_curate(args, _cfg(run, tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    for sid in strong:
        assert f"archive {sid}" in out, out


def test_curation_signal_strong_8of8_shape_is_list():
    """Contrat de forme : curation_signal.R4_archive_candidates.strong_8of8 est une
    LISTE d'ids (pas une chaîne concaténée)."""
    data = {"curation_signal": {"R4_archive_candidates": {"strong_8of8": ["a", "b", "c"]}}}
    strong = data["curation_signal"]["R4_archive_candidates"]["strong_8of8"]
    assert isinstance(strong, list)
    assert all(isinstance(s, str) and "," not in s for s in strong)


def test_skill_curate_one_final_decision_recalibrate_beats_r4_archive(tmp_path):
    """Un même skill reçoit une seule décision; recalibrate bat l'archive R4."""
    from weekly_telemetry_aggregator.cli import _cmd_skill_curate

    date = "2026-08-28"
    run = tmp_path / "reports"
    run.mkdir()
    (run / f"weekly-coherence-findings-{date}.json").write_text(
        json.dumps(
            {
                "findings": [
                    {
                        "tag_action": "recalibrate",
                        "target_skill_id": "caveman",
                        "reason": "description mismatch",
                    }
                ],
                "curation_signal": {"R4_archive_candidates": {"strong_8of8": ["caveman"]}},
            }
        ),
        encoding="utf-8",
    )
    (run / f"weekly-summary-{date}.json").write_text(
        json.dumps(
            {
                "skill_catalog": [
                    {
                        "skill_id": "caveman",
                        "metadata": {"origin": "weekly-background", "ttl_policy": None},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    args = _ns(
        anchor=date,
        coherence=None,
        catalog=None,
        usage=None,
        runs_seen=0,
        stale_days=90,
        apply=False,
    )
    assert _cmd_skill_curate(args, _cfg(run, tmp_path)) == 0

    report = json.loads((run / f"skill-curate-{date}.json").read_text(encoding="utf-8"))
    decisions = [d for d in report["decisions"] if d["skill_id"] == "caveman"]
    assert len(decisions) == 1
    assert decisions[0]["action"] == "recalibrate"
    assert report["move_status_counts"]["not_attempted"] == 1


def test_skill_curate_apply_is_project_local_and_reports_move_statuses(tmp_path):
    """Apply ne touche jamais une skill hors des racines projet et expose les statuts."""
    from weekly_telemetry_aggregator.cli import _cmd_skill_curate

    date = "2026-08-28"
    project = tmp_path / "project"
    local = project / ".opencode" / "skills" / "local-skill"
    local.mkdir(parents=True)
    (local / "SKILL.md").write_text("---\nname: local-skill\n---\n", encoding="utf-8")
    archive_target = project / ".opencode" / "skills" / "archive-target"
    archive_target.mkdir(parents=True)
    (archive_target / "SKILL.md").write_text(
        "---\nname: archive-target\ndescription: stale skill\nmetadata:\n  origin: weekly-background\n  verification: unit test\n  ttl_policy: null\n---\n## How\nbody\n",
        encoding="utf-8",
    )
    outside = tmp_path / "global-skills" / "outside-skill"
    outside.mkdir(parents=True)
    (outside / "SKILL.md").write_text("global", encoding="utf-8")
    run = tmp_path / "reports"
    run.mkdir()
    (run / f"weekly-coherence-findings-{date}.json").write_text(
        json.dumps(
            [
                {"tag_action": "archive", "target_skill_id": "local-skill"},
                {"tag_action": "archive", "target_skill_id": "archive-target"},
                {"tag_action": "archive", "target_skill_id": "outside-skill"},
                {"tag_action": "merge", "target_skill_id": "local-skill"},
            ]
        ),
        encoding="utf-8",
    )
    (run / f"weekly-summary-{date}.json").write_text(
        json.dumps({"skill_catalog": []}), encoding="utf-8"
    )

    args = _ns(
        anchor=date,
        coherence=None,
        catalog=None,
        usage=None,
        runs_seen=0,
        stale_days=90,
        apply=True,
    )
    assert _cmd_skill_curate(args, _cfg(run, project)) == 0

    report = json.loads((run / f"skill-curate-{date}.json").read_text(encoding="utf-8"))
    # A proposal on the same skill wins over the weaker archive signal, so no
    # move is attempted for ``local-skill``.
    assert local.exists()
    assert not archive_target.exists()
    assert outside.exists()
    assert report["move_status_counts"]["moved"] == 1
    assert report["move_status_counts"]["missing"] == 1
    assert report["proposed"] == 1
    assert report["decisions"]


def test_skill_curate_rejects_path_traversal_archive_target(tmp_path):
    """An archive target cannot escape the configured project skill roots."""
    from weekly_telemetry_aggregator.cli import _cmd_skill_curate

    date = "2026-08-28"
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "global-skills" / "outside-skill"
    outside.mkdir(parents=True)
    marker = outside / "SKILL.md"
    marker.write_text("global", encoding="utf-8")
    run = tmp_path / "reports"
    run.mkdir()
    (run / f"weekly-coherence-findings-{date}.json").write_text(
        json.dumps(
            [{"tag_action": "archive", "target_skill_id": "../../../global-skills/outside-skill"}]
        ),
        encoding="utf-8",
    )

    args = _ns(
        anchor=date,
        coherence=None,
        catalog=None,
        usage=None,
        runs_seen=0,
        stale_days=90,
        apply=True,
    )
    assert _cmd_skill_curate(args, _cfg(run, project)) == 0

    report = json.loads((run / f"skill-curate-{date}.json").read_text(encoding="utf-8"))
    assert marker.exists()
    assert report["move_status_counts"]["missing"] == 1


def test_skill_curate_rejects_root_archive_target(tmp_path):
    """A malformed root skill id cannot make apply move a whole skill root."""
    from weekly_telemetry_aggregator.cli import _cmd_skill_curate

    date = "2026-08-28"
    project = tmp_path / "project"
    skill_root = project / ".opencode" / "skills"
    skill_root.mkdir(parents=True)
    marker = skill_root / "keep" / "SKILL.md"
    marker.parent.mkdir()
    marker.write_text("keep", encoding="utf-8")
    run = tmp_path / "reports"
    run.mkdir()
    (run / f"weekly-coherence-findings-{date}.json").write_text(
        json.dumps([{"tag_action": "archive", "target_skill_id": "."}]),
        encoding="utf-8",
    )

    args = _ns(
        anchor=date,
        coherence=None,
        catalog=None,
        usage=None,
        runs_seen=0,
        stale_days=90,
        apply=True,
    )
    assert _cmd_skill_curate(args, _cfg(run, project)) == 0

    report = json.loads((run / f"skill-curate-{date}.json").read_text(encoding="utf-8"))
    assert marker.exists()
    assert report["move_status_counts"]["missing"] == 1


def test_duplicate_catalog_protection_is_order_independent():
    findings = [{"tag_action": "archive", "target_skill_id": "same"}]
    weak = {"skill_id": "same", "metadata": {"origin": "weekly-background", "ttl_policy": None}}
    user = {"skill_id": "same", "metadata": {"origin": "user", "ttl_policy": None}}
    pinned = {"skill_id": "same", "metadata": {"origin": "weekly-background", "ttl_policy": "pin"}}
    assert curation.decide_actions(findings, [weak, user])[0]["action"] == "skip"
    assert curation.decide_actions(findings, [user, weak])[0]["action"] == "skip"
    assert curation.decide_actions(findings, [weak, pinned])[0]["action"] == "pin"


def test_read_carry_uses_configured_memory_and_unique_run_markers(tmp_path):
    output_dir = tmp_path / "reports"
    output_dir.mkdir()
    memory = output_dir / "custom-watch.jsonl"
    memory.write_text(
        "\n".join(
            [
                json.dumps({"run_id": "r1", "skill_id": "a", "usage": {"load_count": 0}}),
                json.dumps({"run_id": "r1", "skill_id": "b", "usage": {"load_count": 0}}),
                json.dumps({"run_id": "r2", "skill_id": "a", "usage": {"load_count": 2}}),
                json.dumps(["not", "a", "mapping"]),
            ]
        ),
        encoding="utf-8",
    )
    carry = curation.read_carry(
        tmp_path / "engine", output_dir=output_dir, memory_file="custom-watch.jsonl"
    )
    assert carry["runs_seen"] == 2
    assert carry["usage"]["a"]["load_count"] == 2
    assert carry["usage"]["b"]["load_count"] == 0


def test_summary_catalog_entries_preserve_protection_metadata(tmp_path):
    from weekly_telemetry_aggregator.cli import _auto_load_catalog

    run = tmp_path / "reports"
    run.mkdir()
    date = "2026-08-28"
    (run / f"weekly-summary-{date}.json").write_text(
        json.dumps(
            {
                "skill_catalog_entries": [
                    {
                        "name": "user-skill",
                        "origin": "user",
                        "ttl_policy": "pin",
                        "usage": {"load_count": 0},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    catalog = _auto_load_catalog(run, _cfg(run, tmp_path), date)
    assert catalog[0]["metadata"] == {
        "origin": "user",
        "ttl_policy": "pin",
        "usage": {"load_count": 0},
    }


def test_archive_rejects_nested_leaf_fallback_and_unverified_source(tmp_path):
    from weekly_telemetry_aggregator.cli import _archive_skill

    root = tmp_path / ".opencode" / "skills"
    source = root / "nested" / "skill"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("---\nname: skill\n---\n", encoding="utf-8")
    status, _detail = _archive_skill(
        "nested/skill",
        [root],
        "2026-08-28",
        lambda src, dst: ("moved", str(dst)),
        catalog_entry={
            "skill_id": "nested/skill",
            "metadata": {"origin": "weekly-background", "ttl_policy": None},
        },
    )
    assert status == "unverified"
    assert source.is_dir()


def test_path_containment_fails_closed_on_resolution_oserror(monkeypatch, tmp_path):
    from weekly_telemetry_aggregator import cli

    def fail_resolve(self):
        raise OSError("resolution failed")

    monkeypatch.setattr(type(tmp_path), "resolve", fail_resolve)
    assert cli._path_is_global_or_unresolvable(tmp_path, (tmp_path,)) is True
