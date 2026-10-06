/**
 * weekly-advisor — pré-flight déterministe (neutre, sans SDK).
 *
 * Déplacé à l'identique de `.opencode/plugins/weekly-advisor.ts` : même ordre de
 * vérification (moteur → interpréteur → config → comptage `.py`), même `rc`
 * (`0` ou `3`) et surtout **même texte de message** — il estMatché par les
 * tests de contrat et affiché tel quel à l'agent. Toute reformulation casse
 * `weekly_preflight rc=3 — ...` côté documentation comme côté test.
 *
 * Fail-closed : `rc !== 0` ⇒ aucun run. `preflightOrThrow` (voir
 * `runtime.ts`) est le seul point de levée.
 */
import fs from "node:fs"
import path from "node:path"

import { defaultEnv, engineDirectory, type Env } from "./paths.ts"
import type { PreflightResult } from "./types.ts"

/** Nombre de modules `.py` sous le moteur — un moteur vide est un kit mort. */
function countPythonFiles(engine: string): number {
  let pyCount = 0
  const visit = (dir: string): void => {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const child = path.join(dir, entry.name)
      if (entry.isDirectory()) visit(child)
      else if (entry.isFile() && entry.name.endsWith(".py")) pyCount += 1
    }
  }
  visit(engine)
  return pyCount
}

/** La config est-elle du JSON lisible ? */
function isConfigParsable(configPath: string): boolean {
  try {
    JSON.parse(fs.readFileSync(configPath, "utf8"))
    return true
  } catch {
    return false
  }
}

/**
 * Pré-flight du kit. Jamais d'exception : un kit absent est un `rc=3` porteur
 * d'un message, jamais une exception à propager plus haut dans la pile.
 *
 * @param root racine du kit à valider
 * @param env environnement de lecture, `process.env` par défaut
 * @returns verdict ; `rc === 3` porte `message`
 */
export function preflight(root: string, env: Env = defaultEnv()): PreflightResult {
  const engine = engineDirectory(root)
  const engineOk = fs.existsSync(engine) && fs.statSync(engine).isDirectory()
  let pythonOk = false
  let configOk = false
  let pyCount = 0
  if (engineOk) {
    const venvPython =
      process.platform === "win32"
        ? path.join(engine, ".venv", "Scripts", "python.exe")
        : path.join(engine, ".venv", "bin", "python")
    pythonOk = Boolean(env.WEEKLY_PYTHON && fs.existsSync(env.WEEKLY_PYTHON)) || fs.existsSync(venvPython)
    configOk = isConfigParsable(path.join(engine, "weekly-telemetry-config.json"))
    pyCount = countPythonFiles(engine)
  }
  const ok = engineOk && pythonOk && configOk && pyCount > 0
  return {
    rc: ok ? 0 : 3,
    worktree: root,
    engine_ok: engineOk,
    python_ok: pythonOk,
    config_ok: configOk,
    py_count: pyCount,
    ...(ok
      ? {}
      : {
          message:
            `kit weekly-advisor introuvable depuis worktree=${root} ` +
            `(engine=${engine}, engine_ok=${engineOk}, python_ok=${pythonOk}, config_ok=${configOk}, py_count=${pyCount}) — ` +
            `lancer opencode avec --dir <racine-du-kit> (dossier contenant .opencode/plugins/weekly-advisor-engine) ` +
            `ou poser WEEKLY_KIT_ROOT=<racine-du-kit>`,
        }),
  }
}
