"""Classifieurs de session déterministes (P6) — aucun LLM, aucun I/O, aucun réseau.

Quatre détecteurs purs opérant sur `SessionUsage` :

- ``classify_intent``          — label unique (priorité planning > debug > review > explore > impl.)
- ``detect_spec_driven``       — approche spec-driven + preuves textuelles
- ``classify_production_review`` — % de code relu après édit (> 30 s ⇒ relu)
- ``score_prompt_maturity``    — 5 dimensions 0-20 → score 0-100 → grade A..F

Tout est déterministe et sérialisable ; les fonctions n'ont aucun effet de bord.
"""

from __future__ import annotations

import re

from .models import SessionClassification, SessionUsage, round6

#: Gap (secondes) au-delà duquel on considère qu'un édit a été relu (P6.3).
REVIEW_GAP_SECONDS = 30
#: Bornes basses de grade (score >= seuil) — P6.4.
GRADE_THRESHOLDS: dict[str, int] = {"A": 80, "B": 65, "C": 50, "D": 40}
#: Ordre de résolution des grades (décroissant) pour un score donné.
_GRADE_ORDER = ("A", "B", "C", "D")

#: Outils signalant une production de code (P6.1/P6.3).
_EDIT_WRITE_TOOLS = frozenset(
    {"edit", "write", "multiedit", "patch", "apply_patch", "create", "notebookedit"}
)
#: Outils signalant une exploration en lecture (P6.1).
_READ_TOOLS = frozenset({"read", "grep", "glob", "list", "search", "webfetch", "fetch"})

#: Motifs d'intention, ordonnés par priorité stricte (P6.1).
_INTENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "planning",
        re.compile(
            r"\b(?:plans?|planning|planifi\w*|design\w*|architect\w*|roadmap\w*|"
            r"blueprint\w*|stratég\w*|strateg\w*)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "debug",
        re.compile(
            r"\b(?:bug\w*|erreurs?|errors?|stack\s?traces?|fails?|failed|failures?|fix\w*|"
            r"corrig\w*|broken|ne marche pas|does(?:n'?t| not) work|not working)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "review",
        re.compile(
            r"\b(?:review\w*|relis\w*|relecture\w*|audit\w*|check\w*|vérifi\w*|verif\w*|"
            r"inspect\w*)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "explore",
        re.compile(
            r"\b(?:cherch\w*|trouv\w*|où est|where is|how does|how do|explai\w*|"
            r"explique\w*|explor\w*|comprend\w*|understand\w*)\b",
            re.IGNORECASE,
        ),
    ),
)

#: Chemin documentaire .md/.txt, combiné à un mot-clé spec pour compter comme preuve.
_DOC_PATH_RE = re.compile(r"[\w./\\-]+\.(?:md|txt)\b", re.IGNORECASE)
_SPEC_PATH_KEYWORD_RE = re.compile(r"(?:spec|prd|plan|design|requirements)", re.IGNORECASE)
#: Modaux d'exigence (P6.2), ordre déterministe.
_MODAL_WORDS = ("must", "should", "ensure", "doit", "doivent", "exige")
_MODAL_RE = {word: re.compile(rf"\b{word}\b", re.IGNORECASE) for word in _MODAL_WORDS}
#: Marqueurs de listes markdown (P6.2).
_LIST_MARKERS = (("list:bullet", "- "), ("list:ordered", "1. "), ("list:checkbox", "[ ]"))
#: Mention explicite de la commande /plan (bornée : `docs/plan.md` ne matche pas).
_PLAN_COMMAND_RE = re.compile(r"(?<![\w/])/plan\b", re.IGNORECASE)

#: Signaux textuels de la dimension « spécificité ».
_PATH_RE = re.compile(r"[\w./\\-]+\.[a-z0-9]{1,6}\b|(?:/[\w.\-]+){2,}", re.IGNORECASE)
_CODE_SPAN_RE = re.compile(r"```.*?```|`[^`]*`", re.DOTALL)
_IDENT_RE = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*\(\)|[a-z_]+\.(?:py|ts|js|json|md|ya?ml|java|go|rs)\b"
)
_NUMBER_RE = re.compile(r"\d+")
#: Mots de contexte (localisation/référence).
_CONTEXT_WORD_RE = re.compile(
    r"\b(?:dans|fichier|file|module|ligne|line|fonction|function|classe|class|"
    r"src|docs?|repo|projet|project)\b",
    re.IGNORECASE,
)
#: Mots de contrainte (impératifs/négations).
_CONSTRAINT_WORDS = (
    "must",
    "should",
    "ensure",
    "doit",
    "doivent",
    "exige",
    "obligatoire",
    "sans",
    "only",
    "jamais",
    "ne pas",
)
#: Mots de vérifiabilité.
_VERIFY_WORDS = (
    "test",
    "tests",
    "pytest",
    "vérifie",
    "verifie",
    "vérifier",
    "verify",
    "lint",
    "ruff",
    "mypy",
    "assert",
    "attendu",
    "expected",
    "critère",
    "acceptance",
    "rc",
)


def grade_for_score(score: int) -> str:
    """Grade P6.4 : A≥80, B≥65, C≥50, D≥40, F sinon (bornes exactes)."""
    for grade in _GRADE_ORDER:
        if score >= GRADE_THRESHOLDS[grade]:
            return grade
    return "F"


def _turn_blob(usage: SessionUsage) -> str:
    """Concatène les tours utilisateur (ordre d'occurrence) en un seul texte."""
    return "\n".join(str(t) for t in (usage.user_turns or []) if str(t).strip())


def _tool_count(usage: SessionUsage, names: frozenset[str]) -> int:
    """Somme des appels d'outils dont le nom (minuscule) appartient à `names`."""
    return sum(
        int(count) for tool, count in (usage.tool_calls or {}).items() if str(tool).lower() in names
    )


def _edit_write_active(usage: SessionUsage) -> bool:
    return _tool_count(usage, _EDIT_WRITE_TOOLS) > 0


def _timestamps(usage: SessionUsage, attr: str) -> list:
    """Liste triée d'attributs timestamp optionnels (tolérant à l'absence)."""
    values = getattr(usage, attr, None) or []
    return sorted(values)


def classify_intent(usage: SessionUsage) -> str:
    """Label d'intention unique, priorité planning > debug > review > explore > impl.

    Les signaux textuels dominent ; à défaut, les outils edit/write → implementation,
    les outils de lecture (read/grep/…) → explore, sinon implementation par défaut.
    """
    text = _turn_blob(usage)
    for label, pattern in _INTENT_PATTERNS:
        if pattern.search(text):
            return label
    if _edit_write_active(usage):
        return "implementation"
    if _tool_count(usage, _READ_TOOLS) > 0:
        return "explore"
    return "implementation"


def detect_spec_driven(usage: SessionUsage) -> dict:
    """Détecte une approche spec-driven et retourne ``{is_spec, preuves}``.

    Preuves (chaque signal détecté, ordre déterministe) :
      - ``path:<chemin>`` : chemin ``.md``/``.txt`` combiné à spec/prd/plan/design/requirements
      - ``modal:<mot>`` : must/should/ensure/doit/doivent/exige
      - ``list:bullet|ordered|checkbox`` : listes markdown
      - ``command:/plan`` : mention explicite de la commande plan
    """
    text = _turn_blob(usage)
    preuves: list[str] = []

    for match in _DOC_PATH_RE.finditer(text):
        path = match.group(0)
        if _SPEC_PATH_KEYWORD_RE.search(path):
            preuve = f"path:{path}"
            if preuve not in preuves:
                preuves.append(preuve)

    for word in _MODAL_WORDS:
        if _MODAL_RE[word].search(text):
            preuves.append(f"modal:{word}")

    for label, marker in _LIST_MARKERS:
        if marker in text:
            preuves.append(label)

    if _PLAN_COMMAND_RE.search(text):
        preuves.append("command:/plan")

    return {"is_spec": bool(preuves), "preuves": preuves}


def classify_production_review(usage: SessionUsage) -> dict:
    """% de code relu après édit (gap > 30 s vers le tour utilisateur suivant).

    Retourne ``{review_pct, measured, reviewed, warning}`` :
      - pas d'activité edit/write → ``review_pct=null`` sans warning ;
      - activité edit/write mais timestamps indisponibles → ``review_pct=null``
        + ``review-unmeasurable:<harness>`` (jamais 0, jamais inventé) ;
      - sinon ratio arrondi (round6) des édits dont le tour suivant arrive > 30 s après.
    """
    harness = getattr(usage, "harness", "") or ""
    if not _edit_write_active(usage) and not _timestamps(usage, "edit_write_timestamps"):
        return {"review_pct": None, "measured": 0, "reviewed": 0, "warning": None}

    edit_ts = _timestamps(usage, "edit_write_timestamps")
    user_ts = _timestamps(usage, "user_turn_timestamps")
    if not edit_ts or not user_ts:
        return {
            "review_pct": None,
            "measured": 0,
            "reviewed": 0,
            "warning": f"review-unmeasurable:{harness}",
        }

    measured = 0
    reviewed = 0
    for edit in edit_ts:
        following = next((u for u in user_ts if u > edit), None)
        if following is None:
            continue
        measured += 1
        if (following - edit).total_seconds() > REVIEW_GAP_SECONDS:
            reviewed += 1
    if measured == 0:
        return {"review_pct": None, "measured": 0, "reviewed": 0, "warning": None}
    return {
        "review_pct": round6(reviewed / measured),
        "measured": measured,
        "reviewed": reviewed,
        "warning": None,
    }


def _count_distinct(low_text: str, words: tuple[str, ...]) -> int:
    return sum(1 for word in words if word in low_text)


def score_prompt_maturity(usage: SessionUsage) -> dict:
    """Note 5 dimensions 0-20 → score 0-100 → grade A..F (P6.4).

    Dimensions : spécificité, contexte, contrainte, vérifiabilité, itérativité.
    Chaque dimension est bornée à 20 ; le score est la somme (donc 0-100).
    """
    turns = [str(t) for t in (usage.user_turns or []) if str(t).strip()]
    if not turns:
        dims = {
            "specificite": 0,
            "contexte": 0,
            "contrainte": 0,
            "verifiabilite": 0,
            "iterativite": 0,
        }
        return {"score": 0, "grade": "F", "dimensions": dims}

    text = "\n".join(turns)
    low = text.lower()

    specificite = 4
    if _PATH_RE.search(text):
        specificite += 4
    if _CODE_SPAN_RE.search(text):
        specificite += 4
    if _IDENT_RE.search(text):
        specificite += 4
    if _NUMBER_RE.search(text):
        specificite += 4
    specificite = min(20, specificite)

    contexte = 4
    if _PATH_RE.search(text):
        contexte += 4
    if len(turns) >= 2:
        contexte += 4
    if _CONTEXT_WORD_RE.search(low):
        contexte += 4
    if any(len(t) >= 120 for t in turns):
        contexte += 4
    contexte = min(20, contexte)

    contrainte = min(20, 4 * _count_distinct(low, _CONSTRAINT_WORDS))
    verifiabilite = min(20, 4 * _count_distinct(low, _VERIFY_WORDS))
    iterativite = min(20, 4 * len(turns))

    dims = {
        "specificite": specificite,
        "contexte": contexte,
        "contrainte": contrainte,
        "verifiabilite": verifiabilite,
        "iterativite": iterativite,
    }
    score = sum(dims.values())
    return {"score": score, "grade": grade_for_score(score), "dimensions": dims}


def classify_session(usage: SessionUsage) -> SessionClassification:
    """Assemble les 4 détecteurs en une `SessionClassification` sérialisable."""
    spec = detect_spec_driven(usage)
    review = classify_production_review(usage)
    maturity = score_prompt_maturity(usage)
    return SessionClassification(
        session_id=usage.session_id,
        intent=classify_intent(usage),
        spec_driven=bool(spec["is_spec"]),
        spec_preuves=list(spec["preuves"]),
        cost_usd=round6(usage.cost_usd),
        production_review_pct=review["review_pct"],
        production_review_measured=int(review["measured"]),
        production_review_warning=review["warning"],
        prompt_maturity_score=int(maturity["score"]),
        prompt_maturity_grade=str(maturity["grade"]),
        prompt_maturity_dimensions=dict(maturity["dimensions"]),
    )


def classify_sessions(usages: list[SessionUsage]) -> list[SessionClassification]:
    """Classifie toutes les sessions, triées par ``session_id`` (déterminisme)."""
    return [classify_session(u) for u in sorted(usages, key=lambda u: u.session_id)]
