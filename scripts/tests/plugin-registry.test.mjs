// Contrat du registre neutre d'outils (`.opencode/plugins/weekly-advisor/tool-registry.ts`).
//
// Le registre est censé être la source de vérité unique du contrat V1. Ce test
// le confronte donc à la fixture gelée — même confrontation que
// `plugin-tool-contract.test.mjs`, mais côté registre : noms, ordre, descriptions
// (byte-for-byte), schémas de champs, timeouts, ancrage, sous-commandes CLI,
// valeurs par défaut, immuabilité, et comportement observable des handlers
// (argv émis, notes de gate, refus).
//
// Toute divergence doit être validée explicitement : fixture et registre sont
// modifiés dans le même commit que le changement de contrat.
import assert from "node:assert/strict"
import fs from "node:fs"
import os from "node:os"
import path from "node:path"
import test from "node:test"
import { fileURLToPath } from "node:url"

import {
  CLI_COMMANDS,
  TOOL_BY_NAME,
  TOOL_DEFAULTS,
  TOOL_NAMES,
  TOOL_REGISTRY,
} from "../../.opencode/plugins/weekly-advisor/tool-registry.ts"
import { WeeklyRuntime } from "../../.opencode/plugins/weekly-advisor/runtime.ts"
import { makeCapturingKit } from "./helpers/fake-kit.mjs"

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..")
const CONTRACT = JSON.parse(
  fs.readFileSync(path.join(ROOT, "scripts", "fixtures", "weekly-advisor-tool-contract.json"), "utf8"),
)

/** Runtime branché sur un kit factice dont l'interpréteur journalise son argv. */
function runtimeFor(kit) {
  return new WeeklyRuntime({
    location: { v1Worktree: kit.root },
    env: { WEEKLY_PYTHON: kit.python, CAPTURE: kit.capture },
  })
}

/**
 * Lignes de capture utiles : drapeaux, sous-commande et arguments isolés.
 * Le préambule que `runCli` préfixe à chaque appel (`-m
 * weekly_telemetry_aggregator [--config <cfg>]`) est retiré : l'interpréteur
 * factice ne décale que le drapeau, pas sa valeur.
 */
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

/** Drapeaux émis, dans l'ordre. */
function flags(lines) {
  return lines.filter((line) => line.startsWith("--"))
}

/** Valeur d'un drapeau apparié (`--flag=valeur`), ou `undefined`. */
function flagValue(lines, flag) {
  const entry = lines.find((line) => line.startsWith(`${flag}=`))
  return entry === undefined ? undefined : entry.slice(flag.length + 1)
}

/** Exécute un handler du registre contre le kit, et rend l'argv capturé. */
async function runCaptured(name, input) {
  const kit = makeCapturingKit()
  try {
    const tool = TOOL_BY_NAME.get(name)
    assert.ok(tool?.run, `${name} : handler absent`)
    const result = await tool.run(input, runtimeFor(kit))
    return { result, lines: argvLines(kit) }
  } finally {
    fs.rmSync(kit.root, { recursive: true, force: true })
  }
}

// ---------------------------------------------------------------------------
// Forme du registre face à la fixture
// ---------------------------------------------------------------------------

test("registre : 19 outils, ordre et noms gelés", () => {
  assert.equal(TOOL_REGISTRY.length, 19)
  assert.deepEqual(TOOL_NAMES, CONTRACT.tools.map((tool) => tool.name))
})

test("registre : correspondance exacte avec la fixture", () => {
  const projected = TOOL_REGISTRY.map((tool) => ({
    name: tool.name,
    description: tool.description,
    fields: tool.fields.map((field) => ({
      name: field.name,
      kind: field.kind,
      required: field.required,
      description: field.description,
      ...(field.values === undefined ? {} : { values: [...field.values] }),
    })),
    timeoutMs: tool.timeoutMs,
    anchored: tool.anchored,
    cliSubcommand: tool.cliSubcommand ?? null,
  }))
  assert.deepEqual(projected, CONTRACT.tools)
})

test("registre : sous-commandes CLI = celles appelées par les outils, dans l'ordre", () => {
  assert.deepEqual(CLI_COMMANDS, CONTRACT.cliCommands)
  assert.equal(CLI_COMMANDS.length, 18)
})

test("registre : valeurs par défaut = fixture", () => {
  assert.deepEqual(TOOL_DEFAULTS, CONTRACT.defaults)
})

test("registre : index par nom total et sans doublon", () => {
  assert.equal(TOOL_BY_NAME.size, 19)
  for (const tool of TOOL_REGISTRY) {
    assert.equal(TOOL_BY_NAME.get(tool.name), tool)
  }
})

test("registre : tout outil a un handler exécutable", () => {
  for (const tool of TOOL_REGISTRY) {
    assert.equal(typeof tool.run, "function", `${tool.name} : handler manquant`)
    // `timeoutMs === 0` et `cliSubcommand` absent d designating le même
    // outil in-process : une divergence betrayerait un subprocess fantôme.
    assert.equal(
      tool.timeoutMs === 0,
      tool.cliSubcommand === undefined,
      `${tool.name} : timeout nul et cliSubcommand absent doivent coïncider`,
    )
  }
})

test("registre : weekly_preflight est in-process (aucune sous-commande)", () => {
  const preflight = TOOL_BY_NAME.get("weekly_preflight")
  assert.equal(preflight.timeoutMs, 0)
  assert.equal(preflight.cliSubcommand, undefined)
  assert.equal(preflight.anchored, false)
  assert.deepEqual(preflight.fields, [])
})

// ---------------------------------------------------------------------------
// Immuabilité — un registre writable corromprait le contrat pour tous
// ---------------------------------------------------------------------------

test("registre : gelé en profondeur (registre, champs, valeurs, défauts)", () => {
  assert.ok(Object.isFrozen(TOOL_REGISTRY))
  assert.ok(Object.isFrozen(TOOL_NAMES))
  assert.ok(Object.isFrozen(CLI_COMMANDS))
  assert.ok(Object.isFrozen(TOOL_DEFAULTS))
  for (const tool of TOOL_REGISTRY) {
    assert.ok(Object.isFrozen(tool), `${tool.name} : définition gelée`)
    assert.ok(Object.isFrozen(tool.fields), `${tool.name} : champs gelés`)
    for (const field of tool.fields) {
      assert.ok(Object.isFrozen(field), `${tool.name}.${field.name} : champ gelé`)
      if (field.values !== undefined) {
        assert.ok(Object.isFrozen(field.values), `${tool.name}.${field.name} : valeurs gelées`)
      }
    }
  }
})

test("registre : une tentative de mutation échoue", () => {
  // Le module est en mode strict : écrire dans un objet gelé lève, ce qui prouve
  // l'immuabilité réelle et pas seulement le drapeau `Object.isFrozen`.
  assert.throws(() => {
    TOOL_REGISTRY[0] = null
  }, TypeError)
  assert.throws(() => {
    TOOL_BY_NAME.get("weekly_run").description = "pirate"
  }, TypeError)
  assert.throws(() => {
    TOOL_DEFAULTS.lookback_days = 7
  }, TypeError)
})

// ---------------------------------------------------------------------------
// Handlers : argv émis (kit factice qui journalise)
// ---------------------------------------------------------------------------

test("handler : weekly_preflight rend le verdict du runtime, sans subprocess", async () => {
  const { result, lines } = await runCaptured("weekly_preflight", {})
  const verdict = JSON.parse(result)
  assert.equal(verdict.rc, 0)
  assert.equal(verdict.engine_ok, true)
  assert.equal(verdict.python_ok, true)
  assert.equal(verdict.config_ok, true)
  assert.equal(verdict.py_count, 1)
  assert.deepEqual(lines, [])
})

test("handler : outil ancré simple → sous-commande puis --anchor", async () => {
  const { lines } = await runCaptured("weekly_harness", {})
  assert.deepEqual(lines[0], "ARG=harness")
  assert.deepEqual(flags(lines), [`--anchor=${flagValue(lines, "--anchor")}`])
  assert.match(flagValue(lines, "--anchor") ?? "", /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/)
})

test("handler : weekly_run propage lookback_days puis --lookback-days", async () => {
  const { lines } = await runCaptured("weekly_run", { lookback_days: 14 })
  assert.equal(lines[0], "ARG=run")
  assert.deepEqual(flags(lines), [`--anchor=${flagValue(lines, "--anchor")}`, "--lookback-days=14"])
})

test("handler : lookback_days absent ou nul n'émet aucun drapeau (comportement figé)", async () => {
  const absent = await runCaptured("weekly_run", {})
  const zero = await runCaptured("weekly_run", { lookback_days: 0 })
  assert.ok(!flags(absent.lines).some((line) => line.startsWith("--lookback-days")))
  assert.ok(!flags(zero.lines).some((line) => line.startsWith("--lookback-days")))
})

test("handler : ancre explicite évite la lecture du fichier d'ancre", async () => {
  const { lines } = await runCaptured("weekly_harness", { anchor: "2026-01-02T03:04:05Z" })
  assert.equal(flagValue(lines, "--anchor"), "2026-01-02T03:04:05Z")
})

test("handler : outil non ancré n'émet jamais --anchor", async () => {
  const { lines } = await runCaptured("weekly_self_cost", { anchor: "2026-01-02T03:04:05Z" })
  assert.deepEqual(lines, ["ARG=self-cost"])
})

test("handler : weekly_show_session vise le répertoire d'extraits du run actif", async () => {
  const { lines } = await runCaptured("weekly_show_session", { session_id: "ses_abc" })
  assert.equal(lines[0], "ARG=show-session")
  assert.ok(lines.includes("ARG=ses_abc"))
  assert.match(flagValue(lines, "--extract-dir") ?? "", /extracts$/)
  assert.ok(!lines.includes("--include-children"))
})

test("handler : include_children n'est émis que s'il est vrai", async () => {
  const withFlag = await runCaptured("weekly_show_session", {
    session_id: "ses_abc",
    include_children: true,
  })
  const withoutFlag = await runCaptured("weekly_show_session", { session_id: "ses_abc", include_children: false })
  assert.ok(withFlag.lines.includes("--include-children"))
  assert.ok(!withoutFlag.lines.includes("--include-children"))
})

test("handler : weekly_harness_remediate utilise --mode dry-run par défaut", async () => {
  const { lines } = await runCaptured("weekly_harness_remediate", { proposal_file: "/tmp/p.json" })
  assert.equal(flagValue(lines, "ARG"), "harness-remediate")
  assert.equal(flagValue(lines, "--proposal"), "/tmp/p.json")
  assert.equal(flagValue(lines, "--mode"), CONTRACT.defaults["harness_remediate.mode"])
  assert.ok(flagValue(lines, "--anchor"))
})

test("handler : weekly_harness_remediate transmet mode=apply", async () => {
  const { lines } = await runCaptured("weekly_harness_remediate", {
    proposal_file: "/tmp/p.json",
    mode: "apply",
  })
  assert.equal(flagValue(lines, "--mode"), "apply")
})

test("handler : weekly_skill_curate stage les JSON et nettoie après coup", async () => {
  const { lines } = await runCaptured("weekly_skill_curate", {
    coherence: '{"tag_action":["archive"]}',
    catalog: '{"skills":[]}',
    usage: "[]",
    runs_seen: "3",
    stale_days: "45",
    apply: "true",
    anchor: "2026-01-02T03:04:05Z",
  })
  assert.equal(lines[0], "ARG=skill-curate")
  assert.equal(flagValue(lines, "--anchor"), "2026-01-02T03:04:05Z")
  assert.equal(flagValue(lines, "--runs-seen"), "3")
  assert.equal(flagValue(lines, "--stale-days"), "45")
  assert.ok(lines.includes("--apply"), "apply=true doit émettre le drapeau nu")
  for (const flag of ["--coherence", "--catalog", "--usage"]) {
    const staged = flagValue(lines, flag)
    const label = flag.slice(2)
    assert.ok(staged?.endsWith(path.join("input.json")), `${flag} : ${staged}`)
    assert.ok(
      path.basename(path.dirname(staged ?? "")).startsWith(`weekly-${label}-`),
      `${flag} : répertoire temporaire hors prefixe weekly-${label}- : ${staged}`,
    )
    assert.ok(!fs.existsSync(staged), `${flag} : fichier temporaire résiduel après le run`)
  }
})

test("handler : weekly_skill_curate omet les entrées absentes et apply≠true", async () => {
  const { lines } = await runCaptured("weekly_skill_curate", { apply: "false" })
  assert.equal(lines[0], "ARG=skill-curate")
  for (const flag of ["--coherence", "--catalog", "--usage", "--runs-seen", "--stale-days", "--apply"]) {
    assert.equal(flagValue(lines, flag), undefined, `${flag} ne devait pas être émis`)
  }
})

test("handler : un champ obligatoire manquant est refusé, pas transmis muet", async () => {
  const kit = makeCapturingKit()
  try {
    const tool = TOOL_BY_NAME.get("weekly_show_session")
    await assert.rejects(() => tool.run({}, runtimeFor(kit)), /champ obligatoire manquant/)
  } finally {
    fs.rmSync(kit.root, { recursive: true, force: true })
  }
})

// ---------------------------------------------------------------------------
// Gate de portabilité de weekly_commit_draft
// ---------------------------------------------------------------------------

/** Runtime minimal : seul le comportement de la gate est piloté par le test. */
function gateRuntime(outcome) {
  const calls = []
  return {
    calls,
    api: {
      runPortabilityGate: async (file, kind) => {
        calls.push({ file, kind })
        return outcome
      },
      runCli: async (args, timeoutMs) => `cli ${args.join(" ")} @${timeoutMs}`,
    },
  }
}

/** Exécute `weekly_commit_draft` sur un artefact existant, avec une gate pilotée. */
async function runCommitDraft(outcome, input = {}) {
  const file = path.join(fs.mkdtempSync(path.join(os.tmpdir(), "wa-registry-")), "SKILL.md")
  fs.writeFileSync(file, "# draft\n")
  const stub = gateRuntime(outcome)
  try {
    const tool = TOOL_BY_NAME.get("weekly_commit_draft")
    const result = await tool.run({ kind: "skill", file, ...input }, stub.api)
    return { result, calls: stub.calls, file }
  } finally {
    fs.rmSync(path.dirname(file), { recursive: true, force: true })
  }
}

test("handler : commit-draft refuse un artefact bloqué par la gate", async () => {
  await assert.rejects(
    () => runCommitDraft({ kind: "blocked", findings: ["[custom/portability/a] x", "[custom/portability/b] y"] }),
    (error) => {
      assert.ok(error.message.startsWith(CONTRACT.errorMessages.commit_blocked))
      assert.match(error.message, /1\. \[custom\/portability\/a\] x/)
      assert.match(error.message, /2\. \[custom\/portability\/b\] y/)
      return true
    },
  )
})

test("handler : commit-draft refuse une gate inexécutable (jamais de faux vert)", async () => {
  await assert.rejects(
    () => runCommitDraft({ kind: "unusable", reason: "timeout après 60 s" }),
    (error) => {
      assert.ok(error.message.startsWith(CONTRACT.errorMessages.commit_unusable))
      assert.match(error.message, /timeout après 60 s/)
      return true
    },
  )
})

test("handler : commit-draft joint une note aux warnings sans bloquer", async () => {
  const { result, calls, file } = await runCommitDraft({
    kind: "pass",
    warnings: ["[custom/portability/w] un", "[custom/portability/w] deux"],
  })
  const expectedNote = CONTRACT.errorMessages.commit_warnings_note.replace("${gate.warnings.length}", "2")
  const [noteHead, ...warningLines] = result.split("\n\n")[0].split("\n")
  assert.equal(noteHead, expectedNote)
  assert.deepEqual(warningLines, ["  - [custom/portability/w] un", "  - [custom/portability/w] deux"])
  assert.deepEqual(calls, [{ file, kind: "skill" }])
  assert.match(result, /cli commit-draft --kind skill --file /)
})

test("handler : commit-draft note l'ignore fail-soft de la gate", async () => {
  const reason = "binaire harness-eval introuvable"
  const { result } = await runCommitDraft({ kind: "ignored", reason })
  const expectedNote = CONTRACT.errorMessages.commit_ignored_note.replace("${gate.reason}", reason)
  assert.equal(result.split("\n\n")[0], expectedNote)
})

test("handler : commit-draft rend le skip visible pour kind=command", async () => {
  const { result, calls } = await runCommitDraft(
    { kind: "skipped", reason: CONTRACT.errorMessages.gate_skip_reason },
    { kind: "command" },
  )
  const expectedNote = CONTRACT.errorMessages.commit_skipped_note.replace(
    "${gate.reason}",
    CONTRACT.errorMessages.gate_skip_reason,
  )
  assert.equal(result.split("\n\n")[0], expectedNote)
  assert.equal(calls[0].kind, "command")
})

test("handler : commit-draft sans gate ne préfixe aucune note", async () => {
  const kit = makeCapturingKit()
  const missing = path.join(fs.mkdtempSync(path.join(os.tmpdir(), "wa-registry-absent-")), "SKILL.md")
  try {
    const tool = TOOL_BY_NAME.get("weekly_commit_draft")
    const result = await tool.run({ kind: "skill", file: missing }, runtimeFor(kit))
    assert.equal(result, "ok")
    const lines = argvLines(kit)
    assert.equal(flagValue(lines, "--kind"), "skill")
    assert.equal(flagValue(lines, "--file"), missing)
  } finally {
    fs.rmSync(kit.root, { recursive: true, force: true })
    fs.rmSync(path.dirname(missing), { recursive: true, force: true })
  }
})

// ---------------------------------------------------------------------------
// Annulation (`AbortSignal`). Le signal est un paramètre de handler, jamais un
// champ d'outil : il reste donc absent des `fields` et invisible de la fixture.
// Ce qu'il faut prouver, c'est qu'il traverse le handler jusqu'aux primitives
// du runtime — et qu'un staging interrompu ne laisse rien derrière soi.
// ---------------------------------------------------------------------------

/** Répertoire temporaire jetable, avec sa suppression. */
function tempDir(prefix) {
  return fs.mkdtempSync(path.join(os.tmpdir(), prefix))
}

test("handler : l'annulation se propage au staging, et les payloads sont nettoyés", async () => {
  const tmp = tempDir("wa-registry-abort-")
  const staged = []
  const cleaned = []
  const stageCalls = []
  const seen = {}
  const controller = new AbortController()
  const api = {
    anchorArg: () => "2026-01-02T03:04:05Z",
    stageJsonPayload: (payload, label, signal) => {
      stageCalls.push({ label, signal })
      const file = path.join(tmp, `input-${label}.json`)
      fs.writeFileSync(file, payload)
      staged.push({ label, file })
      return {
        file,
        cleanup: () => {
          cleaned.push(label)
          fs.rmSync(file, { force: true })
        },
      }
    },
    runCli: async (args, timeoutMs, signal) => {
      seen.cli = { args, timeoutMs, signal }
      throw new Error("weekly_telemetry_aggregator skill-curate → annulé (AbortSignal)")
    },
  }
  try {
    const tool = TOOL_BY_NAME.get("weekly_skill_curate")
    await assert.rejects(
      () =>
        tool.run(
          { coherence: '{"a":1}', catalog: '{"b":2}', usage: "[]", anchor: "2026-01-02T03:04:05Z" },
          api,
          controller.signal,
        ),
      /annulé \(AbortSignal\)/,
    )
    // 1. le signal traverse le handler et atteint les deux primitives — pour
    //    les trois payloads, pas seulement le premier.
    assert.deepEqual(
      stageCalls.map((call) => call.label),
      ["coherence", "catalog", "usage"],
    )
    for (const call of stageCalls) {
      assert.equal(call.signal, controller.signal, `stageJsonPayload(${call.label}) n'a pas reçu le signal`)
    }
    assert.equal(seen.cli.signal, controller.signal, "runCli n'a pas reçu le signal")
    // 2. l'échec annulé n'empêche pas le nettoyage : les trois payloads restent
    //    le seul point de sortie du `finally`, dans l'ordre de staging.
    assert.deepEqual(cleaned, ["coherence", "catalog", "usage"])
    for (const { file } of staged) {
      assert.ok(!fs.existsSync(file), `résidu après annulation : ${file}`)
    }
  } finally {
    fs.rmSync(tmp, { recursive: true, force: true })
  }
})

test("handler : l'annulation atteint la gate de portabilité, puis le CLI", async () => {
  const tmp = tempDir("wa-registry-abort-")
  const file = path.join(tmp, "SKILL.md")
  fs.writeFileSync(file, "# draft\n")
  const controller = new AbortController()
  const seen = {}
  const api = {
    runPortabilityGate: async (gateFile, kind, signal) => {
      seen.gate = { file: gateFile, kind, signal }
      return { kind: "pass", warnings: [] }
    },
    runCli: async (args, timeoutMs, signal) => {
      seen.cli = { args, timeoutMs, signal }
      return "ok"
    },
  }
  try {
    const tool = TOOL_BY_NAME.get("weekly_commit_draft")
    const result = await tool.run({ kind: "skill", file }, api, controller.signal)
    // La gate et le commit partagent le même signal ; l'ordre
    // gate-puis-commit reste inchangé, seule l'annulation devient observable.
    assert.equal(result, "ok")
    assert.equal(seen.gate.kind, "skill")
    assert.equal(seen.gate.signal, controller.signal, "runPortabilityGate n'a pas reçu le signal")
    assert.equal(seen.cli.signal, controller.signal, "runCli n'a pas reçu le signal")
  } finally {
    fs.rmSync(tmp, { recursive: true, force: true })
  }
})

test("handler : les 18 outils à sous-commande transmettent tous le signal au CLI", async () => {
  const tmp = tempDir("wa-registry-sweep-")
  const draft = path.join(tmp, "SKILL.md")
  fs.writeFileSync(draft, "# draft\n")
  const controller = new AbortController()
  // Entrées minimales valides, lues dans le contrat lui-même : un champ
  // `required` oublié ici ferait échouer le test plutôt que le balayage.
  const REQUIRED = {
    session_id: "ses_sweep",
    proposal_file: path.join(tmp, "proposals.json"),
    kind: "skill",
    file: draft,
  }
  const covered = []
  try {
    for (const tool of TOOL_REGISTRY) {
      if (tool.cliSubcommand === undefined) continue
      let seen
      const api = {
        anchorArg: () => "2026-01-02T03:04:05Z",
        resolveEngine: () => ({ outputDir: tmp }),
        runPortabilityGate: async () => ({ kind: "pass", warnings: [] }),
        runCli: async (args, timeoutMs, signal) => {
          seen = { args, timeoutMs, signal }
          return "ok"
        },
      }
      const input = Object.fromEntries(
        tool.fields.filter((field) => field.required).map((field) => [field.name, REQUIRED[field.name]]),
      )
      assert.equal(await tool.run(input, api, controller.signal), "ok", `${tool.name} : appel en échec`)
      assert.ok(seen, `${tool.name} : runCli non atteint`)
      assert.equal(seen.signal, controller.signal, `${tool.name} : signal non transmis à runCli`)
      // Le balayage re-vérifie au passage argv[0] et timeout de chaque outil.
      assert.equal(seen.args[0], tool.cliSubcommand, `${tool.name} : sous-commande inattendue`)
      assert.equal(seen.timeoutMs, tool.timeoutMs, `${tool.name} : timeout inattendu`)
      covered.push(tool.name)
    }
    assert.equal(covered.length, 18)
  } finally {
    fs.rmSync(tmp, { recursive: true, force: true })
  }
})

test("handler : annulation bout-en-bout contre un vrai WeeklyRuntime", async () => {
  const kit = makeCapturingKit()
  try {
    // `runCli` : signal déjà déclenché ⇒ rejet explicite, aucun subprocess.
    const cliAbort = new AbortController()
    cliAbort.abort()
    await assert.rejects(
      () =>
        TOOL_BY_NAME.get("weekly_harness").run(
          { anchor: "2026-01-02T03:04:05Z" },
          runtimeFor(kit),
          cliAbort.signal,
        ),
      /annulé \(AbortSignal\)/,
    )
    assert.deepEqual(argvLines(kit), [], "un subprocess ne doit pas être lancé après annulation")

    // `stageJsonPayload` : signal déjà déclenché ⇒ rejet avant toute création de
    // répertoire, donc rien à nettoyer.
    const stagingAbort = new AbortController()
    stagingAbort.abort()
    await assert.rejects(
      () =>
        TOOL_BY_NAME.get("weekly_skill_curate").run(
          { coherence: "{}", catalog: "{}", usage: "[]", anchor: "2026-01-02T03:04:05Z" },
          runtimeFor(kit),
          stagingAbort.signal,
        ),
      /staging annulé \(AbortSignal\)/,
    )
  } finally {
    fs.rmSync(kit.root, { recursive: true, force: true })
  }
})

test("handler : le signal reste facultatif (appel sans signal inchangé)", async () => {
  const tmp = tempDir("wa-registry-nosig-")
  const draft = path.join(tmp, "SKILL.md")
  fs.writeFileSync(draft, "# draft\n")
  const seen = []
  const api = {
    anchorArg: () => "2026-01-02T03:04:05Z",
    runPortabilityGate: async (_file, _kind, signal) => {
      seen.push(signal)
      return { kind: "pass", warnings: [] }
    },
    runCli: async (args, _timeoutMs, signal) => {
      seen.push(signal)
      return "ok"
    },
  }
  try {
    // Un adaptateur qui ignore `signal` (V1, ou un double de test) reste valide :
    // c'est ce que garantit le paramètre facultatif, pas une assertion de test.
    const result = await TOOL_BY_NAME.get("weekly_commit_draft").run(
      { kind: "skill", file: draft },
      api,
    )
    assert.equal(result, "ok")
    assert.deepEqual(seen, [undefined, undefined])
  } finally {
    fs.rmSync(tmp, { recursive: true, force: true })
  }
})
