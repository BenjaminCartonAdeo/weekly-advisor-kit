import assert from "node:assert/strict"
import fs from "node:fs"
import test from "node:test"
import path from "node:path"

import { canonicalPytestCommand, checkTestCounts, collectPytest, documentedTestCounts } from "../check-flow-docs.mjs"

const ROOT = path.resolve(import.meta.dirname, "../..")

test("collection probe invokes uv python module pytest with quiet collection", () => {
  let invocation
  const result = collectPytest({
    cwd: "/engine",
    runner: (...args) => {
      invocation = args
      return { status: 0, stdout: "12 tests collected\n", stderr: "" }
    },
  })

  assert.deepEqual(invocation.slice(0, 2), ["uv", ["run", "python", "-m", "pytest", "--collect-only", "-q"]])
  assert.equal(invocation[2].cwd, "/engine")
  assert.deepEqual(result, { kind: "ok", stdout: "12 tests collected\n", stderr: "", status: 0, collected: 12 })
})

test("collection probe distinguishes command execution failure and keeps diagnostics", () => {
  const result = collectPytest({
    runner: () => ({ status: 2, stdout: "partial", stderr: "collection crashed" }),
  })

  assert.equal(result.kind, "execution-failure")
  assert.equal(result.status, 2)
  assert.equal(result.stderr, "collection crashed")
})

test("collection probe distinguishes parser failure", () => {
  const result = collectPytest({ runner: () => ({ status: 0, stdout: "unexpected", stderr: "warning" }) })

  assert.deepEqual(result, { kind: "parser-failure", stdout: "unexpected", stderr: "warning", status: 0, collected: null })
})

test("documentation count mismatch is informational while collection remains healthy", () => {
  const docs = documentedTestCounts(["README: 10 tests", "INSTALL: 11 tests", "CI: 12 tests"])
  const result = checkTestCounts({ docs, collection: { kind: "ok", collected: 12 } })

  assert.equal(result.blocking, false)
  assert.match(result.messages[0], /documentation counts differ/)
})

test("execution and parser failures remain blocking", () => {
  for (const kind of ["execution-failure", "parser-failure"]) {
    assert.equal(checkTestCounts({ docs: [10], collection: { kind } }).blocking, true)
  }
})

test("worker protocol requires canonical bounded pytest guard", () => {
  const worker = fs.readFileSync(
    path.join(ROOT, ".opencode", "agents", "weekly-advisor", "weekly-advisor-worker.md"),
    "utf8",
  )
  assert.match(worker, /cwd moteur|\.opencode\/plugins\/weekly-advisor-engine/)
  assert.match(worker, /uv run python -m pytest -q/)
  assert.match(worker, /--collect-only -q/)
  assert.match(worker, /Maximum un test ciblé puis ce fallback/)
  assert.match(worker, /Ne jamais utiliser `uv run pytest`[\s\S]*préfixer la commande par `rtk`/)
})

test("canonical worker pytest command enforces engine cwd and selector", () => {
  const [binary, args] = canonicalPytestCommand({
    cwd: path.join(ROOT, ".opencode", "plugins", "weekly-advisor-engine"),
    selector: "tests/test_cli.py",
  })
  assert.equal(binary, "uv")
  assert.deepEqual(args, ["run", "python", "-m", "pytest", "-q", "tests/test_cli.py"])
  assert.throws(() => canonicalPytestCommand({ cwd: ROOT, selector: "tests/test_cli.py" }), /cwd/)
  assert.throws(() => canonicalPytestCommand({
    cwd: path.join(ROOT, ".opencode", "plugins", "weekly-advisor-engine"),
    selector: "../README.md",
  }), /inside engine/)
})

test("execution contract documents provenance, bounded delegation, transport and statuses", () => {
  const docs = ["README.md", "INSTALL.md", path.join("doc", "ARCHITECTURE.md")].map((file) =>
    fs.readFileSync(path.join(ROOT, file), "utf8"),
  )
  const source = docs.join("\n")
  for (const field of ["anchor", "generated_at", "source", "run_dir", "artifact_inputs", "start_time", "elapsed_s"]) {
    assert.match(source, new RegExp(`\\b${field}\\b`), `missing provenance field ${field}`)
  }
  assert.match(source, /jamais dans `argv`/)
  assert.match(source, /10 min/)
  assert.match(source, /K ≤ audit_max_sessions/)
  assert.match(source, /skills_loaded\.ok=false/)
  assert.match(source, /rc=2[\s\S]*sans rapport/)
  for (const status of ["missing", "truncated", "timeout", "worker_status"]) {
    assert.match(source, new RegExp(`\\b${status}\\b`), `missing status ${status}`)
  }
})
