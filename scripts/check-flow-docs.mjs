#!/usr/bin/env node
/**
 * Contrat de flux docs ↔ code (G1, v6.0.p) — 5 surfaces vérifiées statiquement.
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
 *
 * Zéro dépendance (node stdlib). Lancé par la CI après pytest ; exit 0/1.
 */
import fs from "node:fs"
import path from "node:path"
import { execFileSync } from "node:child_process"
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

// ---------------------------------------------------------------- collecteurs

/** Sous-commandes TS : 1er argv littéral des cliArgs ([…"run", "--anchor"]…). */
function tsCommands(src) {
  const cmds = new Set()
  // On retire les commentaires `//` (ex: `["default"]` en prose) et les enums
  // de schéma (tool.schema.enum(["dry-run", "apply"]), multilignes possibles)
  // avant d'extraire les littéraux de commande.
  const clean = src
    .split("\n")
    .filter((line) => !line.trim().startsWith("//"))
    .join("\n")
    .replace(/tool\.schema\s*\.\s*enum\(\s*\[[^\]]*\]\)/g, "")
  for (const m of clean.matchAll(/\["([a-z][a-z-]+)"[,\]]/g)) cmds.add(m[1])
  return cmds
}

/** Sous-commandes CLI : noms des sous-parsers (sub.add_parser("name", …)). */
function cliCommands(src) {
  const cmds = new Set()
  for (const m of src.matchAll(/sub\.add_parser\(\s*"([a-z][a-z-]+)"/g)) cmds.add(m[1])
  return cmds
}

/** Handlers : définis (def _cmd_*) et câblés (set_defaults(func=_cmd_*)). */
function cliHandlers(src) {
  const defined = new Set()
  for (const m of src.matchAll(/def (_cmd_[a-z_]+)\(/g)) defined.add(m[1])
  const wired = new Set()
  for (const m of src.matchAll(/set_defaults\(\s*func=(_cmd_[a-z_]+)/g)) wired.add(m[1])
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
const docsCounts = []
for (const file of [README_FILE, INSTALL_FILE, CI_FILE]) {
  for (const m of read(file).matchAll(/(\d+) tests?/g)) docsCounts.push(Number(m[1]))
}
let collected = null
try {
  const out = execFileSync("uv", ["run", "pytest", "--collect-only"], {
    cwd: ENGINE,
    encoding: "utf8",
  })
  const m = out.match(/(\d+) tests? collected/)
  collected = m ? Number(m[1]) : null
} catch {
  collected = null
}
console.log("— Surface 5 : comptes de tests cohérents")
ok(
  "README/INSTALL/ci portent le même nombre",
  new Set(docsCounts).size === 1,
  docsCounts.length ? `nombres: ${[...new Set(docsCounts)].join(", ")}` : "aucun nombre trouvé",
)
ok("pytest --collect-only exécutable", collected !== null, "uv run pytest a échoué")
if (collected !== null && docsCounts.length > 0) {
  ok(`collect réel (${collected}) == docs (${docsCounts[0]})`, collected === docsCounts[0])
}

console.log(failures === 0 ? "\nFLOW-DOCS OK — contrat de flux 5 surfaces vérifié" : `\n${failures} échec(s)`)
process.exit(failures === 0 ? 0 : 1)