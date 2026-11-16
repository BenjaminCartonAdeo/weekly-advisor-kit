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

import {
  ANCHOR_FIELD,
  COMMIT_DRAFT_TIMEOUT_MS,
  deepFreeze,
  HARNESS_REMEDIATE_DEFAULT_MODE,
  HARNESS_REMEDIATE_TIMEOUT_MS,
  LOOKBACK_FIELD,
  optionalString,
  requiredString,
  SHOW_SESSION_TIMEOUT_MS,
  simpleTool,
  SKILL_CURATE_TIMEOUT_MS,
  TOOL_DEFAULTS,
} from "./tool-registry-common.ts"
import {
  ANNEX_SPECS,
  DRAFT_CONSUMED_NOTE,
  HARNESS_SPEC,
  OPENING_SPECS,
  REPORT_RC_SEMANTICS,
  REPORTING_SPECS,
  SECTION_4_FILE_CONTRACT,
  SECTION_4_FILES,
} from "./tool-registry-specs.ts"
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

const _ABORT_CONTRACT_CONFORMS: AbortableStaysRuntimeApi & RuntimeImplementsAbortable =
  true;

export interface AbortableRuntimeApi extends RuntimeApi {
  runCli(args: readonly string[], timeoutMs: number, signal?: AbortSignal): Promise<string>;
  stageJsonPayload(payload: string, label: string, signal?: AbortSignal): StagedPayload;
  runPortabilityGate(
    file: string,
    kind: "skill" | "command",
    signal?: AbortSignal,
  ): Promise<PortabilityOutcome>;
}
