import assert from "node:assert/strict"
import fs from "node:fs"
import os from "node:os"
import path from "node:path"
import test from "node:test"

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

test("preflight accepts a complete kit", () => {
  const kit = makeKit()
  const result = preflight(kit.root, { WEEKLY_PYTHON: kit.python } as never)
  assert.equal(result.rc, 0)
  assert.equal(result.engine_ok, true)
  assert.equal(result.python_ok, true)
  assert.equal(result.config_ok, true)
  assert.ok((result.py_count ?? 0) > 0)
})

test("preflight rejects missing engine", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "wa-broken-"))
  const result = preflight(root, {} as never)
  assert.equal(result.rc, 3)
  assert.equal(result.engine_ok, false)
  assert.match(result.message ?? "", /introuvable/)
})

test("preflight rejects unreadable config", () => {
  const kit = makeKit()
  fs.writeFileSync(path.join(kit.engine, "weekly-telemetry-config.json"), "{not json")
  const result = preflight(kit.root, { WEEKLY_PYTHON: kit.python } as never)
  assert.equal(result.rc, 3)
  assert.equal(result.config_ok, false)
})