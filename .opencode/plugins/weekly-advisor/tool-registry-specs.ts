import type { SimpleToolSpec } from "./types.ts"

import {
  simpleTool,
} from "./tool-registry-common.ts"
export const OPENING_SPECS: readonly SimpleToolSpec[] = [
  {
    name: "weekly_run",
    description:
      "Étape 1 du weekly-advisor : collecte télémétrique complète (run). Écrit weekly-summary-<date>.json. L'ancre est lue/créée/rafraîchie dans <output_dir>/anchor-last.txt.",
    subcommand: "run",
    timeoutMs: 1_800_000,
    lookback: true,
  },
  {
    name: "weekly_releases",
    description: "Étape 2 : veille écosystème (releases). Écrit weekly-ecosystem-<date>.json.",
    subcommand: "releases",
    timeoutMs: 900_000,
    lookback: true,
  },
  {
    name: "weekly_watch_context",
    description:
      "Étape 2.5 : inventaire déterministe du worktree et crosswalk marché/existant. Écrit weekly-watch-context-<date>.json. SÉQUENTIEL : nécessite weekly-ecosystem-<date>.json de l'étape 2 — exécuter weekly_releases d'abord, jamais en parallèle.",
    subcommand: "watch-context",
    timeoutMs: 120_000,
  },
  {
    name: "weekly_watch_distill",
    description:
      "Étape 2.2 : distillation déterministe de l'écosystème vers ~30 fiches candidates (scoring, screening sécurité, mémoire inter-run). Écrit watch-candidates-<date>.json et watch-memory-digest-<date>.json. SÉQUENTIEL : exécuter après weekly_releases, avant weekly_watch_context ; exit 2 = écosystème absent ou étape désactivée (config watch_distill.enabled) — dégradation attendue, le flux aval retombe sur l'écosystème complet.",
    subcommand: "watch-distill",
    timeoutMs: 120_000,
  },
  {
    name: "weekly_watch_validate",
    description:
      "Étape 3.6 : valide les findings bruts de la veille contre l'inventaire déterministe. Écrit weekly-watch-findings-<date>.json.",
    subcommand: "watch-validate",
    timeoutMs: 120_000,
  },
  {
    name: "weekly_audit_candidates",
    description:
      "Étape 3 (1/2) : sélection déterministe des sessions à auditer (audit-candidates) → weekly-audit-candidates-<date>.json (audited/unaudited, plafond audit_max_sessions).",
    subcommand: "audit-candidates",
    timeoutMs: 120_000,
  },
]

/** Outil simple de l'étape 5, seul avant la remédiation harness. */
export const HARNESS_SPEC: SimpleToolSpec = {
  name: "weekly_harness",
  description: "Étape 5 : lint de .opencode/ (harness-eval). Écrit weekly-harness-digest-<date>.json.",
  subcommand: "harness",
  timeoutMs: 900_000,
}

// ---------------------------------------------------------------------------
// Descriptions mutualisées — fragments de contrat écrits UNE fois et composés
// dans les descriptions qui les détiennent.
//
// Raison d'être (dérive du run cron 2026-10-01) : ces règles vivaient
// uniquement dans `report.py` et dans les skills. Un agent qui n'avait que la
// description de l'outil devait relire `report.py` par tranches — 10 lectures de
// slicing sur ce seul run — pour reconstruire les contrats de section 4 et la
// sémantique des codes de sortie. La description est le premier canal
// (« l'agent n'a pas à deviner ») ; elle doit donc porter le contrat, pas un
// renvoi.
//
// Conséquence de conception : un fragment partagé est seulement partagé s'il est
// cité par au moins deux descriptions. `scripts/tests/plugin-v2.test.mjs`
// (« parité des fragments partagés ») verrouille l'égalité **verbatim** entre
// le fragment et chaque description qui l'utilise : on ne peut plus le reformuler
// d'un côté et laisser l'autre obsolète.
// ---------------------------------------------------------------------------

/**
 * Consumption du draft. Cité par `weekly_report_assemble` (qui consomme) et par
 * `weekly_report_contract` (qui expose la règle sans rien écrire).
 *
* ⚠ CONTRAINTE CI : `scripts/check-flow-docs.mjs` surface 4 impose que la phrase
 * « relancer weekly_report_prep » apparaisse dans les 400 caractères qui suivent
 * la PREMIÈRE mention de `weekly_report_assemble` dans ce fichier. Le fragment se
 * nomme donc lui-même, et la phrase le suit immédiatement : la fenêtre est
 * portée par le TEXTE DU FRAGMENT, pas par ce commentaire, ni par une mention
 * éparse plus bas dans la liste des outils. Le test « CI : la regex de
 * check-flow-docs surface 4 » verrouille cet invariant.
 */
export const DRAFT_CONSUMED_NOTE =
  "⚠ `weekly_report_assemble` consomme le draft à chaque réussite : " +
  "pour un nouvel assemble, relancer weekly_report_prep d'abord."

/**
 * Les deux fichiers de section 4, et lequel est lequel. Source de vérité :
 * `_report_assemble_inner` (`weekly-report-blocks-auto-<date>.md` = filet de
 * sécurité déterministe, `weekly-report-blocks-<date>.md` = prose agent 7b).
 */
export const SECTION_4_FILES =
  "Deux fichiers, deux rôles : `weekly-report-blocks-<date>.md` = ta prose agent (7b), " +
  "validée par l'assemble ; `weekly-report-blocks-auto-<date>.md` = brouillon déterministe " +
  "de repli, produit par weekly_report_blocks_draft, jamais validé."

/**
 * Contrat du fichier de section 4 écrit par l'agent (`weekly-report-blocks-<date>.md`).
 * Chaque clause est appliquée par `validate_llm_blocks` / `_assemble_quality_block`
 * — toute violation fait REJETER le bloc, qui retombe alors sur le brouillon auto.
 */
export const SECTION_4_FILE_CONTRACT =
  "CONTRAT DU FICHIER (validate_llm_blocks, violations ⇒ bloc rejeté ⇒ fallback auto) : " +
  "≤ 60 lignes (cible ~40) ; ≥ 40 mots (config.py:190 blocks_min_words=40) ; " +
  "tags de source `[F:<session_id_complet>#<category>]` — jamais un id tronqué, la balise " +
  "doit résoudre exactement dans weekly-quality-findings-<date>.json —, `[A:<rule>]` pour " +
  "chaque alerte (weekly-insights), `[M:<category>]` pour chaque constat de maintenance ; " +
  "chaque finding de sévérité `high` doit être cité (sinon : coverage warning dans l'annexe) ; " +
  "aucun chiffre libre dans le texte visible (seules dates ISO, pourcentages et versions " +
  "sont tolérés) — le bloc ne cite que des catégories et des sévérités ; " +
  "AUCUN titre Markdown dans le bloc, à aucun niveau (`#`, `##`, `###`, …) : le bloc " +
  "qualitatif est de la prose pure — commence par une phrase (le gabarit fournit déjà " +
  "le titre de la section 4)."

/**
 * Sémantique des codes de sortie des étapes de rapport. `rc=1` est une
 * **dégradation visible**, pas un échec : c'est exactement la distinction que le
 * rapport du 2026-10-01 n'a pas su faire (annoncé « Veille 265 nouveautés » alors
 * que la branche veille rendait rc=1).
 */
export const REPORT_RC_SEMANTICS =
  "CODES DE SORTIE : rc=0 nominal ; rc=1 = dégradation warn-only, le livrable EST produit " +
  "(rc=1 ≠ échec du pipeline) ; rc≥2 = échec, aucun livrable. " +
  "Ne jamais convertir un rc=1 en « échec » ni le masquer dans le rapport."

/** Outils simples de l'étape 6 (insights) à 7c (assemblage). */
export const REPORTING_SPECS: readonly SimpleToolSpec[] = [
  {
    name: "weekly_insights",
    description: "Étape 6 : deltas, alertes et maintenance. Écrit weekly-insights-<date>.json.",
    subcommand: "insights",
    timeoutMs: 300_000,
  },
  {
    name: "weekly_draft_candidates",
    description:
      "Étape 4 (1/2) : candidats à l'auto-drafting (draft-candidates) → weekly-draft-candidates-<date>.json (skill-candidate / command-candidate / command-improvement, plafonné).",
    subcommand: "draft-candidates",
    timeoutMs: 120_000,
  },
  {
    name: "weekly_report_prep",
    description:
      "Étape 7a (1/2) : rend le gabarit déterministe du rapport (report-prep → " +
      "weekly-report-draft-<date>.md). " +
      "PRODUIT : le draft, sections déterministes + le marqueur `<!-- QUALITY_BLOCK -->` " +
      "(s'il manque, l'assemble sort en rc=2). " +
      "CONSOMME : weekly-summary-<date>.json (obligatoire), plus les artefacts optionnels " +
      "du run (insights, digest harness, findings qualité, ecosystem, contexte de veille) " +
      "lus au gate `validate_required_artifacts`. " +
      "NE FAIT PAS : n'écrit aucune prose — la section 4 reste le marqueur vide ; " +
      "ne valide pas le bloc de constats (pas de gate anti-hallucination ici) ; " +
      "ne produit pas le rapport final. " +
      `rc : 0 nominal, 1 = résumé dégradé (livrable écrit), 2 = summary absente ou gate artefacts ≠ pass (rien). ${REPORT_RC_SEMANTICS}`,
    subcommand: "report-prep",
    timeoutMs: 120_000,
  },
  {
    name: "weekly_report_blocks_draft",
    description:
      "Étape 7a (2/2) : écrit le BROUILLON DÉTERMINISTE des blocs (report-blocks-draft → " +
      "weekly-report-blocks-auto-<date>.md). Zéro LLM, zéro jugement : agrège alertes, " +
      "maintenance, sessions coûteuses, findings qualité et règles harness. " +
      "NE FAIT PAS : n'écrit pas `weekly-report-blocks-<date>.md` (c'est ta prose 7b, " +
      "que ce tool n'a pas le droit de produire) ; ne valide rien — son propre contenu est " +
      "explicitement marqué « brouillon automatique, revue humaine requise » et ne passe " +
      "jamais la gate de prose. " +
      `${SECTION_4_FILES} ` +
      `${SECTION_4_FILE_CONTRACT} ` +
      "Ce brouillon n'est que le filet de sécurité : pour que la section 4 soit marquée " +
      "`prose agent (7b LLM)`, écris toi-même le fichier de prose et respecte le contrat ci-dessus. " +
      `rc : 0 nominal, 1 = résumé dégradé (fichier écrit), 2 = summary absente (rien). ${REPORT_RC_SEMANTICS}`,
    subcommand: "report-blocks-draft",
    timeoutMs: 120_000,
  },
  {
    name: "weekly_report_blocks_check",
    // `REPORT_RC_SEMANTICS` n'est délibérément PAS composé ici : ce fragment
    // définit rc=1 comme une dégradation warn-only « le livrable EST produit ».
    // Ici rc=1 signifie l'inverse — le bloc sera REJETÉ par l'assemble. Réutiliser
    // le fragment dirait donc à l'agent l'inverse de la vérité ; les rc de ce
    // check sont énoncés dans la description, sans vocabulaire « warn-only ».
    description:
      "Étape 7b.1 : VALIDE ta prose `weekly-report-blocks-<date>.md` (report-blocks-check) " +
      "SANS la consommer. Même validateur que l'assemble (`validate_llm_blocks`) : verdict + " +
      "liste NUMÉROTÉE des violations, chacune avec son numéro de ligne. " +
      `CONTRÔLE : ${DRAFT_CONSUMED_NOTE} ` +
      "C'est précisément pour cela que ce check existe : corriger un bloc rejeté ne coûte " +
      "PAS un report-prep complet (run 2026-10-01 : 2 assemble ratés + 1 prep supplémentaire). " +
      "APPELLE-LE après avoir écrit la prose (7b), AVANT l'assemble (7c), et réécris le " +
      "fichier entre deux checks — il reste en place. " +
      `${SECTION_4_FILES} ` +
      `${SECTION_4_FILE_CONTRACT} ` +
      "EN PLUS : le plancher de mots (contract.minWords) est appliqué ici comme par " +
      "`_assemble_quality_block`, sinon ce check validerait un bloc que l'assemble rejette — " +
      "le faux vert qu'on veut supprimer. " +
      "rc : 0 conforme (assemblable tel quel) ; 1 = NON conforme, violations listées et le " +
      "bloc sera rejeté par l'assemble (section 4 en `auto_draft_fallback`) ; " +
      "2 = prose absente ou illisible (rien à valider). " +
      "Les coverage warnings (`high` non cité) ne font PAS baisser le rc : ils signalent " +
      "une omission, jamais une invention. " +
      "RIEN N'EST ÉCRIT, RIEN N'EST SUPPRIMÉ : ni le bloc ni le draft ne sont touchés.",
    subcommand: "report-blocks-check",
    timeoutMs: 120_000,
  },
  {
    name: "weekly_report_assemble",
    description:
      "Étape 7c : assemble le rapport final (report-assemble → weekly-report-<date>.md, " +
      "+ `weekly-report-gates-<date>.json`). " +
      `CONTRÔLE : ${DRAFT_CONSUMED_NOTE} ` +
      "rc=1 ≠ échec du pipeline — ce sont des déGRADATIONS WARN-ONLY, le rapport EST écrit ; " +
      "les 3 cas à connaître : (1) findings security/critical dans le digest harness → gate " +
      "security.status=warn, rapport écrit ; (2) JOIN partiel (audit dynamique manquant) → " +
      "rapport écrit sans ces sessions ; (3) rc=1 propagé du résumé (branche veille dégradée) " +
      "ou du rendu HTML best-effort → rapport markdown écrit. " +
      "rc≥2 = AUCUN rapport (draft absent/consommé, artefact requis manquant ou illisible, " +
      "marqueur QUALITY_BLOCK absent, manifeste de curation malformé). " +
      "État machine-readable de chaque gate : weekly-report-gates-<date>.json. " +
      `${SECTION_4_FILE_CONTRACT}`,
    subcommand: "report-assemble",
    timeoutMs: 120_000,
  },
]

/** Outils simples non ancrés de l'annexe finale. */
export const ANNEX_SPECS: readonly SimpleToolSpec[] = [
  {
    name: "weekly_self_cost",
    description: "Étape 8 (annexe) : coût de la fenêtre du run (self-cost).",
    subcommand: "self-cost",
    timeoutMs: 120_000,
    anchored: false,
  },
  {
    name: "weekly_doctor",
    description: "Diagnostic du kit : vérifie opencode, harness-eval, git, gh, DB et la config (doctor).",
    subcommand: "doctor",
    timeoutMs: 120_000,
    anchored: false,
  },
]

// ---------------------------------------------------------------------------
// Outils à comportement propre : argv ou pré-traitement hors du cas simple.
// ---------------------------------------------------------------------------

/** Pré-flight : purement in-process, `cliSubcommand` absent, timeout nul. */
