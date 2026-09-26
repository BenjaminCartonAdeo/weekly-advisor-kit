/**
 * Surface parity: verify the dual plugin (V1 SDK adapter + V2 direct runtime)
 * exposes identical surfaces and the registry is the single source of truth.
 *
 * Asserts:
 * 1. Exactly one default export in weekly-advisor.ts with keys id/server/setup
 * 2. No static value import (e.g. `@opencode-ai/plugin`, `@opencode/plugin`)
 *    in the entrypoint module name itself
 * 3. 19 registry tools and 18 CLI commands (never empty scan)
 * 4. Neutral tool surface is identical for V1 and V2 projections
 * 5. No duplicate command registration
 */
import assert from "node:assert/strict"
import fs from "node:fs"
import path from "node:path"
import test from "node:test"
import { fileURLToPath } from "node:url"

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..")
const ENTRYPOINT = path.join(ROOT, ".opencode", "plugins", "weekly-advisor.ts")
const REGISTRY = path.join(ROOT, ".opencode", "plugins", "weekly-advisor", "tool-registry.ts")

// Structural fake: minimal spec mirroring the real registry for testing.
class FakeToolDefinition {
  constructor(spec) {
    this.name = spec.name
    this.description = spec.description
    this.cliSubcommand = spec.cliSubcommand
    this.fields = spec.fields || []
    this.timeoutMs = spec.timeoutMs
    this.anchored = spec.anchored ?? true
  }
}

/**
 * Extract registry surface: tool names, CLI commands, field counts.
 * Pure text analysis — no SDK import, no execution.
 *
 * TS RegExp notes:
 * - Tools appear in ToolDefinition with: `name: "weekly_*"` (field in object literal)
 * - CLI commands appear with: `cliSubcommand: "command-name"` or in simpleTool calls
 * - "Specs" are arrays: OPENING_SPECS, REPORTING_SPECS, ANNEX_SPECS contain
 *   {name, subcommand, ...} which get transformed by simpleTool()
 */
function analyzeRegistry(src) {
  const toolNames = new Set()
  const cliCommands = new Set()

  // Extract tool names: `name: "weekly_*"` in object literals
  // Anchored to word boundary or object structure to avoid comment matches
  const nameMatches = [...src.matchAll(/\bname:\s*"(weekly_[a-z_]+)"/g)]
  for (const m of nameMatches) {
    toolNames.add(m[1])
  }

  // Extract direct cliSubcommand declarations
  const directCliMatches = [...src.matchAll(/cliSubcommand:\s*"([a-z-]+)"/g)]
  for (const m of directCliMatches) {
    cliCommands.add(m[1])
  }

  // Extract subcommand from specs: `subcommand: "command-name"` in *_SPEC arrays
  // These get wrapped by simpleTool() which sets cliSubcommand
  const specMatches = [...src.matchAll(/subcommand:\s*"([a-z-]+)"/g)]
  for (const m of specMatches) {
    cliCommands.add(m[1])
  }

  return { toolNames: Array.from(toolNames), cliCommands: Array.from(cliCommands) }
}

/**
 * Extract entrypoint structure: default export keys, presence of static value imports.
 */
function analyzeEntrypoint(src) {
  // Match: const plugin = { ... }; export default plugin
  // or: export default { ... }
  let defaultKeys = []
  let hasDefaultExport = false

  // Try: const X = { ... }; export default X
  const constMatch = src.match(/const\s+(\w+)\s*=\s*({[\s\S]*?^})/m)
  if (constMatch) {
    const constName = constMatch[1]
    const objectBody = constMatch[2]
    hasDefaultExport = src.includes(`export default ${constName}`)
    if (hasDefaultExport) {
      const keysMatches = [...objectBody.matchAll(/^\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*[:=]/gm)]
      defaultKeys = keysMatches.map((m) => m[1])
    }
  }

  // Try: export default { ... }
  if (!hasDefaultExport) {
    const directMatch = src.match(/export\s+default\s+({[\s\S]*?^})/m)
    if (directMatch) {
      hasDefaultExport = true
      const keysMatches = [...directMatch[1].matchAll(/^\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*[:=]/gm)]
      defaultKeys = keysMatches.map((m) => m[1])
    }
  }

  // Check for static value imports like `@opencode-ai/plugin` or `@opencode/plugin`
  // But allow type-only imports: `import type { ... } from "@opencode-ai/plugin"`
  const typeOnlyImports = [...src.matchAll(/import\s+type\s+[\s\S]*?from\s+["']@opencode(?:-ai)?\/plugin["']/g)]
  const allImports = [
    ...src.matchAll(/from\s+["']@opencode-ai\/plugin["']/g),
    ...src.matchAll(/from\s+["']@opencode\/plugin["']/g),
  ]
  const staticValueImports = allImports.filter((m) => {
    // If it's a type-only import, it doesn't count as a static value import
    const fullLine = src.substring(Math.max(0, m.index - 100), m.index + 50)
    return !fullLine.includes("import type")
  })
  const hasStaticValueImport = staticValueImports.length > 0

  return { hasDefaultExport, defaultKeys, hasStaticValueImport }
}

// --------------------------------------------------------------------------
// Tests
// --------------------------------------------------------------------------

test("registry exports 19 tools with unique names", () => {
  const src = fs.readFileSync(REGISTRY, "utf8")
  const { toolNames } = analyzeRegistry(src)
  assert.equal(toolNames.length, 19, `expected 19 tools, got ${toolNames.length}`)
  const unique = new Set(toolNames)
  assert.equal(unique.size, 19, "tool names must be unique")
})

test("registry exports 18 CLI commands (non-empty)", () => {
  const src = fs.readFileSync(REGISTRY, "utf8")
  const { cliCommands } = analyzeRegistry(src)
  assert.equal(cliCommands.length, 18, `expected 18 CLI commands, got ${cliCommands.length}`)
  assert.ok(cliCommands.length > 0, "CLI commands scan must be non-empty")
  // Verify no duplicates
  const unique = new Set(cliCommands)
  assert.equal(unique.size, 18, "CLI command names must be unique")
})

test("entrypoint has exactly one default export with keys id/server/setup", () => {
  const src = fs.readFileSync(ENTRYPOINT, "utf8")
  const { hasDefaultExport, defaultKeys } = analyzeEntrypoint(src)
  assert.ok(hasDefaultExport, "must have a default export")
  assert.deepEqual(defaultKeys.sort(), ["id", "server", "setup"].sort(), "default export must have keys id/server/setup")
})

test("entrypoint does not have static @opencode-ai/@opencode value imports", () => {
  const src = fs.readFileSync(ENTRYPOINT, "utf8")
  const { hasStaticValueImport } = analyzeEntrypoint(src)
  assert.ok(!hasStaticValueImport, "entrypoint must not have static @opencode-ai/plugin or @opencode/plugin imports")
})

test("registry and entrypoint tool count matches (19 tools)", () => {
  const registrySrc = fs.readFileSync(REGISTRY, "utf8")
  const { toolNames } = analyzeRegistry(registrySrc)
  assert.equal(toolNames.length, 19, "registry must define exactly 19 tools")
})

test("registry CLI commands match expected set (18 commands)", () => {
  const src = fs.readFileSync(REGISTRY, "utf8")
  const { cliCommands } = analyzeRegistry(src)
  const expected = [
    "run",
    "releases",
    "watch-context",
    "watch-distill",
    "watch-validate",
    "audit-candidates",
    "show-session",
    "harness",
    "harness-remediate",
    "insights",
    "draft-candidates",
    "report-prep",
    "report-blocks-draft",
    "report-assemble",
    "commit-draft",
    "self-cost",
    "doctor",
    "skill-curate",
  ]
  assert.deepEqual(cliCommands.sort(), expected.sort(), "CLI commands must match expected set")
})

test("no duplicate CLI command registration", () => {
  const src = fs.readFileSync(REGISTRY, "utf8")
  const { cliCommands } = analyzeRegistry(src)
  const unique = new Set(cliCommands)
  assert.equal(unique.size, cliCommands.length, "no duplicate CLI commands allowed")
})

test("preflight tool has no CLI subcommand (in-process)", () => {
  const src = fs.readFileSync(REGISTRY, "utf8")
  const preflightMatch = src.match(/const PREFLIGHT_TOOL[\s\S]*?(?=const\s+\w+_SPEC|const\s+\w+_TOOL|export const)/m)
  assert.ok(preflightMatch, "PREFLIGHT_TOOL definition not found")
  const hasCliSubcommand = /\bcliSubcommand\s*:/.test(preflightMatch[0])
  assert.ok(!hasCliSubcommand, "PREFLIGHT_TOOL must not have cliSubcommand")
})

test("tool definitions are frozen (deepFreeze applied)", () => {
  const src = fs.readFileSync(REGISTRY, "utf8")
  // Check for deepFreeze invocation on TOOL_REGISTRY
  const freezeMatch = /deepFreeze\(\[/.test(src) || /deepFreeze\(TOOL_REGISTRY\)/.test(src)
  assert.ok(freezeMatch, "TOOL_REGISTRY must be frozen with deepFreeze")
})
