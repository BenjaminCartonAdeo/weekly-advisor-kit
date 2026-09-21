/**
 * weekly-advisor — résolution des chemins du kit (neutre, sans SDK).
 *
 * Aucune dépendance à `@opencode-ai/plugin` ni à un adaptateur : ce module ne
 * connaît que `node:path` / `node:fs` et l'environnement fourni. Les
 * adaptateurs V1 et V2 s'en servent pour construire leur {@link RuntimeApi},
 * ce qui garantit une seule implémentation de la résolution de racines.
 *
 * Logique déplacée à l'identique de `.opencode/plugins/weekly-advisor.ts` :
 * - `expandHome` : expansion `~` alignée sur `Path.expanduser()` du moteur
 *   Python (v6.2.c) — sans elle une config `"output_dir": "~/…"` donnait côté TS
 *   un littéral `<engine>/~/x` (split-brain avec le CLI qui, lui, expandait).
 * - `resolveEngine` : localisation moteur + interptimeur + config + output dir,
 *   avec l'ordre de vérification d'origine (moteur d'abord, puis Python) pour
 *   préserver les messages d'erreur gelés.
 * - `resolveWorktree` devient {@link resolveKitRoot} : même précédence, mais la
 *   source du candidat « répertoire du point d'entrée » devient un paramètre
 *   (`location.entrypointDirectory`) au lieu de `import.meta.url`. Le calcul
 *   `import.meta.url` reste du ressort de l'adaptateur, qui connaît son
 *   emplacement réel.
 */
import fs from "node:fs"
import os from "node:os"
import path from "node:path"

import type { EngineLoc } from "./types.ts"

/** Emplacement du moteur Python, relatif à la racine du kit. */
export const ENGINE_REL = [".opencode", "plugins", "weekly-advisor-engine"] as const

/** Fichier d'ancre glissante, relatif à `output_dir`. */
export const ANCHOR_FILE = "anchor-last.txt"

/**
 * Candidats de racine, du plus prioritaire au moins prioritaire. L'ordre est
 * normatif : il est vérifié par `scripts/tests/plugin-runtime.test.mjs`.
 */
export interface RootCandidate {
  /** Étiquette lisible, utilisée dans les messages d'échec de test. */
  readonly label: string;
  /** Valeur candidate, déjà absolue côté adaptateur. */
  readonly value: string | undefined;
}

/** Environnement effectif, par défaut `process.env`. */
export type Env = Readonly<Record<string, string | undefined>>

/** Abrège `process.env` sans muter l'environnement du processus. */
export function defaultEnv(): Env {
  return process.env
}

/** Construit `<root>/.opencode/plugins/weekly-advisor-engine`. */
export function engineDirectory(root: string): string {
  return path.join(root, ...ENGINE_REL)
}

/**
 * Expansion `~` alignée sur `Path.expanduser()` côté moteur Python (v6.2.c).
 *
 * @param target chemin pouvant commencer par `~`
 * @returns `os.homedir()` pour `~`, `homedir/<reste>` pour `~/` et `~\`, sinon
 *   la valeur inchangée
 */
export function expandHome(target: string): string {
  if (target === "~") return os.homedir()
  if (target.startsWith("~/") || target.startsWith("~\\")) return path.join(os.homedir(), target.slice(2))
  return target
}

/** Le candidat porte-t-il réellement un moteur weekly-advisor ? */
export function hasEngineDirectory(candidate: string): boolean {
  return fs.existsSync(path.join(candidate, ...ENGINE_REL))
}

/**
 * Résout la racine du kit, fail-closed : si aucun candidat ne contient de
 * moteur, la meilleure estimation est tout de même rendue pour que le
 * préflight puisse rapporter un `rc=3` explicite plutôt que de lever.
 *
 * Précédence (identique à l'ancien `resolveWorktree`, plus les sources V2) :
 *
 * ```text
 * WEEKLY_KIT_ROOT
 * emplacement absolu du point d'entrée
 * V1 worktree
 * V1 directory
 * V2 location.directory
 * V2 project directory
 * cwd
 * ```
 *
 * `WEEKLY_KIT_ROOT` est repris tel quel (`trim` + `resolve`) : sa validation
 * reste fail-closed côté préflight, pas ici — un env mal réglé doit produire le
 * message `rc=3` documenté, pas une résolution silencieuse ailleurs.
 *
 * @param candidates sources de racines, dans l'ordre ci-dessus ; toute source
 *   non applicable est passée `undefined` ou omise
 * @param env environnement de lecture, `process.env` par défaut
 * @returns racine du kit
 */
export function resolveKitRoot(candidates: readonly RootCandidate[], env: Env = defaultEnv()): string {
  const configured = env.WEEKLY_KIT_ROOT?.trim()
  if (configured) return path.resolve(configured)
  for (const candidate of candidates) {
    if (!candidate.value) continue
    // Le point d'entrée n'est retenu que s'il porte un moteur : c'est le seul
    // candidat qui puisse être écarté sans casser la précédence observée.
    if (candidate.label === "entrypoint" && !hasEngineDirectory(candidate.value)) continue
    return candidate.value
  }
  return process.cwd()
}

/**
 * Localise le moteur, l'interpréteur, la config et l'output dir de la racine.
 *
 * @param root racine du kit
 * @param env environnement de lecture, `process.env` par défaut
 * @returns localisation du moteur
 * @throws si le moteur est absent, ou si aucun interpréteur n'est utilisable —
 *   messages identiques à ceux du plugin d'origine
 */
export function resolveEngine(root: string, env: Env = defaultEnv()): EngineLoc {
  const engine = engineDirectory(root)
  // Résolution platform-aware : le venv est posé en .venv\Scripts\python.exe
  // sur Windows, .venv/bin/python ailleurs — un seul candidat par OS.
  const venvPython =
    process.platform === "win32"
      ? path.join(engine, ".venv", "Scripts", "python.exe")
      : path.join(engine, ".venv", "bin", "python")
  const candidates = [env.WEEKLY_PYTHON, venvPython].filter(
    (value): value is string => typeof value === "string" && value.length > 0,
  )
  const python = candidates.find((value) => fs.existsSync(value))
  if (!fs.existsSync(engine)) {
    throw new Error(`moteur introuvable: ${engine} (structure du kit corrompue)`)
  }
  if (!python) {
    throw new Error(
      `interpréteur Python introuvable (candidats testés: ${candidates.join(", ")}) — définir WEEKLY_PYTHON ou créer le venv : uv sync --project .opencode/plugins/weekly-advisor-engine --extra dev`,
    )
  }
  // config : <moteur>/weekly-telemetry-config.json (même résolution que le CLI)
  const configPath = path.join(engine, "weekly-telemetry-config.json")
  let config: Record<string, unknown> = {}
  if (fs.existsSync(configPath)) {
    config = JSON.parse(fs.readFileSync(configPath, "utf8")) as Record<string, unknown>
  }
  // Expansion `~` des clés de chemins — sémantique identique au moteur Python
  // (`expanduser()`), pour que l'ancre et tout usage TS voient la même racine.
  const expanded = { ...config }
  for (const key of ["project_root", "output_dir", "kit_root"]) {
    const value = expanded[key]
    if (typeof value === "string" && value.startsWith("~")) expanded[key] = expandHome(value)
  }
  const out = expanded["output_dir"]
  const outputDir =
    typeof out === "string" && path.isAbsolute(out)
      ? out
      : path.join(engine, typeof out === "string" ? out : "reports")
  return { engine, python, config: expanded, outputDir, configPath }
}
