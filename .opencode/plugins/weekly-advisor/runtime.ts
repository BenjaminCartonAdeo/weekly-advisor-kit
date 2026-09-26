/**
 * weekly-advisor — runtime neutre (`WeeklyRuntime`).
 *
 * Toutes les capacités d'exécution dont dépend un outil : résolution de racine,
 * pré-flight, ancre, appel CLI, transport JSON par fichier temporaire et gate de
 * portabilité. Aucun import de `@opencode-ai/plugin` : les adaptateurs V1 et V2
 * construisent une instance et la passent aux outils, ce qui garantit que les
 * deux versions partagent exactement le même comportement.
 *
 * Deux propriétés structurantes :
 *
 * 1. **Aucune contamination entre instances.** L'ancien `worktree` module-scope
 *    était muté à l'init du plugin ; deux racines de kit ne pouvaient pas
 *    coexister. `root` est ici figé à la construction et `readonly` : deux
 *    runtimes pointent sur deux kits, y compris lors d'appels concurrents.
 * 2. **Aucun état global d'exécution.** L'environnement est capturé par instance,
 *    `process.env` restant la référence par défaut : le comportement par défaut
 *    est strictement identique à celui du plugin d'origine.
 *
 * Compatibilité de contrat — ce module satisfait les deux descriptions de
 * `RuntimeApi` (cf. `types.ts` et le plan dual V1/V2) :
 * - racine : {@link WeeklyRuntime.root} (plan) et l'alias
 *   {@link WeeklyRuntime.worktree} (contrat neutre) ;
 * - moteur : {@link WeeklyRuntime.engine} (propriété paresseuse, plan) et
 *   {@link WeeklyRuntime.resolveEngine} (contrat neutre) ;
 * - pré-flight : {@link WeeklyRuntime.preflight} (contrat neutre) et
 *   {@link WeeklyRuntime.preflightOrThrow} (plan) ;
 * - ancre : {@link WeeklyRuntime.readOrCreateAnchor} (plan),
 *   {@link WeeklyRuntime.anchorArg} → valeur `string` (contrat neutre, identique
 *   au source gelé) et {@link WeeklyRuntime.anchorArgs} → `["--anchor", …]` ;
 * - gate : {@link WeeklyRuntime.runPortabilityGate} conserve les arguments
 *   `(file, kind)` du comportement gelé ; `signal` est optionnel.
 *
 * Aucun journal : ni contenu de prompt, ni secret, ni variable d'environnement.
 */
import fs from "node:fs"
import os from "node:os"
import path from "node:path"
import { execFile } from "node:child_process"

import { ANCHOR_FILE, defaultEnv, resolveEngine, resolveKitRoot, type Env, type RootCandidate } from "./paths.ts"
import { preflight } from "./preflight.ts"
import type { EngineLoc, PreflightResult, PortabilityOutcome, RuntimeApi, SkillVerifyDetail, StagedPayload } from "./types.ts"

/** Localisation fournie par l'adaptateur : chaque source est optionnelle. */
export interface RuntimeLocation {
  /**
   * Répertoire absolu du point d'entrée (connu de l'adaptateur) — retenu
   * uniquement s'il porte réellement un `weekly-advisor-engine`.
   */
  readonly entrypointDirectory?: string;
  /** `ctx.worktree` côté V1. */
  readonly v1Worktree?: string;
  /** `ctx.directory` côté V1. */
  readonly v1Directory?: string;
  /** `input.directory` côté V2. */
  readonly v2Directory?: string;
  /** Répertoire projet V2. */
  readonly v2ProjectDirectory?: string;
  /** Repli explicite, sinon `process.cwd()`. */
  readonly cwd?: string;
}

/** Options de construction. */
export interface RuntimeOptions {
  readonly location: RuntimeLocation;
  /** Environnement de lecture, `process.env` par défaut. */
  readonly env?: Env;
}

/** Buffer maximal de sortie CLI — gelé. */
const MAX_BUFFER = 64 * 1024 * 1024

/** Timeout de la gate `skill-verify`, en ms — gelé. */
const SKILL_VERIFY_TIMEOUT_MS = 60_000

/** Préfixe des règles custom qui décident du verdict de la gate. */
const PORTABILITY_PREFIX = "custom/portability/"

/** Rapport `skill-verify` : seule la forme utile est décrite. */
type SkillVerifyReport = { findings?: Array<{ details?: readonly SkillVerifyDetail[] }> }

/** Répertoire temporaire isolé, avec sa fonction de nettoyage. */
interface ScanDir {
  readonly dir: string;
  readonly cleanup: () => void;
}

/**
 * Copie réelle (zéro symlink) de l'artefact dans un répertoire temporaire : le
 * scan porte sur LE seul artefact commis — jamais sur le voisinage du dossier
 * de drafts (des findings tiers bloqueraient à tort). Pour un skill, le dossier
 * entier est copié afin que la règle `self-contained-scripts` voie les scripts
 * frères.
 */
// ponytail: isolation par copie plutôt que scan du dossier parent
function prepareScanDir(file: string): ScanDir {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "wa-portab-"))
  const cleanup = () => fs.rmSync(tmp, { recursive: true, force: true })
  if (path.basename(file) === "SKILL.md") {
    const dir = path.join(tmp, "skill")
    fs.cpSync(path.dirname(file), dir, { recursive: true })
    return { dir, cleanup }
  }
  fs.copyFileSync(file, path.join(tmp, path.basename(file)))
  return { dir: tmp, cleanup }
}

/** Extrait les findings `custom/portability/*` d'un rapport `skill-verify` JSON. */
function collectPortability(stdout: string): { errors: string[]; warnings: string[] } {
  const report = JSON.parse(stdout) as SkillVerifyReport
  const errors: string[] = []
  const warnings: string[] = []
  for (const finding of report.findings ?? []) {
    for (const detail of finding.details ?? []) {
      if (!detail.rule?.startsWith(PORTABILITY_PREFIX)) continue
      const entry =
        `[${detail.rule}] ${detail.message ?? ""}` +
        (detail.suggestion ? ` — suggestion : ${detail.suggestion}` : "")
      // Les findings critical sont aussi bloquants : harness-eval émet
      // normalement `error`, conserver ce niveau évite une gate fail-open si une
      // règle custom emploie le nom de sévérité le plus fort.
      const severity = detail.severity?.toLowerCase()
      ;(severity === "error" || severity === "critical" ? errors : warnings).push(entry)
    }
  }
  return { errors, warnings }
}

/** Binaire présent sur PATH ? POSIX : ENOENT détecté au spawn, pré-check inutile. */
function commandOnPath(cmd: string): Promise<boolean> {
  if (process.platform !== "win32") return Promise.resolve(true)
  // Windows : `where` (natif) résout les shims .cmd — spawn direct sans shell non.
  return new Promise((resolve) => {
    execFile("where", [cmd], { windowsHide: true }, (err) => resolve(!err))
  })
}

/**
 * Runtime d'exécution d'un kit weekly-advisor. Une instance = une racine.
 *
 * @example
 * ```ts
 * const runtime = new WeeklyRuntime({ location: { v1Worktree: rootA } })
 * const { engine, python, outputDir } = runtime.engine
 * ```
 */
export class WeeklyRuntime implements RuntimeApi {
  /** Racine du kit, figée à la construction : jamais mutée par un autre runtime. */
  readonly root: string

  /** Environnement capturé par instance — `process.env` par défaut. */
  private readonly env: Env

  constructor(options: RuntimeOptions) {
    this.env = options.env ?? defaultEnv()
    this.root = resolveKitRoot(this.rootCandidates(options.location), this.env)
  }

  /**
   * Ordre normatif des racines, du plus prioritaire au moins prioritaire. Le
   * candidat « point d'entrée » est le seul écartable, et uniquement quand il ne
   * porte pas de moteur : il ne peut donc pas masquer un contexte valide.
   */
  static readonly rootPrecedence = [
    "WEEKLY_KIT_ROOT",
    "entrypoint",
    "v1-worktree",
    "v1-directory",
    "v2-directory",
    "v2-project-directory",
    "cwd",
  ] as const

  /**
   * Candidats de racine dans l'ordre ci-dessus. Les valeurs sont rendues telles
   * quelles (non résolues) : l'adaptateur a déjà fourni des chemins absolus, et
   * résoudre une valeur inexistante déformerait le message du pré-flight.
   */
  private rootCandidates(location: RuntimeLocation): readonly RootCandidate[] {
    return [
      { label: "entrypoint", value: location.entrypointDirectory },
      { label: "v1-worktree", value: location.v1Worktree },
      { label: "v1-directory", value: location.v1Directory },
      { label: "v2-directory", value: location.v2Directory },
      { label: "v2-project-directory", value: location.v2ProjectDirectory },
      { label: "cwd", value: location.cwd },
    ]
  }

  /** Alias de {@link root} : dénomination du contrat neutre (`types.ts`). */
  get worktree(): string {
    return this.root
  }

  /**
   * Localisation du moteur. Résolue à chaque accès — la config est relue à
   * chaque appel, comportement d'origine : un run peut reconfigurer le kit entre
   * deux outils sans reconstruire le runtime.
   */
  get engine(): EngineLoc {
    return resolveEngine(this.root, this.env)
  }

  /** Localise moteur + interpréteur + config + output dir. */
  resolveEngine(): EngineLoc {
    return this.engine
  }

  /** Pré-flight déterministe du kit. Jamais d'exception. */
  preflight(): PreflightResult {
    return preflight(this.root, this.env)
  }

  /**
   * Pré-flight fail-closed : lève si le kit n'est pas utilisable. Message gelé
   * (`weekly_preflight rc=3 — …`), repris tel quel par la documentation et les
   * tests de contrat.
   *
   * @throws si `preflight().rc !== 0`
   */
  preflightOrThrow(): void {
    const result = this.preflight()
    if (result.rc !== 0) throw new Error(`weekly_preflight rc=3 — ${result.message}`)
  }

  /**
   * Ancre glissante : ancre explicite si fournie, sinon ancre du jour lue,
   * rafraîchie ou créée dans `<output_dir>/anchor-last.txt`.
   *
   * @param anchor ancre ISO-8601 explicite (override rare)
   * @returns ancre effective
   * @throws si le fichier d'ancre est illisible, ou contient une date invalide
   */
  readOrCreateAnchor(anchor?: string): string {
    return anchor ?? readOrCreateAnchorFile(this.engine.outputDir)
  }

  /**
   * Ancre effective seule, pour les outils qui assemblent eux-mêmes leurs
   * drapeaux (contrat neutre et source d'origine).
   *
   * @param anchor ancre ISO-8601 explicite (override rare)
   * @returns ancre ISO-8601
   */
  anchorArg(anchor?: string): string {
    return this.readOrCreateAnchor(anchor)
  }

  /**
   * Drapeaux `--anchor <valeur>` prêts à être concaténés à un argv. Variante
   * tableau du contrat dual V1/V2 ; {@link anchorArg} reste la valeur nue.
   *
   * @param anchor ancre ISO-8601 explicite (override rare)
   * @returns `["--anchor", "<ancre>"]`
   */
  anchorArgs(anchor?: string): readonly string[] {
    return ["--anchor", this.readOrCreateAnchor(anchor)]
  }

  /**
   * Exécute une sous-commande du moteur.
   *
   * Options `execFile` conservées à l'identique : `cwd` = moteur, timeout fourni,
   * buffer 64 MiB, `PYTHONUTF8`/`PYTHONIOENCODING` forcés (Windows : consoles
   * cp1252/cp850). `--config` n'est émis que si le fichier existe — absent du
   * disque, le CLI retombe sur sa découverte par défaut (même chemin
   * `<cwd>/weekly-telemetry-config.json`, comportement inchangé).
   *
   * @param args sous-commande et ses options
   * @param timeoutMs budget de la sous-commande
   * @param signal annulation ; l'enfant est tué et la promesse rejetée
   * @returns stdout du moteur, `trim`é
   * @throws message gelé `weekly_telemetry_aggregator <args> → exit <code>` ;
   *   message distinct et explicite si `signal` est déclenché
   */
  runCli(args: readonly string[], timeoutMs: number, signal?: AbortSignal): Promise<string> {
    if (signal?.aborted) return Promise.reject(abortError(args))
    const { engine, python, configPath } = this.engine
    const argv = [
      "-m",
      "weekly_telemetry_aggregator",
      ...(fs.existsSync(configPath) ? ["--config", configPath] : []),
      ...args,
    ]
    return new Promise((resolve, reject) => {
      execFile(
        python,
        argv,
        {
          cwd: engine,
          timeout: timeoutMs,
          maxBuffer: MAX_BUFFER,
          // `this.env` par-dessus `process.env` : comportement par défaut
          // identique (les deux valent process.env), et un env injecté reste
          // visible de l'interpréteur.
          env: { ...process.env, ...this.env, PYTHONUTF8: "1", PYTHONIOENCODING: "utf-8" },
          ...(signal ? { signal } : {}),
        },
        (err, stdout, stderr) => {
          if (!err) return resolve(stdout.trim())
          if (signal?.aborted) return reject(abortError(args))
          const detail = (stderr || stdout || "").trim().split("\n").slice(-12).join("\n")
          reject(new Error(`weekly_telemetry_aggregator ${args.join(" ")} → exit ${err.code ?? "?"}\n${detail}`))
        },
      )
    })
  }

  /**
   * Écrit un payload JSON dans un fichier temporaire : `argv` a une taille
   * limitée et variable selon la plateforme, un JSON volumineux n'a pas sa
   * place dedans.
   *
   * L'annulation est traitée aux deux extrémités (avant création, après
   * écriture) : aucun répertoire résiduel, quel que soit le point de sortie.
   *
   * @param payload JSON sérialisé
   * @param label libellé de traçage (`coherence`, `catalog`, `usage`)
   * @param signal annulation
   * @returns chemin du fichier et fonction de nettoyage
   * @throws si le JSON est invalide, l'écriture impossible, ou `signal` déclenché
   */
  stageJsonPayload(payload: string, label: string, signal?: AbortSignal): StagedPayload {
    // ponytail: file transport is the minimum reliable fix for oversized tool arguments.
    if (signal?.aborted) throw stagingAbortError(label)
    try {
      JSON.parse(payload)
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error)
      throw new Error(`weekly_skill_curate ${label} JSON invalide: ${detail}`)
    }
    const directory = fs.mkdtempSync(path.join(path.resolve(os.tmpdir()), `weekly-${label}-`))
    const file = path.resolve(directory, "input.json")
    const cleanup = () => fs.rmSync(directory, { recursive: true, force: true })
    try {
      fs.writeFileSync(file, payload, "utf8")
    } catch (error) {
      try {
        cleanup()
      } catch {
        // Préserver l'échec d'écriture : le nettoyage reste best-effort.
      }
      const detail = error instanceof Error ? error.message : String(error)
      throw new Error(`weekly_skill_curate ${label} JSON temporaire impossible: ${detail}`)
    }
    if (signal?.aborted) {
      try {
        cleanup()
      } catch {
        // Préserver l'annulation : le nettoyage reste best-effort.
      }
      throw stagingAbortError(label)
    }
    return { file, cleanup }
  }

  /**
   * Gate de portabilité : chaque draft est scanné par `harness-eval skill-verify`
   * AVANT tout commit-draft. Seules les règles `custom/portability/*` du kit
   * décident (`.harness-eval/rules/portability.yaml`) :
   * - `cwd` = racine du kit OBLIGATOIRE (chargement des règles custom depuis
   *   `<racine>/.harness-eval/rules/` ; lancée ailleurs : 0 règle → gate
   *   aveugle) — c'est `this.root`, plus le module-global d'origine ;
   * - ≥ 1 finding error → commit refusé, fix manuel requis ;
   * - warnings seuls → commit autorisé, note jointe au résultat ;
   * - binaire absent (ENOENT POSIX / `where` négatif Windows) → fail-soft : note
   *   non bloquante — gap d'install documenté, signalé par le doctor ;
   * - timeout, crash scanner ou sortie illisible → REFUS « gate non exécutable »
   *   : jamais de faux vert — safety-first.
   *
   * win32 : `execFile` sans shell ne résout pas le shim `.cmd` de harness-eval
   * (uv tool install) → ENOENT systématique, gate jamais active. Sous Windows on
   * passe `shell:true` (résolution via cmd) avec `scanDir` quoté (join par
   * espaces) ; un pré-check `where` préserve la sémantique fail-soft du binaire
   * absent. Garde non exécutable sur poste POSIX — vérifier sous Windows avant
   * release.
   *
   * @param file artefact à scanner
   * @param kind `skill` → gate applicable ; `command` → skip explicite
   * @param signal annulation ; une annulation n'est pas un verdict de gate
   * @returns verdict ; `blocked`/`unusable` ⇒ refus obligatoire
   */
  async runPortabilityGate(
    file: string,
    kind: "skill" | "command",
    signal?: AbortSignal,
  ): Promise<PortabilityOutcome> {
    // E2 : harness-eval skill-verify n'inspecte que les dossiers SKILL.md
    // (≥ 7.10.1). Pour une command, lancer la gate produirait un crash
    // systématique (« No agent components found ») → refus injustifié. Skip
    // explicite avec motif remonté au résultat du tool — jamais de silence ni
    // de faux vert.
    if (kind === "command") {
      return {
        kind: "skipped",
        reason:
          "Gate portabilité non applicable aux commands (harness-eval 7.10.1 : skills uniquement) — commit autorisé sans gate.",
      }
    }
    const scan = prepareScanDir(file)
    try {
      let stdout: string
      try {
        stdout = await this.runSkillVerify(scan.dir, signal)
      } catch (error) {
        // Crash/timeout du scanner ≠ faute de l'artefact, mais un commit passé
        // sur une gate morte serait un faux vert → REFUS. Une annulation, elle,
        // n'est pas un verdict : elle remonte à l'appelant.
        if (signal?.aborted) throw error
        return { kind: "unusable", reason: (error as Error).message.split("\n")[0] }
      }
      if (!stdout.trim()) return { kind: "ignored", reason: "binaire harness-eval introuvable" }
      let errors: string[]
      let warnings: string[]
      try {
        ;({ errors, warnings } = collectPortability(stdout))
      } catch {
        return { kind: "unusable", reason: "sortie skill-verify illisible (JSON invalide)" }
      }
      if (errors.length > 0) return { kind: "blocked", findings: errors }
      return { kind: "pass", warnings }
    } finally {
      scan.cleanup()
    }
  }

  /**
   * Spawn `harness-eval` avec `cwd` = racine du kit. Résout `""` si le binaire
   * est absent — le verdict `ignored` est rendu par l'appelant.
   *
   * @param scanDir répertoire de scan isolé (copie de l'artefact)
   * @param signal annulation
   * @returns rapport JSON brut, `""` si binaire absent
   * @throws si le scanner est installé mais crash, timeout ou illisible
   */
  private async runSkillVerify(scanDir: string, signal?: AbortSignal): Promise<string> {
    if (!(await commandOnPath("harness-eval"))) return ""
    const winShell = process.platform === "win32"
    return new Promise((resolve, reject) => {
      execFile(
        "harness-eval",
        // shell:true → argv joint par espaces : quotage obligatoire (tmpdir peut
        // contenir des espaces sous Windows). POSIX : arg brut, pas de shell.
        ["skill-verify", winShell ? `"${scanDir}"` : scanDir, "--format", "json"],
        {
          cwd: this.root,
          timeout: SKILL_VERIFY_TIMEOUT_MS,
          maxBuffer: 16 * 1024 * 1024,
          env: { ...process.env, ...this.env },
          shell: winShell,
          ...(signal ? { signal } : {}),
        },
        (err, stdout, stderr) => {
          if (err && (err as NodeJS.ErrnoException).code === "ENOENT") return resolve("")
          if (err) {
            if (signal?.aborted) return reject(abortError(["skill-verify", scanDir]))
            const why = (err as NodeJS.ErrnoException & { killed?: boolean }).killed
              ? `timeout après ${SKILL_VERIFY_TIMEOUT_MS / 1000} s`
              : `exit ${String(err.code ?? "?")}`
            return reject(
              new Error(
                `skill-verify indisponible (${why})\n${(stderr || stdout || "").trim().slice(-400)}`,
              ),
            )
          }
          resolve(stdout)
        },
      )
    })
  }
}

/**
 * Ancre glissante lue/créée/rafraîchie : créée si absente, conservée dans la
 * même journée (stabilité intra-run : tous les tools du run partagent la même
 * fenêtre), rafraîchie vers maintenant chaque jour. La fraîcheur par « âge ≤
 * fenêtre » gelait la fenêtre quand des runs s'enchaînaient en moins de 7 j
 * (chaque run rejouait la même période). Rejouer une fenêtre historique =
 * passer `--anchor` explicitement (v6.0.n).
 *
 * @param outputDir répertoire de sortie du moteur
 * @returns ancre ISO-8601 à la seconde
 * @throws si le fichier est illisible (hors absence) ou contient une date invalide
 */
export function readOrCreateAnchorFile(outputDir: string): string {
  const file = path.join(outputDir, ANCHOR_FILE)
  let existing: string
  try {
    existing = fs.readFileSync(file, "utf8").trim()
  } catch (err) {
    // Seule une ancre initiale réellement absente est récupérable ; préserver la
    // preuve des échecs de permission, de répertoire et autres.
    if ((err as NodeJS.ErrnoException).code !== "ENOENT") {
      throw new Error(`lecture de ${file} impossible: ${String(err)}`)
    }
    existing = ""
  }
  if (existing) {
    const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})Z$/.exec(existing)
    const validDate =
      match !== null &&
      (() => {
        const [, year, month, day, hour, minute, second] = match
        const date = new Date(Date.UTC(+year, +month - 1, +day, +hour, +minute, +second))
        return (
          date.getUTCFullYear() === +year &&
          date.getUTCMonth() === +month - 1 &&
          date.getUTCDate() === +day &&
          date.getUTCHours() === +hour &&
          date.getUTCMinutes() === +minute &&
          date.getUTCSeconds() === +second
        )
      })()
    if (!validDate) {
      throw new Error(`ancre invalide dans ${file}: ${existing}`)
    }
    const today = new Date().toISOString().slice(0, 10)
    if (existing.slice(0, 10) === today) return existing
  }
  const anchor = new Date().toISOString().replace(/\.\d{3}Z$/, "Z")
  fs.mkdirSync(outputDir, { recursive: true })
  fs.writeFileSync(file, anchor)
  return anchor
}

/** Message d'annulation : distinct des erreurs ordinaires, jamais silencieux. */
function abortError(args: readonly string[]): Error {
  return new Error(`weekly_telemetry_aggregator ${args.join(" ")} → annulé (AbortSignal)`)
}

function stagingAbortError(label: string): Error {
  return new Error(`weekly_skill_curate ${label} staging annulé (AbortSignal)`)
}

/**
 * Vérification de conformité **à la compilation** : `WeeklyRuntime` doit
 * satisfaire le contrat neutre de `types.ts`. Si un membre diverge, l'erreur
 * apparaît au typecheck, pas à l'exécution. Effacé par le type stripping.
 */
type ConformsToNeutralContract = WeeklyRuntime extends RuntimeApi ? true : never
export type _WeeklyRuntimeConforms = ConformsToNeutralContract
