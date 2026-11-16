"""Validation des blocs LLM du rapport."""
import re
from collections.abc import Iterable, Mapping

_NUMBER_BAND_RE = re.compile(r"\[N:([^\[\]]*)\]")

#: Titre Markdown dans le bloc qualitatif (C1) : `# Foo` … `###### Foo`, plus le
#: `#` nu (`^#{1,6}(\s|$)`). La garde `line.startswith("#")` du validateur ratisse
#: le reste (`###Foo`, `#######Foo`), donc les deux formes sont nécessaires.
_MD_HEADING_RE = re.compile(r"^#{1,6}(\s|$)")

#: Unités décomptables du rapport — ce qui suit un nombre en français.
_COUNT_UNITS = (
    r"findings?|constats?|alertes?|sessions?|skills?|règles?|violations?|fichiers?|"
    r"catégories|occurrences?|éléments?|entrées?|lignes?|points?|appels?|tests?|"
    r"jours?|semaines?|heures?|fois|tokens?|dossiers?|commits?|cas"
)

#: Forme écrite d'un décompte (C4) — le contournement du check « chiffres », où
#: l'agent écrit « huit sessions » au lieu de « [N:8] sessions ».
#:
#: Deux tiers, parce que la forme écrite n'est pas une catégorie uniforme :
#: - « huit », « dix-neuf », « quatre-vingt-dix » n'ont AUCUN autre sens que
#:   numérique → insensibles à la casse, ancrés sur une unité comptable ;
#: - « un constat », « une alerte » sont du français normal : les petits nombres
#:   (un→sept) ne sont refusés qu'en position de décompte, derrière un cue
#:   (« sur quatre sessions », « au total trois alertes »). Sans ce cue, la règle
#:   tuerait la moitié des blocs valides.
_WRITTEN_COUNT_RE = re.compile(
    r"(?<![\w-])(?i:"
    r"soixante-dix-neuf|soixante-dix-huit|soixante-dix-sept|soixante-et-onze|soixante-dix|"
    r"quatre-vingt-onze|quatre-vingt-dix|quatre-vingts|quatre-vingt|"
    r"vingt-et-un|dix-neuf|dix-huit|dix-sept|"
    r"zéro|huit|neuf|dix|onze|douze|treize|quatorze|quinze|seize|"
    r"vingt|trente|quarante|cinquante|soixante|cent|mille"
    r")\s+(?:" + _COUNT_UNITS + r")(?![\w-])"
)
#: Cue FAIBLE : admis devant deux→sept, mais PAS devant un/une. « sur une
#: session » est l'article indéfini (« on a session »), pas un décompte : le cue
#: `sur` est trop faible pour transformer « une » en nombre.
_WRITTEN_COUNT_CUE_RE = re.compile(
    r"(?<![\w-])(?:plus de|plus qu[ei']|sur|exactement|au total|total de|en tout|"
    r"au nombre de|combien de)\s+(?:deux|trois|quatre|cinq|six|sept)"
    r"\s+(?:" + _COUNT_UNITS + r")(?![\w-])"
)
#: Cue FORT : seul contexte où « un/une » dénote réellement un décompte
#: (« exactement une alerte », « au total une session »). `sur` est exclu.
_WRITTEN_COUNT_STRONG_CUE_RE = re.compile(
    r"(?<![\w-])(?:exactement|au total|total de|en tout|au nombre de|combien de)"
    r"\s+(?:un|une)\s+(?:" + _COUNT_UNITS + r")(?![\w-])"
)

#: Entrées de l'index publiées dans weekly-report-gates.json. L'index complet sert
#: à la résolution ; le gates reste lisible (borné, avec son total pour vérifier
#: que la troncature n'a rien caché).
_INDEX_PUBLISH_LIMIT = 200


def _iter_scalar_numbers(node: object, path: str = "") -> Iterable[tuple[str, float]]:
    """Scalaires numériques d'un artefact JSON, avec leur chemin d-dot.

    Les booléens sont exclus (`isinstance(True, int)` est vrai en Python) et une
    chaîne n'est retenue que si elle EST un nombre — pas un nombre contenu dans
    une phrase, sinon n'importe quel décompte cité en prose deviendrait résolvable.
    """
    if isinstance(node, Mapping):
        for key, value in node.items():
            yield from _iter_scalar_numbers(value, f"{path}.{key}" if path else str(key))
    elif isinstance(node, list | tuple):
        for idx, value in enumerate(node):
            yield from _iter_scalar_numbers(value, f"{path}[{idx}]")
    elif isinstance(node, bool) or node is None:
        return
    elif isinstance(node, int | float):
        yield path, float(node)
    elif isinstance(node, str):
        try:
            yield path, float(node.strip().replace(",", "."))
        except ValueError:
            return


def number_band_index(findings: dict | None, insights: dict | None) -> dict[str, object]:
    """Scalaires vers lesquels une bande `[N:…]` doit résoudre (C2, politique D2).

    Deux familles, toutes deux publiées dans `weekly-report-gates.json` pour que
    le refus soit auditable sans relire le validateur :
    - `scalars`  : chaque valeur numérique des deux artefacts de la section 4
      (`weekly-quality-findings`, `weekly-insights`), par chemin d-dot ;
    - `counts`   : les cardinances dérivées (nombre de findings, d'alertes, de
      constats de maintenance, par sévérité) — un décompte d'agents est un
      agrégat, pas un scalaire stocké.

    Index COMPLET : la résolution d'une bande ne doit pas dépendre du formatage
    du gates. La troncature d'affichage est faite par `published_number_bands`.
    """
    findings_list = (findings or {}).get("findings", []) if findings else []
    alerts = (insights or {}).get("alerts", []) if insights else []
    maint = (insights or {}).get("maintenance", {}).get("findings", []) if insights else []

    scalars = dict(_iter_scalar_numbers(findings or {}))
    scalars.update(_iter_scalar_numbers(insights or {}))

    counts: dict[str, int] = {
        "findings_total": len(findings_list),
        "alerts_total": len(alerts),
        "maintenance_total": len(maint),
    }
    for severity in ("high", "medium", "low"):
        counts[f"findings_{severity}"] = sum(
            1 for f in findings_list if f.get("severity") == severity
        )
    return {"scalars": scalars, "scalars_total": len(scalars), "counts": counts}


def published_number_bands(index: dict[str, object]) -> dict[str, object]:
    """Vue lisible de l'index pour le gates : `scalars` borné, total conservé.

    Un gates de plusieurs milliers d'entrées serait illisible ; `scalars_total`
    permet au lecteur de voir que la troncature existe au lieu de le deviner.
    """
    scalars = index.get("scalars", {})
    items = list(scalars.items()) if isinstance(scalars, Mapping) else []
    return {**index, "scalars": dict(items[:_INDEX_PUBLISH_LIMIT])}


def _band_resolves(raw: str, index: dict[str, object]) -> bool:
    """La charge utile `[N:…]` est-elle un nombre, et présent dans l'index ?"""
    head = raw.split()[0] if raw.split() else ""
    try:
        value = float(head.replace(",", "."))
    except ValueError:
        return False
    scalars = index.get("scalars", {})
    counts = index.get("counts", {})
    candidates = [*(scalars.values() if isinstance(scalars, Mapping) else []), *counts.values()]
    return any(abs(float(candidate) - value) < 1e-9 for candidate in candidates)


def validate_llm_blocks(
    text: str,
    findings: dict | None,
    insights: dict | None,
    *,
    blocks_min_words: int | None = None,
):
    """Garde-fous anti-hallucination du bloc 7b LLM (v5.29, hybride).

    Retourne (violations, coverage_warnings) :
    - violations → le bloc est REJETÉ, report-assemble bascule sur le brouillon
      déterministe (fallback) ;
    - coverage_warnings → le bloc est accepté mais l'annexe signale les constats
      high non cités (omission ≠ hallucination).
    Checks : zéro chiffre (spec : le bloc ne cite que catégories/sévérités) ;
    balises de source [F:ses_xxx#cat] / [M:cat] / [A:rule] résolues dans les
    entrées ; bandes [N:…] résolues vers un scalaire des artefacts ; aucune
    forme écrite d'un décompte ; taille ≤ 60 lignes ; tout finding high doit
    être cité ; et DEUX contrôles distincts sur les titres Markdown, dont il
    faut connaître la portée exacte de chacun :

    - tête — la 1re ligne NON vide ne peut pas être un titre `##`/`#` (un ou
      deux dièses suivis d'une espace ; le motif `^#{1,2}` + espace), d'où un
      message dédié « en tête de bloc » : le gabarit fournit déjà le titre de
      la section 4, un titre en tête y créerait un doublon ;
    - nulle part — AUCUNE ligne ne peut être un titre Markdown, à aucun
      niveau : `_MD_HEADING_RE` (`#{1,6}` puis une espace ou la fin de ligne,
      donc `# Foo`…`###### Foo` et le `#` nu) ou la garde plus large
      `line.startswith("#")`, qui ratisse en plus les `###Foo` sans espace et
      les `#######Foo`. Une ligne de tête n'est ignorée par ce contrôle que si
      le contrôle de tête l'a déjà signalée (jamais de double message).

    Conjointement : aucun titre, à aucun niveau (`#`, `##`, `###`, …), nulle
    part dans le bloc — commencer par une phrase.

    Portée : ce validateur ne voit QUE la prose de l'agent. Le brouillon
    automatique `-auto-` (qui porte volontairement des `###`) n'est jamais
    validé — il est injecté tel quel, cf. `_assemble_quality_block`.
    """
    violations: list[str] = []
    coverage: list[str] = []

    if blocks_min_words is not None:
        n_words = len(text.split())
        if n_words < blocks_min_words:
            violations.append(
                f"bloc LLM trop court ({n_words} mots < {blocks_min_words}) — "
                f"détailler les constats, ne pas résumer"
            )

    # les balises [F:...]/[M:...]/[A:...] portent des ids/session_ids (chiffres) —
    # le check chiffres ne s'applique qu'au texte visible (hors balises). Les
    # bandes [N:…] portent un nombre par construction : elles sont neutralisées
    # ici et contrôlées plus bas (C2), sinon toute bande serait un « chiffre libre ».
    text_no_tags = re.sub(r"\[[FMA]:[^\]]+\]", "", text)
    text_no_tags = _NUMBER_BAND_RE.sub("", text_no_tags)
    # chiffres autorisés hors balise : dates ISO, pourcentages, numéros de version.
    # la spec interdit toujours les chiffres « libres » (coûts, décomptes, durées).
    _ALLOWED_NUM = re.compile(
        r"\b\d{4}-\d{2}-\d{2}\b"  # date ISO
        r"|\b\d{1,3}([.,]\d+)?%"  # pourcentage
        r"|\bv?\d+\.\d+(\.\d+)?\b"  # version sémantique
    )
    text_no_allowed = _ALLOWED_NUM.sub(" ", text_no_tags)
    illegal_digits = re.findall(r"\d+", text_no_allowed)
    if illegal_digits:
        snippet = ", ".join(illegal_digits[:5])
        first_digit = re.search(r"\d+", text_no_allowed)
        line = text_no_allowed.count("\n", 0, first_digit.start()) + 1 if first_digit else 1
        violations.append(
            "chiffres interdits dans le bloc LLM (spec : catégories/sévérités "
            f"uniquement) — ligne {line}, chiffres hors date/pourcentage/version : {snippet}"
        )

    body_lines = text.splitlines()
    n_lines = len(body_lines)
    if n_lines > 60:
        violations.append(f"bloc trop long (ligne {n_lines}, {n_lines} lignes > 60)")

    # Le gabarit fournit déjà le titre de la section 4 : un titre en tête de bloc
    # le dupliquerait dans le rapport assemblé (2 H2). On vise la 1re ligne NON
    # vide — une ligne vide en tête ne doit pas servir d'échappatoire.
    head = next((i for i, ln in enumerate(body_lines) if ln.strip()), None)
    head_reported = head is not None and re.match(r"^#{1,2}\s", body_lines[head]) is not None
    if head_reported:
        violations.append(
            f"titre Markdown interdit en tête de bloc (ligne {head + 1}) — le gabarit "
            f"fournit déjà le titre de la section 4 : {body_lines[head].strip()[:60]!r}"
        )

    # C1 : AUCUNE ligne de titre, n'importe où et n'importe quel niveau. Les
    # `###` de l'agent passaient la validation et n'existaient que dans le rendu
    # markdown — le HTML, lui, échappe le texte puis le paraphe, donc il affichait
    # littéralement « <p>###…</p> » (run 2026-10-03, 4 sous-titres). Deux rendus,
    # deux sémantiques : on ferme la divergence à la racine, à l'écriture, plutôt
    # qu'en essayant d'apprendre le markdown au renderer HTML.
    for idx, line in enumerate(body_lines):
        # La ligne de tête n'est ignorée que si le message « en tête de bloc » la
        # a déjà signalée : sinon `### Foo` en 1re ligne passerait par les deux
        # checks (le check de tête ne couvre que `#{1,2}`).
        if idx == head and head_reported:
            continue
        if _MD_HEADING_RE.match(line) or line.startswith("#"):
            violations.append(
                f"titre Markdown interdit dans le bloc (ligne {idx + 1}) — le bloc "
                f"qualitatif est de la prose, pas du markdown structuré : "
                f"{line.strip()[:60]!r}"
            )
            break  # une seule occurrence suffit : le bloc est rejeté dans tous les cas

    # C4 : la forme écrite est le contournement du check « chiffres » — « huit
    # sessions » passe là où « 8 sessions » est refusé. Ancrée sur une unité
    # comptable (+ cue pour les petits nombres) pour ne pas tuer « un constat ».
    for pattern in (_WRITTEN_COUNT_RE, _WRITTEN_COUNT_CUE_RE, _WRITTEN_COUNT_STRONG_CUE_RE):
        match = pattern.search(text)
        if match:
            line = text.count("\n", 0, match.start()) + 1
            violations.append(
                f"nombre écrit interdit (ligne {line}) — un décompte s'écrit en chiffre "
                f"arabe dans une bande [N:…] résolue, pas en toutes lettres : "
                f"{match.group(0)[:60]!r}"
            )
            break

    findings_list = (findings or {}).get("findings", []) if findings else []
    alerts = (insights or {}).get("alerts", []) if insights else []
    maint = (insights or {}).get("maintenance", {}).get("findings", []) if insights else []

    f_refs = {f"{f.get('session_id')}#{f.get('category')}" for f in findings_list}
    m_refs = {f.get("category") for f in maint}
    a_refs = {a.get("rule") for a in alerts}
    seen_f: set[str] = set()

    # Empty source references are not merely unknown: report them as malformed so
    # the author can repair the exact traceability marker instead of guessing.
    for match in re.finditer(r"\[([FMA]):([^\]]*)\]", text):
        kind, ref = match.group(1), match.group(2)
        line = text.count("\n", 0, match.start()) + 1
        if not ref.strip():
            violations.append(f"balise de source mal formée [{kind}:] — ligne {line}")
            continue
        if kind == "F":
            if ref not in f_refs:
                violations.append(
                    f"balise inconnue [F:{ref}] — ligne {line}, aucun finding correspondant"
                )
            else:
                seen_f.add(ref)
        elif kind == "M":
            if ref not in m_refs:
                violations.append(
                    f"balise inconnue [M:{ref}] — ligne {line}, aucun constat de maintenance"
                )
        elif kind == "A" and ref not in a_refs:
            violations.append(f"balise inconnue [A:{ref}] — ligne {line}, aucune alerte")

    # C2 : une bande [N:…] est une AFFIRMATION chiffrée, donc elle se résout comme
    # une balise de source : vers un scalaire réellement présent dans les artefacts
    # de la section 4. Pas d'assouplissement « petits entiers autorisés » (D2) — un
    # décompte juste par hasard reste un décompte inventé.
    band_index = number_band_index(findings, insights)
    for match in _NUMBER_BAND_RE.finditer(text):
        line = text.count("\n", 0, match.start()) + 1
        raw = match.group(1).strip()
        if not raw:
            violations.append(f"bande de chiffre mal formée [N:] — ligne {line}")
            continue
        head_token = raw.split()[0]
        if not re.fullmatch(r"\d+(?:[.,]\d+)?", head_token):
            violations.append(
                f"bande de chiffre non numérique [N:{raw[:40]}] — ligne {line}, "
                "un décompte s'écrit en chiffre arabe (la forme écrite est refusée)"
            )
        elif not _band_resolves(raw, band_index):
            violations.append(
                f"bande de chiffre non résolue [N:{raw[:40]}] — ligne {line}, aucun "
                "scalaire correspondant dans weekly-quality-findings / weekly-insights"
            )

    for f in findings_list:
        if f.get("severity") == "high":
            ref = f"{f.get('session_id')}#{f.get('category')}"
            if ref not in seen_f:
                coverage.append(f"constat high non couvert par le bloc : [F:{ref}]")

    return violations, coverage


