---
name: weekly-report-prose
description: Rédaction du bloc prose du rapport hebdomadaire (étape 7b du weekly-advisor) — contrat anti-hallucination, balises de source, zéro chiffre, sources closes.
metadata:
  authored_by: opencode-weekly-advisor
  skill_class: pipeline-step
---

# Weekly Report Prose — étape 7b

Le rapport final est assemblé par du code (`report-prep` → draft → `report-assemble`).
Cette étape écrit UNIQUEMENT le bloc « Constats qualitatifs » (section 4) dans
`weekly-report-blocks-<date>.md`, à côté des artefacts du run actif (**utiliser le
chemin absolu retourné par le tool précédent ; jamais de Glob depuis la racine sur
l'arbre `reports/`**). Le brouillon déterministe
(`weekly-report-blocks-auto-<date>.md`, produit par `report-blocks-draft`) est le filet
de sécurité : si le bloc est rejeté ou absent, le rapport sort quand même avec lui.

## Déclenchement — prose OPTIONNELLE

Écrire la prose **seulement si** contenu qualitatif (findings étape 3, alertes ou
maintenance non vides). Sinon, NE PAS créer le fichier (le brouillon auto suffit, coût zéro).

## Contrat anti-hallucination (vérifié par le code — toute violation ⇒ rejet + fallback auto)

1. **AUCUN chiffre** dans le texte visible (les balises de citation sont exclues du check)
2. **Balise de source sur chaque affirmation** :
   - `[F:<session_id_complet>#categorie]` — finding étape 3 ; **l'ID de session doit être
     complet** (ex. `ses_01J7XQ4...`, jamais tronqué — un ID raccourci est rejeté par
     `report-assemble` (v6.0.k F5))
   - `[M:categorie]` — maintenance (R1-R4)
   - `[A:regle]` — alerte insights
   - chaque balise doit exister dans les JSON d'entrée
3. **Taille ≤ 60 lignes** (cible ~40) et ≥ 40 mots
4. Tout finding `severity: high` doit être cité au moins une fois (sinon warning annexe)

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

`report-assemble` injecte le bloc dans le draft → `weekly-report-<date>.md` (le signal
du cron) **et rend le rapport HTML interactif** (`reports/html/weekly-report-latest.html`,
v6.1 — ton bloc y apparaît section « Constats qualitatifs » avec légende F/M/A et
distribution par catégorie). ⚠ Un assemble réussi **supprime le draft (consommé)** : pour un nouvel assemble
(ex. après édition du bloc), relancer `report-prep` d'abord — sinon erreur « draft inexistant ».