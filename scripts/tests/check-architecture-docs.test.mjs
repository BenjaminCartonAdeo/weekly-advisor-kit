import test from "node:test"
import assert from "node:assert/strict"
import { checkArchitectureDocs } from "../check-architecture-docs.mjs"

test("architecture documentation contract reports all baseline checks", () => {
  const result = checkArchitectureDocs({
    architecture: "graphify-architecture-summary.py summary schema_version built_at_commit node_count edge_count source_file_count filtered files relations",
    summaryScript: "--output summary.json raw graph is never touched",
    diagrams: ["architecture.html", "architecture.svg"],
    config: "TelemetryConfig sources storage cost curation read-only JSON flat keys",
    rollout: "observation_only: true no CI impact no curation no apply",
  })

  assert.deepEqual(result.failures, [])
  assert.equal(result.checks.length, 4)
})

test("checks stay advisory when documentation predates the contract", () => {
  const result = checkArchitectureDocs({ architecture: "", summaryScript: "", diagrams: [], config: "", rollout: "" })

  assert.equal(result.failures.length, 4)
  assert.equal(result.advisory, true)
})
