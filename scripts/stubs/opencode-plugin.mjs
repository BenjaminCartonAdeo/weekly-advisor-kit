// Stub minimal de `@opencode-ai/plugin` pour les tests de contrat node (zéro dépendance).
// Le vrai module est fourni par opencode au chargement ; ici seul le contrat
// utilisé par `weekly-advisor/adapters/v1.ts` compte : `tool(def)` + les builders
// `tool.schema.*`.
//
// Chaque appel de builder renvoie un objet NEUF qui enregistre ce qui a été
// demandé — primitive, valeurs d'enum, `optional()`, `describe(...)`. C'est ce qui
// rend `required` et la description OBSERVABLES à l'exécution, donc testables sans
// analyser le source du plugin (l'ancien contrat le faisait, à une époque où le
// plugin déclarait ses outils en clair). Chaque objet reste chaînable :
// `tool.schema.string().optional().describe("…")`.
//
// ponytail: un objet par builder suffit — pas de zod, pas de validation.
const builder = (kind, values) => ({
  kind,
  ...(kind === "enum" ? { values: [...values] } : {}),
  required: true,
  description: "",
  optional() {
    this.required = false
    return this
  },
  describe(description) {
    this.description = description
    return this
  },
})

export const tool = (def) => def
tool.schema = {
  string: () => builder("string"),
  boolean: () => builder("boolean"),
  number: () => builder("number"),
  enum: (values) => builder("enum", values),
}
