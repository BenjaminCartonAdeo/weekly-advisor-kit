/**
 * weekly-advisor — plugin enveloppe (V1 plugin API).
 *
 * Rôle : exposer les sous-commandes du moteur python (weekly_telemetry_aggregator)
 * comme tools appelables par l'agent `weekly-advisor`. Aucune logique métier ici.
 *
 * Résolution des chemins (toutes dérivées du worktree, zéro config absolue) :
 *   - moteur : <worktree>/.opencode/plugins/weekly-advisor-engine
 *   - python : $WEEKLY_PYTHON ?? <moteur>/.venv/bin/python (venv du projet moteur)
 *   - config : <moteur>/weekly-telemetry-config.json (relue à chaque appel)
 *   - ancre  : <output_dir>/anchor-last.txt — lue si fraîche (âge ≤ fenêtre du run),
 *              rafraîchie vers maintenant si périmée, créée si absente (v6.0.b).
 *              Aucune gestion LLM d'ancre ; le refresh conditionnel évite la
 *              cadence figée (une ancre immuable rejouerait la même fenêtre).
 */
import { type Plugin, tool } from "@opencode-ai/plugin"
import path from "node:path"
import fs from "node:fs"
import { execFile } from "node:child_process"

const ENGINE_REL = [".opencode", "plugins", "weekly-advisor-engine"]
const ANCHOR_FILE = "anchor-last.txt"
const DEFAULT_LOOKBACK_DAYS = 7

// `worktree` est module-scope, affecté à l'init du plugin (v6.0.b) : les factories
// de tools sont définies au niveau module et leurs closures en dépendent — une
// déclaration locale au factory `WeeklyAdvisorPlugin` provoquait
// `ReferenceError: worktree is not defined` au premier appel de tool (layout v6.0).
let worktree = ""

interface EngineLoc {
  engine: string
  python: string
  config: Record<string, unknown>
  outputDir: string
}

function resolveEngine(worktree: string): EngineLoc {
  const engine = path.join(worktree, ...ENGINE_REL)
  const python =
    process.env.WEEKLY_PYTHON ?? path.join(engine, ".venv", "bin", "python")
  if (!fs.existsSync(engine)) {
    throw new Error(`moteur introuvable: ${engine} (structure du kit corrompue)`)
  }
  if (!fs.existsSync(python)) {
    throw new Error(
      `venv introuvable: ${python} — exécuter depuis la racine du kit : uv sync --project .opencode/plugins/weekly-advisor-engine --all-extras`,
    )
  }
  // config : <moteur>/weekly-telemetry-config.json (même résolution que le CLI)
  const configPath = path.join(engine, "weekly-telemetry-config.json")
  let config: Record<string, unknown> = {}
  if (fs.existsSync(configPath)) {
    config = JSON.parse(fs.readFileSync(configPath, "utf8"))
  }
  const out = config["output_dir"]
  const outputDir =
    typeof out === "string" && path.isAbsolute(out)
      ? out
      : path.join(engine, typeof out === "string" ? out : "reports")
  return { engine, python, config, outputDir }
}

/** Fenêtre du run dérivée de la config (jamais d'édition de config par le plugin). */
function windowHours(config: Record<string, unknown>): number {
  const days = config["lookback_days"]
  return (typeof days === "number" ? days : DEFAULT_LOOKBACK_DAYS) * 24
}

/**
 * Ancre : créée si absente, lue si fraîche (âge ≤ fenêtre), rafraîchie sinon.
 * Sans refresh, une ancre figée à vie gèle la cadence : chaque run rejouerait
 * la même fenêtre (mêmes fichiers de sortie, mêmes deltas).
 */
function readOrCreateAnchor(outputDir: string, windowHours: number): string {
  const file = path.join(outputDir, ANCHOR_FILE)
  if (fs.existsSync(file)) {
    const existing = fs.readFileSync(file, "utf8").trim()
    if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/.test(existing)) {
      const ageHours = (Date.now() - Date.parse(existing)) / 3_600_000
      if (ageHours <= windowHours) return existing
    }
  }
  const anchor = new Date().toISOString().replace(/\.\d{3}Z$/, "Z")
  fs.mkdirSync(outputDir, { recursive: true })
  fs.writeFileSync(file, anchor)
  return anchor
}

function anchorArg(
  anchor: string | undefined,
  config: Record<string, unknown>,
  outputDir: string,
): string {
  return anchor ?? readOrCreateAnchor(outputDir, windowHours(config))
}

function runCli(worktree: string, args: string[], timeoutMs: number): Promise<string> {
  const { engine, python } = resolveEngine(worktree)
  return new Promise((resolve, reject) => {
    execFile(
      python,
      ["-m", "weekly_telemetry_aggregator", ...args],
      { cwd: engine, timeout: timeoutMs, maxBuffer: 64 * 1024 * 1024 },
      (err, stdout, stderr) => {
        if (!err) return resolve(stdout.trim())
        const detail = (stderr || stdout || "").trim().split("\n").slice(-12).join("\n")
        reject(new Error(`weekly_telemetry_aggregator ${args.join(" ")} → exit ${err.code ?? "?"}\n${detail}`))
      },
    )
  })
}

// Factories pour les tools « 1 sous-commande → 1 appel CLI » (le cas majoritaire).
// `lookbackFlag` ajoute l'arg optionnel `lookback_days` (override de run déduit du
// prompt — v6.0.b : fenêtre en jours, jamais d'édition de config).
function anchorTool(
  description: string,
  cliArgs: (anchor: string, lookbackDays: number | undefined) => string[],
  timeoutMs: number,
  lookbackFlag = false,
) {
  return tool({
    description,
    args: {
      anchor: tool.schema.string().optional().describe("ISO-8601 override (rare)"),
      ...(lookbackFlag
        ? {
            lookback_days: tool.schema
              .number()
              .optional()
              .describe("Override de run : fenêtre en jours (rare ; défaut = config)"),
          }
        : {}),
    },
    async execute(args) {
      const { config, outputDir } = resolveEngine(worktree)
      return runCli(worktree, cliArgs(anchorArg(args.anchor, config, outputDir), args.lookback_days), timeoutMs)
    },
  })
}

function noArgTool(description: string, cliArgs: string[], timeoutMs: number) {
  return tool({
    description,
    args: {},
    async execute() {
      return runCli(worktree, cliArgs, timeoutMs)
    },
  })
}

function withLookback(cliArgs: string[], lookbackDays: number | undefined): string[] {
  return lookbackDays ? [...cliArgs, "--lookback-days", String(lookbackDays)] : cliArgs
}

export const WeeklyAdvisorPlugin: Plugin = async (ctx) => {
  worktree = ctx.worktree ?? ctx.directory
  return {
    tool: {
      weekly_run: anchorTool(
        "Étape 1 du weekly-advisor : collecte télémétrique complète (run --force). " +
          "Écrit weekly-summary-<date>.json. L'ancre est lue/créée/rafraîchie dans <output_dir>/anchor-last.txt.",
        (anchor, lookback) => withLookback(["run", "--force", "--anchor", anchor], lookback),
        1_800_000,
        true,
      ),

      weekly_releases: anchorTool(
        "Étape 2 : veille écosystème (releases). Écrit weekly-ecosystem-<date>.json.",
        (anchor, lookback) => withLookback(["releases", "--anchor", anchor], lookback),
        900_000,
        true,
      ),

      weekly_watch_context: anchorTool(
        "Étape 2.5 : inventaire déterministe du worktree et crosswalk marché/existant. " +
          "Écrit weekly-watch-context-<date>.json.",
        (anchor) => ["watch-context", "--anchor", anchor],
        120_000,
      ),

      weekly_watch_validate: anchorTool(
        "Étape 3.6 : valide les findings bruts de la veille contre l'inventaire déterministe. " +
          "Écrit weekly-watch-findings-<date>.json.",
        (anchor) => ["watch-validate", "--anchor", anchor],
        120_000,
      ),

      weekly_audit_candidates: anchorTool(
        "Étape 3 (1/2) : sélection déterministe des sessions à auditer (audit-candidates) → " +
          "weekly-audit-candidates-<date>.json (audited/unaudited, plafond audit_max_sessions).",
        (anchor) => ["audit-candidates", "--anchor", anchor],
        120_000,
      ),

      weekly_show_session: tool({
        description:
          "Étape 3 : transcrit une session (show-session) vers <output_dir>/extracts/ et " +
          "retourne le texte structuré. Utiliser après audit-candidates.",
        args: {
          session_id: tool.schema.string().describe("id de session (ses_...)"),
          include_children: tool.schema.boolean().optional().describe("inclure les subagents"),
        },
        async execute(args) {
          const { outputDir } = resolveEngine(worktree)
          const cliArgs = ["show-session", args.session_id, "--extract-dir", path.join(outputDir, "extracts")]
          if (args.include_children) cliArgs.push("--include-children")
          return runCli(worktree, cliArgs, 300_000)
        },
      }),

      weekly_harness: anchorTool(
        "Étape 5 : lint de .opencode/ (harness-eval). Écrit weekly-harness-digest-<date>.json.",
        (anchor) => ["harness", "--anchor", anchor],
        900_000,
      ),

      weekly_insights: anchorTool(
        "Étape 6 : deltas, alertes et maintenance. Écrit weekly-insights-<date>.json.",
        (anchor) => ["insights", "--anchor", anchor],
        300_000,
      ),

      weekly_draft_candidates: anchorTool(
        "Étape 4 (1/2) : candidats à l'auto-drafting (draft-candidates) → " +
          "weekly-draft-candidates-<date>.json (skill-candidate / command-candidate / command-improvement, plafonné).",
        (anchor) => ["draft-candidates", "--anchor", anchor],
        120_000,
      ),

      weekly_report_prep: anchorTool(
        "Étape 7a (1/2) : prépare le brouillon de rapport (report-prep → weekly-report-draft-<date>.md).",
        (anchor) => ["report-prep", "--anchor", anchor],
        120_000,
      ),

      weekly_report_blocks_draft: anchorTool(
        "Étape 7a (2/2) : génère le brouillon auto des blocs (report-blocks-draft → weekly-report-blocks-auto-<date>.md).",
        (anchor) => ["report-blocks-draft", "--anchor", anchor],
        120_000,
      ),

      weekly_report_assemble: anchorTool(
        "Étape 7c : assemble le rapport final (report-assemble → weekly-report-<date>.md). " +
          "⚠ un assemble réussi consomme le draft : pour un nouvel assemble, relancer weekly_report_prep d'abord.",
        (anchor) => ["report-assemble", "--anchor", anchor],
        120_000,
      ),

      weekly_commit_draft: tool({
        description:
          "Étape 4 : commit auto-rédigé d'un skill/command draft (commit-draft). 1 commit par écriture, " +
          "message construit depuis le frontmatter, pré-checks git intégrés.",
        args: {
          kind: tool.schema.enum(["skill", "command"]),
          file: tool.schema.string().describe("chemin absolu du fichier draft"),
        },
        async execute(args) {
          return runCli(worktree, ["commit-draft", "--kind", args.kind, "--file", args.file], 120_000)
        },
      }),

      weekly_self_cost: noArgTool(
        "Étape 8 (annexe) : coût de la fenêtre du run (self-cost).",
        ["self-cost"],
        120_000,
      ),

      weekly_doctor: noArgTool(
        "Diagnostic du kit : vérifie opencode, harness-eval, git, gh, DB et la config (doctor).",
        ["doctor"],
        120_000,
      ),
    },
  }
}

// Le loader d'opencode (v1.18.18 / next-17297) exige un export `default`
// (SchemaError: Missing key at ["default"] sinon) — l'export nommé est conservé
// pour le smoke test (scripts/plugin-smoke.mjs).
export default WeeklyAdvisorPlugin
