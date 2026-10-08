"""Primitives textuelles du corpus : normalisation, narration, compaction, bruit, empreintes.

Région extraite de l'ancien `aggregator.py` monolithique (lignes 104-302 du commit
`02c1281`). Aucune dépendance à un autre module de `aggregator.` : c'est la feuille du
sous-paquet, et `repeats` / `outliers` / `usage` s'y branchent tous.

Ces primitives sont PURES — pas d'I/O, pas de modèle, pas d'options. Elles décident
qu'un tour utilisateur est exploitable (bruit ? narration ? artefact de compaction ?) et
comment deux tours se ressemblent (empreinte O(n) à 4 tokens normalisés puis triés).
"""

from __future__ import annotations

import re

__all__ = [
    "is_noise",
    "is_tool_narration",
    "normalize_fingerprint",
    "normalize_prompt",
]


def normalize_prompt(text: str) -> str:
    """Normalize a user turn (v5.15/v5.19): lowercase, flattened whitespace, trailing punctuation off."""
    text = " ".join(str(text).split()).strip().lower()
    return text.rstrip(".,;!?…:)]}")


def _strip_quotes(text: str) -> str:
    """Retire une paire de guillemets encadrants.

    Le client sérialise certaines invocations : le tour stocké est
    `"/swarmx test: …"` (guillemets inclus) et non `/swarmx test: …`. Sans ce
    retrait, `_command_name` voyait un tour qui ne commence pas par `/` et le
    corpus comptait zéro commande slash alors qu'il en contient (B5.1).
    """
    t = str(text).strip()
    for quote in ('"', "'"):
        if len(t) >= 2 and t.startswith(quote) and t.endswith(quote):
            return t[1:-1].strip()
    return t


#: Narration d'outillage réinjectée comme tour `user` par le harnais (« Called the
#: Read tool with the following input: {…} »). Les 4 PREMIERS TOKENS TRIÉS de ces
#: phrases forment le MÊME préfixe pour tous les outils d'une langue — d'où le
#: mégabucket `called|following|read|tool` (× 7 sur le run 2026-10-03) qui noie les
#: vraies répétitions sous le seuil (B4). Ce n'est pas une intention : aucun bucket.
_TOOL_NARRATION_RE = re.compile(
    r"^(?:called|calling|running|executing|reading|searching|writing|editing|invoking)\s+"
    r"(?:the\s+)?[\w.-]*\s*(?:tool|function)\b",
    re.IGNORECASE,
)


def is_tool_narration(text: str) -> bool:
    """True si le tour est de la narration d'outillage, pas une intention humaine (B4)."""
    return bool(_TOOL_NARRATION_RE.match(_strip_quotes(text)))


def _command_name(turn: str) -> str | None:
    """Slash-command invoked by a user turn, if any (v5.22). Returns the command name or None.

    v5.30 (5) : les chemins absolus (`/home/.../java`, `/tmp/x`) et URLs ne sont pas des
    commands — tout slash suivi d'un autre slash ou d'un chemin est ignoré.
    B5.1 : narration d'outillage écartée (même filtre que B3/B4) et guillemets
    encadrants retirés avant test — le client sérialise l'invocation.
    """
    text = _strip_quotes(turn)
    if is_tool_narration(text) or not text.startswith("/"):
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

#: B4 — sous ce nombre de tokens normalisés, une empreinte n'est pas une signature.
#: La normalisation (codes → `code`, chemins → `path`, nombres → `num`, stop-list)
#: réduit certains tours à UN seul token : le bucket qui en découle ne distingue plus
#: rien, il mesure la sur-normalisation. Mesuré sur le run 2026-10-03 : `str` × 8,
#: `es` × 2 — deux buckets sans signal, prêts à franchir le seuil de répétition.
#: Seuil à 2 et non 3 : « améliore ce script » (2 tokens de contenu) est une intention
#: réelle et répétée, pas un artefact — le couper casserait la métrique sur les prompts
#: courts, qui sont les plus répétés.
_FINGERPRINT_MIN_TOKENS = 2

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

    B4 : la narration d'outillage ne produit AUCUNE empreinte. Le tri des 4 premiers
    tokens fait que tous les outils d'une langue partagent le même préfixe — le
    mégabucket absorbait alors le budget de findings sans rien signaler d'anormal.
    Même raison pour une empreinte dégénérée : sous `_FINGERPRINT_MIN_TOKENS` tokens
    survivants, ce n'est pas une signature mais un artefact de sur-normalisation.
    Sur le run 2026-10-03 ces buckets d'un seul token portaient `str` (× 8) et `es` (× 2).
    """
    if is_tool_narration(prompt):
        return ""
    text = _strip_quotes(prompt).lower()
    text = _CODE_BLOCK_RE.sub(" code ", text)
    text = _QUOTED_RE.sub(" str ", text)
    text = _WINDOWS_PATH_RE.sub(" path ", text)
    text = _POSIX_PATH_RE.sub(" path ", text)
    text = _NUMBER_RE.sub(" num ", text)
    text = _NON_WORD_RE.sub(" ", text)
    tokens = [tok for tok in text.split() if len(tok) > 1 and tok not in _STOPWORDS]
    if len(tokens) < _FINGERPRINT_MIN_TOKENS:
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
