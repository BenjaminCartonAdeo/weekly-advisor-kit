import assert from "node:assert/strict"
import fs from "node:fs"
import os from "node:os"
import path from "node:path"
import test from "node:test"

// Fix P1 (run cron) : le tool Glob résout `.` depuis le cwd du serveur persistant
// (/home/benjamin), pas depuis `--dir /home/benjamin/Dev/Adeo`, et l'ancien pattern
// `summary-*.json` ne matchait jamais le fichier réel `weekly-summary-<date>.json`
// (préfixe `weekly-` manquant). Ce test fige le pattern corrigé.

function makeRunCurrent() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "wa-glob-"))
  const dir = path.join(root, "reports", "runs", "current")
  fs.mkdirSync(dir, { recursive: true })
  fs.writeFileSync(path.join(dir, "weekly-summary-2026-09-05.json"), '{"exit":0}')
  return root
}

test("ancien pattern summary-*.json ne matchait pas weekly-summary-*.json", () => {
  const root = makeRunCurrent()
  try {
    const oldMatches = fs.globSync("reports/runs/current/summary-*.json", { cwd: root })
    assert.equal(oldMatches.length, 0, "l'ancien pattern doit retourner 0 match (bug P1)")
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
})

test("pattern corrige weekly-summary-*.json matche le fichier temoin", () => {
  const root = makeRunCurrent()
  try {
    const fixed = fs.globSync("reports/runs/current/weekly-summary-*.json", { cwd: root })
    assert.equal(fixed.length, 1)
    assert.match(fixed[0], /weekly-summary-2026-09-05\.json$/)
    const absPattern = path.join(root, "reports", "runs", "current", "weekly-summary-*.json")
    const absMatches = fs.globSync(absPattern)
    assert.equal(absMatches.length, 1, "le pattern absolu (worktree) doit matcher hors cwd serveur")
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
})
