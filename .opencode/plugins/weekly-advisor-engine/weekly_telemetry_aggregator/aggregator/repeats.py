"""Détection des prompts utilisateur répétés (v5.15 / v5.19 / v5.30 P3).

Région extraite de l'ancien `aggregator.py` monolithique (lignes 176-346 du commit
`02c1281`). Détection O(n) par empreinte : plus de `difflib`, plus de scan quadratique —
les tours candidats sont indexés par `normalize_fingerprint`, puis chaque bucket est émis
si son support ET la longueur de son prompt canonique franchissent les deux seuils.

Ne dépend que de `aggregator.text` (feuille du sous-paquet) et de `models`.
"""

from __future__ import annotations

from ..models import SessionUsage, UserPromptRepeat, round6
from .text import (
    _canonical_prompt,
    _is_cancel_turn,
    _is_compaction_artifact,
    _is_correction_turn,
    is_noise,
    normalize_fingerprint,
    normalize_prompt,
)

#: Cap on emitted prompt-repeat groups (spec §4: "plafonné à 20").
PROMPT_REPEATS_CAP = 20

__all__ = ["PROMPT_REPEATS_CAP"]


def _build_skill_draft(label: str, count: int, sessions: int, examples: list[str]) -> str:
    """Brouillon markdown (# Skill / When to use / Steps / Example prompts)."""
    samples = [e[:120] for e in examples[:3]]
    lines = [
        f"# Skill: {label}",
        "",
        "## When to use",
        f"Réponse répétée {count}× sur {sessions} session(s) — automatiser ce flux.",
        "",
        "## Steps",
        "1. Reproduire le flux récurrent et figer ses entrées/sorties.",
        "2. Encapsuler la procédure dans un skill portable.",
        "3. Valider sur un cas réel avant généralisation.",
        "",
        "## Example prompts",
        *[f"- {sample}" for sample in samples],
    ]
    return "\n".join(lines)


def _new_repeat_bucket() -> dict:
    return {
        "count": 0,
        "chars_sum": 0,
        "prompts": [],
        "sessions": set(),
        "harnesses": set(),
        "cancels": 0,
        "corrections": 0,
        "first": None,
        "last": None,
    }


def _accumulate_repeat_turn(
    groups: dict[str, dict], usage: SessionUsage, turn: str, first_ts, last_ts
) -> None:
    fingerprint = normalize_fingerprint(turn)
    if not fingerprint:
        return
    norm = normalize_prompt(turn)
    group = groups.get(fingerprint)
    if group is None:
        group = _new_repeat_bucket()
        groups[fingerprint] = group
    group["count"] += 1
    group["chars_sum"] += len(turn)
    group["prompts"].append(turn)
    group["sessions"].add(usage.session_id)
    if usage.harness:
        group["harnesses"].add(usage.harness)
    if _is_cancel_turn(norm):
        group["cancels"] += 1
    if _is_correction_turn(norm):
        group["corrections"] += 1
    if first_ts is not None and (group["first"] is None or first_ts < group["first"]):
        group["first"] = first_ts
    if last_ts is not None and (group["last"] is None or last_ts > group["last"]):
        group["last"] = last_ts


def _repeat_examples(prompts: list[str], limit: int = 5) -> list[str]:
    examples: list[str] = []
    seen: set[str] = set()
    for candidate in prompts:
        stripped = candidate.strip()
        if not stripped or stripped in seen:
            continue
        seen.add(stripped)
        examples.append(stripped)
        if len(examples) >= limit:
            break
    return examples


def _emit_repeat_group(group: dict, *, repeat_min: int, min_chars: int) -> UserPromptRepeat | None:
    count = group["count"]
    if count < repeat_min:
        return None
    canonical = _canonical_prompt(group["prompts"])
    preview = normalize_prompt(canonical)
    if len(preview) < min_chars:
        return None
    sessions = sorted(group["sessions"])
    session_count = len(sessions)
    examples = _repeat_examples(group["prompts"])
    return UserPromptRepeat(
        normalized_preview=preview[:80],
        count=count,
        session_id=sessions[0] if sessions else "",
        avg_chars=round(group["chars_sum"] / count),
        sessions_distinct=session_count,
        harnesses_distinct=len(group["harnesses"]),
        cancel_rate=round6(group["cancels"] / count),
        avg_correction_turns=round6(group["corrections"] / session_count) if session_count else 0.0,
        first_seen=group["first"].isoformat() if group["first"] else "",
        last_seen=group["last"].isoformat() if group["last"] else "",
        examples=examples,
        skill_draft=_build_skill_draft(preview[:80], count, session_count, examples),
        estimated_time_saved_mins=count * 2,
    )


def _is_repeat_candidate(turn: str) -> bool:
    """Un tour est candidat s'il n'est ni compaction, ni bruit, ni vide normalisé."""
    if _is_compaction_artifact(turn):
        return False
    if is_noise(turn):
        return False
    return bool(normalize_prompt(turn))


def _accumulate_usage_repeats(groups: dict[str, dict], usage: SessionUsage) -> None:
    """Indexe les tours candidats d'un usage (sessions enfants exclues)."""
    if usage.parent_id is not None:
        return  # v5.30 (7) : tours des sessions enfants exclus
    step_ts = [s.timestamp for s in usage.steps]
    first_ts = min(step_ts) if step_ts else None
    last_ts = max(step_ts) if step_ts else None
    for turn in usage.user_turns:
        if not _is_repeat_candidate(turn):
            continue
        _accumulate_repeat_turn(groups, usage, turn, first_ts, last_ts)


def _emit_repeat_list(
    groups: dict[str, dict], *, repeat_min: int, min_chars: int
) -> list[UserPromptRepeat]:
    """Émet, trie et plafonne les groupes répétés."""
    out: list[UserPromptRepeat] = []
    for group in groups.values():
        emitted = _emit_repeat_group(group, repeat_min=repeat_min, min_chars=min_chars)
        if emitted is not None:
            out.append(emitted)
    out.sort(key=lambda r: (-r.count, r.session_id))
    return out[:PROMPT_REPEATS_CAP]


def _prompt_repeat_groups(
    uses: list[SessionUsage],
    *,
    repeat_min: int,
    similarity: float,  # noqa: ARG001 — vestigial, conservé pour compat d'appel (v5.30 P3)
    min_chars: int,
) -> list[UserPromptRepeat]:
    """User prompts repeated across the window, grouped by O(n) fingerprint (v5.30, P3).

    Filtre bruit / compaction / sessions enfants, puis indexe les tours par
    `normalize_fingerprint` (bucket map) — plus de `difflib` ni de scan quadratique.
    Un groupe est émis si `count >= repeat_min` et que son prompt canonique atteint
    `min_chars`. Sortie triée `(-count, session_id)`, plafonnée à `PROMPT_REPEATS_CAP`.

    NOTE B4.2 — le seuil de support RELATIF demandé par le plan est ici VOLONTAIREMENT
    absent, et c'est un refus argumenté, pas un oubli. Plafonner la part du corpus
    (`count / total > seuil ⇒ bruit de fond`) tue la tête de la métrique : sur le
    stress-test `test_prompt_repeats_large_volume_is_fast`, 100 prompts identiques =
    100 % du corpus, et l'assertion « les répétitions exactes par tâche sont détectées »
    tombe. Un plancher proportionnel (`max(repeat_min, total / N)`) se heurte au même
    test par l'autre côté : 20 répétitions réelles sur 2000 tours. Il n'existe donc
    aucun calibrage « relatif » qui distingue « la même intention, 100 fois » de « la
    narration de l'agent, 100 fois » — les deux sont, par construction, dominants.
    Ce qui les distingue est leur NATURE, pas leur poids : c'est le filtre de
    boilerplate (`normalize_fingerprint` → "") qui fait le travail. Le seuil reste
    absolu, et `repeat_min` reste le seul levier.
    """
    groups: dict[str, dict] = {}

    for usage in sorted(uses, key=lambda u: u.session_id):
        _accumulate_usage_repeats(groups, usage)

    return _emit_repeat_list(groups, repeat_min=repeat_min, min_chars=min_chars)
