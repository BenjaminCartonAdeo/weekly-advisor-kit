import assert from "node:assert/strict"
import fs from "node:fs"
import os from "node:os"
import path from "node:path"
import test from "node:test"

import { engineDirectory, expandHome, hasEngineDirectory, resolveEngine, resolveKitRoot } from "../paths.ts"
import { preflight } from "../preflight.ts"

function makeKit(python?: string): { root: string; engine: string; python: string } {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "wa-fakekit-"))
  const engine = path.join(root, ".opencode", "plugins", "weekly-advisor-engine")
  fs.mkdirSync(path.join(engine, ".venv", "bin"), { recursive: true })
  fs.mkdirSync(path.join(engine, "weekly_telemetry_aggregator"), { recursive: true })
  fs.writeFileSync(path.join(engine, "weekly_telemetry_aggregator", "main.py"), "")
  fs.writeFileSync(path.join(engine, "weekly-telemetry-config.json"), "{}")
  const py = path.join(engine, ".venv", "bin", "python")
  fs.writeFileSync(py, python ?? "")
  return { root, engine, python: py }
}

test("expandHome matches Python expanduser semantics", () => {
  const home = os.homedir()
  assert.equal(expandHome("~"), home)
  assert.equal(expandHome("~/reports"), path.join(home, "reports"))
  assert.equal(expandHome("~\\reports"), path.join(home, "reports"))
  assert.equal(expandHome("/absolute/reports"), "/absolute/reports")
  assert.equal(expandHome("relative/reports"), "relative/reports")
})

test("resolveKitRoot prefers WEEKLY_KIT_ROOT", () => {
  const kit = makeKit()
  const resolved = resolveKitRoot([{ label: "v1-worktree", value: "/nope" }], {
    WEEKLY_KIT_ROOT: `  ${kit.root}  `,
  })
  assert.equal(resolved, path.resolve(kit.root))
})

test("resolveEngine reports missing engine before interpreter", () => {
  const bare = fs.mkdtempSync(path.join(os.tmpdir(), "wa-bare-"))
  assert.throws(() => resolveEngine(bare, { WEEKLY_PYTHON: "" }), /moteur introuvable/)
  const kit = makeKit()
  fs.rmSync(kit.python)
  assert.throws(() => resolveEngine(kit.root, { WEEKLY_PYTHON: "" }), /interpréteur Python introuvable/)
})

test("resolveEngine expands relative output_dir against engine", () => {
  const kit = makeKit()
  const relative = resolveEngine(kit.root, { WEEKLY_PYTHON: kit.python })
  assert.equal(relative.outputDir, path.join(kit.engine, "reports"))
})

test("hasEngineDirectory detects engine", () => {
  const kit = makeKit()
  assert.equal(hasEngineDirectory(kit.root), true)
  assert.equal(engineDirectory(kit.root), path.join(kit.root, ".opencode", "plugins", "weekly-advisor-engine"))
})

test("preflight accepts a complete kit", () => {
  const kit = makeKit()
  const result = preflight(kit.root, { WEEKLY_PYTHON: kit.python })
  assert.equal(result.rc, 0)
  assert.equal(result.engine_ok, true)
  assert.equal(result.python_ok, true)
  assert.equal(result.config_ok, true)
  assert.equal(result.py_count, 1)
})