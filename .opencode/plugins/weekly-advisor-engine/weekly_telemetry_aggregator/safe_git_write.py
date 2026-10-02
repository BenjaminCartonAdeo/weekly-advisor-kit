"""Secure git writes for auto-drafted skills/commands (Partie 4 §7, v5.23).

Exposed via the CLI sub-command `commit-draft`; reused in v2 by Partie 6 R4
(shared module, zero duplication). Every step lives in code: frontmatter
validation, pre-checks, scoped `git add`, injected identity, built message —
the agent never types a raw git command.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from .config import TelemetryConfig

_MESSAGE_PREFIX = {
    "skill": "skill:",
    "command": "command:",
    "fix": "harness-fix:",
    "agent": "agent:",  # v6.0.n : drafts .opencode/agents/ (java-pro.md, ...)
}
_SKILL_ORIGINS = frozenset({"user", "bundled", "weekly-foreground", "weekly-background"})
_SKILL_TTL_POLICIES = frozenset({"decay", "pin", "null", "none", ""})

# Préfixe de la ligne de faits du retour `commit-draft` (v6.1). Une ligne, un
# préfixe stable : le handler TypeScript la retrouve sans connaître le moteur et
# l'agent n'a plus à deviner le SHA complet — surtout à relire `.git/refs` à la
# main (détour observé au run 2026-10-01). La prose reste la ligne du dessus :
# elle affiche le SHA COURT donné par `_head_sha`, la ligne de faits porte le SHA
# COMPLET. Les deux ne doivent jamais se confondre (cf. `with_payload_line`).
COMMIT_DRAFT_RESULT_MARKER = "commit-draft-result: "


@dataclass(frozen=True)
class DraftCommitResult:
    """Ce que le moteur sait d'un `commit-draft` : la prose ET les faits.

    `message` est la prose historique, gelée — lisible telle quelle, aucune
    assertion existante n'en dépend. `sha` est le SHA **COMPLET** du commit :
    c'est lui que le rapport (`_recorded_draft_commits`) et l'artefact
    `weekly-timings-<date>.json` consomment. `subject` porte le marqueur
    `auto-rédigé, revue hebdo` que le rapport exige pour reconnaître un draft.
    """

    ok: bool
    kind: str
    file: str
    message: str
    sha: str | None = None
    branch: str | None = None
    subject: str | None = None

    @property
    def short_sha(self) -> str | None:
        """Abrégé du SHA complet, 10 hex — jamais recollé à la prose."""
        return self.sha[:10] if self.sha else None

    def payload(self) -> dict[str, str | None]:
        """Faits structurés. Volontairement SANS `message` : la prose est déjà
        dans le retour, la recopier ici ne ferait que doubler la ligne."""
        return {
            "kind": self.kind,
            "file": self.file,
            "sha": self.sha,
            "short_sha": self.short_sha,
            "branch": self.branch,
            "subject": self.subject,
        }

    def with_payload_line(self) -> DraftCommitResult:
        """Copie dont `message` porte les faits sur une SECONDE ligne marquée.

        Réservé au succès : un refus n'a produit aucun commit, donc aucun fait à
        enregistrer — et le rapport ne doit jamais pouvoir le compter.
        """
        line = COMMIT_DRAFT_RESULT_MARKER + json.dumps(
            self.payload(), ensure_ascii=False, sort_keys=True
        )
        return replace(self, message=f"{self.message}\n{line}")


def _run_git(cwd: Path, *args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
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


def validate_skill_source(
    path: Path,
) -> tuple[bool, dict[str, str | None], str]:
    """Re-read and validate a source ``SKILL.md`` before a curation move.

    This is deliberately stricter than the report/catalogue parser.  A catalogue
    can be stale or incomplete, while an apply operation must prove that the
    file currently on disk is a real, verified skill and still carries its
    protection metadata.  ``frontmatter_blocks`` flattens the tiny YAML subset
    used by the kit, so both flattened and explicit ``metadata.*`` spellings are
    accepted here.
    """

    meta, _body, error = frontmatter_blocks(path)
    origin = (meta.get("origin") or "").strip().lower()
    ttl_policy_raw = (meta.get("ttl_policy") or "").strip().lower()
    ttl_policy = None if ttl_policy_raw in {"", "none", "null"} else ttl_policy_raw
    metadata = {
        "origin": origin or None,
        "ttl_policy": ttl_policy,
    }
    if error:
        return False, metadata, f"source SKILL.md malformed: {error}"
    try:
        regular = path.name == "SKILL.md" and path.is_file()
    except OSError as exc:
        return False, metadata, f"source SKILL.md unreadable: {exc}"
    if not regular:
        return False, metadata, "source SKILL.md absent or not a regular file"
    if not (meta.get("name") or "").strip():
        return False, metadata, "source SKILL.md malformed: name absent"
    if meta.get("name") != path.parent.name:
        return False, metadata, "source SKILL.md malformed: name does not match directory"
    if origin not in _SKILL_ORIGINS:
        return False, metadata, "source SKILL.md unverified: origin absent or invalid"
    if ttl_policy_raw not in _SKILL_TTL_POLICIES:
        return False, metadata, "source SKILL.md malformed: ttl_policy invalid"
    if not (meta.get("description") or "").strip():
        return False, metadata, "source SKILL.md malformed: description absent"
    verification = (meta.get("verification") or "").strip()
    metadata["verification"] = verification or None
    if not verification or verification.casefold() in {"none", "null", "unverified"}:
        return False, metadata, "source SKILL.md unverified: metadata.verification absent"
    return True, metadata, "source SKILL.md verified"


def draft_name(path: Path, kind: str) -> str:
    """Nom logique du draft pour le message de commit (v5.30, C).

    Skill → nom du dossier (`.opencode/skills/<name>/SKILL.md` → `<name>`) ;
    command/fix → stem du fichier (`ts-check.md` → `ts-check`).
    """
    if kind == "skill":
        return path.parent.name
    return path.stem


def validate_draft(path: Path, kind: str) -> tuple[bool, str]:
    """Deterministic post-edit guard (v5.21 + R6): frontmatter, name==dir (skills),
    non-empty description, mandatory metadata.verification + ## section (skill/command)."""
    meta, body, err = frontmatter_blocks(path)
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
    if kind in ("skill", "command"):
        if not (meta.get("verification") or "").strip():
            return False, "missing metadata.verification"
        if not re.search(r"(?m)^##\s", body):
            return False, "missing mandatory section"
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
    return True, f"synchro kit {rel} (HEAD {(_head_sha(kit) or '')[:10]})"


def _commit_draft_root(
    cfg: TelemetryConfig, file_path: Path, kind: str
) -> tuple[Path | None, str | None, str | None]:
    """Garde-fous pré-commit : (root, None, branch) si OK, (None, message, None) si refus.

    `branch` est résolu ici — le commit va l'écrire dans l'en-tête, autant le
    garder plutôt que de relancer `git rev-parse` après coup.
    """
    if kind not in _MESSAGE_PREFIX:
        return None, f"kind inconnu: {kind}", None
    ok, msg = validate_draft(file_path, kind)
    if not ok:
        return None, f"draft invalide — pas de commit: {msg}", None
    root = _repo_root(file_path)
    if root is None:
        return None, "fichier hors dépôt git — pas de commit", None
    ok, msg = _ensure_opencode_scope(root, file_path)
    if not ok:
        return None, f"{msg} — pas de commit", None
    if cfg.project_root is not None and root.resolve() != cfg.project_root.resolve():
        return None, f"cible hors du projet configuré ({cfg.project_root}) — pas de commit", None
    branch = _run_git(root, "rev-parse", "--abbrev-ref", "HEAD")
    if branch.returncode != 0 or branch.stdout.strip() == "HEAD":
        return None, "HEAD détaché — pas de commit auto", None
    for marker in ("rebase-merge", "rebase-apply", "MERGE_HEAD"):
        if (root / ".git" / marker).exists():
            return (
                None,
                f"{marker} détecté — rebase/merge en cours, fichier écrit non commité",
                None,
            )
    return root, None, branch.stdout.strip()


def _draft_commit_message(file_path: Path, kind: str, meta: dict) -> tuple[str, str]:
    """Sujet + corps du commit draft (corps R4 pour kind == fix)."""
    prefix = _MESSAGE_PREFIX[kind]
    date = datetime.now(UTC).strftime("%Y-%m-%d")
    name = draft_name(file_path, kind)
    subject = f"{prefix}{name} (auto-rédigé, revue hebdo {date})"
    if kind == "fix":
        return subject, "Violation harness corrigée (triviale, R4)."
    body = (
        f"Source: sessions {_list_field(meta.get('source_sessions', 'n/a'))}\n"
        f"Chevauchement détecté: {_list_field(meta.get('overlaps_with', 'aucun'))}\n"
        f"Cible: agents {_list_field(meta.get('target_agents', 'aucune'))}"
    )
    return subject, body


def _refused(kind: str, file_path: Path, message: str) -> DraftCommitResult:
    """Refus : prose seule, aucun fait — aucun commit n'a eu lieu."""
    return DraftCommitResult(ok=False, kind=kind, file=str(file_path), message=message)


def _head_sha(root: Path) -> str | None:
    """SHA COMPLET de HEAD, tel que git le connaît — jamais une découpe de sortie.

    `git commit` écrit `[branche sha-court] sujet` sur sa première ligne : la
    tronquer à 10 caractères donne `[master be`, pas un SHA. C'est exactement ce
    que l'agent du run 2026-10-01 a pris pour une troncature, avant d'aller lire
    `.git/refs/heads/…` à la main pour récupérer les vrais SHA. Elle sert
    désormais aux DEUX sorties : la prose (tronquée à 10) comme la ligne de faits
    (complète). Un seul point de vérité, aucun chemin ne peut réintroduire la
    découpe.
    """
    proc = _run_git(root, "rev-parse", "HEAD")
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def commit_draft_detailed(cfg: TelemetryConfig, file_path: Path, kind: str) -> DraftCommitResult:
    """Validate + pre-checks + scoped add + commit. Retour structuré (v6.1)."""
    root, fail, branch = _commit_draft_root(cfg, file_path, kind)
    if root is None:
        return _refused(kind, file_path, fail or "refus pré-commit")

    add = _run_git(root, "add", "--", str(file_path))
    if add.returncode != 0:
        return _refused(kind, file_path, f"git add échoué: {add.stderr.strip()}")

    meta, _body, _err = frontmatter_blocks(file_path)
    subject, body = _draft_commit_message(file_path, kind, meta)
    commit = _run_git(
        root,
        "-c",
        f"user.name={cfg.git_name}",
        "-c",
        f"user.email={cfg.git_email}",
        "commit",
        "--no-edit",
        "-m",
        subject,
        "-m",
        body,
        "--",
        str(file_path),
    )
    if commit.returncode != 0:
        return _refused(kind, file_path, f"commit échoué: {commit.stderr.strip()}")
    sync_ok, sync_note = _sync_kit_draft(cfg, root, file_path)
    # Le SHA affiché vient de `_head_sha` (source de vérité = git), JAMAIS d'une
    # découpe de la sortie de `git commit` : sa première ligne est l'en-tête
    # `[branche sha-court] sujet`, dont les 10 premiers caractères sont
    # `[master 0d` — un fragment de décoration affiché à l'humain, pas un SHA.
    # C'est ce faux jeton qui a envoyé l'agent du run 2026-10-01 vers `.git/refs`.
    msg = f"{file_path.name} committé (HEAD {(_head_sha(root) or '')[:10]})"
    if sync_note:
        msg += f" ; {sync_note}"
    return DraftCommitResult(
        ok=True,
        kind=kind,
        file=str(file_path),
        message=msg,
        sha=_head_sha(root),
        branch=branch,
        subject=subject,
    ).with_payload_line()


def commit_draft(cfg: TelemetryConfig, file_path: Path, kind: str) -> tuple[bool, str]:
    """Validate + pre-checks + scoped add + commit. Returns (ok, message).

    Contrat historique conservé tel quel pour le CLI : la prose ne change pas.
    Un succès porte en plus, sur une seconde ligne, les faits du commit
    (`COMMIT_DRAFT_RESULT_MARKER`) — lisibles sans changer le mode d'emploi.
    """
    result = commit_draft_detailed(cfg, file_path, kind)
    return result.ok, result.message


def safe_git_move(src: Path, dst: Path) -> tuple[str, str | None]:
    """Déplace ``src`` vers ``dst`` de façon idempotente et SANS suppression.

    Garanties (WAVE 2.5, apply) :
    - Si ``dst`` existe déjà (fichier ou répertoire) -> ``("exists", None)`` :
      aucun mouvement (idempotent, le skill est déjà archivé).
    - Sinon : tente ``git mv`` (préserve l'historique quand ``src`` est suivi) ;
      en échec (non versionné, répertoire hors git) repli sur ``shutil.move``.
    - Jamais de ``rm`` / suppression. Toute erreur OSError est capturée et
      renvoyée comme ``("error", message)`` (aucun crash).

    Retour : ``(status, detail)`` avec ``status`` ∈ {moved, exists, missing, error}.
    """
    src = Path(src)
    dst = Path(dst)
    if not src.exists():
        return "missing", f"{src} introuvable"
    if dst.exists():
        return "exists", f"{dst} déjà présent (idempotent)"
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        # ``git mv`` is intentionally attempted only when the source and
        # destination belong to the same repository.  Passing absolute paths
        # from unrelated directories to git can otherwise produce misleading
        # failures before the safe shutil fallback.
        src_root = _repo_root(src)
        dst_root = _repo_root(dst.parent)
        if src_root is not None and src_root == dst_root:
            mv = _run_git(src_root, "mv", "--", str(src), str(dst))
            if mv.returncode == 0:
                return "moved", str(dst)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        shutil.move(str(src), str(dst))
        return "moved", str(dst)
    except (OSError, shutil.Error) as exc:
        return "error", str(exc)
