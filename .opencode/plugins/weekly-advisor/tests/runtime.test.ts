import assert from "node:assert/strict"
import fs from "node:fs"
import os from "node:os"
import path from "node:path"
import test from "node:test"

import { WeeklyRuntime, readOrCreateAnchorFile } from "../runtime.ts"

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

test("WeeklyRuntime root alias and preflight", () => {
  const kit = makeKit()
  const runtime = new WeeklyRuntime({ location: { v1Worktree: kit.root }, env: { WEEKLY_PYTHON: kit.python } })
  assert.equal(runtime.worktree, runtime.root)
  assert.equal(runtime.root, kit.root)
  assert.equal(runtime.preflight().rc, 0)
  assert.doesNotThrow(() => runtime.preflightOrThrow())
})

test("WeeklyRuntime throws on broken kit", () => {
  const broken = fs.mkdtempSync(path.join(os.tmpdir(), "wa-broken-"))
  const runtime = new WeeklyRuntime({ location: { v1Worktree: broken }, env: {} })
  assert.throws(() => runtime.preflightOrThrow(), /weekly_preflight rc=3/)
})

test("anchor explicit and stable", () => {
  const kit = makeKit()
  const runtime = new WeeklyRuntime({ location: { v1Worktree: kit.root }, env: { WEEKLY_PYTHON: kit.python } })
  assert.equal(runtime.anchorArg("2026-09-01"), "2026-09-01")
  assert.deepEqual(runtime.anchorArgs("2026-09-01"), ["--anchor", "2026-09-01"])
  const created = runtime.readOrCreateAnchor()
  const anchorFile = path.join(runtime.engine.outputDir, "anchor-last.txt")
  assert.equal(fs.readFileSync(anchorFile, "utf8"), created)
  assert.equal(readOrCreateAnchorFile(runtime.engine.outputDir), created)
})

test("two runtimes keep distinct roots", () => {
  const a = makeKit()
  const b = makeKit()
  const ra = new WeeklyRuntime({ location: { v1Worktree: a.root } })
  const rb = new WeeklyRuntime({ location: { v1Worktree: b.root } })
  assert.notEqual(ra.root, rb.root)
})