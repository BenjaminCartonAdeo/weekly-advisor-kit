#!/usr/bin/env node
/**
 * Contrat de flux docs ↔ code (G1, v6.0.p) — 9 surfaces vérifiées statiquement.
 *
 *  1. Tools TS → sous-commandes CLI : chaque outil du plugin invoque une
 *     sous-commande réelle du moteur (aucun argv fantôme).
 *  2. Sous-commandes CLI → handlers : tous les sous-parsers sont câblés à un
 *     `_cmd_*` défini, et aucun `_cmd_*` défini n'est orphelin.
 *  3. Artefacts : chaque étape produit son artefact daté (littéral
 *     `weekly-<étape>-` présent dans les sources du moteur).
 *  4. Chaînage : les dépendances d'ordre SÉQUENTIEL documentées dans le plugin
 *     TS existent aussi côté moteur (watch-context → ecosystem ; assemble → draft).
 *  5. Comptes de tests : **aucune dérive** entre les comptes que portent les
 *     documents et la collecte `pytest --collect-only` réelle. Les trois états —
 *     aucun littéral (cible) / littéraux égaux au réel / sinon ÉCHEC — sont
 *     bloquants au sens du gate : l'ex-`WARN` advisory a été supprimé (dérive C10 :
 *     les trois documents disaient 171/177/224, puis 1212, puis 2081).
 *  6. Étapes commande ↔ agent : les tokens d'outils (`weekly_*`) du
 *     « Déroulement » de la commande /weekly-review sont EXACTEMENT ceux du
 *     tableau de l'agent weekly-advisor, dans le même ordre (dérive observée :
 *     sémantique d'ancre contradictoire commande/agent, ré-alignée en v6.0.p).
 *  7. Orchestration waves : l'orchestrateur, son worker et la commande lanceuse
 *     documentent le dispatch parallèle de façon cohérente (frontmatter
 *     `task: allow`, marqueurs WAVE/JOIN, contrat retour JSON du worker,
 *     référence agent dans la commande — dérive observée : phrase interdisant
 *     encore le dispatch en subagent côté commande).
 *  8. Envelopes LLM : les exemples JSON embarqués dans les SKILL.md (marqués
 *     `<!-- envelope-example: <id> -->`) respectent le contrat réellement
 *     vérifié par le JOIN (miroir des schémas
 *     `doc/architecture/schemas/<id>.schema.json` — dérive observée : prose
 *     dupliquée qui dérive du validateur moteur).
 *  9. Table branche→skill : la table du worker (§Pre-flight skills) est la
 *     source unique — chaque skill listée existe sous `.opencode/skills/`, et
 *     l'agent orchestrateur pointe vers le worker au lieu de dupliquer la
 *     table (dérive observée : table recopiée agent/worker, divergence
 *     silencieuse).
 *
 * Zéro dépendance (node stdlib). Lancé par la CI après pytest ; exit 0/1.
 *
 * **Migration v6.0.q (Task 6)**
 *
 * check-flow-docs.mjs lit désormais le **registre neutre** 
 * `.opencode/plugins/weekly-advisor/tool-registry.ts` au lieu du fichier 
 * d'entrée `.opencode/plugins/weekly-advisor.ts` (hardcodé, dupliquait le 
 * contrat). La source de vérité est unique : le registre gèle les 21 outils 
 * et les 19 sous-commandes CLI.
 *
 * Surfaces impactées :
 * - Surface 1 : outils TS → sous-commandes CLI, lus depuis le registre
 * - Surface 3 : artefacts (inchangé, source moteur)
 * - Surface 4 : chaînage d'ordre (inchangé, source moteur)
 * - Surface 5 : comptes de tests (inchangé, pytest)
 * - Surface 6 : étapes commande ↔ agent (inchangé, markdown)
 * - Surface 7-9 : doc cohérence (inchangé, markdown)
 *
 * **Split `cli.py` (v6.2, Task 7)**
 *
 * Les surfaces 1 et 2 lisaient le seul `cli.py`. Après le split, la façade ne
 * définit plus que `_cmd_run` / `build_parser` / `main`, les 18 autres handlers
 * vivent dans `cli_core` / `cli_debug` / `cli_report` / `cli_skill_curate` /
 * `cli_watch`, et la table `_SUBCOMMANDS` est RETOURNÉE par
 * `cli_commands.build_subcommands` puis instanciée par la façade.
 *
 * Les deux surfaces pointent donc sur les fichiers qui portent réellement la
 * donnée (`CLI_FILES`, liste explicite et ordonnée). Elles ne suivent PAS les
 * ré-exports de la façade : traquer un `from .cli_core import _cmd_x` se
 * cassera à chaque prochain déplacement, alors que lire le module propriétaire
 * ne casse que si le contrat doit changer — ce qui est le but.
 */
import fs from "node:fs"
import path from "node:path"
import { spawnSync } from "node:child_process"
import { fileURLToPath } from "node:url"

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..")
const REGISTRY_FILE = path.join(ROOT, ".opencode", "plugins", "weekly-advisor", "tool-registry.ts")
const ENGINE = path.join(ROOT, ".opencode", "plugins", "weekly-advisor-engine")
const CLI_DIR = path.join(ENGINE, "weekly_telemetry_aggregator")
const CLI_FACADE_FILE = path.join(CLI_DIR, "cli.py")
const CLI_TABLE_FILE = path.join(CLI_DIR, "cli_commands.py")
// Split `cli.py` (v6.2) : la façade ne définit plus que `_cmd_run`, `build_parser`
// et `main` ; les 18 autres handlers vivent dans les cinq modules `cli_*`, et la
// table `_SUBCOMMANDS` est construite par `cli_commands.build_subcommands`. Le
// contrat doit donc lire les fichiers qui portent RÉELLEMENT la donnée.
// Liste EXPLICITE et ordonnée, jamais un glob : un glob accepterait en silence
// un module laissé hors du contrat, et c'est précisément le silence que ces
// surfaces doivent interdire.
const CLI_FILES = [
  CLI_FACADE_FILE,
  CLI_TABLE_FILE,
  ...["cli_core.py", "cli_debug.py", "cli_report.py", "cli_skill_curate.py", "cli_watch.py"].map((name) =>
    path.join(CLI_DIR, name),
  ),
]
const README_FILE = path.join(ROOT, "README.md")
const INSTALL_FILE = path.join(ROOT, "INSTALL.md")
const CI_FILE = path.join(ROOT, ".github", "workflows", "ci.yml")

let failures = 0
function ok(label, cond, detail = "") {
  if (cond) {
    console.log(`ok  ${label}`)
  } else {
    failures += 1
    console.log(`FAIL ${label}${detail ? ` — ${detail}` : ""}`)
  }
}
const read = (p) => fs.readFileSync(p, "utf8")

/** Execute the collection probe without throwing, preserving all diagnostics. */
export function collectPytest({ cwd, runner = spawnSync } = {}) {
  const result = runner("uv", ["run", "python", "-m", "pytest", "--collect-only", "-q"], {
    cwd,
    encoding: "utf8",
  })
  const stdout = String(result?.stdout ?? "")
  const stderr = String(result?.stderr ?? "")
  const status = result?.status ?? null
  if (result?.error || status !== 0) {
    return { kind: "execution-failure", stdout, stderr, status, error: result?.error ?? null }
  }
  // pytest's quiet collector may emit one ``path: count`` line per module
  // without its usual summary; sum those lines as equivalent parser output.
  const match = stdout.match(/(\d+) tests? collected/)
  const perFile = [...stdout.matchAll(/:\s*(\d+)\s*$/gm)]
  const collected = match ? Number(match[1]) : perFile.length ? perFile.reduce((sum, m) => sum + Number(m[1]), 0) : null
  if (collected === null) return { kind: "parser-failure", stdout, stderr, status, collected: null }
  return { kind: "ok", stdout, stderr, status, collected }
}

/** Build the only supported worker test command; cwd must be the engine root. */
export function canonicalPytestCommand({ cwd, selector }) {
  const engine = path.resolve(ENGINE)
  if (!cwd || path.resolve(cwd) !== engine) throw new Error("pytest cwd must be engine root")
  if (!selector || typeof selector !== "string") throw new Error("pytest selector is required")
  const resolved = path.resolve(engine, selector)
  if (resolved !== engine && !resolved.startsWith(`${engine}${path.sep}`)) {
    throw new Error("pytest selector must stay inside engine root")
  }
  if (!fs.existsSync(resolved)) throw new Error(`pytest selector not found: ${selector}`)
  return ["uv", ["run", "python", "-m", "pytest", "-q", selector]]
}

/**
 * Un compte de tests documenté = UN ENTIER PORTEUR D'UNE UNITÉ DE COMPTE, pas
 * une formulation figée. On matche donc la NATURE (un nombre immédiatement suivi —
 * ou précédé, pour les badges — de l'unité `test`), pas une liste de phrases :
 *
 *   - `2081 tests`, `1219 test`      (entier avant l'unité)
 *   - `1219-test suite`              (unité accolée par un tiret)
 *   - `tests-2081`, `tests_2081`     (badge, entier après l'unité)
 *
 * `(?<![.\d])` refuse un entier qui est le FRAGMENT d'une version : sans lui,
 * « python 3.13 tests » rendrait `13` et le gate comparerait une version à un
 * compte de suite. Symétriquement, un compte n'est retenu qu'au-dessus de
 * `tests?` suivi d'une frontière de mot, donc `testsuite`/`testing` ne matchent pas.
 *
 * Élargir ce motif est sans danger pour le gate : plus de littéraux → plus de
 * comparaisons, jamais l'inverse. Ce qui le rend sûr, c'est que l'état « aucun
 * littéral » reste VERT (cf. `checkTestCounts`) — le motif peut donc agresser.
 */
const TEST_COUNT_RE = /(?<![.\d])(\d{1,7})[-\s]tests?\b|\btests[-_](\d{1,7})\b/g

export function documentedTestCounts(contents) {
  return contents.flatMap((source) =>
    [...source.matchAll(TEST_COUNT_RE)].map((m) => Number(m[1] ?? m[2])),
  )
}

/**
 * Contrat de comptes, TROIS ÉTATS et non deux :
 *
 *  1. aucun littéral dans les docs → OK. Rien à maintenir, donc rien qui peut
 *     dériver. C'est l'ÉTAT CIBLE, pas un cas limite : un `unique.length !== 1`
 *     naïf ferait échouer le tableau vide et casserait la CI sur le dépôt propre.
 *  2. tous les littéraux présents, ÉGAUX entre eux ET ÉGALS au collect réel → OK.
 *  3. sinon → BLOQUANT (l'ex-`WARN`).
 *
 * Les deux premiers états sont mutuellement exclusifs avec le troisième par les
 * gardes `!hasLiterals` : l'absence de littéral ne peut jamais être lue comme un
 * désaccord. `collectAgrees` reste `true` quand la collecte a échoué — le compte
 * réel est alors INCONNU, pas divergent, et la panne de collecte est déjà
 * signalée par son propre contrôle avec son diagnostic ; la compter deux fois
 * ferait doublon sans rien apprendre.
 */
export function checkTestCounts({ docs, collection }) {
  const unique = [...new Set(docs)]
  const hasLiterals = docs.length > 0
  const messages = []
  if (unique.length > 1) messages.push(`documentation counts differ: ${unique.join(", ")}`)
  if (collection.kind === "execution-failure") messages.push("pytest collection execution failed")
  if (collection.kind === "parser-failure") messages.push("pytest collection output could not be parsed")
  const countsAgree = !hasLiterals || unique.length === 1
  // `countsAgree` est une PRÉCONDITION de `collectAgrees` : si les docs
  // divergent entre eux, « le compte documenté » n'est pas défini, et le valider
  // quand même afficherait un vert faux — le premier littéral serait comparé au
  // réel au hasard de l'ordre de lecture des fichiers.
  const collectAgrees =
    !hasLiterals || collection.kind !== "ok" || (countsAgree && unique[0] === collection.collected)
  return {
    blocking: collection.kind !== "ok" || !countsAgree || !collectAgrees,
    messages,
    hasLiterals,
    countsAgree,
    collectAgrees,
  }
}

// ---------------------------------------------------------------- collecteurs

/** Sous-commandes TS : 1er argv littéral (`["run", "--anchor"]…`) ou table déclarative (`subcommand: "run"`). */
export function tsCommands(src) {
  const cmds = new Set()
  // On retire les commentaires `//` (ex: `["default"]` en prose) et les enums
  // de schéma (tool.schema.enum(["dry-run", "apply"]), multilignes possibles)
  // avant d'extraire les littéraux de commande.
  const clean = src
    .split("\n")
    .filter((line) => !line.trim().startsWith("//"))
    .join("\n")
    .replace(/tool\.schema\s*\.\s*enum\(\s*\[[^\]]*\]\)/g, "")
    // Le contrat porte sur les outils TS ↔ NOTRE CLI aggregator. Les spawns
    // d'un binaire EXTERNE (execFile("<binaire>", [...]), ex: la gate de
    // portabilité invoquant `harness-eval skill-verify`) n'en font pas partie :
    // leur argv est retiré avant l'extraction (exclusion par mécanisme, pas par
    // token — un futur binaire externe reste hors contrat automatiquement).
    .replace(/execFile\(\s*"[^"]+"\s*,\s*\[[^\]]*\]/g, "")
  for (const m of clean.matchAll(/\[\s*"([a-z][a-z-]+)"\s*[,\]]/g)) cmds.add(m[1])
  for (const m of clean.matchAll(/subcommand:\s*"([a-z][a-z-]+)"/g)) cmds.add(m[1])
  return cmds
}

/** Commandes CLI du registre neutre: cliSubcommand défini dans ToolDefinition. */
export function registryCLICommands(src) {
  const cmds = new Set()
  // Extraire cliSubcommand: "..." depuis les définitions d'outils
  // (pas de faux positifs d'autres patterns, regarder juste les déclarations de champ)
  const matches = [...src.matchAll(/cliSubcommand\s*:\s*"([a-z][a-z-]*)"/g)]
  for (const m of matches) {
    cmds.add(m[1])
  }
  // Ajouter les subcommands des specs qui sont transformés en cliSubcommand par simpleTool()
  // Seul si le spec est utilisé (pas en commentaire)
  const specMatches = [...src.matchAll(/(?<!\/\/).*?subcommand\s*:\s*"([a-z][a-z-]*)"/g)]
  for (const m of specMatches) {
    // Heuristique : si la ligne contient {, elle est probablement dans un tableau d'objet
    const context = src.substring(Math.max(0, m.index - 50), m.index + 100)
    if (/[{,]/.test(context)) {
      cmds.add(m[1])
    }
  }
  return cmds
}

/**
 * Entrée de la table déclarative : `("nom", "aide", handler, options…)`.
 * Calée sur la FORME de l'entrée, pas sur son indentation (8 espaces avant le
 * split de `cli.py`, 12 dans le tuple retourné) ni sur le nom du paramètre
 * injecté pour `run` — ces deux détails changent au prochain déplacement, la
 * forme d'une entrée de table, non.
 */
const SUB_COMMAND_ENTRY = /\(\s*"([a-z][a-z-]+)",\s*"[^"]*",\s*([A-Za-z_][A-Za-z0-9_]*)\s*,/g

/**
 * Handlers câblés par la table `_SUBCOMMANDS`, sous forme `[nom, handler]`.
 *
 * L'entrée `run` ne porte pas un `_cmd_*` littéral : `build_subcommands` la reçoit
 * en paramètre et la façade l'instancie avec `_cmd_run`. On résout ce paramètre via
 * la signature ET le site d'appel réels — sans ce lien, `run` disparaîtrait du
 * contrat (surface 1) ET `_cmd_run` deviendrait un orphelin (surface 2).
 */
function subCommandEntries(facadeSrc, tableSrc) {
  const param = tableSrc.match(/def\s+build_subcommands\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*:/)?.[1] ?? null
  const injected =
    facadeSrc.match(/_SUBCOMMANDS\s*=\s*build_subcommands\(\s*(_cmd_[A-Za-z0-9_]*)\s*\)/)?.[1] ?? null
  return [...tableSrc.matchAll(SUB_COMMAND_ENTRY)].map(([, name, token]) => [
    name,
    // Un paramètre non résolu reste un identifiant distinct : il ne peut ni égaler
    // un `_cmd_*` défini, ni disparaître du câblage. Le contrat échoue, comme il doit.
    param !== null && token === param ? (injected ?? `<${param} non résolu>`) : token,
  ])
}

/** Contenu des modules CLI, indexés par nom de fichier. */
function cliModuleSources() {
  return Object.fromEntries(CLI_FILES.map((file) => [path.basename(file), read(file)]))
}

/** Sous-commandes CLI (Python) : entrées de la table déclarative, ou sous-parsers explicites. */
function cliCommands(src) {
  const cmds = new Set(subCommandEntries(src["cli.py"], src["cli_commands.py"]).map(([name]) => name))
  // Un sous-parser explicite (`sub.add_parser("x")`) reste un portage de
  // commande. Union : ce chemin ne peut qu'élargir le contrat, jamais l'affaiblir.
  for (const file of CLI_FILES) {
    for (const m of src[path.basename(file)].matchAll(/sub\.add_parser\(\s*"([a-z][a-z-]+)"/g)) {
      cmds.add(m[1])
    }
  }
  return cmds
}

/**
 * Handlers : définis (`def _cmd_*`, sur l'ensemble des modules CLI après le split)
 * et câblés par la table `_SUBCOMMANDS` retournée par `cli_commands.build_subcommands`.
 */
function cliHandlers(src) {
  const defined = new Set()
  for (const file of CLI_FILES) {
    for (const m of src[path.basename(file)].matchAll(/def (_cmd_[a-z_]+)\(/g)) defined.add(m[1])
  }
  const wired = new Set(subCommandEntries(src["cli.py"], src["cli_commands.py"]).map(([, handler]) => handler))
  return { defined, wired }
}

/** Sources Python du moteur (hors caches). */
function enginePythonFiles() {
  const out = []
  const walk = (dir) => {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      if (entry.name.startsWith(".")) continue
      const abs = path.join(dir, entry.name)
      if (entry.isDirectory()) walk(abs)
      else if (entry.name.endsWith(".py")) out.push(abs)
    }
  }
  walk(path.join(ENGINE, "weekly_telemetry_aggregator"))
  return out
}

// ------------------------------------------------------------------ surface 1

const isMain = process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)

if (isMain) {
const registrySrc = read(REGISTRY_FILE)
const cliSrc = cliModuleSources()
const tsCmds = registryCLICommands(registrySrc)
const cliCmds = cliCommands(cliSrc)

console.log("— Surface 1 : médiation registre ↔ CLI")
for (const cmd of [...tsCmds].sort()) {
  ok(`registre → sous-commande "${cmd}" existe`, cliCmds.has(cmd))
}

// Verify registry has exactly 19 CLI commands (never empty scan)
if (tsCmds.size === 0) {
  failures += 1
  console.log("FAIL registre — 19 sous-commandes trouvées — commandes: aucune")
} else if (tsCmds.size !== 19) {
  failures += 1
  console.log(`FAIL registre — 19 sous-commandes trouvées — trouvé: ${tsCmds.size}`)
}

// ------------------------------------------------------------------ surface 2

const { defined, wired } = cliHandlers(cliSrc)
for (const handler of [...wired].sort()) {
  ok(`handler "${handler}" câblé et défini`, defined.has(handler))
}
const orphan = [...defined].filter((h) => !wired.has(h)).sort()
ok(
  "aucun handler _cmd_* orphelin",
  orphan.length === 0,
  orphan.length ? `non câblés: ${orphan.join(", ")}` : "",
)

// ------------------------------------------------------------------ surface 3

// Étape → préfixe d'artefact produit (sources du moteur).
const ARTIFACTS = [
  ["run", "weekly-summary-"],
  ["releases", "weekly-ecosystem-"],
  ["watch-context", "weekly-watch-context-"],
  ["watch-validate", "weekly-watch-findings-"],
  ["insights", "weekly-insights-"],
  ["harness", "weekly-harness-digest-"],
  ["harness-remediate", "weekly-harness-remediation-"],
  ["audit-candidates", "weekly-audit-candidates-"],
  ["draft-candidates", "weekly-draft-candidates-"],
  ["report-prep", "weekly-report-draft-"],
  ["report-assemble", "weekly-report-"],
]
const pyFiles = enginePythonFiles()
console.log("— Surface 3 : artefacts produits par étape")
for (const [cmd, prefix] of ARTIFACTS) {
  ok(
    `"${cmd}" produit ${prefix}<date>.json`,
    hasLiteral(pyFiles, prefix),
    `littéral "${prefix}" absent des sources`,
  )
}
function hasLiteral(files, needle) {
  return files.some((f) => read(f).includes(needle))
}

// ------------------------------------------------------------------ surface 4

// Dépendances d'ordre : documentées dans le registre neutre (description SÉQUENTIEL)
// ET présentes côté moteur (le handler lit l'artefact amont).
const DEPENDENCIES = [
  // weekly_watch_context décrit sa dépendance à l'écosystème (étape 2).
  [
    "registre weekly_watch_context → SÉQUENTIEL weekly-ecosystem",
    /weekly_watch_context[\s\S]{0,400}?weekly-ecosystem/,
    null,
  ],
  // Le handler watch-context lit l'écosystème produit par releases.
  ["moteur watch-context lit weekly-ecosystem (chaînage)", null, "weekly-ecosystem"],
  // weekly_report_assemble documente la consommation du draft.
  [
    "registre weekly_report_assemble → relancer prep après draft",
    /weekly_report_assemble[\s\S]{0,400}?relancer weekly_report_prep/,
    null,
  ],
  ["moteur assemble consomme weekly-report-draft", null, "weekly-report-draft"],
  // Chaînage 7a → 7b.1 → 7c : le check doit être déclaré ENTRE le brouillon et
  // l'assemble, sinon il ne sert à rien — l'assemble consomme le draft et un
  // rejet impose un prep complet. On verrouille l'ORDRE de DÉCLARATION des specs
  // (donc l'ordre d'exposition et l'ordre des sous-commandes), pas la distance
  // entre deux mentions de token : les tool specs sont à 1-2 kB les uns des
  // autres, une fenêtre de 400 caractères ne peut pas les relier. Les deux
  // fenêtres sont calées sur les deltas mesurés (1083 et 1954 caractères) avec
  // une marge explicite, et le même ordre est documenté côté agent et côté
  // commande lanceuse (surface 6).
  [
    "registre : ordre des specs blocks-draft → blocks-check → assemble",
    /name: "weekly_report_blocks_draft"[\s\S]{0,4000}?name: "weekly_report_blocks_check"[\s\S]{0,4000}?name: "weekly_report_assemble"/,
    null,
  ],
  // Le check valide sans consommer : ni le fichier de prose ni le draft ne sont
  // supprimés par la sous-commande (seul l'assemble supprime le draft).
  ["moteur blocks-check ne consomme pas le fichier de prose", null, "fichier NON consommé"],
]
console.log("— Surface 4 : chaînage d'ordre")
for (const [label, regexPattern, pyNeedle] of DEPENDENCIES) {
  if (regexPattern !== null) {
    ok(label, regexPattern.test(registrySrc))
  }
  if (pyNeedle !== null) {
    ok(label, hasLiteral(pyFiles, pyNeedle))
  }
}

// ------------------------------------------------------------------ surface 5

// Comptes de tests (surface 5). Le contrôle porte sur l'absence de DÉRIVE, pas
// sur la présence d'un nombre : les documents n'ont aucun compte à porter (état
// cible, cf. `checkTestCounts`), et s'ils en portent un, il doit être exact.
// Un compte de tests change à chaque test ajouté — un littéral unique serait donc
// un nouveau point de dérive, pas une garantie. C'est pourquoi le badge et les
// deux extraits de commande ne portent aucun nombre : `pytest -q` l'affiche sur
// stdout au moment où l'on s'en sert.
const docsCounts = documentedTestCounts([README_FILE, INSTALL_FILE, CI_FILE].map(read))
const collection = collectPytest({ cwd: ENGINE })
const countCheck = checkTestCounts({ docs: docsCounts, collection })
console.log("— Surface 5 : comptes de tests cohérents")
ok("pytest collection executable and parseable", collection.kind === "ok",
  collection.kind === "execution-failure" ? `status=${collection.status}; stderr=${collection.stderr}` :
    collection.kind === "parser-failure" ? `stdout=${collection.stdout}` : "")
ok(
  "README/INSTALL/ci ne portent aucun compte de tests divergent",
  countCheck.countsAgree,
  countCheck.countsAgree ? "" : `comptes=[${docsCounts.join(", ")}]`,
)
ok(
  "le compte documenté, s'il existe, égale la collecte réelle",
  countCheck.collectAgrees,
  countCheck.collectAgrees ? "" :
    `collect réel=${collection.collected} != docs=${docsCounts.join(", ")}`,
)

// ------------------------------------------------------------------ surface 6

// Étapes commande ↔ agent : les tokens d'outils (weekly_*) du « Déroulement » de
// la commande /weekly-review doivent être EXACTEMENT ceux du tableau de l'agent
// weekly-advisor, dans le même ordre. Empêche la re-divergence des deux
// présentations du flux (dérive observée : sémantique d'ancre contradictoire).
const AGENT_FILE = path.join(ROOT, ".opencode", "agents", "weekly-advisor", "weekly-advisor.md")
const COMMAND_FILE = path.join(ROOT, ".opencode", "commands", "weekly-review.md")
const TOKEN_RE = /weekly_[a-z_]+/g
function sectionTools(src, fromHeader, toHeader) {
  const start = src.indexOf(fromHeader)
  const end = src.indexOf(toHeader, start + 1)
  const section = start === -1 || end === -1 ? "" : src.slice(start, end)
  return [...new Set(section.match(TOKEN_RE) ?? [])]
}
console.log("— Surface 6 : étapes commande ↔ agent (ordre figé)")
const agentTools = sectionTools(read(AGENT_FILE), "| Étape | Action (tool) | Sortie |", "## Invariants")
const commandTools = sectionTools(read(COMMAND_FILE), "## Déroulement", "## Règles")
ok(
  "commande et agent déclarent les mêmes outils d'étapes, dans le même ordre",
  JSON.stringify(agentTools) === JSON.stringify(commandTools),
  `agent: [${agentTools.join(", ")}] | commande: [${commandTools.join(", ")}]`,
)

// ------------------------------------------------------------------ surface 7

// Orchestration waves : le dispatch parallèle (orchestrateur → workers
// subagents) doit être documenté de façon cohérente entre l'agent orchestrateur,
// son worker et la commande lanceuse. Détecte une re-divergence (ex. retour à
// un flux séquentiel décrit, ou phrase interdisant le dispatch en subagent).
const WORKER_FILE = path.join(ROOT, ".opencode", "agents", "weekly-advisor", "weekly-advisor-worker.md")
const agentSrc7 = read(AGENT_FILE)
const commandSrc7 = read(COMMAND_FILE)
const frontmatter = (src) => src.match(/^---\r?\n([\s\S]*?)\r?\n---/)?.[1]?.replace(/\r/g, "") ?? ""
const workerSrc = fs.existsSync(WORKER_FILE) ? read(WORKER_FILE) : ""
console.log("— Surface 7 : cohérence orchestration waves")
ok(
  "agent orchestrateur déclare task: allow dans son frontmatter",
  /\btask:\s*allow\b/.test(frontmatter(agentSrc7)),
)
ok(
  "commande sans phrase interdite « jamais de dispatch en subagent »",
  !commandSrc7.includes("jamais de dispatch en subagent"),
)
for (const marker of ["WAVE 1", "WAVE 2", "JOIN"]) {
  ok(`orchestrateur documente la vague « ${marker} »`, agentSrc7.includes(marker))
}
// `sha` n'est pas un champ d'état de branche comme les autres : c'est le canal par
// lequel la branche D (seule committer) remonte à l'orchestrateur les FAITS de ses
// drafts confirmés. Sans lui, l'enregistrement des commits de draft dans
// `weekly-timings-<date>.json` n'a aucune source — le worker D détient les retours
// `commit-draft` et l'orchestrateur ne voit que son contrat de retour.
const RETURN_FIELDS = ["branch", "rc", "steps_done", "warnings", "artifacts", "elapsed_s", "sha"]
for (const field of RETURN_FIELDS) {
  ok(
    `worker weekly-advisor-worker déclare le champ retour "${field}"`,
    new RegExp(`"${field}"\\s*:`).test(workerSrc),
  )
}
ok(
  "commande référence agent: weekly-advisor dans son frontmatter",
  /agent:\s*weekly-advisor\b/.test(frontmatter(commandSrc7)),
)

// ------------------------------------------------------------------ surface 8

// Envelopes LLM : chaque SKILL.md producteur montre un exemple marqué
// `<!-- envelope-example: <id> -->`. Le check extrait le bloc clôturé qui suit,
// retire les commentaires jsonc (sans casser les `//` dans les chaînes, ex.
// les URLs `https://…`), parse, puis contrôle les clés requises — le même
// contrat que le schéma `doc/architecture/schemas/<id>.schema.json` et le
// validateur moteur (report.py). Zéro dépendance : pas de validateur JSON
// Schema externe, les contrôles sont codés en dur ci-dessous.
const SKILLS_DIR = path.join(ROOT, ".opencode", "skills")
const ENVELOPES = [
  "weekly-quality-audit/audit-findings",
  "weekly-watch-review/weekly-watch-findings-raw",
  "harness-remediation/weekly-harness-remediation-proposals",
]
function stripJsonc(src) {
  let out = ""
  let inStr = false
  let esc = false
  for (let i = 0; i < src.length; i += 1) {
    const c = src[i]
    if (inStr) {
      out += c
      if (esc) esc = false
      else if (c === "\\") esc = true
      else if (c === '"') inStr = false
    } else if (c === '"') {
      inStr = true
      out += c
    } else if (c === "/" && src[i + 1] === "/") {
      while (i < src.length && src[i] !== "\n") i += 1
      out += "\n"
    } else {
      out += c
    }
  }
  return out
}
function envelopeExample(skillSrc, id) {
  const at = skillSrc.indexOf(`<!-- envelope-example: ${id}`)
  if (at === -1) return { found: false }
  const open = skillSrc.indexOf("```", at)
  const openEnd = open === -1 ? -1 : skillSrc.indexOf("\n", open)
  const close = openEnd === -1 ? -1 : skillSrc.indexOf("```", openEnd)
  if (open === -1 || openEnd === -1 || close === -1) return { found: true, block: null }
  return { found: true, block: skillSrc.slice(openEnd + 1, close) }
}
function isRecord(v) {
  return typeof v === "object" && v !== null && !Array.isArray(v)
}
function checkEnvelope(id, value) {
  if (id === "audit-findings") {
    return (
      isRecord(value) &&
      value.schema_version === 1 &&
      typeof value.session_id === "string" &&
      typeof value.summary === "string" &&
      value.summary.trim().length > 0 &&
      Array.isArray(value.findings) &&
      (value.rc === 0 || value.rc === 1) &&
      Array.isArray(value.warnings)
    )
  }
  if (id === "weekly-watch-findings-raw") {
    return (
      isRecord(value) &&
      value.schema_version === 1 &&
      Array.isArray(value.findings) &&
      value.findings.every(
        (f) =>
          isRecord(f) &&
          typeof f.category === "string" &&
          typeof f.severity === "string" &&
          typeof f.token_impact === "string",
      )
    )
  }
  // weekly-harness-remediation-proposals
  const DECISIONS = new Set(["apply", "propose", "manual", "dismiss"])
  return (
    isRecord(value) &&
    (value.schema_version === 1 || value.schema_version === 2) &&
    typeof value.date === "string" &&
    /^\d{4}-\d{2}-\d{2}$/.test(value.date) &&
    Array.isArray(value.proposals) &&
    value.proposals.every(
      (p) =>
        isRecord(p) &&
        typeof p.rule === "string" &&
        typeof p.path === "string" &&
        DECISIONS.has(p.decision),
    )
  )
}
console.log("— Surface 8 : exemples d'envelopes conformes au contrat JOIN")
for (const pair of ENVELOPES) {
  const [skill, id] = pair.split("/")
  const src = read(path.join(SKILLS_DIR, skill, "SKILL.md"))
  const ex = envelopeExample(src, id)
  ok(`SKILL ${skill} marque un exemple d'envelope "${id}"`, ex.found)
  if (!ex.found) continue
  let value = null
  let parsed = false
  if (ex.block !== null) {
    try {
      value = JSON.parse(stripJsonc(ex.block))
      parsed = true
    } catch {
      parsed = false
    }
  }
  ok(`exemple "${id}" parse comme JSON (commentaires jsonc ignorés)`, parsed, parsed ? "" : "bloc illisible")
  if (parsed) ok(`exemple "${id}" respecte le contrat JOIN`, checkEnvelope(id, value))
}

// ------------------------------------------------------------------ surface 9

// Table branche→skill : source unique = worker §Pre-flight skills. Le check
// extrait les noms en backticks des lignes de table, vérifie que chaque skill
// existe sous `.opencode/skills/`, et que l'agent pointe vers le worker sans
// recopier la table.
console.log("— Surface 9 : table branche→skill (source unique worker)")
const preflightAt = workerSrc.indexOf("## Pre-flight skills")
const mappingAt = workerSrc.indexOf("**Mapping rc**", preflightAt === -1 ? 0 : preflightAt)
const tableZone = preflightAt !== -1 && mappingAt !== -1 ? workerSrc.slice(preflightAt, mappingAt) : ""
const tableSkills = [...tableZone.matchAll(/^\|[^|\n]*\|([^|\n]*)\|/gm)]
  .flatMap((m) => [...m[1].matchAll(/`([^`]+)`/g)].map((b) => b[1]))
ok("worker §Pre-flight skills liste ≥1 skill en table", tableSkills.length > 0)
for (const skill of tableSkills) {
  ok(
    `skill de branche "${skill}" existe sous .opencode/skills/`,
    fs.existsSync(path.join(SKILLS_DIR, skill, "SKILL.md")),
  )
}
const agentSrc9 = read(AGENT_FILE)
ok(
  "agent pointe vers weekly-advisor-worker.md (pas de table dupliquée)",
  agentSrc9.includes("weekly-advisor-worker.md"),
)
ok(
  "agent ne recopie pas la table branche→skill",
  !agentSrc9.includes("| Branche | Skill(s) requise(s) |"),
)

console.log(
  failures === 0
    ? "\nFLOW-DOCS OK — contrat de flux 9 surfaces vérifié"
    : `\n${failures} échec(s)`,
)
process.exit(failures === 0 ? 0 : 1)
}
