"""Pure aggregation logic (Part 1) — no I/O, no network, unit-testable in isolation.

Input: list of `SessionUsage` (window-filtered upstream) + options.
Output: `WeeklySummary` following the spec's normative serialization rules (v5.22/v5.25):
- `top_sessions_by_cost` ordered by (cost_usd DESC, session_id ASC)
- lists otherwise sorted by their stable alphabetical key
- amounts rounded to 6 decimals; divisions by zero → null; warnings capped at 50
"""

from __future__ import annotations

import difflib
import hashlib
import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .classifiers import classify_sessions
from .models import (
    MAX_WARNINGS,
    OUTLIER_MIN_SESSIONS,
    AgentTypeUsage,
    CommandUsage,
    CostOutlier,
    DailyTotal,
    HarnessUsage,
    ModelUsage,
    Period,
    SessionUsage,
    SkillSimilarPair,
    SkillUsage,
    SubagentTotals,
    ToolUsage,
    TopSession,
    Totals,
    UserPromptRepeat,
    WarningEntry,
    WeeklySummary,
    round6,
    split_canonical_session_id,
)
from .util import descendants_by_parent, robust_z, root_and_orphan_ids

#: Gaps strictly below this threshold count as "active" time (5 minutes).
ACTIVE_GAP_LIMIT = timedelta(minutes=5)
#: Estimated tokens per tool call = len(args) / 4 (documented approximation).
TOKENS_PER_CHAR = 4
#: Quasi-duplicate prompt comparison window (first N normalized chars, v5.19).
PROMPT_COMPARE_CHARS = 100
#: Cap on emitted prompt-repeat groups (spec §4: "plafonné à 20").
PROMPT_REPEATS_CAP = 20
#: Skill-similarity body window (v5.25: description + first 200 chars of body).
SKILL_BODY_WINDOW = 200

#: hard floor for any outlier computation (v5.28 K6).
OUTLIER_MIN_ROOTS = 5
#: Top-N output for skill_similar_pairs.
SKILL_PAIRS_CAP = 5


def _estimated_tokens(arg_chars: int) -> int:
    return round(arg_chars / TOKENS_PER_CHAR)


def _cache_hit_rate(cache_read: float, fresh_input: float) -> float | None:
    """Unique formula: cache_read / (cache_read + fresh_input); null on no input.

    cache_write is a cost, not a miss — it never enters the denominator.
    """
    denom = cache_read + fresh_input
    if denom <= 0:
        return None
    return round6(cache_read / denom)


def session_duration(usage: SessionUsage) -> tuple[int, int]:
    """(duration_seconds, active_time_seconds) for one session, from its window steps."""
    steps = sorted(usage.steps, key=lambda s: s.timestamp)
    if not steps:
        return 0, 0
    first, last = steps[0].timestamp, steps[-1].timestamp
    duration = max(0, int((last - first).total_seconds()))
    active = 0
    prev = first
    for step in steps[1:]:
        gap = step.timestamp - prev
        if gap < ACTIVE_GAP_LIMIT:
            active += int(gap.total_seconds())
        prev = step.timestamp
    return duration, active


def _descendants(by_id: dict[str, SessionUsage], root: SessionUsage) -> list[SessionUsage]:
    """All direct/indirect children of a root session (indexed BFS, util)."""
    ids = descendants_by_parent(
        ((u.session_id, u.parent_id) for u in by_id.values()), root.session_id
    )
    return [by_id[sid] for sid in ids]


def normalize_prompt(text: str) -> str:
    """Normalize a user turn (v5.15/v5.19): lowercase, flattened whitespace, trailing punctuation off."""
    text = " ".join(str(text).split()).strip().lower()
    return text.rstrip(".,;!?…:)]}")


def _command_name(turn: str) -> str | None:
    """Slash-command invoked by a user turn, if any (v5.22). Returns the command name or None.

    v5.30 (5) : les chemins absolus (`/home/.../java`, `/tmp/x`) et URLs ne sont pas des
    commands — tout slash suivi d'un autre slash ou d'un chemin est ignoré.
    """
    text = turn.lstrip()
    if not text.startswith("/"):
        return None
    rest = text[1:].strip()
    if not rest or "/" in rest:
        return None
    return rest.split()[0]


_COMPACTION_MARKERS = ("\u25a3", "\u2588", "\u2591", "\u2502", "\u23ff", "dcp |")


def _is_compaction_artifact(turn: str) -> bool:
    """Filtre les artefacts système de compaction du client (v5.30, B).

    Le client injecte des tours d'état (ex. `\u25a3 dcp | -374.5k removed, +4.2k summary
    \u2502\u2588...`) qui ne sont PAS des prompts utilisateur : ils polluaient
    `user_prompt_repeats` et gonflaient le coût quadratique de la détection.
    """
    t = turn.strip()
    if not t:
        return True
    if t[0] in ("\u25a3", "\u2588", "\u2591", "\u2502", "\u23ff"):
        return True
    low = t.lower()
    return "dcp |" in low and "removed" in low and ("summary" in low or "compact" in low)


#: Séparateurs de layout (`═─━=-_*` × 10+) : bruit visuel, pas un prompt.
_NOISE_SEPARATOR_RE = re.compile(r"[-═─━=_*]{10,}")
#: Micro-réponses oui/non/ponctuation (`y`, `n!`, `?!`, `...`) sans intention.
_NOISE_YESNO_RE = re.compile(r"[yn.!?]{1,3}")
#: Tours = commande de contrôle seule (pilotage, pas contenu réutilisable).
_CONTROL_TURNS = frozenset(
    {"continue", "try again", "yes", "no", "cancel", "abort", "stop", "retry"}
)
#: Au-delà, un "prompt" est un collage (log, artefact), pas une intention (v5.30, P3).
_NOISE_MAX_CHARS = 2000

#: Stop-list de formulation (~60 mots FR/EN) retirée avant empreinte.
_STOPWORD_SOURCE = (
    "le la les un une des du de d dans et ou mais donc or ni car que qui quoi dont "
    "au aux en y il elle ils elles je tu nous vous on ce cet cette ces mon ma mes "
    "ton ta tes son sa ses pour par sur sous avec sans vers chez est sont etre avoir "
    "fait faire pas plus moins tres tout tous toute toutes bien peux peut veux faut "
    "the a an and or but to of in on for with is are be this that it as at by from "
    "can you i we please just my me do"
)
_STOPWORDS = frozenset(_STOPWORD_SOURCE.split())

_CODE_BLOCK_RE = re.compile(r"```.*?```|`[^`]*`", re.DOTALL)
_QUOTED_RE = re.compile(r"\"[^\"]*\"|'[^'\n]*'")
_WINDOWS_PATH_RE = re.compile(r"[a-z]:\\[^\s]+", re.IGNORECASE)
_POSIX_PATH_RE = re.compile(r"(?:/[\w.\-]+){2,}/?")
_NUMBER_RE = re.compile(r"\d+")
_NON_WORD_RE = re.compile(r"[^\w\s]+")

#: Tours d'annulation (n'expriment pas un contenu réutilisable).
_CANCEL_TURNS = frozenset({"cancel", "abort", "stop", "annule", "abandonne"})
#: Préfixes de correction d'un tour précédent.
_CORRECTION_PREFIXES = (
    "no ",
    "non ",
    "nope",
    "pas ",
    "not ",
    "actually ",
    "instead ",
    "plutot ",
    "plutôt ",
    "wait ",
)


def is_noise(prompt: str) -> bool:
    """True si le tour n'est pas un prompt utilisateur exploitable (v5.30, P3).

    Bruit : séparateurs de layout (`═` × 10+), préfixe `system`, tour hors-borne
    (> 2000 chars), commande de contrôle seule (`continue`, `yes`, `retry`…),
    `continue to iterate` court, ou micro-réponse `[yn.!?]{1,3}`.
    """
    raw = str(prompt)
    text = raw.strip()
    if not text:
        return True
    if _NOISE_SEPARATOR_RE.search(text):
        return True
    low = text.lower()
    if low.startswith("system"):
        return True
    if len(raw) > _NOISE_MAX_CHARS:
        return True
    collapsed = " ".join(low.split())
    if collapsed in _CONTROL_TURNS:
        return True
    if "continue to iterate" in collapsed and len(text) < 80:
        return True
    return bool(_NOISE_YESNO_RE.fullmatch(collapsed))


def normalize_fingerprint(prompt: str) -> str:
    """Empreinte O(n) : 4 premiers tokens normalisés, triés, joints par `|` (v5.30, P3).

    Normalisation : minuscules ; blocs de code → `code` ; chaînes quotées → `str` ;
    chemins → `path` ; nombres → `num` ; ponctuation → espace ; stop-list retirée ;
    tokens de longueur ≤ 1 ignorés. Le tri rend l'empreinte insensible à l'ordre des
    mots : deux prompts qui ne diffèrent que par des valeurs partagent la même empreinte.
    """
    text = str(prompt).lower()
    text = _CODE_BLOCK_RE.sub(" code ", text)
    text = _QUOTED_RE.sub(" str ", text)
    text = _WINDOWS_PATH_RE.sub(" path ", text)
    text = _POSIX_PATH_RE.sub(" path ", text)
    text = _NUMBER_RE.sub(" num ", text)
    text = _NON_WORD_RE.sub(" ", text)
    tokens = [tok for tok in text.split() if len(tok) > 1 and tok not in _STOPWORDS]
    if not tokens:
        return ""
    return "|".join(sorted(tokens[:4]))


def _canonical_prompt(prompts: list[str]) -> str:
    """Prompt canonique : le plus court ≥ 20 chars normalisés, sinon le plus court."""
    long_enough = [p for p in prompts if len(normalize_prompt(p)) >= 20]
    pool = long_enough or prompts
    return min(pool, key=lambda p: (len(p), p))


def _is_cancel_turn(norm: str) -> bool:
    return norm in _CANCEL_TURNS


def _is_correction_turn(norm: str) -> bool:
    return norm.startswith(_CORRECTION_PREFIXES)


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
    """
    groups: dict[str, dict] = {}

    for usage in sorted(uses, key=lambda u: u.session_id):
        _accumulate_usage_repeats(groups, usage)

    return _emit_repeat_list(groups, repeat_min=repeat_min, min_chars=min_chars)


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


#: Nombre de turns de tête (user_turns, y c. textes synthétiques) hachés pour la
#: détection de session reprise (v6.1 R3). Une reprise OpenCode copie tout le
#: transcript sous un NOUVEL id de session ; les 30 premiers parts restent
#: byte-identiques (vérifié sur données terrain), alors que deux invocations
#: distinctes d'une même commande ne partagent que le prompt initial (1/30).
RESUME_FINGERPRINT_TURNS = 8


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


def _build_tool_usage(
    usages: list[SessionUsage],
) -> tuple[list[ToolUsage], dict[str, dict[str, int]], dict[str, dict[str, int]]]:
    """Tool usage + fingerprints (roots + children, window only)."""
    tool_counts: dict[str, int] = defaultdict(int)
    tool_chars: dict[str, int] = defaultdict(int)
    argument_fingerprints: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    result_fingerprints: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for u in usages:
        for tool, count in u.tool_calls.items():
            tool_counts[tool] += count
            tool_chars[tool] += u.tool_arg_chars.get(tool, 0)
        for tool, fingerprints in u.tool_arg_fingerprints.items():
            for fingerprint, count in fingerprints.items():
                argument_fingerprints[tool][fingerprint] += count
        for tool, fingerprints in u.tool_result_fingerprints.items():
            for fingerprint, count in fingerprints.items():
                result_fingerprints[tool][fingerprint] += count
    tool_usage = [
        ToolUsage(
            tool=t, call_count=tool_counts[t], estimated_tokens=_estimated_tokens(tool_chars[t])
        )
        for t in sorted(tool_counts)
    ]
    return tool_usage, argument_fingerprints, result_fingerprints


def _build_skill_usage(
    usages: list[SessionUsage], catalog: list[str]
) -> tuple[list[SkillUsage], list[str]]:
    """Skill usage + never-loaded list."""
    skill_counts: dict[str, int] = defaultdict(int)
    skill_sessions: dict[str, set] = defaultdict(set)
    for u in usages:
        for skill, count in u.skills_loaded.items():
            skill_counts[skill] += count
            skill_sessions[skill].add(u.session_id)
    skill_usage = [
        SkillUsage(skill=s, load_count=skill_counts[s], sessions_used_in=len(skill_sessions[s]))
        for s in sorted(skill_counts)
    ]
    loaded = set(skill_counts)
    return skill_usage, [s for s in catalog if s not in loaded]


def _build_command_usage(usages: list[SessionUsage]) -> list[CommandUsage]:
    """Command usage (v5.22)."""
    command_counts: dict[str, int] = defaultdict(int)
    command_sessions: dict[str, set] = defaultdict(set)
    for u in usages:
        for turn in u.user_turns:
            name = _command_name(turn)
            if name is None:
                continue
            command_counts[name] += 1
            command_sessions[name].add(u.session_id)
    return [
        CommandUsage(
            command=c, call_count=command_counts[c], sessions_used_in=len(command_sessions[c])
        )
        for c in sorted(command_counts)
    ]


def _unmeasurable_warnings(session_classifications) -> list[WarningEntry]:
    """One aggregated review-unmeasurable warning per harness (not per session)."""
    _unmeasurable: dict[str, int] = defaultdict(int)
    for sc in session_classifications:
        warning = sc.production_review_warning
        if not warning:
            continue
        harness = warning[len("review-unmeasurable:") :]
        _unmeasurable[harness] += 1
    return [
        WarningEntry(message=f"review-unmeasurable:{harness} ({_unmeasurable[harness]} sessions)")
        for harness in sorted(_unmeasurable)
    ]


def _build_subagent_totals(
    children: list[SessionUsage], usages: list[SessionUsage], orphan_ids: set[str]
) -> SubagentTotals:
    """Subagent totals (children + orphans, spec §8)."""
    sub_children = children + [u for u in usages if u.session_id in orphan_ids]
    by_agent: dict[str, dict] = defaultdict(lambda: {"count": 0, "cost": 0.0})
    for c in sub_children:
        agent = c.agent_type or "unknown"
        by_agent[agent]["count"] += 1
        by_agent[agent]["cost"] = round6(by_agent[agent]["cost"] + c.cost_usd)
    return SubagentTotals(
        child_session_count=len(sub_children),
        total_cost_usd=round6(sum(c.cost_usd for c in sub_children)),
        by_agent_type=[
            AgentTypeUsage(a, d["count"], round6(d["cost"])) for a, d in sorted(by_agent.items())
        ],
    )


def _usage_agg() -> dict:
    """Fabrique defaultdict pour model_agg / harness_agg."""
    return defaultdict(
        lambda: {"sessions": set(), "tokens": 0, "cost": 0.0, "cache_read": 0.0, "fresh": 0.0}
    )


@dataclass(slots=True)
class _RootTotals:
    """Accumulateurs de la boucle roots (totals + lignes agrégées par root)."""

    totals: Totals = field(default_factory=Totals)
    root_costs: dict[str, float] = field(default_factory=dict)
    model_agg: dict = field(default_factory=_usage_agg)
    harness_agg: dict = field(default_factory=_usage_agg)
    day_cost: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    day_tokens: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    day_cache: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    day_fresh: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    top_sessions: list[TopSession] = field(default_factory=list)


def _process_root(
    root: SessionUsage,
    by_id: dict[str, SessionUsage],
    acc: _RootTotals,
    *,
    include_subagents: bool,
    children_ids: set[str],
) -> None:
    """Traite un root : fusionne descendants, MAJ accumulateurs + TopSession."""
    descendants = _descendants(by_id, root) if include_subagents else []
    merged = [s for u in [root, *descendants] for s in u.steps]
    cost = round6(sum(s.cost for s in merged if s.cost is not None))
    acc.root_costs[root.session_id] = cost
    tokens = sum(s.total_tokens for s in merged)
    acc.totals.session_count += 1
    acc.totals.total_tokens += tokens
    acc.totals.total_cost_usd = round6(acc.totals.total_cost_usd + cost)
    cache_read = round(sum(s.tokens_cache_read for s in merged))
    cache_write = round(sum(s.tokens_cache_write for s in merged))
    fresh = round(sum(s.tokens_input for s in merged))
    output = round(sum(s.tokens_output for s in merged))
    reasoning = round(sum(s.tokens_reasoning for s in merged))
    acc.totals.cache_read_tokens += cache_read
    acc.totals.cache_write_tokens += cache_write
    acc.totals.fresh_input_tokens += fresh
    acc.totals.output_tokens += output
    acc.totals.reasoning_tokens += reasoning
    for s in merged:
        agg = acc.model_agg[s.model]
        agg["sessions"].add(root.session_id)
        agg["tokens"] += s.total_tokens
        agg["cost"] = round6(agg["cost"] + (s.cost if s.cost is not None else 0.0))
        agg["cache_read"] += s.tokens_cache_read
        agg["fresh"] += s.tokens_input
        _bucket = s.timestamp.strftime("%Y-%m-%d")
        acc.day_cost[_bucket] = round6(
            acc.day_cost[_bucket] + (s.cost if s.cost is not None else 0.0)
        )
        acc.day_tokens[_bucket] += s.total_tokens
        acc.day_cache[_bucket] += s.tokens_cache_read
        acc.day_fresh[_bucket] += s.tokens_input
    has_children = include_subagents and any(c.session_id in children_ids for c in descendants)
    duration, active = session_duration(root)
    harness = root.harness or split_canonical_session_id(root.session_id)[0] or ""
    h_agg = acc.harness_agg[harness]
    h_agg["sessions"].add(root.session_id)
    h_agg["tokens"] += tokens
    h_agg["cost"] = round6(h_agg["cost"] + cost)
    h_agg["cache_read"] += cache_read
    h_agg["fresh"] += fresh
    context_chars: dict[str, int] = {"file": 0, "tool_result": 0, "text": 0, "reasoning": 0}
    for u in [root, *descendants]:
        for k in ("file", "tool_result", "text", "reasoning"):
            context_chars[k] += u.context_chars.get(k, 0)
    acc.top_sessions.append(
        TopSession(
            session_id=root.session_id,
            title_or_topic=root.title or root.first_user_text,
            cost_usd=cost,
            reported_cost_usd_lifetime=root.reported_cost_usd_lifetime,
            total_tokens=tokens,
            project_path=root.project_path,
            duration_seconds=duration,
            active_time_seconds=active,
            cost_per_active_minute=round6(cost / (active / 60.0)) if active > 0 else None,
            api_call_count=len(merged),
            includes_subagents=has_children,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            cache_efficiency=_cache_hit_rate(cache_read, fresh),
            harness=harness,
            context_composition={  # chars/4 estimation (spec §2, TokenScope pattern)
                "file_tokens": _estimated_tokens(context_chars["file"]),
                "tool_result_tokens": _estimated_tokens(context_chars["tool_result"]),
                "text_tokens": _estimated_tokens(context_chars["text"]),
                "reasoning_tokens": _estimated_tokens(context_chars["reasoning"]),
            },
        )
    )


def aggregate(
    usages: list[SessionUsage],
    *,
    period: Period,
    generated_at: datetime,
    top_sessions_limit: int = 5,
    include_subagents: bool = True,
    skill_catalog: list[str] | None = None,
    skill_catalog_entries=None,
    skill_catalog_snapshot: list[dict] | None = None,
    warnings: list[WarningEntry] | None = None,
    known_parent_ids: set[str] | None = None,
    session_outlier_z: float = 3.0,
    session_outlier_min_cost_usd: float = 0.5,
    outlier_min_sessions: int = OUTLIER_MIN_SESSIONS,
    user_prompt_repeat_min: int = 3,
    user_prompt_repeat_similarity: float = 0.9,
    user_prompt_repeat_min_chars: int = 80,
    skill_similarity_min: float = 0.8,
) -> WeeklySummary:
    """Aggregate window-filtered session usages into a WeeklySummary.

    Roots are sessions without a parent active in the window (children →
    subagent_totals). Orphans (parent declared but absent and not in
    `known_parent_ids`) are counted in subagent_totals without attachment and
    journalised. Totals merge children into their root exactly once.
    """
    catalog = sorted(set(skill_catalog or []))
    catalog_entries = sorted(skill_catalog_entries or [], key=lambda e: e.name)
    skills_targets = {e.name: list(e.target_agents) for e in catalog_entries if e.target_agents}
    all_warnings = list(warnings or [])

    by_id: dict[str, SessionUsage] = {u.session_id: u for u in usages}
    orphan_ids, root_ids = root_and_orphan_ids(
        ((u.session_id, u.parent_id) for u in usages), known_parent_ids=known_parent_ids
    )
    for sid in sorted(orphan_ids):
        all_warnings.append(
            WarningEntry(
                session_id=sid,
                message="orphan child session (parent not found); counted in subagent_totals",
            )
        )
    children = [u for u in usages if u.parent_id is not None and u.parent_id in by_id]
    roots = [u for u in usages if u.session_id in root_ids]
    children_ids = {u.session_id for u in children}

    # ---- totals (roots + children merged once) + per-root aggregated rows ----
    acc = _RootTotals()
    for root in sorted(roots, key=lambda u: u.session_id):
        _process_root(
            root, by_id, acc, include_subagents=include_subagents, children_ids=children_ids
        )
    totals = acc.totals
    root_costs = acc.root_costs
    model_agg = acc.model_agg
    harness_agg = acc.harness_agg
    day_cost = acc.day_cost
    day_tokens = acc.day_tokens
    day_cache = acc.day_cache
    day_fresh = acc.day_fresh
    top_sessions = acc.top_sessions

    totals.cache_hit_rate = _cache_hit_rate(
        float(totals.cache_read_tokens), float(totals.fresh_input_tokens)
    )
    daily_totals = [
        DailyTotal(
            date=day,
            cost_usd=day_cost[day],
            total_tokens=day_tokens[day],
            cache_hit_rate=round6(day_cache[day] / (day_cache[day] + day_fresh[day]))
            if (day_cache[day] + day_fresh[day]) > 0
            else None,
        )
        for day in sorted(day_cost)
    ]

    # ---- cost outliers (roots, window cost) ----
    cost_outliers = _cost_outliers(
        [(sid, root_costs[sid]) for sid in sorted(root_costs)],
        z_min=session_outlier_z,
        min_cost=session_outlier_min_cost_usd,
    )
    n_roots = len(roots)
    cost_outliers_state = compute_cost_outliers_state(n_roots, outlier_min_sessions, all_warnings)

    # ---- by_model (normalized provider/model keys) ----
    by_model = [
        ModelUsage(
            model=key,
            session_count=len(agg["sessions"]),
            total_tokens=agg["tokens"],
            total_cost_usd=round6(agg["cost"]),
            cache_hit_rate=_cache_hit_rate(agg["cache_read"], agg["fresh"]),
        )
        for key, agg in sorted(model_agg.items())
    ]

    # ---- by_harness (miroir de by_model : sessions, tokens, cost) ----
    by_harness = [
        HarnessUsage(
            harness=key,
            session_count=len(agg["sessions"]),
            total_tokens=agg["tokens"],
            total_cost_usd=round6(agg["cost"]),
            cache_hit_rate=_cache_hit_rate(agg["cache_read"], agg["fresh"]),
        )
        for key, agg in sorted(harness_agg.items())
    ]

    # ---- top sessions by cost: select top N, output ordered (cost DESC, session_id ASC) ----
    ordered_all = sorted(top_sessions, key=lambda s: (-s.cost_usd, s.session_id))
    selected = ordered_all[: max(0, top_sessions_limit)]

    # ---- tool usage (roots + children, window only) ----
    tool_usage, argument_fingerprints, result_fingerprints = _build_tool_usage(usages)

    # ---- skill usage + never loaded ----
    skill_usage, skills_never_loaded = _build_skill_usage(usages, catalog)

    # ---- command usage (v5.22) ----
    command_usage = _build_command_usage(usages)

    # ---- skill similar pairs (v5.25) ----
    skill_similar_pairs = _skill_similar_pairs(catalog_entries, skill_similarity_min)

    # ---- repeated user prompts (v5.15/v5.19) ----
    user_prompt_repeats = _prompt_repeat_groups(
        usages,
        repeat_min=user_prompt_repeat_min,
        similarity=user_prompt_repeat_similarity,
        min_chars=user_prompt_repeat_min_chars,
    )

    # ---- deterministic session classifications (P6) ----
    session_classifications = classify_sessions(usages)
    # De-noise: one aggregated review-unmeasurable warning per harness (not per session).
    all_warnings.extend(_unmeasurable_warnings(session_classifications))

    # ---- subagent totals (children + orphans, spec §8) ----
    subagent_totals = _build_subagent_totals(children, usages, orphan_ids)

    return WeeklySummary(
        period=period,
        generated_at=generated_at,
        totals=totals,
        daily_totals=daily_totals,
        by_model=by_model,
        by_harness=by_harness,
        top_sessions_by_cost=selected,
        all_sessions=ordered_all,
        cost_outliers=cost_outliers,
        cost_outliers_state=cost_outliers_state,
        tool_usage=tool_usage,
        skill_usage=skill_usage,
        command_usage=command_usage,
        skill_similar_pairs=skill_similar_pairs,
        skill_catalog_count=len(catalog),
        skill_catalog_entries=list(skill_catalog_snapshot or []),
        skills_never_loaded=skills_never_loaded,
        skills_targets=skills_targets,
        user_prompt_repeats=user_prompt_repeats,
        session_classifications=session_classifications,
        subagent_totals=subagent_totals,
        tool_argument_fingerprints={
            tool: dict(sorted(values.items()))
            for tool, values in sorted(argument_fingerprints.items())
        },
        tool_result_fingerprints={
            tool: dict(sorted(values.items()))
            for tool, values in sorted(result_fingerprints.items())
        },
        warnings=_cap_warnings(all_warnings),
    )


def _cap_warnings(warnings: list[WarningEntry]) -> list[WarningEntry]:
    """Cap warnings at MAX_WARNINGS, sorted by (session_id, message)."""
    ordered = sorted(warnings, key=lambda w: (w.session_id or "", w.message))
    return ordered[:MAX_WARNINGS]
