/**
 * weekly-advisor — adaptateur V1 (`@opencode-ai/plugin`).
 *
 * **Seul module du kit autorisé à importer le SDK V1.** Le point d'entrée
 * `.opencode/plugins/weekly-advisor.ts` n'a aucun import *statique* : il charge
 * ce module par `import()` dynamique depuis `server()`. Conséquence directe : un
 * hôte V2 qui n'instancie jamais `server` ne charge jamais le SDK V1, et
 * inversement. Symétriquement, l'adaptateur V2 sera le seul module à importer le
 * SDK V2.
 *
 * Cet adaptateur ne contient **aucune logique d'outil** : il traduit le registre
 * neutre ({@link TOOL_REGISTRY}) vers la forme attendue par `tool()`. Tout ce
 * qui touche au contrat — noms, descriptions, champs, timeouts, argv, messages
 * d'erreur — reste donc défini à un seul endroit, et l'adaptateur V2 obtiendra
 * exactement le même comportement.
 *
 * Correspondances gelées (cf. `scripts/fixtures/weekly-advisor-tool-contract.json`) :
 * - **ordre** : `Object.fromEntries` sur {@link TOOL_REGISTRY}, donc l'ordre
 *   d'enregistrement reste celui de la fixture (19 outils, ordre figé) ;
 * - **champs** : un {@link FieldSpec} → un `tool.schema.<primitive>`, puis
 *   `.optional()` si et seulement si `required === false`, puis
 *   `.describe(description)` — ordre des appels identique à la source d'origine,
 *   description présente même pour un champ optionnel ;
 * - **résultat** : `execute` rend la `string` du handler, jamais un objet
 *   `{ output }` — le moteur et les tests lisent du texte brut ;
 * - **filtres de hooks** : seuls `weekly-review` (commande) et l'agent
 *   `weekly-advisor` déclenchent le pré-flight ; tout autre agent/message passe
 *   sans coût ni erreur.
 *
 * Un runtime par appel de {@link server} : plus de `worktree` module-scope, donc
 * deux racines de kit peuvent coexister — y compris pour des appels concurrents
 * (l'ancien défaut était une variable module mutée à l'init du plugin). Le
 * `AbortSignal` du contexte d'exécution est transmis tel quel au handler.
 */
import { type Plugin, type ToolContext, tool } from "@opencode-ai/plugin"
import path from "node:path"
import { fileURLToPath } from "node:url"

import { TOOL_REGISTRY, type AbortableRuntimeApi } from "../tool-registry.ts"
import { WeeklyRuntime } from "../runtime.ts"
import type { FieldSpec, ToolDefinition } from "../types.ts"

/**
 * Racine du kit déduite de l'emplacement de cet adaptateur, soit quatre
 * remontées : `<racine>/.opencode/plugins/weekly-advisor/adapters/v1.ts` →
 * `adapters` → `weekly-advisor` → `plugins` → `.opencode` → `<racine>`.
 *
 * `resolveKitRoot` ne retient ce candidat que s'il porte réellement un moteur,
 * et seulement après `WEEKLY_KIT_ROOT` : la précédence d'origine est donc
 * préservée à l'identique. Une erreur de comptage ici n'est jamais silencieuse —
 * le candidat est ignoré, la racine tombe sur le contexte, et le pré-flight
 * rapporte alors un `rc=3` qui nomme la racine utilisée.
 */
const ENTRYPOINT_DIRECTORY = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "..",
  "..",
  "..",
  "..",
)

/**
 * Forme des `args` attendue par `tool()`, dérivée du SDK lui-même — le kit ne
 * dépend pas de `zod` en direct, donc aucun type n'est importé d'un paquet
 * transitif.
 */
type ToolArgs = Parameters<typeof tool>[0]["args"]

/** Un champ d'argument : n'importe quel schéma que `tool()` accepte. */
type FieldSchema = ToolArgs[string]

/**
 * Valeurs d'un champ `enum` sous la forme exigée par `tool.schema.enum` : un
 * tuple non vide, pas un tableau.
 *
 * @param values valeurs déclarées par le contrat
 * @returns tuple `[première, …reste]`
 * @throws si le contrat annonce un `enum` sans valeurs — erreur d'adaptateur
 *   nommée, jamais un champ silencieusement traité comme du texte
 */
function enumValues(values: readonly string[] | undefined): [string, ...string[]] {
  // Inatteignable sur un registre gelé (Task 3) ; nommé explicitement pour qu'un
  // jour une régression de contrat soit lisible.
  if (!values?.length) throw new Error("weekly-advisor: enum sans valeurs")
  return [values[0], ...values.slice(1)]
}

/**
 * Traduit un {@link FieldSpec} du contrat neutre en builder `tool.schema.*`.
 *
 * @param field spécification du champ (primitive, caractère obligatoire, aide)
 * @returns schéma `tool()` du champ, description comprise
 */
function buildField(field: FieldSpec): FieldSchema {
  const schema =
    field.kind === "enum"
      ? tool.schema.enum(enumValues(field.values))
      : field.kind === "boolean"
        ? tool.schema.boolean()
        : field.kind === "number"
          ? tool.schema.number()
          : tool.schema.string()
  return field.required
    ? schema.describe(field.description)
    : schema.optional().describe(field.description)
}

/**
 * Assemble les `args` d'un outil dans l'ordre du contrat. L'ordre d'insertion
 * des clés est celui de l'objet rendu : l'ordre des champs reste donc figé.
 *
 * @param fields champs du contrat neutre
 * @returns objet `args` attendu par `tool()`
 */
function buildArgs(fields: readonly FieldSpec[]): ToolArgs {
  const args: Record<string, FieldSchema> = {}
  for (const field of fields) args[field.name] = buildField(field)
  return args
}

/**
 * Traduit une entrée du registre neutre en outil `tool()` V1.
 *
 * @param definition outil gelé du registre
 * @param runtime capacités d'exécution, partagées par les 19 outils
 * @returns outil enregistré auprès d'opencode
 * @throws si l'outil n'a pas de handler — erreur d'adaptateur, jamais de silence
 */
function toV1Tool(definition: ToolDefinition, runtime: AbortableRuntimeApi) {
  const run = definition.run
  if (run === undefined) {
    throw new Error(`weekly-advisor: outil sans handler : ${definition.name}`)
  }
  return tool({
    description: definition.description,
    args: buildArgs(definition.fields),
    // `context` reste facultatif : les tests de contrat appellent `execute`
    // avec le seul `input`, et un handler in-process s'en passe parfaitement.
    async execute(input: Readonly<Record<string, unknown>>, context?: ToolContext): Promise<string> {
      return run(input, runtime, context?.abort)
    },
  })
}

/**
 * Point d'entrée V1 : deux hooks de garde et les 19 outils.
 *
 * Le pré-flight reste **fail-closed** et son message est gelé
 * (`weekly_preflight rc=3 — …`) : il est repris tel quel par la documentation et
 * les tests de contrat.
 *
 * @param input contexte fourni par opencode (`worktree` et `directory` V1)
 * @returns hooks V1 enregistrés
 */
export const server: Plugin = async (input) => {
  // Un runtime par appel de `server()` : la racine est figée à la construction
  // et n'est plus réécrite par un autre serveur ni par un test.
  const runtime = new WeeklyRuntime({
    location: {
      entrypointDirectory: ENTRYPOINT_DIRECTORY,
      v1Worktree: input?.worktree,
      v1Directory: input?.directory,
    },
  })
  return {
    "command.execute.before": async (hook) => {
      if (hook.command !== "weekly-review") return
      runtime.preflightOrThrow()
    },
    "chat.message": async (hook) => {
      if (hook.agent !== "weekly-advisor") return
      runtime.preflightOrThrow()
    },
    tool: Object.fromEntries(
      TOOL_REGISTRY.map((definition) => [definition.name, toV1Tool(definition, runtime)] as const),
    ),
  }
}
