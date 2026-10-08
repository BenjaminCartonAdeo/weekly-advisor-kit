"""Outliers et dédoublonnage : paires de skills proches, coûts atypiques, reprises.

Région extraite de l'ancien `aggregator.py` monolithique (lignes 349-475 du commit
`02c1281`). Trois décisions distinctes vivent ici :

- similarité de skills (description + 200 premiers caractères du corps, v5.25) ;
- outliers de coût par z-score robuste median+MAD calculé sur `log10(cost)` (K6) — la
  queue lourde des coûts noierait sinon les sessions petites mais signalées ;
- détection des sessions reprises, qui dupliquent tous les compteurs sous un nouvel id.

Ne dépend que de `models`, `util` et `aggregator.text`.
"""

from __future__ import annotations

import difflib
import hashlib
import math

from ..models import CostOutlier, SessionUsage, SkillSimilarPair, WarningEntry, round6
from ..util import robust_z
from .text import normalize_prompt

#: Skill-similarity body window (v5.25: description + first 200 chars of body).
SKILL_BODY_WINDOW = 200

#: hard floor for any outlier computation (v5.28 K6).
OUTLIER_MIN_ROOTS = 5
#: Top-N output for skill_similar_pairs.
SKILL_PAIRS_CAP = 5

#: Nombre de turns de tête (user_turns, y c. textes synthétiques) hachés pour la
#: détection de session reprise (v6.1 R3). Une reprise OpenCode copie tout le
#: transcript sous un NOUVEL id de session ; les 30 premiers parts restent
#: byte-identiques (vérifié sur données terrain), alors que deux invocations
#: distinctes d'une même commande ne partagent que le prompt initial (1/30).
RESUME_FINGERPRINT_TURNS = 8

__all__ = [
    "OUTLIER_MIN_ROOTS",
    "RESUME_FINGERPRINT_TURNS",
    "SKILL_BODY_WINDOW",
    "SKILL_PAIRS_CAP",
    "compute_cost_outliers_state",
    "dedup_resumed_usages",
    "resume_fingerprint",
]


def _skill_similar_pairs(entries, min_similarity: float) -> list[SkillSimilarPair]:
    """difflib on description + first 200 chars of body (v5.25) → top 5 pairs."""
    pairs: list[SkillSimilarPair] = []
    names = sorted(e.name for e in entries)
    by_name = {e.name: e for e in entries}
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a = by_name[names[i]]
            b = by_name[names[j]]
            ta = normalize_prompt(f"{a.description} {a.body[:SKILL_BODY_WINDOW]}")
            tb = normalize_prompt(f"{b.description} {b.body[:SKILL_BODY_WINDOW]}")
            ratio = difflib.SequenceMatcher(None, ta, tb).ratio()
            if ratio >= min_similarity:
                pairs.append(SkillSimilarPair(skills=[a.name, b.name], similarity=round6(ratio)))
    pairs.sort(key=lambda p: (-p.similarity, p.skills[0], p.skills[1]))
    return pairs[:SKILL_PAIRS_CAP]


def _robust_z(values: list[float]) -> dict[str, float]:
    """Robust z-scores per index (median + MAD, shared core in util); 6-decimal rounding."""
    return {str(i): round6(z) for i, z in enumerate(robust_z(values))}


def _cost_outliers(
    root_costs: list[tuple[str, float]],
    *,
    z_min: float,
    min_cost: float,
) -> list[CostOutlier]:
    """Median+MAD robust outliers among root window costs (cost >= floor).

    v5.28 (K6): z-scores computed on log10(cost) — heavy-tail cost distributions
    otherwise drown small-but-flagged sessions; the floor still applies on $.
    """
    values = [c for _sid, c in root_costs]
    zs = _robust_z([math.log10(max(c, 1e-6)) for c in values])
    out = []
    for i, (sid, cost) in enumerate(root_costs):
        if zs.get(str(i), 0.0) >= z_min and cost >= min_cost:
            out.append(CostOutlier(session_id=sid, cost_usd=cost, z_score=zs[str(i)]))
    out.sort(key=lambda o: (-o.z_score, o.session_id))
    return out


def resume_fingerprint(usage: SessionUsage) -> str | None:
    """Empreinte SHA-256 des premiers turns d'une session — None si trop courte.

    Les sessions plus courtes que `RESUME_FINGERPRINT_TURNS` ne participent pas
    au dédup : deux vraies sessions peuvent partager leur unique prompt initial
    (invocations répétées d'une même commande) sans être des reprises.
    """
    turns = [t for t in usage.user_turns if t and t.strip()]
    if len(turns) < RESUME_FINGERPRINT_TURNS:
        return None
    joined = "\x1f".join(turns[:RESUME_FINGERPRINT_TURNS])
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def dedup_resumed_usages(
    usages: list[SessionUsage],
) -> tuple[list[SessionUsage], list[dict]]:
    """Fusionne les copies de sessions reprises (resume-fork) dans l'original.

    Cause racine (R3) : une reprise OpenCode copie le transcript sous un nouvel
    id de session en conservant les timestamps d'origine des messages → la copie
    retombe dans la fenêtre avec les mêmes coûts/tokens que l'original : les deux
    ids sont comptés comme 2 sessions distinctes (totaux, top sessions,
    candidats d'audit). Aucune métadonnée de lineage n'existe dans le schéma DB
    (`session` V1 sans fork_session_id) → détection par contenu.

    Clé de dédup : (harness, project_path, empreinte des premiers turns).
    Primaire conservé = transcript le plus complet (la continuation) ;
    tie-break = session_id lexicographiquement minimal. Déterministe.
    """
    groups: dict[tuple[str, str, str], list[int]] = {}
    for i, usage in enumerate(usages):
        fp = resume_fingerprint(usage)
        if fp is None:
            continue
        groups.setdefault((usage.harness or "", usage.project_path or "", fp), []).append(i)
    dropped: dict[int, int] = {}  # index supprimé -> index conservé
    for _key, idxs in sorted(groups.items()):
        if len(idxs) < 2:
            continue
        ordered = sorted(idxs, key=lambda i: (-len(usages[i].user_turns), usages[i].session_id))
        kept_index = ordered[0]
        for i in ordered[1:]:
            dropped[i] = kept_index
    if not dropped:
        return usages, []
    kept = [u for i, u in enumerate(usages) if i not in dropped]
    records = [
        {
            "kept_session_id": usages[kept_index].session_id,
            "dropped_session_id": usages[i].session_id,
        }
        for i, kept_index in sorted(dropped.items())
    ]
    return kept, records


def compute_cost_outliers_state(
    n_roots: int, outlier_min_sessions: int, all_warnings: list[WarningEntry]
) -> str:
    """État de fiabilité des cost_outliers selon la taille d'échantillon (+ warning si petit)."""
    if n_roots == 0:
        return "no-data"
    if n_roots < OUTLIER_MIN_ROOTS:
        all_warnings.append(
            WarningEntry(
                session_id=None,
                message=f"sample trop petit ({n_roots} sessions < {OUTLIER_MIN_ROOTS}), cost_outliers peu fiables",
            )
        )
        return "skipped:small-sample"
    if n_roots < outlier_min_sessions:
        # K6: MAD robuste sur log-cost — fiable dès 5 racines (état dédié).
        return "computed:small-sample"
    return "computed"
