"""Cibles de drafting mono-cible (cellule 2.1) — décision seule, zéro écriture.

Décision actée (brief §2.1) : UN harnais cible par projet, résolu par priorité
**override config > défaut opencode** ; liste vide ``[]`` = mode legacy (toutes
les cibles, comportement historique). Les marqueurs du project_root
(`DRAFT_HARNESS_MARKERS`) ne décident plus rien — ils sont purement informatifs
(D3 : la surface auditée est déclarée par la config, jamais inférée) et ne
produisent qu'un **warning** (`marker_warnings`, A3) qui n'altère ni `mode` ni
`harnesses`. La surface projetée est décrite **une seule fois** par
`DRAFT_HARNESS_LAYOUTS` (A1, 3 catégories skills/commands/agents) ; la vue «
skills » `DRAFT_HARNESS_TARGETS` en est une projection, comme
`DRAFT_HARNESS_COMMAND_TARGETS` pour les commands.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class DraftTargetsLike(Protocol):
    """Structure minimale requise (mode + targets) — évite le cycle statique vers config."""

    mode: str
    targets: list[str] | tuple[str, ...]


#: Identifiants de harnais alignés sur le registre des providers
# (`providers/base.py` + `PROVIDER_TYPE` des implementations).
HARNESS_CLAUDE_CODE = "claude-code"
HARNESS_OPENCODE = "opencode"
HARNESS_COPILOT_CLI = "copilot-cli"
HARNESS_CODEX = "codex"

#: Ordre canonique des harnais (décision §2.1) : claude > opencode > copilot >
#: codex. Depuis A2 il ne tranche plus rien par les marqueurs — c'est l'ordre du
#: mode legacy, l'ordre d'itération de `marker_warnings` et celui des warnings.
DRAFT_TARGET_PRIORITY: tuple[str, ...] = (
    HARNESS_CLAUDE_CODE,
    HARNESS_OPENCODE,
    HARNESS_COPILOT_CLI,
    HARNESS_CODEX,
)

# ---- A1 : table unique harnais → {skills, commands, agents} --------------------

#: Catégories de destination d'un draft. Les trois existent pour **tout** harnais
#: (champ vide `()` = aucune destination connue) : la connaissance « où vit un
#: draft » n'est écrite qu'une fois, dans `DRAFT_HARNESS_LAYOUTS`.
HARNESS_CATEGORY_SKILLS = "skills"
HARNESS_CATEGORY_COMMANDS = "commands"
HARNESS_CATEGORY_AGENTS = "agents"

HARNESS_CATEGORIES: tuple[str, ...] = (
    HARNESS_CATEGORY_SKILLS,
    HARNESS_CATEGORY_COMMANDS,
    HARNESS_CATEGORY_AGENTS,
)


@dataclass(frozen=True, slots=True)
class HarnessLayout:
    """Destinations de projection d'un harnais (chemins relatifs au project_root).

    ``skills`` est la seule catégorie projetée par la cellule 2.2 — les racines
    ``commands``/``agents`` en sont dérivées, jamais fusionnées avec elle (une
    fusion ferait porter les skills engine dans `.opencode/commands/`).
    """

    skills: tuple[str, ...]
    commands: tuple[str, ...] = ()
    agents: tuple[str, ...] = ()


#: **Source unique** de la surface par harnais (A1). Les 3 catégories sont
#: déclarées pour les 4 harnais, y compris quand la destination est inconnue —
#: une case vide vaut « rien à projeter », pas « table incomplète ».
DRAFT_HARNESS_LAYOUTS: dict[str, HarnessLayout] = {
    HARNESS_CLAUDE_CODE: HarnessLayout(
        skills=(".claude/skills",),
        commands=(".claude/commands",),
        agents=(".claude/agents",),
    ),
    HARNESS_OPENCODE: HarnessLayout(
        skills=(".opencode/skills",),
        commands=(".opencode/commands",),
        agents=(".opencode/agents",),
    ),
    HARNESS_COPILOT_CLI: HarnessLayout(
        # chez copilot, prompts est la destination commands ET une racine skills
        skills=(".github/prompts", ".github/skills"),
        commands=(".github/prompts",),
        agents=(".github/agents",),
    ),
    HARNESS_CODEX: HarnessLayout(
        skills=(".agents",),
        commands=(".agents/commands",),
        # codex n'expose pas de profil d'agent : `.agents/` est déjà la racine skills
        agents=(),
    ),
}

#: Vue « skills » dérivée de la table unique — surface de projection réellement
#: consommée par `harness_extra_roots` et `inject_engine_content`. Projection,
#: pas deuxième autorité : toute valeur ajoutée ici est une projection.
DRAFT_HARNESS_TARGETS: dict[str, tuple[str, ...]] = {
    harness: layout.skills for harness, layout in DRAFT_HARNESS_LAYOUTS.items()
}

#: Racine skills → destination commands, dérivée de la table unique
#: (ex-`ENGINE_COMMAND_TARGETS` codé en dur dans `harness_scope` — parité
#: identique, une seule autorité). Racine sans catégorie `commands` → rien.
DRAFT_HARNESS_COMMAND_TARGETS: dict[str, str] = {
    skills_root: layout.commands[0]
    for layout in DRAFT_HARNESS_LAYOUTS.values()
    if layout.commands
    for skills_root in layout.skills
}

#: Harnais → chemins marqueurs relatifs au project_root (répertoires dont
#: l'existence désigne le harnais). **Non décisionnels** depuis A2/A3 : purs
#: signaux d'alerte, lus par `marker_warnings`.
DRAFT_HARNESS_MARKERS: dict[str, tuple[str, ...]] = {
    HARNESS_CLAUDE_CODE: (".claude/",),
    HARNESS_OPENCODE: (".opencode/",),
    HARNESS_COPILOT_CLI: (".github/prompts/", ".github/skills/"),
    HARNESS_CODEX: (".agents/",),
}

#: Défaut quand la config ne déclare aucun `draft_targets` (warning doctor).
DEFAULT_DRAFT_HARNESS = HARNESS_OPENCODE

# Modes d'origine portés par `ResolvedDraftTarget.mode` (+ mode config "auto").
#: `MODE_DETECTED` a été retiré (A2) : la détection par marqueurs ne décide plus.
MODE_AUTO = "auto"
MODE_OVERRIDE = "override"
MODE_DEFAULT = "default"
MODE_LEGACY = "legacy"

_MODE_LABELS: dict[str, str] = {
    MODE_OVERRIDE: "config",
    MODE_DEFAULT: "défaut",
    MODE_LEGACY: "legacy",
}


# ---- A3 : marqueurs = signal d'alerte, jamais une décision --------------------


def _foreign_marker_warning(harness: str, markers: tuple[str, ...]) -> str:
    """Alerte « marqueur d'un harnais hors cible résolue » (surface non auditée)."""
    return (
        f"marqueur(s) de harnais hors cible résolue : {', '.join(markers)} "
        f"→ {harness} — ajouter draft_targets: [{harness}] pour auditer cette surface"
    )


def _absent_marker_warning(harnesses: tuple[str, ...], markers: tuple[str, ...]) -> str:
    """Alerte « aucun marqueur pour la cible résolue » (surface absente)."""
    return (
        f"aucun marqueur pour la cible résolue ({', '.join(harnesses)}) : "
        f"{', '.join(markers)} — surface de projection absente du projet"
    )


def marker_warnings(project_root: Path | str | None, harnesses: tuple[str, ...]) -> tuple[str, ...]:
    """Warnings de marqueurs — **warn-only**, aucun pouvoir de décision (A3).

    Signale l'écart entre la surface *décidée* (config) et la surface *observée*
    (marqueurs sur disque), sans jamais modifier `mode` ni `harnesses` (D3) :

    1. marqueur d'un harnais absent de la cible résolue → surface non auditée ;
    2. aucun marqueur ne correspond à la cible résolue → surface absente.

    Ordre déterministe (foreign par priorité harnais, puis absent). Sans
    `project_root` ou sans cible connue → liste vide : rien à comparer, donc
    rien à dire.
    """
    if project_root is None or not harnesses:
        return ()
    root = Path(project_root)
    resolved = {str(harness) for harness in harnesses}
    foreign: list[str] = []
    resolved_present = False
    resolved_missing: list[str] = []
    for harness in DRAFT_TARGET_PRIORITY:
        markers = DRAFT_HARNESS_MARKERS[harness]
        present = tuple(marker for marker in markers if (root / marker).is_dir())
        if harness not in resolved:
            if present:
                foreign.append(_foreign_marker_warning(harness, present))
            continue
        if present:
            resolved_present = True
        else:
            resolved_missing.extend(markers)
    warnings = list(foreign)
    if not resolved_present and resolved_missing:
        warnings.append(_absent_marker_warning(harnesses, tuple(resolved_missing)))
    return tuple(warnings)


@dataclass(frozen=True, slots=True)
class ResolvedDraftTarget:
    """Résultat de la résolution mono-cible (consommé par le doctor et la 2.2).

    ``harnesses`` compte exactement un harnais sauf en mode legacy (toutes les
    cibles connues, ordre de priorité conservé).
    """

    #: Origine de la décision : MODE_* ("override" | "default" | "legacy").
    mode: str
    #: Harnais cibles effectifs, dans l'ordre de priorité.
    harnesses: tuple[str, ...]
    #: Alertes non décisionnelles, séparées par " ; " (défaut appliqué, marqueurs
    #: hors cible, marqueur absent), sinon None. Jamais lues pour décider.
    warning: str | None = None


def resolve_draft_targets(
    project_root: Path | str | None, draft_cfg: DraftTargetsLike
) -> ResolvedDraftTarget:
    """Résolution effective : override config > défaut opencode.

    Mode legacy (liste vide []) → toutes les cibles connues. Sinon défaut
    `DEFAULT_DRAFT_HARNESS` avec warning explicite pour le doctor.

    Les marqueurs ne décident de rien (A2) : ils alimentent uniquement
    `ResolvedDraftTarget.warning` (A3). Le warning **n'altère ni `mode` ni
    `harnesses`** — changer de config change la cible, lire un marqueur non.
    """
    if draft_cfg.mode == MODE_LEGACY:
        mode, harnesses = MODE_LEGACY, DRAFT_TARGET_PRIORITY
    elif draft_cfg.mode == MODE_OVERRIDE and draft_cfg.targets:
        mode, harnesses = MODE_OVERRIDE, tuple(draft_cfg.targets)
    else:
        mode, harnesses = MODE_DEFAULT, (DEFAULT_DRAFT_HARNESS,)

    alerts: list[str] = list(marker_warnings(project_root, harnesses))
    if mode == MODE_DEFAULT:
        root_desc = str(project_root) if project_root is not None else "project_root manquant"
        # Le défaut de config garde sa place en tête : il explique le mode.
        alerts.insert(
            0,
            f"aucun draft_targets explicite en config ({root_desc}) — "
            f"défaut {DEFAULT_DRAFT_HARNESS} appliqué pour la projection des drafts",
        )
    return ResolvedDraftTarget(
        mode=mode,
        harnesses=harnesses,
        warning=" ; ".join(alerts) or None,
    )


def describe_draft_target(resolved: ResolvedDraftTarget) -> str:
    """Ligne compacte d'affichage : ``<harnais> (<mode>)`` ou legacy étendu."""
    label = _MODE_LABELS.get(resolved.mode, resolved.mode)
    if resolved.mode == MODE_LEGACY:
        return f"toutes cibles ({label})"
    return f"{', '.join(resolved.harnesses)} ({label})"
