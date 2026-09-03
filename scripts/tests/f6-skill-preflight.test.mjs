import test from "node:test"
import assert from "node:assert/strict"
import fs from "node:fs"
import path from "node:path"
import { fileURLToPath } from "node:url"

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..")
const read = (p) => fs.readFileSync(path.join(ROOT, p), "utf8")

const WORKER = ".opencode/agents/weekly-advisor/weekly-advisor-worker.md"
const ORCH = ".opencode/agents/weekly-advisor/weekly-advisor.md"
const CMD = ".opencode/commands/weekly-review.md"

/**
 * Contrat F6 — fiabilité du chargement des skills worker (design
 * 2026-09-01-weekly-run-reliability-design.md §5/§6/§7). Vérifie statiquement
 * que worker + orchestrateur + commande déclarent le pre-flight skills :
 *   - `skills_loaded` dans le contrat worker ;
 *   - mapping rc : primaire absente → rc=2 (STOP), secondaire absente → rc=1 +
 *     warning `skill-missing:<name>` (dégradé) ;
 *   - vérif agent worker + skills primaires avant dispatch (orchestrateur/commande) ;
 *   - aucun apply automatique ni déplacement de skill (F6 = signal, pas de mutation).
 */
test("worker déclare le champ skills_loaded dans son contrat retour", () => {
  const src = read(WORKER)
  assert.match(src, /"skills_loaded"/)
  assert.match(src, /ok,\s*primary,\s*secondary,\s*missing/)
})

test("worker mappe rc par classe de skill (primaire/secondaire)", () => {
  const src = read(WORKER)
  // Primaire absente → rc=2 fatalité branche
  assert.match(src, /Skill primaire absente[\s\S]{0,120}rc=2/)
  assert.match(src, /rc=2[\s\S]{0,200}primaire de branche absente — F6/)
  // Secondaire absente → rc=1 + warning skill-missing exact
  assert.match(src, /Skill secondaire absente[\s\S]{0,160}rc=1/)
  assert.match(src, /skill-missing:<name>/)
})

test("worker dresse la table branch → skill requise", () => {
  const src = read(WORKER)
  for (const [branch, skill] of [
    ["A", "weekly-quality-audit"],
    ["V", "weekly-watch-review"],
    ["H", "harness-remediation"],
    ["D", "weekly-drafting"],
    ["C", "weekly-coherence-review"],
  ]) {
    assert.match(src, new RegExp(`\\|\\s*${branch}\\s*\\(`), `branche ${branch} présente`)
    assert.match(src, new RegExp(skill.replaceAll("-", "\\-")), `skill ${skill} référencée`)
  }
  // T/I : aucune skill (étapes déterministes)
  assert.match(src, /T \/ I/)
})

test("orchestrateur vérifie l'agent worker et les skills primaires avant dispatch", () => {
  const src = read(ORCH)
  assert.match(src, /weekly-advisor-worker\.md/)
  assert.match(src, /Vérif dispatch \(F6\)/)
  assert.match(src, /STOP avant WAVE 1[\s\S]{0,80}rc=2/)
  // « skills primaires de branche » (l.92) → « rc=2 » (l.94, STOP orchestrateur)
  assert.match(src, /skills primaires de branche[\s\S]{0,400}rc=2/)
  assert.match(src, /Primaire absente[\s\S]{0,160}rc=2/)
  assert.match(src, /skill-missing:<name>/)
})

test("orchestrateur agrège skills_loaded au JOIN", () => {
  const src = read(ORCH)
  assert.match(src, /skills_loaded.*agrégés?/)
  assert.match(src, /skills_loaded.*JOIN|JOIN.*skills_loaded/s)
})

test("commande documente la vérif dispatch F6 sans mutation de skills", () => {
  const src = read(CMD)
  assert.match(src, /vérif dispatch F6|Vérif dispatch F6/i)
  assert.match(src, /skill-missing:<name>/)
  assert.match(src, /Aucune écriture ni déplacement dans `?\.opencode\/skills\/`?/)
})

test("aucun apply automatique ni déplacement de skill (F6 signal only)", () => {
  const worker = read(WORKER)
  const orch = read(ORCH)
  const cmd = read(CMD)
  const all = worker + "\n" + orch + "\n" + cmd
  // F6 ne déclenche jamais d'apply ni de move : le pre-flight signale, ne mute pas.
  assert.match(all, /Aucune écriture/)
  assert.match(all, /jamais de chargement implicite|Aucune écriture ni déplacement/)
  assert.doesNotMatch(worker, /apply=true/)
})
