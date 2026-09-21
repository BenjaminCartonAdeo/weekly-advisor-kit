"""Allowlist bloquante du gate sécurité — source unique MD/HTML.

Les trois règles bloquantes et leurs paraphrases publiques vivaient en double
(``report._BLOCKING_SECURITY_RULES`` / ``html_report._BLOCKING_RULES_HTML``)
parce que ``report`` importe ``html_report`` : un import inverse serait un
cycle. Ce module casse la duplication sans en créer un.
"""

from __future__ import annotations

# Ordre = tri alphabétique des trois identifiants : stable pour le rendu MD
# (``sorted``) comme pour le HTML (énumération directe).
BLOCKING_RULES_ORDERED: tuple[str, str, str] = (
    "mcp-tool-poisoning",
    "memory-write-unscoped",
    "unbounded-delegation",
)
BLOCKING_RULES: frozenset[str] = frozenset(BLOCKING_RULES_ORDERED)

SECURITY_PARAPHRASE: dict[str, str] = {
    "mcp-tool-poisoning": "Contenu outil non fiable — revue humaine avant usage.",
    "unbounded-delegation": "Délégation trop large — restreindre le périmètre.",
    "memory-write-unscoped": "Écriture mémoire hors périmètre — revue requise.",
}
