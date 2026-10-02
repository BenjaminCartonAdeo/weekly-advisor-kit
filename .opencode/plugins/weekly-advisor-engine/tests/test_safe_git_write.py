"""Deterministic + safe git writes (Part 4 §7) — validate_draft & commit_draft."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from weekly_telemetry_aggregator.config import TelemetryConfig
from weekly_telemetry_aggregator.safe_git_write import (
    COMMIT_DRAFT_RESULT_MARKER,
    commit_draft,
    commit_draft_detailed,
    frontmatter_blocks,
    validate_draft,
)


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


VALID_SKILL = (
    "---\nname: my-skill\ndescription: Fait quelque chose d'utile\nmetadata:\n  verification: none\n---\n\n"
    "## Quand utiliser\n\nContenu du skill.\n"
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


VALID_COMMAND = (
    "---\nname: x\ndescription: Fait x\nmetadata:\n  verification: none\n---\n\n"
    "## Procédure\n\nContenu de la commande.\n"
)


def test_commit_draft_rejects_foreign_repo_outside_project_root(tmp_path: Path):
    """Nested/foreign repo : project_root configuré ⇒ commit refusé hors de ce dépôt.

    Trou réel (run 2026-09-13) : un draft ciblant un AUTRE dépôt git (dont le
    chemin contient `.opencode/`) pouvait être committé dans ce dépôt étranger.
    """
    project = _init_repo(tmp_path)  # repo_A = projet configuré
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    _git(foreign, "init", "-q")
    _git(foreign, "config", "user.email", "advisor@local")
    _git(foreign, "config", "user.name", "t")
    (foreign / "base.txt").write_text("base", encoding="utf-8")
    _git(foreign, "add", ".")
    _git(foreign, "commit", "-qm", "base")
    draft = foreign / ".opencode" / "commands" / "x.md"
    draft.parent.mkdir(parents=True)
    draft.write_text(VALID_COMMAND, encoding="utf-8")

    cfg = TelemetryConfig(git_name="Advisor Test", git_email="advisor@test")
    cfg.project_root = project
    ok, msg = commit_draft(cfg, draft, "command")
    assert ok is False
    assert "projet" in msg
    # aucun commit ajouté dans le dépôt étranger
    assert _git(foreign, "log", "-1", "--format=%s") == "base"


def test_commit_draft_project_root_allows_in_project(tmp_path: Path):
    """Contre-preuve : project_root configuré et cible DANS ce dépôt ⇒ commit OK."""
    project = _init_repo(tmp_path)
    draft = project / ".opencode" / "commands" / "x.md"
    draft.parent.mkdir(parents=True)
    draft.write_text(VALID_COMMAND, encoding="utf-8")
    cfg = TelemetryConfig(git_name="Advisor Test", git_email="advisor@test")
    cfg.project_root = project
    ok, msg = commit_draft(cfg, draft, "command")
    assert ok, msg
    assert "command:x" in _git(project, "log", "-1", "--format=%s")


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
    skill.write_text(
        "---\nname: det\ndescription: d\nmetadata:\n  verification: none\n---\n\n## Procédure\n\nx",
        encoding="utf-8",
    )
    ok, msg = commit_draft(TelemetryConfig(), skill, "skill")
    assert ok is False
    assert "détaché" in msg


def test_commit_body_includes_target_agents(tmp_path: Path):
    repo = _init_repo(tmp_path)
    skill = repo / ".opencode" / "skills" / "my-skill" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: my-skill\ndescription: un skill de test\nmetadata:\n  verification: none\n  source_sessions:\n    - ses_abc\n  target_agents:\n    - java-pro\n    - backend-architect\n---\n## Corps\n",
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


def test_commit_draft_rejects_missing_verification_field(tmp_path: Path):
    """R6 gate: un draft sans metadata.verification est rejeté."""
    skill = tmp_path / "my-skill" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: my-skill\ndescription: d\n---\n\n## Section\n",
        encoding="utf-8",
    )
    ok, msg = validate_draft(skill, "skill")
    assert ok is False
    assert "metadata.verification" in msg


def test_commit_draft_rejects_missing_sections(tmp_path: Path):
    """R6 gate: un body sans heading ## est rejeté."""
    skill = tmp_path / "my-skill" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: my-skill\ndescription: d\nmetadata:\n  verification: none\n---\n\ncorps sans section\n",
        encoding="utf-8",
    )
    ok, msg = validate_draft(skill, "skill")
    assert ok is False
    assert "missing mandatory section" in msg


# ---------------------------------------------------------------------------
# v6.1 — faits structurés du commit (le SHA complet sans relire `.git/refs`)
# ---------------------------------------------------------------------------


def _valid_skill_in(repo: Path, name: str) -> Path:
    skill = repo / ".opencode" / "skills" / name / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text(
        f"---\nname: {name}\ndescription: Fait {name}\nmetadata:\n  verification: none\n---\n\n"
        "## Quand utiliser\n\nContenu.\n",
        encoding="utf-8",
    )
    return skill


def test_commit_draft_detailed_exposes_full_sha_branch_and_subject(tmp_path: Path):
    """Le SHA COMPLEUT est un fait du moteur, pas un `git rev-parse` à refaire."""
    repo = _init_repo(tmp_path)
    skill = _valid_skill_in(repo, "sha-complet")
    result = commit_draft_detailed(TelemetryConfig(), skill, "skill")

    assert result.ok is True, result.message
    assert result.sha == _git(repo, "rev-parse", "HEAD")
    assert re.fullmatch(r"[0-9a-f]{40}", result.sha)
    assert result.short_sha == result.sha[:10]
    assert result.branch == _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    assert result.subject == _git(repo, "log", "-1", "--format=%s")
    assert "auto-rédigé, revue hebdo" in result.subject


def test_commit_draft_message_keeps_short_sha_prose_verbatim(tmp_path: Path):
    """Format de prose figé : UNE ligne, préfixe gelé. Les faits vont à part."""
    repo = _init_repo(tmp_path)
    skill = _valid_skill_in(repo, "prose")
    result = commit_draft_detailed(TelemetryConfig(), skill, "skill")

    prose, _, facts = result.message.partition(f"\n{COMMIT_DRAFT_RESULT_MARKER}")
    assert "\n" not in prose
    assert prose.startswith("SKILL.md committé (HEAD ")
    assert prose == f"SKILL.md committé (HEAD {result.sha[:10]})"
    payload = json.loads(facts)
    assert payload == {
        "branch": result.branch,
        "file": str(skill),
        "kind": "skill",
        "sha": result.sha,
        "short_sha": result.short_sha,
        "subject": result.subject,
    }
    # `payload()` ne recopie jamais la prose — la ligne ne double pas de volume.
    assert "message" not in payload


def test_prose_head_is_a_real_short_sha_and_never_a_commit_header_fragment(tmp_path: Path):
    """Cause racine du détour du 2026-10-01, figée comme invariant (RÉGRESSION).

    `git commit` écrit `[branche sha-court] sujet` : tronquer sa première ligne à
    10 caractères affichait `[master 0d` — un fragment de décoration que l'agent
    a pris pour une troncature, avant d'aller lire `.git/refs/heads/…` à la main.
    La prose affiche désormais un VRAI SHA court, résolu par `_head_sha`.

    Ce test verrouille les deux moitiés du contrat : un SHA hexadécimal de 7 à 10
    caractères, et AUCUN `[` (le caractère signature du header porcelain). Sans
    lui, le bug revient : aucun test ne l'attrapait.
    """
    repo = _init_repo(tmp_path)
    skill = _valid_skill_in(repo, "cause-racine")
    result = commit_draft_detailed(TelemetryConfig(), skill, "skill")

    prose = result.message.split("\n", 1)[0]
    head_token = prose.split("(HEAD ", 1)[1].rstrip(")")
    assert re.fullmatch(r"[0-9a-f]{7,10}", head_token), (
        f"la prose doit afficher un vrai SHA court, pas {head_token!r} : "
        "reviens à `_head_sha(root)[:10]`, jamais à une découpe de `commit.stdout`"
    )
    assert "[" not in prose, (
        f"`[` = header porcelain `[branche sha]`, marqueur du bug du 2026-10-01 : {prose!r}"
    )
    # et il s'agit bien du SHA du commit que l'agent vient de faire
    assert result.sha == _git(repo, "rev-parse", "HEAD")
    assert len(result.sha) == 40
    assert head_token == result.sha[: len(head_token)]


def test_kit_sync_prose_head_is_a_real_short_sha_not_a_header_fragment(tmp_path: Path):
    """Même correction sur le miroir kit — sans quoi l'agent relit `.git/refs` aussi.

    Le miroir commit dans le dépôt du KIT, donc le SHA affiché doit venir de
    `_head_sha(kit)`. Régression si quelqu'un réintroduit la découpe, ou pire, si
    on affiche par erreur le HEAD du dépôt *projet* (autre dépôt, autre SHA).
    """
    project = _init_repo(tmp_path)
    kit_root = tmp_path / "kit-workspace"
    kit_root.mkdir()
    kit = _init_repo(kit_root)
    skill = _valid_skill_in(project, "miroir")
    target = kit / ".opencode" / "skills" / "miroir" / "SKILL.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("contenu kit divergent\n", encoding="utf-8")
    _git(kit, "add", ".")
    _git(kit, "commit", "-qm", "base kit")

    result = commit_draft_detailed(TelemetryConfig(kit_root=kit), skill, "skill")

    assert result.ok is True, result.message
    # la prose seule, ligne de faits mise de côté
    prose = result.message.split(f"\n{COMMIT_DRAFT_RESULT_MARKER}", 1)[0]
    sync_note = prose.split(" ; ", 1)[1]
    head_token = sync_note.split("(HEAD ", 1)[1].rstrip(")")
    assert re.fullmatch(r"[0-9a-f]{7,10}", head_token), f"SHA invalide: {head_token!r}"
    assert "[" not in sync_note
    # le SHA est celui du KIT (le miroir), pas celui du projet
    assert head_token == _git(kit, "rev-parse", "HEAD")[: len(head_token)]
    assert head_token != _git(project, "rev-parse", "HEAD")[: len(head_token)]


def test_commit_draft_tuple_contract_unchanged_for_the_cli(tmp_path: Path):
    """`commit_draft` reste (ok, message) : `cli.py` n'a pas bougé d'une ligne."""
    repo = _init_repo(tmp_path)
    skill = _valid_skill_in(repo, "contrat")
    ok, msg = commit_draft(TelemetryConfig(), skill, "skill")
    assert ok is True
    assert msg.splitlines()[0] == f"SKILL.md committé (HEAD {_git(repo, 'rev-parse', 'HEAD')[:10]})"
    assert msg.splitlines()[1].startswith(COMMIT_DRAFT_RESULT_MARKER)


def test_commit_draft_detailed_success_line_carries_parsable_json_facts(tmp_path: Path):
    """Le structured return doit être PARSABLE bout-en-bout, pas seulement inerte.

    Boucle fermée sur v6.1 : la prose vient d'être corrigée pour afficher un vrai
    SHA court, donc les deux ne doivent plus jamais se confondre. On rejoue le
    contrat du handler TypeScript — trouver le marqueur, lire SA ligne, parser le
    JSON, récupérer le SHA complet sans relire `.git/refs`.
    """
    repo = _init_repo(tmp_path)
    skill = _valid_skill_in(repo, "parsing")
    ok, msg = commit_draft(TelemetryConfig(), skill, "skill")

    assert ok is True
    # 1. le marqueur est présent, une fois, sur une ligne à lui
    assert msg.count(COMMIT_DRAFT_RESULT_MARKER) == 1
    lines = msg.splitlines()
    marker_idx = next(i for i, ln in enumerate(lines) if ln.startswith(COMMIT_DRAFT_RESULT_MARKER))
    prose, facts_line = lines[:marker_idx], lines[marker_idx]

    # 2. la prose reste une SEULE ligne, distincte des faits, sans le marqueur
    assert len(prose) == 1
    assert COMMIT_DRAFT_RESULT_MARKER not in prose[0]
    # après correction : plus de `[master 0d` dans la prose
    assert "[" not in prose[0]

    # 3. la ligne de faits se parse et porte tous les champs attendus
    payload = json.loads(facts_line[len(COMMIT_DRAFT_RESULT_MARKER) :])
    assert set(payload) == {"kind", "file", "sha", "short_sha", "branch", "subject"}
    assert re.fullmatch(r"[0-9a-f]{40}", payload["sha"])
    assert payload["short_sha"] == payload["sha"][:10]
    assert payload["branch"] == _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    assert payload["file"] == str(skill)
    assert "auto-rédigé, revue hebdo" in payload["subject"]

    # 4. le SHA parsé est bien celui du commit, sans lecture de `.git/refs`
    assert payload["sha"] == _git(repo, "rev-parse", "HEAD")


def test_commit_draft_refusal_carries_no_facts(tmp_path: Path):
    """Pas de commit ⇒ pas de ligne payload : le rapport ne peut pas le compter."""
    outside = tmp_path / "no-git" / "SKILL.md"
    outside.parent.mkdir(parents=True)
    outside.write_text(VALID_SKILL, encoding="utf-8")

    result = commit_draft_detailed(TelemetryConfig(), outside, "skill")
    assert result.ok is False
    assert result.sha is None and result.branch is None and result.subject is None
    assert result.short_sha is None
    assert COMMIT_DRAFT_RESULT_MARKER not in result.message
    # et le refus ne « pollue » pas l'artefact timings : rien à parser du tout
    assert commit_draft(TelemetryConfig(), outside, "skill")[0] is False


def test_commit_draft_payload_absent_when_subject_marker_missing(tmp_path: Path):
    """Un `kind` inconnu est un refus de pref-check : toujours aucun fait."""
    repo = _init_repo(tmp_path)
    skill = _valid_skill_in(repo, "kind-inconnu")
    result = commit_draft_detailed(TelemetryConfig(), skill, "kind-bidon")
    assert result.ok is False
    assert "kind inconnu" in result.message
    assert COMMIT_DRAFT_RESULT_MARKER not in result.message
