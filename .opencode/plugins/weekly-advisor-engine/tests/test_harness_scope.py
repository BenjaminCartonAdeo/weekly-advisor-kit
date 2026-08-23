"""Harness allowlist, projection accounting, and digest remapping tests."""

from __future__ import annotations

import json
from pathlib import Path

from weekly_telemetry_aggregator.config import (
    DEFAULT_HARNESS_EXCLUDE_PATTERNS,
    HarnessIncludeConfig,
)
from weekly_telemetry_aggregator.draft_targets import (
    DRAFT_TARGET_PRIORITY,
    HARNESS_CLAUDE_CODE,
    HARNESS_CODEX,
    HARNESS_COPILOT_VSCODE,
    HARNESS_OPENCODE,
    MODE_LEGACY,
    ResolvedDraftTarget,
)
from weekly_telemetry_aggregator.harness_scope import (
    copy_scope_to_projection,
    enrich_harness_digest,
    harness_extra_roots,
    inject_engine_content,
    resolve_harness_scope,
    resolve_remediation_surface,
)


def _touch(root: Path, relative: str, content: str = "x") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_resolve_scope_matches_strict_and_advisory_profiles(tmp_path: Path):
    _touch(tmp_path, ".opencode/AGENTS.md")
    _touch(tmp_path, ".opencode/agents/weekly/agent.md")
    _touch(tmp_path, ".opencode/commands/weekly.md")
    _touch(tmp_path, ".opencode/plugins/local.ts")
    _touch(tmp_path, ".opencode/plugins/nested/local.ts")
    _touch(tmp_path, ".opencode/opencode.json")
    _touch(tmp_path, ".opencode/context/python/standards.md")  # C3 : context/** inclus
    _touch(tmp_path, ".opencode/skills/demo/SKILL.md")
    _touch(tmp_path, ".opencode/skills/demo/references/guide.md")
    _touch(tmp_path, ".opencode/skills/demo/examples/example.txt")
    _touch(tmp_path, ".opencode/skills/demo/README.md")
    _touch(tmp_path, ".opencode/package.json")  # C3 : *.json racine inclus

    strict = resolve_harness_scope(tmp_path, HarnessIncludeConfig(default_profile="strict"))
    advisory = resolve_harness_scope(tmp_path, HarnessIncludeConfig(default_profile="advisory"))

    assert ".opencode/skills/demo/SKILL.md" not in strict.included_files
    assert ".opencode/plugins/local.ts" in strict.included_files
    assert ".opencode/plugins/nested/local.ts" not in strict.included_files
    assert ".opencode/context/python/standards.md" in strict.included_files
    assert ".opencode/package.json" in strict.included_files
    assert ".opencode/skills/demo/SKILL.md" in advisory.included_files
    assert ".opencode/skills/demo/references/guide.md" in advisory.included_files
    assert ".opencode/skills/demo/examples/example.txt" in advisory.included_files
    assert ".opencode/skills/demo/README.md" in advisory.unscoped_files
    assert ".opencode/package.json" in advisory.included_files
    assert any("unscoped" in warning for warning in advisory.warnings)


def test_scope_excludes_vendor_engine_and_generated_content(tmp_path: Path):
    _touch(tmp_path, ".opencode/plugins/local.ts")
    _touch(tmp_path, ".opencode/node_modules/pkg/index.js")
    _touch(tmp_path, ".opencode/plugins/weekly-advisor-engine/source.py")
    _touch(tmp_path, ".opencode/.pytest_cache/state")
    _touch(tmp_path, ".opencode/.cache/state")
    _touch(tmp_path, ".venv/lib/site.py")
    _touch(tmp_path, "nested/__pycache__/module.pyc")
    _touch(tmp_path, ".git/index")
    _touch(tmp_path, "dist/bundle.js")
    _touch(tmp_path, "build/output.js")
    _touch(tmp_path, "coverage/index.html")

    scope = resolve_harness_scope(tmp_path, HarnessIncludeConfig())
    included = set(scope.included_files)

    assert included == {".opencode/plugins/local.ts"}
    assert scope.excluded_file_count == 4
    for pattern in DEFAULT_HARNESS_EXCLUDE_PATTERNS:
        assert pattern in scope.excluded_counts_by_pattern
    assert scope.excluded_counts_by_pattern[".opencode/node_modules/**"] == 1
    assert scope.excluded_counts_by_pattern[".opencode/plugins/weekly-advisor-engine/**"] == 1
    assert scope.excluded_counts_by_pattern["**/.cache/**"] == 1
    assert scope.excluded_counts_by_pattern["**/.venv/**"] == 0
    assert scope.excluded_counts_by_pattern["**/.git/**"] == 0
    assert scope.excluded_counts_by_pattern["**/dist/**"] == 0
    assert scope.excluded_counts_by_pattern["**/build/**"] == 0
    assert scope.excluded_counts_by_pattern["**/coverage/**"] == 0


def test_scope_does_not_walk_application_repository_outside_opencode(tmp_path: Path):
    """Strict profile: only files under .opencode/ are considered (v6.0.o: rglob instead of os.walk)."""
    _touch(tmp_path, ".opencode/commands/review.md")
    _touch(tmp_path, "src/application.py")
    scope = resolve_harness_scope(tmp_path, HarnessIncludeConfig(default_profile="strict"))

    assert "src/application.py" not in scope.unscoped_files
    assert scope.included_files == [".opencode/commands/review.md"]


def test_mandatory_excludes_cannot_be_removed_by_broad_custom_profile(tmp_path: Path):
    _touch(tmp_path, ".opencode/local.md")
    _touch(tmp_path, ".opencode/node_modules/vendor.js")
    _touch(tmp_path, ".opencode/plugins/weekly-advisor-engine/source.py")

    config = HarnessIncludeConfig(
        default_profile="broad",
        profiles={"broad": [".opencode/**/*"]},
        exclude_patterns=[],
    )
    scope = resolve_harness_scope(tmp_path, config)

    assert ".opencode/local.md" in scope.included_files
    assert ".opencode/node_modules/vendor.js" not in scope.included_files
    assert ".opencode/plugins/weekly-advisor-engine/source.py" not in scope.included_files


def test_projection_preserves_relative_paths_and_digest_remap(tmp_path: Path):
    source = _touch(tmp_path, ".opencode/commands/review.md", "review")
    scope = resolve_harness_scope(tmp_path, HarnessIncludeConfig(default_profile="strict"))
    projection = tmp_path / "projection"
    projection.mkdir()

    copy_scope_to_projection(tmp_path, scope, projection)
    assert (projection / ".opencode/commands/review.md").read_text(encoding="utf-8") == "review"
    assert source.exists()

    digest = {
        "metadata": {"components_scanned": 1},
        "inspection": {
            "uncategorized": [
                {
                    "findings": [{"rule": "example/rule", "severity": "warning", "message": "m"}],
                }
            ]
        },
        "uncategorized_files": [str(projection / ".opencode/commands/review.md")],
    }
    enriched = enrich_harness_digest(digest, scope, projection)

    assert enriched["inspection"]["uncategorized"][0]["path"] == ".opencode/commands/review.md"
    assert enriched["harness_include"]["included_file_count"] == 1
    assert enriched["harness_counts"] == {
        "files_scanned": 1,
        "components_scanned": 1,
        "findings_raw": 1,
        "findings_unique": 1,
    }
    assert enriched["normalized"]["files_scanned"] == 1
    json.dumps(enriched)


# ---- cellule 2.2 : projection multi-répertoires ------------------------------


def test_resolve_scope_includes_detected_harness_dirs(tmp_path: Path):
    """Les répertoires du harnais détecté entrent dans le scope allowlisté."""
    _touch(tmp_path, ".opencode/commands/review.md")
    _touch(tmp_path, ".claude/skills/demo/SKILL.md")
    _touch(tmp_path, ".claude/skills/demo/references/guide.md")
    _touch(tmp_path, ".claude/node_modules/vendor.js")

    scope = resolve_harness_scope(
        tmp_path,
        HarnessIncludeConfig(default_profile="strict"),
        extra_roots=(".claude/skills",),
    )

    assert ".claude/skills/demo/SKILL.md" in scope.included_files
    assert ".claude/skills/demo/references/guide.md" in scope.included_files
    assert ".claude/node_modules/vendor.js" not in scope.included_files
    assert ".opencode/commands/review.md" in scope.included_files
    assert scope.extra_roots == [".claude/skills"]
    assert scope.to_dict()["extra_root_file_count"] == 2


def test_resolve_scope_extra_root_missing_warns(tmp_path: Path):
    _touch(tmp_path, ".opencode/commands/review.md")

    scope = resolve_harness_scope(
        tmp_path,
        HarnessIncludeConfig(default_profile="strict"),
        extra_roots=(".github/prompts",),
    )

    assert any(".github/prompts" in warning for warning in scope.warnings)


def test_resolve_scope_rejects_unsafe_extra_roots(tmp_path: Path):
    _touch(tmp_path, ".opencode/commands/review.md")

    scope = resolve_harness_scope(
        tmp_path,
        HarnessIncludeConfig(default_profile="strict"),
        extra_roots=("../escape", "/absolute", ""),
    )

    assert scope.extra_roots == []
    assert any("rejet" in warning for warning in scope.warnings)
    assert scope.included_files == [".opencode/commands/review.md"]


def test_projection_copies_extra_roots_with_project_relative_paths(tmp_path: Path):
    _touch(tmp_path, ".agents/skills/demo/SKILL.md", "demo")
    scope = resolve_harness_scope(
        tmp_path,
        HarnessIncludeConfig(default_profile="strict"),
        extra_roots=(".agents",),
    )
    projection = tmp_path / "projection"
    projection.mkdir()

    copy_scope_to_projection(tmp_path, scope, projection)

    assert (projection / ".agents/skills/demo/SKILL.md").read_text(encoding="utf-8") == "demo"


def test_harness_extra_roots_mapping():
    """Un harnais → ses répertoires additionnels ; legacy = toutes cibles."""
    assert harness_extra_roots(ResolvedDraftTarget("detected", (HARNESS_OPENCODE,))) == ()
    assert harness_extra_roots(ResolvedDraftTarget("detected", (HARNESS_CLAUDE_CODE,))) == (
        ".claude/skills",
    )
    assert harness_extra_roots(
        ResolvedDraftTarget("detected", (HARNESS_COPILOT_VSCODE,))
    ) == (".github/prompts", ".github/skills")
    assert harness_extra_roots(ResolvedDraftTarget("detected", (HARNESS_CODEX,))) == (".agents",)
    assert harness_extra_roots(
        ResolvedDraftTarget(MODE_LEGACY, DRAFT_TARGET_PRIORITY)
    ) == (
        ".agents",
        ".claude/skills",
        ".github/prompts",
        ".github/skills",
    )


# ---- cellule 2.2 : injection du contenu engine + orphelins --------------------


def _make_kit(root: Path) -> Path:
    _touch(root, ".opencode/skills/kit-skill/SKILL.md", "kit skill")
    _touch(root, ".opencode/skills/kit-skill/references/deep.md", "deep")
    _touch(root, ".opencode/commands/kit-cmd.md", "kit cmd")
    return root


def test_inject_engine_content_creates_orphans(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    kit = _make_kit(tmp_path / "kit")
    projection = tmp_path / "projection"
    projection.mkdir()

    orphans = inject_engine_content(
        project, (".claude/skills",), projection, kit_root=kit
    )

    assert ".claude/skills/kit-skill/SKILL.md" in orphans
    assert ".claude/skills/kit-skill/references/deep.md" in orphans
    assert ".claude/commands/kit-cmd.md" in orphans
    assert (projection / ".claude/skills/kit-skill/SKILL.md").is_file()
    for path in projection.rglob("*"):
        assert not path.is_symlink()


def test_inject_engine_content_real_project_file_wins(tmp_path: Path):
    project = tmp_path / "project"
    _touch(project, ".claude/skills/kit-skill/SKILL.md", "real content")
    kit = _make_kit(tmp_path / "kit")
    projection = tmp_path / "projection"
    projection.mkdir()
    copy_scope_to_projection(
        project,
        resolve_harness_scope(project, HarnessIncludeConfig(), extra_roots=(".claude/skills",)),
        projection,
    )

    orphans = inject_engine_content(project, (".claude/skills",), projection, kit_root=kit)

    assert (projection / ".claude/skills/kit-skill/SKILL.md").read_text(
        encoding="utf-8"
    ) == "real content"
    assert ".claude/skills/kit-skill/SKILL.md" not in orphans
    # les fichiers non collisionnés restent injectés
    assert ".claude/skills/kit-skill/references/deep.md" in orphans


def test_inject_engine_content_skips_symlink_sources(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    kit = _make_kit(tmp_path / "kit")
    link = kit / ".opencode/skills/linked"
    link.symlink_to(kit / ".opencode/skills/kit-skill")
    projection = tmp_path / "projection"
    projection.mkdir()

    orphans = inject_engine_content(project, (".claude/skills",), projection, kit_root=kit)

    assert not (projection / ".claude/skills/linked").exists()
    assert all("linked" not in orphan for orphan in orphans)


def test_inject_engine_content_without_kit_is_noop(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    projection = tmp_path / "projection"
    projection.mkdir()

    assert (
        inject_engine_content(project, (".claude/skills",), projection, kit_root=None) == []
    )
    assert not (projection / ".claude").exists()


def test_inject_engine_content_commands_target_mapping(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    kit = _make_kit(tmp_path / "kit")
    projection_opencode = tmp_path / "p-opencode"
    projection_prompts = tmp_path / "p-prompts"
    projection_agents = tmp_path / "p-agents"
    for directory in (projection_opencode, projection_prompts, projection_agents):
        directory.mkdir()

    opencode_orphans = inject_engine_content(
        project, (".opencode/skills",), projection_opencode, kit_root=kit
    )
    prompts_orphans = inject_engine_content(
        project, (".github/prompts",), projection_prompts, kit_root=kit
    )
    agents_orphans = inject_engine_content(
        project, (".agents",), projection_agents, kit_root=kit
    )

    assert ".opencode/commands/kit-cmd.md" in opencode_orphans
    assert ".github/prompts/kit-cmd.md" in prompts_orphans
    assert ".agents/commands/kit-cmd.md" in agents_orphans


# ---- cellule 2.2 : matrice de décision 5.5 ------------------------------------


def test_resolve_remediation_surface_matrix_exhaustive():
    known = (HARNESS_CLAUDE_CODE, HARNESS_OPENCODE, HARNESS_COPILOT_VSCODE, HARNESS_CODEX)
    decisions: set[str] = set()
    for harness in known:
        surface = resolve_remediation_surface((harness,), "detected")
        decisions.add(surface.decision)
        assert surface.reason
        assert surface.mode == "detected"
        if harness == HARNESS_OPENCODE:
            assert surface.decision == "projection"
            assert "native" in surface.reason
        else:
            assert surface.decision == "portability"
            assert harness in surface.reason

    combined = resolve_remediation_surface(
        (HARNESS_OPENCODE, HARNESS_CLAUDE_CODE), MODE_LEGACY
    )
    assert combined.decision == "combined"
    assert "portability.yaml" in combined.reason

    foreign_multi = resolve_remediation_surface(
        (HARNESS_CLAUDE_CODE, HARNESS_CODEX), "override"
    )
    assert foreign_multi.decision == "portability"

    empty = resolve_remediation_surface((), "default")
    assert empty.decision == "projection"

    unknown = resolve_remediation_surface("cursor-windsurf", "override")
    assert unknown.decision == "projection"
    assert "inconnu" in unknown.reason

    assert decisions == {"projection", "portability"}
