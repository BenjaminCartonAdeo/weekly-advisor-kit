"""Deterministic worktree inventory and ecosystem context tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from weekly_telemetry_aggregator.cli import main
from weekly_telemetry_aggregator.util import load_jsonc
from weekly_telemetry_aggregator.watch_context import (
    EnvironmentInventory,
    FileRecord,
    PluginRecord,
    _category_is_observable,
    _config_evidence,
    _markdown_description,
    _market_identifiers,
    _match_market_item,
    _match_summary,
    _observed_evidence,
    _record_to_dict,
    _repo_slug,
    _unique_identities,
    build_local_inventory,
    build_watch_context,
    enrich_candidates,
    hints_for,
    inventory_environment,
    normalize_npm_package,
    normalize_repo_url,
    parse_plugin_spec,
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


# ============================================================ characterization
# Behaviour captured BEFORE the module split so that moving code is provably
# behaviour-preserving.  Every assertion below was checked against the real
# implementation, not inferred from names.


# ------------------------------------------- identité : normaliseurs d'URL

GH = "https://github.com/owner/repo"


def test_normalize_repo_url_canonicalizes_transport_case_query_and_slashes() -> None:
    """Toutes les écritures d'un même dépôt GitHub donnent la forme canonique."""
    assert normalize_repo_url("https://github.com/Owner/Repo.git") == GH
    assert normalize_repo_url("git+https://github.com/Owner/Repo.git") == GH
    assert normalize_repo_url("git://github.com/Owner/Repo.git") == GH
    assert normalize_repo_url("ssh://git@github.com/Owner/Repo.git") == GH
    assert normalize_repo_url("HTTPS://GitHub.com/Owner/Repo") == GH
    assert normalize_repo_url("https://github.com/owner/repo/") == GH
    assert normalize_repo_url("https://github.com/owner/repo.git/") == GH
    assert normalize_repo_url("https://github.com/owner//repo//") == GH
    assert normalize_repo_url("https://user:pw@github.com/Owner/Repo.git") == GH
    assert normalize_repo_url("  https://github.com/Owner/Repo  ") == GH
    # query + fragment : retirés, la normalisation est purement locale.
    assert normalize_repo_url("https://github.com/owner/repo/?ref=main#readme") == GH
    # percent-decoding du path.
    assert normalize_repo_url("https://github.com/owner/repo%20x") == (
        "https://github.com/owner/repo x"
    )


def test_normalize_repo_url_casefolds_path_only_for_github() -> None:
    """Seul github.com casefolds le path : un autre forge garde sa casse."""
    assert normalize_repo_url("https://gitlab.com/Owner/Repo.GIT") == (
        "https://gitlab.com/Owner/Repo"
    )
    assert normalize_repo_url("https://example.com/a/b/") == "https://example.com/a/b"


def test_normalize_repo_url_rejects_scp_syntax_and_non_http_schemes() -> None:
    """`git@host:owner/repo.git` n'est PAS normalisé (docstring vs code)."""
    assert normalize_repo_url("git@github.com:owner/repo.git") is None
    assert normalize_repo_url("ftp://github.com/a/b") is None
    assert normalize_repo_url("file:///tmp/x") is None


def test_normalize_repo_url_rejects_empty_hostless_and_non_string() -> None:
    assert normalize_repo_url("https://github.com/") is None
    assert normalize_repo_url("https://github.com") is None
    assert normalize_repo_url("") is None
    assert normalize_repo_url("   ") is None
    assert normalize_repo_url(None) is None
    assert normalize_repo_url(123) is None


def test_repo_slug_is_last_segment_and_rejects_trailing_slash() -> None:
    assert _repo_slug("https://github.com/owner/repo") == "repo"
    assert _repo_slug("abc") == "abc"
    # rsplit("/") sur un slash final donne une chaîne vide -> None.
    assert _repo_slug("https://github.com/owner/repo/") is None
    assert _repo_slug("https://x/") is None
    assert _repo_slug("") is None
    assert _repo_slug(None) is None


def test_unique_identities_dedups_blanks_but_is_case_sensitive() -> None:
    """Déduplication sensible à la casse ; tri par casefold.

    L'ordre relatif de deux variantes de casse n'est PAS normé (la fonction
    construit un ``set`` puis trie par ``casefold``) : seules les clés de casse
    distincte ont un ordre garanti.
    """
    identities = _unique_identities(["b", "a", "A", None, "  ", " c ", "c"])
    assert set(identities) == {"A", "a", "b", "c"}  # "A" et "a" coexistent
    assert " c " not in identities and "  " not in identities  # blankes retirées, strip() fait
    # Sur les clés de casse distincte, l'ordre est un casefold croissant.
    distinct = [value for value in identities if value not in {"A", "a"}]
    assert distinct == ["b", "c"]
    assert _unique_identities([]) == ()
    # Idempotent : ré-appliquer sur sa propre sortie ne change rien.
    assert _unique_identities(identities) == identities


# ------------------------------------------- identité : specs de plugins

CONFIG_PATH = ".opencode/opencode.json"


def test_parse_plugin_spec_resolves_npm_git_and_bare_names() -> None:
    scoped = parse_plugin_spec("@acme/tool@latest", path=CONFIG_PATH)
    assert (scoped.npm_package, scoped.repo_url, scoped.name) == ("@acme/tool", None, "@acme/tool")
    assert scoped.identities == ("@acme/tool",)
    assert scoped.declared is True
    assert scoped.source == "config"
    assert scoped.path == CONFIG_PATH

    gitted = parse_plugin_spec("sample@git+https://github.com/Acme/Tool.git", path=CONFIG_PATH)
    assert gitted.npm_package == "sample"
    assert gitted.repo_url == "https://github.com/acme/tool"
    # name + npm + repo canonique + slug : 3 identités distinctes.
    assert gitted.identities == ("https://github.com/acme/tool", "sample", "tool")

    plain = parse_plugin_spec("  plain-name  ", path=CONFIG_PATH)
    assert (plain.npm_package, plain.repo_url, plain.name) == ("plain-name", None, "plain-name")
    assert plain.raw == "plain-name"


def test_parse_plugin_spec_names_local_paths_by_stem() -> None:
    """Un chemin local n'a ni npm ni repo : l'identité est le nom de fichier."""
    rel = parse_plugin_spec("./plugins/local", path=CONFIG_PATH)
    assert (rel.npm_package, rel.repo_url, rel.name) == (None, None, "local")
    absolute = parse_plugin_spec("/abs/path/x", path=CONFIG_PATH)
    assert absolute.name == "x"
    file_url = parse_plugin_spec("file:../shared/thing", path=CONFIG_PATH)
    assert file_url.name == "thing"


def test_parse_plugin_spec_drops_fragment_from_local_path() -> None:
    assert parse_plugin_spec("file:../shared/thing#v2", path=CONFIG_PATH).name == "thing"


def test_parse_plugin_spec_uses_repo_slug_when_no_npm_package() -> None:
    spec = parse_plugin_spec("https://github.com/owner/repo", path=CONFIG_PATH)
    assert spec.name == "repo"
    assert spec.npm_package is None
    assert spec.repo_url == GH


# ------------------------------------------- records & inventaire


def test_plugin_record_defaults_are_optional_and_record_to_dict_lists_identities() -> None:
    record = PluginRecord(name="p", source="config", path="c")
    assert (record.raw, record.npm_package, record.repo_url) == (None, None, None)
    assert record.identities == ()
    assert record.declared is False
    rendered = _record_to_dict(record)
    # dataclass -> dict, mais identities devient une liste JSON-compatible.
    assert rendered["identities"] == []
    assert rendered["declared"] is False


def test_file_record_defaults_and_rendering() -> None:
    record = FileRecord(name="f", path="p")
    assert record.identities == ()
    assert _record_to_dict(record) == {"name": "f", "path": "p", "identities": []}


def test_environment_inventory_defaults_are_per_instance() -> None:
    first = EnvironmentInventory()
    second = EnvironmentInventory()
    first.plugins.append(PluginRecord(name="x", source="config", path="c"))
    first.directories["plugins"] = True
    assert second.plugins == [] and second.directories == {}
    assert (first.config_available, first.config_valid) == (False, False)
    assert (first.skills, first.commands, first.agents, first.warnings) == ([], [], [], [])


def test_inventory_environment_orders_declared_before_local(tmp_path: Path) -> None:
    root = tmp_path / "project"
    (root / ".opencode" / "plugins").mkdir(parents=True)
    (root / ".opencode" / "plugins" / "zzz-local.mjs").write_text("", encoding="utf-8")
    (root / ".opencode" / "opencode.json").write_text(
        json.dumps({"plugin": ["@acme/tool@latest"]}), encoding="utf-8"
    )

    inventory = inventory_environment(root)

    assert [p.name for p in inventory.plugins] == ["@acme/tool", "zzz-local"]
    assert [p.declared for p in inventory.plugins] == [True, False]
    assert inventory.config_available is True
    assert inventory.config_valid is True
    assert inventory.config_files == [".opencode/opencode.json"]
    assert inventory.directories == {
        "plugins": True,
        "skills": False,
        "commands": False,
        "agents": False,
    }


def test_inventory_environment_ignores_non_string_declarations(tmp_path: Path) -> None:
    """Un tableau de non-chaînes reste « valide » : chaque entrée est ignorée."""
    root = tmp_path / "project"
    (root / ".opencode").mkdir(parents=True)
    (root / ".opencode" / "opencode.json").write_text(
        json.dumps({"plugin": [1, 2]}), encoding="utf-8"
    )

    inventory = inventory_environment(root)

    assert inventory.config_available is True
    assert inventory.config_valid is True
    assert inventory.plugins == []
    # Un warning par entrée ignorée (les warnings ne sont pas dédupliqués ici).
    assert (
        inventory.warnings.count("ignored non-string plugin declaration in .opencode/opencode.json")
        == 2
    )


def test_inventory_environment_flags_non_sequence_plugin_payload(tmp_path: Path) -> None:
    root = tmp_path / "project"
    (root / ".opencode").mkdir(parents=True)
    (root / ".opencode" / "opencode.json").write_text(json.dumps({"plugin": 42}), encoding="utf-8")

    inventory = inventory_environment(root)

    assert inventory.config_available is True
    assert inventory.config_valid is False  # plugin n'est pas une séquence
    assert any("plugin must be an array of strings" in w for w in inventory.warnings)


def test_inventory_environment_missing_root_is_a_warning_not_a_crash(tmp_path: Path) -> None:
    inventory = inventory_environment(tmp_path / "absent")

    assert inventory.plugins == []
    assert any("does not exist" in w for w in inventory.warnings)


# ------------------------------------------- evidence : identité marché


def test_market_identifiers_infers_npm_and_repo_only_from_url_shaped_names() -> None:
    assert _market_identifiers({"name": "Tool", "npm_package": "@acme/tool"}) == (
        "@acme/tool",
        None,
        {"@acme/tool", "tool"},
    )
    # found_via=npm: le nom devient le paquet.
    assert _market_identifiers({"name": "dcp", "found_via": ["npm:registry"]}) == (
        "dcp",
        None,
        {"dcp"},
    )
    # found_via=github: le nom n'est PAS une URL -> aucune repo.
    assert _market_identifiers({"name": "beta", "found_via": ["github:acme"]}) == (
        None,
        None,
        {"beta"},
    )
    # ... mais un nom en forme d'URL est normalisé.
    assert _market_identifiers({"name": "owner/repo", "found_via": ["github:acme"]}) == (
        None,
        None,
        {"owner/repo"},
    )
    assert _market_identifiers({}) == (None, None, set())


def test_config_evidence_uses_declared_plugins_only() -> None:
    records = [
        PluginRecord(
            name="@acme/tool",
            source="config",
            path="cfg",
            raw="@acme/tool@latest",
            npm_package="@acme/tool",
            identities=("@acme/tool",),
            declared=True,
        ),
        PluginRecord(name="local", source="local_file", path="l", identities=("local",)),
    ]

    assert _config_evidence("@acme/tool", None, {"@acme/tool"}, records) == [
        {
            "type": "npm_package",
            "value": "@acme/tool",
            "source": "config",
            "path": "cfg",
            "raw": "@acme/tool@latest",
        }
    ]
    # Un plugin non déclaré (local) ne produit jamais de preuve « config ».
    assert _config_evidence(None, None, {"local"}, records) == []


def test_config_evidence_identity_fallback_only_without_npm_and_repo() -> None:
    record = PluginRecord(
        name="thing",
        source="config",
        path="cfg",
        raw="thing",
        identities=("thing",),
        declared=True,
    )

    # Sans npm ni repo : l'identité seule suffit.
    assert _config_evidence(None, None, {"thing"}, [record])[0]["type"] == "plugin_identity"
    # Avec un npm : le repli identité est inhibé (pas de faux positif).
    assert _config_evidence("other", None, {"thing"}, [record]) == []


def _evidence_inventory() -> EnvironmentInventory:
    return EnvironmentInventory(
        plugins=[
            PluginRecord(
                name="@acme/tool",
                source="config",
                path="cfg",
                npm_package="@acme/tool",
                identities=("@acme/tool",),
                declared=True,
            ),
            PluginRecord(
                name="local-tool",
                source="local_file",
                path="p/local-tool.mjs",
                identities=("local-tool",),
            ),
        ],
        skills=[
            FileRecord(name="existing-skill", path="s/SKILL.md", identities=("existing-skill",))
        ],
        commands=[FileRecord(name="ship", path="c/ship.md", identities=("ship",))],
        agents=[FileRecord(name="reviewer", path="a/reviewer.md", identities=("reviewer",))],
        directories={"plugins": True, "skills": True, "commands": True, "agents": True},
        config_available=True,
        config_valid=True,
    )


def test_observed_evidence_scopes_to_category_and_falls_back_to_all() -> None:
    inventory = _evidence_inventory()

    skill = _observed_evidence(
        {"name": "existing-skill", "category": "skill"}, {"existing-skill"}, inventory
    )
    assert skill == [
        {
            "type": "skill",
            "value": "existing-skill",
            "source": "worktree",
            "path": "s/SKILL.md",
            "name": "existing-skill",
        }
    ]

    # Un plugin local observé est typé « basename ».
    plugin = _observed_evidence(
        {"name": "local-tool", "category": "plugin"}, {"local-tool"}, inventory
    )
    assert plugin[0]["type"] == "basename"

    # Catégorie inconnu -> les quatre groupes sont inspectés.
    unknown = _observed_evidence({"name": "ship", "category": "weird"}, {"ship"}, inventory)
    assert unknown == [
        {
            "type": "command",
            "value": "ship",
            "source": "worktree",
            "path": "c/ship.md",
            "name": "ship",
        }
    ]

    # Catégorie plugin -> seuls les plugins NON déclarés sont considérés.
    assert (
        _observed_evidence(
            {"name": "existing-skill", "category": "plugin"}, {"existing-skill"}, inventory
        )
        == []
    )


def test_observed_evidence_category_plural_is_accepted() -> None:
    inventory = _evidence_inventory()
    for category in ("skill", "skills"):
        found = _observed_evidence(
            {"name": "existing-skill", "category": category}, {"existing-skill"}, inventory
        )
        assert [entry["type"] for entry in found] == ["skill"]


def test_category_is_observable_requires_directory_or_valid_config() -> None:
    skill_dir = EnvironmentInventory(directories={"skills": True})
    assert _category_is_observable({"category": "skill"}, skill_dir) is True
    assert _category_is_observable({"category": "command"}, skill_dir) is False
    # Catégorie inconnue : au moins un répertoire existe.
    assert _category_is_observable({"category": "weird"}, skill_dir) is True

    no_config = EnvironmentInventory(directories={"plugins": True})
    # Un paquet nommé ne peut pas être prouvé absent sans config valide.
    assert _category_is_observable({"category": "plugin", "npm_package": "x"}, no_config) is False
    # Sans identité nom/package, le répertoire plugins suffit.
    assert _category_is_observable({"category": "plugin"}, no_config) is True

    # Config présente MAIS invalide : c'est ce couple qui distingue « lisible »
    # de « exploitable ». Une config lisible/illisible à la fois ne prouve rien.
    unreadable_config = EnvironmentInventory(
        directories={"plugins": True}, config_available=True, config_valid=False
    )
    assert (
        _category_is_observable({"category": "plugin", "npm_package": "x"}, unreadable_config)
        is False
    )
    assert _category_is_observable({"category": "plugin"}, unreadable_config) is True
    # Config valide : l'absence devient prouvable pour un paquet nommé.
    valid_config = EnvironmentInventory(
        directories={"plugins": True}, config_available=True, config_valid=True
    )
    assert _category_is_observable({"category": "plugin", "npm_package": "x"}, valid_config) is True


def _state_inventory(
    *, available: bool, valid: bool, dirs: dict[str, bool]
) -> EnvironmentInventory:
    return EnvironmentInventory(
        plugins=[
            PluginRecord(
                name="@acme/tool",
                source="config",
                path="c",
                npm_package="@acme/tool",
                identities=("@acme/tool",),
                declared=True,
            )
        ],
        directories=dirs,
        config_available=available,
        config_valid=valid,
    )


def test_match_market_item_state_matrix() -> None:
    declared = {"name": "x", "npm_package": "@acme/tool", "category": "plugin"}
    other = {"name": "zzz", "npm_package": "other-pkg", "category": "plugin"}
    good = _state_inventory(available=True, valid=True, dirs={"plugins": True})
    bad = _state_inventory(available=False, valid=False, dirs={"plugins": True})

    # « declared » l'emporte même quand la config n'est pas valide.
    assert _match_market_item(declared, good)["existing_state"] == "declared"
    assert _match_market_item(declared, bad)["existing_state"] == "declared"
    # « absent » exige une preuve d'observabilité.
    assert _match_market_item(other, good)["existing_state"] == "absent"
    # Sans config lisible : « unknown », jamais « absent » (pas de faux candidat).
    assert _match_market_item(other, bad)["existing_state"] == "unknown"
    # Item sans aucune identité -> unknown immédiat.
    assert _match_market_item({}, good)["existing_state"] == "unknown"


def test_match_market_item_result_is_a_json_safe_copy_with_capability_state() -> None:
    good = _state_inventory(available=True, valid=True, dirs={"plugins": True})
    item = {"name": "x", "npm_package": "@acme/tool", "category": "plugin"}

    result = _match_market_item(item, good)

    assert result["normalized"] == {"npm_package": "@acme/tool", "repo_url": None}
    # Identité prouvée -> covered ; à défaut -> unknown (jamais « absent »).
    assert result["capability_state"] == "covered"
    assert result["match"]["evidence"][0]["type"] == "npm_package"
    # Le snapshot d'entrée n'est pas muté.
    assert item == {"name": "x", "npm_package": "@acme/tool", "category": "plugin"}


def test_match_market_item_declared_is_covered_and_absent_is_not() -> None:
    good = _state_inventory(available=True, valid=True, dirs={"plugins": True})
    absent = _match_market_item({"name": "z", "npm_package": "nope"}, good)
    assert absent["existing_state"] == "absent"
    assert absent["capability_state"] == "unknown"
    assert absent["match"] is None


def test_match_summary_is_none_without_evidence_and_embeds_otherwise() -> None:
    assert _match_summary([]) is None
    entry = {"type": "npm_package", "value": "p", "source": "config"}
    summary = _match_summary([entry])
    assert summary == {**entry, "evidence": [entry]}
    assert summary is not entry


# ------------------------------------------- inventaire local & hints


def test_markdown_description_prefers_frontmatter_then_first_heading(tmp_path: Path) -> None:
    def write(name: str, text: str) -> Path:
        path = tmp_path / name
        path.write_text(text, encoding="utf-8")
        return path

    assert _markdown_description(write("a.md", "---\ndescription: FM\n---\nBody\n")) == "FM"
    # description vide -> on retombe sur le corps.
    assert (
        _markdown_description(write("b.md", "---\ndescription:  \n---\nFallback\n")) == "Fallback"
    )
    assert _markdown_description(write("c.md", "# Titre\ncorps\n")) == "Titre"
    assert _markdown_description(write("d.md", "\n\n  ##  Espacé \n")) == "Espacé"
    assert _markdown_description(write("e.md", "")) == ""
    # Fichier illisible / absent -> chaîne vide, jamais d'exception.
    assert _markdown_description(tmp_path / "absent.md") == ""


def test_build_local_inventory_sorts_by_kind_then_name(tmp_path: Path) -> None:
    root = tmp_path / "project"
    (root / ".opencode" / "skills" / "zeta").mkdir(parents=True)
    (root / ".opencode" / "skills" / "zeta" / "SKILL.md").write_text(
        "---\ndescription: Cache\n---\n", encoding="utf-8"
    )
    (root / ".opencode" / "commands").mkdir(parents=True)
    (root / ".opencode" / "commands" / "ship.md").write_text("Ship\n", encoding="utf-8")
    (root / ".opencode" / "plugins").mkdir(parents=True)
    (root / ".opencode" / "plugins" / "local.mjs").write_text("", encoding="utf-8")

    inv = build_local_inventory(root)

    # tri (kind, casefold(name), path) : agent absent, command < plugin < skill
    assert [(i["kind"], i["name"]) for i in inv["items"]] == [
        ("command", "ship"),
        ("plugin", "local"),
        ("skill", "zeta"),
    ]
    # Un plugin n'a jamais de description locale fiable.
    assert next(i for i in inv["items"] if i["kind"] == "plugin")["description"] == ""
    assert all(set(i) == {"name", "kind", "path", "description"} for i in inv["items"])
    # warnings dedup + triés.
    assert inv["warnings"] == sorted(set(inv["warnings"]))


def test_build_local_inventory_returns_empty_shape_on_absent_project(tmp_path: Path) -> None:
    """Projet absent : aucun item, et le seul warning est l'absence de config."""
    inv = build_local_inventory(tmp_path / "absent")

    assert inv["items"] == []
    assert inv["warnings"] == [
        "plugin config not found under .opencode/ (opencode.json/opencode.jsonc)"
    ]


def test_hints_for_dedups_by_name_and_keeps_inventory_order() -> None:
    items = [
        {"name": "zulu", "description": "cache"},
        {"name": "alpha", "description": "cache"},
        {"name": "zulu", "description": "cache"},
        {"name": "", "description": "cache"},
        {"name": "mike", "description": "cache"},
    ]

    assert hints_for({"name": "cache tool"}, items) == ["zulu", "alpha", "mike"]


def test_hints_for_matches_summary_tokens_not_only_name() -> None:
    items = [{"name": "context-cache", "description": "Gestion du cache"}]

    assert hints_for({"name": "zzz", "summary": "cache local"}, items) == ["context-cache"]
    assert hints_for({"name": "zzz", "summary": "rien a voir"}, items) == []


# ------------------------------------------- build_watch_context : horodatage


def test_generated_at_accepts_datetime_with_and_without_timezone(tmp_path: Path) -> None:
    """Un datetime naïf est interprété en UTC (pas d'exception)."""
    root = _bare_project(tmp_path)
    eco = {"new_items": []}

    aware = build_watch_context(root, eco, generated_at=datetime(2026, 8, 12, 6, 0, tzinfo=UTC))
    naive = build_watch_context(root, eco, generated_at=datetime(2026, 8, 12, 6, 0))

    assert aware["generated_at"] == "2026-08-12T06:00:00Z"
    assert aware["date"] == "2026-08-12"
    assert naive["generated_at"] == "2026-08-12T06:00:00Z"
    assert naive["date"] == "2026-08-12"


def test_generated_at_accepts_iso_string_and_falls_back_when_unparsable(tmp_path: Path) -> None:
    root = _bare_project(tmp_path)
    eco = {"new_items": []}

    parsed = build_watch_context(root, eco, generated_at="2026-08-12T06:00:00Z")
    assert parsed["generated_at"] == "2026-08-12T06:00:00Z"

    # Chaîne illisible -> heure courante (jamais une exception).
    broken = build_watch_context(root, eco, generated_at="pas-une-date")
    assert broken["generated_at"].endswith("Z")
    assert len(broken["generated_at"]) == len("2026-08-12T06:00:00Z")


def test_generated_at_defaults_to_now_when_omitted(tmp_path: Path) -> None:
    root = _bare_project(tmp_path)

    context = build_watch_context(root, {"new_items": []})

    assert context["generated_at"].endswith("Z")
    assert len(context["generated_at"]) == len("2026-08-12T06:00:00Z")


def test_ecosystem_provenance_keys_are_added_only_when_available(tmp_path: Path) -> None:
    root = _bare_project(tmp_path)

    bare = build_watch_context(root, {"new_items": []}, generated_at=ANCHOR)
    assert "ecosystem_file" not in bare
    assert "ecosystem_generated_at" not in bare

    report = tmp_path / "weekly-ecosystem-2026-08-12.json"
    report.write_text("{}", encoding="utf-8")
    named = build_watch_context(root, {"new_items": []}, generated_at=ANCHOR, ecosystem_path=report)
    # Seul le nom de fichier est conservé, jamais le chemin absolu.
    assert named["ecosystem_file"] == "weekly-ecosystem-2026-08-12.json"

    stamped = build_watch_context(
        root,
        {"new_items": [], "generated_at": "2026-01-01T00:00:00Z"},
        generated_at=ANCHOR,
    )
    assert stamped["ecosystem_generated_at"] == "2026-01-01T00:00:00Z"

    # generated_at non textuel -> clé omise plutôt qu'inventée.
    ignored = build_watch_context(
        root, {"new_items": [], "generated_at": 12345}, generated_at=ANCHOR
    )
    assert "ecosystem_generated_at" not in ignored


def test_counts_split_declared_and_local_plugins(tmp_path: Path) -> None:
    root = tmp_path / "project"
    (root / ".opencode" / "plugins").mkdir(parents=True)
    (root / ".opencode" / "plugins" / "local.mjs").write_text("", encoding="utf-8")
    (root / ".opencode" / "opencode.json").write_text(
        json.dumps({"plugin": ["@acme/tool@latest"]}), encoding="utf-8"
    )

    context = build_watch_context(root, {"new_items": []}, generated_at=ANCHOR)

    assert context["counts"]["declared_plugins"] == 1
    assert context["counts"]["local_plugins"] == 1
    assert [p["name"] for p in context["declared_plugins"]] == ["@acme/tool"]
    assert [p["name"] for p in context["local_plugins"]] == ["local"]
    assert context["plugin_config"] == {
        "files": [".opencode/opencode.json"],
        "available": True,
        "valid": True,
    }


def test_context_schema_and_keys_are_stable(tmp_path: Path) -> None:
    root = tmp_path / "project"
    (root / ".opencode").mkdir(parents=True)

    context = build_watch_context(root, {"new_items": []}, generated_at=ANCHOR)

    assert context["schema_version"] == 1
    assert context["project_root"] == "."
    assert set(context) == {
        "schema_version",
        "generated_at",
        "date",
        "project_root",
        "plugin_config",
        "plugins",
        "declared_plugins",
        "local_plugins",
        "skills",
        "commands",
        "agents",
        "counts",
        "market_matches",
        "warnings",
        "architecture_observations",
    }


def test_residual_branch_keeps_retained_and_unmatched_and_drops_blocked(
    tmp_path: Path,
) -> None:
    """Branche résiduelle : retenus + non-matchés, jamais les bloqués sécurité."""
    root, ecosystem = _crosswalk_project(tmp_path)
    candidates_file = _candidates_file(
        tmp_path,
        [_fiche("npm:alpha-pkg", "alpha")],
        blocked=[{"id": "npm:delta-pkg", "name": "delta", "reason": "typosquat:x"}],
    )

    ctx = build_watch_context(root, ecosystem, generated_at=ANCHOR, candidates_path=candidates_file)

    ids = {_mm_id(m) for m in ctx["market_matches"]}
    assert "npm:alpha-pkg" in ids  # retenu
    assert "url:gamma" in ids  # non apparié -> résiduel, conservé
    assert "npm:delta-pkg" not in ids  # bloqué sécurité
    legacy = build_watch_context(root, ecosystem, generated_at=ANCHOR)
    assert len(legacy["market_matches"]) == 4


def _bare_project(tmp_path: Path) -> Path:
    """Racine projet minimale : .opencode existe, aucun plugin déclaré."""
    root = tmp_path / "project"
    (root / ".opencode").mkdir(parents=True)
    return root
