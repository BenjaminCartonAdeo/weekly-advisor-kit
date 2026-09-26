/**
 * weekly-advisor — types neutres du contrat d'outils.
 *
 * Volontairement **sans dépendance** : ni `@opencode-ai/plugin`, ni `node:*`.
 * Ces types décrivent la forme du contrat (V1) afin que le registre d'outils,
 * les adaptateurs V1 et V2, et les tests de caractérisation partagent une seule
 * source de vérité. Toute logique d'exécution passe par {@link RuntimeApi}.
 *
 * Portée : contrat outil V1 actuel — 19 outils, 18 sous-commandes CLI appelées
 * par ces outils. Toute divergence avec `scripts/fixtures/weekly-advisor-tool-contract.json`
 * doit faire échouer `scripts/tests/plugin-tool-contract.test.mjs`.
 */

/** Primitives exposées par `tool.schema.*` dans le contrat V1. */
export type PrimitiveKind = "string" | "boolean" | "number" | "enum";

/** Un champ d'argument d'outil : nom, primitive, caractère obligatoire, aide. */
export interface FieldSpec {
  readonly name: string;
  readonly kind: PrimitiveKind;
  readonly required: boolean;
  readonly description: string;
  /** Valeurs admises — renseigné uniquement pour `kind === "enum"`. */
  readonly values?: readonly string[];
}

/** Emplacement résolu du moteur Python + config, dérivé du worktree. */
export interface EngineLoc {
  readonly engine: string;
  readonly python: string;
  readonly config: Readonly<Record<string, unknown>>;
  readonly outputDir: string;
  readonly configPath: string;
}

/** Résultat du pré-flight déterministe. `rc !== 0` ⇒ aucun run. */
export interface PreflightResult {
  readonly rc: 0 | 3;
  readonly worktree: string;
  readonly engine_ok: boolean;
  readonly python_ok: boolean;
  readonly config_ok: boolean;
  readonly py_count: number;
  readonly message?: string;
}

/** Payload JSON transporté par fichier temporaire (jamais dans argv). */
export interface StagedPayload {
  readonly file: string;
  readonly cleanup: () => void;
}

/** Détail d'un finding `harness-eval skill-verify`. */
export interface SkillVerifyDetail {
  readonly rule?: string;
  readonly severity?: string;
  readonly message?: string;
  readonly suggestion?: string;
}

/**
 * Verdict de la gate de portabilité. `unusable` = environnement défaillant :
 * refus mandatory (jamais de faux vert).
 */
export type PortabilityOutcome =
  | { readonly kind: "blocked"; readonly findings: readonly string[] }
  | { readonly kind: "pass"; readonly warnings: readonly string[] }
  | { readonly kind: "ignored"; readonly reason: string }
  | { readonly kind: "unusable"; readonly reason: string }
  | { readonly kind: "skipped"; readonly reason: string };

/**
 * Capacités d'exécution fournies à un outil. Les implémentations V1 et V2
 * injectent ces primitives ; la logique d'outils reste agnostique du runtime.
 */
export interface RuntimeApi {
  /** Racine du kit (résolue depuis le plugin, `WEEKLY_KIT_ROOT`, ou le contexte). */
  readonly worktree: string;
  /** Localise moteur + interpréteur + config + output dir. */
  resolveEngine(): EngineLoc;
  /** Ancre explicite, ou ancre glissante lue/créée/rafraîchie. */
  anchorArg(anchor: string | undefined): string;
  /** Exécute une sous-commande du moteur. Rejette avec le message d'erreur gelé. */
  runCli(args: readonly string[], timeoutMs: number): Promise<string>;
  /** Écrit un payload JSON dans un fichier temporaire et rend son nettoyage. */
  stageJsonPayload(payload: string, label: string): StagedPayload;
  /** Gate `harness-eval skill-verify` sur un artefact, avant tout commit. */
  runPortabilityGate(
    file: string,
    kind: "skill" | "command",
  ): Promise<PortabilityOutcome>;
  /** Pré-flight déterministe du kit. */
  preflight(): PreflightResult;
}

/** Spécification déclarative d'un outil « 1 sous-commande → 1 appel CLI ». */
export interface SimpleToolSpec {
  readonly name: string;
  readonly description: string;
  readonly subcommand: string;
  /** Timeout du `runCli` correspondant, en ms. */
  readonly timeoutMs: number;
  /** `false` ⇒ aucun flag `--anchor` émis et aucun champ `anchor` exposé. */
  readonly anchored?: boolean;
  /** `true` ⇒ champ optionnel `lookback_days` exposé. */
  readonly lookback?: boolean;
}

/**
 * Outil gelé. `run` est absent tant que le runtime n'est pas construit
 * (Tâche 3 du plan) : `cliSubcommand === undefined` ⇒ outil in-process.
 */
export interface ToolDefinition {
  readonly name: string;
  readonly description: string;
  readonly fields: readonly FieldSpec[];
  /** Timeout de la sous-commande en ms ; `0` = in-process, aucun subprocess. */
  readonly timeoutMs: number;
  readonly anchored: boolean;
  /** Sous-commande CLI appelée ; absent pour un outil purement in-process. */
  readonly cliSubcommand?: string;
  readonly run?: (
    input: Readonly<Record<string, unknown>>,
    runtime: RuntimeApi,
    signal?: AbortSignal,
  ) => Promise<string>;
}
