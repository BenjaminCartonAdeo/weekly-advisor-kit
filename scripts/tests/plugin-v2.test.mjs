// Contrat structurel de l'adaptateur V2 de weekly-advisor
// (`.opencode/plugins/weekly-advisor/adapters/v2.ts`).
//
// Ce test fige la couture V2 telle que l'hôte la voit : ce que `setup()`
// enregistre (un hook de prompt, 21 outils), avec quelles formes (schéma JSON
// d'entrée fermé, `{content}`, `ctx.location.project` réduit structurellement),
// et ce qu'il ne fait pas (aucune commande en doublon, aucun import du SDK V1,
// aucun log de prompt, aucun accès disque pendant `transform`).
//
// Le contexte V2 est **factice et structurel** : aucun SDK V2 n'est en
// dépendance, donc rien à installer ni à braquer. Le test n'observe que la
// surface documentée — `session.hook`, `tool.transform`, `tool.list` — et
// n'affirme rien que l'hôte ne pourrait pas garantir lui-même.
//
// Deux sources de vérité croisées :
//   - la fixture gelée `weekly-advisor-tool-contract.json` pour les noms, l'ordre,
//     les descriptions, les champs et les enums (source indépendante du registre,
//     donc une dérive du registre ne peut pas passer inaperçue) ;
//   - le kit factice de `helpers/fake-kit.mjs` pour l'exécution réelle (argv
//     émis, annulation, nettoyage du staging).
import assert from "node:assert/strict"
import fs from "node:fs"
import os from "node:os"
import path from "node:path"
import test from "node:test"
import { fileURLToPath } from "node:url"
import { register } from "node:module"

import { makeCapturingKit, makeKit, useKitEnv } from "./helpers/fake-kit.mjs"
import {
  CLI_COMMANDS,
  REPORT_ARTIFACT_CONTRACT,
  TOOL_BY_NAME,
  TOOL_REGISTRY,
} from "../../.opencode/plugins/weekly-advisor/tool-registry.ts"

// ---------------------------------------------------------------------------
// Détecteur de chargement du SDK V1.
//
// `@opencode-ai/plugin` n'est pas installé (opencode le fournit au chargement
// réel). Plutôt qu'un stub silencieux — qui ne prouverait rien — on braque le
// specifier sur un module-sentinel qui **compte ses chargements** dans un global
// du realm de test. `__weeklyV1Loads === 0` prouve alors que le chemin V2 ne
// charge pas le SDK V1, et le test « contraste » prouve que le détecteur
// fonctionne (un détecteur toujours à zéro ne prouverait rien).
// ---------------------------------------------------------------------------
const V1_SENTINEL = `data:text/javascript,${encodeURIComponent(
  "globalThis.__weeklyV1Loads = (globalThis.__weeklyV1Loads ?? 0) + 1;" +
    // Builders chaînables : le contraste appelle réellement `server()`, donc le
    // chemin V1 doit s'exécuter jusqu'au bout (le stub ne compte pas, il triche).
    "const builder = () => ({ optional() { return this }, describe() { return this } });" +
    "export const tool = (definition) => definition;" +
    "tool.schema = { string: builder, boolean: builder, number: builder, enum: builder };",
)}`
const V1_HOOK = `data:text/javascript,${encodeURIComponent(
  `const SENTINEL = ${JSON.stringify(V1_SENTINEL)};
   export function resolve(specifier, context, nextResolve) {
     return specifier === "@opencode-ai/plugin"
       ? { url: SENTINEL, shortCircuit: true }
       : nextResolve(specifier, context);
   }`,
)}`
register(V1_HOOK, import.meta.url)

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..")
const V2_ADAPTER = path.join(ROOT, ".opencode", "plugins", "weekly-advisor", "adapters", "v2.ts")
const ENTRYPOINT = path.join(ROOT, ".opencode", "plugins", "weekly-advisor.ts")
const CONTRACT = JSON.parse(
  fs.readFileSync(path.join(ROOT, "scripts", "fixtures", "weekly-advisor-tool-contract.json"), "utf8"),
)
const EXPECTED = CONTRACT.tools

const adapter = await import(V2_ADAPTER)
const plugin = await import(ENTRYPOINT)

// ---------------------------------------------------------------------------
// Faux contexte V2 : enregistre ce que `setup()` déclare, rejoue les callbacks.
// ---------------------------------------------------------------------------

/**
 * Contexte V2 minimal mais complet, avec espions.
 * @param {object} [location] `ctx.location` (défaut : racine du kit de test)
 * @returns {{ctx: object, hook: Function, transforms: Function[], runs: object[][], commands: unknown[]}}
 */
function makeV2Context(location = { directory: ROOT }) {
  const hooks = new Map()
  const transforms = []
  const runs = []
  const commands = []
  const ctx = {
    location,
    session: {
      hook(name, callback) {
        hooks.set(name, callback)
      },
    },
    tool: {
      transform(callback) {
        transforms.push(callback)
        const added = []
        runs.push(added)
        callback({
          add(tool) {
            added.push(tool)
          },
        })
      },
      async list() {
        return runs.flatMap((added) => added.map((tool) => tool.name))
      },
    },
    // Espion de commande : V1 enregistre déjà `weekly-review`, l'adaptateur V2 ne
    // doit donc rien enregistrer de ce côté (un doublon serait un conflit hôte).
    command: {
      register(...args) {
        commands.push(args)
      },
    },
  }
  return { ctx, hooks, transforms, runs, commands }
}

/** Enregistre le kit et rend un contexte V2 déjà passé par `setup()`. */
async function setupWith(kit, location = { directory: kit.root }) {
  const fake = makeV2Context(location)
  await adapter.setup(fake.ctx)
  return { ...fake, tools: fake.runs[0] ?? [], byName: new Map(fake.runs[0]?.map((t) => [t.name, t])) }
}

/** Lignes de capture utiles (préambule `-m … --config …` retiré). */
function argvLines(kit) {
  if (!fs.existsSync(kit.capture)) return []
  return fs
    .readFileSync(kit.capture, "utf8")
    .split("\n")
    .filter(
      (line) =>
        (line.startsWith("--") || line.startsWith("ARG=")) &&
        line !== "ARG=weekly_telemetry_aggregator" &&
        !line.endsWith("weekly-telemetry-config.json"),
    )
}

/** Répertoires de staging résiduels dans le tmpdir.
 *
 *  Préfixe restreint à ceux que le MOTEUR produit (`weekly-harness-*`, via
 *  `main.py` et `harness_remediation.py`). Un balayage large `weekly-*` rendait
 *  cette assertion pseudosatisfaisante et fausse : `node --test` exécute les
 *  fichiers de test en PARALLÈLE et `plugin-preflight.test.mjs` crée
 *  `weekly-coherence-*`/`weekly-catalog-*`/`weekly-usage-*`. L'assertion
 *  échouait donc selon ce qui tournait à côté — un test qui dépend de l'état
 *  global de la machine n'est pas une détection de fuite, c'est un faux positif.
 *  ponytail: la fuite est mesurée par delta avant/après, pas par absence globale.
 */
function stagedLeftovers() {
  return fs
    .readdirSync(os.tmpdir())
    .filter((name) => name.startsWith("weekly-harness-"))
    .sort()
}

/** Schéma `input` attendu pour un outil de la fixture. */
function expectedSchema(tool) {
  const properties = {}
  const required = []
  for (const field of tool.fields) {
    properties[field.name] = {
      type: { string: "string", boolean: "boolean", number: "number", enum: "string" }[field.kind],
      description: field.description,
      ...(field.kind === "enum" ? { enum: [...field.values] } : {}),
    }
    if (field.required) required.push(field.name)
  }
  return { type: "object", properties, required, additionalProperties: false }
}

/** Exécute `fn` en comptant les appels `fs` — un transformeur pur n'en fait aucun. */
function countFsCalls(fn) {
  const methods = [
    "readFileSync",
    "writeFileSync",
    "existsSync",
    "statSync",
    "readdirSync",
    "mkdirSync",
    "mkdtempSync",
    "readFile",
    "writeFile",
  ]
  const saved = methods.map((name) => [name, fs[name]])
  const calls = []
  for (const [name, original] of saved) {
    fs[name] = (...args) => {
      calls.push(name)
      return original(...args)
    }
  }
  try {
    return { calls, result: fn() }
  } finally {
    for (const [name, original] of saved) fs[name] = original
  }
}

// ---------------------------------------------------------------------------
// Point d'entrée et chargement de SDK
// ---------------------------------------------------------------------------

test("point d'entrée : un seul export par défaut, id et setup conformes", () => {
  assert.deepEqual(Object.keys(plugin), ["default"])
  assert.equal(plugin.default.id, "weekly-advisor")
  assert.equal(typeof plugin.default.setup, "function")
  assert.equal(typeof adapter.setup, "function")
})

test("V2 : setup() n'enregistre aucune commande (pas de doublon avec V1)", async () => {
  const kit = makeKit()
  const restore = useKitEnv(kit)
  try {
    const fake = makeV2Context()
    await plugin.default.setup(fake.ctx)
    assert.deepEqual(fake.commands, [])
  } finally {
    restore()
    fs.rmSync(kit.root, { recursive: true, force: true })
  }
})

test("V2 : setup() ne charge pas le SDK V1 (détecteur vérifié par contraste)", async () => {
  const kit = makeKit()
  const restore = useKitEnv(kit)
  const before = globalThis.__weeklyV1Loads ?? 0
  try {
    const fake = makeV2Context()
    await plugin.default.setup(fake.ctx)
    assert.equal(globalThis.__weeklyV1Loads ?? 0, before, "setup() V2 a chargé @opencode-ai/plugin")

    // Contraste : le chemin V1, lui, charge bien le SDK. Sans ce test, un
    // détecteur cassé (toujours à zéro) passerait le test précédent.
    await plugin.default.server({ worktree: kit.root, directory: kit.root })
    assert.equal(
      globalThis.__weeklyV1Loads ?? 0,
      before + 1,
      "le détecteur de chargement V1 ne fonctionne pas",
    )
  } finally {
    restore()
    fs.rmSync(kit.root, { recursive: true, force: true })
  }
})

test("V2 : source sans import statique du SDK V1 ni de l'adaptateur V1", () => {
  const source = fs.readFileSync(V2_ADAPTER, "utf8")
  const imports = [...source.matchAll(/^\s*(?:import|export)[^;]*?from\s+["']([^"']+)["']/gm)].map(
    (match) => match[1],
  )
  assert.ok(imports.length >= 3, `imports inattendus : ${imports.join(", ")}`)
  for (const specifier of imports) {
    assert.ok(
      !specifier.includes("@opencode"),
      `import de SDK dans l'adaptateur V2 : ${specifier}`,
    )
    assert.ok(
      !specifier.endsWith("v1.ts"),
      `l'adaptateur V2 ne doit pas importer l'adaptateur V1 : ${specifier}`,
    )
  }
  assert.equal(source.includes("@opencode-ai/plugin"), false)
})

test("V2 : capabilities absentes → erreur nommée, pas de TypeError", async () => {
  await assert.rejects(() => adapter.setup({}), /contexte V2 sans session\.hook/)
  await assert.rejects(
    () => adapter.setup({ session: { hook() {} } }),
    /contexte V2 sans tool\.transform/,
  )
})

// ---------------------------------------------------------------------------
// Garde de pré-flight : filtre strict sur prompt.agents
// ---------------------------------------------------------------------------

/** Racine sans moteur : le pré-flight y répond `rc=3`. */
function brokenKit() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "wa-brokenkit-"))
  return { root, python: path.join(root, "absent-python") }
}

test("V2 : prompt de l'agent weekly-advisor → pré-flight passé (rc=0)", async () => {
  const kit = makeKit()
  const restore = useKitEnv(kit)
  try {
    const fake = await setupWith(kit)
    const callback = fake.hooks.get("prompt")
    assert.equal(typeof callback, "function")
    assert.equal(fake.hooks.size, 1, "un seul hook enregistré")
    await callback({ prompt: { agents: ["weekly-advisor"], text: "lance le run" } })
  } finally {
    restore()
    fs.rmSync(kit.root, { recursive: true, force: true })
  }
})

test("V2 : prompt sans l'agent weekly-advisor → pré-flight jamais appelé", async () => {
  const kit = brokenKit()
  const restore = useKitEnv(kit)
  try {
    const fake = await setupWith(kit)
    const callback = fake.hooks.get("prompt")
    // Kit cassé : si la garde se déclenchait, chaque cas ci-dessous rejetterait.
    await callback({ prompt: { agents: ["weekly-advisor-engine", "autre"], text: "weekly-advisor" } })
    await callback({ prompt: { text: "peux-tu lancer weekly-advisor ?" } })
    await callback({ prompt: { skills: ["weekly-advisor"], files: ["/tmp/a.md"] } })
    await callback({ prompt: {} })
    await callback({})
  } finally {
    restore()
    fs.rmSync(kit.root, { recursive: true, force: true })
  }
})

test("V2 : pré-flight invalide → rejet fail-closed au message gelé", async () => {
  const kit = brokenKit()
  const restore = useKitEnv(kit)
  try {
    const fake = await setupWith(kit)
    const callback = fake.hooks.get("prompt")
    await assert.rejects(
      () => callback({ prompt: { agents: ["weekly-advisor"] } }),
      (error) => {
        assert.ok(
          error.message.startsWith("weekly_preflight rc=3 — "),
          `message gelé attendu, obtenu : ${error.message}`,
        )
        assert.ok(error.message.includes(`worktree=${kit.root}`), error.message)
        return true
      },
    )
  } finally {
    restore()
    fs.rmSync(kit.root, { recursive: true, force: true })
  }
})

test("V2 : le hook de prompt ne journalise ni le prompt ni ses fichiers", async () => {
  const kit = brokenKit()
  const restore = useKitEnv(kit)
  const marker = "SECRET-PROMPT-9yv6id"
  const methods = ["log", "info", "warn", "error", "debug", "trace"]
  const recorded = []
  const saved = methods.map((name) => [name, console[name]])
  for (const name of methods) console[name] = (...args) => recorded.push(args.join(" "))
  try {
    const fake = await setupWith(kit)
    const callback = fake.hooks.get("prompt")
    let message = ""
    try {
      await callback({
        prompt: { agents: ["weekly-advisor"], text: marker, files: [`/tmp/${marker}.md`] },
      })
    } catch (error) {
      message = error.message
    }
    // Le rejet du pré-flight est attendu (kit cassé) : c'est aussi l'occasion de
    // vérifier que le message d'échec ne recopie ni le prompt ni ses fichiers.
    assert.ok(message.startsWith("weekly_preflight rc=3 — "), message)
    for (const line of recorded) {
      assert.equal(line.includes(marker), false, `prompt journalisé : ${line}`)
    }
    assert.equal(message.includes(marker), false, `prompt recopié dans l'erreur : ${message}`)
  } finally {
    for (const [name, original] of saved) console[name] = original
    restore()
    fs.rmSync(kit.root, { recursive: true, force: true })
  }
})

// ---------------------------------------------------------------------------
// Enregistrement des 21 outils
// ---------------------------------------------------------------------------

test("V2 : 21 outils enregistrés, noms et ordre conformes à la fixture", async () => {
  const kit = makeKit()
  const restore = useKitEnv(kit)
  try {
    const fake = await setupWith(kit)
    assert.equal(EXPECTED.length, 21, "la fixture doit figer 21 outils")
    assert.equal(fake.tools.length, 21)
    assert.deepEqual(fake.tools.map((tool) => tool.name), EXPECTED.map((tool) => tool.name))
    assert.deepEqual(await fake.ctx.tool.list(), EXPECTED.map((tool) => tool.name))
    assert.deepEqual(fake.transforms.length, 1, "un seul transform")
    assert.deepEqual(fake.commands, [])
  } finally {
    restore()
    fs.rmSync(kit.root, { recursive: true, force: true })
  }
})

test("V2 : schéma JSON d'entrée fermé et exact pour les 21 outils", async () => {
  const kit = makeKit()
  const restore = useKitEnv(kit)
  try {
    const { byName } = await setupWith(kit)
    for (const expected of EXPECTED) {
      const tool = byName.get(expected.name)
      assert.ok(tool, `${expected.name} : non enregistré`)
      assert.equal(tool.description, expected.description, `${expected.name} : description`)
      assert.deepEqual(tool.input, expectedSchema(expected), `${expected.name} : input`)
      assert.equal(tool.input.additionalProperties, false, `${expected.name} : objet non fermé`)
      assert.equal(tool.input.type, "object", `${expected.name} : type`)
      assert.ok(Array.isArray(tool.input.required), `${expected.name} : required absent`)
      // L'ordre d'insertion des propriétés suit l'ordre des champs du contrat.
      assert.deepEqual(
        Object.keys(tool.input.properties),
        expected.fields.map((field) => field.name),
        `${expected.name} : ordre des propriétés`,
      )
    }
  } finally {
    restore()
    fs.rmSync(kit.root, { recursive: true, force: true })
  }
})

test("V2 : weekly_preflight est sans champ (required vide) et weekly_harness_remediate expose l'enum", async () => {
  const kit = makeKit()
  const restore = useKitEnv(kit)
  try {
    const { byName } = await setupWith(kit)
    assert.deepEqual(byName.get("weekly_preflight").input, {
      type: "object",
      properties: {},
      required: [],
      additionalProperties: false,
    })
    const mode = byName.get("weekly_harness_remediate").input.properties.mode
    assert.deepEqual(mode, {
      type: "string",
      description: "dry-run par défaut ; apply uniquement après toutes les gates",
      enum: ["dry-run", "apply"],
    })
    const required = byName.get("weekly_show_session").input.required
    assert.deepEqual(required, ["session_id"], "un seul champ obligatoire, ordre figé")
  } finally {
    restore()
    fs.rmSync(kit.root, { recursive: true, force: true })
  }
})

// ---------------------------------------------------------------------------
// Exécution : {content}, mapping CLI, annulation
// ---------------------------------------------------------------------------

test("V2 : execute est async et rend exactement {content: string}", async () => {
  const kit = makeKit()
  const restore = useKitEnv(kit)
  try {
    const { byName } = await setupWith(kit)
    const pending = byName.get("weekly_preflight").execute({}, {})
    assert.equal(typeof pending.then, "function", "execute doit renvoyer une promesse")
    const result = await pending
    assert.deepEqual(Object.keys(result), ["content"])
    assert.equal(typeof result.content, "string")
    const verdict = JSON.parse(result.content)
    assert.equal(verdict.rc, 0, "un kit factice valide doit passer le pré-flight")
    assert.equal(verdict.worktree, kit.root)
  } finally {
    restore()
    fs.rmSync(kit.root, { recursive: true, force: true })
  }
})

test("V2 : erreurs du handler remontent, jamais converties en succès", async () => {
  const kit = makeCapturingKit()
  const restore = useKitEnv(kit)
  try {
    const { byName } = await setupWith(kit)
    await assert.rejects(
      () => byName.get("weekly_show_session").execute({}, {}),
      /champ obligatoire manquant/,
    )
  } finally {
    restore()
    fs.rmSync(kit.root, { recursive: true, force: true })
  }
})

test("V2 : le mapping CLI de la fixture est préservé (outil ancré et non ancré)", async () => {
  const kit = makeCapturingKit()
  const restore = useKitEnv(kit)
  try {
    const { byName } = await setupWith(kit)
    await byName
      .get("weekly_harness")
      .execute({ anchor: "2026-01-02T03:04:05Z" }, {})
    const anchored = argvLines(kit)
    assert.equal(anchored[0], "ARG=harness")
    assert.ok(anchored.includes("--anchor=2026-01-02T03:04:05Z"), anchored.join(" | "))

    await byName.get("weekly_doctor").execute({}, {})
    const unanchored = argvLines(kit)
    assert.equal(unanchored[0], "ARG=doctor")
    assert.equal(
      unanchored.some((line) => line.startsWith("--anchor")),
      false,
      `outil non ancré : ${unanchored.join(" | ")}`,
    )
  } finally {
    restore()
    fs.rmSync(kit.root, { recursive: true, force: true })
  }
})

test("V2 : context.signal est transmis au runtime (annulation de runCli)", async () => {
  const kit = makeCapturingKit()
  const restore = useKitEnv(kit)
  try {
    const { byName } = await setupWith(kit)
    const controller = new AbortController()
    controller.abort()
    await assert.rejects(
      () => byName.get("weekly_harness").execute({ anchor: "2026-01-02T03:04:05Z" }, { signal: controller.signal }),
      /annulé \(AbortSignal\)/,
    )
    assert.deepEqual(argvLines(kit), [], "aucun subprocess ne doit être lancé après annulation")
  } finally {
    restore()
    fs.rmSync(kit.root, { recursive: true, force: true })
  }
})

test("V2 : annulation du staging — aucun répertoire résiduel", async () => {
  const kit = makeCapturingKit()
  const restore = useKitEnv(kit)
  try {
    const { byName } = await setupWith(kit)
    const before = stagedLeftovers()
    const controller = new AbortController()
    controller.abort()
    await assert.rejects(
      () =>
        byName.get("weekly_skill_curate").execute(
          { coherence: "{}", catalog: "{}", usage: "[]", anchor: "2026-01-02T03:04:05Z" },
          { signal: controller.signal },
        ),
      /staging annulé \(AbortSignal\)/,
    )
    assert.deepEqual(stagedLeftovers(), before, "annulation : staging résiduel")
  } finally {
    restore()
    fs.rmSync(kit.root, { recursive: true, force: true })
  }
})

test("V2 : sur succès, le staging est nettoyé après exécution", async () => {
  const kit = makeCapturingKit()
  const restore = useKitEnv(kit)
  try {
    const { byName } = await setupWith(kit)
    const before = stagedLeftovers()
    const result = await byName.get("weekly_skill_curate").execute(
      {
        coherence: '{"tag_action":["archive"]}',
        catalog: '{"skills":[]}',
        usage: "[]",
        anchor: "2026-01-02T03:04:05Z",
      },
      {},
    )
    assert.equal(result.content, "ok")
    const staged = argvLines(kit)
      .filter((line) => line.startsWith("--coherence="))
      .map((line) => line.slice("--coherence=".length))
    assert.equal(staged.length, 1, "le payload doit voyager par fichier temporaire")
    assert.equal(fs.existsSync(staged[0]), false, "fichier de staging résiduel après le run")
    assert.deepEqual(stagedLeftovers(), before, "répertoire de staging résiduel après le run")
  } finally {
    restore()
    fs.rmSync(kit.root, { recursive: true, force: true })
  }
})

// ---------------------------------------------------------------------------
// transform : synchrone, rejouable, sans effet de bord
// ---------------------------------------------------------------------------

test("V2 : le callback de transform est synchrone, rejouable et sans effet de bord", async () => {
  const kit = makeCapturingKit()
  const restore = useKitEnv(kit)
  try {
    const fake = await setupWith(kit)
    const transform = fake.transforms[0]
    const first = fake.tools
    const stagedBefore = stagedLeftovers()

    for (let replay = 0; replay < 3; replay += 1) {
      const added = []
      const { calls, result } = countFsCalls(() => {
        const returned = transform({
          add(tool) {
            added.push(tool)
          },
        })
        assert.equal(returned, undefined, "le callback de transform ne doit rien renvoyer")
        assert.equal(
          returned?.then,
          undefined,
          "le callback de transform doit être synchrone (pas de promesse)",
        )
        return returned
      })
      assert.deepEqual(calls, [], `rejeu ${replay} : le transform touche le disque`)
      assert.deepEqual(added.map((tool) => tool.name), EXPECTED.map((tool) => tool.name))
      for (const [index, tool] of added.entries()) {
        assert.equal(tool.description, first[index].description)
        assert.deepEqual(tool.input, first[index].input)
      }
      // Objets neufs à chaque rejeu : aucun état partagé entre deux exécutions.
      assert.notEqual(added[0], first[0], "les outils rejoués doivent être des objets neufs")
    }

    assert.deepEqual(argvLines(kit), [], "aucun appel CLI pendant les rejeux")
    assert.deepEqual(stagedLeftovers(), stagedBefore, "aucun staging pendant les rejeux")
  } finally {
    restore()
    fs.rmSync(kit.root, { recursive: true, force: true })
  }
})

// ---------------------------------------------------------------------------
// Isolation des racines
// ---------------------------------------------------------------------------

test("V2 : deux setup() → deux runtimes, deux racines, aucun état partagé", async () => {
  const kitA = makeKit()
  const kitB = makeKit()
  try {
    const restoreA = useKitEnv(kitA)
    const a = await setupWith(kitA)
    restoreA()

    const restoreB = useKitEnv(kitB)
    const b = await setupWith(kitB)
    restoreB()

    // Les deux jeux d'outils restent boundés à leur racine, alors que
    // l'environnement a changé entre les deux enregistrements.
    const worktreeA = JSON.parse((await a.byName.get("weekly_preflight").execute({}, {})).content)
    const worktreeB = JSON.parse((await b.byName.get("weekly_preflight").execute({}, {})).content)
    assert.equal(worktreeA.worktree, kitA.root)
    assert.equal(worktreeB.worktree, kitB.root)
    assert.notEqual(a.tools[0], b.tools[0], "les jeux d'outils ne doivent pas être partagés")
  } finally {
    fs.rmSync(kitA.root, { recursive: true, force: true })
    fs.rmSync(kitB.root, { recursive: true, force: true })
  }
})

test("V2 : métadonnées projet en objet — canonical prioritaire, directory en repli", () => {
  const canonical = path.join(os.tmpdir(), "wa-projet-canonique")
  const directory = path.join(os.tmpdir(), "wa-projet-repertoire")

  // Forme réelle de l'hôte V2 : un **objet** `{canonical?, directory?}`. Une
  // chaîne serait ignorée : le repli ne fonctionne qu'avec la réduction
  // structurelle.
  assert.equal(adapter.projectDirectory({ canonical }), canonical)
  assert.equal(adapter.projectDirectory({ directory }), directory)
  assert.equal(
    adapter.projectDirectory({ canonical, directory }),
    canonical,
    "canonical doit primer sur directory",
  )

  // Rien d'exploitable → aucun candidat, donc repli ultérieur sur cwd.
  assert.equal(adapter.projectDirectory({}), undefined)
  assert.equal(adapter.projectDirectory({ canonical: "", directory: "" }), undefined)
  assert.equal(adapter.projectDirectory({ canonical: 42, directory: null }), undefined)

  // Projections non-objet : ignorées sans lever — un hôte exotique ne doit pas
  // empêcher l'enregistrement des 21 outils.
  for (const projection of [undefined, null, "/tmp/une-chaine", ["/tmp/liste"], 7]) {
    assert.equal(
      adapter.projectDirectory(projection),
      undefined,
      `projection non-objet acceptée : ${String(projection)}`,
    )
  }
})

test("V2 : le point d'entrée prime sur un ctx.location et un projet V2 étrangers", async () => {
  const foreign = brokenKit()
  const saved = process.env.WEEKLY_KIT_ROOT
  delete process.env.WEEKLY_KIT_ROOT
  try {
    // Les trois formes de métadonnées projet, dont les deux réelles : le point
    // d'entrée (prioritaire) doit gagner dans tous les cas, donc le projet ne
    // peut pas détourner l'hôte V2 vers un kit différent.
    for (const project of [{ canonical: foreign.root }, { directory: foreign.root }, foreign.root]) {
      const fake = makeV2Context({ directory: foreign.root, project })
      await adapter.setup(fake.ctx)
      const tool = fake.runs[0].find((entry) => entry.name === "weekly_preflight")
      const verdict = JSON.parse((await tool.execute({}, {})).content)
      assert.equal(verdict.worktree, ROOT, "un répertoire projet étranger ne doit pas détourner la racine")
    }
  } finally {
    if (saved === undefined) delete process.env.WEEKLY_KIT_ROOT
    else process.env.WEEKLY_KIT_ROOT = saved
    fs.rmSync(foreign.root, { recursive: true, force: true })
  }
})

// ---------------------------------------------------------------------------
// Parité des fragments de description partagés
//
// Les descriptions d'outils ne sont pas de la documentation : c'est le premier
// canal par lequel un agent découvre les contrats du moteur. Le run cron du
// 2026-10-01 a payé 10 lectures de `report.py` parce que les règles de section 4
// et la sémantique des rc n'étaient portées par aucune description.
//
// Corollaire de conception : un fragment partagé (constante) doit apparaître
// VERBATIM dans chaque description qui l'utilise. Un test de parité le verrouille,
// sinon le mécanisme se dégrade en deux copies divergentes — exactement le défaut
// qu'il prétend corriger.
// ---------------------------------------------------------------------------

const REGISTRY_FILE = path.join(ROOT, ".opencode", "plugins", "weekly-advisor", "tool-registry.ts")
const ENGINE_REPORT = path.join(
  ROOT,
  ".opencode",
  "plugins",
  "weekly-advisor-engine",
  "weekly_telemetry_aggregator",
  "report.py",
)
const REGISTRY_SRC = fs.readFileSync(REGISTRY_FILE, "utf8")

/** Extrait la valeur d'une constante `const X = <expr>` du registre. */
function registryConst(name) {
  const match = REGISTRY_SRC.match(new RegExp(`const ${name} =\\s*([\\s\\S]*?)\\n\\n`, "m"))
  assert.ok(match, `constante ${name} introuvable dans tool-registry.ts`)
  return match[1]
}

/** Concatène les littéraux d'une constante de description TS (lignes `"…" +`). */
function descriptionLiteral(name) {
  const expr = registryConst(name)
  const parts = [...expr.matchAll(/"((?:[^"\\]|\\.)*)"/g)].map((m) => m[1])
  assert.ok(parts.length > 0, `constante ${name} : aucun littéral de chaîne`)
  return parts
    .join("")
    .replace(/\\`/g, "`")
    .replace(/\\"/g, '"')
    .replace(/\\\\/g, "\\")
}

test("parité : chaque fragment partagé apparaît verbatim dans ≥ 2 descriptions", () => {
  const descriptions = new Map(EXPECTED.map((tool) => [tool.name, tool.description]))
  const SHARED = {
    DRAFT_CONSUMED_NOTE: ["weekly_report_assemble", "weekly_report_contract"],
    SECTION_4_FILES: ["weekly_report_blocks_draft", "weekly_report_contract"],
    SECTION_4_FILE_CONTRACT: ["weekly_report_blocks_draft", "weekly_report_assemble", "weekly_report_contract"],
    REPORT_RC_SEMANTICS: ["weekly_report_prep", "weekly_report_blocks_draft", "weekly_report_contract"],
  }
  for (const [name, owners] of Object.entries(SHARED)) {
    assert.ok(owners.length >= 2, `${name} : un fragment « partagé » doit servir ≥ 2 descriptions`)
    const literal = descriptionLiteral(name)
    assert.ok(literal.length > 0, `${name} : fragment vide`)
    for (const owner of owners) {
      const description = descriptions.get(owner)
      assert.ok(description, `${owner} : outil absent de la fixture`)
      assert.ok(
        description.includes(literal),
        `${name} absent verbatim de la description de ${owner} — le fragment et sa copie divergent`,
      )
    }
  }
})

test("parité : les fragments de section 4 énoncent les seuils vérifiés par le moteur", () => {
  const contract = descriptionLiteral("SECTION_4_FILE_CONTRACT")
  // `config.py:190` — blocks_min_words
  assert.ok(contract.includes("40"), "le seuil de 40 mots doit rester dans le fragment")
  assert.ok(contract.includes("60"), "le plafond de 60 lignes doit rester dans le fragment")
  assert.ok(contract.includes("[F:"), "le tag de finding doit rester dans le fragment")
  assert.ok(contract.includes("[A:"), "le tag d'alerte doit rester dans le fragment")
  assert.ok(contract.includes("[M:"), "le tag de maintenance doit rester dans le fragment")
  assert.ok(contract.includes("high"), "l'obligation de citer les findings high doit rester")
  assert.ok(/jamais un id tronqué/.test(contract), "l'interdiction de tronquer l'id doit rester")
})

test("parité : les rc=1 warn-only de l'assemble sont énumérés, et rc=1 ≠ échec est dit", () => {
  const assemble = descriptionsOf("weekly_report_assemble")
  assert.match(assemble, /rc=1\s*≠\s*échec du pipeline/)
  // Les trois cas documentés dans `_report_assemble_inner` / `_assemble_*`.
  assert.match(assemble, /security/i, "cas warn-only sécurité")
  assert.match(assemble, /JOIN partiel/i, "cas warn-only JOIN partiel")
  assert.match(assemble, /rc=1 propagé/i, "cas warn-only rc propagé")
  assert.match(assemble, /rc≥2\s*=\s*AUCUN rapport/, "rc≥2 doit rester le vrai échec")
  const rcSemantics = descriptionsOf("weekly_report_contract")
  assert.match(rcSemantics, /rc=1\s*≠\s*échec du pipeline/)
  assert.equal(REPORT_ARTIFACT_CONTRACT.assembleWarnOnlyRc1.length, 3)
})

test("parité : weekly_report_contract est read-only strict (in-process, sans champ ni rc de CLI)", () => {
  const tool = TOOL_BY_NAME.get("weekly_report_contract")
  assert.ok(tool, "weekly_report_contract absent du registre")
  assert.equal(tool.cliSubcommand, undefined, "aucune sous-commande : read-only in-process")
  assert.equal(tool.timeoutMs, 0, "timeout nul : rien à annuler")
  assert.deepEqual(tool.fields, [], "aucun champ : rien à valider")
  assert.equal(tool.anchored, false, "l'ancre est irrelevante pour un contrat figé")
  // 19 appels CLI pour 21 outils : les deux nouveaux tools in-process n'en ajoutent aucun.
  assert.equal(CLI_COMMANDS.length, 19)
  const inProc = TOOL_REGISTRY.filter((t) => t.cliSubcommand === undefined).map((t) => t.name)
  assert.deepEqual(inProc, ["weekly_preflight", "weekly_report_contract"])
})

test("weekly_report_contract : le handler rend le contrat en JSON, sans effet de bord", async () => {
  const kit = makeKit()
  const restore = useKitEnv(kit)
  try {
    const fake = await setupWith(kit)
    const tool = fake.tools.find((t) => t.name === "weekly_report_contract")
    assert.ok(tool, "weekly_report_contract non enregistré")
    const before = fs.readdirSync(kit.root, { recursive: true }).sort()
    const payload = JSON.parse((await tool.execute({}, {})).content)
    assert.deepEqual(payload, JSON.parse(JSON.stringify(REPORT_ARTIFACT_CONTRACT)))
    const after = fs.readdirSync(kit.root, { recursive: true }).sort()
    assert.deepEqual(after, before, "un tool read-only ne doit rien écrire")
  } finally {
    restore()
    fs.rmSync(kit.root, { recursive: true, force: true })
  }
})

test("parité : REPORT_ARTIFACT_CONTRACT suit les littéraux de report.py", () => {
  const reportSrc = fs.readFileSync(ENGINE_REPORT, "utf8")
  const contracts = reportSrc.match(/_ARTIFACT_CONTRACTS: dict\[str, Callable\[\[Mapping\], bool\]\] = \{([\s\S]*?)\n\}/)
  assert.ok(contracts, "_ARTIFACT_CONTRACTS introuvable dans report.py")
  const pyNames = [...contracts[1].matchAll(/"([a-z][a-z0-9-]*)":/g)].map((m) => m[1])
  assert.deepEqual(
    [...REPORT_ARTIFACT_CONTRACT.artifactContracts].sort(),
    pyNames.sort(),
    "artifactContracts a dérivé de _ARTIFACT_CONTRACTS",
  )
  // `validate_required_artifacts` : la liste `optional` du moteur.
  const optional = reportSrc.match(/"optional": \(([\s\S]*?)\n\s{8}\),/)
  assert.ok(optional, "liste optional de validate_required_artifacts introuvable")
  const pyOptional = [...optional[1].matchAll(/"([a-z][a-z0-9-]*)",/g)].map((m) => m[1])
  assert.deepEqual(
    [...REPORT_ARTIFACT_CONTRACT.validateRequiredArtifacts.optionalNames].sort(),
    pyOptional.sort(),
    "optionalNames a dérivé de validate_required_artifacts",
  )
  // Un nom du moteur absent du tool laisserait l'agent sans gate à découvrir.
  for (const name of [...pyNames, ...pyOptional]) {
    assert.ok(
      REPORT_ARTIFACT_CONTRACT.artifactContracts.includes(name) ||
        REPORT_ARTIFACT_CONTRACT.validateRequiredArtifacts.optionalNames.includes(name),
      `artefact moteur "${name}" absent du contrat exposé`,
    )
  }
})

test("CI : la regex de check-flow-docs surface 4 (draft consommé) reste satisfaite", () => {
  // Copie littérale de la regex de `scripts/check-flow-docs.mjs` surface 4 : la
  // CI échoue si « relancer weekly_report_prep » sort de la fenêtre de 400
  // caractères qui suit la PREMIÈRE mention de `weekly_report_assemble`.
  const gate = /weekly_report_assemble[\s\S]{0,400}?relancer weekly_report_prep/
  assert.ok(gate.test(REGISTRY_SRC), "gate surface 4 (relancer weekly_report_prep) cassée")
  // La fenêtre doit être portée par le TEXTE du fragment partagé, pas par son
  // commentaire ni par une mention éparse plus bas : sinon le prochain refactor
  // du commentaire casse la CI sans qu'aucun test ici ne l'ait vu venir.
  const note = descriptionLiteral("DRAFT_CONSUMED_NOTE")
  const named = note.indexOf("weekly_report_assemble")
  const phrase = note.indexOf("relancer weekly_report_prep")
  assert.ok(named >= 0, "DRAFT_CONSUMED_NOTE ne nomme pas weekly_report_assemble")
  assert.ok(phrase > named, "la phrase doit suivre la mention, pas la précéder")
  assert.ok(phrase - named < 400, "la phrase est hors de la fenêtre de 400 caractères")
})

function descriptionsOf(name) {
  const tool = EXPECTED.find((t) => t.name === name)
  assert.ok(tool, `${name} : outil absent de la fixture`)
  return tool.description
}
