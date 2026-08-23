"""Cibles de drafting mono-cible (cellule 2.1) — décision seule, zéro écriture.

Décision actée (brief §2.1) : UN harnais cible par projet, résolu par priorité
**override config > détection par marqueurs > défaut opencode** ; liste vide
``[]`` = mode legacy (toutes les cibles, comportement historique). La
détection lit les marqueurs du project_root (jamais de symlink, jamais
``~/.claude/*`` réel ici : ce module ne fait qu'arbitrer). La projection des
fichiers draft sur disque est la cellule 2.2, qui consommera
`DRAFT_HARNESS_TARGETS` + `resolve_draft_targets`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import DraftTargetsConfig

#: Identifiants de harnais alignés sur le registre des providers
# (`providers/base.py` + `PROVIDER_TYPE` des implementations).
HARNESS_CLAUDE_CODE = "claude-code"
HARNESS_OPENCODE = "opencode"
HARNESS_COPILOT_VSCODE = "copilot-vscode"
HARNESS_CODEX = "codex"

#: Priorité documentée en cas de marqueurs multiples (décision §2.1) :
#: claude > opencode > copilot > codex — le premier gagne.
DRAFT_TARGET_PRIORITY: tuple[str, ...] = (
    HARNESS_CLAUDE_CODE,
    HARNESS_OPENCODE,
    HARNESS_COPILOT_VSCODE,
    HARNESS_CODEX,
)

#: Harnais → chemins marqueurs relatifs au project_root (répertoires dont
#: l'existence désigne le harnais).
DRAFT_HARNESS_MARKERS: dict[str, tuple[str, ...]] = {
    HARNESS_CLAUDE_CODE: (".claude/",),
    HARNESS_OPENCODE: (".opencode/",),
    HARNESS_COPILOT_VSCODE: (".github/prompts/", ".github/skills/"),
    HARNESS_CODEX: (".agents/",),
}

#: Harnais → répertoires cibles de projection (structure exportée pour la 2.2 :
#: chaque skill draft y est projeté selon le harnais actif). Chemins relatifs
#: au project_root ; la création réelle reste à la charge de la 2.2.
DRAFT_HARNESS_TARGETS: dict[str, tuple[str, ...]] = {
    HARNESS_CLAUDE_CODE: (".claude/skills",),
    HARNESS_OPENCODE: (".opencode/skills",),
    HARNESS_COPILOT_VSCODE: (".github/prompts", ".github/skills"),
    HARNESS_CODEX: (".agents",),
}

#: Défaut quand aucun marqueur n'est trouvé (warning affiché par le doctor).
DEFAULT_DRAFT_HARNESS = HARNESS_OPENCODE

# Modes d'origine portés par `ResolvedDraftTarget.mode` (+ mode config "auto").
MODE_AUTO = "auto"
MODE_OVERRIDE = "override"
MODE_DETECTED = "detected"
MODE_DEFAULT = "default"
MODE_LEGACY = "legacy"

_MODE_LABELS: dict[str, str] = {
    MODE_OVERRIDE: "config",
    MODE_DETECTED: "détection",
    MODE_DEFAULT: "défaut",
    MODE_LEGACY: "legacy",
}


@dataclass(frozen=True, slots=True)
class ResolvedDraftTarget:
    """Résultat de la résolution mono-cible (consommé par le doctor et la 2.2).

    ``harnesses`` compte exactement un harnais sauf en mode legacy (toutes les
    cibles connues, ordre de priorité conservé).
    """

    #: Origine de la décision : MODE_* ("override" | "detected" | "default" | "legacy").
    mode: str
    #: Harnais cibles effectifs, dans l'ordre de priorité.
    harnesses: tuple[str, ...]
    #: Message d'avertissement si un défaut a dû être appliqué, sinon None.
    warning: str | None = None


def detect_draft_target(project_root: Path | str | None) -> str | None:
    """LE harnais détecté via marqueurs, priorité `DRAFT_TARGET_PRIORITY`.

    Renvoie None si project_root est None ou si aucun marqueur n'existe —
    None signifie "rien décidé" ; le repli par défaut est du ressort de
    `resolve_draft_targets` (qui porte le warning associé).
    """
    if project_root is None:
        return None
    root = Path(project_root)
    for harness in DRAFT_TARGET_PRIORITY:
        if any((root / marker).is_dir() for marker in DRAFT_HARNESS_MARKERS[harness]):
            return harness
    return None


def resolve_draft_targets(
    project_root: Path | str | None, draft_cfg: DraftTargetsConfig
) -> ResolvedDraftTarget:
    """Résolution effective : override config > détection > défaut opencode.

    Mode legacy (liste vide []) → toutes les cibles connues. Aucun marqueur →
    défaut `DEFAULT_DRAFT_HARNESS` avec warning explicite pour le doctor.
    """
    if draft_cfg.mode == MODE_LEGACY:
        return ResolvedDraftTarget(mode=MODE_LEGACY, harnesses=DRAFT_TARGET_PRIORITY)
    if draft_cfg.mode == MODE_OVERRIDE and draft_cfg.targets:
        return ResolvedDraftTarget(mode=MODE_OVERRIDE, harnesses=tuple(draft_cfg.targets))
    detected = detect_draft_target(project_root)
    if detected is not None:
        return ResolvedDraftTarget(mode=MODE_DETECTED, harnesses=(detected,))
    root_desc = str(project_root) if project_root is not None else "project_root manquant"
    return ResolvedDraftTarget(
        mode=MODE_DEFAULT,
        harnesses=(DEFAULT_DRAFT_HARNESS,),
        warning=(
            f"aucun marqueur de harnais trouvé ({root_desc}) — "
            f"défaut {DEFAULT_DRAFT_HARNESS} appliqué pour la projection des drafts"
        ),
    )


def describe_draft_target(resolved: ResolvedDraftTarget) -> str:
    """Ligne compacte d'affichage : ``<harnais> (<mode>)`` ou legacy étendu."""
    label = _MODE_LABELS.get(resolved.mode, resolved.mode)
    if resolved.mode == MODE_LEGACY:
        return f"toutes cibles ({label})"
    return f"{', '.join(resolved.harnesses)} ({label})"
