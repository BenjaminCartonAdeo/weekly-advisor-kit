/**
 * weekly-advisor — adaptateur V2 (APIs structurelles).
 *
 * **Symétrique de {@link ./v1.ts}, jamais dépendant de lui.** Le point d'entrée
 * `.opencode/plugins/weekly-advisor.ts` charge ce module par `import()` dynamique
 * depuis `setup()` : un hôte V2 n'a donc jamais à résoudre le SDK V1, et ce
 * fichier n'importe **aucun** paquet `@opencode-ai/*` — les APIs V2 sont
 * décrites ici sous forme structurelle (aucun SDK V2 n'est en dépendance, aucun
 * typage ne peut donc en être dérivé : cf. `V2Context` plus bas et la couture
 * équivalente dans le point d'entrée).
 *
 * Comme l'adaptateur V1, il ne contient **aucune logique d'outil** : il traduit
 * le registre neutre ({@link TOOL_REGISTRY}) vers la forme attendue par
 * `ctx.tool.transform`. Noms, descriptions, champs, required, enums, timeouts et
 * argv restent définis à un seul endroit, donc le comportement est identique en
 * V1 et en V2. Correspondances figées par
 * `scripts/fixtures/weekly-advisor-tool-contract.json` (19 outils, ordre gelé).
 *
 * Trois différences avec V1, toutes dictées par la forme de l'hôte V2 :
 *
 * 1. **Admission par prompt, pas par commande.** Le pré-flight se déclenche sur
 *    `ctx.session.hook("prompt", …)` et filtre **strictement** sur
 *    `event.prompt.agents` contenant `weekly-advisor` : le texte du prompt n'est
 *    jamais inspecté (une mention de « weekly-advisor » dans une phrase ne
 *    déclenche rien) ni journalisé (aucun log de prompt, fichier ou secret —
 *    le callback ne lit que `agents`). Aucun `command` n'est enregistré ici :
 *    V1 le fait déjà, un doublon serait un conflit de commande côté hôte.
 * 2. **Schéma JSON explicite** au lieu des builders `tool.schema.*` : chaque
 *    outil porte `input: {type:"object", properties, required,
 *    additionalProperties:false}` — `required` est toujours présent, vide pour
 *    un outil sans champ obligatoire. `additionalProperties:false` est le point
 *    important : un champ non déclaré doit être refusé, pas accepté en silence.
 * 3. **Enregistrement différé.** `ctx.tool.transform` reçoit un callback qui doit
 *    être synchrone, bon marché et rejouable. Le runtime (donc la racine) est
 *    donc résolu **avant**, et le callback ne fait que construire des objets
 *    littéraux : aucun `fs`, aucun appel CLI, aucun `await`, aucun effet de bord
 *    ponctuel. Le même callback peut donc être rejoué par l'hôte sans coût ni
 *    divergence.
 *
 * **Métadonnées projet.** V1 ne connaît que `ctx.worktree` / `ctx.directory`, des
 * chaînes. En V2, `ctx.location.project` est un **objet** `{canonical?,
 * directory?}` : il est réduit structurellement ({@link projectDirectory}, `canonical`
 * d'abord) et passé au runtime **après** `location.directory`, conformément à
 * `WeeklyRuntime.rootPrecedence`. Une réduction par `typeof === "string"` jetterait
 * la forme réelle et ferait retomber la racine sur `cwd`.
 *
 * **Résultat et annulation.** `execute` est `async` et rend `{content: string}` —
 * l'enveloppe attendue par l'hôte V2, le texte brut du handler étant identique
 * à celui de V1. `context.signal` est transmis tel quel au handler neutre : il
 * tue le subprocess (`runCli`) et empêche tout répertoire de staging résiduel
 * (`stageJsonPayload`). Les erreurs et annulations remontent, jamais converties
 * en chaîne de succès.
 *
 * **Un runtime par appel de `setup()`**, comme en V1 : plus de variable module,
 * donc deux racines peuvent coexister, y compris pour des appels concurrents.
 */
import path from "node:path"
import { fileURLToPath } from "node:url"

import { TOOL_REGISTRY, type AbortableRuntimeApi } from "../tool-registry.ts"
import { WeeklyRuntime } from "../runtime.ts"
import type { FieldSpec, ToolDefinition } from "../types.ts"

// ---------------------------------------------------------------------------
// APIs V2 — structurelles, déclarées localement.
//
// Aucun paquet V2 n'est en dépendance : ces interfaces décrivent la surface
// réellement utilisée (prompt, transform, `input` + `execute`), et le contrat
// réel est vérifié par `scripts/tests/plugin-v2.test.mjs` via un faux contexte.
// `location`, `session` et `tool` sont facultatifs pour que l'absence de
// capability remonte une erreur d'adaptateur **nommée** plutôt qu'un `TypeError`
// obscur — même exigence que « pas de faux vert » côté V1.
// ---------------------------------------------------------------------------

/** Événement de prompt V2. Seul `prompt.agents` est lu, jamais `text`/`files`. */
export interface V2PromptEvent {
  readonly prompt: {
    readonly text?: string;
    readonly files?: readonly unknown[];
    readonly agents?: readonly string[];
    readonly skills?: readonly string[];
  };
}

/** Propriété JSON Schema d'un champ d'outil. */
export interface V2PropertySchema {
  readonly type: "string" | "boolean" | "number";
  readonly description: string;
  /** Présent pour un champ `enum` du contrat neutre ; `type` reste `"string"`. */
  readonly enum?: readonly string[];
}

/** Schéma d'entrée d'un outil V2 — objet fermé, `required` toujours présent. */
export interface V2InputSchema {
  readonly type: "object";
  readonly properties: Readonly<Record<string, V2PropertySchema>>;
  readonly required: readonly string[];
  readonly additionalProperties: false;
}

/** Enveloppe de résultat V2 : le texte brut du handler, sous `content`. */
export interface V2ToolResult {
  readonly content: string;
}

/** Contexte d'exécution fourni par l'hôte — porteur du signal d'annulation. */
export interface V2ToolContext {
  readonly signal?: AbortSignal;
}

/** Outil enregistré auprès de l'hôte V2. */
export interface V2ToolInput {
  readonly name: string;
  readonly description: string;
  readonly input: V2InputSchema;
  execute(
    input: Readonly<Record<string, unknown>>,
    context: V2ToolContext,
  ): Promise<V2ToolResult>;
}

/** Éditeur d'outils fourni par `ctx.tool.transform`. */
export interface V2ToolEditor {
  add(tool: V2ToolInput): void;
}

/** Registre d'outils V2. */
export interface V2ToolRegistry {
  transform(callback: (editor: V2ToolEditor) => void): Promise<void> | void;
  list(): Promise<readonly string[]>;
}

/** Emplacement fourni par l'hôte V2. */
export interface V2Location {
  readonly directory?: string;
  /**
   * Métadonnées projet — forme **structurelle** imposée par l'hôte : un objet,
   * pas une chaîne. Décrit comme `unknown` (aucun SDK V2 en dépendance) et
   * réduit par {@link projectDirectory}, jamais casté.
   */
  readonly project?: unknown;
}

/** Contexte V2 minimal requis par le kit. */
export interface V2Context {
  readonly location?: V2Location;
  readonly session?: {
    hook(
      name: "prompt",
      callback: (event: V2PromptEvent) => Promise<void> | void,
    ): Promise<void> | void;
  };
  readonly tool?: V2ToolRegistry;
}

// ---------------------------------------------------------------------------
// Constantes gelées.
// ---------------------------------------------------------------------------

/**
 * Racine du kit déduite de l'emplacement de cet adaptateur, soit quatre
 * remontées : `<racine>/.opencode/plugins/weekly-advisor/adapters/v2.ts` →
 * `adapters` → `weekly-advisor` → `plugins` → `.opencode` → `<racine>`.
 *
 * Même calcul et même précédence que V1 (`WEEKLY_KIT_ROOT` d'abord, puis
 * l'emplacement du point d'entrée s'il porte un moteur, puis les sources du
 * contexte) : un `ctx.location` arbitraire ne peut donc pas détourner un hôte V2
 * vers un kit différent de celui qui a chargé le plugin.
 */
const ENTRYPOINT_DIRECTORY = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "..",
  "..",
  "..",
  "..",
)

/** Agent unique déclenchant le pré-flight — gelé, identique à V1. */
const AGENT = "weekly-advisor"

/**
 * Type JSON d'une primitive du contrat neutre. `enum` est un `string` contraint :
 * le contrat n'expose que des valeurs textuelles.
 */
const JSON_TYPES: Readonly<Record<FieldSpec["kind"], V2PropertySchema["type"]>> = {
  string: "string",
  boolean: "boolean",
  number: "number",
  enum: "string",
}

// ---------------------------------------------------------------------------
// Réduction structurelle des métadonnées projet V2.
// ---------------------------------------------------------------------------

/** Chaîne non vide — un chemin vide ne peut pas désigner un répertoire. */
function pathValue(value: unknown): string | undefined {
  return typeof value === "string" && value.length > 0 ? value : undefined
}

/**
 * Extrait le répertoire projet des métadonnées V2 — `canonical` d'abord,
 * `directory` en repli.
 *
 * `ctx.location.project` est un **objet** (`{ canonical?, directory? }`), pas une
 * chaîne : un test `typeof === "string"` le réduirait à `undefined` et ferait
 * tomber la racine sur `cwd`, en perdant le projet courant. La forme n'étant
 * pas garantie par un SDK en dépendance, la réduction est **structurelle** :
 * objet non nul, hors tableau, puis lecture des deux clés. Toute autre forme
 * (`undefined`, `null`, chaîne, tableau) donne `undefined` sans lever — un hôte
 * exotique ne doit pas empêcher l'enregistrement des 19 outils.
 *
 * Exporté pour être figé par `scripts/tests/plugin-v2.test.mjs` : le repli est
 * le seul point où la forme réelle de `project` est lisible, et il n'est pas
 * observable de bout en bout (le point d'entrée, prioritaire, porte toujours le
 * moteur dans le dépôt).
 *
 * @param project valeur brute de `ctx.location.project`
 * @returns répertoire canonique, sinon `directory`, sinon `undefined`
 */
export function projectDirectory(project: unknown): string | undefined {
  if (typeof project !== "object" || project === null || Array.isArray(project)) return undefined
  const metadata = project as Record<string, unknown>
  return pathValue(metadata["canonical"]) ?? pathValue(metadata["directory"])
}

// ---------------------------------------------------------------------------
// Traduction du contrat neutre.
// ---------------------------------------------------------------------------

/**
 * Copie les valeurs d'un `enum` du contrat.
 *
 * Duplique le garde-fou de V1 plutôt que de l'exporter : chaque adaptateur doit
 * rester lisible seul, et un `enum` sans valeur ne peut pas être silencieusement
 * traité comme du texte libre.
 *
 * @param values valeurs déclarées par le contrat
 * @returns copie du tuple
 * @throws si le contrat annonce un `enum` sans valeurs
 */
function enumValues(values: readonly string[] | undefined): readonly string[] {
  if (!values?.length) throw new Error("weekly-advisor: enum sans valeurs")
  return [...values]
}

/**
 * Traduit un {@link FieldSpec} du contrat neutre en propriété JSON Schema.
 *
 * @param field spécification du champ
 * @returns propriété `{type, description}` (+ `enum` pour un champ contraint)
 */
function propertySchema(field: FieldSpec): V2PropertySchema {
  const base = {
    type: JSON_TYPES[field.kind],
    description: field.description,
  } as const
  return field.kind === "enum" ? { ...base, enum: enumValues(field.values) } : base
}

/**
 * Traduit les champs d'un outil en schéma d'entrée V2, dans l'ordre du contrat.
 *
 * `required` est toujours présent (vide si aucun champ obligatoire) et
 * `additionalProperties` est `false` : l'hôte rejette donc un champ non
 * déclaré, là où V1 laissait le builder de schéma décider.
 *
 * @param definition outil gelé du registre
 * @returns schéma `input` de l'outil
 */
function toV2Schema(definition: ToolDefinition): V2InputSchema {
  const properties: Record<string, V2PropertySchema> = {}
  const required: string[] = []
  for (const field of definition.fields) {
    properties[field.name] = propertySchema(field)
    if (field.required) required.push(field.name)
  }
  return { type: "object", properties, required, additionalProperties: false }
}

/**
 * Traduit une entrée du registre neutre en outil V2.
 *
 * Fonction **pure** : aucun `fs`, aucun subprocess, aucun `await`. Elle capture
 * le runtime déjà résolu et rend un objet littéral — c'est ce qui rend le
 * callback de `transform` rejouable sans effet de bord.
 *
 * @param definition outil gelé du registre
 * @param runtime capacités d'exécution, partagées par les 19 outils
 * @returns outil enregistré auprès de l'hôte
 * @throws si l'outil n'a pas de handler — erreur d'adaptateur, jamais de silence
 */
function toV2Tool(definition: ToolDefinition, runtime: AbortableRuntimeApi): V2ToolInput {
  const run = definition.run
  if (run === undefined) {
    throw new Error(`weekly-advisor: outil sans handler : ${definition.name}`)
  }
  return {
    name: definition.name,
    description: definition.description,
    input: toV2Schema(definition),
    // `context` reste tolérant à l'absence : un hôte qui appellerait `execute`
    // avec le seul `input` obtient `signal` indéfini, ce que les 19 handlers
    // acceptent déjà (le signal est facultatif de bout en bout).
    async execute(
      input: Readonly<Record<string, unknown>>,
      context: V2ToolContext,
    ): Promise<V2ToolResult> {
      return { content: await run(input, runtime, context?.signal) }
    },
  }
}

// ---------------------------------------------------------------------------
// Point d'entrée V2.
// ---------------------------------------------------------------------------

/**
 * Point d'entrée V2 : garde de pré-flight sur prompt + les 19 outils.
 *
 * Le pré-flight reste **fail-closed** et son message est gelé
 * (`weekly_preflight rc=3 — …`), identique à V1 : même texte, donc mêmes
 * documentation et mêmes tests de contrat.
 *
 * @param ctx contexte fourni par l'hôte V2 (`location`, `session`, `tool`)
 * @returns résolution de l'enregistrement
 * @throws si `session.hook` ou `tool.transform` manquent, ou si le pré-flight échoue
 */
export const setup = async (ctx: V2Context): Promise<void> => {
  // Gardes de capability : appelées en méthode (`ctx.session.hook(…)`) et non
  // détachées, pour qu'un hôte реалиant `hook`/`transform` sur état privé
  // (`#fields`, WeakMap) reste valide. `?.` tolère un contexte absent ; un
  // `TypeError` obscur est remplacé par une erreur d'adaptateur nommée.
  if (typeof ctx?.session?.hook !== "function") {
    throw new Error("weekly-advisor: contexte V2 sans session.hook")
  }
  if (typeof ctx?.tool?.transform !== "function") {
    throw new Error("weekly-advisor: contexte V2 sans tool.transform")
  }

  // Racine résolue ICI, avant l'enregistrement : le callback de `transform` doit
  // rester pur, or résoudre une racine touche le disque. Précédence inchangée
  // (`WeeklyRuntime.rootPrecedence`) : `entrypointDirectory` d'abord, puis
  // `location.directory`, puis les métadonnées projet réduites par
  // {@link projectDirectory} — un projet V2 ne peut donc pas détourner un hôte
  // vers un kit différent de celui qui a chargé le plugin.
  const runtime = new WeeklyRuntime({
    location: {
      entrypointDirectory: ENTRYPOINT_DIRECTORY,
      v2Directory: ctx.location?.directory,
      v2ProjectDirectory: projectDirectory(ctx.location?.project),
    },
  })

  // Admission : uniquement l'agent `weekly-advisor`. Le texte du prompt n'est ni
  // lu ni journalisé — un prompt qui mentionne le kit sans l'appeler reste donc
  // gratuit, et aucun contenu utilisateur ne part dans un log.
  await ctx.session.hook("prompt", async (event) => {
    if (!event?.prompt?.agents?.includes(AGENT)) return
    runtime.preflightOrThrow()
  })

  // Enregistrement : 19 outils, ordre du registre. Le callback ne fait que
  // construire des objets — rejouable, synchrone, sans coût.
  await ctx.tool.transform((editor) => {
    for (const definition of TOOL_REGISTRY) editor.add(toV2Tool(definition, runtime))
  })
}
