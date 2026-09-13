"""Deterministic worktree inventory and ecosystem context tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from weekly_telemetry_aggregator.cli import main
from weekly_telemetry_aggregator.util import load_jsonc
from weekly_telemetry_aggregator.watch_context import (
    build_local_inventory,
    build_watch_context,
    enrich_candidates,
    hints_for,
    inventory_environment,
    normalize_npm_package,
    normalize_repo_url,
)
from weekly_telemetry_aggregator.watch_memory import normalize_id

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


def test_context_exposes_observation_only_architecture_projection(tmp_path: Path) -> None:
    root = _project(tmp_path)
    context = build_watch_context(
        root,
        {"new_items": [_item(name="candidate", npm_package="candidate")]},
        generated_at=ANCHOR,
        harness_scope={"profile": "advisory", "unscoped_file_count": 2},
    )
    observation = context["architecture_observations"]
    assert observation["state_counts"]["unknown"] == 1
    assert observation["config"]["available"] is False
    assert observation["harness_scope"]["profile"] == "advisory"


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


def test_jsonc_config_declarations_are_parsed(tmp_path: Path) -> None:
    """C4 (v6.0.p) : opencode.jsonc (commentaires + virgules finales) déclare les plugins."""
    root = _project(tmp_path)
    (root / ".opencode" / "opencode.jsonc").write_text(
        """{
          // commentaire JSONC
          "plugin": [
            "@tarquinen/opencode-dcp@latest", // virgule finale tolérée
          ],
        }
        """,
        encoding="utf-8",
    )

    inventory = inventory_environment(root)
    assert inventory.config_available is True
    assert inventory.config_valid is True
    assert inventory.config_files == [".opencode/opencode.jsonc"]
    declared = [p for p in inventory.plugins if p.declared]
    assert [p.npm_package for p in declared] == ["@tarquinen/opencode-dcp"]

    context = build_watch_context(root, {"new_items": []}, generated_at=ANCHOR)
    assert context["plugin_config"]["valid"] is True
    assert context["counts"]["declared_plugins"] == 1


def test_ecosystem_report_accepts_jsonc(tmp_path: Path) -> None:
    """C4 (v6.0.p) : la docstring « JSONC is accepted » de load_ecosystem_report est vraie."""
    from weekly_telemetry_aggregator.watch_context import load_ecosystem_report

    path = tmp_path / "weekly-ecosystem-2026-08-12.json"
    path.write_text(
        '{"schema_version": 2, "new_items": [{"name": "sample"}], /* bloc */}\n',
        encoding="utf-8",
    )
    payload, error = load_ecosystem_report(path)
    assert error is None
    assert payload["new_items"] == [{"name": "sample"}]


# ------------------------------------------------- T6 : inventaire + crosswalk


def _mm_id(market_match: dict) -> str:
    return normalize_id(
        str(market_match.get("name") or ""),
        market_match.get("npm_package"),
        market_match.get("repo_url"),
    )


def _fiche(fiche_id: str, name: str, summary: str = "") -> dict:
    return {
        "id": fiche_id,
        "name": name,
        "sources": [],
        "score": {"total": 50, "breakdown": {}},
        "security": {"verdict": "clean", "reason": None},
        "summary": summary,
        "signature": {"version": None, "published_at": None},
        "local_relevance_hints": [],
    }


def _crosswalk_project(tmp_path: Path) -> tuple[Path, dict]:
    root = _project(tmp_path)
    ecosystem = {
        "schema_version": 2,
        "new_items": [
            {
                "name": "alpha",
                "npm_package": "alpha-pkg",
                "found_via": ["npm:registry"],
                "description": "Alpha tooling for builds",
            },
            {
                "name": "beta",
                "repo_url": "https://github.com/acme/beta",
                "found_via": ["github:acme"],
                "description": "Beta repository tool",
            },
            {"name": "gamma", "description": "Gamma residual item"},
            {
                "name": "delta",
                "npm_package": "delta-pkg",
                "description": "Blocked supply-chain sample",
            },
        ],
    }
    return root, ecosystem


def _candidates_file(tmp_path: Path, fiches: list[dict], blocked: list[dict] | None = None) -> Path:
    payload = {
        "schema_version": 1,
        "mode": "distill",
        "date": "2026-08-12",
        "candidates": fiches,
        "security_annex": blocked or [],
        "dropped_memory": 0,
        "quotas_applied": {},
        "warnings": [],
    }
    path = tmp_path / "watch-candidates-2026-08-12.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_build_local_inventory_kinds_and_descriptions(tmp_path: Path) -> None:
    root = tmp_path / "project"
    (root / ".opencode" / "skills" / "context-cache").mkdir(parents=True)
    (root / ".opencode" / "skills" / "context-cache" / "SKILL.md").write_text(
        "---\nname: context-cache\ndescription: Gestion du cache contexte\n---\nCorps.\n",
        encoding="utf-8",
    )
    (root / ".opencode" / "agents").mkdir(parents=True)
    (root / ".opencode" / "agents" / "reviewer.md").write_text(
        "# Reviewer agent\n\nDétails.\n", encoding="utf-8"
    )
    (root / ".opencode" / "commands").mkdir(parents=True)
    (root / ".opencode" / "commands" / "ship.md").write_text("Ship it\n", encoding="utf-8")
    (root / ".opencode" / "plugins").mkdir(parents=True)
    (root / ".opencode" / "plugins" / "local.mjs").write_text("", encoding="utf-8")

    inv = build_local_inventory(root)

    # Seul le warning « config plugin absente » (comportement legacy partagé
    # avec inventory_environment) est toléré ici.
    assert [w for w in inv["warnings"] if "plugin config not found" not in w] == []
    by_key = {(item["kind"], item["name"]): item for item in inv["items"]}
    assert by_key[("skill", "context-cache")]["description"] == "Gestion du cache contexte"
    assert by_key[("skill", "context-cache")]["path"] == (".opencode/skills/context-cache/SKILL.md")
    assert by_key[("agent", "reviewer")]["description"] == "Reviewer agent"
    assert by_key[("command", "ship")]["description"] == "Ship it"
    assert by_key[("plugin", "local")]["path"] == ".opencode/plugins/local.mjs"
    assert all(set(item) == {"name", "kind", "path", "description"} for item in inv["items"])


def test_build_local_inventory_includes_declared_plugins(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / ".opencode" / "opencode.json").write_text(
        json.dumps({"plugin": ["@acme/tool@latest"]}), encoding="utf-8"
    )

    inv = build_local_inventory(root)

    declared = [item for item in inv["items"] if item["kind"] == "plugin"]
    assert [item["name"] for item in declared] == ["@acme/tool"]
    # Plugin déclaré : pas de description locale fiable → chaîne vide.
    assert all(item["description"] == "" for item in declared)


def test_hints_for_matches_tokens_and_caps_at_five() -> None:
    items = [
        {"name": f"skl-{n}", "kind": "skill", "path": f"p{n}", "description": f"cache contexte {n}"}
        for n in range(7)
    ]
    fiche = {"name": "context-goblin", "summary": "gestion du cache pour prompts"}

    hints = hints_for(fiche, items)

    assert hints == [f"skl-{n}" for n in range(5)]
    assert hints_for({"name": "sans-rapport", "summary": "aucun jeton commun"}, items) == []


def test_hints_for_ignores_tokens_shorter_than_three_chars() -> None:
    items = [{"name": "ai-helper", "kind": "skill", "path": "p", "description": "ai powered"}]

    assert hints_for({"name": "ai thing", "summary": ""}, items) == []


def test_crosswalk_scopes_market_matches_to_candidates_plus_residual(
    tmp_path: Path,
) -> None:
    root, ecosystem = _crosswalk_project(tmp_path)
    candidates_file = _candidates_file(
        tmp_path,
        [_fiche("npm:alpha-pkg", "alpha"), _fiche("gh:acme/beta", "beta")],
        blocked=[{"id": "npm:delta-pkg", "name": "delta", "reason": "typosquat:x"}],
    )

    ctx = build_watch_context(root, ecosystem, generated_at=ANCHOR, candidates_path=candidates_file)
    legacy = build_watch_context(root, ecosystem, generated_at=ANCHOR)

    ids = {_mm_id(match) for match in ctx["market_matches"]}
    assert ids == {"npm:alpha-pkg", "gh:acme/beta", "url:gamma"}
    assert len(legacy["market_matches"]) == 4  # sans candidats : comportement legacy inchangé
    assert not any("watch-candidates" in warning for warning in ctx["warnings"])


def test_corrupt_candidates_degrades_to_legacy_with_warning(tmp_path: Path) -> None:
    root, ecosystem = _crosswalk_project(tmp_path)
    bad_json = tmp_path / "watch-candidates-broken.json"
    bad_json.write_text("{oops", encoding="utf-8")
    wrong_mode = _candidates_file(tmp_path, [])
    wrong_mode.write_text(
        json.dumps({**json.loads(wrong_mode.read_text(encoding="utf-8")), "mode": "fallback"}),
        encoding="utf-8",
    )

    for bad in (bad_json, wrong_mode):
        ctx = build_watch_context(root, ecosystem, generated_at=ANCHOR, candidates_path=bad)
        assert len(ctx["market_matches"]) == 4  # repli legacy complet
        assert any("watch-candidates" in warning for warning in ctx["warnings"])


def test_enrich_candidates_fills_state_match_hints_and_residual(tmp_path: Path) -> None:
    root, ecosystem = _crosswalk_project(tmp_path)
    skill_dir = root / ".opencode" / "skills" / "context-cache"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: context-cache\ndescription: Gestion du cache contexte\n---\n",
        encoding="utf-8",
    )
    fiches = [
        _fiche("npm:alpha-pkg", "alpha", summary="outillage du cache et contexte local"),
        _fiche("gh:acme/beta", "beta", summary="outil sans rapport"),
    ]
    candidates_file = _candidates_file(
        tmp_path,
        fiches,
        blocked=[{"id": "npm:delta-pkg", "name": "delta", "reason": "typosquat:x"}],
    )
    ctx = build_watch_context(root, ecosystem, generated_at=ANCHOR, candidates_path=candidates_file)
    payload = load_jsonc(candidates_file)
    inv = build_local_inventory(root)

    enriched = enrich_candidates(payload, ctx, ecosystem, inv["items"], now=ANCHOR)

    assert enriched is not None
    assert enriched["mode"] == "enriched"
    assert enriched["date"] == "2026-08-12"
    by_id = {fiche["id"]: fiche for fiche in enriched["candidates"]}
    alpha = by_id["npm:alpha-pkg"]
    assert alpha["existing_state"] == "unknown"  # pas de config plugin → non prouvé
    assert alpha["market_match"] is None
    assert alpha["local_relevance_hints"] == ["context-cache"]
    beta = by_id["gh:acme/beta"]
    assert beta["existing_state"] == "unknown"
    assert beta["local_relevance_hints"] == []
    residual_ids = {row["id"] for row in enriched["residual"]}
    assert "url:gamma" in residual_ids
    assert "npm:delta-pkg" not in residual_ids  # bloqué sécurité : jamais en résiduel
    gamma = next(row for row in enriched["residual"] if row["id"] == "url:gamma")
    assert set(gamma) == {"id", "name", "description", "score_total"}
    assert isinstance(gamma["score_total"], int | float)


def test_enrich_candidates_returns_none_on_invalid_payload(tmp_path: Path) -> None:
    root, ecosystem = _crosswalk_project(tmp_path)
    ctx = build_watch_context(root, ecosystem, generated_at=ANCHOR)
    inv = build_local_inventory(root)

    assert enrich_candidates({"mode": "fallback"}, ctx, ecosystem, inv["items"], now=ANCHOR) is None
    assert enrich_candidates(None, ctx, ecosystem, inv["items"], now=ANCHOR) is None


def test_residual_capped_at_50_with_compact_sorted_entries(tmp_path: Path) -> None:
    root = _project(tmp_path)
    ecosystem = {
        "schema_version": 2,
        "new_items": [
            {"name": f"pkg-{n}", "npm_package": f"pkg-{n}", "description": "x" * 400}
            for n in range(60)
        ],
    }
    candidates_file = _candidates_file(tmp_path, [_fiche("npm:pkg-0", "pkg-0")])
    ctx = build_watch_context(root, ecosystem, generated_at=ANCHOR, candidates_path=candidates_file)
    payload = load_jsonc(candidates_file)
    inv = build_local_inventory(root)

    enriched = enrich_candidates(payload, ctx, ecosystem, inv["items"], now=ANCHOR)

    assert len(enriched["residual"]) == 50
    assert all(len(row["description"]) <= 200 for row in enriched["residual"])
    totals = [row["score_total"] for row in enriched["residual"]]
    assert totals == sorted(totals, reverse=True)


def test_empty_candidates_snapshot_still_scopes_blocked_out(tmp_path: Path) -> None:
    """Snapshot valide avec candidates:[] : le scope s'applique quand même.

    Sinon les bloqués sécurité fuient dans market_matches pendant que le
    fichier enrichi (résiduel) les exclut — incohérence inter-artefacts.
    """

    root, ecosystem = _crosswalk_project(tmp_path)
    candidates_file = _candidates_file(
        tmp_path,
        [],
        blocked=[{"id": "npm:delta-pkg", "name": "delta", "reason": "typosquat:x"}],
    )

    ctx = build_watch_context(root, ecosystem, generated_at=ANCHOR, candidates_path=candidates_file)
    payload = load_jsonc(candidates_file)
    inv = build_local_inventory(root)
    enriched = enrich_candidates(payload, ctx, ecosystem, inv["items"], now=ANCHOR)

    ctx_ids = {_mm_id(match) for match in ctx["market_matches"]}
    assert "npm:delta-pkg" not in ctx_ids
    # Sans fiche retenue, tout le non-bloqué devient résiduel : les deux
    # artefacts exposent exactement le même ensemble d'ids.
    assert ctx_ids == {"npm:alpha-pkg", "gh:acme/beta", "url:gamma"}
    assert enriched["candidates"] == []
    assert {row["id"] for row in enriched["residual"]} == ctx_ids


def test_cli_writes_enriched_candidates_alongside_context(tmp_path: Path, capsys) -> None:
    root, ecosystem = _crosswalk_project(tmp_path)
    skill_dir = root / ".opencode" / "skills" / "context-cache"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: context-cache\ndescription: Gestion du cache contexte\n---\n",
        encoding="utf-8",
    )
    reports = tmp_path / "reports"
    reports.mkdir()
    date = "2026-08-12"
    (reports / f"weekly-ecosystem-{date}.json").write_text(json.dumps(ecosystem), encoding="utf-8")
    candidates_file = _candidates_file(
        tmp_path,
        [_fiche("npm:alpha-pkg", "alpha", summary="gestion du cache contexte")],
        blocked=[{"id": "npm:delta-pkg", "name": "delta", "reason": "typosquat:x"}],
    )
    (reports / f"watch-candidates-{date}.json").write_text(
        candidates_file.read_text(encoding="utf-8"), encoding="utf-8"
    )
    config = tmp_path / "weekly-config.json"
    config.write_text(
        json.dumps({"project_root": str(root), "output_dir": str(reports)}), encoding="utf-8"
    )

    rc = main(["watch-context", "--config", str(config), "--anchor", f"{date}T06:00:00Z"])

    assert rc == 0
    enriched = json.loads(
        (reports / f"watch-candidates-enriched-{date}.json").read_text(encoding="utf-8")
    )
    assert enriched["mode"] == "enriched"
    fiche = enriched["candidates"][0]
    assert fiche["id"] == "npm:alpha-pkg"
    assert fiche["local_relevance_hints"] == ["context-cache"]
    # beta (sans fiche) et gamma tombent en résiduel ; delta bloqué en annexe.
    assert {row["id"] for row in enriched["residual"]} == {"gh:acme/beta", "url:gamma"}
    context = json.loads(
        (reports / f"weekly-watch-context-{date}.json").read_text(encoding="utf-8")
    )
    # Scope candidats : candidat + résiduels ; le bloqué delta en est exclu.
    assert {_mm_id(match) for match in context["market_matches"]} == {
        "npm:alpha-pkg",
        "gh:acme/beta",
        "url:gamma",
    }
    assert f"watch-candidates-enriched-{date}" in capsys.readouterr().out
