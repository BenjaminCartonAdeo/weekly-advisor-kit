// Contrat structurel de l'adaptateur V2 de weekly-advisor
// (`.opencode/plugins/weekly-advisor/adapters/v2.ts`).
//
// Ce test fige la couture V2 telle que l'hôte la voit : ce que `setup()`
// enregistre (un hook de prompt, 19 outils), avec quelles formes (schéma JSON
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

/** Répertoires de staging résiduels dans le tmpdir. */
function stagedLeftovers() {
  return fs.readdirSync(os.tmpdir()).filter((name) => name.startsWith("weekly-")).sort()
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
// Enregistrement des 19 outils
// ---------------------------------------------------------------------------

test("V2 : 19 outils enregistrés, noms et ordre conformes à la fixture", async () => {
  const kit = makeKit()
  const restore = useKitEnv(kit)
  try {
    const fake = await setupWith(kit)
    assert.equal(EXPECTED.length, 19, "la fixture doit figer 19 outils")
    assert.equal(fake.tools.length, 19)
    assert.deepEqual(fake.tools.map((tool) => tool.name), EXPECTED.map((tool) => tool.name))
    assert.deepEqual(await fake.ctx.tool.list(), EXPECTED.map((tool) => tool.name))
    assert.deepEqual(fake.transforms.length, 1, "un seul transform")
    assert.deepEqual(fake.commands, [])
  } finally {
    restore()
    fs.rmSync(kit.root, { recursive: true, force: true })
  }
})

test("V2 : schéma JSON d'entrée fermé et exact pour les 19 outils", async () => {
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
    assert.deepEqual(stagedLeftovers(), [], "aucun staging pendant les rejeux")
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
  // empêcher l'enregistrement des 19 outils.
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
