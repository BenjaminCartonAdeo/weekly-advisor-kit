# microsoft/ai-engineering-coach → pistes pour weekly-advisor-kit

> Rapport d'étude **read-only**, destiné à être repris par un autre agent (implémentation ou arbitrage).

## Métadonnées

| Champ | Valeur |
|---|---|
| Source | https://github.com/microsoft/ai-engineering-coach (MIT) |
| Clone local | `/tmp/opencode/ai-engineering-coach` (shallow `--depth 1`, ~14 Mo, 447 fichiers) |
| Date d'étude | 2026-09-14 |
| Méthode | lecture ciblée + 2 subagents `explore` parallèles (moteurs d'analyse / inventaire docs+skills) |
| Cible | `weekly-advisor-kit` — moteur `.opencode/plugins/weekly-advisor-engine/weekly_telemetry_aggregator/` |
| Statut | **pistes non implémentées** — aucune modification du kit n'a été faite |
| Contexte kit | pipeline `run → quality-audit → watch → drafting → harness-remediate → insights → report` ; ~660 tests ; branche au moment de l'étude : `feat/copilot_compat` |

Si le clone a disparu : `git clone --depth 1 https://github.com/microsoft/ai-engineering-coach /tmp/opencode/ai-engineering-coach`.

---

## 0. TL;DR — les 5 pistes à plus fort levier

1. **P1 — Règles d'audit déclaratives `.md` + tests inline** : externaliser les détecteurs de `weekly-quality-audit` en fichiers versionnés (frontmatter + mini-DSL + bloc `test`). Ajouter une règle ne touche plus le code.
2. **P3 — Skill Finder par fingerprint O(n)** : algorithme complet à transposer dans le moteur (`weekly-drafting` / futur détecteur « prompts répétés »), avec draft de skill auto et estimation de temps gagné.
3. **P4 — Statuts de couverture des coûts** (`complete/partial/pending/no-data/missing` + `% manquant`) : rend les chiffres du rapport honnêtes (facturé vs estimé), complément direct du fix copilot-cli.
4. **P5 — Scores composites 0-100 + deltas WoW/MoM + heatmaps** : ossature chiffrée 100 % déterministe du rapport HTML (aucun LLM).
5. **P2 — Rule Playground + Data Explorer** : commande de debug qui évalue une règle contre les JSON du run courant → tune les seuils d'`audit-candidates` sur données réelles et détecte la dérive des parsers.

---

## 1. Ce qu'est AEC (résumé)

Extension VS Code (MIT, Microsoft) qui **parse les logs locaux** de sessions d'assistants IA et produit un dashboard **Observe / Measure / Improve / Level-Up**, 100 % local, zéro télémétrie sortante.

- **Harnais supportés** : VS Code Agent (+Server/Insiders), **Copilot CLI**, Claude Code, Codex, OpenCode (via sqlite `opencode*.db`), Xcode.
- **Socle** : 11 analyseurs `src/core/analyzer-*.ts`, **45 règles anti-patterns déclaratives** (`src/core/rules/*.md` + mini-DSL interprété), scores 0-100 avec tendances WoW/MoM, Skill Finder par clustering, chat `@aicoach` (12 tools), exports PNG / Markdown / JSON.
- **Gouvernance** : les règles custom passent par un trust `pending → review → approve → reload` (`src/core/rule-trust.ts`) ; 3 couches (built-in / `~/.ai-engineer-coach/rules/` / `<workspace>/.ai-engineer-coach/rules/`).
- **Gates** : `npm run check` (typecheck+lint+spellcheck+knip+test), `check-size` (ext ≤ 2 Mo, vsix ≤ 5 Mo).

**Ancrage kit (vérifié dans `README.md` du kit)** : moteur Python 100 % déterministe, zéro LLM sur les chiffres ; sources = SQLite OpenCode + JSONL Claude + sessions Copilot VS Code via `session_sources`, extensible ; coûts estimés via `cost_rate_usd_per_mtok` + alertes budget ; étape `self-cost` existante.

---

## 2. Carte AEC pour naviguer les preuves

| Zone | Fichiers |
|---|---|
| Analyseurs | `src/core/analyzer.ts` (façade, 11 sous-analyseurs, cache, warm-up worker) ; `analyzer-{base,consumption,context,dashboard,flow,images,insights,patterns,production,timeline,workflows}.ts` |
| Moteur anti-patterns | `src/core/rules/*.md` (45) ; `rule-loader.ts` ; `detector-registry.ts` (≥ 250 l.) ; `rule-pipeline.ts` ; `metric-engine.ts` ; `dsl/{lexer,parser,interpreter,schema}.ts` ; `detectors/scoring.ts` ; `types/rule-types.ts` |
| Ingestion | `src/core/parser.ts` (6 phases) + `parser-{vscode,vscode-cli,vscode-files,vscode-request,claude,codex,opencode,xcode,harnesses,shared}.ts` ; `edit-loc-diff.ts` ; `edit-tool-diff.ts` ; `cache.ts` + `*-worker.ts` |
| UI | `src/webview/page-{dashboard,timeline,output,patterns,antipatterns,rule-editor,rule-playground,data-explorer,skills,workflows,context-mgmt,insights,sdlc,achievements,learning,share,burndown,github-app,image-gallery}.ts` |
| Chat/exports | `src/chat/{participant,system-prompt}.ts` ; `src/mcp/tools.ts` (12 tools) ; `src/canvas/host.ts` ; `src/summary-export-vscode.ts` |
| Docs d'autorité | `docs/AUTHORING_RULES.md`, `docs/content/improve/{anti-patterns,rule-editor,rule-playground,data-explorer,skill-finder,context-health}.md` |

---

## 3. Pistes détaillées

Chaque piste : **quoi** → **mécanisme AEC (preuve)** → **transposition kit** → **effort / risque**.

### P1 — Règles d'audit en `.md` versionnées, avec tests inline

- **Quoi** : sortir les détecteurs/heuristiques de `weekly-quality-audit` du code Python pour des fichiers de règles versionnés, testables.
- **Mécanisme AEC** : `src/core/rules/lazy-prompting.md:1-41` = frontmatter YAML (`id, name, group, severity high|medium|low, scope requests|sessions|both, thresholds, patterns, fileTypes, extends, version, tags`) + sections `# Description / When Triggered ({{count}},{{pct}},{{extra.*}}) / How to Improve / Examples` + bloc ```detect``` (`scan / match / aggregate / check / examples / severity`) + bloc ```test``` (`fixtures -> triggered|clean`) exécuté par `npm test`. Chargement : `rule-loader.ts:84-113` (built-in `dist/rules/` ou `src/core/rules/`, + couches perso/projet) ; exécution : `detector-registry.ts:231-250` (`buildRegistry` → `parsePipeline` → `executePipeline` → `checkPipelineTrigger`) ; DSL compilé dans `rule-pipeline.ts:28-36` via `dsl/` (lexer/parser/interpreter + `safe-regex`).
- **Transposition kit** : un répertoire de règles (ex. `<engine>/rules/*.md|yaml`), un évaluateur en Python, un bloc de tests inline interprété par pytest (ou un runner paramétré sur les fixtures). Chaque rule produit un finding au **format AEC uniforme** (voir P5/annexe) : `{id, severity, group, occurrences, description{{pct}}, suggestion, examples, details[{timestamp, workspace, sessionId, message, stats}], weeklyHist}` — `detector-registry.ts:140-185`.
- **Effort** : moyen (DSL volontairement simple : scan/match/aggregate/check suffit). **Risque** : faible — la sortie reste observation-only.

### P2 — Rule Playground + Data Explorer (debug des seuils)

- **Quoi** : une commande qui évalue une expression/une règle contre les données du run courant et affiche distributions + tailles d'échantillon.
- **Mécanisme AEC** : `docs/content/improve/rule-playground.md` = REPL du **même runtime** que le moteur (`scope requests|sessions`, `Evaluate` → résultat + nombre de lignes matchées, filtres date/workspace actifs) ; catalogue de champs typés (`dsl/schema.ts`) + catalogue de fonctions (`length, contains, matches, hour, dayOfWeek, countWhere, flatCount, someWhere, ratio, …`) + 10 métriques primitives + 10 `.metric.md`. `data-explorer.md` = 2 colonnes `SessionRequest`/`Session`, **distribution (top valeurs / min-max) + sample size (non-vides) par champ**, respectant les filtres.
- **Transposition kit** : sous-commande debug (ex. `weekly_doctor`-like) qui charge les JSON du run (`weekly-summary-*.json`, `weekly-audit-candidates-*.json`, `weekly-harness-digest-*.json`) et affiche pour chaque champ-clé sa distribution + son taux de remplissage.
- **Effort** : faible-moyen. **Risque** : faible. **Bénéfice immédiat** : détecter la **dérive de parsers** (champ présent mais vide selon le harnais) et tuner `audit_max_sessions`, seuils de coût, etc., sur données réelles au lieu d'intuitions.

### P3 — Skill Finder : clustering O(n) de prompts répétés → draft auto

- **Quoi** : remplacer/doubler la détection « prompts répétés » du moteur par l'algo AEC, et générer un draft de skill + estimation de gain.
- **Mécanisme AEC** (`src/core/analyzer-workflows.ts:40-51,149-222`) :
  1. collecte des prompts `len ≥ 15`, exclus par `isNoise()` (contexte injecté par le harnais, séparateurs `═─━=-_*` ×≥10, `^system`, `len > 2000`, `continue/try again/yes/no/cancel/abort/stop/retry`, `continue to iterate` si len < 80, `^[yn.!?]{≤3}`) ;
  2. normalisation (lowercase, code → `CODE`, quotes → `STR`, paths → `PATH`, nombres → `NUM`, ponctuation → espace) puis tokenisation (stop-list ~60 mots, tokens `len > 1`) ;
  3. **fingerprint = 4 premiers tokens triés joints par `|`**, regroupement par bucket map → **O(n)** ;
  4. garde `occurrences ≥ 3` (`MIN_OCCURRENCES`), tri desc, cap **200 clusters** (`MAX_CLUSTERS`) ; canonique = le plus court ≥ 20 chars, label tronqué 80 ;
  5. sortie par cluster : `occurrences`, sessions distinctes, workspaces, harnais, `cancelRate`, `avgCorrectionTurns`, `firstSeen/lastSeen`, ≤ 5 exemples, **`skillDraft` markdown** (`# Skill / When to use / Steps / Example prompts`, 3 exemples ≤ 120 c), **`estimatedTimeSavedMins = repetitions × 2`**.
- **Note** : `_SIMILARITY_THRESHOLD = 0.55` est du **code mort** non utilisé — ne pas le recopier par mimétisme.
- **Transposition kit** : l'étape `weekly-drafting` (+ `weekly_draft_candidates`) gagnerait un détecteur de répétition inter-sessions alimentant `skill-candidate` / `command-candidate`, avec matching contre le catalogue existant → input pour `weekly-coherence-review` (déjà appelé par `weekly_skill_curate`).
- **Effort** : faible (algo court, pur texte). **Risque** : faible.

### P4 — Statuts de couverture des coûts (facturé vs estimé)

- **Quoi** : chaque session/agrégat porte un **statut de complétude du coût** + un pourcentage manquant, plutôt qu'un nombre ambigu.
- **Mécanisme AEC** : `analyzer-consumption.ts:12-44,80-87` — statuts `complete / partial / pending / no-data / missing` + `computeMissingPct` ; l'UI affiche un **lower bound** assumé. Le burndown (`analyzer-consumption.ts:277-339`) projette fin de cycle avec statuts `on-track / warning / over-budget`.
- **Transposition kit** : ajouter au résumé et à `self-cost` (et au rapport) un statut par session : **réel facturé** vs **estimé au taux** vs **prix manquant** vs **tokens manquants**, + `%` manquant. Se branche directement sur le fix copilot-cli (coût nano = facturé réel vs `cost_rate_usd_per_mtok` = estimation).
- **Effort** : faible. **Risque** : faible. **Pré-requis** : fix copilot-cli (sinon le statut mentirait sur la source).

### P5 — Scores composites + WoW/MoM + heatmaps (ossature du rapport)

- **Mécanisme AEC** : `detectors/scoring.ts:44-50` + `analyzer-patterns.ts:260-318` → score groupe = `100 × (1 − Σ pénalités / (nbDétecteurs × 12))`, pénalités high=12 / medium=7 / low=3 (`types/rule-types.ts:41-47`), avec `wowPct/momPct` ; pénalités par requête (prompt < 30 c +1, sans file-ref +0.5, cancel +1, nuit +0.3, code non relu +0.5, sans outil +0.3). Scores dérivés : `analyzer-flow.ts:41-68` (flow = rapidité 40 % + latence médiane 30 % + durée 15 % pic ~40 min + densité 15 %, seuils deep ≥ 70 / moderate ≥ 45 / shallow ≥ 25 / fragmented) ; `analyzer-context.ts:320-342` (contexte = util − saturation × 0.3 − compaction) ; `analyzer-insights.ts:88-126` (maturité de prompt 5 dims → grade A ≥ 80 … F) ; `analyzer-timeline.ts:293-308` (équilibre de vie). Affichages : heatmap 7×24 (`analyzer-dashboard.ts:455-503`), calendrier streaks, matrice règle × workspace, treemap.
- **Transposition kit** : KPI déterministes du rapport HTML + colonne delta WoW (le kit a déjà `weekly_insights` pour deltas/alertes — c'est le bon point d'accroche).
- **Effort** : moyen (choisir 3-4 scores, pas 8). **Risque** : faible (0 LLM).

### P6 — Classifieurs déterministes (intent / spec-driven / relecture / maturité)

- **Mécanisme AEC** : `analyzer-insights.ts:84-118,329-378` — intent Planning/Debug/Review/Explore par regex + signaux code → Implementation ; spec-driven (fichier `.md/spec/prd`, mots-clés `must/should/ensure`, listes markdown, plan-mode) ; **production review** : `gap > 30 s après du code IA = relu` ; maturité de prompt 5 dimensions.
- **Transposition kit** : nouvelles catégories de findings avec exemples sourcés, compatibles contrats anti-hallucination (`weekly-quality-audit` fait déjà de la paraphrase stricte avec findings liés à la commande lanceuse).
- **Effort** : faible-moyen.

### P7 — Burndown budgétaire (projection fin de cycle)

- **Mécanisme AEC** : `analyzer-consumption.ts:277-339` — pace idéale budget → 0 vs conso réelle + projection, statuts `on-track/warning/over-budget` ; budgets par SKU dans `constants.ts` (1 crédit = $0.01).
- **Transposition kit** : projection fin de mois dans les alertes budget existantes (config kit : coûts estimés via `cost_rate_usd_per_mtok` + alertes budget). Alerte **avant** dépassement.
- **Attention** : chez AEC le burndown est **désactivé** (`FF_TOKEN_REPORTING_ENABLED = false`, billing GitHub non vérifié) — la leçon transférable est le **gate** : n'afficher un budget que si la source est facturée/vérifiée.

### P8 — Jumeau JSON du rapport + carte partageable

- **Mécanisme AEC** : `src/summary-export-vscode.ts` + `docs/content/level-up/share.md` → stat card PNG + export **Markdown + JSON jumeaux**, copie presse-papier, régénération.
- **Transposition kit** : exporter le rapport hebdo aussi en JSON machine-readable (en plus du `.md`) → archivage, diff inter-semaines, requêtage. Faible coût, forte valeur pour l'automatisation.
- **Effort** : faible.

---

## 4. À NE PAS copier

- **Gamification** (`achievements.md`, XP, quizzes `learning.md`) : hors-sujet pour une revue hebdomadaire.
- **Burndown « allumé » par défaut** : chez eux il est désactivé faute de billing vérifié — reprendre le mécanisme **avec** le gate.
- **Reproduire `_SIMILARITY_THRESHOLD`** (code mort) et tout mimétisme d'API sans vérifier l'usage réel.
- **Le README de jonmagic** (voir annexe pré-requis) : le schéma Copilot CLI **varie** — ne pas figer les tables sans vérification.

## 5. Pré-requis / questions ouvertes avant implémentation

1. **Localisation des données Copilot CLI** : la DB `~/.copilot/session-store.db` (tables `sessions/turns/assistant_usage_events`) et/ou les logs `~/.copilot/logs/*.log` ? Une source tierce (voir annexe) affirme que la télémétrie d'usage est écrite en **JSON dans les logs**, pas dans la DB. À trancher **sur une machine avec le CLI installé**.
2. **Divergence de schéma** : au moins un projet tiers listant les tables de `session-store.db` ne mentionne **pas** `assistant_usage_events` → prévoir variantes + dégradation explicite (cf. fix copilot-cli §6).
3. **Périmètre des scores** : combien de rubriques le rapport doit-il porter ? (3-4 suffisent ; au-delà, coût de maintenance.)
4. **Trust des règles** : reprendre le modèle AEC `pending → review → approve → reload` ou rester sur la gouvernance actuelle du kit ?

## 6. Annexe — preuves citées (chemins relatifs au clone AEC sauf mention)

- Règles : `src/core/rules/lazy-prompting.md:1-41` ; `docs/AUTHORING_RULES.md:61-70` ; `rule-loader.ts:84-113` ; `detector-registry.ts:140-185,231-250` ; `rule-pipeline.ts:28-36` ; `types/rule-types.ts:41-47` ; `detectors/scoring.ts:44-50`.
- Score/contexte/flow/insights : `analyzer-patterns.ts:17-19,23-42,227-250,260-318,393-413` ; `analyzer-context.ts:134-161,320-342,468-614,788-829` ; `analyzer-flow.ts:41-68,103-133,200-212,228-275` ; `analyzer-insights.ts:32-78,84-126,329-378,411-479,597-607`.
- Coûts : `analyzer-consumption.ts:12-44,54-71,80-87,277-339,635-710,848-877` ; `constants.ts:10-73` (SKU/rates) ; `github-app-issue-credits.ts:68` (`SUM(total_nano_aiu)`) ; `helpers.ts:306-324` (« Returns credits, not dollars », « 1 credit = $0.01 »).
- Production/LoC : `analyzer-production.ts:32-78` ; `edit-loc-diff.ts`.
- Workflows/Skill Finder : `analyzer-workflows.ts:40-51,100-117,149-168,171-222,220-242`.
- Dashboard/heatmaps : `analyzer-dashboard.ts:16-45,162-217,349-371,455-503` ; `analyzer-timeline.ts:17-43,293-308`.
- Ingestion : `parser.ts:70-77` ; `cache.ts:125-127,337-383,443-469` ; `parser-{vscode,claude,codex,opencode,xcode,harnesses}.ts`.
- Chat/MCP/exports : `chat/participant.ts:28-33,115-162` ; `chat/system-prompt.ts:13-42` ; `mcp/tools.ts:50-55,74-189` ; `canvas/host.ts:6-11,39-66,218-239` ; `summary-export-vscode.ts`.
- Sources externes (échelle des coûts Copilot) : docs.github.com « Models and pricing for GitHub Copilot » (1 AI credit = $0.01 USD) ; skill tiers `trsdn/copilot-usage-report` (AIU = `total_nano_aiu/1e9`, USD = AIU/100) ; `jonmagic/copilot-sessions` (tables de `session-store.db`).
