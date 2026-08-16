// Stub minimal de `@opencode-ai/plugin` pour le smoke test (CI, zéro dépendance).
// Le vrai module est fourni par opencode au chargement ; ici seul le contrat
// utilisé par weekly-advisor.ts compte : `tool(def)` + chaîne `tool.schema.*`.
const chain = {
  optional() {
    return this
  },
  describe() {
    return this
  },
}

export const tool = (def) => def
tool.schema = {
  string: () => chain,
  boolean: () => chain,
  number: () => chain,
  enum: () => chain,
}