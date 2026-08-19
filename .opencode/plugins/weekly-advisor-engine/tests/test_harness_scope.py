"""Harness allowlist, projection accounting, and digest remapping tests."""

from __future__ import annotations

import json
from pathlib import Path

from weekly_telemetry_aggregator.config import (
    DEFAULT_HARNESS_EXCLUDE_PATTERNS,
    HarnessIncludeConfig,
)
from weekly_telemetry_aggregator.harness_scope import (
    copy_scope_to_projection,
    enrich_harness_digest,
    resolve_harness_scope,
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
    _touch(tmp_path, ".opencode/skills/demo/SKILL.md")
    _touch(tmp_path, ".opencode/skills/demo/references/guide.md")
    _touch(tmp_path, ".opencode/skills/demo/examples/example.txt")
    _touch(tmp_path, ".opencode/skills/demo/README.md")
    _touch(tmp_path, ".opencode/package.json")

    strict = resolve_harness_scope(tmp_path, HarnessIncludeConfig(default_profile="strict"))
    advisory = resolve_harness_scope(tmp_path, HarnessIncludeConfig(default_profile="advisory"))

    assert ".opencode/skills/demo/SKILL.md" not in strict.included_files
    assert ".opencode/plugins/local.ts" in strict.included_files
    assert ".opencode/plugins/nested/local.ts" not in strict.included_files
    assert ".opencode/skills/demo/SKILL.md" in advisory.included_files
    assert ".opencode/skills/demo/references/guide.md" in advisory.included_files
    assert ".opencode/skills/demo/examples/example.txt" in advisory.included_files
    assert ".opencode/skills/demo/README.md" in advisory.unscoped_files
    assert ".opencode/package.json" in advisory.unscoped_files
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
