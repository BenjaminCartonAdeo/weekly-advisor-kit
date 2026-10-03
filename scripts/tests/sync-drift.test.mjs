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
// Les 3 unités qui manquaient du périmètre : sans elles, un cron peut exécuter
// un plugin TS, des agents et une commande obsolètes tout en répondant « aligné ».
const PLUGIN_REL = ".opencode/plugins/weekly-advisor" // plugin TS — unité DIRECTORY
const AGENTS_REL = ".opencode/agents/weekly-advisor" // agents — unité DIRECTORY
const COMMAND_REL = ".opencode/commands/weekly-review.md" // commande — unité FICHIER
// Point d'entrée que OpenCode charge pour enregistrer les outils du plugin. Une
// 4e unité oubliée, et du même défaut que les 3 ci-dessus : un point d'entrée
// périmé côté cible ne casse pas les 5 autres unités, il casse le plugin
// entier — en silence, verdict vert. Chemin distinct de PLUGIN_REL (fichier vs
// répertoire) : les deux sont gardés.
const ENTRYPOINT_REL = ".opencode/plugins/weekly-advisor.ts" // point d'entrée — unité FICHIER

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

// Préfixe volontairement HORS de « weekly- » : les suites voisines filtrent ce
// préfixe pour vérifier leurs propres effets sur le tmpdir, et un nom partagé
// rendrait leurs assertions dépendantes de ce fichier (et réciproquement).
const created = []

// ponytail: 18 sites d'appel, donc le nettoyage vit dans l.unique fabrique — pas
// 18 `t.after` à maintenir. `exit` suffit : ce sont des fichiers temporaires.
process.on("exit", () => {
  for (const dir of created) fs.rmSync(dir, { recursive: true, force: true })
})

function tmpdir() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "wa-sync-drift-"))
  created.push(dir)
  return dir
}

/** Unité à.upload : chemin RELATIF à la racine cible, comme dans la sortie des scripts. */
function esc(rel) {
  return rel.replace(/[.*+?^${}()|[\]\\/]/g, "\\$&")
}

/**
 * En-tête d'unité `── <rel>` attendu dans la sortie. `\s` (et non `\b`) après le
 * chemin : un `\b` matcherait aussi `.opencode/plugins/weekly-advisor` dans
 * l'en-tête du moteur `…weekly-advisor-engine`, et le test passerait à vide.
 */
function unitHeader(rel) {
  return new RegExp(`── ${esc(rel)}\\s`)
}

// Les 4 unités ajoutées après coup. `file` : chemin relatif à la racine cible du
// fichier déjà présent (servant à supprimer / modifier). `msg` : motif attendu
// sur la ligne de dérive — les lignes de dérive sont relatives à l'UNITÉ (le
// chemin complet de l'unité est dans l'en-tête), sauf pour l'unité fichier dont
// le chemin relatif EST l'unité.
const NEW_UNITS = [
  {
    kind: "dir",
    rel: PLUGIN_REL,
    file: `${PLUGIN_REL}/tool-registry.ts`,
    msg: "tool-registry\\.ts",
  },
  {
    kind: "dir",
    rel: AGENTS_REL,
    file: `${AGENTS_REL}/weekly-advisor-worker.md`,
    msg: "weekly-advisor-worker\\.md",
  },
  { kind: "file", rel: COMMAND_REL, file: COMMAND_REL, msg: esc(COMMAND_REL), isUnitFile: true },
  {
    kind: "file",
    rel: ENTRYPOINT_REL,
    file: ENTRYPOINT_REL,
    msg: esc(ENTRYPOINT_REL),
    isUnitFile: true,
  },
]

/** Les unités `f` du périmètre : chemin relatif à la racine (parent = répertoire). */
const FILE_UNITS = [COMMAND_REL, ENTRYPOINT_REL]

// Filtre d'exclusion sur le NOM DE BASE, partagé par buildTarget et buildFakeKit
// (les deux doivent produire la même arborescence, sinon un kit et sa cible ne
// sont pas comparables).
const EXCLUDED = EXCLUDES.map((pattern) =>
  pattern.includes("*") ? new RegExp(`^${pattern.replaceAll("*", ".*")}$`) : pattern,
)
const keep = (src) => {
  const name = path.basename(src)
  return !EXCLUDED.some((pattern) =>
    typeof pattern === "string" ? name === pattern : pattern.test(name),
  )
}

/** Répertoires weekly* du kit réel — lus dans l'arborescence, jamais codés en dur. */
function weeklySkillUnits() {
  return fs
    .readdirSync(path.join(ROOT, ".opencode", "skills"), { withFileTypes: true })
    .filter((e) => e.isDirectory() && e.name.startsWith("weekly"))
    .map((e) => `.opencode/skills/${e.name}`)
}

/**
 * Copie l'arborescence du kit vers une racine cible, avec les exclusions des
 * scripts. Sans rsync ni tar : `fs.cpSync` natif, filtre sur le nom de base
 * (une exclusion de répertoire saute tout son contenu).
 */
function buildTarget(target) {
  for (const rel of [ENGINE_REL, PLUGIN_REL, AGENTS_REL, ...weeklySkillUnits()]) {
    const dst = path.join(target, rel)
    fs.mkdirSync(dst, { recursive: true })
    fs.cpSync(path.join(ROOT, rel), dst, { recursive: true, filter: keep })
  }
  // Unités fichier : le parent est un répertoire, la cible est le fichier. La
  // liste est itérée (pas un cas unique codé en dur) : buildTarget omettrait
  // silencieusement une unité `f` et la sandbox ne serait jamais alignée, donc
  // tous les tripwires passeraient à vide.
  for (const rel of FILE_UNITS) {
    const dst = path.join(target, rel)
    fs.mkdirSync(path.dirname(dst), { recursive: true })
    fs.cpSync(path.join(ROOT, rel), dst)
  }
  return target
}

/**
 * KIT FACTICE : miroir de ROOT (les 2 scripts + les 6 unités, mêmes exclusions)
 * dans une racine temporaire. Les scripts tirent leur racine de BASH_SOURCE,
 * donc un kit copié se comporte comme un kit indépendant.
 *
 * Nécessaire pour tripwirer le sens « DRIFT en trop » d'une unité `f` : la
 * branche `[ ! -f "$src" ]` se déclenche quand le KIT n'a plus le fichier —
 * impossible à produire honnêtement dans l'arbre réel, où l'écrire serait une
 * corruption. Le kit factice est le seul moyen de l'exercer.
 */
function buildFakeKit(kitRoot) {
  for (const rel of ["scripts/check-drift.sh", "scripts/sync-to-target.sh"]) {
    const dst = path.join(kitRoot, rel)
    fs.mkdirSync(path.dirname(dst), { recursive: true })
    fs.cpSync(path.join(ROOT, rel), dst)
  }
  buildTarget(kitRoot)
  return kitRoot
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

test("check-drift: commande supprimée côté cible = exit 1 (garde-fou anti-faux-vert)", (t) => {
  // TRIPWIRE. Une unité fichier absente d'un côté ne doit JAMAIS produire un
  // verdict vert : une garde qui ne compare rien quand le fichier manque est
  // pire que pas de garde — elle certifie un alignement inexistant. C'est
  // exactement le défaut qui a produit « OK : kit et cible alignés » sur trois
  // fichiers périmés.
  t.diagnostic("unité à un seul fichier : la suppression doit rester visible")
  const base = tmpdir()
  t.after(() => fs.rmSync(base, { recursive: true, force: true }))
  const target = buildTarget(path.join(base, "cible"))
  assert.equal(run(DRIFT, ["--target", target]).status, 0, "pré-condition : sandbox alignée")

  fs.rmSync(path.join(target, COMMAND_REL))

  const drifted = run(DRIFT, ["--target", target])
  assert.equal(drifted.status, 1, `la commande supprimée doit être une dérive :\n${drifted.output}`)
  // Le message nomme l'unité ET dit quoi faire.
  assert.match(drifted.output, new RegExp(`DRIFT (absent|manquant)\\s+:\\s+${esc(COMMAND_REL)}`))
  assert.match(drifted.output, /sync-to-target\.sh --apply/)

  // Le plan doit exposer le transfert, pas seulement constater.
  const plan = run(SYNC, ["--dry-run", "--target", target])
  assert.equal(plan.status, 0, plan.output)
  assert.match(plan.output, /\+ weekly-review\.md/)
})

for (const unit of NEW_UNITS) {
  test(`check-drift: fichier supprimé dans ${unit.rel} = exit 1`, (t) => {
    t.diagnostic(
      unit.isUnitFile
        ? "unité fichier : supprimer le fichier, c'est supprimer l'unité"
        : "fichier retiré de la cible alors qu'il existe dans le kit",
    )
    const base = tmpdir()
    t.after(() => fs.rmSync(base, { recursive: true, force: true }))
    const target = buildTarget(path.join(base, "cible"))
    assert.equal(run(DRIFT, ["--target", target]).status, 0, "pré-condition : sandbox alignée")

    fs.rmSync(path.join(target, unit.file))

    const drifted = run(DRIFT, ["--target", target])
    assert.equal(drifted.status, 1, drifted.output)
    // L'unité est nommée (en-tête) ET le fichier manquant est nommé (ligne de
    // dérive) : « exit 1 » seul ne prouverait pas que le bon fichier est vu.
    assert.match(drifted.output, unitHeader(unit.rel), drifted.output)
    assert.match(
      drifted.output,
      new RegExp(`DRIFT (absent|manquant)\\s+:\\s+${unit.msg}`),
      `la sortie doit nommer le fichier manquant :\n${drifted.output}`,
    )
    assert.match(drifted.output, /sync-to-target\.sh --apply/)
  })

  test(`check-drift: contenu modifié dans ${unit.rel} = exit 1`, (t) => {
    const base = tmpdir()
    t.after(() => fs.rmSync(base, { recursive: true, force: true }))
    const target = buildTarget(path.join(base, "cible"))
    fs.appendFileSync(path.join(target, unit.file), "\n# dérive\n")

    const drifted = run(DRIFT, ["--target", target])
    assert.equal(drifted.status, 1, drifted.output)
    assert.match(drifted.output, unitHeader(unit.rel), drifted.output)
    assert.match(
      drifted.output,
      new RegExp(`DRIFT différent\\s+:\\s+${unit.msg}`),
      drifted.output,
    )
  })
}

test("check-drift: point d'entrée absent du kit mais présent en cible = DRIFT en trop, exit 1", (t) => {
  // TRIPWIRE, 3e sens. Unité `f` retirée du kit alors que la cible la garde :
  // le cas « la cible traîne un fichier que le kit ne connaît plus ». Les
  // deux autres sens sont couverts par les boucles ci-dessus ; sans celui-ci,
  // une branche `f` qui ne traiterait que « absent » et « différent » passerait
  // verte. Kit factice obligatoire : retirer le fichier de l'arbre réel serait
  // une corruption de la source de vérité.
  t.diagnostic("le point d'entrée est le FICHIER que OpenCode charge — pas une ligne optionnelle")
  const base = tmpdir()
  t.after(() => fs.rmSync(base, { recursive: true, force: true }))
  const kit = buildFakeKit(path.join(base, "kit"))
  const target = buildTarget(path.join(base, "cible"))
  const fakeDrift = path.join(kit, "scripts", "check-drift.sh")
  const fakeSync = path.join(kit, "scripts", "sync-to-target.sh")

  // Pré-condition : le kit factice est aligné avec sa cible. Sans ce garde-fou,
  // le exit 1 final serait atribuable à n'importe quoi.
  const aligned = run(fakeDrift, ["--target", target])
  assert.equal(aligned.status, 0, `pré-condition : kit factice aligné\n${aligned.output}`)

  fs.rmSync(path.join(kit, ENTRYPOINT_REL))

  const drifted = run(fakeDrift, ["--target", target])
  assert.equal(drifted.status, 1, `un point d'entrée orphelin doit être une dérive :\n${drifted.output}`)
  assert.match(drifted.output, unitHeader(ENTRYPOINT_REL), drifted.output)
  // Le motif exige le suffixe `.ts` : l'unité DIRECTORY voisine (PLUGIN_REL)
  // produit la même ligne « DRIFT en trop » sans le `.ts`, donc cette assertion
  // ne peut pas être satisfaite par la mauvaise unité.
  assert.match(
    drifted.output,
    new RegExp(`DRIFT en trop\\s+:\\s+${esc(ENTRYPOINT_REL)}`),
    `la sortie doit nommer le point d'entrée en trop :\n${drifted.output}`,
  )

  // Le plan doit exposer la suppression, pas seulement constater.
  const plan = run(fakeSync, ["--dry-run", "--target", target])
  assert.equal(plan.status, 0, plan.output)
  assert.match(plan.output, new RegExp(`DEL ${esc(ENTRYPOINT_REL)} \\(fichier absent du kit\\)`), plan.output)

  // Retour au vert une fois le point d'entrée restauré côté kit.
  fs.cpSync(path.join(ROOT, ENTRYPOINT_REL), path.join(kit, ENTRYPOINT_REL))
  const restored = run(fakeDrift, ["--target", target])
  assert.equal(restored.status, 0, `retour à l'alignement attendu :\n${restored.output}`)
  assert.match(restored.output, /OK : kit et cible alignés/)
})

test("check-drift: le point d'entrée d'origine (non factice) reste aligné sur les 6 unités", (t) => {
  // ANTI-VACUITÉ. Les tripwires passent tous par buildFakeKit ; si la 6e unité
  // était déclarée avec un chemin faux, le kit factice serait construit sur le
  // même chemin faux et resterait aligné avec lui — vert pour rien. Ce test
  // vérifie que le chemin déclaré existe bien dans le kit RÉEL.
  t.diagnostic("le chemin de l'unité 6 doit exister dans l'arbre réel, pas seulement dans la sandbox")
  assert.ok(
    fs.existsSync(path.join(ROOT, ENTRYPOINT_REL)),
    `${ENTRYPOINT_REL} n'existe pas dans le kit réel`,
  )
  const base = tmpdir()
  t.after(() => fs.rmSync(base, { recursive: true, force: true }))
  const target = buildTarget(path.join(base, "cible"))
  const result = run(DRIFT, ["--target", target])
  assert.equal(result.status, 0, `sandbox alignée sur les 6 unités :\n${result.output}`)
  // Les 6 unités doivent être annoncées, pas seulement 5.
  for (const rel of [ENGINE_REL, PLUGIN_REL, AGENTS_REL, COMMAND_REL, ENTRYPOINT_REL]) {
    assert.match(result.output, unitHeader(rel), `unité non annoncée : ${rel}\n${result.output}`)
  }
})

for (const unit of NEW_UNITS.filter((u) => !u.isUnitFile)) {
  test(`check-drift: fichier ajouté sous ${unit.rel} côté cible = exit 1`, (t) => {
    const base = tmpdir()
    t.after(() => fs.rmSync(base, { recursive: true, force: true }))
    const target = buildTarget(path.join(base, "cible"))
    fs.writeFileSync(path.join(target, unit.rel, "parasite-ajoute.ts"), "export const x = 1\n")

    const drifted = run(DRIFT, ["--target", target])
    assert.equal(drifted.status, 1, drifted.output)
    assert.match(drifted.output, unitHeader(unit.rel), drifted.output)
    assert.match(drifted.output, new RegExp(`DRIFT en trop\\s+:\\s+parasite-ajoute\\.ts`), drifted.output)

    fs.rmSync(path.join(target, unit.rel, "parasite-ajoute.ts"))
    assert.equal(run(DRIFT, ["--target", target]).status, 0, "retour à l'alignement")
  })
}

test("check-drift: la récursion couvre les sous-répertoires du plugin TS", (t) => {
  t.diagnostic("le plugin TS a un sous-répertoire adapters/ : un find non récursif le raterait")
  const base = tmpdir()
  t.after(() => fs.rmSync(base, { recursive: true, force: true }))
  const target = buildTarget(path.join(base, "cible"))
  fs.rmSync(path.join(target, PLUGIN_REL, "adapters", "v2.ts"))

  const drifted = run(DRIFT, ["--target", target])
  assert.equal(drifted.status, 1, drifted.output)
  assert.match(drifted.output, unitHeader(PLUGIN_REL), drifted.output)
  assert.match(
    drifted.output,
    /DRIFT (absent|manquant)\s+:\s+adapters\/v2\.ts/,
    `un fichier imbriqué manquant doit être vu :\n${drifted.output}`,
  )
})

test("les exclusions correspondent au NOM DE BASE, pas au chemin relatif ancré", (t) => {
  // RÉGRESSION déjà payée une fois dans ce repo : un motif ancré `^dist$`
  // testé contre `dist/weekly_advisor-0.4.1-py3-none-any.whl` ne matche pas, et
  // la sync a pollué la cible. Les motifs doivent donc être lus sur le NOM DE
  // BASE à n'importe quelle profondeur (ici `find -name`, pas un ancrage sur le
  // chemin relatif). Les entrées ci-dessous sont imbriquées : une exclusion
  // ancrée ne les verrait pas.
  t.diagnostic("bruit exclu présent côté CIBLE SEULEMENT, à des profondeurs variables")
  const base = tmpdir()
  t.after(() => fs.rmSync(base, { recursive: true, force: true }))
  const target = buildTarget(path.join(base, "cible"))

  // Bruit UNIQUE au côté cible : rien n'est écrit dans le kit (ce test doit
  // pouvoir tourner en parallèle des autres fichiers de suite sans laisser
  // d'artefact dans l'arborescence réelle). Si l'exclusion cessait de
  // fonctionner, chaque entrée deviendrait visible — `DEL` au plan, `DRIFT en
  // trop` au verdict — et les assertions ci-dessous échoueraient.
  const noise = [
    // Moteur : unité gardée depuis le début, ce cas ne dépend pas des 3 nouvelles.
    { unit: ENGINE_REL, in: "dist/wa-exclusion-probe.whl" },
    // Plugin TS : dépendances installées en local + sortie de build, au premier
    // niveau et imbriquées (`node_modules/vendor/nested/deep.js` est exactement
    // la chaîne qu'un motif ancré `^node_modules$` ne matche pas).
    { unit: PLUGIN_REL, in: "node_modules/vendored/index.js" },
    { unit: PLUGIN_REL, in: "node_modules/vendored/nested/deep.js" },
    { unit: PLUGIN_REL, in: "dist/weekly_advisor-0.4.1-py3-none-any.whl" },
    // Agents.
    { unit: AGENTS_REL, in: "node_modules/pkg/readme.md" },
  ]
  for (const entry of noise) {
    const dst = path.join(target, entry.unit, entry.in)
    fs.mkdirSync(path.dirname(dst), { recursive: true })
    fs.writeFileSync(dst, "bruit exclu\n")
  }

  const plan = run(SYNC, ["--dry-run", "--target", target])
  assert.equal(plan.status, 0, plan.output)
  for (const entry of noise) {
    // Pré-condition anti-vacuité : le fichier est RÉELLEMENT côté cible. Sans
    // ceci, une régression de buildTarget pourrait rendre le test vert pour rien.
    assert.ok(fs.existsSync(path.join(target, entry.unit, entry.in)), `bruit absent : ${entry.in}`)
    // `entry.in` et non le chemin complet : les lignes de plan et de dérive sont
    // relatives à l'UNITÉ (le chemin complet est dans l'en-tête `── …`). Un
    // `includes()` sur le chemin complet ne trouverait jamais rien et l'assertion
    // passerait dans le vide — c'est-à-dire ne testerait rien.
    assert.ok(
      !plan.output.includes(entry.in),
      `le plan ne doit jamais mentionner un chemin exclu :\n${plan.output}`,
    )
  }

  const verdict = run(DRIFT, ["--target", target])
  assert.equal(
    verdict.status,
    0,
    `du bruit exclu ne doit pas créer de fausse dérive :\n${verdict.output}`,
  )
  for (const entry of noise) {
    assert.ok(!verdict.output.includes(entry.in), `sortie polluée :\n${verdict.output}`)
  }
  // Et le kit n'a pas été pollué par le test lui-même.
  for (const entry of noise) {
    assert.ok(!fs.existsSync(path.join(ROOT, entry.unit, entry.in)), `le kit a été écrit : ${entry.in}`)
  }
})

test("le plan couvre les 6 unités du périmètre cron", (t) => {
  // Un garde qui vérifie 2 unités et en documente 5 (ou l'inverse) est le même
  // défaut que celui corrigé ici : le périmètre doit être visible dans le plan.
  const base = tmpdir()
  t.after(() => fs.rmSync(base, { recursive: true, force: true }))
  const target = path.join(base, "cible")
  fs.mkdirSync(target, { recursive: true })

  const result = run(SYNC, ["--dry-run", "--target", target])
  assert.equal(result.status, 0, result.output)
  for (const rel of [ENGINE_REL, PLUGIN_REL, AGENTS_REL, COMMAND_REL, ENTRYPOINT_REL]) {
    assert.match(result.output, unitHeader(rel), `le plan doit exposer l'unité ${rel} :\n${result.output}`)
  }
  // Les unités fichier doivent apparaître comme des fichiers, pas comme des
  // répertoires vides.
  assert.match(result.output, /\+ weekly-review\.md/)
  assert.match(result.output, /\+ weekly-advisor\.ts/)
})

test("les deux scripts déclarent les mêmes unités (mêmes constantes, même fonction)", () => {
  const sync = fs.readFileSync(SYNC, "utf8")
  const drift = fs.readFileSync(DRIFT, "utf8")
  const relOf = (src, name) => src.match(new RegExp(`^${name}="([^"]+)"`, "m"))?.[1]
  for (const constName of [
    "ENGINE_REL",
    "PLUGIN_REL",
    "AGENTS_REL",
    "COMMAND_REL",
    "ENTRYPOINT_REL",
    "SKILLS_REL",
  ]) {
    assert.ok(relOf(sync, constName), `${constName} absent de sync-to-target.sh`)
    assert.equal(
      relOf(drift, constName),
      relOf(sync, constName),
      `${constName} divergent : périmètre différent entre sync et drift`,
    )
  }
  // Corps de unit_lines() normalisé (commentaires et blancs retirés) : c'est
  // la fonction qui décide quelles unités existent et lesquelles sont des
  // fichiers. Un corps différent = deux périmètres de fait.
  const bodyOf = (src) =>
    src
      .match(/unit_lines\(\) \{[\s\S]*?\n\}/)?.[0]
      .split("\n")
      .map((line) => line.trim())
      .filter((line) => line && !line.startsWith("#"))
      .join("\n")
  assert.ok(bodyOf(sync).length > 0, "unit_lines() introuvable dans sync-to-target.sh")
  assert.equal(bodyOf(drift), bodyOf(sync), "les deux scripts doivent partager unit_lines()")
  // Chaque unité fichier doit être déclarée comme `f`, sinon les helpers
  // directory-shaped la comparent à vide et le verdict redevient vert.
  for (const constName of ["COMMAND_REL", "ENTRYPOINT_REL"]) {
    assert.match(
      bodyOf(sync),
      new RegExp(`printf 'f\\\\t%s\\\\n' "\\$\\{?${constName}`),
      `${constName} doit être déclaré comme unité FICHIER (\`f\`) dans unit_lines()`,
    )
  }
  // Anti-récidive : une unité déclarée mais jamais listée, ou listée deux fois,
  // déporterait le périmètre. 6 lignes fixes = moteur, skills, plugin, agents,
  // commande, point d'entrée (le « + les skills » est la boucle, pas une ligne).
  assert.equal(
    bodyOf(sync).split("\n").filter((line) => line.includes("printf '")).length,
    6,
    "unit_lines() doit émettre 6 lignes fixes (moteur, skills, plugin, agents, commande, point d'entrée)",
  )
  assert.ok(
    !bodyOf(sync).includes(ENTRYPOINT_REL),
    `unit_lines() doit passer par \${ENTRYPOINT_REL}, pas par la valeur en dur`,
  )
})

test("INSTALL.md §2.10 documente les 6 unités réellement vérifiées", () => {
  // Une garde qui documente 2 unités pendant qu'en vérifie 5 est exactement le
  // défaut corrigé ici, déplacé dans la documentation.
  const install = fs.readFileSync(path.join(ROOT, "INSTALL.md"), "utf8")
  const start = install.indexOf("### 2.10")
  assert.notEqual(start, -1, "section §2.10 introuvable dans INSTALL.md")
  const rest = install.slice(start + "### 2.10".length)
  const end = rest.search(/^#{2,3} /m)
  const section = end === -1 ? rest : rest.slice(0, end)
  for (const rel of [ENGINE_REL, PLUGIN_REL, AGENTS_REL, COMMAND_REL, ENTRYPOINT_REL]) {
    assert.ok(section.includes(rel), `§2.10 ne documente pas ${rel} :\n${section}`)
  }
  assert.ok(section.includes(".opencode/skills/weekly*/"), "§2.10 ne documente plus les skills weekly*")
  // `ENTRYPOINT_REL` contient `PLUGIN_REL` en préfixe : sans cette assertion,
  // documenter uniquement « .opencode/plugins/weekly-advisor.ts » satisferait
  // aussi le contrôle de PLUGIN_REL, et la table pourrait perdre sa ligne
  // répertoire sans qu'aucun test ne le voie.
  assert.ok(
    section.includes("| `.opencode/plugins/weekly-advisor` |"),
    "§2.10 ne distingue plus le répertoire plugin de son point d'entrée .ts",
  )
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
