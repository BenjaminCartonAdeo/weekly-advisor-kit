import assert from "node:assert/strict"
import fs from "node:fs"
import test from "node:test"
import path from "node:path"

import { canonicalPytestCommand, checkTestCounts, collectPytest, documentedTestCounts, tsCommands, registryCLICommands } from "../check-flow-docs.mjs"

const ROOT = path.resolve(import.meta.dirname, "../..")
const REGISTRY_PATH = path.join(ROOT, ".opencode", "plugins", "weekly-advisor", "tool-registry.ts")

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

test("documentation count mismatch between docs now BLOCKS the gate", () => {
  // Régression de l'état warn-only : ce contrôle était structurellement incapable
  // de faire échouer le gate, et son test encodait cette impuissance comme un
  // comportement voulu (« mismatch is informational »).
  const docs = documentedTestCounts(["README: 10 tests", "INSTALL: 11 tests", "CI: 12 tests"])
  const result = checkTestCounts({ docs, collection: { kind: "ok", collected: 12 } })

  assert.equal(result.blocking, true)
  assert.equal(result.countsAgree, false)
  assert.match(result.messages[0], /documentation counts differ/)
})

test("zero documented literal is the TARGET state and stays green", () => {
  // Le piège d'implémentation : après le retrait des littéraux, `docs` est vide.
  // Un `unique.length !== 1` naïf échouerait ici et casserait la CI sur le dépôt
  // propre. « Rien à maintenir » ne peut pas être lu comme « désaccord ».
  const result = checkTestCounts({ docs: [], collection: { kind: "ok", collected: 2081 } })

  assert.equal(result.blocking, false)
  assert.equal(result.hasLiterals, false)
  assert.equal(result.countsAgree, true)
  assert.equal(result.collectAgrees, true)
})

test("literals all equal to the real collection are green (the tolerated state)", () => {
  const docs = documentedTestCounts(["README: 2081 tests", "INSTALL: 2081 tests"])
  const result = checkTestCounts({ docs, collection: { kind: "ok", collected: 2081 } })

  assert.equal(result.blocking, false)
  assert.equal(result.hasLiterals, true)
  assert.deepEqual(result.messages, [])
})

test("literals self-consistent but contradicting the real collection now BLOCK", () => {
  // C'est la dérive que le warn-only ne pouvait structurellement pas voir :
  // README, INSTALL et ci.yml qui portent TOUS le même mauvais nombre.
  const docs = documentedTestCounts(["README: 9999 tests", "INSTALL: 9999 tests"])
  const result = checkTestCounts({ docs, collection: { kind: "ok", collected: 2081 } })

  assert.equal(result.countsAgree, true, "les docs sont cohérentes entre elles")
  assert.equal(result.collectAgrees, false)
  assert.equal(result.blocking, true)
})

test("collect agreement is not claimed when the docs disagree with each other", () => {
  // Sans cette précondition, la ligne « le compte documenté égale la collecte »
  // afficherait un vert faux : elle validerait le PREMIER littéral contre le réel
  // au hasard de l'ordre de lecture des fichiers.
  const docs = documentedTestCounts(["README: 9999 tests", "INSTALL: 2081 tests"])
  const result = checkTestCounts({ docs, collection: { kind: "ok", collected: 2081 } })

  assert.equal(result.collectAgrees, false)
  assert.equal(result.blocking, true)
})

test("failed collection is reported once, not double-counted as a count mismatch", () => {
  // Le compte réel est alors INCONNU, pas divergent. Le keeping de
  // `collectAgrees` à true évite un doublon sans diagnostic nouveau ; la panne
  // de collecte reste signalée par `blocking` et par son propre message.
  const result = checkTestCounts({ docs: [2081], collection: { kind: "execution-failure" } })

  assert.equal(result.blocking, true)
  assert.equal(result.collectAgrees, true)
  assert.deepEqual(result.messages, ["pytest collection execution failed"])
})

test("count extraction matches the NATURE of the defect, not a fixed phrasing", () => {
  const cases = [
    ["README: 2081 tests", [2081]], //           entier avant l'unité (forme d'origine)
    ["README: 1 test", [1]], //                    singulier
    ["the 1219-test suite", [1219]], //            unité accolée par un tiret — trou historique
    ["badge/tests-2081-brightgreen", [2081]], //   badge, entier APRÈS l'unité — trou historique
    ["badge/tests_2081-green", [2081]], //         idem, séparateur underscore
  ]
  for (const [source, expected] of cases) {
    assert.deepEqual(documentedTestCounts([source]), expected, `for ${JSON.stringify(source)}`)
  }
})

test("count extraction refuses the fragments that are not test counts", () => {
  // Un motif élargi sur la NATURE doit rester précis : sans garde, « 3.13 »
  // d'une version deviendrait un compte de suite et le gate comparerait une
  // version à la collecte.
  for (const source of ["python 3.13 tests", "v2.4 tests", "testsuite", "testing 900"]) {
    assert.deepEqual(documentedTestCounts([source]), [], `for ${JSON.stringify(source)}`)
  }
})

test("execution and parser failures remain blocking", () => {
  for (const kind of ["execution-failure", "parser-failure"]) {
    assert.equal(checkTestCounts({ docs: [10], collection: { kind } }).blocking, true)
  }
})

test("engine README carries the canonical bounded pytest guard for workers", () => {
  // Single-source post-dedup : la garde pytest vit dans le README du moteur, pas
  // dupliquée dans le doc worker.
  const readme = fs.readFileSync(
    path.join(ROOT, ".opencode", "plugins", "weekly-advisor-engine", "README.md"),
    "utf8",
  )
  assert.match(readme, /depuis ce dossier, `uv run python -m pytest -q`/)
  assert.match(readme, /--collect-only -q/)
  assert.match(readme, /fallback diagnostique\s+unique et borné/)
  assert.match(readme, /jamais `uv run pytest` ni[\s\S]*`uv run rtk pytest`/)
})

test("tsCommands extracts command literals from multi-line argv arrays and declarative tables", () => {
  const cmds = tsCommands(
    'tool: { argv: [\n  "harness-remediate",\n  "--anchor",\n] }, entry: { subcommand: "self-cost" },\n// ["default"]',
  )
  assert.deepEqual([...cmds].sort(), ["harness-remediate", "self-cost"])
})

test("registryCLICommands discovers every CLI-mediating tool in the neutral registry (19 commands)", () => {
  const ts = fs.readFileSync(REGISTRY_PATH, "utf8")
  const cmds = [...registryCLICommands(ts)].sort()
  assert.equal(cmds.length, 19, `expected 19 CLI commands from registry, got ${cmds.length}`)
  assert.ok(cmds.includes("harness-remediate"))
  assert.ok(cmds.includes("run"))
  assert.ok(cmds.includes("skill-curate"))
  assert.ok(cmds.includes("report-blocks-check"), "le check de prose doit être exposé comme tool")
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
  const docs = ["README.md", "INSTALL.md", path.join("doc", "architecture", "README.md")].map((file) =>
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
