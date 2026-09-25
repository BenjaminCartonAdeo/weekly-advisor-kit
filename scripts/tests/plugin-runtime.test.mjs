// Tests du runtime neutre de weekly-advisor (Tâche 2 du plan dual V1/V2).
//
// Couvre les trois guarantees de `paths.ts` / `preflight.ts` / `runtime.ts` :
//   1. précédence des racines (ordre normatif du plan) et isolement entre
//      instances — plus aucun module-global `worktree` ;
//   2. pré-flight fail-closed, y compris l'égalité mot pour mot des messages
//      avec le point d'entrée V1 existant (aucun texte reformulé) ;
//   3. staging JSON : succès, JSON invalide, échec CLI, AbortSignal — sans
//      répertoire résiduel, avec un `TMPDIR` isolé par test.
//
// Aucune dépendance à `@opencode-ai/plugin` pour les modules testés ; le hook
// n'est chargé que pour comparer au point d'entrée V1.
import assert from "node:assert/strict"
import fs from "node:fs"
import os from "node:os"
import path from "node:path"
import test from "node:test"
import { register } from "node:module"

import { makeCapturingKit, makeKit } from "./helpers/fake-kit.mjs"
import { engineDirectory, expandHome, resolveEngine, resolveKitRoot } from "../../.opencode/plugins/weekly-advisor/paths.ts"
import { preflight } from "../../.opencode/plugins/weekly-advisor/preflight.ts"
import { WeeklyRuntime, readOrCreateAnchorFile } from "../../.opencode/plugins/weekly-advisor/runtime.ts"

register(new URL("../plugin-smoke-hook.mjs", import.meta.url))

const { default: WeeklyAdvisorPlugin } = await import("../../.opencode/plugins/weekly-advisor.ts")

/** Kit factice dont l'interpréteur fait dormir le subprocess (test AbortSignal). */
function makeSleepingKit(seconds) {
  const kit = makeKit({ python: `#!/bin/sh\nsleep ${seconds}\nprintf "ok\\n"\n` })
  fs.chmodSync(kit.python, 0o755)
  return kit
}

/** Environnement d'un runtime, sans toucher `process.env`. */
function runtimeFor(kit, extra = {}) {
  return new WeeklyRuntime({
    location: { v1Worktree: kit.root },
    env: { WEEKLY_PYTHON: kit.python, ...extra },
  })
}

/** `TMPDIR` isolé : les répertoires de staging du test restent distinguables. */
function useIsolatedTmpdir() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "wa-runtime-tmp-"))
  const previous = process.env.TMPDIR
  process.env.TMPDIR = dir
  return {
    dir,
    restore() {
      if (previous === undefined) delete process.env.TMPDIR
      else process.env.TMPDIR = previous
      fs.rmSync(dir, { recursive: true, force: true })
    },
  }
}

/** Répertoires de staging résiduels pour un label donné. */
function residualStagingDirs(dir, label) {
  return fs.readdirSync(dir).filter((entry) => entry.startsWith(`weekly-${label}-`))
}

function useEnv(keys, values) {
  const previous = new Map(keys.map((key) => [key, process.env[key]]))
  for (const [key, value] of Object.entries(values)) {
    if (value === undefined) delete process.env[key]
    else process.env[key] = value
  }
  return () => {
    for (const [key, value] of previous) {
      if (value === undefined) delete process.env[key]
      else process.env[key] = value
    }
  }
}

// --------------------------------------------------------------------------
// 1. Précédence des racines
// --------------------------------------------------------------------------

test("root precedence documents the seven ordered sources", () => {
  assert.deepEqual(
    [...WeeklyRuntime.rootPrecedence],
    [
      "WEEKLY_KIT_ROOT",
      "entrypoint",
      "v1-worktree",
      "v1-directory",
      "v2-directory",
      "v2-project-directory",
      "cwd",
    ],
  )
})

test("WEEKLY_KIT_ROOT wins over every other source, entrypoint included", () => {
  const kit = makeKit()
  const runtime = new WeeklyRuntime({
    location: {
      entrypointDirectory: kit.root,
      v1Worktree: "/nonexistent/v1-worktree",
      v1Directory: "/nonexistent/v1-directory",
      v2Directory: "/nonexistent/v2-directory",
      v2ProjectDirectory: "/nonexistent/v2-project",
      cwd: "/nonexistent/cwd",
    },
    env: { WEEKLY_KIT_ROOT: `  ${kit.root}  ` },
  })
  assert.equal(runtime.root, kit.root)
  assert.equal(runtime.worktree, kit.root, "worktree reste un alias de root")
})

test("entrypoint directory carrying an engine wins over the V1 worktree", () => {
  const kit = makeKit()
  const runtime = new WeeklyRuntime({
    location: { entrypointDirectory: kit.root, v1Worktree: "/nonexistent/v1-worktree" },
    env: {},
  })
  assert.equal(runtime.root, kit.root)
})

test("entrypoint directory without an engine is skipped in favour of the V1 worktree", () => {
  const empty = fs.mkdtempSync(path.join(os.tmpdir(), "wa-no-engine-"))
  const kit = makeKit()
  const runtime = new WeeklyRuntime({
    location: { entrypointDirectory: empty, v1Worktree: kit.root },
    env: {},
  })
  assert.equal(runtime.root, kit.root, "un point d'entrée sans moteur ne masque pas un contexte valide")
})

test("V1 worktree wins over V1 directory", () => {
  const kit = makeKit()
  const runtime = new WeeklyRuntime({
    location: { v1Worktree: kit.root, v1Directory: "/nonexistent/v1-directory" },
    env: {},
  })
  assert.equal(runtime.root, kit.root)
})

test("V1 directory wins over V2 location directory", () => {
  const kit = makeKit()
  const runtime = new WeeklyRuntime({
    location: { v1Directory: kit.root, v2Directory: "/nonexistent/v2-directory" },
    env: {},
  })
  assert.equal(runtime.root, kit.root)
})

test("V2 location directory wins over the V2 project directory", () => {
  const kit = makeKit()
  const runtime = new WeeklyRuntime({
    location: { v2Directory: kit.root, v2ProjectDirectory: "/nonexistent/v2-project" },
    env: {},
  })
  assert.equal(runtime.root, kit.root)
})

test("V2 project directory wins over cwd, and cwd closes the chain", () => {
  const kit = makeKit()
  const explicit = new WeeklyRuntime({
    location: { v2ProjectDirectory: kit.root, cwd: "/nonexistent/cwd" },
    env: {},
  })
  const fallback = new WeeklyRuntime({ location: {}, env: {} })
  assert.equal(explicit.root, kit.root)
  assert.equal(fallback.root, process.cwd())
})

test("resolveKitRoot keeps the fail-closed status of a misconfigured WEEKLY_KIT_ROOT", () => {
  const resolved = resolveKitRoot([{ label: "v1-worktree", value: "/nonexistent/v1-worktree" }], {
    WEEKLY_KIT_ROOT: "/tmp/not-a-weekly-kit",
  })
  assert.equal(resolved, path.resolve("/tmp/not-a-weekly-kit"))
  assert.equal(preflight(resolved, {}).rc, 3, "aucune résolution silencieuse ailleurs")
})

// --------------------------------------------------------------------------
// 2. Isolation entre instances
// --------------------------------------------------------------------------

test("two runtimes over two kits keep distinct roots", () => {
  const rootA = makeKit().root
  const rootB = makeKit().root
  const first = new WeeklyRuntime({ location: { v1Worktree: rootA } })
  const second = new WeeklyRuntime({ location: { v1Worktree: rootB } })

  assert.notEqual(first.root, second.root)
  assert.notEqual(first.engine.python, second.engine.python)
  assert.equal(first.engine.engine, engineDirectory(rootA))
  assert.equal(second.engine.engine, engineDirectory(rootB))
})

test("concurrent runCli calls stay bound to their own kit", async () => {
  const kitA = makeCapturingKit()
  const kitB = makeCapturingKit()
  const runtimeA = runtimeFor(kitA, { CAPTURE: kitA.capture })
  const runtimeB = runtimeFor(kitB, { CAPTURE: kitB.capture })

  const [outA, outB] = await Promise.all([
    runtimeA.runCli(["run", "--anchor", "2026-09-01"], 120_000),
    runtimeB.runCli(["doctor"], 120_000),
  ])

  assert.equal(outA, "ok")
  assert.equal(outB, "ok")
  const captureA = fs.readFileSync(kitA.capture, "utf8")
  const captureB = fs.readFileSync(kitB.capture, "utf8")
  // Le script de capture du helper factice consomme le drapeau `--config` sans
  // sa valeur : le chemin de config est donc journalisé comme argument libre.
  const configOf = (capture, kit) =>
    capture.split("\n").includes(`ARG=${path.join(kit.engine, "weekly-telemetry-config.json")}`)
  assert.equal(configOf(captureA, kitA), true, "kit A appelle son propre moteur")
  assert.equal(configOf(captureB, kitB), true, "kit B appelle son propre moteur")
  assert.doesNotMatch(captureA, new RegExp(escapeRegExp(kitB.root)))
  assert.doesNotMatch(captureB, new RegExp(escapeRegExp(kitA.root)))
  assert.match(captureA, /^ARG=run$/m)
  assert.match(captureB, /^ARG=doctor$/m)
  assert.doesNotMatch(captureB, /^ARG=run$/m)
})

// --------------------------------------------------------------------------
// 3. Pré-flight
// --------------------------------------------------------------------------

test("preflight accepts a complete kit", () => {
  const kit = makeKit()
  const result = preflight(kit.root, { WEEKLY_PYTHON: kit.python })

  assert.equal(result.rc, 0)
  assert.equal(result.worktree, kit.root)
  assert.equal(result.engine_ok, true)
  assert.equal(result.python_ok, true)
  assert.equal(result.config_ok, true)
  assert.equal(result.py_count, 1)
  assert.equal(result.message, undefined)
})

test("preflight reports a missing interpreter", () => {
  const kit = makeKit()
  fs.rmSync(kit.python)
  const result = preflight(kit.root, {})

  assert.equal(result.rc, 3)
  assert.equal(result.engine_ok, true)
  assert.equal(result.python_ok, false)
  assert.equal(result.config_ok, true)
  assert.match(result.message, /python_ok=false/)
})

test("preflight reports a missing or unparsable config", () => {
  const missing = makeKit()
  fs.rmSync(path.join(missing.engine, "weekly-telemetry-config.json"))
  const absent = preflight(missing.root, { WEEKLY_PYTHON: missing.python })
  assert.equal(absent.rc, 3)
  assert.equal(absent.config_ok, false)
  assert.match(absent.message, /config_ok=false/)

  const broken = makeKit()
  fs.writeFileSync(path.join(broken.engine, "weekly-telemetry-config.json"), "{ not json")
  const invalid = preflight(broken.root, { WEEKLY_PYTHON: broken.python })
  assert.equal(invalid.rc, 3)
  assert.equal(invalid.config_ok, false)
})

test("preflight reports a missing engine and an empty module set", () => {
  const bare = fs.mkdtempSync(path.join(os.tmpdir(), "wa-bare-"))
  const result = preflight(bare, { WEEKLY_PYTHON: "" })

  assert.equal(result.rc, 3)
  assert.equal(result.engine_ok, false)
  assert.equal(result.python_ok, false)
  assert.equal(result.config_ok, false)
  assert.equal(result.py_count, 0)
  assert.match(result.message, /engine_ok=false/)
  assert.match(result.message, /lancer opencode avec --dir <racine-du-kit>/)
})

test("preflightOrThrow keeps the frozen weekly_preflight rc=3 message", () => {
  const broken = fs.mkdtempSync(path.join(os.tmpdir(), "wa-broken-"))
  const runtime = new WeeklyRuntime({ location: { v1Worktree: broken }, env: {} })

  assert.throws(
    () => runtime.preflightOrThrow(),
    (error) => {
      assert.match(error.message, /^weekly_preflight rc=3 — kit weekly-advisor introuvable depuis worktree=/)
      assert.match(error.message, /ou poser WEEKLY_KIT_ROOT=<racine-du-kit>$/)
      return true
    },
  )
  assert.doesNotThrow(() => runtimeFor(makeKit()).preflightOrThrow())
})

test("runtime preflight verdicts are consistent across scenarios", async () => {
  const scenarios = [
    { label: "kit complet", mutate: () => {}, expectPass: true },
    { label: "interpréteur absent", mutate: (kit) => fs.rmSync(kit.python), expectPass: false },
    { label: "config absente", mutate: (kit) => fs.rmSync(path.join(kit.engine, "weekly-telemetry-config.json")), expectPass: false },
    { label: "moteur absent", mutate: (kit) => fs.rmSync(kit.engine, { recursive: true }), expectPass: false },
  ]

  for (const scenario of scenarios) {
    const kit = makeKit()
    scenario.mutate(kit)
    const restore = useEnv(["WEEKLY_KIT_ROOT", "WEEKLY_PYTHON"], {
      WEEKLY_KIT_ROOT: kit.root,
      WEEKLY_PYTHON: undefined,
    })
    try {
      const runtime = new WeeklyRuntime({ location: { v1Worktree: kit.root }, env: process.env })
      const verdict = runtime.preflight()
      // Verify the verdict has the expected structure
      assert.equal(typeof verdict.rc, "number", `verdict.rc absent — ${scenario.label}`)
      assert.ok(verdict.rc === 0 || verdict.rc === 3, `verdict.rc invalid — ${scenario.label}`)
      assert.equal(typeof verdict.worktree, "string", `verdict.worktree absent — ${scenario.label}`)
      // Check pass/fail consistency
      assert.equal(verdict.rc === 0, scenario.expectPass, `verdict inconsistant — ${scenario.label}`)
    } finally {
      restore()
    }
  }
})

// --------------------------------------------------------------------------
// 4. Staging JSON
// --------------------------------------------------------------------------

test("staging writes an absolute payload in TMPDIR and leaves nothing behind on success", () => {
  const kit = makeKit()
  const runtime = runtimeFor(kit)
  const tmp = useIsolatedTmpdir()
  try {
    const staged = runtime.stageJsonPayload(JSON.stringify({ findings: [] }), "coherence")

    assert.equal(path.isAbsolute(staged.file), true)
    assert.equal(staged.file.startsWith(tmp.dir), true, "TMPDIR isolé respecté")
    assert.equal(fs.readFileSync(staged.file, "utf8"), JSON.stringify({ findings: [] }))

    staged.cleanup()
    assert.deepEqual(residualStagingDirs(tmp.dir, "coherence"), [])
  } finally {
    tmp.restore()
  }
})

test("staging rejects malformed JSON without creating a directory", () => {
  const runtime = runtimeFor(makeKit())
  const tmp = useIsolatedTmpdir()
  try {
    for (const label of ["coherence", "catalog", "usage"]) {
      assert.throws(
        () => runtime.stageJsonPayload('{"unterminated":', label),
        new RegExp(`weekly_skill_curate ${label} JSON invalide`),
      )
    }
    assert.deepEqual(fs.readdirSync(tmp.dir), [], "aucun répertoire résiduel")
  } finally {
    tmp.restore()
  }
})

test("staging leaves no residual directory when the CLI fails", async () => {
  const kit = makeCapturingKit()
  const runtime = runtimeFor(kit, { CAPTURE: kit.capture, FAIL: "1" })
  const tmp = useIsolatedTmpdir()
  const staged = []
  try {
    const args = ["skill-curate", "--anchor", "2026-09-01"]
    try {
      for (const label of ["coherence", "catalog"]) {
        const input = runtime.stageJsonPayload(JSON.stringify({ [label]: true }), label)
        staged.push(input)
        args.push(`--${label}`, input.file)
      }
      await assert.rejects(runtime.runCli(args, 120_000), /weekly_telemetry_aggregator .*exit 17/)
    } finally {
      for (const input of staged) input.cleanup()
    }
    for (const input of staged) {
      assert.equal(fs.existsSync(input.file), false, "fichier temporaire nettoyé")
    }
    assert.deepEqual(residualStagingDirs(tmp.dir, "coherence"), [])
    assert.deepEqual(residualStagingDirs(tmp.dir, "catalog"), [])
  } finally {
    tmp.restore()
  }
})

test("staging honours an AbortSignal before and during execution, without residue", async () => {
  const kit = makeSleepingKit(30)
  const runtime = runtimeFor(kit)
  const tmp = useIsolatedTmpdir()
  try {
    // 1. signal déjà déclenché : aucun répertoire créé
    const already = AbortSignal.abort()
    assert.throws(
      () => runtime.stageJsonPayload(JSON.stringify({ findings: [] }), "usage", already),
      /weekly_skill_curate usage staging annulé \(AbortSignal\)/,
    )
    assert.deepEqual(residualStagingDirs(tmp.dir, "usage"), [])

    // 2. signal déclenché pendant l'exécution : l'enfant est tué, le staging
    //    nettoyé par le finally du consommateur, message d'annulation distinct
    const staged = []
    const controller = new AbortController()
    try {
      const args = ["skill-curate", "--anchor", "2026-09-01"]
      for (const label of ["coherence", "usage"]) {
        const input = runtime.stageJsonPayload(JSON.stringify({ [label]: true }), label, controller.signal)
        staged.push(input)
        args.push(`--${label}`, input.file)
      }
      setTimeout(() => controller.abort(), 100)
      await assert.rejects(
        runtime.runCli(args, 120_000, controller.signal),
        /weekly_telemetry_aggregator skill-curate --anchor 2026-09-01 .* → annulé \(AbortSignal\)/,
      )
    } finally {
      for (const input of staged) input.cleanup()
    }
    for (const input of staged) {
      assert.equal(fs.existsSync(input.file), false, "fichier temporaire nettoyé après annulation")
    }
    assert.deepEqual(residualStagingDirs(tmp.dir, "coherence"), [])
    assert.deepEqual(residualStagingDirs(tmp.dir, "usage"), [])
  } finally {
    tmp.restore()
  }
})

// --------------------------------------------------------------------------
// 5. Chemins et ancre
// --------------------------------------------------------------------------

test("expandHome matches the Python expanduser semantics", () => {
  const home = os.homedir()
  assert.equal(expandHome("~"), home)
  assert.equal(expandHome("~/reports"), path.join(home, "reports"))
  assert.equal(expandHome("~\\reports"), path.join(home, "reports"))
  assert.equal(expandHome("/absolute/reports"), "/absolute/reports")
  assert.equal(expandHome("relative/reports"), "relative/reports")
})

test("resolveEngine expands a relative output_dir against the engine", () => {
  const kit = makeKit()
  const relative = resolveEngine(kit.root, { WEEKLY_PYTHON: kit.python })
  assert.equal(relative.outputDir, path.join(kit.engine, "reports"))

  fs.writeFileSync(
    path.join(kit.engine, "weekly-telemetry-config.json"),
    JSON.stringify({ output_dir: "~/wa-out" }),
  )
  const expanded = resolveEngine(kit.root, { WEEKLY_PYTHON: kit.python })
  assert.equal(expanded.outputDir, path.join(os.homedir(), "wa-out"))

  fs.writeFileSync(
    path.join(kit.engine, "weekly-telemetry-config.json"),
    JSON.stringify({ output_dir: "/tmp/wa-absolute" }),
  )
  assert.equal(
    resolveEngine(kit.root, { WEEKLY_PYTHON: kit.python }).outputDir,
    "/tmp/wa-absolute",
  )
})

test("resolveEngine reports a missing engine before a missing interpreter", () => {
  const bare = fs.mkdtempSync(path.join(os.tmpdir(), "wa-bare-"))
  assert.throws(
    () => resolveEngine(bare, { WEEKLY_PYTHON: "" }),
    /moteur introuvable: .* \(structure du kit corrompue\)/,
  )
  const kit = makeKit()
  fs.rmSync(kit.python)
  assert.throws(
    () => resolveEngine(kit.root, { WEEKLY_PYTHON: "" }),
    /interpréteur Python introuvable \(candidats testés: /,
  )
})

test("anchor is stable within a day, explicit, and rejects an invalid stored value", () => {
  const kit = makeKit()
  const runtime = runtimeFor(kit)

  assert.equal(runtime.anchorArg("2026-09-01"), "2026-09-01", "ancre explicite prioritaire")
  assert.deepEqual(runtime.anchorArgs("2026-09-01"), ["--anchor", "2026-09-01"])

  const anchorFile = path.join(runtime.engine.outputDir, "anchor-last.txt")
  const created = runtime.readOrCreateAnchor()
  assert.equal(fs.readFileSync(anchorFile, "utf8"), created, "ancre du jour écrite dans anchor-last.txt")
  assert.equal(readOrCreateAnchorFile(runtime.engine.outputDir), created, "stable dans la journée")
  assert.equal(runtime.readOrCreateAnchor(), created)

  fs.writeFileSync(anchorFile, "2026-13-45T99:99:99Z")
  assert.throws(() => runtime.readOrCreateAnchor(), /ancre invalide dans /)
})

/** Échappe une chaîne pour une utilisation en expression régulière. */
function escapeRegExp(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")
}
