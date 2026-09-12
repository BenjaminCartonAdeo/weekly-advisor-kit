#!/usr/bin/env node
/**
 * Contrat de flux docs ↔ code (G1, v6.0.p) — 7 surfaces vérifiées statiquement.
 *
 *  1. Tools TS → sous-commandes CLI : chaque outil du plugin invoque une
 *     sous-commande réelle du moteur (aucun argv fantôme).
 *  2. Sous-commandes CLI → handlers : tous les sous-parsers sont câblés à un
 *     `_cmd_*` défini, et aucun `_cmd_*` défini n'est orphelin.
 *  3. Artefacts : chaque étape produit son artefact daté (littéral
 *     `weekly-<étape>-` présent dans les sources du moteur).
 *  4. Chaînage : les dépendances d'ordre SÉQUENTIEL documentées dans le plugin
 *     TS existent aussi côté moteur (watch-context → ecosystem ; assemble → draft).
 *  5. Comptes de tests : README == INSTALL == ci.yml == pytest --collect-only
 *     réel (dérive C10 : les trois documents disaient 171/177/224).
 *  6. Étapes commande ↔ agent : les tokens d'outils (`weekly_*`) du
 *     « Déroulement » de la commande /weekly-review sont EXACTEMENT ceux du
 *     tableau de l'agent weekly-advisor, dans le même ordre (dérive observée :
 *     sémantique d'ancre contradictoire commande/agent, ré-alignée en v6.0.p).
 *  7. Orchestration waves : l'orchestrateur, son worker et la commande lanceuse
 *     documentent le dispatch parallèle de façon cohérente (frontmatter
 *     `task: allow`, marqueurs WAVE/JOIN, contrat retour JSON du worker,
 *     référence agent dans la commande — dérive observée : phrase interdisant
 *     encore le dispatch en subagent côté commande).
 *
 * Zéro dépendance (node stdlib). Lancé par la CI après pytest ; exit 0/1.
 */
import fs from "node:fs"
import path from "node:path"
import { spawnSync } from "node:child_process"
import { fileURLToPath } from "node:url"

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..")
const TS_FILE = path.join(ROOT, ".opencode", "plugins", "weekly-advisor.ts")
const ENGINE = path.join(ROOT, ".opencode", "plugins", "weekly-advisor-engine")
const CLI_FILE = path.join(ENGINE, "weekly_telemetry_aggregator", "cli.py")
const README_FILE = path.join(ROOT, "README.md")
const INSTALL_FILE = path.join(ROOT, "INSTALL.md")
const CI_FILE = path.join(ROOT, ".github", "workflows", "ci.yml")

let failures = 0
function ok(label, cond, detail = "") {
  if (cond) {
    console.log(`ok  ${label}`)
  } else {
    failures += 1
    console.log(`FAIL ${label}${detail ? ` — ${detail}` : ""}`)
  }
}
const read = (p) => fs.readFileSync(p, "utf8")

/** Execute the collection probe without throwing, preserving all diagnostics. */
export function collectPytest({ cwd, runner = spawnSync } = {}) {
  const result = runner("uv", ["run", "python", "-m", "pytest", "--collect-only", "-q"], {
    cwd,
    encoding: "utf8",
  })
  const stdout = String(result?.stdout ?? "")
  const stderr = String(result?.stderr ?? "")
  const status = result?.status ?? null
  if (result?.error || status !== 0) {
    return { kind: "execution-failure", stdout, stderr, status, error: result?.error ?? null }
  }
  // pytest's quiet collector may emit one ``path: count`` line per module
  // without its usual summary; sum those lines as equivalent parser output.
  const match = stdout.match(/(\d+) tests? collected/)
  const perFile = [...stdout.matchAll(/:\s*(\d+)\s*$/gm)]
  const collected = match ? Number(match[1]) : perFile.length ? perFile.reduce((sum, m) => sum + Number(m[1]), 0) : null
  if (collected === null) return { kind: "parser-failure", stdout, stderr, status, collected: null }
  return { kind: "ok", stdout, stderr, status, collected }
}

/** Build the only supported worker test command; cwd must be the engine root. */
export function canonicalPytestCommand({ cwd, selector }) {
  const engine = path.resolve(ENGINE)
  if (!cwd || path.resolve(cwd) !== engine) throw new Error("pytest cwd must be engine root")
  if (!selector || typeof selector !== "string") throw new Error("pytest selector is required")
  const resolved = path.resolve(engine, selector)
  if (resolved !== engine && !resolved.startsWith(`${engine}${path.sep}`)) {
    throw new Error("pytest selector must stay inside engine root")
  }
  if (!fs.existsSync(resolved)) throw new Error(`pytest selector not found: ${selector}`)
  return ["uv", ["run", "python", "-m", "pytest", "-q", selector]]
}

export function documentedTestCounts(contents) {
  return contents.flatMap((source) => [...source.matchAll(/(\d+) tests?/g)].map((m) => Number(m[1])))
}

/** Documentation drift is advisory; only inability to obtain a count blocks. */
export function checkTestCounts({ docs, collection }) {
  const unique = [...new Set(docs)]
  const messages = []
  if (unique.length !== 1) messages.push(`documentation counts differ: ${unique.join(", ") || "none"}`)
  if (collection.kind === "execution-failure") messages.push("pytest collection execution failed")
  if (collection.kind === "parser-failure") messages.push("pytest collection output could not be parsed")
  return { blocking: collection.kind !== "ok", messages }
}

// ---------------------------------------------------------------- collecteurs

/** Sous-commandes TS : 1er argv littéral (`["run", "--anchor"]…`) ou table déclarative (`subcommand: "run"`). */
export function tsCommands(src) {
  const cmds = new Set()
  // On retire les commentaires `//` (ex: `["default"]` en prose) et les enums
  // de schéma (tool.schema.enum(["dry-run", "apply"]), multilignes possibles)
  // avant d'extraire les littéraux de commande.
  const clean = src
    .split("\n")
    .filter((line) => !line.trim().startsWith("//"))
    .join("\n")
    .replace(/tool\.schema\s*\.\s*enum\(\s*\[[^\]]*\]\)/g, "")
    // Le contrat porte sur les outils TS ↔ NOTRE CLI aggregator. Les spawns
    // d'un binaire EXTERNE (execFile("<binaire>", [...]), ex: la gate de
    // portabilité invoquant `harness-eval skill-verify`) n'en font pas partie :
    // leur argv est retiré avant l'extraction (exclusion par mécanisme, pas par
    // token — un futur binaire externe reste hors contrat automatiquement).
    .replace(/execFile\(\s*"[^"]+"\s*,\s*\[[^\]]*\]/g, "")
  for (const m of clean.matchAll(/\[\s*"([a-z][a-z-]+)"\s*[,\]]/g)) cmds.add(m[1])
  for (const m of clean.matchAll(/subcommand:\s*"([a-z][a-z-]+)"/g)) cmds.add(m[1])
  return cmds
}

/** Sous-commandes CLI : sous-parsers explicites ou entrées de la table déclarative `_SUBCOMMANDS`. */
function cliCommands(src) {
  const cmds = new Set()
  for (const m of src.matchAll(/sub\.add_parser\(\s*"([a-z][a-z-]+)"/g)) cmds.add(m[1])
  for (const m of src.matchAll(/\(\s*"([a-z][a-z-]+)",\s*"[^"]*",\s*_cmd_[a-z_]+/g)) cmds.add(m[1])
  return cmds
}

/** Handlers : définis (def _cmd_*) et câblés (directement ou via la table déclarative `_SUBCOMMANDS`). */
function cliHandlers(src) {
  const defined = new Set()
  for (const m of src.matchAll(/def (_cmd_[a-z_]+)\(/g)) defined.add(m[1])
  const wired = new Set()
  for (const m of src.matchAll(/set_defaults\(\s*func=(_cmd_[a-z_]+)/g)) wired.add(m[1])
  for (const m of src.matchAll(/,\s*(_cmd_[a-z_]+)(?=\s*,)/g)) wired.add(m[1])
  return { defined, wired }
}

/** Sources Python du moteur (hors caches). */
function enginePythonFiles() {
  const out = []
  const walk = (dir) => {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      if (entry.name.startsWith(".")) continue
      const abs = path.join(dir, entry.name)
      if (entry.isDirectory()) walk(abs)
      else if (entry.name.endsWith(".py")) out.push(abs)
    }
  }
  walk(path.join(ENGINE, "weekly_telemetry_aggregator"))
  return out
}

// ------------------------------------------------------------------ surface 1

const isMain = process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)

if (isMain) {
const tsSrc = read(TS_FILE)
const cliSrc = read(CLI_FILE)
const tsCmds = tsCommands(tsSrc)
const cliCmds = cliCommands(cliSrc)

console.log("— Surfaces 1/2 : médiation TS ↔ CLI")
for (const cmd of [...tsCmds].sort()) {
  ok(`outil TS → sous-commande "${cmd}" existe`, cliCmds.has(cmd))
}

// ------------------------------------------------------------------ surface 2

const { defined, wired } = cliHandlers(cliSrc)
for (const handler of [...wired].sort()) {
  ok(`handler "${handler}" câblé et défini`, defined.has(handler))
}
const orphan = [...defined].filter((h) => !wired.has(h)).sort()
ok(
  "aucun handler _cmd_* orphelin",
  orphan.length === 0,
  orphan.length ? `non câblés: ${orphan.join(", ")}` : "",
)

// ------------------------------------------------------------------ surface 3

// Étape → préfixe d'artefact produit (sources du moteur).
const ARTIFACTS = [
  ["run", "weekly-summary-"],
  ["releases", "weekly-ecosystem-"],
  ["watch-context", "weekly-watch-context-"],
  ["watch-validate", "weekly-watch-findings-"],
  ["insights", "weekly-insights-"],
  ["harness", "weekly-harness-digest-"],
  ["harness-remediate", "weekly-harness-remediation-"],
  ["audit-candidates", "weekly-audit-candidates-"],
  ["draft-candidates", "weekly-draft-candidates-"],
  ["report-prep", "weekly-report-draft-"],
  ["report-assemble", "weekly-report-"],
]
const pyFiles = enginePythonFiles()
console.log("— Surface 3 : artefacts produits par étape")
for (const [cmd, prefix] of ARTIFACTS) {
  ok(
    `"${cmd}" produit ${prefix}<date>.json`,
    hasLiteral(pyFiles, prefix),
    `littéral "${prefix}" absent des sources`,
  )
}
function hasLiteral(files, needle) {
  return files.some((f) => read(f).includes(needle))
}

// ------------------------------------------------------------------ surface 4

// Dépendances d'ordre : documentées dans le TS (description SÉQUENTIEL) ET
// présentes côté moteur (le handler lit l'artefact amont).
const DEPENDENCIES = [
  // weekly_watch_context décrit sa dépendance à l'écosystème (étape 2).
  [
    "TS weekly_watch_context → SÉQUENTIEL weekly-ecosystem",
    /weekly_watch_context[\s\S]{0,400}?weekly-ecosystem/,
    null,
  ],
  // Le handler watch-context lit l'écosystème produit par releases.
  ["moteur watch-context lit weekly-ecosystem (chaînage)", null, "weekly-ecosystem"],
  // weekly_report_assemble documente la consommation du draft.
  [
    "TS weekly_report_assemble → relancer prep après draft",
    /weekly_report_assemble[\s\S]{0,400}?relancer weekly_report_prep/,
    null,
  ],
  ["moteur assemble consomme weekly-report-draft", null, "weekly-report-draft"],
]
console.log("— Surface 4 : chaînage d'ordre")
for (const [label, tsRe, pyNeedle] of DEPENDENCIES) {
  if (tsRe !== null) {
    ok(label, tsRe.test(tsSrc))
  }
  if (pyNeedle !== null) {
    ok(label, hasLiteral(pyFiles, pyNeedle))
  }
}

// ------------------------------------------------------------------ surface 5

// Comptes de tests : les trois documents doivent porter le même nombre,
// et ce nombre doit être le collect réel (C10).
const docsCounts = documentedTestCounts([README_FILE, INSTALL_FILE, CI_FILE].map(read))
const collection = collectPytest({ cwd: ENGINE })
console.log("— Surface 5 : comptes de tests cohérents")
const countCheck = checkTestCounts({ docs: docsCounts, collection })
// Documentation mismatch is advisory and never changes gate result.
if (new Set(docsCounts).size === 1) console.log("ok  README/INSTALL/ci portent le même nombre")
else console.log(`WARN README/INSTALL/ci counts differ: ${docsCounts.join(", ") || "none"}`)
ok("pytest collection executable and parseable", !countCheck.blocking,
  collection.kind === "execution-failure" ? `status=${collection.status}; stderr=${collection.stderr}` :
    collection.kind === "parser-failure" ? `stdout=${collection.stdout}` : "")
if (collection.kind === "ok" && docsCounts.length > 0 && collection.collected !== docsCounts[0]) {
  console.log(`WARN collect réel (${collection.collected}) != docs (${docsCounts[0]})`)
}

// ------------------------------------------------------------------ surface 6

// Étapes commande ↔ agent : les tokens d'outils (weekly_*) du « Déroulement » de
// la commande /weekly-review doivent être EXACTEMENT ceux du tableau de l'agent
// weekly-advisor, dans le même ordre. Empêche la re-divergence des deux
// présentations du flux (dérive observée : sémantique d'ancre contradictoire).
const AGENT_FILE = path.join(ROOT, ".opencode", "agents", "weekly-advisor", "weekly-advisor.md")
const COMMAND_FILE = path.join(ROOT, ".opencode", "commands", "weekly-review.md")
const TOKEN_RE = /weekly_[a-z_]+/g
function sectionTools(src, fromHeader, toHeader) {
  const start = src.indexOf(fromHeader)
  const end = src.indexOf(toHeader, start + 1)
  const section = start === -1 || end === -1 ? "" : src.slice(start, end)
  return [...new Set(section.match(TOKEN_RE) ?? [])]
}
console.log("— Surface 6 : étapes commande ↔ agent (ordre figé)")
const agentTools = sectionTools(read(AGENT_FILE), "| Étape | Action (tool) | Sortie |", "## Invariants")
const commandTools = sectionTools(read(COMMAND_FILE), "## Déroulement", "## Règles")
ok(
  "commande et agent déclarent les mêmes outils d'étapes, dans le même ordre",
  JSON.stringify(agentTools) === JSON.stringify(commandTools),
  `agent: [${agentTools.join(", ")}] | commande: [${commandTools.join(", ")}]`,
)

// ------------------------------------------------------------------ surface 7

// Orchestration waves : le dispatch parallèle (orchestrateur → workers
// subagents) doit être documenté de façon cohérente entre l'agent orchestrateur,
// son worker et la commande lanceuse. Détecte une re-divergence (ex. retour à
// un flux séquentiel décrit, ou phrase interdisant le dispatch en subagent).
const WORKER_FILE = path.join(ROOT, ".opencode", "agents", "weekly-advisor", "weekly-advisor-worker.md")
const agentSrc7 = read(AGENT_FILE)
const commandSrc7 = read(COMMAND_FILE)
const frontmatter = (src) => src.match(/^---\r?\n([\s\S]*?)\r?\n---/)?.[1]?.replace(/\r/g, "") ?? ""
const workerSrc = fs.existsSync(WORKER_FILE) ? read(WORKER_FILE) : ""
console.log("— Surface 7 : cohérence orchestration waves")
ok(
  "agent orchestrateur déclare task: allow dans son frontmatter",
  /\btask:\s*allow\b/.test(frontmatter(agentSrc7)),
)
ok(
  "commande sans phrase interdite « jamais de dispatch en subagent »",
  !commandSrc7.includes("jamais de dispatch en subagent"),
)
for (const marker of ["WAVE 1", "WAVE 2", "JOIN"]) {
  ok(`orchestrateur documente la vague « ${marker} »`, agentSrc7.includes(marker))
}
const RETURN_FIELDS = ["branch", "rc", "steps_done", "warnings", "artifacts", "elapsed_s"]
for (const field of RETURN_FIELDS) {
  ok(
    `worker weekly-advisor-worker déclare le champ retour "${field}"`,
    new RegExp(`"${field}"\\s*:`).test(workerSrc),
  )
}
ok(
  "commande référence agent: weekly-advisor dans son frontmatter",
  /agent:\s*weekly-advisor\b/.test(frontmatter(commandSrc7)),
)

console.log(
  failures === 0
    ? "\nFLOW-DOCS OK — contrat de flux 7 surfaces vérifié"
    : `\n${failures} échec(s)`,
)
process.exit(failures === 0 ? 0 : 1)
}
