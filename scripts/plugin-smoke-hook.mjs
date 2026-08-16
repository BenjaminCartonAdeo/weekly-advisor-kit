// Hook de résolution node:module — braque `@opencode-ai/plugin` vers le stub local
// (le paquet n'est pas installé en CI ; opencode le fournit au chargement réel).
// NB : dans le thread loader, import.meta.url est un objet URL → .href explicite.
const STUB = new URL("./stubs/opencode-plugin.mjs", import.meta.url).href

export async function resolve(specifier, context, nextResolve) {
  if (specifier === "@opencode-ai/plugin") {
    return { url: STUB, shortCircuit: true }
  }
  return nextResolve(specifier, context)
}