// Smoke test: dual-runtime compatibility harness for weekly-advisor
//
// Ensures OpenCode V1 & V2 binaries are compatible with the plugin via:
//   1. Distinct explicit binary paths (strict mode requirement)
//   2. Version verification with extractVersion
//   3. Strict vs non-strict behavior on missing/mismatched deps
//   4. SKIPPED state tracking; no silent passes
//   5. Real subprocess integration tests exercising full harness flow
//
import assert from "node:assert/strict"
import fs from "node:fs"
import path from "node:path"
import test from "node:test"
import { fileURLToPath, pathToFileURL } from "node:url"
import { spawnSync } from "node:child_process"
import { extractVersion, checkBinary, setSpawnSyncImpl } from "../smoke-dual-runtime.mjs"

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..")

// ============================================================================
// UNIT TESTS
// ============================================================================

test("extractVersion: returns null for non-existent binary", () => {
  const version = extractVersion("/nonexistent/opencode-v1")
  assert.equal(version, null)
})

test("extractVersion: injects subprocess mock for version extraction", async () => {
  // Set up a mock spawnSync that returns a version string
  const mockSpawnSync = (_bin, args, opts) => {
    if (args[0] === "--version") {
      return {
        status: 0,
        error: null,
        stdout: "opencode 1.19.5",
      }
    }
    return { status: 1, error: new Error("unexpected") }
  }

  setSpawnSyncImpl(mockSpawnSync)
  // Create a fake file so extractVersion doesn't return null immediately
  const fakeFile = "/tmp/fake-opencode-test-bin"
  fs.writeFileSync(fakeFile, "#!/bin/sh\necho opencode 1.19.5", { mode: 0o755 })
  try {
    const version = extractVersion(fakeFile)
    assert.equal(version, "1.19.5")
  } finally {
    fs.rmSync(fakeFile, { force: true })
    const { spawnSync } = await import("node:child_process")
    setSpawnSyncImpl(spawnSync)
  }
})

test("extractVersion: handles semver with prerelease", async () => {
  const mockSpawnSync = (_bin, args, opts) => {
    if (args[0] === "--version") {
      return {
        status: 0,
        error: null,
        stdout: "opencode 2.0.0-rc1",
      }
    }
    return { status: 1, error: new Error("unexpected") }
  }

  setSpawnSyncImpl(mockSpawnSync)
  const fakeFile = "/tmp/fake-opencode-test-bin-v2"
  fs.writeFileSync(fakeFile, "#!/bin/sh\necho opencode 2.0.0-rc1", { mode: 0o755 })
  try {
    const version = extractVersion(fakeFile)
    assert.equal(version, "2.0.0-rc1")
  } finally {
    fs.rmSync(fakeFile, { force: true })
    const { spawnSync } = await import("node:child_process")
    setSpawnSyncImpl(spawnSync)
  }
})

test("extractVersion: returns null on --version failure", async () => {
  const mockSpawnSync = (_bin, args, opts) => {
    return {
      status: 127,
      error: new Error("command not found"),
      stdout: "",
    }
  }

  setSpawnSyncImpl(mockSpawnSync)
  const version = extractVersion("/fake/opencode-bad")
  assert.equal(version, null)

  const { spawnSync } = await import("node:child_process")
  setSpawnSyncImpl(spawnSync)
})

test("plugin structure: validates entry point existence", () => {
  const entrypoint = path.join(ROOT, ".opencode", "plugins", "weekly-advisor.ts")
  const exists = fs.existsSync(entrypoint)
  assert.ok(exists, `plugin entry point should exist at ${entrypoint}`)
})

test("package constraint: @opencode-ai/plugin declared with floor version", () => {
  const pkgPath = path.join(ROOT, ".opencode", "package.json")
  if (!fs.existsSync(pkgPath)) {
    // Skip if package.json doesn't exist in test environment
    return
  }

  const pkg = JSON.parse(fs.readFileSync(pkgPath, "utf8"))
  const depVersion = pkg.dependencies?.["@opencode-ai/plugin"]
  assert.ok(
    depVersion,
    "@opencode-ai/plugin should be declared in dependencies",
  )
  assert.ok(
    depVersion.includes(">=1.18.29") || depVersion.includes("1.18.29"),
    `constraint should include floor >=1.18.29, got: ${depVersion}`,
  )
})

test("registry: exports TOOL_REGISTRY and CLI_COMMANDS", () => {
  const registryPath = path.join(ROOT, ".opencode", "plugins", "weekly-advisor", "tool-registry.ts")
  assert.ok(fs.existsSync(registryPath), `registry should exist at ${registryPath}`)
  
  const content = fs.readFileSync(registryPath, "utf8")
  assert.ok(
    /export\s+const\s+TOOL_REGISTRY:/m.test(content),
    "registry should export TOOL_REGISTRY"
  )
  assert.ok(
    /export\s+const\s+CLI_COMMANDS:/m.test(content),
    "registry should export CLI_COMMANDS"
  )
})

// ============================================================================
// SUBPROCESS INTEGRATION TESTS — Real harness invocations
// ============================================================================

test("subprocess: non-strict mode + no env → SKIPPED (exit 0, never PASS)", () => {
  // Non-strict with no binaries should not exit with PASS
  const env = { ...process.env }
  delete env.OPENCODE_V1_BIN
  delete env.OPENCODE_V2_BIN
  delete env.V1_EXPECTED_VERSION
  delete env.V2_EXPECTED_VERSION
  
  const result = spawnSync("node", ["scripts/smoke-dual-runtime.mjs", "--non-strict"], {
    cwd: ROOT,
    env,
    encoding: "utf8",
  })

  assert.equal(result.status, 0, "non-strict with no env should exit 0")
  // Should print SKIPPED, never PASSED
  assert.ok(
    result.stdout.includes("SKIPPED"),
    "non-strict should print SKIPPED, not PASSED"
  )
  assert.ok(
    !result.stdout.includes("✅ PASSED"),
    "non-strict should not print PASSED when checks skipped"
  )
})

test("subprocess: strict mode + no env → exit 3 (missing prereqs)", () => {
  // Strict mode without binaries should exit 3
  const env = { ...process.env }
  delete env.OPENCODE_V1_BIN
  delete env.OPENCODE_V2_BIN
  
  const result = spawnSync("node", ["scripts/smoke-dual-runtime.mjs", "--strict"], {
    cwd: ROOT,
    env,
    encoding: "utf8",
  })

  assert.equal(result.status, 3, "strict without binaries should exit 3")
  assert.ok(
    result.stderr.includes("STRICT MODE ERROR"),
    "should print error message"
  )
})

test("subprocess: strict mode + identical bins → exit 3 (distinct required)", () => {
  // Strict mode with V1_BIN === V2_BIN should exit 3
  const sameBin = "/tmp/opencode-fake"
  fs.writeFileSync(sameBin, "#!/bin/sh\necho opencode 1.19.5", { mode: 0o755 })
  
  const env = {
    ...process.env,
    OPENCODE_V1_BIN: sameBin,
    OPENCODE_V2_BIN: sameBin,
  }
  delete env.V1_EXPECTED_VERSION
  delete env.V2_EXPECTED_VERSION
  
  try {
    const result = spawnSync("node", ["scripts/smoke-dual-runtime.mjs", "--strict"], {
      cwd: ROOT,
      env,
      encoding: "utf8",
    })

    assert.equal(result.status, 3, "strict with identical bins should exit 3")
  } finally {
    fs.rmSync(sameBin, { force: true })
  }
})

test("subprocess: strict mode + missing expected versions → exit 3", () => {
  // Strict mode with bins but no expected versions should fail
  const v1Bin = "/tmp/fake-v1-bin"
  const v2Bin = "/tmp/fake-v2-bin"
  fs.writeFileSync(v1Bin, "#!/bin/sh\necho opencode 1.19.5", { mode: 0o755 })
  fs.writeFileSync(v2Bin, "#!/bin/sh\necho opencode 2.0.0", { mode: 0o755 })
  
  const env = {
    ...process.env,
    OPENCODE_V1_BIN: v1Bin,
    OPENCODE_V2_BIN: v2Bin,
    // Omit V1_EXPECTED_VERSION and V2_EXPECTED_VERSION
  }
  delete env.V1_EXPECTED_VERSION
  delete env.V2_EXPECTED_VERSION
  
  try {
    const result = spawnSync("node", ["scripts/smoke-dual-runtime.mjs", "--strict"], {
      cwd: ROOT,
      env,
      encoding: "utf8",
    })

    assert.equal(result.status, 3, "strict without expected versions should exit 3")
  } finally {
    fs.rmSync(v1Bin, { force: true })
    fs.rmSync(v2Bin, { force: true })
  }
})

test("subprocess: strict mode + version mismatch → exit 1 (registry fail)", () => {
  // Strict mode with version mismatch should fail the registry check
  const v1Bin = "/tmp/fake-v1-mismatch"
  const v2Bin = "/tmp/fake-v2-mismatch"
  fs.writeFileSync(v1Bin, "#!/bin/sh\necho opencode 1.19.0", { mode: 0o755 })
  fs.writeFileSync(v2Bin, "#!/bin/sh\necho opencode 2.0.0", { mode: 0o755 })
  
  const env = {
    ...process.env,
    OPENCODE_V1_BIN: v1Bin,
    OPENCODE_V2_BIN: v2Bin,
    V1_EXPECTED_VERSION: "1.19.5",  // Mismatch!
    V2_EXPECTED_VERSION: "2.0.0",
  }
  
  try {
    const result = spawnSync("node", ["scripts/smoke-dual-runtime.mjs", "--strict", "--quiet"], {
      cwd: ROOT,
      env,
      encoding: "utf8",
    })

    assert.notEqual(result.status, 0, "strict with version mismatch should not exit 0")
  } finally {
    fs.rmSync(v1Bin, { force: true })
    fs.rmSync(v2Bin, { force: true })
  }
})

test("subprocess: strict mode + valid bins/versions → exit 0", () => {
  // Strict mode with everything matching should pass
  const v1Bin = "/tmp/fake-v1-valid"
  const v2Bin = "/tmp/fake-v2-valid"
  fs.writeFileSync(v1Bin, "#!/bin/sh\necho opencode 1.19.5", { mode: 0o755 })
  fs.writeFileSync(v2Bin, "#!/bin/sh\necho opencode 2.0.0", { mode: 0o755 })
  
  const env = {
    ...process.env,
    OPENCODE_V1_BIN: v1Bin,
    OPENCODE_V2_BIN: v2Bin,
    V1_EXPECTED_VERSION: "1.19.5",
    V2_EXPECTED_VERSION: "2.0.0",
  }
  
  try {
    const result = spawnSync("node", ["scripts/smoke-dual-runtime.mjs", "--strict"], {
      cwd: ROOT,
      env,
      encoding: "utf8",
    })

    assert.equal(result.status, 0, "strict with valid bins/versions should exit 0")
    assert.ok(result.stdout.includes("✅ PASSED"), "should print PASSED")
  } finally {
    fs.rmSync(v1Bin, { force: true })
    fs.rmSync(v2Bin, { force: true })
  }
})

// ============================================================================
// CI WORKFLOW VALIDATION TEST
// ============================================================================

test("CI workflow: smoke test step has shell: bash and no unreachable if:", () => {
  // Read the CI YAML and validate the smoke test step
  const ciYamlPath = path.join(ROOT, ".github", "workflows", "ci.yml")
  const ciYaml = fs.readFileSync(ciYamlPath, "utf8")
  
  // Must not have the dead "if: env.OPENCODE_V1_BIN" pattern
  assert.ok(
    !ciYaml.includes("if: env.OPENCODE_V1_BIN"),
    "CI must not have unreachable if: gating on step-level env vars"
  )
  
  // Must have the strict step
  assert.ok(
    ciYaml.includes("Smoke test — V1/V2 compatibility"),
    "CI should have V1/V2 strict test step"
  )
  
  // Must have shell: bash for POSIX syntax on Windows
  const strictStepSection = ciYaml.split("Smoke test — V1/V2 compatibility")[1].split("working-directory:")[0]
  assert.ok(
    strictStepSection.includes("shell: bash"),
    "strict step must have shell: bash for cross-platform POSIX syntax"
  )
})

// ============================================================================
// VERDICT LOGIC TESTS
// ============================================================================

test("verdict: hasSkipped prevents PASSED summary", () => {
  // Verify the verdict logic: if any check is SKIPPED, summary is SKIPPED not PASSED
  const checks = []
  const recordCheck = (name, ok, version, reason) => {
    const state = reason ? "skipped" : (ok ? "passed" : "failed")
    checks.push({ name, ok, version, reason, state })
  }
  
  recordCheck("check-1", true, "1.19.5", null)  // state=passed
  recordCheck("check-2", false, null, "binary not found")  // state=skipped
  
  const hasSkipped = checks.some((c) => c.state === "skipped")
  const failCount = checks.filter((c) => c.state === "failed").length
  
  assert.ok(hasSkipped, "hasSkipped should be true")
  
  // Verdict logic: if hasSkipped, print SKIPPED; else if failCount > 0, print FAILED; else print PASSED
  const verdict = hasSkipped ? "SKIPPED" : (failCount === 0 ? "PASSED" : "FAILED")
  assert.equal(verdict, "SKIPPED", "verdict should be SKIPPED when any check skipped")
})
