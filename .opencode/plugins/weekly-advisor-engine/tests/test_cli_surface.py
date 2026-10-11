"""Contract fence for the cli.py split.

`cli.py` must stay a facade: every handler may live in a sub-module, but every
handler must still be an attribute of the `cli` module under its own name.

Two invariants this file pins:

  1. Every handler named in `cli._SUBCOMMANDS` is an attribute of `cli` under
     `func.__name__`, and it is the *same object*.
  2. Dispatch resolves that name when `build_parser()` runs -- i.e. late with
     respect to IMPORT -- so `monkeypatch.setattr(cli, "<name>", ...)` applied
     after the import still bites.

Invariant 2 is bound to `build_parser`, not to `parse_args`: `build_parser`
snapshots `globals()[func.__name__]` into the parser's defaults. A patch applied
*after* `build_parser()` is therefore invisible. That boundary is asserted on
purpose (see the last test) so nobody reads the green dispatch test as
"the lookup happens at parse time".

If this file goes red, the split broke dispatch. Do not "fix" it by weakening
an assertion.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from weekly_telemetry_aggregator import cli

# --------------------------------------------------------------------------
# Step 1: the patch surface, enumerated from tests/ (2026-10, HEAD 2540555).
#
# MONKEYPATCH_TARGETS -- names tests rebind ON the cli module with
# `monkeypatch.setattr(cli, name, ...)` or the string form. The patch writes
# into `cli.__dict__`, so the name must be a real module attribute AND its
# caller must resolve it through cli's namespace, not through a captured local.
#
# REEXPORTED_NAMES -- names tests bind with `from weekly_telemetry_aggregator.cli
# import name`. The import happens at test-module import time, so the split may
# move the definition but `cli` must keep re-exporting the same name.
# --------------------------------------------------------------------------
MONKEYPATCH_TARGETS = (
    "run",  # tests/test_cli.py:136,144,153 -- patched, then _cmd_run called
    "_cmd_doctor",  # tests/test_cli.py:386,400,414 -- patched, then main() called
)

REEXPORTED_NAMES = (
    # tests/test_cli.py:11
    "_cmd_run",
    "_deduplicate_worker_statuses",
    "build_parser",
    "main",
    # tests/test_blocks_check.py:22
    "_cmd_report_blocks_check",
    # tests/test_curation.py:199..803
    "_cmd_skill_curate",
    "_auto_load_catalog",
    "_archive_skill",
    # tests/test_main.py:2533..2570, tests/test_skill_curate_integration.py:96
    "_skill_dirs_for",
)


def test_every_subcommand_handler_is_an_attribute_of_cli():
    """Invariant 1 -- the table names only handlers `cli` can resolve by name."""
    for _name, _help, func, _args in cli._SUBCOMMANDS:
        attr = func.__name__
        assert attr in vars(cli), f"{attr} is not an attribute of cli"
        assert vars(cli)[attr] is func, f"cli.{attr} is not the object in _SUBCOMMANDS"


def test_named_cli_surface_stays_reachable():
    """The enumerated surface from Step 1 survives the split."""
    for name in MONKEYPATCH_TARGETS + REEXPORTED_NAMES:
        assert name in vars(cli), f"cli.{name} disappeared"


def test_dispatch_resolves_handler_when_the_parser_is_built(monkeypatch):
    """Invariant 2 -- the `globals()[func.__name__]` lookup runs inside
    `build_parser`, so a post-import patch is picked up by the built parser."""
    sentinel = object()
    monkeypatch.setattr(cli, "_cmd_doctor", sentinel)

    parser = cli.build_parser()
    args = parser.parse_args(["doctor"])

    assert args.func is sentinel


def test_default_run_binding_reads_cli_namespace_when_built(monkeypatch):
    """The bare-invocation default (`command="run"`) also reads `cli`'s
    namespace at build time, not a value frozen at import."""
    sentinel = object()
    monkeypatch.setattr(cli, "_cmd_run", sentinel)

    args = cli.build_parser().parse_args([])

    assert args.command == "run"
    assert args.func is sentinel


def test_lookup_is_not_late_relative_to_parse_args(monkeypatch):
    """Boundary, asserted on purpose: `build_parser` snapshots the handler into
    the parser defaults, so a patch applied after the build is NOT seen. This
    is the real semantics of the "Résolution tardive" comment -- late with
    respect to import, not with respect to parse_args."""
    original = cli._cmd_doctor
    parser = cli.build_parser()

    sentinel = object()
    monkeypatch.setattr(cli, "_cmd_doctor", sentinel)
    args = parser.parse_args(["doctor"])

    assert args.func is original
    assert args.func is not sentinel


# ==========================================================================
# Task 8 (decompose the seven C-or-worse handlers) -- Step 0 characterisation.
#
# The two thinnest-covered bodies are rewritten by Task 8. Before any body is
# edited, their observable behaviour is pinned here: the refactor must be
# behaviour-preserving, so every assertion below stays green UNCHANGED.
#
# They live in this file because it is the only test file reserved for this
# cell; the names are reached through `cli` on purpose, which also re-asserts
# the facade re-export contract while the bodies move.
# ==========================================================================


# --- _load_previous_carried_over ------------------------------------------


def _write_candidates(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_load_previous_carried_over_picks_latest_strictly_before(tmp_path):
    from weekly_telemetry_aggregator.cli import _load_previous_carried_over

    _write_candidates(
        tmp_path / "weekly-audit-candidates-2026-08-01.json",
        {"carried_over": [{"session_id": "a"}]},
    )
    _write_candidates(
        tmp_path / "runs" / "x" / "weekly-audit-candidates-2026-08-05.json",
        {"unaudited": [{"session_id": "b"}]},
    )
    _write_candidates(
        tmp_path / "runs" / "x" / "legacy" / "weekly-audit-candidates-2026-08-03.json",
        {"carried_over": [{"session_id": "c"}]},
    )
    # Same-date file must NOT be selected by the strict `< current_date` filter.
    _write_candidates(
        tmp_path / "weekly-audit-candidates-2026-08-10.json",
        {"carried_over": [{"session_id": "today"}]},
    )

    assert _load_previous_carried_over(tmp_path, tmp_path / "other", "2026-08-10") == [
        {"session_id": "b"}
    ]


def test_load_previous_carried_over_same_date_fallback_excludes_current_dir(tmp_path):
    from weekly_telemetry_aggregator.cli import _load_previous_carried_over

    _write_candidates(
        tmp_path / "weekly-audit-candidates-2026-08-10.json",
        {"carried_over": [{"session_id": "x"}]},
    )

    assert _load_previous_carried_over(tmp_path, tmp_path / "other", "2026-08-10") == [
        {"session_id": "x"}
    ]
    # The only candidate lives in the excluded dir -> nothing to carry.
    assert _load_previous_carried_over(tmp_path, tmp_path, "2026-08-10") == []


def test_load_previous_carried_over_returns_empty_when_absent(tmp_path):
    from weekly_telemetry_aggregator.cli import _load_previous_carried_over

    assert _load_previous_carried_over(tmp_path, tmp_path / "other", "2026-08-10") == []


def test_load_previous_carried_over_swallows_unreadable_json(tmp_path):
    from weekly_telemetry_aggregator.cli import _load_previous_carried_over

    (tmp_path / "weekly-audit-candidates-2026-08-01.json").write_text("{not json", encoding="utf-8")

    assert _load_previous_carried_over(tmp_path, tmp_path / "other", "2026-08-10") == []


def test_load_previous_carried_over_falls_back_to_unaudited_and_filters(tmp_path):
    from weekly_telemetry_aggregator.cli import _load_previous_carried_over

    _write_candidates(
        tmp_path / "weekly-audit-candidates-2026-08-01.json",
        {"carried_over": [{"session_id": "ok"}, {"no": "sid"}, "string"]},
    )
    assert _load_previous_carried_over(tmp_path, tmp_path / "other", "2026-08-10") == [
        {"session_id": "ok"}
    ]

    _write_candidates(
        tmp_path / "weekly-audit-candidates-2026-08-02.json",
        {"unaudited": [{"session_id": "u"}]},
    )
    # 2026-08-02 is more recent than 2026-08-01 -> the `unaudited` chain wins.
    assert _load_previous_carried_over(tmp_path, tmp_path / "other", "2026-08-10") == [
        {"session_id": "u"}
    ]


# --- _archive_skill --------------------------------------------------------

_VALID_SKILL = (
    "---\nname: {name}\ndescription: stale skill\nmetadata:\n"
    "  origin: weekly-background\n  verification: unit test\n  ttl_policy: null\n---\n## How\nbody\n"
)
_COMPLETE_CATALOG = {
    "skill_id": "old-skill",
    "metadata": {"origin": "weekly-background", "ttl_policy": None},
}


def _seed_skill(root: Path, name: str = "old-skill") -> Path:
    skill = root / name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(_VALID_SKILL.format(name=name), encoding="utf-8")
    return skill


def _real_mover(src: Path, dst: Path) -> tuple[str, str]:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return "moved", str(dst)


def test_archive_skill_moves_tree_and_reports_moved(tmp_path):
    from weekly_telemetry_aggregator.cli import _archive_skill

    project = tmp_path / "project"
    root = project / ".opencode" / "skills"
    skill = _seed_skill(root)
    body = (skill / "SKILL.md").read_text(encoding="utf-8")

    status, detail = _archive_skill(
        "old-skill", [root], "2026-08-28", _real_mover, catalog_entry=_COMPLETE_CATALOG
    )

    archived = root / "_archive" / "2026-08-28" / "old-skill"
    assert status == "moved"
    assert detail == str(archived)
    assert not skill.exists()
    assert (archived / "SKILL.md").read_text(encoding="utf-8") == body


def test_archive_skill_is_idempotent_when_destination_present(tmp_path):
    from weekly_telemetry_aggregator.cli import _archive_skill

    project = tmp_path / "project"
    root = project / ".opencode" / "skills"
    archived = root / "_archive" / "2026-08-28" / "old-skill"
    archived.mkdir(parents=True)
    (archived / "SKILL.md").write_text("already archived", encoding="utf-8")

    status, detail = _archive_skill(
        "old-skill", [root], "2026-08-28", _real_mover, catalog_entry=_COMPLETE_CATALOG
    )

    assert status == "exists"
    assert detail == f"{archived} déjà présent (idempotent)"


def test_archive_skill_missing_source_is_non_fatal(tmp_path):
    from weekly_telemetry_aggregator.cli import _archive_skill

    project = tmp_path / "project"
    root = project / ".opencode" / "skills"
    root.mkdir(parents=True)

    assert _archive_skill("ghost", [root], "2026-08-28", _real_mover) == ("missing", None)


def test_archive_skill_protects_user_origin_catalogue(tmp_path):
    from weekly_telemetry_aggregator.cli import _archive_skill

    project = tmp_path / "project"
    root = project / ".opencode" / "skills"
    skill = _seed_skill(root)

    status, detail = _archive_skill(
        "old-skill",
        [root],
        "2026-08-28",
        _real_mover,
        catalog_entry={"skill_id": "old-skill", "metadata": {"origin": "user", "ttl_policy": None}},
    )

    assert (status, detail) == (
        "protected",
        "catalogue protège cette skill (origin=user ou ttl_policy=pin)",
    )
    assert skill.is_dir()


def test_archive_skill_incomplete_catalogue_refuses_apply(tmp_path):
    from weekly_telemetry_aggregator.cli import _archive_skill

    project = tmp_path / "project"
    root = project / ".opencode" / "skills"
    skill = _seed_skill(root)

    assert _archive_skill("old-skill", [root], "2026-08-28", _real_mover) == (
        "unverified",
        "catalogue incomplet ou absent; archive apply refusé",
    )
    assert skill.is_dir()


def test_archive_skill_rejects_unsafe_id_and_date(tmp_path):
    from weekly_telemetry_aggregator.cli import _archive_skill

    project = tmp_path / "project"
    root = project / ".opencode" / "skills"
    root.mkdir(parents=True)

    assert _archive_skill("..", [root], "2026-08-28", _real_mover) == (
        "missing",
        "skill_id absent or unsafe",
    )
    assert _archive_skill("ok", [root], "2026-8-28", _real_mover) == (
        "missing",
        "date archive absente ou unsafe",
    )


def test_archive_skill_ambiguous_across_two_roots(tmp_path):
    from weekly_telemetry_aggregator.cli import _archive_skill

    project = tmp_path / "project"
    root_a = project / ".opencode" / "skills"
    root_b = project / ".claude" / "skills"
    _seed_skill(root_a)
    _seed_skill(root_b)

    status, detail = _archive_skill(
        "old-skill", [root_a, root_b], "2026-08-28", _real_mover, catalog_entry=_COMPLETE_CATALOG
    )

    assert (status, detail) == ("ambiguous", "skill_id existe dans plusieurs racines projet")
    assert (root_a / "old-skill").is_dir()
    assert (root_b / "old-skill").is_dir()
