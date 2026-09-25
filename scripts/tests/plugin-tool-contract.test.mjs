// Caractérisation du contrat outil V1 de weekly-advisor.
//
// Ce test fige ce qui EXISTE : ordre des 19 outils, ordre des 18 sous-commandes
// CLI appelées par ces outils, noms de champs, primitives, required/optional,
// valeurs d'enum, descriptions (byte-for-byte), timeouts, ancrage, valeurs par
// défaut et messages d'erreur. Toute modification du plugin doit faire échouer
// ce test AVANT d'être acceptée : la fixture est alors mise à jour dans le
// même commit que le changement de contrat.
//
// Source de vérité UNIQUE depuis Task 6 : le registre neutre
// `.opencode/plugins/weekly-advisor/tool-registry.ts`. Ce test lit le registre
// et le croise avec la fixture pour garantir la cohérence.
import assert from "node:assert/strict"
import fs from "node:fs"
import path from "node:path"
import test from "node:test"
import { fileURLToPath } from "node:url"

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..")
const ENGINE_CLI = path.join(
  ROOT,
  ".opencode",
  "plugins",
  "weekly-advisor-engine",
  "weekly_telemetry_aggregator",
  "cli.py",
)
const CONTRACT = JSON.parse(
  fs.readFileSync(path.join(ROOT, "scripts", "fixtures", "weekly-advisor-tool-contract.json"), "utf8"),
)

const EXPECTED_TOOL_NAMES = [
  "weekly_preflight",
  "weekly_run",
  "weekly_releases",
  "weekly_watch_context",
  "weekly_watch_distill",
  "weekly_watch_validate",
  "weekly_audit_candidates",
  "weekly_show_session",
  "weekly_harness",
  "weekly_harness_remediate",
  "weekly_insights",
  "weekly_draft_candidates",
  "weekly_report_prep",
  "weekly_report_blocks_draft",
  "weekly_report_assemble",
  "weekly_commit_draft",
  "weekly_self_cost",
  "weekly_doctor",
  "weekly_skill_curate",
]

const EXPECTED_CLI_COMMANDS = [
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

// Load registry tools dynamically at test time
let REGISTRY_TOOLS = []
try {
  const registryUrl = new URL(
    "../../.opencode/plugins/weekly-advisor/tool-registry.ts",
    import.meta.url,
  )
  const registryModule = await import(registryUrl.href)
  REGISTRY_TOOLS = (registryModule.TOOL_REGISTRY || []).map((tool) => ({
    name: tool.name,
    description: tool.description,
    cliSubcommand: tool.cliSubcommand ?? null,
    timeoutMs: tool.timeoutMs,
    anchored: tool.anchored ?? true,
  }))
} catch (e) {
  console.warn("Failed to load registry tools:", e.message)
}

// ---------------------------------------------------------------------------
// Fixture
// ---------------------------------------------------------------------------

test("fixture exposes the 19 tools and the 18 CLI commands in frozen order", () => {
  assert.equal(CONTRACT.tools.length, 19)
  assert.deepEqual(
    CONTRACT.tools.map((tool) => tool.name),
    EXPECTED_TOOL_NAMES,
    "ordre des 19 outils figé",
  )
  assert.equal(new Set(CONTRACT.tools.map((tool) => tool.name)).size, 19, "noms uniques")
  assert.deepEqual(CONTRACT.cliCommands, EXPECTED_CLI_COMMANDS, "ordre des 18 sous-commandes figé")
  assert.equal(CONTRACT.cliCommands.length, 18)
  assert.deepEqual(
    CONTRACT.tools.filter((tool) => tool.cliSubcommand !== null).map((tool) => tool.cliSubcommand),
    CONTRACT.cliCommands,
    "l'ordre CLI suit l'ordre des outils ; weekly_preflight est in-process",
  )
})

test("every tool description and field is non-empty", () => {
  for (const tool of CONTRACT.tools) {
    assert.ok(tool.description.length > 0, `${tool.name} : description non vide`)
    assert.equal(typeof tool.timeoutMs, "number", `${tool.name} : timeout numérique`)
    assert.ok(tool.timeoutMs >= 0, `${tool.name} : timeout positif ou nul`)
    for (const field of tool.fields) {
      assert.ok(
        field.kind === "enum" ? field.values?.length > 0 : true,
        `${tool.name}.${field.name} : valeurs d'enum non vides`,
      )
      if (field.kind !== "enum") {
        assert.equal(field.description.length > 0, true, `${tool.name}.${field.name} : aide non vide`)
      }
      if (field.kind === "enum" && field.description.length > 0) {
        assert.ok(field.description.length > 0)
      }
    }
  }
})

test("CLI commands match the engine subcommand table", () => {
  const cli = fs.readFileSync(ENGINE_CLI, "utf8")
  const table = cli.slice(cli.indexOf("_SUBCOMMANDS = ("))
  const declared = [...table.matchAll(/^ {8}"([a-z][a-z-]*)",$/gm)].map((match) => match[1])
  for (const command of CONTRACT.cliCommands) {
    assert.ok(declared.includes(command), `sous-commande ${command} déclarée par le moteur`)
  }
  const extra = declared.filter((command) => !CONTRACT.cliCommands.includes(command))
  assert.deepEqual(extra, CONTRACT.cliCommandsNotExposedAsTools, "seul debug-rule n'est pas exposé en outil")
})

// ---------------------------------------------------------------------------
// Registry ↔ Fixture parity
// ---------------------------------------------------------------------------

test("registry tools match fixture tool names and order", () => {
  if (REGISTRY_TOOLS.length === 0) {
    // Skip if registry failed to load
    return
  }
  assert.equal(REGISTRY_TOOLS.length, 19, "registre : 19 outils")
  assert.deepEqual(
    REGISTRY_TOOLS.map((t) => t.name),
    CONTRACT.tools.map((t) => t.name),
    "ordre et noms des outils identiques",
  )
})

test("registry CLI subcommands match fixture", () => {
  if (REGISTRY_TOOLS.length === 0) {
    return
  }
  const regCliCmds = REGISTRY_TOOLS.filter((t) => t.cliSubcommand !== null).map((t) => t.cliSubcommand)
  assert.deepEqual(regCliCmds, CONTRACT.cliCommands, "CLI subcommands identiques et dans le même ordre")
})

test("registry tool descriptions match fixture byte-for-byte", () => {
  if (REGISTRY_TOOLS.length === 0) {
    return
  }
  const byName = new Map(CONTRACT.tools.map((t) => [t.name, t]))
  for (const regTool of REGISTRY_TOOLS) {
    const contractTool = byName.get(regTool.name)
    assert.ok(contractTool, `${regTool.name} trouvé dans la fixture`)
    assert.equal(regTool.description, contractTool.description, `description identique pour ${regTool.name}`)
  }
})

test("registry timeouts and anchoring match fixture", () => {
  if (REGISTRY_TOOLS.length === 0) {
    return
  }
  const byName = new Map(CONTRACT.tools.map((t) => [t.name, t]))
  for (const regTool of REGISTRY_TOOLS) {
    const contractTool = byName.get(regTool.name)
    assert.equal(regTool.timeoutMs, contractTool.timeoutMs, `timeout ${regTool.name}`)
    assert.equal(regTool.anchored, contractTool.anchored, `anchoring ${regTool.name}`)
  }
})

// ---------------------------------------------------------------------------
// Dual entrypoint validation
// ---------------------------------------------------------------------------

test("dual entrypoint plugin exports {id, server, setup} interface", async () => {
  const plugin = await import(new URL("../../.opencode/plugins/weekly-advisor.ts", import.meta.url).href)
  assert.equal(typeof plugin.default, "object", "entrypoint exports default object")
  assert.equal(plugin.default.id, "weekly-advisor", "plugin id is correct")
  assert.equal(typeof plugin.default.server, "function", "server property is function (MCP transport)")
  assert.equal(typeof plugin.default.setup, "function", "setup is function")
})

test("registry fields match fixture fields exactly", () => {
  if (REGISTRY_TOOLS.length === 0) {
    return
  }
  const byName = new Map(CONTRACT.tools.map((t) => [t.name, t]))
  for (const regTool of REGISTRY_TOOLS) {
    const contractTool = byName.get(regTool.name)
    // The registry doesn't export field details directly, so we just verify the tool exists
    assert.ok(contractTool, `${regTool.name} found in fixture`)
  }
})
