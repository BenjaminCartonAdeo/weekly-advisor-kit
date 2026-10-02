// Contrat du flux kit → cible du cron : scripts/sync-to-target.sh (transfert,
// dry-run par défaut) et scripts/check-drift.sh (verdict de dérive).
//
// Les deux scripts sont des coquilles POSIX : ils sont testés en sandbox
// temporaire via `--target <tmpdir>`, jamais contre le déploiement réel. La
// cible réelle n'est jamais écrite par cette suite.
//
// Le cycle complet (dry-run → apply → drift 0 → parasite → drift ≠ 0) exige
// `rsync`. Sans rsync, seule la branche `--apply` est sautée ; le contrat du
// plan, du verdict et de la cohérence entre les deux scripts reste couvert.
import assert from "node:assert/strict"
import { spawnSync } from "node:child_process"
import fs from "node:fs"
import os from "node:os"
import path from "node:path"
import test from "node:test"
import { fileURLToPath } from "node:url"

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..")
const SYNC = path.join(ROOT, "scripts", "sync-to-target.sh")
const DRIFT = path.join(ROOT, "scripts", "check-drift.sh")
const ENGINE_REL = ".opencode/plugins/weekly-advisor-engine"

// Mêmes exclusions que les scripts : la cible sandbox doit être bâtie avec les
// mêmes exclusions que le plan, sinon le test mesure autre chose.
const EXCLUDES = [
  "__pycache__",
  ".ruff_cache",
  ".pytest_cache",
  "*.egg-info",
  ".venv",
  "dist",
  "reports",
  ".git",
  "node_modules",
  "weekly-telemetry-config.json",
]

const hasRsync = spawnSync("rsync", ["--version"], { encoding: "utf8" }).status === 0
const skipRsync = hasRsync ? false : "rsync absent : cycle --apply non exécutable"

function run(script, args, env = {}) {
  const result = spawnSync("bash", [script, ...args], {
    cwd: ROOT,
    encoding: "utf8",
    env: { ...process.env, ...env },
  })
  return {
    status: result.status,
    stdout: String(result.stdout ?? ""),
    stderr: String(result.stderr ?? ""),
    output: String(result.stdout ?? "") + String(result.stderr ?? ""),
  }
}

// Préfixe volontairement HORS de « weekly- » : plugin-v2.test.mjs exige que
// /tmp ne contienne aucun staging résiduel nommé « weekly-* ».
function tmpdir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "wa-sync-drift-"))
}

/**
 * Copie l'arborescence du kit vers une racine cible, avec les exclusions des
 * scripts. Sans rsync ni tar : `fs.cpSync` natif, filtre sur le nom de base
 * (une exclusion de répertoire saute tout son contenu).
 */
function buildTarget(target) {
  const excluded = EXCLUDES.map((pattern) =>
    pattern.includes("*")
      ? new RegExp(`^${pattern.replaceAll("*", ".*")}$`)
      : pattern,
  )
  const keep = (src) => {
    const name = path.basename(src)
    return !excluded.some((pattern) =>
      typeof pattern === "string" ? name === pattern : pattern.test(name),
    )
  }
  const units = [
    [path.join(ROOT, ENGINE_REL), path.join(target, ENGINE_REL)],
    ...fs
      .readdirSync(path.join(ROOT, ".opencode", "skills"), { withFileTypes: true })
      .filter((e) => e.isDirectory() && e.name.startsWith("weekly"))
      .map((e) => [
        path.join(ROOT, ".opencode", "skills", e.name),
        path.join(target, ".opencode", "skills", e.name),
      ]),
  ]
  for (const [src, dst] of units) {
    fs.mkdirSync(dst, { recursive: true })
    fs.cpSync(src, dst, { recursive: true, filter: keep })
  }
  return target
}

test("check-drift: cible absente = SKIP propre, exit 0", (t) => {
  t.diagnostic("un runner CI n'a pas la cible du cron à côté du kit")
  const base = tmpdir()
  t.after(() => fs.rmSync(base, { recursive: true, force: true }))
  const result = run(DRIFT, ["--target", path.join(base, "absent")])
  assert.equal(result.status, 0)
  assert.match(result.stdout, /SKIP/)
})

test("check-drift: usage invalide = exit 2", (t) => {
  t.diagnostic("un argument inconnu ne doit pas passer pour un verdict")
  assert.equal(run(DRIFT, ["--cible-qui-nexiste-pas"]).status, 2)
  assert.equal(run(DRIFT, ["--target"]).status, 2)
})

test("check-drift: sandbox alignée = exit 0, tolérances signalées", (t) => {
  const base = tmpdir()
  t.after(() => fs.rmSync(base, { recursive: true, force: true }))
  const target = buildTarget(path.join(base, "cible"))
  const result = run(DRIFT, ["--target", target])
  assert.equal(result.status, 0, result.output)
  assert.match(result.output, /OK : kit et cible alignés/)
  // Les deux tolérances doivent être annoncées, pas ignorées en silence.
  assert.match(result.output, /\[toléré\] weekly-telemetry-config\.json/)
  assert.match(result.output, /\[toléré\] weekly_advisor\.egg-info\/SOURCES\.txt/)
})

test("check-drift: fichier parasite dans la cible = exit 1 + sortie actionnable", (t) => {
  const base = tmpdir()
  t.after(() => fs.rmSync(base, { recursive: true, force: true }))
  const target = buildTarget(path.join(base, "cible"))
  const parasite = path.join(target, ENGINE_REL, "__parasite__.py")
  fs.writeFileSync(parasite, "# parasite\n")
  const drifted = run(DRIFT, ["--target", target])
  assert.equal(drifted.status, 1)
  assert.match(drifted.output, /DRIFT en trop\s+:\s+__parasite__\.py/)
  // Le message doit dire quoi faire, pas seulement constater.
  assert.match(drifted.output, /sync-to-target\.sh --apply/)
  fs.rmSync(parasite)
  assert.equal(run(DRIFT, ["--target", target]).status, 0, "retour à l'alignement")
})

test("check-drift: répertoire weekly* orphelin côté cible = exit 1, DEL au plan", (t) => {
  t.diagnostic("compétence weekly laissée dans la cible après un renommage côté kit")
  const base = tmpdir()
  t.after(() => fs.rmSync(base, { recursive: true, force: true }))
  const target = buildTarget(path.join(base, "cible"))
  // Le répertoire est ajouté APRÈS buildTarget : il n'existe pas dans le kit,
  // donc seule une liste d'unités en UNION kit ∪ cible peut le voir.
  const orphanRel = ".opencode/skills/weekly-zzz-orphelin"
  const orphan = path.join(target, orphanRel)
  fs.mkdirSync(orphan, { recursive: true })
  fs.writeFileSync(path.join(orphan, "SKILL.md"), "---\nname: weekly-zzz-orphelin\n---\n")

  const drifted = run(DRIFT, ["--target", target])
  assert.equal(drifted.status, 1, drifted.output)
  assert.match(drifted.output, /DRIFT en trop\s+: \.opencode\/skills\/weekly-zzz-orphelin/)
  // Le message doit dire quoi faire, pas seulement constater.
  assert.match(drifted.output, /n'existe pas dans le kit/)
  assert.match(drifted.output, /supprime-le/)

  // Le plan doit exposer la suppression au niveau du répertoire.
  const plan = run(SYNC, ["--dry-run", "--target", target])
  assert.equal(plan.status, 0, plan.output)
  assert.match(plan.output, /DEL \.opencode\/skills\/weekly-zzz-orphelin/)
  assert.ok(fs.existsSync(orphan), "le dry-run ne doit rien écrire")

  if (!hasRsync) {
    t.diagnostic("rsync absent : --apply non exécutable, plan et verdict suffisent")
    fs.rmSync(orphan, { recursive: true, force: true })
  } else {
    const applied = run(SYNC, ["--apply", "--target", target])
    assert.equal(applied.status, 0, applied.output)
    assert.ok(!fs.existsSync(orphan), "--apply doit supprimer la compétence orpheline")
  }
  assert.equal(run(DRIFT, ["--target", target]).status, 0, "retour à l'alignement")
})

test("check-drift: contenu modifié dans la cible = exit 1", (t) => {
  const base = tmpdir()
  t.after(() => fs.rmSync(base, { recursive: true, force: true }))
  const target = buildTarget(path.join(base, "cible"))
  const victim = path.join(target, ENGINE_REL, "pyproject.toml")
  fs.appendFileSync(victim, "\n# drift A3\n")
  const drifted = run(DRIFT, ["--target", target])
  assert.equal(drifted.status, 1)
  assert.match(drifted.output, /DRIFT différent : pyproject\.toml/)
})

test("sync-to-target: dry-run est le défaut, affiche un plan, n'écrit rien", (t) => {
  const base = tmpdir()
  t.after(() => fs.rmSync(base, { recursive: true, force: true }))
  const target = path.join(base, "cible")
  fs.mkdirSync(target, { recursive: true })
  // Une seule skill, volontairement en retard, pour que le plan soit non vide.
  const skillRel = ".opencode/skills/weekly-drafting"
  fs.mkdirSync(path.join(target, skillRel), { recursive: true })
  fs.writeFileSync(path.join(target, skillRel, "SKILL.md"), "---\nname: weekly-drafting\n---\n")
  fs.writeFileSync(path.join(target, skillRel, "obsolete.md"), "à supprimer\n")

  const result = run(SYNC, ["--target", target])
  assert.equal(result.status, 0, result.output)
  assert.match(result.output, /mode {3}: dry-run/)
  assert.match(result.output, /M SKILL\.md/, "contenu différent = modifié")
  assert.match(result.output, /- obsolete\.md/, "fichier en trop = suppression")
  // Le dry-run n'écrit rien : la cible est inchangée.
  assert.ok(fs.existsSync(path.join(target, skillRel, "obsolete.md")))
  assert.match(
    fs.readFileSync(path.join(target, skillRel, "SKILL.md"), "utf8"),
    /^---\nname: weekly-drafting\n---\n$/,
    "dry-run ne doit pas écraser la cible",
  )
  // Action proposée explicitement.
  assert.match(result.output, /--apply/)
})

test("sync-to-target: --apply refuse une cible absente", (t) => {
  const base = tmpdir()
  t.after(() => fs.rmSync(base, { recursive: true, force: true }))
  const result = run(SYNC, ["--apply", "--target", path.join(base, "absent")])
  assert.equal(result.status, 1)
  assert.match(result.output, /cible absente/)
  assert.ok(!fs.existsSync(path.join(base, "absent")), "aucun dépôt ne doit être créé")
})

test("sync-to-target: --apply : cycle complet quand rsync est disponible", { skip: skipRsync }, (t) => {
  const base = tmpdir()
  t.after(() => fs.rmSync(base, { recursive: true, force: true }))
  const target = buildTarget(path.join(base, "cible"))
  // Salit la cible : contenu modifié, fichier en trop, config par-deployment.
  const engine = path.join(target, ENGINE_REL)
  fs.appendFileSync(path.join(engine, "pyproject.toml"), "\n# sali\n")
  fs.writeFileSync(path.join(engine, "parasite.py"), "x\n")
  fs.writeFileSync(path.join(engine, "weekly-telemetry-config.json"), '{"project_root":"/cible"}\n')

  assert.equal(run(DRIFT, ["--target", target]).status, 1, "pré-condition : dérive réelle")

  const plan = run(SYNC, ["--dry-run", "--target", target])
  assert.equal(plan.status, 0, plan.output)
  assert.match(plan.output, /M pyproject\.toml/)
  assert.match(plan.output, /- parasite\.py/)
  assert.ok(
    fs.existsSync(path.join(engine, "parasite.py")),
    "le dry-run ne doit rien écrire",
  )

  const applied = run(SYNC, ["--apply", "--target", target])
  assert.equal(applied.status, 0, applied.output)
  assert.match(applied.output, /APPLY terminé/)

  const verdict = run(DRIFT, ["--target", target])
  assert.equal(verdict.status, 0, verdict.output)
  assert.ok(!fs.existsSync(path.join(engine, "parasite.py")), "--delete doit élaguer")
  assert.ok(
    fs.existsSync(path.join(engine, "weekly-telemetry-config.json")),
    "un fichier exclu est protégé de --delete",
  )
  assert.match(
    fs.readFileSync(path.join(engine, "weekly-telemetry-config.json"), "utf8"),
    /"project_root":"\/cible"/,
    "l'exclusion protège la config par-deployment : elle ne doit pas être écrasée",
  )
})

test("un artefact de build (dist/) du kit n'est ni transféré ni compté comme dérive", () => {
  // `uv build` écrit dist/ dans l'arborescence du moteur. C'est ignoré par git,
  // donc invisible dans un diff — mais bien présent sur le disque, et donc
  // candidat au transfert. Un wheel déployé dans la cible du cron n'est pas un
  // défaut anodin : il double la version embarquée et crée une fausse dérive au
  // premier nettoyage de dist/.
  const base = tmpdir()
  const target = buildTarget(path.join(base, "cible"))
  const engine = path.join(target, ENGINE_REL)
  fs.mkdirSync(path.join(engine, "dist"), { recursive: true })
  fs.writeFileSync(path.join(engine, "dist", "weekly_advisor-0.4.1-py3-none-any.whl"), "binaire\n")

  const plan = run(SYNC, ["--dry-run", "--target", target])
  assert.equal(plan.status, 0, plan.output)
  assert.ok(
    !plan.output.includes("dist/"),
    `le plan ne doit jamais mentionner dist/ :\n${plan.output}`,
  )

  const verdict = run(DRIFT, ["--target", target])
  assert.equal(
    verdict.status,
    0,
    `un dist/ côté cible ne doit pas créer une fausse dérive :\n${verdict.output}`,
  )
  assert.ok(!verdict.output.includes("dist/"), verdict.output)
})

test("les deux scripts déclarent la même cible par défaut et les mêmes exclusions", () => {
  const sync = fs.readFileSync(SYNC, "utf8")
  const drift = fs.readFileSync(DRIFT, "utf8")
  const defaultOf = (src) => src.match(/TARGET_DEFAULT="\$\(dirname -- "\$ROOT"\)\/(\S+)"/)?.[1]
  const excludesOf = (src) =>
    [...src.matchAll(/WA_EXCLUDES=\(\n([\s\S]*?)\n\)/g)][0]?.[1]
      .split("\n")
      .map((line) => line.trim().replace(/^"|"$/g, ""))
      .filter(Boolean)
  assert.equal(defaultOf(sync), "Adeo", "cible par défaut du script de sync")
  assert.equal(defaultOf(drift), defaultOf(sync), "les deux scripts doivent cibler le même dépôt")
  assert.deepEqual(excludesOf(drift), excludesOf(sync), "exclusions divergentes = faux/drifts")
  assert.ok(excludesOf(sync).includes("weekly-telemetry-config.json"))
  assert.ok(excludesOf(sync).includes("*.egg-info"))
  assert.ok(excludesOf(sync).includes(".venv"))
  assert.ok(excludesOf(sync).includes("dist"), "dist/ est un artefact de build, jamais déployable")
})
