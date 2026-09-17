# Plan — OpenSpace quick-wins sans embedding (A1 + A3 parallèles)

> **Date:** 2026-09-17  
> **Source étude:** https://github.com/HKUDS/OpenSpace (Skill Management Layer)  
> **Kit cible:** `weekly-advisor-kit` — moteur `weekly_telemetry_aggregator` Python 3.11, 19 tools `weekly_*`, pipeline Vague T/V/H + K audits + D/I/C + 2.5 curation + tail  
> **Statut:** décisions utilisateur figées §0, plan build autorisé

---

## 0. Décisions utilisateur figées

| # | Décision | Valeur retenue |
|---|----------|----------------|
| 1 | Clef embedding dispo ? | **Non** → BM25-only stdlib, pas d'appel `text-embedding-3-small`, pas de `numpy`/`openai`/`rank_bm25` |
| 2 | Seuil promotion `provisional → trusted` | **2 succès indépendants** (préco §1) |
| 3 | Plafond drafting `max_candidates_per_run` | **3 → 5** |
| 4 | Priorités | **A1 + A3 en parallèle** (disjoints fichiers) |

---

## 1. Préco seuil promotion : 2 succès indépendants

### Référence OpenSpace

`openspace/skill_engine/store.py:__init__(trust_promotion_min_independent_successes=2)`  
`store.py:_record_trust_observation_locked()` :
- `failure` → démotion immédiate `trusted → provisional` + event `trust_demoted`
- `success` si `provisional` + `successes_since_last_failure >= threshold` → `provisional → trusted` + event `trust_promoted`
- `enabled` orthogonal à `trust_state` (toggle réutilisation sans changer révision/`is_active`)
- `observation_id = task:{task_id}` sinon `skill-origin:{skill_id}` (`evolver.py:_record_origin_trust`)

### Comparatif seuils

| Seuil | Avantage | Inconvénient | Verdict |
|-------|----------|--------------|---------|
| **1** | Rapide (1 semaine) | Faux positif élevé, 1 run chanceux promeut pattern fragile ; contredit retention `watch-memory` 26 semaines (`config.py:129`) et `TTL 90j` (`curation.py`) | Rejeté — bruit |
| **2** | Équilibre: 2 runs = 14j preuve indépendante, aligne défaut OpenSpace, compatible `audit_max_sessions=8` (`candidates.py:127`), `TTL 90j/3 runs` | Léger délai vs 1 | **Choisi** |
| **3** | Très sûr | 3 semaines mini → trop lent cadence hebdo, bloque `max_candidates=5` → file attente 15 candidates → goulot, freine boucle apprentissage | Rejeté — velocity |

### Pourquoi 2 ici

- Cadence hebdo → 2 succès = 14j preuve, compatible `insights.never_loaded_runs_threshold=8` (`config.py:94`) et `stale_days 90`.
- Asymétrie voulue : **1 échec suffit à démotion** (sécurité), 2 succès pour promotion (confiance).
- `enabled` orthogonal permet désactiver sans perdre historique (comme `SkillRecord.enabled` OpenSpace).
- Rendre configurable : `TelemetryConfig.trust_promotion_min_successes` défaut 2, parsé fail-soft dans `config.py:_parse` ; override via config JSON `trust_promotion_min_successes`.
- Si besoin plus strict plus tard : passer à 3 via config sans migration.

---

## 2. Scope A1 + A3 (parallélisables)

### A1 — Ranking BM25-only (sans embedding)

**Contexte actuel :** scoring `watch_distill.py:144-207` somme pondérée `authority 25 + relevance 30 (substring `RELEVANCE_KEYWORDS` + `release_keywords`) + freshness 20 (linéaire 90j) + multi_source 15 + traction 10 (stars/50)`. Similarité skills `aggregator.py:218-233` via `difflib` + `SequenceMatcher` ratio ≥0.9. Pas de recherche sémantique.

**Objectif A1 :** remplacer substring/difflib par BM25 stdlib (même fallback qu'OpenSpace sans lib externe).

**Fichiers A1 :**
- Nouveau `weekly_telemetry_aggregator/skill_ranker.py` (stdlib only)
- Modifiés : `watch_distill.py:144-207`, `aggregator.py:140-233`, `candidates.py:overlaps_with`

**Détail A1 :**

1. **Nouveau `skill_ranker.py`** — port minimal d'`openspace/skill_engine/skill_ranker.py` :
   - `_tokenize = re.split(r"[^\w]+", lower)` identique OpenSpace
   - `SKILL_EMBEDDING_MAX_CHARS=12000`, `PREFILTER_THRESHOLD=10`, `BM25_CANDIDATES_MULTIPLIER=3` ignorés sans embedding → BM25 pur
   - `rank_bm25.BM25Okapi` non dispo → fallback token-overlap `|q ∩ doc| / |q|` (déjà fallback OpenSpace `skill_ranker.py`)
   - `text = name + description + body[:2000]` (frontmatter strippé) — cohérent MCP+cloud
   - Cache pickle `.weekly-advisor/cache/skill_ranker_cache.pkl` versioné `{version, model:"bm25-only", last_updated, cache}` comme OpenSpace `skill_embeddings_v1.pkl`
   - API : `bm25_score(query_tokens, doc_tokens)`, `rank(query, candidates, top_k)`, `invalidate_cache(skill_id)`

2. **`watch_distill.py:score_item` :**
   - Actuel `relevance = min(w_relevance, w_relevance * matches/len(keywords))` substring `watch_distill.py:178-181`
   - Nouveau : `relevance = bm25_score(query=RELEVANCE_KEYWORDS+extra_keywords, doc=name+description)` normalisé → `min(w_relevance, score_norm * w_relevance)`
   - Garder `DEFAULT_WEIGHTS` somme 100 `watch_distill.py:41`, `AUTHORITY_BY_SOURCE` `watch_distill.py:50`
   - `rank()` `watch_distill.py:270` garde tie-breaker `(-score.total, -published_ts, id)` déterministe

3. **`aggregator.py` :**
   - `_skill_similar_pairs` `218` : `difflib` sur `description+200 chars body` → BM25 similarity
   - `_prompt_repeat_groups` `140` : `SequenceMatcher` ratio ≥0.9 sur 100 chars → BM25 near-duplicate (bucket `len//16` conservé)

4. **`candidates.py:consolidate_candidates` :** enrichir détection `overlaps_with` via ranker BM25 au lieu de seul `difflib` (seuil similarité ≥0.8 existant `config.py:167` `skill_similarity_min`)

**Hors scope A1 :** embedding, API `resolve_embedding_api`, `numpy`, `openai`, hybrid re-rank.

### A3 — 4 compteurs + Tool penalty + provisional→trusted

**Contexte actuel :** `aggregator.py:536-559` seulement `load_count`, `sessions_used_in`, `skills_never_loaded`. `insights.py:400-455` flag `never_loaded ≥ threshold` binaire. `weekly-drafting` `SKILL.md:78-93` minte `skill_<sha8>` `candidates.py:49-55` avec `confidence: medium`, `load_count:0` mais pas de signal qualité post-déploiement. Pas de `selected/applied/completed/fallback`.

**Objectif A3 :** boucler qualité à la OpenSpace : 4 phases + tool reliability + lifecycle 2 états.

**Fichiers A3 :**
- Nouveaux : `weekly_telemetry_aggregator/tool_quality.py`, `weekly_telemetry_aggregator/skill_quality.py`
- Modifiés : `models.py`, `aggregator.py:536`, `candidates.py:49-124`, `curation.py`, `insights.py:400`, `config.py:191/322`, `weekly-telemetry-config.json:87-91`

**Détail A3 :**

1. **4 compteurs + `skill_phase_failed`** (OpenSpace `SkillRecord` `types.py:250-380` + `store.py` counters `total_selected/invocations/applied/completions/fallbacks`) :
   - `models.py` + `aggregator.py` : splitter `total_uses` → `total_selected`, `total_applied`, `total_completed`, `total_fallbacks`
   - Ajouter `skill_phase_failed_skill_ids: List[str]` (phase guidée échouée avant fallback tool-only → pas de crédit completion, comme `analyzer.py:_extract_skill_phase_failed_skill_ids`)
   - Incréments SQL atomiques (pas Python) comme `store.record_analysis`, idempotent `load_analyses_for_task(task_id)` check duplicates
   - Computed : `applied_rate, completion_rate, effective_rate, fallback_rate, total_uses = max(selections, invocations)` (OpenSpace `SkillRecord` computed)

2. **Sidecar `.skill_id`** (OpenSpace `registry.py:_read_or_create_skill_id` → `{name}__imp_{uuid8}`) :
   - `candidates.py:generate_skill_id` garde `skill_<sha256[:8]>` déterministe `candidates.py:49-55` + ajout `.skill_id` stable côté `registry` (lecture/création)
   - `SkillRecord.skill_id` mappe `skill_id → SkillMeta`, `get_skill_by_name()` fallback seulement

3. **Tool penalty 0.2-1.0** (OpenSpace `grounding/core/quality/types.py:80-115` + `manager.py`) :
   - Nouveau `tool_quality.py` : `ToolQualityRecord` clé `{backend}:{server}:{tool_name}` normalisée `_normalize_tool_dependency_key() → backend:default:tool`
   - Champs `total_calls, success_count, total_execution_time_ms, recent_executions[100]`, computed `success_rate, recent_success_rate, consecutive_failures, avg_execution_time_ms`
   - `penalty` :
     ```
     if total_calls < 3: 1.0
     elif recent_success_rate >= 0.4: 1.0
     else: base = 0.3 + (success_rate/threshold)*0.7  # linéaire 0.3-1.0
           if consecutive_failures >= 3: base -= min(0.3, (consec-2)*0.1)
           clamp [0.2, 1.0]
     quality_score = penalty
     ```
   - `ToolQualityManager` : `record_execution(tool,result,ms) → record_outcome(success,error[:500]) → add_execution(...)`, `global_execution_count`, `adjust_ranking([(tool,semantic_score)]) → semantic*penalty` re-sorted, `compute_adaptive_quality_weight()`, `get_quality_report()`, `get_problematic_tools(threshold=0.5, min_calls=5)`
   - Pondération `watch_distill.py:score_item` traction/authority par `penalty` si `found_via` contient tool mcp défaillant ; futur `harness-eval` rule scores pondérés (question ouverte §6)

4. **Lifecycle `provisional → trusted`** (OpenSpace `SkillTrustState` 2 états) :
   - Nouvelles skills : `trust_state=provisional`, `enabled=true`, `trust_successes=0`
   - Table `skill_trust_observations(skill_id, observation_id UNIQUE, outcome success|failure, task_id, session_id, source, evidence_refs_json, created_at)` — append-only
   - `observation_id = task:{task_id}` sinon `skill-origin:{skill_id}`
   - Promotion/démotion dans `curation.py:decide_actions` + `insights.py:compute` (alert `skill_quality_low`)

5. **Config :**
   - `config.py:191` `max_candidates_per_run: int = 3` → **5**
   - `config.py:322` `_get("max_candidates_per_run", ...)` parse fail-soft
   - `weekly-telemetry-config.json` racine `max_candidates_per_run: 5` (était défaut 3) + nouveau `trust_promotion_min_successes: 2`, `tool_quality_threshold: 0.4`
   - `config.py:117` `WatchDistillConfig` inchangé `top_n 30`, `quotas new12/improvable8/resurfaced5` `watch_distill.py:293`

**Dépendance A1↔A3 :** aucune — `watch_distill.py` relevance vs compteurs quality disjoints → parallélisable sans conflit.

---

## 3. Plan d'exécution (2 tracks parallèles + join)

### Track A1 (BM25) — ~1j

1. Créer `weekly_telemetry_aggregator/skill_ranker.py` (tokenize, BM25 fallback, rank, cache pickle versioné).
2. Modifier `watch_distill.py:score_item` → `skill_ranker.bm25_score` pour relevance (remplace substring matches).
3. Modifier `aggregator.py:_skill_similar_pairs` `218` + `_prompt_repeat_groups` `140` → BM25 similarity.
4. Tests : `tests/test_skill_ranker.py` (nouveau), `tests/test_watch_distill.py` update snapshots (déterministe `rank` tie-breaker `watch_distill.py:271`), `tests/test_aggregator.py` similar_pairs.
5. Vérif : `pytest` + `ruff check` ; `anchor` identique → `rank` identique (tri stable, `round6`).

### Track A3 (Quality) — ~1.5j parallèle

1. Créer `tool_quality.py` + `skill_quality.py` (dataclasses, SQLite `skill_quality.jsonl` append-only + decay).
2. Modifier `models.py` + `aggregator.py:536` compteurs 4 phases + `skill_phase_failed`.
3. Modifier `candidates.py:49` `.skill_id` + `provisional` lifecycle ; `consolidate_candidates` garde `action=patch/create` + `target_skill_id`.
4. Modifier `curation.py:decide_actions` promotion auto 2 succès / démotion immédiate + `insights.py:compute` alert `skill_quality_low` (graded vs binaire `never_loaded`).
5. Modifier `config.py:191/322` + `weekly-telemetry-config.json:87` seuils (`max_candidates 5`, `trust_promotion 2`).
6. Tests : `tests/test_candidates.py` (anti-learning `is_anti_learning` conservé, `select_draft_candidates` cap 5), `tests/test_curation.py` TTL, `tests/test_insights.py` quality alerts, `tests/test_tool_quality.py` (penalty 0.2-1.0).

### Join — ~0.5j

- `cli.py` expose `weekly_skill_quality` diag optionnel (lecture seule, pas de réseau).
- `pyproject.toml` `ruff` + `pytest` 660 tests source-de-vérité passe.
- Doc : `doc/architecture/README.md` § Invariants + Veille + Gouvernance ; `INSTALL.md` portability rules (5 rules `safe_git_write.py`, `harness_include` `config.py:23-66`).
- Vérif harness : `harness-eval 7.9.0` `config.py:15` digest inchangé (zero symlink `harness_remediation.py:203`, baseline `weekly-harness-baseline.json` captured-never-rewritten).

---

## 4. Fichiers touchés

```
Nouveaux:
  .opencode/plugins/weekly-advisor-engine/weekly_telemetry_aggregator/skill_ranker.py
  .opencode/plugins/weekly-advisor-engine/weekly_telemetry_aggregator/tool_quality.py
  .opencode/plugins/weekly-advisor-engine/weekly_telemetry_aggregator/skill_quality.py

Modifiés:
  .opencode/plugins/weekly-advisor-engine/weekly_telemetry_aggregator/watch_distill.py:41-207,271,293
  .opencode/plugins/weekly-advisor-engine/weekly_telemetry_aggregator/aggregator.py:140-233,536-559
  .opencode/plugins/weekly-advisor-engine/weekly_telemetry_aggregator/candidates.py:49-55,97-124,248-263
  .opencode/plugins/weekly-advisor-engine/weekly_telemetry_aggregator/config.py:117-131,163-191,248-322,473-511
  .opencode/plugins/weekly-advisor-engine/weekly_telemetry_aggregator/curation.py:decide_actions,ttl_archive_candidates
  .opencode/plugins/weekly-advisor-engine/weekly_telemetry_aggregator/insights.py:58-184,400-455
  .opencode/plugins/weekly-advisor-engine/weekly_telemetry_aggregator/models.py: SkillRecord counters
  .opencode/plugins/weekly-advisor-engine/weekly_telemetry_aggregator/cli.py: _SUBCOMMANDS
  .opencode/plugins/weekly-advisor-engine/weekly-telemetry-config.json:87-91 + racine max_candidates
  .opencode/plugins/weekly-advisor-engine/pyproject.toml (si deps stdlib only → aucun ajout)

Tests:
  tests/test_skill_ranker.py (nouveau)
  tests/test_tool_quality.py (nouveau)
  tests/test_watch_distill.py (snapshots)
  tests/test_aggregator.py (similar_pairs)
  tests/test_candidates.py (cap 5, provisional)
  tests/test_curation.py (TTL + trust)
  tests/test_insights.py (quality alerts)
```

---

## 5. Vérification

- `uv run pytest` (660 tests + nouveaux) — source de vérité `pyproject.toml:1-48`
- `ruff check` + `ruff format --check` sur `weekly_telemetry_aggregator/`
- `node --test scripts/tests/*.test.mjs` contrats CLI ↔ plugin
- `scripts/check-flow-docs.mjs` 7 surfaces docs ↔ code
- Déterminisme : même `anchor` + mêmes entrées → `rank` identique (`round6` `watch_distill.py:138`, tri stable `watch_distill.py:279-284`)
- Fail-soft : `watch_distill` exit 2 si écosystème absent/désactivé → fallback legacy `watch_distill.py:495-511` ; jamais crash
- Coût : BM25 stdlib = 0 API, <10ms pour corpus 100 skills ; `weekly_timings-<date>.json` durées tracées
- Harness : `harness-eval` projection copies réelles zero symlink, `baseline_summary_path` `config.py:319`

---

## 6. Risques & mitigations

| Risque | Mitigation |
|--------|------------|
| BM25 sans embedding sur-valorise mots rares | Pondération `authority 25 + freshness 20` déjà présente `watch_distill.py:41` compense ; `multi_source 15` favorise corroboration |
| 5 drafts/run → +66% charge LLM audit | Garde-fou `audit_max_sessions=8` `config.py:166` + `cost_per_active_minute>0.5` `config.py:77` + `cache_efficiency_gap 0.2` `config.py:78` filtrent déjà |
| Provisional 2 succès trop laxiste si runs corrélés | Exiger `observation_id` distinct + `session_id` distinct (comme OpenSpace `skill_trust_observations` UNIQUE) |
| Cache pickle BM25 corrompu | `load_memory` pattern warning + démarrage à vide `watch_distill.py:518-520` ; `invalidate_cache(skill_id)` sur evolution |
| Régression déterminisme | Tests invariants ancre `scripts/tests/*.test.mjs` + `round6` `watch_distill.py:138` |

---

## 7. Question ouverte

Veux-tu que `tool penalty` s'applique aussi aux scores `harness-eval` (poids règles `harness_remediation.py`) ou seulement à `watch_distill` relevance ? Recommandation : `watch_distill` seul en V1, harness en V2 si `problematic_tools` `min_calls=5` montre signal.

---

## 8. Références OpenSpace

- `openspace/skill_engine/skill_ranker.py` — hybrid BM25*3 + embedding, `PREFILTER_THRESHOLD=10`, cache pickle `skill_embeddings_v1.pkl`
- `openspace/skill_engine/types.py:250-380` — `SkillRecord` 4 compteurs + `effective_rate`, `CaptureContract`
- `openspace/grounding/core/quality/types.py:80-115` — `ToolQualityRecord.penalty` 0.2-1.0 threshold 0.4
- `openspace/skill_engine/store.py` — `skill_trust_observations`, `trust_promoted/demoted`, `SkillTrustState provisional/trusted`
- `openspace/skill_engine/registry.py:270-350` — frontmatter `when_to_use`, `paths`, `allowed-tools`, `.skill_id`
