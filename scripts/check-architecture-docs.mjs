#!/usr/bin/env node
/** Advisory checks for architecture documentation contracts. */
import fs from "node:fs"
import path from "node:path"
import { fileURLToPath } from "node:url"

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..")

const read = (file) => fs.readFileSync(file, "utf8")

/**
 * Check architecture documentation without failing rollout.
 * @param {{architecture?: string, diagrams?: string[], config?: string, rollout?: string}} [sources]
 * @returns {{checks: Array<{name: string, passed: boolean}>, failures: string[], advisory: boolean}}
 */
export function checkArchitectureDocs(sources = {}) {
  const architecture = sources.architecture ?? read(path.join(ROOT, "doc", "ARCHITECTURE.md"))
  const diagrams = sources.diagrams ?? fs.readdirSync(path.join(ROOT, "doc", "diagrams"))
  const config = sources.config ?? architecture
  const rollout = sources.rollout ?? architecture
  const checks = [
    {
      name: "paired HTML/SVG diagram parity",
      passed: (() => {
        const html = new Set(diagrams.filter((name) => name.endsWith(".html")).map((name) => name.slice(0, -5)))
        const svg = new Set(diagrams.filter((name) => name.endsWith(".svg")).map((name) => name.slice(0, -4)))
        return html.size > 0 && html.size === svg.size && [...html].every((name) => svg.has(name))
      })(),
    },
    {
      name: "configuration schema documentation",
      passed:
        /TelemetryConfig/.test(config) &&
        /sources/.test(config) &&
        /storage/.test(config) &&
        /cost/.test(config) &&
        /curation/.test(config) &&
        (/read[\s\S]{0,20}only/i.test(config) || /lecture[\s\S]{0,20}seule/i.test(config)) &&
        /flat keys|clés plates/i.test(config),
    },
    {
      name: "observation-only rollout",
      passed:
        /observation_only\s*:\s*true/.test(rollout) &&
        /no CI impact|pas d'impact CI/i.test(rollout) &&
        /no curation|ni curation/i.test(rollout) &&
        /no apply|ni application/i.test(rollout),
    },
  ]
  return {
    checks,
    failures: checks.filter((check) => !check.passed).map((check) => check.name),
    advisory: true,
  }
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const result = checkArchitectureDocs()
  for (const check of result.checks) console.log(`${check.passed ? "ok" : "WARN"} ${check.name}`)
  console.log("ARCHITECTURE-DOCS ADVISORY — non-blocking until baseline")
}
