"""Résolution et scan de la surface skills — A4/A5/A6.

Isolé de ``main`` parce que ``curation`` en a besoin : le catalogue doit être
dérivé d'un scan UNIQUE, donc la source de vérité doit être atteignable sans
passer par ``main``. Gardé ici, ``curation`` devait importer ``main`` en différé
pour éviter le cycle ``main`` → ``curation`` → ``main`` ; ici le lien est
unidirectionnel (``curation`` → ``skill_surface``, et ``main`` ré-exporte pour
préserver son API publique).

Le bloc n'a besoin que de ``models.SkillCatalogEntry`` et ``draft_targets`` —
toutes deux des feuilles en amont de ``main``, donc aucun cycle n'est recréé.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Iterable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from .draft_targets import DRAFT_HARNESS_TARGETS
from .models import SkillCatalogEntry

# --------------------------------------------------------------------------- skills catalog
#
# A4+A5 — UNE seule surface skills, deux floors (projet / global lecture seule).
# racines **projet** (projétables : cible de projection, origine de draft) et de
# racines **globales** strictement LECTURE SEULE (A4). Une racine globale alimente
# le catalogue — l'audit et le report la voient — mais ne peut structurellement
# apparaître ni comme destination de projection ni comme origine de draft.

#: A6 — un skill sous ce segment est archivé : plus chargeable, donc hors catalogue.
SKILL_ARCHIVE_DIR_NAME = "_archive"
_NULL_TTLS = frozenset({"", "none", "null"})


@dataclasses.dataclass(frozen=True, slots=True)
class SkillSurface:
    """Racines skills résolues, séparées par eligibilité à l'écriture."""

    #: Racines sous `project_root` : surface projetable (projection, draft).
    project_roots: tuple[Path, ...] = ()
    #: Racines utilisateur : lecture seule, jamais écrites (A4).
    global_roots: tuple[Path, ...] = ()

    @property
    def read_roots(self) -> tuple[Path, ...]:
        """Toutes les racines lues par le scan, projet d'abord (ordre stable)."""
        return (*self.project_roots, *self.global_roots)

    def is_global(self, path: Path) -> bool:
        """True si `path` est sous une racine globale — donc jamais écrivable."""
        try:
            resolved = path.resolve()
        except (OSError, RuntimeError):
            return False
        for root in self.global_roots:
            try:
                if resolved.is_relative_to(root.resolve()):
                    return True
            except (OSError, RuntimeError):
                continue
        return False


def resolve_skill_surface(
    project_root: Path | None = None,
    resolved_drafts: object | None = None,
    global_roots: Iterable[Path] | None = None,
) -> SkillSurface:
    """Surface skills unifiée (A5) : racines projet résolues + racines globales (A4).

    Les racines projet ne sont plus une constante codée en dur (`SKILL_LAYOUTS`,
    supprimée) : ce sont les cibles skills des harnais **résolus** par
    `resolve_draft_targets`, donc la table unique A1. Zéro harnais résolu ⇒
    aucune racine projet : le catalogue se réduit aux racines globales, ce qui
    est le comportement correct (une surface non déclarée n'est pas inventée).

    `global_roots=None` ⇒ défaut documenté (racine skills OpenCode) ; `[]` ⇒
    aucune racine globale, et le scan se comporte comme si le champ n'existait pas.
    """
    if global_roots is None:
        from .config import DEFAULT_GLOBAL_SKILL_ROOT

        roots: list[Path] = [DEFAULT_GLOBAL_SKILL_ROOT]
    else:
        roots = [Path(root) for root in global_roots]

    harnesses = tuple(getattr(resolved_drafts, "harnesses", ()) or ())
    root = project_root or Path.cwd()
    project: list[Path] = []
    seen: set[Path] = set()
    for harness in harnesses:
        for target in DRAFT_HARNESS_TARGETS.get(str(harness), ()):
            candidate = root.joinpath(*PurePosixPath(str(target)).parts)
            if candidate not in seen:
                seen.add(candidate)
                project.append(candidate)
    # Une racine globale ne doit jamais être projetable : on la retire explicitement
    # plutôt que de laisser une racine dupliquée par config.
    writable = [candidate for candidate in project if not any(candidate == g for g in roots)]
    return SkillSurface(project_roots=tuple(writable), global_roots=tuple(roots))


def _parse_skill_md(path: Path) -> tuple[str, str, list[str]]:
    """Minimal YAML-free frontmatter parse: (description, body[:2000], target_agents).

    Réutilise le parseur unique `frontmatter_blocks` (safe_git_write) qui gère
    l'imbrication metadata + listes YAML — plus de parseur dupliqué (v5.30 audit).
    """
    from .safe_git_write import frontmatter_blocks

    meta, body, err = frontmatter_blocks(path)
    if err:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return "", "", []
        return "", text[:2000], []
    description = (meta.get("description") or "").strip()
    targets = [
        x.strip().strip("\"'[] ") for x in (meta.get("target_agents") or "").split(",") if x.strip()
    ]
    return description, body[:2000], targets


def _is_archived_skill(path: Path) -> bool:
    """A6 — un skill sous `_archive/**` n'est plus chargeable : hors catalogue."""
    return SKILL_ARCHIVE_DIR_NAME in path.parts


# --------------------------------------------------------------------------- A6 protection
#
# Normalisation + atteignabilité d'un skill. Vit ici avec le scan parce que
# `scan_skill_catalog` en a besoin : laissée dans `curation`, elle refermait le
# cycle (`skill_surface` → `curation` → `skill_surface`). `curation`, `insights`
# et `cli` ré-importent ces noms, donc l'API publique est inchangée.


def _text_field(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _normalized_ttl(value: object) -> str | None:
    text = _text_field(value)
    if text is None or text.casefold() in _NULL_TTLS:
        return None
    return text.casefold()


def _skill_fields(entry: Mapping[str, Any] | object) -> tuple[str | None, str | None, str | None]:
    """Normalise (skill_id, origin, ttl_policy) depuis une entrée de catalogue.

    Accepte aussi bien ``{skill_id, origin, ttl_policy, metadata{...}}`` que
    ``{metadata:{skill_id, origin, ttl_policy}}``.
    """
    if not isinstance(entry, Mapping):
        return None, None, None
    raw_meta = entry.get("metadata")
    meta = raw_meta if isinstance(raw_meta, Mapping) else {}
    skill_id = _text_field(entry.get("skill_id")) or _text_field(meta.get("skill_id"))
    origin_values = [
        value.casefold()
        for value in (_text_field(meta.get("origin")), _text_field(entry.get("origin")))
        if value is not None
    ]
    # A duplicate/merged row must never weaken explicit user protection.
    origin = "user" if "user" in origin_values else (origin_values[0] if origin_values else None)
    ttl_values = [
        _normalized_ttl(value) for value in (meta.get("ttl_policy"), entry.get("ttl_policy"))
    ]
    ttl = (
        "pin"
        if "pin" in ttl_values
        else next((value for value in ttl_values if value is not None), None)
    )
    return skill_id, origin, ttl


#: « jamais chargé » n'est un constat que si le skill S'IL EST SEUL ne peut pas
#: apparaître dans « jamais chargé » : son absence d'appel est la conséquence de
#: sa déclaration, pas un défaut d'usage. Cas réel : `weekly-safety-guardrails`.
_NEVER_STANDALONE_RE = re.compile(
    r"\b(?:never|not|non)\b[^.;]{0,40}?\b"
    r"(?:load|use|run|invoke|call)(?:ed|ing)?\s*"
    r"(?:standalone|alone|on\s+its\s+own|by\s+itself|directly)\b",
    re.IGNORECASE,
)


def is_reachable_skill(entry: Mapping[str, Any] | object, description: str = "") -> bool:
    """A6 — un skill est-il atteignable par le harnais résolu ?

    Réutilise la normalisation de protection existante (``_skill_fields``) au
    lieu d'une seconde règle : ``origin == 'user'`` est hors périmètre de la
    revue, donc jamais recommandé ; « never load standalone » est unreachable
    par construction. Les deux produisent exactement le faux positif que la
    curation protège déjà de son côté.
    """
    if _skill_fields(entry)[1] == "user":
        return False
    return not _NEVER_STANDALONE_RE.search(description or "")


@dataclasses.dataclass(frozen=True, slots=True)
class SkillRecord:
    """Un `SKILL.md` lu UNE fois, vu sous ses deux formes consumers.

    A5 : le scan ne déduplique plus par nom mais par **chemin canonique** —
    deux skills homonymes dans deux racines sont deux fichiers distincts, pas un
    doublon. `is_global` (A4) marque l'appartenance à une racine lecture seule.
    """

    skill_id: str
    path: Path
    description: str
    body: str
    target_agents: tuple[str, ...]
    metadata: dict
    is_global: bool = False

    def as_catalog_entry(self) -> SkillCatalogEntry:
        return SkillCatalogEntry(
            name=self.skill_id,
            description=self.description,
            body=self.body,
            target_agents=list(self.target_agents),
        )

    def as_protection_entry(self) -> dict:
        """Forme attendue par `decide_actions` (origin/ttl_policy/usage)."""
        return {
            "skill_id": self.skill_id,
            "metadata": {
                "origin": self.metadata.get("origin"),
                "ttl_policy": self.metadata.get("ttl_policy"),
                "usage": self.metadata.get("usage"),
            },
        }


def scan_skill_records(surface: SkillSurface, extra_dirs: Iterable[Path] = ()) -> list[SkillRecord]:
    """Scan UNIQUE de la surface skills (A5) — une lecture par `SKILL.md`.

    Dédup par chemin canonique résolu : symlink et inclusion d'une racine par
    une autre ne produisent qu'une fiche. `_archive/**` est exclu (A6).
    Un `SKILL.md` lu une seule fois ; ordre = ordre de la surface (projet
    d'abord, puis lecture seule), chemins triés dans chaque racine.
    """
    from .safe_git_write import frontmatter_blocks

    records: dict[Path, SkillRecord] = {}
    order: list[Path] = []
    roots = [*surface.read_roots, *extra_dirs]
    for root in roots:
        try:
            candidates = sorted(root.glob("**/SKILL.md"))
        except OSError:
            continue
        for skill_md in candidates:
            if not skill_md.is_file() or _is_archived_skill(skill_md):
                continue
            try:
                canonical = skill_md.resolve()
            except (OSError, RuntimeError):
                continue
            if canonical in records:
                continue
            meta, body, _err = frontmatter_blocks(skill_md)
            description = str(meta.get("description") or "").strip()
            targets = tuple(
                x.strip().strip("\"'[] ")
                for x in str(meta.get("target_agents") or "").split(",")
                if x.strip()
            )
            nested = meta.get("metadata")
            nested = nested if isinstance(nested, Mapping) else {}
            records[canonical] = SkillRecord(
                skill_id=skill_md.parent.name,
                path=canonical,
                description=description,
                body=(body or "")[:2000],
                target_agents=targets,
                # `frontmatter_blocks` is intentionally a tiny parser; nested
                # YAML keys also appear at the top level.  Read both forms so
                # protection remains effective on disk.
                metadata={
                    "origin": nested.get("origin") or meta.get("origin"),
                    "ttl_policy": nested.get("ttl_policy") or meta.get("ttl_policy"),
                    "usage": nested.get("usage") or meta.get("usage"),
                },
                is_global=surface.is_global(skill_md),
            )
            order.append(canonical)
    return [records[key] for key in order]


def scan_skill_catalog(
    records: Iterable[SkillRecord],
) -> tuple[list[str], int, list[SkillCatalogEntry]]:
    """Catalogue dérivé de l'unique scan (A5) : (noms atteignables, count, entries).

    Les `entries` couvrent TOUTE la surface lue (moins `_archive`, A6) : l'audit
    et le report doivent voir les skills `origin=user`, qu'ils décrivent. Les
    `names` — seule source de `skills_never_loaded` — sont restreints aux skills
    **atteignables** (A6) : ni `origin=user`, ni « never load standalone ».
    """
    entries = [record.as_catalog_entry() for record in records]
    names = sorted(
        {
            record.skill_id
            for record in records
            if is_reachable_skill(record.as_protection_entry(), record.description)
        }
    )
    return names, len(names), entries
