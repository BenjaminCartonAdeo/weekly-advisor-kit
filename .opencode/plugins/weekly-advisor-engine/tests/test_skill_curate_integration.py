"""Integration contract for skill-curate manifests and report consumers."""

from __future__ import annotations

import json
from argparse import Namespace
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from weekly_telemetry_aggregator.config import TelemetryConfig
from weekly_telemetry_aggregator.report import report_prep

DATE = "2026-08-28"


def _cfg(output_dir: Path, project_root: Path) -> TelemetryConfig:
    cfg = TelemetryConfig(output_dir=output_dir, project_root=project_root)
    cfg.opencode_db_path = "/nonexistent/opencode.db"
    cfg.open_browser = False
    return cfg


def _args(*, apply: bool = False) -> Namespace:
    return Namespace(
        anchor=DATE,
        coherence=None,
        catalog=None,
        usage=[],
        runs_seen=0,
        stale_days=90,
        apply=apply,
    )


def _inputs(run: Path) -> None:
    (run / f"weekly-coherence-findings-{DATE}.json").write_text(
        json.dumps(
            {
                "findings": [
                    {
                        "tag_action": "archive",
                        "target_skill_id": "kit-stale",
                        "recommendation": "R4 stale",
                    },
                    {
                        "tag_action": "merge",
                        "target_skill_id": "adeo-duplicate",
                        "recommendation": "redondant",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    (run / f"weekly-summary-{DATE}.json").write_text(
        json.dumps(
            {
                "period": {},
                "skill_catalog": [
                    {
                        "skill_id": "kit-stale",
                        "metadata": {"origin": "weekly-background", "ttl_policy": None},
                    },
                    {
                        "skill_id": "adeo-duplicate",
                        "metadata": {"origin": "weekly-background", "ttl_policy": None},
                    },
                ],
            }
        ),
        encoding="utf-8",
    )


def test_kit_adeo_contract_uses_project_skill_roots_only(tmp_path: Path, monkeypatch):
    """Kit/Adeo end-to-end apply roots follow the RESOLVED harness, not a hardcoded list.

    This is the kit-level contract test, on purpose, and not a unit test of
    ``_skill_dirs_for``: the former three-root list was a hardcoded snapshot that
    drifted from the single source of truth (``main.resolve_skill_surface`` fed by
    ``resolve_draft_targets``). Unit coverage of ``_skill_dirs_for`` itself —
    default, override, legacy ordering, and the A4 global-root guard — already
    lives in ``tests/test_main.py`` (section "skill-curate : racines apply
    alignées sur A5").

    What is locked here is the deployable kit behaviour:

    * default / auto mode ⇒ ``[.opencode/skills]`` alone. A harness that is not
      resolved holds no catalogued skill, so there is nothing to move there;
    * an explicit ``claude-code`` override ⇒ ``[.claude/skills]``, proving the
      roots are derived from the resolved target rather than fixed;
    * invariant A4 end-to-end: no user-level/global root ever shows up in the
      write roots, not even when it exists on disk.
    """
    from weekly_telemetry_aggregator.cli import _skill_dirs_for

    project = tmp_path / "adeo"
    (project / ".opencode" / "skills").mkdir(parents=True)
    (project / ".claude" / "skills").mkdir(parents=True)
    home = tmp_path / "home"
    (home / ".config" / "opencode" / "skills").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))

    cfg = _cfg(tmp_path / "reports", project)

    # 1. Default (auto) target: the opencode root only.
    assert _skill_dirs_for(cfg) == [project.resolve() / ".opencode" / "skills"]

    # 2. An explicit claude-code override moves the apply universe.
    claude_cfg = replace(
        cfg, draft_targets=replace(cfg.draft_targets, mode="override", targets=["claude-code"])
    )
    assert _skill_dirs_for(claude_cfg) == [project.resolve() / ".claude" / "skills"]

    # 3. A4 end-to-end: every write root is project-local, never global.
    for root in (_skill_dirs_for(cfg), _skill_dirs_for(claude_cfg)):
        assert root and all(project.resolve() in path.parents for path in root)
        assert not any(home.resolve() in path.parents for path in root)
        assert home.resolve() / ".config" / "opencode" / "skills" not in root


def test_skill_curate_dry_run_manifest_schema_and_no_move(tmp_path: Path):
    """Default mode records decisions while preserving every project file."""
    from weekly_telemetry_aggregator.cli import _cmd_skill_curate

    project = tmp_path / "adeo"
    skill = project / ".opencode" / "skills" / "kit-stale"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: kit-stale\n---\nbody\n", encoding="utf-8")
    run = tmp_path / "reports"
    run.mkdir()
    _inputs(run)
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))

    assert _cmd_skill_curate(_args(), _cfg(run, project)) == 0

    after = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))
    assert set(after) - set(before) == {Path("reports") / f"skill-curate-{DATE}.json"}
    manifest = json.loads((run / f"skill-curate-{DATE}.json").read_text(encoding="utf-8"))
    assert {
        "schema_version",
        "mode",
        "dry_run",
        "date",
        "run_dir",
        "coherence_count",
        "catalog_count",
        "applied",
        "proposed",
        "skipped",
        "archive_pending",
        "decisions",
        "move_status_counts",
        "move_counts",
        "archive_candidates_ttl",
        "archive_candidates",
        "skipped_user",
    } <= manifest.keys()
    assert manifest["schema_version"] == 2
    assert manifest["mode"] == "dry-run"
    assert manifest["dry_run"] is True
    assert manifest["applied"] == 0
    assert manifest["archive_pending"] == 1
    assert manifest["move_status_counts"]["not_attempted"] == 2
    assert skill.is_dir()
    assert not (project / ".opencode" / "skills" / "_archive").exists()


def test_skill_curate_manifest_v2_adds_metadata_summary_and_skip_details(tmp_path: Path):
    """Manifest v2 is additive and explains every skipped decision."""
    from weekly_telemetry_aggregator.cli import _cmd_skill_curate

    project = tmp_path / "adeo"
    skill = project / ".opencode" / "skills" / "kit-stale"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: kit-stale\n---\nbody\n", encoding="utf-8")
    run = tmp_path / "reports"
    run.mkdir()
    _inputs(run)

    assert _cmd_skill_curate(_args(), _cfg(run, project)) == 0

    manifest = json.loads((run / f"skill-curate-{DATE}.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 2
    assert manifest["anchor"] == DATE
    assert manifest["generated_at"].startswith(f"{DATE}T")
    assert manifest["summary"]["by_action"] == {"archive": 1, "merge": 1}
    assert manifest["skipped_details"] == []
    # v1 flat fields remain available.
    assert manifest["archive_pending"] == 1
    assert manifest["decisions"]


def test_skill_curate_manifest_v2_lists_skip_details(tmp_path: Path):
    """Protected decisions appear in explicit skipped details."""
    from weekly_telemetry_aggregator.cli import _cmd_skill_curate

    run = tmp_path / "reports"
    run.mkdir()
    (run / f"weekly-coherence-findings-{DATE}.json").write_text(
        json.dumps([{"tag_action": "delete", "target_skill_id": "user-skill"}]),
        encoding="utf-8",
    )
    (run / f"weekly-summary-{DATE}.json").write_text(
        json.dumps({"skill_catalog": [{"skill_id": "user-skill", "metadata": {"origin": "user"}}]}),
        encoding="utf-8",
    )

    assert _cmd_skill_curate(_args(), _cfg(run, tmp_path)) == 0
    manifest = json.loads((run / f"skill-curate-{DATE}.json").read_text(encoding="utf-8"))
    assert manifest["skipped_details"] == [
        {
            "skill_id": "user-skill",
            "target_skill_id": "user-skill",
            "action": "skip",
            "reason": "user-origin protected",
            "source": "coherence",
            "status": "skipped",
            "move_status": "not_attempted",
        }
    ]


def test_report_prep_consumes_skill_curate_manifest(tmp_path: Path):
    """Report context and deterministic draft consume the generated manifest."""
    from weekly_telemetry_aggregator.aggregator import aggregate
    from weekly_telemetry_aggregator.models import Period
    from weekly_telemetry_aggregator.writer import summary_to_dict

    run = tmp_path / "reports"
    run.mkdir()
    _inputs(run)
    (run / f"skill-curate-{DATE}.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "mode": "dry-run",
                "dry_run": True,
                "date": DATE,
                "applied": 0,
                "proposed": 1,
                "skipped": 0,
                "decisions": [
                    {
                        "skill_id": "adeo-duplicate",
                        "action": "merge",
                        "reason": "redondant",
                        "source": "coherence",
                        "status": "proposed",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    period = Period(
        start=datetime(2026, 8, 21, tzinfo=UTC),
        end=datetime(2026, 8, 28, tzinfo=UTC),
    )
    (run / f"weekly-summary-{DATE}.json").write_text(
        json.dumps(
            summary_to_dict(
                aggregate([], period=period, generated_at=datetime(2026, 8, 28, tzinfo=UTC))
            )
        ),
        encoding="utf-8",
    )

    draft, context = report_prep(_cfg(run, tmp_path), anchor=DATE)

    assert draft is not None and context is not None
    assert context["skill_curate"]["proposed"] == 1
    text = draft.read_text(encoding="utf-8")
    assert "Curation (WAVE 2.5" in text
    assert "adeo-duplicate" in text
    assert "propos" in text
