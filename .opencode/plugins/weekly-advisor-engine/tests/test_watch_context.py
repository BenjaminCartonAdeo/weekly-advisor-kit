"""Deterministic worktree inventory and ecosystem context tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from weekly_telemetry_aggregator.cli import main
from weekly_telemetry_aggregator.watch_context import (
    build_watch_context,
    inventory_environment,
    normalize_npm_package,
    normalize_repo_url,
)

ANCHOR = datetime(2026, 8, 12, 6, 0, tzinfo=UTC)


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / ".opencode" / "plugins").mkdir(parents=True)
    (root / ".opencode" / "skills" / "existing-skill").mkdir(parents=True)
    (root / ".opencode" / "commands").mkdir(parents=True)
    (root / ".opencode" / "agents" / "existing-agent").mkdir(parents=True)
    (root / ".opencode" / "skills" / "existing-skill" / "SKILL.md").write_text(
        "---\nname: existing-skill\n---\n", encoding="utf-8"
    )
    (root / ".opencode" / "commands" / "existing-command.md").write_text(
        "# Existing command\n", encoding="utf-8"
    )
    (root / ".opencode" / "agents" / "existing-agent" / "existing-agent.md").write_text(
        "# Existing agent\n", encoding="utf-8"
    )
    return root


def _item(**values: object) -> dict[str, object]:
    return {"name": "candidate", "category": "plugin", **values}


def test_normalize_npm_package_preserves_exact_identity() -> None:
    assert normalize_npm_package("@Tarquinen/opencode-dcp@latest") == "@tarquinen/opencode-dcp"
    assert (
        normalize_npm_package("superpowers@git+https://github.com/obra/superpowers.git")
        == "superpowers"
    )
    assert normalize_npm_package("@tarquinen/opencode-dcp-extra@latest") != (
        "@tarquinen/opencode-dcp"
    )


def test_declared_package_match_is_exact_and_not_a_candidate(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / ".opencode" / "opencode.json").write_text(
        json.dumps({"plugin": ["@tarquinen/opencode-dcp@latest"]}), encoding="utf-8"
    )

    context = build_watch_context(
        root,
        {
            "new_items": [
                _item(name="dcp", npm_package="@tarquinen/opencode-dcp"),
                _item(name="dcp-extra", npm_package="@tarquinen/opencode-dcp-extra"),
            ]
        },
        generated_at=ANCHOR,
    )

    matches = {item["npm_package"]: item for item in context["market_matches"]}
    assert matches["@tarquinen/opencode-dcp"]["existing_state"] == "declared"
    assert matches["@tarquinen/opencode-dcp"]["capability_state"] == "covered"
    assert matches["@tarquinen/opencode-dcp"]["match"]["type"] == "npm_package"
    assert matches["@tarquinen/opencode-dcp-extra"]["existing_state"] == "absent"


def test_scoped_package_declaration_has_no_implicit_repository_identity(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / ".opencode" / "opencode.json").write_text(
        json.dumps({"plugin": ["@acme/tool@latest"]}), encoding="utf-8"
    )

    inventory = inventory_environment(root)
    declaration = next(plugin for plugin in inventory.plugins if plugin.declared)
    assert declaration.npm_package == "@acme/tool"
    assert declaration.repo_url is None


def test_repo_url_normalization_matches_git_https_git_suffix_and_slash(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / ".opencode" / "opencode.json").write_text(
        json.dumps({"plugin": ["sample@git+https://github.com/Acme/Tool.git"]}),
        encoding="utf-8",
    )
    expected = "https://github.com/acme/tool"
    assert normalize_repo_url("git+https://github.com/Acme/Tool.git") == expected
    assert normalize_repo_url("https://github.com/acme/tool/") == expected
    assert normalize_repo_url("https://github.com/acme/tool.git/") == expected

    context = build_watch_context(
        root,
        {"new_items": [_item(name="tool", npm_package="different", repo_url=expected + "/")]},
        generated_at=ANCHOR,
    )
    match = context["market_matches"][0]
    assert match["existing_state"] == "declared"
    assert match["match"]["type"] == "repo_url"


def test_local_plugin_basename_and_catalog_identities_are_observed(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / ".opencode" / "plugins" / "local-tool.mjs").write_text("", encoding="utf-8")

    context = build_watch_context(
        root,
        {
            "new_items": [
                _item(name="local-tool"),
                {"name": "existing-skill", "category": "skill"},
            ]
        },
        generated_at=ANCHOR,
    )
    matches = {item["name"]: item for item in context["market_matches"]}
    assert matches["local-tool"]["existing_state"] == "observed"
    assert matches["local-tool"]["match"]["type"] == "basename"
    assert matches["existing-skill"]["existing_state"] == "observed"
    assert context["counts"]["skills"] == 1
    assert context["counts"]["commands"] == 1
    assert context["counts"]["agents"] == 1


def test_missing_config_is_nonfatal_and_does_not_read_global_paths(
    tmp_path: Path, monkeypatch
) -> None:
    root = _project(tmp_path)

    def fail_home() -> Path:
        raise AssertionError("watch context must not inspect global paths")

    monkeypatch.setattr(Path, "home", fail_home)
    context = build_watch_context(
        root,
        {"new_items": [_item(npm_package="not-installed")]},
        generated_at=ANCHOR,
    )
    assert context["plugins"] == []
    assert context["plugin_config"]["available"] is False
    assert context["market_matches"][0]["existing_state"] == "unknown"
    assert any("plugin config not found" in warning for warning in context["warnings"])


def test_cli_writes_anchor_dated_context(tmp_path: Path, capsys) -> None:
    root = _project(tmp_path)
    (root / ".opencode" / "opencode.json").write_text(
        json.dumps({"plugin": ["@tarquinen/opencode-dcp@latest"]}), encoding="utf-8"
    )
    reports = tmp_path / "reports"
    reports.mkdir()
    ecosystem = reports / "weekly-ecosystem-2026-08-12.json"
    ecosystem.write_text(
        json.dumps(
            {"schema_version": 2, "new_items": [_item(npm_package="@tarquinen/opencode-dcp")]}
        ),
        encoding="utf-8",
    )
    config = tmp_path / "weekly-config.json"
    config.write_text(
        json.dumps({"project_root": str(root), "output_dir": str(reports)}), encoding="utf-8"
    )

    rc = main(
        [
            "watch-context",
            "--config",
            str(config),
            "--anchor",
            "2026-08-12T06:00:00Z",
        ]
    )

    assert rc == 0
    output = reports / "weekly-watch-context-2026-08-12.json"
    assert output.is_file()
    data = json.loads(output.read_text(encoding="utf-8"))
    assert data["date"] == "2026-08-12"
    assert data["market_matches"][0]["existing_state"] == "declared"
    assert "watch-context:" in capsys.readouterr().out
