/**
 * weekly-advisor — registre neutre et immuable des outils.
 *
 * Source de vérité unique pour : noms, descriptions (byte-for-byte), schémas de
 * champs, ordre d'exposition, timeouts, ancrage, sous-commandes CLI, valeurs par
 * défaut et handlers. Les adaptateurs V1 (`@opencode-ai/plugin`) et V2 s'y
 * branchent sans jamais redéclarer un outil.
 *
 * **Aucune dépendance SDK.** Ni `@opencode-ai/plugin`, ni variable module : un
 * handler ne reçoit que son `input`, un {@link AbortableRuntimeApi} et un
 * `signal` optionnel. Les seules imports d'exécution hors contrat sont
 * `node:fs` / `node:path` — primitives plateforme (existence de fichier,
 * jointure de chemins) et non adaptateurs, déplacées à l'identique depuis la
 * source V1. L'import de `runtime.ts` est un `import type` de vérification
 * (§ « Conformité à la compilation ») : effacé à l'exécution.
 *
 * **Verrouillé.** {@link deepFreeze} gèle récursivement définitions, champs,
 * valeurs d'enum, valeurs par défaut et le registre lui-même : un adaptateur qui
 * muterait une description corromprait le contrat pour tous les autres.
 *
 * Toute divergence avec `scripts/fixtures/weekly-advisor-tool-contract.json`
 * doit faire échouer `scripts/tests/plugin-registry.test.mjs`.
 */
import fs from "node:fs"
import path from "node:path"

import type { WeeklyRuntime } from "./runtime.ts"
import type {
  FieldSpec,
  PortabilityOutcome,
  RuntimeApi,
  SimpleToolSpec,
  StagedPayload,
  ToolDefinition,
} from "./types.ts"

/**
 * Gel récursif. Les fonctions (`run`) ne sont pas des objets : elles sont
 * laissées telles quelles, et le `Object.isFrozen` évite de retraverser un objet
 * déjà gelé.
 */
function deepFreeze<T>(value: T): T {
  if (value === null || typeof value !== "object") return value
  if (Object.isFrozen(value)) return value
  Object.freeze(value)
  for (const child of Object.values(value as Record<string, unknown>)) deepFreeze(child)
  return value
}

/** Lecture d'un champ optionnel, sans coercition : `undefined` si absent ou d'un autre type. */
function optionalString(input: Readonly<Record<string, unknown>>, key: string): string | undefined {
  const value = input[key]
  return typeof value === "string" ? value : undefined
}

/**
 * Champ obligatoire, refusé explicitement. Le schéma de l'outil le déclare
 * `required` : un adaptateur conforme n'atteint jamais ce chemin. Le cas
 * contraire est un bug d'adaptateur, et un message nommé vaut mieux qu'un
 * `undefined` muet dans l'`argv` du moteur. La chaîne vide est conservée : elle
 * est un passage à travers de la source V1, pas une invention d'ici.
 */
function requiredString(input: Readonly<Record<string, unknown>>, key: string, tool: string): string {
  const value = input[key]
  if (typeof value !== "string") throw new Error(`${tool}: champ obligatoire manquant ou non textuel : ${key}`)
  return value
}

// ---------------------------------------------------------------------------
// Annulation.
//
// `ToolDefinition.run` reçoit déjà un `signal` optionnel (`types.ts`). Le
// registre le relaie aux trois primitives du runtime qui savent en faire usage :
// `runCli` (enfant tué), `stageJsonPayload` (aucun répertoire temporaire
// résiduel) et `runPortabilityGate` (spawn `skill-verify` tué, une annulation
// n'étant pas un verdict de gate).
//
// Le contrat d'exécution est ici **élargi, pas remplacé** : `AbortableRuntimeApi`
// reprend `RuntimeApi` à l'identique et ajoute le paramètre facultatif en fin de
// liste. Un `AbortableRuntimeApi` reste donc un `RuntimeApi` valide pour tout
// consommateur du contrat neutre ; symétriquement, un runtime d'exécution sans
// support d'annulation ne satisfait **pas** `AbortableRuntimeApi`, et c'est
// voulu : le typage force l'adaptateur à fournir la capacité plutôt que de la
// laisser tomber en silence.
//
// `signal` reste par ailleurs facultatif et n'apparaît dans les `fields` d'aucun
// outil — il est invisible du contrat d'arguments, donc de la fixture. Rien à
// ajouter à `weekly-advisor-tool-contract.json`.
// ---------------------------------------------------------------------------

/**
 * Surface d'exécution attendue par les handlers : {@link RuntimeApi} plus la
 * capacité d'annulation. Implémentée par `WeeklyRuntime` — voir les
 * vérifications de conformité en bas de fichier.
 */
export interface AbortableRuntimeApi extends RuntimeApi {
  /** Exécute une sous-commande ; `signal` tue l'enfant et rejette la promesse. */
  runCli(args: readonly string[], timeoutMs: number, signal?: AbortSignal): Promise<string>;
  /** Stage un payload JSON ; `signal` empêche tout répertoire résiduel. */
  stageJsonPayload(payload: string, label: string, signal?: AbortSignal): StagedPayload;
  /**
   * Gate de portabilité ; `signal` tue le scanner. Une annulation n'est pas un
   * verdict : elle remonte à l'appelant au lieu d'être convertie en refus.
   */
  runPortabilityGate(
    file: string,
    kind: "skill" | "command",
    signal?: AbortSignal,
  ): Promise<PortabilityOutcome>;
}

// ---------------------------------------------------------------------------
// Champs et valeurs par défaut partagés. Gelés et réemployés tels quels par
// plusieurs outils : l'identité n'est pas observable (le contrat compare des
// valeurs), le gel empêche toute dérive en cours de route.
// ---------------------------------------------------------------------------

const ANCHOR_FIELD: FieldSpec = deepFreeze({
  name: "anchor",
  kind: "string",
  required: false,
  description: "ISO-8601 override (rare)",
})

const LOOKBACK_FIELD: FieldSpec = deepFreeze({
  name: "lookback_days",
  kind: "number",
  required: false,
  description: "Override de run : fenêtre en jours (rare ; défaut = config)",
})

/** Mode par défaut de `weekly_harness_remediate` — seule valeur de défaut pilotée d'ici. */
const HARNESS_REMEDIATE_DEFAULT_MODE = "dry-run"

/**
 * Valeurs par défaut du contrat, clés pointées à l'identique de la fixture.
 * Trois d'entre elles sont appliquées **par le moteur** et non pilotées d'ici :
 * elles documentent le comportement observable (`skill_curate.stale_days`,
 * `show_session.include_children`, `commit_draft.gate_kind_command`).
 * `lookback_days: null` = aucun défaut, la fenêtre vient de la config.
 */
export const TOOL_DEFAULTS: Readonly<Record<string, string | number | boolean | null>> = deepFreeze({
  "harness_remediate.mode": HARNESS_REMEDIATE_DEFAULT_MODE,
  "skill_curate.mode": "dry-run",
  "skill_curate.stale_days": 90,
  "show_session.include_children": false,
  "commit_draft.gate_kind_command": "skipped",
  lookback_days: null,
})

/** Timeout de `weekly_show_session` — gelé. */
const SHOW_SESSION_TIMEOUT_MS = 300_000
/** Timeout de `weekly_harness_remediate` — gelé. */
const HARNESS_REMEDIATE_TIMEOUT_MS = 900_000
/** Timeout de `weekly_commit_draft` — gelé. */
const COMMIT_DRAFT_TIMEOUT_MS = 120_000
/** Timeout de `weekly_skill_curate` — gelé. */
const SKILL_CURATE_TIMEOUT_MS = 900_000

// ---------------------------------------------------------------------------
// Cas majoritaire : 1 sous-commande → 1 appel CLI.
// `anchored !== false` ⇒ champ `anchor` + drapeau `--anchor` ; `lookback`
// ⇒ champ `lookback_days` + drapeau `--lookback-days` (fenêtre en jours, jamais
// une édition de config).
// ---------------------------------------------------------------------------
function simpleTool(spec: SimpleToolSpec): ToolDefinition {
  const anchored = spec.anchored !== false
  const fields: FieldSpec[] = anchored ? [ANCHOR_FIELD] : []
  if (spec.lookback) fields.push(LOOKBACK_FIELD)
  return deepFreeze<ToolDefinition>({
    name: spec.name,
    description: spec.description,
    fields,
    timeoutMs: spec.timeoutMs,
    anchored,
    cliSubcommand: spec.subcommand,
    run: async (
      input: Readonly<Record<string, unknown>>,
      runtime: AbortableRuntimeApi,
      signal?: AbortSignal,
    ): Promise<string> => {
      const argv = [spec.subcommand]
      if (anchored) argv.push("--anchor", runtime.anchorArg(optionalString(input, "anchor")))
      // `withLookback` d'origine : la fenêtre n'est émise que si un nombre a été
      // fourni. `0` reste donc absent — comportement figé.
      const lookbackDays = input.lookback_days
      if (spec.lookback && typeof lookbackDays === "number" && lookbackDays) {
        argv.push("--lookback-days", String(lookbackDays))
      }
      return runtime.runCli(argv, spec.timeoutMs, signal)
    },
  })
}

/** Outils « 1 sous-commande → 1 appel CLI » de l'étape 1 à 3. */
const OPENING_SPECS: readonly SimpleToolSpec[] = [
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
const HARNESS_SPEC: SimpleToolSpec = {
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
const DRAFT_CONSUMED_NOTE =
  "⚠ `weekly_report_assemble` consomme le draft à chaque réussite : " +
  "pour un nouvel assemble, relancer weekly_report_prep d'abord."

/**
 * Les deux fichiers de section 4, et lequel est lequel. Source de vérité :
 * `_report_assemble_inner` (`weekly-report-blocks-auto-<date>.md` = filet de
 * sécurité déterministe, `weekly-report-blocks-<date>.md` = prose agent 7b).
 */
const SECTION_4_FILES =
  "Deux fichiers, deux rôles : `weekly-report-blocks-<date>.md` = ta prose agent (7b), " +
  "validée par l'assemble ; `weekly-report-blocks-auto-<date>.md` = brouillon déterministe " +
  "de repli, produit par weekly_report_blocks_draft, jamais validé."

/**
 * Contrat du fichier de section 4 écrit par l'agent (`weekly-report-blocks-<date>.md`).
 * Chaque clause est appliquée par `validate_llm_blocks` / `_assemble_quality_block`
 * — toute violation fait REJETER le bloc, qui retombe alors sur le brouillon auto.
 */
const SECTION_4_FILE_CONTRACT =
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
const REPORT_RC_SEMANTICS =
  "CODES DE SORTIE : rc=0 nominal ; rc=1 = dégradation warn-only, le livrable EST produit " +
  "(rc=1 ≠ échec du pipeline) ; rc≥2 = échec, aucun livrable. " +
  "Ne jamais convertir un rc=1 en « échec » ni le masquer dans le rapport."

/** Outils simples de l'étape 6 (insights) à 7c (assemblage). */
const REPORTING_SPECS: readonly SimpleToolSpec[] = [
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
const ANNEX_SPECS: readonly SimpleToolSpec[] = [
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
const PREFLIGHT_TOOL: ToolDefinition = deepFreeze<ToolDefinition>({
  name: "weekly_preflight",
  description: "Pré-flight déterministe du kit avant boot agent. Échec: rc=3, aucun run.",
  fields: [],
  timeoutMs: 0,
  anchored: false,
  run: async (
    _input: Readonly<Record<string, unknown>>,
    runtime: AbortableRuntimeApi,
    // Pré-flight purement in-process et synchrone : ni subprocess à tuer, ni
    // fichier temporaire à nettoyer. Le `signal` reste déclaré pour l'uniformité
    // de la signature des 21 handlers, mais il ne peut pas être honoré ici — le
    // contrat gelé dit « jamais d'exception », donc pas de `throwIfAborted`.
    _signal?: AbortSignal,
  ): Promise<string> => JSON.stringify(runtime.preflight()),
})

const SHOW_SESSION_TOOL: ToolDefinition = deepFreeze<ToolDefinition>({
  name: "weekly_show_session",
  description:
    "Étape 3 : transcrit une session (show-session) et retourne le texte structuré. " +
    "L'extrait est écrit à <run>/extracts/transcript-extract-<session_id>.md sous le run actif " +
    "(<output_dir>/runs/current/extracts/, fallback legacy <output_dir>/extracts/) : faire Read sur " +
    "CE FICHIER EXACT (chemin imprimé par le tool), JAMAIS sur le répertoire extracts/ lui-même " +
    "(opencode Read ne liste pas les dirs). Appeler le tool AVANT toute lecture d'extraits. " +
    "Utiliser après audit-candidates.",
  fields: [
    { name: "session_id", kind: "string", required: true, description: "id de session (ses_...)" },
    {
      name: "include_children",
      kind: "boolean",
      required: false,
      description: "inclure les subagents",
    },
  ],
  timeoutMs: SHOW_SESSION_TIMEOUT_MS,
  anchored: false,
  cliSubcommand: "show-session",
  run: async (
    input: Readonly<Record<string, unknown>>,
    runtime: AbortableRuntimeApi,
    signal?: AbortSignal,
  ): Promise<string> => {
    const { outputDir } = runtime.resolveEngine()
    // v6.0.k (F1) : extraits dans le run actif, legacy en fallback. E1 : l'alias
    // à jour du moteur est <output_dir>/runs/current — le symlink top-level
    // <output_dir>/current peut rester accroché à un run périmé (observé
    // 2026-08-25 : extracts écrits dans le run de la veille). Priorité
    // runs/current, puis current top-level (legacy).
    const runsCurrent = path.join(outputDir, "runs", "current")
    const legacyCurrent = path.join(outputDir, "current")
    const runBase = fs.existsSync(runsCurrent)
      ? runsCurrent
      : fs.existsSync(legacyCurrent)
        ? legacyCurrent
        : outputDir
    const argv = [
      "show-session",
      requiredString(input, "session_id", "weekly_show_session"),
      "--extract-dir",
      path.join(runBase, "extracts"),
    ]
    if (input.include_children === true) argv.push("--include-children")
    return runtime.runCli(argv, SHOW_SESSION_TIMEOUT_MS, signal)
  },
})

const HARNESS_REMEDIATE_TOOL: ToolDefinition = deepFreeze<ToolDefinition>({
  name: "weekly_harness_remediate",
  description:
    "Étape 5.5 : analyse/applique les propositions harness via une gate déterministe. " +
    "Dry-run par défaut ; aucun commit automatique.",
  fields: [
    {
      name: "proposal_file",
      kind: "string",
      required: true,
      description: "chemin absolu du JSON de propositions",
    },
    {
      name: "mode",
      kind: "enum",
      required: false,
      description: "dry-run par défaut ; apply uniquement après toutes les gates",
      values: ["dry-run", "apply"],
    },
    ANCHOR_FIELD,
  ],
  timeoutMs: HARNESS_REMEDIATE_TIMEOUT_MS,
  anchored: true,
  cliSubcommand: "harness-remediate",
  run: async (
    input: Readonly<Record<string, unknown>>,
    runtime: AbortableRuntimeApi,
    signal?: AbortSignal,
  ): Promise<string> =>
    runtime.runCli(
      [
        "harness-remediate",
        "--proposal",
        requiredString(input, "proposal_file", "weekly_harness_remediate"),
        "--mode",
        optionalString(input, "mode") ?? HARNESS_REMEDIATE_DEFAULT_MODE,
        "--anchor",
        runtime.anchorArg(optionalString(input, "anchor")),
      ],
      HARNESS_REMEDIATE_TIMEOUT_MS,
      signal,
    ),
})

/**
 * Préfixe de la ligne de faits que le moteur (`safe_git_write.py`) pose sur la
 * DEUXIÈME ligne d'un `commit-draft` réussi. Contrat inter-langages : la constante
 * Python `COMMIT_DRAFT_RESULT_MARKER` porte exactement cette chaîne.
 */
const COMMIT_DRAFT_RESULT_MARKER = "commit-draft-result: "

/**
 * Faits d'un commit de draft. `sha` est le SHA **COMPLET** (40 hex) ; `short_sha`
 * n'est que son affichage abrégé. Les deux sont exposés parce que la prose ne peut
 * pas porter le complet : `git commit` écrit `[branche sha-court] sujet` sur sa
 * première ligne, donc l'abréviation que l'agent y lisait n'était pas un SHA.
 */
type CommitDraftFacts = {
  kind: string
  file: string
  sha: string
  short_sha: string
  branch: string
  subject: string
}

/**
 * Sépare la prose du retour `commit-draft` de sa ligne de faits.
 *
 * `null` sur TOUTE autre sortie — refus `KO`, refus de gate, erreur, JSON
 * illisible, ou faits suivis d'une sortie inattendue : l'appelant rend alors la
 * sortie du CLI telle quelle, octet pour octet. Le structuré n'apparaît donc que
 * lorsqu'un commit a réellement eu lieu, ce qui est exactement la condition pour
 * qu'il existe un commit à enregistrer. Aucun fait n'est jamais deviné.
 */
function splitCommitDraftResult(
  cliOutput: string,
): { facts: CommitDraftFacts; message: string } | null {
  const lines = cliOutput.split("\n")
  const at = lines.findIndex((line) => line.trimStart().startsWith(COMMIT_DRAFT_RESULT_MARKER))
  if (at === -1) return null
  // Les faits doivent être la dernière ligne : une sortie qui continue après
  // ferait perdre de l'information, on rendrait alors le brut.
  if (lines.slice(at + 1).some((line) => line.trim() !== "")) return null
  try {
    const facts = JSON.parse(
      lines[at].trimStart().slice(COMMIT_DRAFT_RESULT_MARKER.length),
    ) as CommitDraftFacts
    if (typeof facts?.sha !== "string" || facts.sha === "") return null
    if (typeof facts?.subject !== "string" || facts.subject === "") return null
    return { facts, message: lines.slice(0, at).join("\n").trim() }
  } catch {
    return null
  }
}

const COMMIT_DRAFT_TOOL: ToolDefinition = deepFreeze<ToolDefinition>({
  name: "weekly_commit_draft",
  description:
    "Étape 4 : commit auto-rédigé d'un skill/command draft (commit-draft). " +
    "Gate de portabilité (skill-verify) avant chaque commit : erreur → refus + fix manuel requis, " +
    "warnings seuls → passage avec note, environnement défaillant (timeout/crash/sortie illisible) → refus. " +
    "kind=command → gate non applicable (harness-eval 7.10.1 : skills uniquement), skip explicite avec note dans le résultat. " +
    "1 commit par écriture, pré-checks git intégrés.",
  fields: [
    { name: "kind", kind: "enum", required: true, description: "", values: ["skill", "command"] },
    { name: "file", kind: "string", required: true, description: "chemin absolu du fichier draft" },
  ],
  timeoutMs: COMMIT_DRAFT_TIMEOUT_MS,
  anchored: false,
  cliSubcommand: "commit-draft",
  run: async (
    input: Readonly<Record<string, unknown>>,
    runtime: AbortableRuntimeApi,
    signal?: AbortSignal,
  ): Promise<string> => {
    const kind = requiredString(input, "kind", "weekly_commit_draft")
    const file = requiredString(input, "file", "weekly_commit_draft")
    // Gate portabilité — avant TOUT commit. Fichier absent : pas de gate,
    // l'erreur vient du CLI (comportement inchangé).
    let note = ""
    if (fs.existsSync(file)) {
      const gate = await runtime.runPortabilityGate(
        file,
        kind === "command" ? "command" : "skill",
        signal,
      )
      if (gate.kind === "blocked") {
        throw new Error(
          `commit REFUSÉ par la gate de portabilité (skill-verify) — fix manuel requis\n` +
            `artefact : ${file}\n` +
            gate.findings.map((finding, i) => `  ${i + 1}. ${finding}`).join("\n") +
            `\nCorrigez le fichier puis relancez le commit.`,
        )
      }
      if (gate.kind === "unusable") {
        // Safety-first : une gate morte ne doit jamais produire un faux vert —
        // refus avec motif précis, fix d'environnement requis.
        throw new Error(
          `commit REFUSÉ — gate de portabilité non exécutable (environnement harness-eval défaillant)\n` +
            `artefact : ${file}\n` +
            `motif : ${gate.reason}\n` +
            `Réparez l'installation de harness-eval (≥ 7.10.1, cf. INSTALL.md §1) puis relancez le commit.`,
        )
      }
      if (gate.kind === "pass" && gate.warnings.length > 0) {
        note =
          `note : gate portabilité — ${gate.warnings.length} warning(s) non bloquant(s)\n` +
          gate.warnings.map((warning) => `  - ${warning}`).join("\n")
      } else if (gate.kind === "ignored") {
        note = `⚠ gate portabilité ignorée (fail-soft) : ${gate.reason}`
      } else if (gate.kind === "skipped") {
        // Skip visible dans le résultat du tool (commands hors périmètre
        // skill-verify) — le commit part SANS gate, dit haut et fort.
        note = `ℹ gate portabilité SKIPPED : ${gate.reason}`
      }
    }
    const result = await runtime.runCli(
      ["commit-draft", "--kind", kind, "--file", file],
      COMMIT_DRAFT_TIMEOUT_MS,
      signal,
    )
    const parsed = splitCommitDraftResult(result)
    // Refus, gate ou sortie non structurée : la sortie brute du CLI EST le
    // retour, note comprise — rien n'est réécrit, rien n'est masqué.
    if (!parsed) return note ? `${note}\n\n${result}` : result
    const { facts, message } = parsed
    return JSON.stringify(
      { ...facts, message, gate_note: note },
      null,
      2,
    )
  },
})

/** Drapeaux et libellés de staging des payloads JSON, dans l'ordre d'émission. */
const STAGED_INPUTS: readonly (readonly [flag: string, label: string])[] = [
  ["--coherence", "coherence"],
  ["--catalog", "catalog"],
  ["--usage", "usage"],
]

const SKILL_CURATE_TOOL: ToolDefinition = deepFreeze<ToolDefinition>({
  name: "weekly_skill_curate",
  description:
    "Étape 6.6 : curation/décroissance des skills (skill-curate). Consomme les décisions " +
    "de `weekly-coherence-review` (2.C) : applique archive/merge sur origin∈{weekly-*} " +
    "(R4 curation/GC + R8 TTL). Protection stricte : JAMAIS origin=user (sans override). " +
    "dry-run par défaut ; `apply=true` uniquement après validation humaine.",
  fields: [
    {
      name: "coherence",
      kind: "string",
      required: false,
      description: "JSON: findings de cohérence (tag_action pertinents)",
    },
    {
      name: "catalog",
      kind: "string",
      required: false,
      description: "JSON: catalogue de skills (skill_id, metadata.origin/ttl_policy)",
    },
    {
      name: "usage",
      kind: "string",
      required: false,
      description: "JSON: usage_records pour TTL (fallback inter-run .watch-memory.jsonl)",
    },
    {
      name: "runs_seen",
      kind: "string",
      required: false,
      description: "runs consécutifs observés (décroissance)",
    },
    {
      name: "stale_days",
      kind: "string",
      required: false,
      description: "seuil obsolescence last_loaded (jours, défaut 90)",
    },
    {
      name: "apply",
      kind: "string",
      required: false,
      description: "'true' pour exécuter (sinon dry-run)",
    },
    ANCHOR_FIELD,
  ],
  timeoutMs: SKILL_CURATE_TIMEOUT_MS,
  anchored: true,
  cliSubcommand: "skill-curate",
  run: async (
    input: Readonly<Record<string, unknown>>,
    runtime: AbortableRuntimeApi,
    signal?: AbortSignal,
  ): Promise<string> => {
    const argv = ["skill-curate", "--anchor", runtime.anchorArg(optionalString(input, "anchor"))]
    // `argv` a une taille limitée et variable selon la plateforme : les JSON
    // volumineux voyagent par fichier temporaire, jamais en argument.
    const staged: StagedPayload[] = []
    try {
      for (const [flag, label] of STAGED_INPUTS) {
        const payload = optionalString(input, label)
        if (payload === undefined) continue
        // `signal` transmis : un `signal` déjà déclenché lève avant toute
        // création de répertoire, et après écriture le staging se rebranche sur
        // le `finally` ci-dessous. Aucun résidu dans les deux cas.
        const stagedInput = runtime.stageJsonPayload(payload, label, signal)
        staged.push(stagedInput)
        argv.push(flag, stagedInput.file)
      }
      const runsSeen = optionalString(input, "runs_seen")
      if (runsSeen !== undefined) argv.push("--runs-seen", String(runsSeen))
      const staleDays = optionalString(input, "stale_days")
      if (staleDays !== undefined) argv.push("--stale-days", String(staleDays))
      if (optionalString(input, "apply") === "true") argv.push("--apply")
      return await runtime.runCli(argv, SKILL_CURATE_TIMEOUT_MS, signal)
    } finally {
      for (const stagedInput of staged) stagedInput.cleanup()
    }
  },
})

/**
 * Contrat d'artefacts, exposé en JSON — **lecture seule**.
 *
 * Miroir gelé de trois symboles du moteur (`report.py`) :
 * `_ARTIFACT_CONTRACTS`, `validate_required_artifacts`, `applicable_summary_rc`.
 *
 * **Read-only strict.** Aucun subprocess, aucune lecture de fichier, aucune écriture,
 * aucun accès au `<output_dir>` : le handler ne fait qu'un `JSON.stringify` d'une
 * constante en mémoire. Rien à nettoyer, rien à annuler — d'où `timeoutMs: 0` et
 * l'absence de `cliSubcommand`, le même couple que `weekly_preflight` (invariant
 * vérifié par `plugin-registry.test.mjs`). Ne pas y ajouter de lecture disque : le
 * contrat doit rester disponible même quand le run et le moteur sont absents.
 *
 * Pourquoi l'exposer : ces trois symboles vivaient hors de portée de l'agent, qui
 * devait relire `report.py` par tranches (10 lectures sur le run 2026-10-01) pour
 * savoir quels artefacts sont requis, lesquels sont seulement optionnels, et ce
 * que vaut un rc=1. Une description ne peut pas porter une liste de 13 noms
 * d'artefacts et une table de gates ; un tool read-only si.
 */
export const REPORT_ARTIFACT_CONTRACT: Readonly<Record<string, unknown>> = deepFreeze({
  /** `report.py::_ARTIFACT_CONTRACTS` — nom d'artefact → validateur de forme. */
  artifactContracts: [
    "weekly-summary",
    "weekly-insights",
    "weekly-harness-digest",
    "weekly-ecosystem",
    "weekly-quality-findings",
    "weekly-watch-findings-raw",
    "weekly-coherence-findings",
    "weekly-audit-candidates",
    "weekly-watch-context",
    "weekly-watch-findings",
    "watch-candidates",
    "weekly-harness-remediation",
    "weekly-timings",
  ],
  /** `report.py::validate_required_artifacts` — qui bloque, qui ne bloque pas. */
  validateRequiredArtifacts: {
    defaultRequired: ["weekly-summary"],
    optionalNames: [
      "weekly-insights",
      "weekly-harness-digest",
      "weekly-ecosystem",
      "weekly-quality-findings",
      "weekly-coherence-findings",
      "skill-curate",
      "weekly-audit-candidates",
      "weekly-watch-context",
      "weekly-watch-findings",
      "weekly-watch-findings-raw",
      "weekly-harness-remediation",
      "weekly-harness-remediation-proposals",
      "weekly-timings",
    ],
    rule:
      "Une branche déclarée applicable dans weekly-timings-<date>.json devient REQUISE même " +
      "si son producteur est normalement optionnel ; une branche désactivée reste visible dans " +
      "`optional` et ne peut pas faire échouer la gate par simple absence.",
    artifactStatuses: ["present", "absent", "ill_readable"],
    gateStatus: ["pass", "incomplete"],
    returnedKeys: [
      "required",
      "optional",
      "artifacts",
      "status",
      "missing",
      "ill_readable",
      "optional_missing",
      "optional_ill_readable",
      "html",
    ],
    htmlStatuses: ["present", "absent", "ill_readable", "disabled"],
    dynamicAudits:
      "audit-findings-<session_id>.json déclarés dans le payload de jointure : requis (rc=2 " +
      "s'ils manquent avec d'autres requis), mais un JOIN partiel (seuls les audits " +
      "dynamiques manquent) donne rc=1 ET le rapport est écrit sans ces sessions.",
  },
  /** `report.py::applicable_summary_rc` — le rc du résumé, explié. */
  applicableSummaryRc: {
    returns: [2, 0, 1],
    fatal:
      "2 si le résumé n'est pas un objet, si son rc brut est ≥ 2, ou si un enregistrement de " +
      "jointure porte rc ≥ 2 — un 2 n'est JAMAIS dégradé vers 0 ou 1.",
    zero:
      "0 quand tous les enregistrements de jointure sont non bloquants : transcript tronqué " +
      "validé, entrée optionnelle récupérée, refus de permission externe au worktree. Ce sont " +
      "des FAITS, pas des échecs.",
    one:
      "1 sinon :(summary ou branche) en partie dégradé. Le pipeline a produit son livrable — " +
      "1 est un signal de qualité à remonter dans le rapport, pas un STOP.",
  },
  /** Rappel des trois gates qui expliquent un rc=1 à l'assemble. */
  assembleWarnOnlyRc1: [
    "security : findings security/critical dans le digest harness → gate status=warn, rapport écrit",
    "jointure partielle : audit dynamique manquant → rapport écrit sans ces sessions",
    "rc propagé : résumé dégradé (branche veille rc=1) ou rendu HTML best-effort en échec → rapport markdown écrit",
  ],
  section4: {
    files: {
      prose: "weekly-report-blocks-<date>.md",
      autoDraft: "weekly-report-blocks-auto-<date>.md",
    },
    contract: {
      maxLines: 60,
      targetLines: 40,
      minWords: 40,
      minWordsSource: "config.py:190 blocks_min_words=40",
      tags: {
        "F:<session_id>#<category>": "finding qualité — id complet, jamais tronqué",
        "A:<rule>": "alerte (weekly-insights)",
        "M:<category>": "constat de maintenance (weekly-insights)",
      },
      mustCiteSeverity: "high",
      forbidden: "chiffres libres dans le texte visible (tolérés : dates ISO, %, versions)",
      onViolation: "bloc REJETÉ → fallback sur le brouillon auto, section marquée auto_draft_fallback",
    },
  },
  draftConsumed: DRAFT_CONSUMED_NOTE,
})

const REPORT_CONTRACT_TOOL: ToolDefinition = deepFreeze<ToolDefinition>({
  name: "weekly_report_contract",
  description:
    "Outil READ-ONLY : renvoie en JSON le contrat d'artefacts du rapport — " +
    "required/optional par `validate_required_artifacts`, validateurs de forme " +
    "(`_ARTIFACT_CONTRACTS`), sémantique du `rc` (`applicable_summary_rc`), les 3 cas " +
    "warn-only rc=1 de l'assemble, et le contrat du fichier de section 4. " +
    "RIEN N'EST ÉCRIT, aucun subprocess, aucun accès disque — appelable à tout moment, " +
    "sans run ni moteur installés. " +
    "À appeler AVANT d'écrire `weekly-report-blocks-<date>.md` (étape 7b) au lieu de " +
    "deviner les tags, la taille et la couverture `high`, et AVANT d'interpréter un rc " +
    "de veille ou d'assemble. " +
    `${SECTION_4_FILES} ` +
    `${SECTION_4_FILE_CONTRACT} ` +
    `${REPORT_RC_SEMANTICS} ${DRAFT_CONSUMED_NOTE}`,
  fields: [],
  timeoutMs: 0,
  anchored: false,
  run: async (): Promise<string> => JSON.stringify(REPORT_ARTIFACT_CONTRACT),
})

/**
 * Les 21 outils dans l'ordre d'exposition du contrat V1 — l'ordre est figé par
 * la fixture, pas choisi ici. Les groupes simples sont entrecoupés d'outils à
 * comportement propre : la composition est donc explicite, jamais un simple
 * `concat` de deux tableaux.
 */
export const TOOL_REGISTRY: readonly ToolDefinition[] = deepFreeze([
  PREFLIGHT_TOOL,
  ...OPENING_SPECS.map(simpleTool),
  SHOW_SESSION_TOOL,
  simpleTool(HARNESS_SPEC),
  HARNESS_REMEDIATE_TOOL,
  ...REPORTING_SPECS.map(simpleTool),
  REPORT_CONTRACT_TOOL,
  COMMIT_DRAFT_TOOL,
  ...ANNEX_SPECS.map(simpleTool),
  SKILL_CURATE_TOOL,
])

/** Noms d'outils, dans l'ordre du contrat. */
export const TOOL_NAMES: readonly string[] = deepFreeze(TOOL_REGISTRY.map((tool) => tool.name))

/** Index par nom — construit une fois, jamais muté. */
export const TOOL_BY_NAME: ReadonlyMap<string, ToolDefinition> = new Map(
  TOOL_REGISTRY.map((tool) => [tool.name, tool] as const),
)

/** Sous-commandes CLI appelées par les outils, dans l'ordre d'exposition. */
export const CLI_COMMANDS: readonly string[] = deepFreeze(
  TOOL_REGISTRY.flatMap((tool) => (tool.cliSubcommand === undefined ? [] : [tool.cliSubcommand])),
)

// ---------------------------------------------------------------------------
// Conformité **à la compilation** — une divergence entre le registre et le
// runtime apparaît au typecheck, pas à l'exécution.
//
// Deux moitiés, à ne pas confondre :
// - `AbortableRuntimeApi extends RuntimeApi` : la rétrocompatibilité dans le sens
//   « tout runtime annulable reste un runtime neutre » est déjà imposée par le
//   `extends` de l'interface (TS2430 la refuse à la déclaration). La condition
//   la rend explicite, rien de plus.
// - `WeeklyRuntime extends AbortableRuntimeApi` : c'est celle-ci qui mord.
//   Vérifié par sabotage — retirer une capacité du runtime rend le type `never`
//   et provoque `TS2322: 'true' is not assignable to 'never'` ici. C'est aussi
//   la seule chose que l'affectation apporte : `T extends true` accepterait
//   `never` (toujours assignable) et ne mordrait pas.
//
// Limite assumée : `signal` étant facultatif des deux côtés, un runtime qui
// l'ignore silencieusement reste conforme au type. Seule l'exécution — le test
// d'annulation bout-en-bout contre un vrai `WeeklyRuntime` — ne peut le voir.
// ---------------------------------------------------------------------------
type AbortableStaysRuntimeApi = AbortableRuntimeApi extends RuntimeApi ? true : never;
type RuntimeImplementsAbortable = WeeklyRuntime extends AbortableRuntimeApi ? true : never;

export const _ABORT_CONTRACT_CONFORMS: AbortableStaysRuntimeApi & RuntimeImplementsAbortable =
  true;
