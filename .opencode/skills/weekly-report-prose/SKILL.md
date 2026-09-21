---
name: weekly-report-prose
description: "Rédige le bloc « Constats qualitatifs » du rapport sous contrat anti-hallucination (sources closes). Use when high/medium findings exist."
metadata:
  authored_by: opencode-weekly-advisor
  skill_class: pipeline-step
---

# Weekly Report Prose — étape 7b

Tu écris UNIQUEMENT le bloc « Constats qualitatifs » (section 4) du rapport, dans
`weekly-report-blocks-<date>.md`. Tout le reste est assemblé par du code
(`report-prep` → draft → `report-assemble`).

## ⛔ Contraintes bloquantes (à lire AVANT d'écrire, pas après)

Quatre seuils. Chacun fait **rejeter** le bloc par le code : le rapport retombe
alors sur le brouillon automatique et la section 4 perd ta prose. Sur le run du
2026-10-01, un bloc de 71 lignes a été rejeté de la sorte, et corriger après coup
a coûté un `report-prep` complet.

| # | Contrainte | Seuil | Vérifié par |
|---|------------|-------|-------------|
| 1 | **Longueur** | **≤ 60 lignes**, cible ~40 | `validate_llm_blocks` |
| 2 | **Volume** | **≥ 40 mots** | `_assemble_quality_block` (`blocks_min_words`) |
| 3 | **IDs de source** | **`[F:<session_id>#<cat>]` avec l'ID COMPLET** — jamais tronqué, jamais abrégé | `validate_llm_blocks` |
| 4 | **Pas de titre** | **1re ligne non vide ≠ `##`** — commencer par une phrase ou un `###` | `validate_llm_blocks` |

Le seuil 3 est un vice de fond : un ID tronqué ne résout dans aucun artefact
d'entrée, la balise est donc traitée comme **inconnue** et le bloc rejeté.
Les seuils 1 et 2 sont des bornes de gabarit, mais leur dépassement coûte
exactement le même fallback — ils ne sont pas négociables.

**Vérifie avant d'assembler** : appelle `weekly_report_blocks_check` (§ « Après
écriture »). Il rend le verdict et les violations numérotées **sans rien
consommer**, donc tu peux corriger et re-checker sans repayer un `report-prep`.

## Écriture du bloc

Le fichier doit être écrit à côté des artefacts du run actif (**utiliser le chemin
absolu retourné par le tool précédent ; jamais de Glob depuis la racine sur l'arbre
`reports/`**). Le brouillon déterministe
(`weekly-report-blocks-auto-<date>.md`, produit par `report-blocks-draft`) est le filet
de sécurité : si le bloc est rejeté ou absent, le rapport sort quand même avec lui.

## Déclenchement — prose OPTIONNELLE

Écrire la prose **seulement si** contenu qualitatif (findings étape 3, alertes ou
maintenance non vides). Sinon, NE PAS créer le fichier (le brouillon auto suffit, coût zéro).

## Contrat anti-hallucination (vérifié par le code — toute violation ⇒ rejet + fallback auto)

> Les quatre seuils bloquants (taille, volume, ID complets, pas de titre) sont énoncés **en tête de
> skill**, dans « ⛔ Contraintes bloquantes ». Ne pas les redécouvrir ici.

1. **AUCUN chiffre** dans le texte visible (les balises de citation sont exclues du check)
2. **Balise de source sur chaque affirmation** :
   - `[F:<session_id_complet>#categorie]` — finding étape 3 ; **l'ID de session doit être
     complet** (ex. `ses_01J7XQ4...`, jamais tronqué — un ID raccourci est rejeté par
     `report-assemble`)
   - `[M:categorie]` — maintenance (R1-R4)
   - `[A:regle]` — alerte insights
   - chaque balise doit exister dans les JSON d'entrée
   - `[N:<nombre> <unité>]` — **bande de chiffre** : le seul moyen d'écrire un nombre.
     Voir « Écrire un chiffre » ci-dessous.
3. **Traçabilité des rejets** : chaque balise inconnue ou mal formée est rejetée
   avec son numéro de ligne ; corriger la balise signalée, sans réécrire les données
   d'entrée. Une balise vide (`[F:]`, `[M:]`, `[A:]`) ne constitue jamais une source.
4. Tout finding `severity: high` doit être cité au moins une fois (sinon warning annexe)

## Écrire un chiffre — la bande `[N:…]` (obligatoire)

La règle « aucun chiffre » et la règle « la prose doit être vérifiable » ne sont pas
contradictoires : **le nombre ne vient pas de toi, il vient d'une bande que le code
résout**.

```
[N:<nombre> <unité libre>]
```

- `[N:3 findings]`, `[N:19 constats]`, `[N:30.0 dollars]`, `[N:2 skills]`
- Le **nombre** doit résoudre vers un scalaire RÉELMENT présent dans les artefacts du
  run (`weekly-quality-findings-<date>.json` et `weekly-insights-<date>.json`, plus
  les cardinances dérivées : nombre de findings, d'alertes, de constats de
  maintenance, par sévérité). L'**unité** est libre et n'est pas validée.
- Le rendu remplace la bande par le chiffre nu, en Markdown comme en HTML :
  `[N:3 findings]` → `3 findings`. Le lecteur ne voit jamais le marqueur.
- Toute bande non résolue est une **violation** : le bloc est rejeté et le rapport
  retombe sur le brouillon automatique. `[N:19 findings]` avec 3 findings dans les
  artefacts est refusée — le nombre doit venir des données, pas de ton estimation.
- La forme écrite est refusée aussi : écris `[N:8 sessions]`, jamais « huit
  sessions » ni « une vingtaine de sessions » (les nombres jusqu'à sept ne sont
  refusés qu'en position de décompte, mais évite la zone grise).

L'index des valeurs acceptées est publié par `report-assemble` dans
`weekly-report-gates-<date>.json`, clé `number_bands` (`scalars` borné à 200
entrées + `scalars_total`, et `counts` en entier). En cas de rejet, c'est là
qu'on vérifie ce qui était admissible — et `scalars_total` dit si l'affichage
est tronqué, donc si l'absence d'une valeur dans la liste est réelle :

```bash
# valeurs admissibles pour une bande [N:…] — LECTURE SEULE, jamais d'écriture
python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['number_bands'])" \
  <run_dir>/weekly-report-gates-<date>.json
```

## Parité déterministe des rapports

Les findings de cohérence et le manifeste `skill-curate-<date>.json` sont des sources
de vérité JSON communes aux sorties Markdown et HTML. Ne pas reformuler, compter ou
déduire leurs décisions dans la prose : le code rend les mêmes entrées dans les deux
formats, y compris les détails `skipped_details` et le statut de chaque décision.

## Règles d'écriture

- **Sources closes** : la prose s'écrit uniquement depuis le brouillon auto + findings +
  insights — jamais de mémoire, jamais de titre/session/ticket inventé
- Ordre : constats triés sévérité DESC puis impact ; low groupés en liste compacte
- **Aucun chiffre** (le template rend les nombres — ne pas les recopier, même approchés)
- Pas de citation verbatim de transcript (paraphrase ≤ 200 caractères)
- Ne jamais recommander l'installation d'un item écosystème à source unique et non nouveau
  (la sélection est du code)
- Auto-relecture AVANT assemble : relire le bloc et recouper chaque affirmation contre
  les JSON (les balises [F]/[M]/[A] doivent toutes exister)
- Doute ou séquence non étayée → ne pas mentionner (l'omission vaut mieux que l'invention)

## Après écriture

1. **`weekly_report_blocks_check`** — valide `weekly-report-blocks-<date>.md` et rend
   le verdict + les violations **numérotées, avec leur numéro de ligne**. rc=0 conforme ;
   rc=1 non conforme ; rc=2 fichier absent/illisible. ⚠ Ce check **ne consomme rien** :
   ni le bloc ni le draft. Corrige le fichier, re-checke, et seulement ensuite assemble.
2. `report-assemble` injecte le bloc dans le draft → `weekly-report-<date>.md` (le signal
   du cron). ⚠ Un assemble réussi **supprime le draft (consommé)** : pour un nouvel assemble
   (ex. après édition du bloc), relancer `report-prep` d'abord — sinon erreur « draft inexistant ».
