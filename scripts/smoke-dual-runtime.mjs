#!/usr/bin/env node
// Smoke harness: dual-runtime compatibility for weekly-advisor
//
// Usage:
//   node scripts/smoke-dual-runtime.mjs [--strict|--non-strict] [--quiet]
//
// Environment:
//   OPENCODE_V1_BIN: path to V1 binary (optional, detected from PATH if omitted)
//   OPENCODE_V2_BIN: path to V2 binary (optional, detected from PATH if omitted)
//   V1_EXPECTED_VERSION: expected V1 semver (optional, skipped if omitted)
//   V2_EXPECTED_VERSION: expected V2 semver (optional, skipped if omitted)
//   WEEKLY_KIT_ROOT: kit root directory (defaults to cwd or parent of .opencode)
//
// Behavior:
//   --strict: fail if any binary/plugin/loader check fails (default)
//   --non-strict: print SKIPPED when checks fail or binaries are missing
//   --quiet: suppress detailed output (exit code and summary only)
//
// Exit codes:
//   0: all checks passed
//   1: checks failed in strict mode, or non-strict with critical errors
//   3: configuration or environment error
//
import fs from "node:fs"
import path from "node:path"
import { execSync, spawnSync } from "node:child_process"
import { fileURLToPath, pathToFileURL } from "node:url"

// For testing: injectable subprocess execution
let spawnSyncImpl = spawnSync

export function setSpawnSyncImpl(impl) {
  spawnSyncImpl = impl
}

// Exported state helpers for testing
export function getCheckState() {
  return { checks, hasSkipped, exitCode, isStrict, isQuiet }
}

// Constants
const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..")
const REPORT_FILE = path.join(ROOT, "reports", "smoke-dual-runtime-report.json")

// Parse CLI arguments
const args = process.argv.slice(2)
const isStrict = args.includes("--strict") || (!args.includes("--non-strict") && !args.includes("--no-strict"))
const isQuiet = args.includes("--quiet")

// Configuration from environment
// BLOCKER FIX #4: In strict mode, V1 and V2 must be DISTINCT and explicitly required.
const config = {
  v1Bin: process.env.OPENCODE_V1_BIN || null,
  v2Bin: process.env.OPENCODE_V2_BIN || null,
  v1Expected: process.env.V1_EXPECTED_VERSION || null,
  v2Expected: process.env.V2_EXPECTED_VERSION || null,
  kitRoot: process.env.WEEKLY_KIT_ROOT || discoverKitRoot(),
}

// State
let exitCode = 0
const checks = []
let hasSkipped = false // BLOCKER FIX #2: track SKIPPED checks globally

// Utilities

function log(msg) {
  if (!isQuiet) console.log(msg)
}

function logError(msg) {
  if (!isQuiet) console.error(msg)
}

function recordCheck(name, ok, version, reason) {
  // State is "skipped" when there's a reason but non-strict allows it (ok=true but reason exists)
  const state = reason ? "skipped" : (ok ? "passed" : "failed")
  if (state === "skipped") hasSkipped = true
  checks.push({ name, ok, version, reason, state, timestamp: new Date().toISOString() })
}

function resolveFromPath(binName) {
  try {
    // Try which/where depending on platform
    const cmd = process.platform === "win32" ? `where ${binName}` : `which ${binName}`
    const result = execSync(cmd, { encoding: "utf8", stdio: ["pipe", "pipe", "ignore"] }).trim()
    return result ? result.split("\n")[0] : null
  } catch {
    return null
  }
}

function discoverKitRoot() {
  // Check WEEKLY_KIT_ROOT
  if (process.env.WEEKLY_KIT_ROOT) return process.env.WEEKLY_KIT_ROOT

  // Walk up from cwd looking for .opencode/plugins/weekly-advisor.ts
  let current = process.cwd()
  const root = path.parse(current).root
  for (let depth = 0; depth < 10; depth++) {
    const candidate = path.join(current, ".opencode", "plugins", "weekly-advisor.ts")
    if (fs.existsSync(candidate)) return current
    if (current === root) break
    current = path.dirname(current)
  }

  return process.cwd()
}

/**
 * Extract version from binary --version output.
 * Handles formats like "opencode 1.19.0" or "1.19.0" or "v1.19.0".
 * Exported for testing.
 */
export function extractVersion(binPath) {
  if (!binPath || !fs.existsSync(binPath)) return null
  try {
    const result = spawnSyncImpl(binPath, ["--version"], {
      encoding: "utf8",
      timeout: 5000,
      stdio: ["ignore", "pipe", "ignore"],
    })
    if (result.error || result.status !== 0) return null
    const output = result.stdout.trim()
    const match = output.match(/v?(\d+\.\d+\.\d+(?:[-.\w]+)?)/)
    return match ? match[1] : null
  } catch {
    return null
  }
}

/**
 * Verify a single binary.
 * Exported for testing.
 */
export function checkBinary(name, binPath, expectedVersion, runtime) {
  const label = `${runtime} ${name}`
  log(`\n[${label}]`)

  if (!binPath) {
    const reason = `${name} binary not specified (env: OPENCODE_${runtime}_BIN)`
    log(`  skip: ${reason}`)
    recordCheck(label, !isStrict, null, reason)
    if (isStrict) {
      logError(`  FAIL (strict mode requires binary)`)
      exitCode = 1
    } else {
      log(`  result: SKIPPED`)
    }
    return !isStrict
  }

  if (!fs.existsSync(binPath)) {
    const reason = `binary not found: ${binPath}`
    log(`  error: ${reason}`)
    recordCheck(label, !isStrict, null, reason)
    if (isStrict) {
      logError(`  FAIL (strict mode requires existing binary)`)
      exitCode = 1
    } else {
      log(`  result: SKIPPED`)
    }
    return !isStrict
  }

  const version = extractVersion(binPath)
  if (!version) {
    const reason = `${name} --version failed or returned unparseable output`
    log(`  error: ${reason}`)
    recordCheck(label, !isStrict, null, reason)
    if (isStrict) {
      logError(`  FAIL (strict mode requires valid version)`)
      exitCode = 1
    } else {
      log(`  result: SKIPPED`)
    }
    return !isStrict
  }

  log(`  version: ${version}`)
  recordCheck(label, true, version, null)

  if (expectedVersion && version !== expectedVersion) {
    const reason = `version mismatch: expected ${expectedVersion}, got ${version}`
    log(`  warning: ${reason}`)
    recordCheck(label, !isStrict, version, reason)
    if (isStrict) {
      logError(`  FAIL (strict mode requires matching version)`)
      exitCode = 1
      return false
    } else {
      log(`  result: SKIPPED`)
      return false
    }
  }

  log(`  ✓ OK`)
  return true
}

/**
 * Verify plugin entry point and package constraint.
 */
function checkPluginStructure() {
  log("\n[Plugin Structure]")

  const entrypoint = path.join(config.kitRoot, ".opencode", "plugins", "weekly-advisor.ts")
  if (!fs.existsSync(entrypoint)) {
    const reason = `plugin entry point not found: ${entrypoint}`
    log(`  error: ${reason}`)
    recordCheck("Plugin entry point", false, null, reason)
    if (isStrict) {
      logError(`  FAIL`)
      exitCode = 1
    } else {
      log(`  result: SKIPPED`)
    }
    return false
  }

  log(`  entry: ${entrypoint}`)
  recordCheck("Plugin entry point", true, null, null)

  // Check package.json constraint
  const pkgPath = path.join(config.kitRoot, ".opencode", "package.json")
  if (!fs.existsSync(pkgPath)) {
    const reason = `package.json not found: ${pkgPath}`
    log(`  error: ${reason}`)
    recordCheck("Package constraint", false, null, reason)
    if (isStrict) {
      logError(`  FAIL`)
      exitCode = 1
    } else {
      log(`  result: SKIPPED`)
    }
    return false
  }

  try {
    const pkg = JSON.parse(fs.readFileSync(pkgPath, "utf8"))
    const depVersion = pkg.dependencies?.["@opencode-ai/plugin"]
    if (!depVersion) {
      const reason = "@opencode-ai/plugin not declared in dependencies"
      log(`  error: ${reason}`)
      recordCheck("Package constraint", false, null, reason)
      if (isStrict) {
        logError(`  FAIL`)
        exitCode = 1
      } else {
        log(`  result: SKIPPED`)
      }
      return false
    }

    log(`  @opencode-ai/plugin: ${depVersion}`)

    // Verify floor: >=1.18.29 <2.0.0
    if (!depVersion.includes(">=1.18.29") && !depVersion.includes("1.18.29")) {
      const reason = `constraint does not include floor >=1.18.29: ${depVersion}`
      log(`  warning: ${reason}`)
      if (isStrict) {
        logError(`  FAIL`)
        exitCode = 1
        return false
      } else {
        log(`  result: SKIPPED`)
        return false
      }
    }

    log(`  ✓ constraint OK`)
    recordCheck("Package constraint", true, depVersion, null)
    return true
  } catch (err) {
    const reason = `package.json parse error: ${err.message}`
    log(`  error: ${reason}`)
    recordCheck("Package constraint", false, null, reason)
    if (isStrict) {
      logError(`  FAIL`)
      exitCode = 1
    } else {
      log(`  result: SKIPPED`)
    }
    return false
  }
}

/**
 * Verify registry: load TOOL_REGISTRY and CLI_COMMANDS via Node 24 type-stripping subprocess.
 * BLOCKER FIX #1: Use documented Node 24 loader to assert TOOL_REGISTRY.length===19 and CLI_COMMANDS.length===18.
 * In strict mode, registry/loader failure or parse failure must FAIL (exit 1).
 * In non-strict, loader failure falls back to regex; record as SKIPPED with reason.
 */
function checkRegistry() {
  log("\n[Registry]")

  const registryPath = path.join(
    config.kitRoot,
    ".opencode",
    "plugins",
    "weekly-advisor",
    "tool-registry.ts",
  )
  if (!fs.existsSync(registryPath)) {
    const reason = `registry not found: ${registryPath}`
    log(`  error: ${reason}`)
    recordCheck("Registry", false, null, reason)
    if (isStrict) {
      logError(`  FAIL`)
      exitCode = 1
    } else {
      log(`  result: SKIPPED`)
    }
    return false
  }

  log(`  registry: ${registryPath}`)

  // Attempt Node 24 type-stripping loader for TOOL_REGISTRY and CLI_COMMANDS counts
  try {
    // Use pathToFileURL + JSON.stringify for safe URL encoding (handles quotes, #, ?, %, UNC)
    const registryUrl = pathToFileURL(registryPath).href
    
    const loaderScript = `
import { TOOL_REGISTRY, CLI_COMMANDS } from ${JSON.stringify(registryUrl)};
console.log(JSON.stringify({ toolCount: TOOL_REGISTRY.length, cliCount: CLI_COMMANDS.length }));
`
    const result = spawnSyncImpl("node", ["--input-type=module", "--eval", loaderScript], {
      encoding: "utf8",
      timeout: 5000,
      stdio: ["pipe", "pipe", "pipe"],
    })

    // Loader failure or non-zero status → in strict mode, FAIL immediately
    if (result.error || result.status !== 0) {
      const reason = `Node loader failed: ${result.error?.message || `exit ${result.status}`}`
      log(`  error: ${reason}`)
      recordCheck("Registry", false, null, reason)
      if (isStrict) {
        logError(`  FAIL`)
        exitCode = 1
      } else {
        // Non-strict: record as SKIPPED, fall through to regex fallback
        log(`  result: SKIPPED (falling back to file-based check)`)
      }
      // Continue to file-based check in non-strict mode
    } else {
      // Loader succeeded; parse output
      try {
        const output = JSON.parse(result.stdout)
        const toolCount = output.toolCount || 0
        const cliCount = output.cliCount || 0

        log(`  tools (loader): ${toolCount}`)
        log(`  CLI commands (loader): ${cliCount}`)

        if (toolCount !== 19 || cliCount !== 18) {
          const reason = `tool/command count mismatch: expected (19 tools, 18 CLI), got (${toolCount}, ${cliCount})`
          log(`  error: ${reason}`)
          recordCheck("Registry", false, null, reason)
          if (isStrict) {
            logError(`  FAIL`)
            exitCode = 1
          } else {
            log(`  result: SKIPPED`)
          }
          return false
        }

        log(`  ✓ OK (19 tools, 18 CLI commands via loader)`)
        recordCheck("Registry", true, null, null)
        return true
      } catch (parseErr) {
        // JSON parse failed
        const reason = `loader output parse failed: ${parseErr.message}`
        log(`  error: ${reason}`)
        recordCheck("Registry", false, null, reason)
        if (isStrict) {
          logError(`  FAIL`)
          exitCode = 1
          return false
        } else {
          // Non-strict: fall back to file-based check
          log(`  result: SKIPPED (falling back to file-based check)`)
        }
      }
    }

    // File-based fallback: verify TOOL_REGISTRY and CLI_COMMANDS are defined and exported.
    // Only used in non-strict mode after loader failure; in strict mode, already returned.
    if (!isStrict) {
      const content = fs.readFileSync(registryPath, "utf8")
      const hasTOOL_REGISTRY = /export\s+const\s+TOOL_REGISTRY:/m.test(content)
      const hasCLI_COMMANDS = /export\s+const\s+CLI_COMMANDS:/m.test(content)

      if (!hasTOOL_REGISTRY) {
        const reason = `TOOL_REGISTRY not exported from registry file`
        log(`  error: ${reason}`)
        recordCheck("Registry TOOL_REGISTRY", false, null, reason)
        log(`  result: SKIPPED`)
        return false
      }

      if (!hasCLI_COMMANDS) {
        const reason = `CLI_COMMANDS not exported from registry file`
        log(`  error: ${reason}`)
        recordCheck("Registry CLI_COMMANDS", false, null, reason)
        log(`  result: SKIPPED`)
        return false
      }

      log(`  ✓ TOOL_REGISTRY exported (file-based fallback)`)
      log(`  ✓ CLI_COMMANDS exported (file-based fallback)`)
      recordCheck("Registry", true, null, null)
      return true
    }

    return false
  } catch (err) {
    const reason = `registry check error: ${err.message}`
    log(`  error: ${reason}`)
    recordCheck("Registry", false, null, reason)
    if (isStrict) {
      logError(`  FAIL`)
      exitCode = 1
    } else {
      log(`  result: SKIPPED`)
    }
    return false
  }
}

// Main execution

function main() {
  // BLOCKER FIX #4: Strict mode requires distinct, explicit V1 and V2 binary paths
  if (isStrict && (!config.v1Bin || !config.v2Bin || config.v1Bin === config.v2Bin)) {
    logError("\n❌ STRICT MODE ERROR: Both OPENCODE_V1_BIN and OPENCODE_V2_BIN must be set and distinct")
    logError("   Set via env vars: export OPENCODE_V1_BIN=/path/to/v1; export OPENCODE_V2_BIN=/path/to/v2")
    process.exit(3)
  }

  // BLOCKER FIX #4: Strict mode also requires BOTH expected versions to be set
  if (isStrict && (!config.v1Expected || !config.v2Expected)) {
    logError("\n❌ STRICT MODE ERROR: Both V1_EXPECTED_VERSION and V2_EXPECTED_VERSION must be set")
    logError("   Set via env vars: export V1_EXPECTED_VERSION=1.19.5; export V2_EXPECTED_VERSION=2.0.0")
    process.exit(3)
  }

  log("=".repeat(70))
  log("weekly-advisor: dual-runtime smoke harness")
  log("=".repeat(70))
  log(`\nmode: ${isStrict ? "STRICT (fail on any error)" : "NON-STRICT (skip missing deps)"}`)
  log(`kit root: ${config.kitRoot}`)
  log(`timestamp: ${new Date().toISOString()}`)

  // Check V1
  const v1Ok = checkBinary("opencode", config.v1Bin, config.v1Expected, "V1")

  // Check V2
  const v2Ok = checkBinary("opencode", config.v2Bin, config.v2Expected, "V2")

  // Check plugin structure
  const pluginOk = checkPluginStructure()

  // Check registry
  const registryOk = checkRegistry()

  // Summary
  log("\n" + "=".repeat(70))
  log("SUMMARY")
  log("=".repeat(70))

  const allPassed = v1Ok && v2Ok && pluginOk && registryOk
  const passCount = checks.filter((c) => c.state === "passed").length
  const failCount = checks.filter((c) => c.state === "failed").length
  const skipCount = checks.filter((c) => c.state === "skipped").length

  log(`checks: ${passCount} passed, ${failCount} failed, ${skipCount} skipped`)
  log(`exit code: ${exitCode}`)

  // Write report
  try {
    const reportDir = path.dirname(REPORT_FILE)
    if (!fs.existsSync(reportDir)) fs.mkdirSync(reportDir, { recursive: true })

    fs.writeFileSync(
      REPORT_FILE,
      JSON.stringify(
        {
          mode: isStrict ? "strict" : "non-strict",
          kitRoot: config.kitRoot,
          checks,
          summary: {
            passed: passCount,
            failed: failCount,
            exitCode,
            timestamp: new Date().toISOString(),
          },
        },
        null,
        2,
      ),
    )

    log(`report written: ${REPORT_FILE}`)
  } catch (err) {
    logError(`warning: failed to write report: ${err.message}`)
  }

  log("=".repeat(70))

  if (isStrict && exitCode !== 0) {
    logError("\n❌ FAILED (strict mode)")
  } else if (hasSkipped) {
    log("\n⚠️  SKIPPED (checks failed or binaries missing)")
  } else if (exitCode === 0) {
    log("\n✅ PASSED")
  } else {
    logError("\n❌ FAILED")
  }

  process.exit(exitCode)
}

// Only invoke main() if run as a script, not when imported for testing
const scriptUrl = pathToFileURL(process.argv[1] ?? "").href
if (import.meta.url === scriptUrl) {
  main()
}
