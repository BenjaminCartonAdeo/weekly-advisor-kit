/**
 * Smoke test d'exécution du plugin TS (CI, node seul — zéro dépendance).
 *
 * Chargé en régression du layout v6.0 : `anchorTool`/`noArgTool` référençaient
 * `worktree` hors de leur portée → `ReferenceError: worktree is not defined` au
 * premier appel de tool. `node --check` ne détectait pas le bug (syntaxe OK).
 *
 * Vérifie en vraie exécution, avec un faux moteur (python factice) :
 *   1. instanciation du plugin + `weekly_doctor.execute({})` (closure noArgTool)
 *   2. `weekly_run.execute({ lookback_days: 21 })` (closure anchorTool + arg)
 *   3. propagation CLI : `--lookback-days 21` présent dans les argv du run
 *   4. ancre glissante : créée si absente, conservée dans la journée,
 *      rafraîchie chaque jour (fenêtre jamais figée, v6.0.n)
 *
 * Exit 0 = OK ; sinon message + exit 1 (CI).
 */
import { register } from "node:module"
import fs from "node:fs"
import os from "node:os"
import path from "node:path"

register(new URL("./plugin-smoke-hook.mjs", import.meta.url))

const { WeeklyAdvisorPlugin } = await import("../.opencode/plugins/weekly-advisor.ts")

// ---------------------------------------------------------------- faux moteur

const fakeRoot = fs.mkdtempSync(path.join(os.tmpdir(), "wa-smoke-"))
const engine = path.join(fakeRoot, ".opencode", "plugins", "weekly-advisor-engine")
const venvBin = path.join(engine, ".venv", "bin")
const outputDir = path.join(fakeRoot, "reports")
fs.mkdirSync(venvBin, { recursive: true })
fs.mkdirSync(outputDir, { recursive: true })

const argvLog = path.join(fakeRoot, "argv.log")
// python factice : journalise ses argv, répond OK, sort 0 (jamais d'appel réseau).
const fakePython = path.join(venvBin, "python")
fs.writeFileSync(
  fakePython,
  `#!/bin/sh\necho "$@" > "${argvLog}"\necho "OK"\nexit 0\n`,
  { mode: 0o755 },
  "utf8",
)
// config minimale — fenêtre 7 j (aucune édition de config dans le test, comme au runtime)
fs.writeFileSync(
  path.join(engine, "weekly-telemetry-config.json"),
  JSON.stringify({ project_root: fakeRoot, output_dir: outputDir, lookback_days: 7 }, null, 2),
  "utf8",
)

const failures = []
function check(label, cond, detail = "") {
  if (!cond) failures.push(`${label}${detail ? ` — ${detail}` : ""}`)
  console.log(`${cond ? "ok" : "FAIL"}  ${label}`)
}

// ---------------------------------------------------------------- exécution

const plugin = await WeeklyAdvisorPlugin({ worktree: fakeRoot, directory: fakeRoot })
const tools = plugin.tool

// 1. noArgTool : closure résolue (le bug v6.0 plantait ici en ReferenceError)
try {
  const out = await tools.weekly_doctor.execute({})
  check("weekly_doctor.execute ({}) → promise résolue", out === "OK", out)
} catch (err) {
  check("weekly_doctor.execute ({}) → promise résolue", false, String(err))
}

// 2+3. anchorTool + arg lookback_days propagé au CLI
try {
  const out = await tools.weekly_run.execute({ lookback_days: 21 })
  const argv = fs.readFileSync(argvLog, "utf8").trim().split(" ")
  check("weekly_run.execute ({lookback_days:21}) → OK", out === "OK", out)
  check("argv run contient --lookback-days 21", argv.includes("--lookback-days") && argv.includes("21"), argv.join(" "))
  check("argv run contient --force", argv.includes("--force"))
  const anchorLine = fs.readFileSync(path.join(outputDir, "anchor-last.txt"), "utf8").trim()
  check("ancre créée au 1er run", /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/.test(anchorLine), anchorLine)
} catch (err) {
  check("weekly_run.execute ({lookback_days:21}) → OK", false, String(err))
}

// 4a. ancre fraîche conservée (même fenêtre) → argv inchangé
fs.writeFileSync(argvLog, "RESET")
await tools.weekly_run.execute({})
const argv2 = fs.readFileSync(argvLog, "utf8").trim().split(" ")
const anchor2 = fs.readFileSync(path.join(outputDir, "anchor-last.txt"), "utf8").trim()
check(
  "ancre du jour conservée (stabilité intra-run)",
  argv2[argv2.indexOf("--anchor") + 1] === anchor2,
  `${argv2.join(" ")} | ${anchor2}`,
)

// 4b. ancre d'un autre jour rafraîchie → nouvelle ancre récente
const old = new Date(Date.now() - 30 * 24 * 3600 * 1000).toISOString().replace(/\.\d{3}Z$/, "Z")
fs.writeFileSync(path.join(outputDir, "anchor-last.txt"), old)
fs.writeFileSync(argvLog, "RESET")
await tools.weekly_run.execute({})
const anchor3 = fs.readFileSync(path.join(outputDir, "anchor-last.txt"), "utf8").trim()
check("ancre périmée rafraîchie (cadence non figée)", anchor3 !== old, `${old} → ${anchor3}`)
check("nouvelle ancre ~maintenant", Date.now() - Date.parse(anchor3) < 60_000, anchor3)

// 5. sans lookback_days : aucun flag ajouté
fs.writeFileSync(argvLog, "RESET")
await tools.weekly_run.execute({})
const argv4 = fs.readFileSync(argvLog, "utf8").trim().split(" ")
check("sans lookback_days : flag absent", !argv4.includes("--lookback-days"), argv4.join(" "))

// 6. tools report : ancre propagée (v6.0.e — sinon report-prep vise la date du
//    jour au lieu de la fenêtre du run → FATAL « summary absente »)
for (const [name, cmd] of [
  ["weekly_report_prep", "report-prep"],
  ["weekly_report_blocks_draft", "report-blocks-draft"],
  ["weekly_report_assemble", "report-assemble"],
]) {
  fs.writeFileSync(argvLog, "RESET")
  await tools[name].execute({})
  const argvN = fs.readFileSync(argvLog, "utf8").trim().split(" ")
  const idx = argvN.indexOf("--anchor")
  check(
    `${name} passe --anchor`,
    idx >= 0 && idx + 1 < argvN.length && argvN[idx + 1] === anchor3,
    argvN.join(" "),
  )
}

// 7. watch context/validation : ancre propagée et tools exposés
for (const [name, cmd] of [
  ["weekly_watch_context", "watch-context"],
  ["weekly_watch_validate", "watch-validate"],
]) {
  fs.writeFileSync(argvLog, "RESET")
  await tools[name].execute({})
  const argvN = fs.readFileSync(argvLog, "utf8").trim().split(" ")
  const idx = argvN.indexOf("--anchor")
  check(
    `${name} passe --anchor`,
    idx >= 0 && idx + 1 < argvN.length && argvN[idx + 1] === anchor3,
    argvN.join(" "),
  )
  check(`${name} appelle ${cmd}`, argvN.includes(cmd), argvN.join(" "))
}

// 8. remediator : proposition bornée et mode dry-run propagés
fs.writeFileSync(argvLog, "RESET")
await tools.weekly_harness_remediate.execute({
  proposal_file: "/tmp/weekly-harness-proposals.json",
  mode: "dry-run",
})
const remediationArgv = fs.readFileSync(argvLog, "utf8").trim().split(" ")
check("weekly_harness_remediate appelle harness-remediate", remediationArgv.includes("harness-remediate"))
check("remediator dry-run par défaut explicite", remediationArgv.includes("dry-run"), remediationArgv.join(" "))
check(
  "remediator passe --anchor",
  remediationArgv.includes("--anchor") && remediationArgv[remediationArgv.indexOf("--anchor") + 1] === anchor3,
  remediationArgv.join(" "),
)

if (failures.length) {
  console.error(`\nSMOKE FAIL (${failures.length})\n- ${failures.join("\n- ")}`)
  process.exit(1)
}
console.log("\nSMOKE OK — plugin chargé et exécuté (closure, lookback, ancre)")
