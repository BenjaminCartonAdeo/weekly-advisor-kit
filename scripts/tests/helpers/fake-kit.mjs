// Fabrique de kit weekly-advisor factice pour les tests de contrat node.
// Aucun effet sur le moteur Python réel : tout est écrit dans un répertoire
// temporaire, et l'interpréteur est un script POSIX qui journalise son argv.
//
// Deux modes :
//   - `makeKit()`          : kit minimal valide (moteur + config + venv factice)
//   - `makeCapturingKit()` : idem + interpréteur qui écrit chaque argument dans
//                            un fichier de capture, pour observer l'argv émis
//                            par un outil (drapeaux, défauts, ordre).
import fs from "node:fs"
import os from "node:os"
import path from "node:path"

/** Script POSIX : journalise `flag=value` pour les paires et `flag` pour les isolés. */
const CAPTURING_PYTHON = `#!/bin/sh
set -eu
: > "$CAPTURE"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --coherence|--catalog|--usage)
      printf "%s=%s\\n" "$1" "$2" >> "$CAPTURE"
      cat "$2" >> "$CAPTURE" 2>/dev/null || true
      printf "\\n" >> "$CAPTURE"
      shift 2
      ;;
    --anchor|--runs-seen|--stale-days|--mode|--kind|--file|--proposal|--lookback-days|--extract-dir)
      printf "%s=%s\\n" "$1" "$2" >> "$CAPTURE"
      shift 2
      ;;
    --apply|--include-children)
      printf "%s\\n" "$1" >> "$CAPTURE"
      shift
      ;;
    -m|--config)
      shift
      ;;
    *)
      printf "ARG=%s\\n" "$1" >> "$CAPTURE"
      shift
      ;;
  esac
done
if [ "\${FAIL:-0}" = "1" ]; then
  printf "forced failure\\n" >&2
  exit 17
fi
printf "ok\\n"
`

/**
 * Crée un kit factice : `<root>/.opencode/plugins/weekly-advisor-engine`
 * avec `weekly_telemetry_aggregator/main.py`, une config et un venv.
 * @param {{ python?: string }} [options] interpréteur à enregistrer dans
 *   `.venv/bin/python` (défaut : fichier vide, suffisant pour le pré-flight).
 * @returns {{ root: string, engine: string, python: string }}
 */
export function makeKit(options = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "wa-fakekit-"))
  const engine = path.join(root, ".opencode", "plugins", "weekly-advisor-engine")
  fs.mkdirSync(path.join(engine, ".venv", "bin"), { recursive: true })
  fs.mkdirSync(path.join(engine, "weekly_telemetry_aggregator"), { recursive: true })
  fs.writeFileSync(path.join(engine, "weekly_telemetry_aggregator", "main.py"), "")
  fs.writeFileSync(path.join(engine, "weekly-telemetry-config.json"), "{}")
  const python = path.join(engine, ".venv", "bin", "python")
  fs.writeFileSync(python, options.python ?? "")
  return { root, engine, python }
}

/**
 * Kit factice dont l'interpréteur journalise son argv dans `$CAPTURE`.
 * @returns {{ root: string, engine: string, python: string, capture: string }}
 */
export function makeCapturingKit() {
  const { root, engine } = makeKit()
  const python = path.join(root, "fake-python.sh")
  fs.writeFileSync(python, CAPTURING_PYTHON)
  fs.chmodSync(python, 0o755)
  return { root, engine, python, capture: path.join(root, "capture.log") }
}

/**
 * Branche le kit sur les variables d'environnement attendues par le plugin
 * (`WEEKLY_KIT_ROOT`, `WEEKLY_PYTHON`, `CAPTURE`, `FAIL`) et rend une fonction
 * de restauration. À appeler dans un `finally`.
 * @param {{ root: string, python: string, capture?: string, fail?: boolean }} kit
 * @returns {() => void}
 */
export function useKitEnv(kit) {
  const keys = ["WEEKLY_KIT_ROOT", "WEEKLY_PYTHON", "CAPTURE", "FAIL"]
  const previous = new Map(keys.map((key) => [key, process.env[key]]))
  process.env.WEEKLY_KIT_ROOT = kit.root
  process.env.WEEKLY_PYTHON = kit.python
  if (kit.capture) process.env.CAPTURE = kit.capture
  if (kit.fail) process.env.FAIL = "1"
  return () => {
    for (const [key, value] of previous) {
      if (value === undefined) delete process.env[key]
      else process.env[key] = value
    }
  }
}

/** Instancie le plugin V1 pointé sur un kit factice. */
export async function instantiatePlugin(kit, loadPlugin) {
  const plugin = await loadPlugin()
  return plugin({ worktree: kit.root, directory: kit.root })
}
