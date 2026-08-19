"""Secure git writes for auto-drafted skills/commands (Partie 4 §7, v5.23).

Exposed via the CLI sub-command `commit-draft`; reused in v2 by Partie 6 R4
(shared module, zero duplication). Every step lives in code: frontmatter
validation, pre-checks, scoped `git add`, injected identity, built message —
the agent never types a raw git command.
"""

from __future__ import annotations

import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from .config import TelemetryConfig

_MESSAGE_PREFIX = {"skill": "skill:", "command": "command:", "fix": "harness-fix:"}


def _run_git(cwd: Path, *args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=timeout
    )


def _list_field(value: str) -> str:
    """'[a, b]' or 'n/a' -ish → comma-separated tokens."""
    cleaned = re.sub(r"[\[\]\"']", "", str(value)).strip()
    if not cleaned or cleaned.lower() in {"n/a", "aucun", "none"}:
        return "n/a"
    return ", ".join(x.strip() for x in cleaned.split(",") if x.strip())


def frontmatter_blocks(path: Path) -> tuple[dict, str, str | None]:
    """Parse `---`-delimited frontmatter with stdlib (no PyYAML). Returns (meta, body, error)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return {}, "", f"lecture impossible: {exc}"
    if not text.startswith("---"):
        return {}, "", "frontmatter manquant (le fichier doit commencer par ---)"
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, "", "frontmatter invalide (deux délimiteurs --- requis)"
    meta: dict[str, str] = {}
    last_key: str | None = None
    for line in parts[1].splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("- "):  # liste YAML -> accumulée sur la clé précédente
            item = stripped[2:].strip().strip('"')
            if last_key and item:
                meta[last_key] = (meta.get(last_key, "") + ", " + item).strip(", ")
            continue
        if ":" in stripped:
            key, _, value = stripped.partition(":")
            key = key.strip()
            meta[key] = value.strip().strip('"')
            last_key = key
    return meta, parts[2], None


def draft_name(path: Path, kind: str) -> str:
    """Nom logique du draft pour le message de commit (v5.30, C).

    Skill → nom du dossier (`.opencode/skills/<name>/SKILL.md` → `<name>`) ;
    command/fix → stem du fichier (`ts-check.md` → `ts-check`).
    """
    if kind == "skill":
        return path.parent.name
    return path.stem


def validate_draft(path: Path, kind: str) -> tuple[bool, str]:
    """Deterministic post-edit guard (v5.21): frontmatter, name==dir (skills), non-empty description."""
    meta, _body, err = frontmatter_blocks(path)
    if err:
        return False, err
    if not (meta.get("description") or "").strip():
        return False, "description vide dans le frontmatter"
    if kind == "skill":
        name = meta.get("name", "")
        if not name.strip():
            return False, "name absent dans le frontmatter"
        if name != path.parent.name:
            return False, f"name ({name}) ≠ nom du dossier ({path.parent.name})"
    return True, "ok"


def _repo_root(file_path: Path) -> Path | None:
    proc = _run_git(file_path.parent, "rev-parse", "--show-toplevel")
    if proc.returncode != 0:
        return None
    return Path(proc.stdout.strip())


def _ensure_opencode_scope(root: Path, file_path: Path) -> tuple[bool, str]:
    """v6.0.c : les drafts vivent uniquement sous `.opencode/` du dépôt.

    Un fichier hors worktree (ex. commande globale `~/.config/opencode/commands/`)
    ou à la racine du projet est refusé — même s'il est dans un repo git.
    """
    try:
        rel = file_path.resolve().relative_to(root.resolve())
    except ValueError:
        return False, f"{file_path} hors du dépôt git"
    top = rel.parts[0] if rel.parts else ""
    if top != ".opencode":
        return False, f"fichier hors .opencode/ du projet ({rel})"
    return True, "ok"


def _sync_kit_draft(cfg: TelemetryConfig, root: Path, file_path: Path) -> tuple[bool, str]:
    """Miroir best-effort d'un draft commité vers le worktree du kit (v6.0.l, E5).

    La distribution (``cfg.kit_root``) ne doit pas diverger silencieusement des
    drafts auto-rédigés. Règles : fichier présent **et tracké** dans le kit,
    contenu différent, pas de merge/rebase en cours. Un échec de synchro ne
    fait jamais échouer le commit du projet — le motif revient à l'agent.
    """
    kit = cfg.kit_root
    if kit is None:
        return False, ""
    try:
        rel = file_path.resolve().relative_to(root.resolve())
    except ValueError:
        return False, "draft hors worktree — sync kit impossible"
    target = kit / rel
    if not target.is_file():
        return False, f"{rel} absent du kit — sync ignoré"
    tracked = _run_git(kit, "ls-files", "--error-unmatch", "--", str(rel))
    if tracked.returncode != 0:
        return False, f"{rel} non suivi par le kit — sync ignoré"
    if target.read_bytes() == file_path.read_bytes():
        return False, f"{rel} déjà synchronisé avec le kit"
    for marker in ("rebase-merge", "rebase-apply", "MERGE_HEAD"):
        if (kit / ".git" / marker).exists():
            return False, f"{marker} dans le kit — sync reporté"
    try:
        target.write_bytes(file_path.read_bytes())
    except OSError as exc:
        return False, f"écriture kit impossible: {exc}"
    add = _run_git(kit, "add", "--", str(rel))
    if add.returncode != 0:
        return False, f"git add kit échoué: {add.stderr.strip()}"
    commit = _run_git(
        kit,
        "-c",
        f"user.name={cfg.git_name}",
        "-c",
        f"user.email={cfg.git_email}",
        "commit",
        "--no-edit",
        "-m",
        f"sync(weekly-advisor): {file_path.name} (auto-rédigé, revue hebdo {datetime.now(UTC).strftime('%Y-%m-%d')})",
        "--",
        str(rel),
    )
    if commit.returncode != 0:
        return False, f"commit kit échoué: {commit.stderr.strip()}"
    return True, f"synchro kit {rel} (HEAD {commit.stdout.strip().splitlines()[0][:10]})"


def commit_draft(cfg: TelemetryConfig, file_path: Path, kind: str) -> tuple[bool, str]:
    """Validate + pre-checks + scoped add + commit. Returns (ok, message)."""
    if kind not in _MESSAGE_PREFIX:
        return False, f"kind inconnu: {kind}"

    ok, msg = validate_draft(file_path, kind)
    if not ok:
        return False, f"draft invalide — pas de commit: {msg}"

    root = _repo_root(file_path)
    if root is None:
        return False, "fichier hors dépôt git — pas de commit"

    ok, msg = _ensure_opencode_scope(root, file_path)
    if not ok:
        return False, f"{msg} — pas de commit"

    branch = _run_git(root, "rev-parse", "--abbrev-ref", "HEAD")
    if branch.returncode != 0 or branch.stdout.strip() == "HEAD":
        return False, "HEAD détaché — pas de commit auto"

    for marker in ("rebase-merge", "rebase-apply", "MERGE_HEAD"):
        if (root / ".git" / marker).exists():
            return False, f"{marker} détecté — rebase/merge en cours, fichier écrit non commité"

    add = _run_git(root, "add", "--", str(file_path))
    if add.returncode != 0:
        return False, f"git add échoué: {add.stderr.strip()}"

    prefix = _MESSAGE_PREFIX[kind]
    date = datetime.now(UTC).strftime("%Y-%m-%d")
    meta, _body, _err = frontmatter_blocks(file_path)
    name = draft_name(file_path, kind)
    commit = _run_git(
        root,
        "-c",
        f"user.name={cfg.git_name}",
        "-c",
        f"user.email={cfg.git_email}",
        "commit",
        "--no-edit",
        "-m",
        f"{prefix}{name} (auto-rédigé, revue hebdo {date})",
        "-m",
        (
            f"Source: sessions {_list_field(meta.get('source_sessions', 'n/a'))}\n"
            f"Chevauchement détecté: {_list_field(meta.get('overlaps_with', 'aucun'))}\n"
            f"Cible: agents {_list_field(meta.get('target_agents', 'aucune'))}"
            if kind != "fix"
            else "Violation harness corrigée (triviale, R4)."
        ),
        "--",
        str(file_path),
    )
    if commit.returncode != 0:
        return False, f"commit échoué: {commit.stderr.strip()}"
    sync_ok, sync_note = _sync_kit_draft(cfg, root, file_path)
    msg = f"{file_path.name} committé (HEAD {commit.stdout.strip().splitlines()[0][:10]})"
    if sync_note:
        msg += f" ; {sync_note}"
    return True, msg
