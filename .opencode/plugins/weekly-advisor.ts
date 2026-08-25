/**
 * weekly-advisor — plugin enveloppe (V1 plugin API).
 *
 * Rôle : exposer les sous-commandes du moteur python (weekly_telemetry_aggregator)
 * comme tools appelables par l'agent `weekly-advisor`. Aucune logique métier ici.
 *
 * Résolution des chemins (toutes dérivées du worktree, zéro config absolue) :
 *   - moteur : <worktree>/.opencode/plugins/weekly-advisor-engine
 *   - python : $WEEKLY_PYTHON ?? <moteur>/.venv/Scripts/python.exe (Windows)
 *              ?? <moteur>/.venv/bin/python (POSIX) — venv du projet moteur
 *   - config : <moteur>/weekly-telemetry-config.json (relue à chaque appel)
 *   - ancre  : <output_dir>/anchor-last.txt — lue si fraîche (âge ≤ fenêtre du run),
 *              rafraîchie vers maintenant si périmée, créée si absente (v6.0.b).
 *              Aucune gestion LLM d'ancre ; le refresh conditionnel évite la
 *              cadence figée (une ancre immuable rejouerait la même fenêtre).
 */
import { type Plugin, tool } from "@opencode-ai/plugin"
import path from "node:path"
import os from "node:os"
import fs from "node:fs"
import { execFile } from "node:child_process"

const ENGINE_REL = [".opencode", "plugins", "weekly-advisor-engine"]
const ANCHOR_FILE = "anchor-last.txt"

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
  configPath: string
}

/**
 * Expansion `~` alignée sur `Path.expanduser()` côté moteur Python (v6.2.c) :
 * sans elle, une config `"output_dir": "~/…"` donnait côté TS un littéral
 * `<engine>/~/x` (split-brain avec le CLI qui, lui, expandait).
 */
function expandHome(p: string): string {
  if (p === "~") return os.homedir()
  if (p.startsWith("~/") || p.startsWith("~\\")) return path.join(os.homedir(), p.slice(2))
  return p
}

function resolveEngine(worktree: string): EngineLoc {
  const engine = path.join(worktree, ...ENGINE_REL)
  // Résolution platform-aware : le venv est posé en .venv\Scripts\python.exe
  // sur Windows, .venv/bin/python ailleurs — un seul candidat par OS.
  const venvPython =
    process.platform === "win32"
      ? path.join(engine, ".venv", "Scripts", "python.exe")
      : path.join(engine, ".venv", "bin", "python")
  const candidates = [process.env.WEEKLY_PYTHON, venvPython].filter(
    (p): p is string => typeof p === "string" && p.length > 0,
  )
  const python = candidates.find((p) => fs.existsSync(p))
  if (!fs.existsSync(engine)) {
    throw new Error(`moteur introuvable: ${engine} (structure du kit corrompue)`)
  }
  if (!python) {
    throw new Error(
      `interpréteur Python introuvable (candidats testés: ${candidates.join(", ")}) — définir WEEKLY_PYTHON ou créer le venv : uv sync --project .opencode/plugins/weekly-advisor-engine --extra dev`,
    )
  }
  // config : <moteur>/weekly-telemetry-config.json (même résolution que le CLI)
  const configPath = path.join(engine, "weekly-telemetry-config.json")
  let config: Record<string, unknown> = {}
  if (fs.existsSync(configPath)) {
    config = JSON.parse(fs.readFileSync(configPath, "utf8"))
  }
  // Expansion `~` des clés de chemins — sémantique identique au moteur Python
  // (`expanduser()`), pour que l'ancre et tout usage TS voient la même racine.
  const expanded = { ...config }
  for (const key of ["project_root", "output_dir", "kit_root"]) {
    const v = expanded[key]
    if (typeof v === "string" && v.startsWith("~")) expanded[key] = expandHome(v)
  }
  const out = expanded["output_dir"]
  const outputDir =
    typeof out === "string" && path.isAbsolute(out)
      ? out
      : path.join(engine, typeof out === "string" ? out : "reports")
  return { engine, python, config: expanded, outputDir, configPath }
}

/**
 * Ancre glissante : créée si absente, conservée dans la même journée (stabilité
 * intra-run : tous les tools du run partagent la même fenêtre), rafraîchie vers
 * maintenant chaque jour. La fraîcheur par « âge ≤ fenêtre » gelait la fenêtre
 * quand des runs s'enchaînaient en moins de 7 j (chaque run rejouait la même
 * période). Désormais la fenêtre avance chaque jour ; rejouer une fenêtre
 * historique = passer --anchor explicitement (v6.0.n).
 */
function readOrCreateAnchor(outputDir: string): string {
  const file = path.join(outputDir, ANCHOR_FILE)
  if (fs.existsSync(file)) {
    const existing = fs.readFileSync(file, "utf8").trim()
    if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/.test(existing)) {
      const today = new Date().toISOString().slice(0, 10)
      if (existing.slice(0, 10) === today) return existing
    }
  }
  const anchor = new Date().toISOString().replace(/\.\d{3}Z$/, "Z")
  fs.mkdirSync(outputDir, { recursive: true })
  fs.writeFileSync(file, anchor)
  return anchor
}

function anchorArg(
  anchor: string | undefined,
  outputDir: string,
): string {
  return anchor ?? readOrCreateAnchor(outputDir)
}

function runCli(worktree: string, args: string[], timeoutMs: number): Promise<string> {
  const { engine, python, configPath } = resolveEngine(worktree)
  // --config explicite (v6.2.c) : le transport ne repose plus sur cwd=engine ;
  // absent du disque → omis, le CLI retombe sur sa découverte par défaut
  // (même chemin <cwd>/weekly-telemetry-config.json — comportement inchangé).
  const argv = [
    "-m",
    "weekly_telemetry_aggregator",
    ...(fs.existsSync(configPath) ? ["--config", configPath] : []),
    ...args,
  ]
  return new Promise((resolve, reject) => {
    execFile(
      python,
      argv,
      {
        cwd: engine,
        timeout: timeoutMs,
        maxBuffer: 64 * 1024 * 1024,
        // Windows : forcer UTF-8 côté Python (consoles cp1252/cp850 sinon).
        env: { ...process.env, PYTHONUTF8: "1", PYTHONIOENCODING: "utf-8" },
      },
      (err, stdout, stderr) => {
        if (!err) return resolve(stdout.trim())
        const detail = (stderr || stdout || "").trim().split("\n").slice(-12).join("\n")
        reject(new Error(`weekly_telemetry_aggregator ${args.join(" ")} → exit ${err.code ?? "?"}\n${detail}`))
      },
    )
  })
}

// ---------------------------------------------------------------------------
// Gate de portabilité (cellule 3.2) : chaque draft est scanné par
// `harness-eval skill-verify` AVANT tout commit-draft. Seules les règles
// custom/portability/* du kit décident (.harness-eval/rules/portability.yaml) :
//   - cwd = racine projet OBLIGATOIRE (chargement des règles custom depuis
//     <racine>/.harness-eval/rules/ ; lancé ailleurs : 0 règle → gate aveugle) ;
//   - ≥1 finding error → commit refusé, fix manuel requis ;
//   - warnings seuls → commit autorisé, note jointe au résultat ;
//   - binaire absent (ENOENT POSIX / `where` négatif Windows) → fail-soft :
//     note ⚠ non bloquante — gap d'install documenté, signalé par le doctor ;
//   - timeout, crash scanner ou sortie illisible → REFUS « gate non
//     exécutable » (v6.2.c) : jamais de faux vert — safety-first.
// win32 : execFile sans shell ne résout pas le shim .cmd de harness-eval
// (uv tool install) → ENOENT systématique, gate jamais active. Sous Windows on
// passe shell:true (résolution via cmd) avec scanDir quoté (join par espaces) ;
// un pré-check `where` préserve la sémantique fail-soft du binaire absent,
// car en mode shell l'absence remonte exit≠0 et plus ENOENT. Garde non
// exécutable sur poste POSIX — vérifier sous Windows avant release.
const PORTABILITY_PREFIX = "custom/portability/"

interface SkillVerifyDetail {
  rule?: string
  severity?: string
  message?: string
  suggestion?: string
}

/** Binaire présent sur PATH ? POSIX : ENOENT détecté au spawn, pré-check inutile. */
function commandOnPath(cmd: string): Promise<boolean> {
  if (process.platform !== "win32") return Promise.resolve(true)
  // Windows : `where` (natif) résout les shims .cmd — spawn direct sans shell non.
  return new Promise((resolve) => {
    execFile("where", [cmd], { windowsHide: true }, (err) => resolve(!err))
  })
}

/** Spawn harness-eval avec cwd = racine projet. Résout "" si binaire absent. */
async function runSkillVerify(scanDir: string): Promise<string> {
  if (!(await commandOnPath("harness-eval"))) return ""
  const winShell = process.platform === "win32"
  return new Promise((resolve, reject) => {
    execFile(
      "harness-eval",
      // shell:true → argv joint par espaces : quotage obligatoire (tmpdir peut
      // contenir des espaces sous Windows). POSIX : arg brut, pas de shell.
      ["skill-verify", winShell ? `"${scanDir}"` : scanDir, "--format", "json"],
      { cwd: worktree, timeout: 60_000, maxBuffer: 16 * 1024 * 1024, env: process.env, shell: winShell },
      (err, stdout, stderr) => {
        if (err && (err as NodeJS.ErrnoException).code === "ENOENT") return resolve("")
        if (err) {
          const why = (err as NodeJS.ErrnoException & { killed?: boolean }).killed
            ? `timeout après 60 s`
            : `exit ${String(err.code ?? "?")}`
          return reject(
            new Error(`skill-verify indisponible (${why})\n${(stderr || stdout || "").trim().slice(-400)}`),
          )
        }
        resolve(stdout)
      },
    )
  })
}

type PortabilityOutcome =
  | { kind: "blocked"; findings: string[] }
  | { kind: "pass"; warnings: string[] }
  | { kind: "ignored"; reason: string }
  | { kind: "unusable"; reason: string }
  | { kind: "skipped"; reason: string }

/** Exécute la gate et tranche : bloqué / passage (warnings seuls) / ignorée (binaire absent) / refusée (environnement défaillant) / skippée (commands). */
export async function runPortabilityGate(file: string, kind: "skill" | "command"): Promise<PortabilityOutcome> {
  // E2 : harness-eval skill-verify n'inspecte que les dossiers SKILL.md (≥ 7.10.1).
  // Pour une command, lancer la gate produirait un crash systématique
  // (« No agent components found ») → refus injustifié. Skip explicite avec
  // motif remonté au résultat du tool — jamais de silence ni de faux vert.
  if (kind === "command") {
    return {
      kind: "skipped",
      reason:
        "Gate portabilité non applicable aux commands (harness-eval 7.10.1 : skills uniquement) — commit autorisé sans gate.",
    }
  }
  const scan = prepareScanDir(file)
  try {
    let stdout: string
    try {
      stdout = await runSkillVerify(scan.dir)
    } catch (e) {
      // Crash/timeout du scanner ≠ faute de l'artefact, mais un commit passé
      // sur une gate morte serait un faux vert → REFUS (v6.2.c).
      return { kind: "unusable", reason: (e as Error).message.split("\n")[0] }
    }
    if (!stdout.trim()) return { kind: "ignored", reason: "binaire harness-eval introuvable" }
    let errors: string[]
    let warnings: string[]
    try {
      ;({ errors, warnings } = collectPortability(stdout))
    } catch {
      return { kind: "unusable", reason: "sortie skill-verify illisible (JSON invalide)" }
    }
    if (errors.length > 0) return { kind: "blocked", findings: errors }
    return { kind: "pass", warnings }
  } finally {
    scan.cleanup()
  }
}

/**
 * Copie réelle (zéro symlink) de l'artefact dans un répertoire temporaire :
 * le scan porte sur LE seul artefact commis — jamais sur le voisinage du
 * dossier de drafts (des findings tiers bloqueraient à tort). Pour un skill,
 * le dossier entier est copié afin que la règle self-contained-scripts voie
 * les scripts frères.
 */ // ponytail: isolation par copie plutôt que scan du dossier parent
function prepareScanDir(file: string): { dir: string; cleanup: () => void } {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "wa-portab-"))
  const cleanup = () => fs.rmSync(tmp, { recursive: true, force: true })
  if (path.basename(file) === "SKILL.md") {
    const dir = path.join(tmp, "skill")
    fs.cpSync(path.dirname(file), dir, { recursive: true })
    return { dir, cleanup }
  }
  fs.copyFileSync(file, path.join(tmp, path.basename(file)))
  return { dir: tmp, cleanup }
}

/** Extrait les findings custom/portability/* d'un rapport skill-verify JSON. */
function collectPortability(stdout: string): { errors: string[]; warnings: string[] } {
  const report = JSON.parse(stdout) as { findings?: Array<{ details?: SkillVerifyDetail[] }> }
  const errors: string[] = []
  const warnings: string[] = []
  for (const finding of report.findings ?? []) {
    for (const detail of finding.details ?? []) {
      if (!detail.rule?.startsWith(PORTABILITY_PREFIX)) continue
      const entry =
        `[${detail.rule}] ${detail.message ?? ""}` +
        (detail.suggestion ? ` — suggestion : ${detail.suggestion}` : "")
      // Toute sévérité != error passe en note non bloquante (error seul bloque).
      ;(detail.severity === "error" ? errors : warnings).push(entry)
    }
  }
  return { errors, warnings }
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
      const { outputDir } = resolveEngine(worktree)
      return runCli(worktree, cliArgs(anchorArg(args.anchor, outputDir), args.lookback_days), timeoutMs)
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
        "Étape 1 du weekly-advisor : collecte télémétrique complète (run). " +
          "Écrit weekly-summary-<date>.json. L'ancre est lue/créée/rafraîchie dans <output_dir>/anchor-last.txt.",
        (anchor, lookback) => withLookback(["run", "--anchor", anchor], lookback),
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
          "Écrit weekly-watch-context-<date>.json. " +
          "SÉQUENTIEL : nécessite weekly-ecosystem-<date>.json de l'étape 2 — " +
          "exécuter weekly_releases d'abord, jamais en parallèle.",
        (anchor) => ["watch-context", "--anchor", anchor],
        120_000,
      ),

      weekly_watch_distill: anchorTool(
        "Étape 2.2 : distillation déterministe de l'écosystème vers ~30 fiches candidates " +
          "(scoring, screening sécurité, mémoire inter-run). " +
          "Écrit watch-candidates-<date>.json et watch-memory-digest-<date>.json. " +
          "SÉQUENTIEL : exécuter après weekly_releases, avant weekly_watch_context ; " +
          "exit 2 = écosystème absent ou étape désactivée (config watch_distill.enabled) — " +
          "dégradation attendue, le flux aval retombe sur l'écosystème complet.",
        (anchor) => ["watch-distill", "--anchor", anchor],
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
          "Étape 3 : transcrit une session (show-session) et retourne le texte structuré. " +
          "L'extrait est écrit à <run>/extracts/transcript-extract-<session_id>.md sous le run actif " +
          "(<output_dir>/runs/current/extracts/, fallback legacy <output_dir>/extracts/) : faire Read sur " +
          "CE FICHIER EXACT (chemin imprimé par le tool), JAMAIS sur le répertoire extracts/ lui-même " +
          "(opencode Read ne liste pas les dirs). Appeler le tool AVANT toute lecture d'extraits. " +
          "Utiliser après audit-candidates.",
        args: {
          session_id: tool.schema.string().describe("id de session (ses_...)"),
          include_children: tool.schema.boolean().optional().describe("inclure les subagents"),
        },
        async execute(args) {
          const { outputDir } = resolveEngine(worktree)
          // v6.0.k (F1) : extraits dans le run actif, legacy en fallback.
          // E1 : l'alias à jour du moteur est <output_dir>/runs/current — le
          // symlink top-level <output_dir>/current peut rester accroché à un run
          // périmé (observé 2026-08-25 : extracts écrits dans le run de la
          // veille). Priorité runs/current, puis current top-level (legacy).
          const runsCurrent = path.join(outputDir, "runs", "current")
          const legacyCurrent = path.join(outputDir, "current")
          const runBase = fs.existsSync(runsCurrent)
            ? runsCurrent
            : fs.existsSync(legacyCurrent)
              ? legacyCurrent
              : outputDir
          const cliArgs = ["show-session", args.session_id, "--extract-dir", path.join(runBase, "extracts")]
          if (args.include_children) cliArgs.push("--include-children")
          return runCli(worktree, cliArgs, 300_000)
        },
      }),

      weekly_harness: anchorTool(
        "Étape 5 : lint de .opencode/ (harness-eval). Écrit weekly-harness-digest-<date>.json.",
        (anchor) => ["harness", "--anchor", anchor],
        900_000,
      ),

      weekly_harness_remediate: tool({
        description:
          "Étape 5.5 : analyse/applique les propositions harness via une gate déterministe. " +
          "Dry-run par défaut ; aucun commit automatique.",
        args: {
          proposal_file: tool.schema.string().describe("chemin absolu du JSON de propositions"),
          mode: tool.schema
            .enum(["dry-run", "apply"])
            .optional()
            .describe("dry-run par défaut ; apply uniquement après toutes les gates"),
          anchor: tool.schema.string().optional().describe("ISO-8601 override (rare)"),
        },
        async execute(args) {
          const { outputDir } = resolveEngine(worktree)
          const anchor = anchorArg(args.anchor, outputDir)
          return runCli(
            worktree,
            [
              "harness-remediate",
              "--proposal",
              args.proposal_file,
              "--mode",
              args.mode ?? "dry-run",
              "--anchor",
              anchor,
            ],
            900_000,
          )
        },
      }),

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
          "Étape 4 : commit auto-rédigé d'un skill/command draft (commit-draft). " +
          "Gate de portabilité (skill-verify) avant chaque commit : erreur → refus + fix manuel requis, " +
          "warnings seuls → passage avec note, environnement défaillant (timeout/crash/sortie illisible) → refus. " +
          "kind=command → gate non applicable (harness-eval 7.10.1 : skills uniquement), skip explicite avec note dans le résultat. " +
          "1 commit par écriture, pré-checks git intégrés.",
        args: {
          kind: tool.schema.enum(["skill", "command"]),
          file: tool.schema.string().describe("chemin absolu du fichier draft"),
        },
        async execute(args) {
          // Gate portabilité (cellule 3.2) — avant TOUT commit. Fichier absent :
          // pas de gate, l'erreur vient du CLI (comportement inchangé).
          let note = ""
          if (fs.existsSync(args.file)) {
            const gate = await runPortabilityGate(args.file, args.kind)
            if (gate.kind === "blocked") {
              throw new Error(
                `commit REFUSÉ par la gate de portabilité (skill-verify) — fix manuel requis\n` +
                  `artefact : ${args.file}\n` +
                  gate.findings.map((f, i) => `  ${i + 1}. ${f}`).join("\n") +
                  `\nCorrigez le fichier puis relancez le commit.`,
              )
            }
            if (gate.kind === "unusable") {
              // Safety-first (v6.2.c) : une gate morte ne doit jamais produire
              // un faux vert — refus avec motif précis, fix d'environnement requis.
              throw new Error(
                `commit REFUSÉ — gate de portabilité non exécutable (environnement harness-eval défaillant)\n` +
                  `artefact : ${args.file}\n` +
                  `motif : ${gate.reason}\n` +
                  `Réparez l'installation de harness-eval (≥ 7.10.1, cf. INSTALL.md §1) puis relancez le commit.`,
              )
            }
            if (gate.kind === "pass" && gate.warnings.length > 0) {
              note =
                `note : gate portabilité — ${gate.warnings.length} warning(s) non bloquant(s)\n` +
                gate.warnings.map((w) => `  - ${w}`).join("\n")
            } else if (gate.kind === "ignored") {
              note = `⚠ gate portabilité ignorée (fail-soft) : ${gate.reason}`
            } else if (gate.kind === "skipped") {
              // E2 : skip visible dans le résultat du tool (commands hors
              // périmètre skill-verify) — le commit part SANS gate, dit haut et fort.
              note = `ℹ gate portabilité SKIPPED : ${gate.reason}`
            }
          }
          const result = await runCli(
            worktree,
            ["commit-draft", "--kind", args.kind, "--file", args.file],
            120_000,
          )
          return note ? `${note}\n\n${result}` : result
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
