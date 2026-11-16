import type {
  FieldSpec,
  SimpleToolSpec,
  ToolDefinition,
} from "./types.ts"

export function deepFreeze<T>(value: T): T {
  if (value === null || typeof value !== "object") return value
  if (Object.isFrozen(value)) return value
  Object.freeze(value)
  for (const child of Object.values(value as Record<string, unknown>)) deepFreeze(child)
  return value
}

export function optionalString(input: Readonly<Record<string, unknown>>, key: string): string | undefined {
  const value = input[key]
  return typeof value === "string" ? value : undefined
}

export function requiredString(input: Readonly<Record<string, unknown>>, key: string, tool: string): string {
  const value = input[key]
  if (typeof value !== "string") throw new Error(`${tool}: champ obligatoire manquant ou non textuel : ${key}`)
  return value
}
export const ANCHOR_FIELD: FieldSpec = deepFreeze({
  name: "anchor",
  kind: "string",
  required: false,
  description: "ISO-8601 override (rare)",
})

export const LOOKBACK_FIELD: FieldSpec = deepFreeze({
  name: "lookback_days",
  kind: "number",
  required: false,
  description: "Override de run : fenêtre en jours (rare ; défaut = config)",
})

/** Mode par défaut de `weekly_harness_remediate` — seule valeur de défaut pilotée d'ici. */
export const HARNESS_REMEDIATE_DEFAULT_MODE = "dry-run"

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
export const SHOW_SESSION_TIMEOUT_MS = 300_000
/** Timeout de `weekly_harness_remediate` — gelé. */
export const HARNESS_REMEDIATE_TIMEOUT_MS = 900_000
/** Timeout de `weekly_commit_draft` — gelé. */
export const COMMIT_DRAFT_TIMEOUT_MS = 120_000
/** Timeout de `weekly_skill_curate` — gelé. */
export const SKILL_CURATE_TIMEOUT_MS = 900_000

// ---------------------------------------------------------------------------
// Cas majoritaire : 1 sous-commande → 1 appel CLI.
// `anchored !== false` ⇒ champ `anchor` + drapeau `--anchor` ; `lookback`
// ⇒ champ `lookback_days` + drapeau `--lookback-days` (fenêtre en jours, jamais
// une édition de config).
// ---------------------------------------------------------------------------
export function simpleTool(spec: SimpleToolSpec): ToolDefinition {
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
      runtime: any,
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
