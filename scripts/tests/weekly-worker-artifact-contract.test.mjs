import assert from "node:assert/strict"
import fs from "node:fs"
import path from "node:path"
import test from "node:test"
import { fileURLToPath } from "node:url"

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..")
const read = (relativePath) => fs.readFileSync(path.join(ROOT, relativePath), "utf8")

const WORKER = ".opencode/agents/weekly-advisor/weekly-advisor-worker.md"
const AUDIT = ".opencode/skills/weekly-quality-audit/SKILL.md"
const WATCH = ".opencode/skills/weekly-watch-review/SKILL.md"
const REMEDIATION = ".opencode/skills/harness-remediation/SKILL.md"
const DRAFTING = ".opencode/skills/weekly-drafting/SKILL.md"

test("audit worker defines a schema-valid bounded transcript artifact", () => {
  const source = read(WORKER)
  assert.match(source, /schema_version["`]?\s*[:：]\s*1/)
  assert.match(source, /summary[^\n]*(?:non-vide|non-empty)/i)
  assert.match(source, /findings[^\n]*array|findings[^\n]*\[\.\.\.\]/i)
  assert.match(source, /warnings[^\n]*array|warnings[^\n]*\[\.\.\.\]/i)
  assert.match(source, /transcript-truncated:<session_id>/)
  assert.match(source, /complete[- ]enough[\s\S]{0,180}rc=0|rc=0[\s\S]{0,180}complete[- ]enough/i)
  assert.doesNotMatch(source, /re-export réussi[\s\S]{0,120}rc=1/i)
})

test("audit retry and worker lifecycle are bounded", () => {
  const source = read(WORKER)
  assert.match(source, /une seule retry|une seule relance|one bounded\s+retry/i)
  assert.match(source, /max_retry\s*[:=]\s*1|max(?:imum)?\s*:?\s*1[^\n]*(?:retry|relance)|retry[^\n]*max(?:imum)?\s*:?\s*1/i)
  assert.match(source, /fenêtres bornées|bounded windows|offset\/limit/i)
  assert.match(source, /sans\s+respawn automatique|no respawn loops|no respawn/i)
  assert.match(source, /10 min maximum|plafond[^\n]*10 min|timeout/i)
})

test("audit skill requires a strict artifact envelope and bounded recovery", () => {
  const source = read(AUDIT)
  assert.match(source, /schema_version/i)
  assert.match(source, /summary[^\n]*(?:non-vide|non-empty)/i)
  assert.match(source, /findings[^\n]*(?:array|\[)/i)
  assert.match(source, /transcript-truncated:<session_id>/)
  assert.match(source, /complete[- ]enough[\s\S]{0,220}rc\s*[:=]\s*0|rc\s*[:=]\s*0[\s\S]{0,220}complete[- ]enough/i)
  assert.match(source, /une seule retry|une seule relance|one bounded\s+retry/i)
  assert.match(source, /ne jamais inventer|never invent/i)
})

test("watch review writes raw findings before validation and forbids invention", () => {
  const source = read(WATCH)
  assert.match(source, /raw/i)
  assert.match(source, /avant[^\n]*(?:validate|validation)|raw[\s\S]{0,400}(?:validate|validation)/i)
  assert.match(source, /weekly-watch-findings-raw-<date>\.json/)
  assert.match(source, /weekly_watch_validate|watch-validate/)
  assert.match(source, /Jamais inventé|ne jamais inventer|never invent/i)
  assert.match(source, /hors worktree|out-of-tree|out of tree/i)
  assert.match(source, /external[-_ ]directory|permission[^\n]*report-only|report-only/i)
})

test("harness remediation requires proposals before apply and keeps security IDs blocking", () => {
  const source = read(REMEDIATION)
  assert.match(source, /raw|proposition/i)
  assert.match(source, /proposal.*(?:avant|before)|avant[\s\S]{0,220}proposal/i)
  assert.match(source, /weekly-harness-remediation-proposals-<date>\.json/)
  assert.match(source, /harness-remediate/)
  assert.match(source, /une seule retry|une seule relance|one bounded\s+retry/i)
  assert.match(source, /ne jamais inventer|never invent/i)
  assert.match(source, /hors worktree|out-of-tree|out of tree/i)
  assert.match(source, /external[-_ ]directory|permission[^\n]*report-only|report-only/i)
  for (const rule of ["mcp-tool-poisoning", "unbounded-delegation", "memory-write-unscoped"]) {
    assert.match(source, new RegExp(rule.replaceAll("-", "\\-")))
  }
  assert.match(source, /bloqu|blocking/i)
})

test("drafting keeps recovery bounded and reports out-of-tree permission requests", () => {
  const source = read(DRAFTING)
  assert.match(source, /une seule retry|une seule relance|one bounded retry/i)
  assert.match(source, /ne jamais inventer|never invent/i)
  assert.match(source, /hors worktree|out-of-tree|out of tree/i)
  assert.match(source, /external[-_ ]directory|permission[^\n]*report-only|report-only/i)
})

test("valid truncation and external permission records are nonblocking facts", () => {
  const worker = read(WORKER)
  const audit = read(AUDIT)
  assert.match(worker, /transcript-truncated:<session_id>[\s\S]{0,260}nonblocking/i)
  assert.match(audit, /transcript-truncated:<session_id>[\s\S]{0,260}nonblocking/i)

  for (const relativePath of [WORKER, AUDIT, WATCH, REMEDIATION, DRAFTING]) {
    const source = read(relativePath)
    assert.match(source, /report_only:\s*true/)
    assert.match(source, /external-permission-refusal/)
    assert.match(source, /out-of-tree[\s\S]{0,320}(?:ne pas|no)\s+(?:lire|read),?\s+(?:écrire|write)/i)
  }
})
