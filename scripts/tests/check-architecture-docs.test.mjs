import test from "node:test"
import assert from "node:assert/strict"
import { checkArchitectureDocs } from "../check-architecture-docs.mjs"

test("architecture documentation contract reports all baseline checks", () => {
  const result = checkArchitectureDocs({
    architecture: "moteur déterministe, vues lecture seule",
    diagrams: ["architecture.html", "architecture.svg"],
    config: "TelemetryConfig sources storage cost curation read-only JSON flat keys",
    rollout: "observation_only: true no CI impact no curation no apply",
  })

  assert.deepEqual(result.failures, [])
  assert.equal(result.checks.length, 3)
})

test("checks stay advisory when documentation predates the contract", () => {
  const result = checkArchitectureDocs({ architecture: "", diagrams: [], config: "", rollout: "" })

  assert.equal(result.failures.length, 3)
  assert.equal(result.advisory, true)
})
