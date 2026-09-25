/**
 * weekly-advisor — point d'entrée unique, dual runtime (V1 + V2).
 *
 * Ce fichier ne contient **aucune logique d'outil** et **aucun import runtime
 * du SDK**. Il expose exactement un objet par défaut, le contrat de module
 * attendu par le loader d'opencode :
 *
 * ```text
 * { id: "weekly-advisor", server(ctx, options), setup(ctx) }
 * ```
 *
 * - `server` est le point d'entrée **V1** (`@opencode-ai/plugin`) : c'est là que
 *   sont exposés les deux hooks de garde et les 19 outils ;
 * - `setup` est le point d'entrée **V2** : il n'implémente rien aujourd'hui, il
 *   ne fait que matérialiser la couture d'import dynamique.
 *
 * **Aucun chargement statique.** Les deux adaptateurs sont chargés par
 * `import()` dynamique, chacun depuis l'entrée qui lui correspond : un hôte V1
 * ne charge pas le SDK V2, un hôte V2 ne charge pas le SDK V1, et le
 * chargement du point d'entrée lui-même n'en charge aucun. C'est la propriété qui
 * permet au kit de tourner sur les deux runtimes sans dépendance croisée.
 *
 * **Aucun repli silencieux.** `setup` ne masque pas l'absence d'adaptateur V2 :
 * l'import et l'appel sont propagés tels quels, un échec reste un échec. De même,
 * il n'existe pas de chemin de repli « si le SDK manque, on fait semblant » — un
 * kit sans outils ne doit pas pouvoir passer pour un kit opérationnel.
 *
 * Les outils eux-mêmes vivent dans `weekly-advisor/tool-registry.ts` (contrat
 * neutre) et `weekly-advisor/runtime.ts` (capacités d'exécution) : le point
 * d'entrée n'en connaît que les noms de fichiers, par import dynamique.
 */
import type { Hooks, PluginInput, PluginOptions } from "@opencode-ai/plugin"

/**
 * Specifier de l'adaptateur V2, résolu au seul appel de `setup()`.
 *
 * Passé par une variable et non en littéral : TypeScript ne doit pas résoudre
 * statiquement un module qui n'existe pas encore (cellule V2), et l'import ne
 * doit jamais se produire au chargement de ce fichier. Le type du module
 * importé est donc celui de {@link V2Adapter}, déclaré localement.
 */
const V2_ADAPTER = "./weekly-advisor/adapters/v2.ts"

/**
 * Contrat local de la couture V2.
 *
 * Tant que l'adaptateur V2 n'existe pas, aucun type ne peut en être dérivé
 * (pas de `@opencode-ai/*` V2 en dépendance) : `unknown` en entrée comme en
 * sortie, et une seule assertion — sur le module importé, à la frontière.
 */
interface V2Adapter {
  readonly setup: (ctx: unknown) => Promise<unknown>
}

const plugin = {
  id: "weekly-advisor",
  /** Adaptateur V1 : hooks de garde + 19 outils. */
  server: async (ctx: PluginInput, options?: PluginOptions): Promise<Hooks> => {
    const adapter = await import("./weekly-advisor/adapters/v1.ts")
    return adapter.server(ctx, options)
  },
  /** Adaptateur V2 : couture d'import, rien d'autre tant qu'il n'existe pas. */
  setup: async (ctx: unknown): Promise<unknown> => {
    const adapter: V2Adapter = await import(V2_ADAPTER)
    return adapter.setup(ctx)
  },
}

export default plugin
