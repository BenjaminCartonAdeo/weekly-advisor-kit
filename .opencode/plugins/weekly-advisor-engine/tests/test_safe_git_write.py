"""Deterministic + safe git writes (Part 4 §7) — validate_draft & commit_draft."""

from __future__ import annotations

import subprocess
from pathlib import Path

from weekly_telemetry_aggregator.config import TelemetryConfig
from weekly_telemetry_aggregator.safe_git_write import (
    commit_draft,
    frontmatter_blocks,
    validate_draft,
)


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


VALID_SKILL = (
    "---\nname: my-skill\ndescription: Fait quelque chose d'utile\n---\n\nContenu du skill.\n"
)


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "advisor@local")
    _git(repo, "config", "user.name", "t")
    (repo / "base.txt").write_text("base", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    return repo


def test_frontmatter_blocks_parses(tmp_path: Path):
    p = tmp_path / "fake-skill.md"
    p.write_text(VALID_SKILL, encoding="utf-8")
    meta, body, err = frontmatter_blocks(p)
    assert err is None
    assert meta["name"] == "my-skill"
    assert meta["description"] == "Fait quelque chose d'utile"
    assert "Contenu" in body


def test_valid_skill_draft_passes(tmp_path: Path):
    skill = tmp_path / "my-skill" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(VALID_SKILL, encoding="utf-8")
    assert validate_draft(skill, "skill") == (True, "ok")


def test_name_mismatch_rejected(tmp_path: Path):
    skill = tmp_path / "other-dir" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(VALID_SKILL, encoding="utf-8")  # name: my-skill ≠ other-dir
    ok, msg = validate_draft(skill, "skill")
    assert ok is False
    assert "dossier" in msg


def test_missing_description_rejected(tmp_path: Path):
    p = tmp_path / "x.md"
    p.write_text("---\nname: x\n---\nbody", encoding="utf-8")
    ok, msg = validate_draft(p, "command")
    assert ok is False
    assert "description" in msg


def test_commit_draft_creates_commit(tmp_path: Path):
    repo = _init_repo(tmp_path)
    skill = repo / ".opencode" / "skills" / "my-skill" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(VALID_SKILL, encoding="utf-8")
    cfg = TelemetryConfig()
    cfg.git_name = "Advisor Test"
    ok, msg = commit_draft(cfg, skill, "skill")
    assert ok, msg
    log = _git(repo, "log", "-1", "--format=%s %an")
    assert "skill:my-skill" in log and "Advisor Test" in log  # v5.30 : nom = dossier, plus SKILL.md


def test_commit_draft_rejects_outside_opencode(tmp_path: Path):
    """v6.0.c : un draft hors .opencode/ (ex. commande globale, racine du repo) est refusé."""
    repo = _init_repo(tmp_path)
    root_draft = repo / "my-skill" / "SKILL.md"  # dans le repo mais PAS sous .opencode/
    root_draft.parent.mkdir()
    root_draft.write_text(VALID_SKILL, encoding="utf-8")
    ok, msg = commit_draft(TelemetryConfig(), root_draft, "skill")
    assert ok is False
    assert ".opencode" in msg
    # contre-preuve : le même dossier sous .opencode/ passe (couvert par creates_commit)
    assert _git(repo, "log", "-1", "--format=%s") == "base"  # aucun commit ajouté


def test_commit_draft_rejects_outside_repo(tmp_path: Path):
    outside = tmp_path / "no-git" / "SKILL.md"
    outside.parent.mkdir()
    outside.write_text(VALID_SKILL, encoding="utf-8")
    ok, msg = commit_draft(TelemetryConfig(), outside, "skill")
    assert ok is False
    assert "git" in msg


def test_commit_draft_rejects_invalid_frontmatter(tmp_path: Path):
    repo = _init_repo(tmp_path)
    bad = repo / "bad.md"
    bad.write_text("pas de frontmatter", encoding="utf-8")
    ok, msg = commit_draft(TelemetryConfig(), bad, "command")
    assert ok is False
    assert "commit" not in msg or "invalide" in msg


def test_commit_draft_rejects_detached_head(tmp_path: Path):
    repo = _init_repo(tmp_path)
    dash = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", dash)  # detached HEAD
    skill = repo / ".opencode" / "skills" / "det" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: det\ndescription: d\n---\nx", encoding="utf-8")
    ok, msg = commit_draft(TelemetryConfig(), skill, "skill")
    assert ok is False
    assert "détaché" in msg


def test_commit_body_includes_target_agents(tmp_path: Path):
    repo = _init_repo(tmp_path)
    skill = repo / ".opencode" / "skills" / "my-skill" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: my-skill\ndescription: un skill de test\nmetadata:\n  source_sessions:\n    - ses_abc\n  target_agents:\n    - java-pro\n    - backend-architect\n---\n# Corps\n",
        encoding="utf-8",
    )
    cfg = TelemetryConfig(git_name="Advisor Test", git_email="advisor@test")
    ok, msg = commit_draft(cfg, skill, "skill")
    assert ok, msg
    body = _git(repo, "log", "-1", "--format=%b")
    assert "Cible: agents java-pro, backend-architect" in body
    assert "Source: sessions ses_abc" in body


def test_commit_draft_agent_kind(tmp_path: Path):
    """P4 : les drafts agents (.opencode/agents/) se committent avec le préfixe agent:."""
    from weekly_telemetry_aggregator.config import TelemetryConfig

    repo = _init_repo(tmp_path)
    agent_dir = repo / ".opencode" / "agents" / "my-agent"
    agent_dir.mkdir(parents=True)
    draft = agent_dir / "my-agent.md"
    draft.write_text(
        "---\nname: my-agent\ndescription: Agent de test\n---\n\nCorps du draft.\n",
        encoding="utf-8",
    )
    cfg = TelemetryConfig()
    cfg.git_name = "advisor"
    cfg.git_email = "advisor@local"
    ok, msg = commit_draft(cfg, draft, "agent")
    assert ok, msg
    subject = _git(repo, "log", "-1", "--format=%s")
    assert subject.startswith("agent:my-agent")
    # le reste du worktree est intact (add scoped)
    assert _git(repo, "status", "--porcelain") == ""
