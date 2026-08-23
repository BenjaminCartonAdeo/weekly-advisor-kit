# Refonte veille weekly-advisor — Plan d'implémentation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Réduire le coût tokens de l'étape de veille de ~100 K à ~15 K/run (~90 %) via entonnoir déterministe (2.2 distill), mémoire inter-run, screening sécurité, élargissement sources (repos Copilot/Codex/Gemini CLI + agents-radar MCP).

**Architecture:** Étape 2.2 `watch_distill` déterministe zéro-LLM filtre/score/screen l'écosystème → ~30 fiches (~15 KB) ; 2.5 produit inventaire local structuré + crosswalk candidats + hints ; 3.5 (LLM) ne voit que fiches enrichies + digest mémoire + findings qualité ; 3.6 valide puis écrit la mémoire. Filet LLM = phase de triage optionnelle DANS le skill 3.5 (jamais d'infra LLM python). Chaque brique dégrade gracieusement : échec distill → 3.5 retombe sur l'écosystème complet (comportement actuel).

**Tech Stack:** Python 3.13 stdlib + httpx (déjà dépendance), pytest, JSON-RPC 2.0 brut pour MCP streamable-http (pas de SDK), config déclarative `weekly-telemetry-config.json`.

## Global Constraints

- Python : stdlib-first, pas de nouvelle dépendance (httpx déjà présent)
- Zéro appel LLM/réseau dans watch_distill / watch_memory / scoring / screening
- Schéma aval `weekly-watch-findings-<date>.json` : champs existants conservés (insights/report intacts) ; seuls ajouts autorisés : `token_impact`, `security_annex`
- Jamais d'installation auto d'outils externes ; items `blocked` jamais visibles du LLM
- Ordre figé agent : 2 → **2.2** → 2.5 → 3 → 3.5 → 3.6 (2.2 séquentiel après 2 comme 2.5)
- Exit codes CLI inchangés : 0 OK/partiel, 1 échec tool (run continue), 2 dépendance manquante
- Tests : hermétiques (conftest `_no_browser`), pas de réseau réel ; fixtures locales
- Style repo : docstrings FR courtes, type hints complets, `from __future__ import annotations`

---

### Task 1: Module mémoire inter-run `watch_memory.py`

**Files:**
- Create: `.opencode/plugins/weekly-advisor-engine/weekly_telemetry_aggregator/watch_memory.py`
- Test: `.opencode/plugins/weekly-advisor-engine/tests/test_watch_memory.py`

**Interfaces:**
- Produces (consommé par Tasks 4, 8, 9):
```python
def normalize_id(name: str, npm_package: str | None, repo_url: str | None) -> str
    # "npm:<pkg>" si npm_package sinon "gh:<owner/repo>" si repo_url github sinon "url:<slug minuscule>"
def load_memory(path: Path) -> tuple[dict[str, dict], list[str]]
    # -> ({id: entry}, warnings) ; lignes malformées skippées+warning ; purge >26 semaines sauf dernier statut recommended|blocked-security
def entry_from_item(item: Mapping[str, Any], week: str) -> dict   # squelette ligne neuve
def item_signature(item: Mapping[str, Any]) -> dict               # {"version": str|None, "published_at": iso}
def signature_changed(entry: dict, sig: dict) -> bool             # version différente OU published_at plus récent
def filter_items(items: list[dict], memory: dict[str, dict], week: str) -> tuple[list[dict], list[dict]]
    # -> (kept, dropped[{id, reason}]) ; dropped si ignored et signature inchangée ; seen ≤4 sem dépriorisé via flag "_stale_seen"
def build_digest(memory: dict[str, dict], week: str) -> dict
    # {"recently_ignored": [{"id","week","note"} ≤20], "previously_recommended": [ids] ≤30, "recurrents": [ids occ≥3]}
def append_entries(path: Path, updates: Iterable[dict]) -> list[str]
    # append-only JSONL crash-safe (une ligne par update, flush par ligne), fusionne avec l'existant par id ; retourne warnings
```
- Ligne mémoire : `{"id", "name", "first_seen_week", "last_seen_week", "occurrences", "history": [{"week","status"}], "last_signature": {...}, "note"}`
- Statuts valides : `seen | candidate | recommended | ignored | blocked-security`
- Semaine ISO : `datetime.isocalendar()` → `"2026-W34"`

- [ ] **Step 1: Test failing — normalize_id + load/save roundtrip**

```python
# tests/test_watch_memory.py
from __future__ import annotations
from pathlib import Path
from weekly_telemetry_aggregator import watch_memory as wm

def test_normalize_id_prefers_npm_then_repo_then_url():
    assert wm.normalize_id("x", "@v/x", "https://github.com/v/x") == "npm:@v/x"
    assert wm.normalize_id("x", None, "https://github.com/v/x") == "gh:v/x"
    assert wm.normalize_id("Some Tool", None, "https://example.com/a") == "url:https://example.com/a"

def test_load_skips_malformed_lines_with_warning(tmp_path: Path):
    p = tmp_path / "watch-memory.jsonl"
    p.write_text('{"id":"a"}\nnot-json\n{"id":"b","name":"b","first_seen_week":"2026-W1","last_seen_week":"2026-W1","occurrences":1,"history":[],"last_signature":{}}\n', encoding="utf-8")
    entries, warnings = wm.load_memory(p)
    assert set(entries) == {"a", "b"}
    assert len(warnings) == 1

def test_purge_old_ignored_keeps_recommended(tmp_path: Path):
    # entrée ignorée vue 40 sem → purgée ; recommended 40 sem → gardée
```

- [ ] **Step 2:** `cd .opencode/plugins/weekly-advisor-engine && uv run pytest tests/test_watch_memory.py -v` → FAIL (module absent)
- [ ] **Step 3:** Implémenter `watch_memory.py` (fonctions ci-dessus, ~180 lignes). `filter_items` : ignored+signature identique → dropped ; kept porte `_stale_seen=True` si seen dans les 4 dernières semaines (utilisé au tri par Task 4).
- [ ] **Step 4:** pytest → PASS (couvrir : resurface sur version bump, purge, append fusion id existant, digest bornes)
- [ ] **Step 5: Commit**

```bash
git add .opencode/plugins/weekly-advisor-engine/weekly_telemetry_aggregator/watch_memory.py tests/test_watch_memory.py
git commit -m "feat(watch): memoire inter-run append-only crash-safe (watch-memory.jsonl)"
```

---

### Task 2: Scoring + screening sécurité `watch_distill.py` (cœur)

**Files:**
- Create: `.opencode/plugins/weekly-advisor-engine/weekly_telemetry_aggregator/watch_distill.py` (partie scoring/screening)
- Test: `.opencode/plugins/weekly-advisor-engine/tests/test_watch_distill.py`

**Interfaces:**
- Consumes: `watch_memory.filter_items`, `watch_context.normalize_npm_package/normalize_repo_url` (DRY)
- Produces:
```python
DEFAULT_WEIGHTS = {"authority": 25, "relevance": 30, "freshness": 20, "multi_source": 15, "traction": 10}
AUTHORITY_BY_SOURCE = {"repo": 25, "mcp": 20, "topic": 16, "npm": 14, "rss": 10, "radar": 8}
RELEVANCE_KEYWORDS = ("skill", "plugin", "agent", "context", "cache", "compaction",
                      "token", "prompt", "mcp", "skill.md")  # complétée par cfg.release_keywords

def score_item(item: Mapping, *, weights: dict, now: datetime, extra_keywords: Sequence[str]) -> dict
    # -> {"total": 0-100, "breakdown": {authority,relevance,freshness,multi_source,traction}}
    # authority = max(AUTHORITY_BY_SOURCE[s]) sur found_via parsés ("watch:repo:x"→"repo"…)
    # relevance = 30 * matches/len(keywords_uniqs) plafonné ; fraîcheur = 20 * max(0, 1 - age_days/90)
    # traction = min(10, stars/50) si dispo sinon neutre 5
def screen_item(item: Mapping) -> tuple[str, str | None]
    # -> (clean|suspicious|blocked, raison) ; heuristiques sans réseau :
    # blocked: regex exfiltration-env/prompt-injection/credential-path sur name+description ;
    #          typosquat Levenshtein<=2 vs CORE_PKGS=("opencode-plugin","@opencode-ai/plugin","@opencode/plugin")
    # suspicious: description ratio majuscules>0.5, mention postinstall, published<30j sans aucune traction
def rank(candidates: list[dict]) -> list[dict]
    # tri reproductible: (-score.total, -published_ts, id)
```

- [ ] **Step 1: Tests failing** — breakdown somme = total ; tie-breaker déterministe sur items à score égal ; fixture toxique (description contenant `curl $OPENCODE_API_KEY` → blocked) ; typosquat `opencode-plugn` → blocked ; description bénigne → clean.
- [ ] **Step 2:** pytest → FAIL
- [ ] **Step 3:** Implémenter scoring + screening + rank (~150 lignes). Levenshtein : version stdlib maison 10 lignes (pas de dép).
- [ ] **Step 4:** pytest → PASS
- [ ] **Step 5: Commit** `feat(watch): scoring determinant + screening securite supply-chain`

---

### Task 3: Orchestration distill + quotas + sortie fiches

**Files:**
- Modify: `.opencode/plugins/weekly-advisor-engine/weekly_telemetry_aggregator/watch_distill.py`
- Modify: `.opencode/plugins/weekly-advisor-engine/weekly_telemetry_aggregator/cli.py` (subcommand `watch-distill`, après `p_watch` ligne ~454)
- Modify: `.opencode/plugins/weekly-advisor-engine/weekly-telemetry-config.json` (section `"watch_distill"`)
- Test: `.opencode/plugins/weekly-advisor-engine/tests/test_watch_distill.py`

**Interfaces:**
- Produces:
```python
QUOTAS = {"new": 12, "improvable": 8, "resurfaced": 5}  # reste = meilleur score brut
FICHE_KEYS = ("id", "name", "sources", "score", "security", "summary",
              "signature", "local_relevance_hints")   # hints=[] ici, remplis par Task 7
def run(cfg, *, anchor: str | None = None) -> tuple[dict, int]
    # lit <output_dir>/runs/current/weekly-ecosystem-<date>.json (date du run)
    # écrit watch-candidates-<date>.json + watch-memory-digest-<date>.json dans runs/current/
    # retourne ({"schema_version":1,"mode":"distill"|"fallback","candidates":[...],"dropped_memory":N,"quotas_applied":{},"warnings":[]}, exit_code)
    # ecosystem absent → exit 2 ; exception → exit 1 (agent retombe sur legacy)
```
- Config ajoutée :
```jsonc
"watch_distill": {
  "enabled": true,
  "top_n": 30,
  "quotas": {"new": 12, "improvable": 8, "resurfaced": 5},
  "weights": {"authority": 25, "relevance": 30, "freshness": 20, "multi_source": 15, "traction": 10},
  "memory_file": "watch-memory.jsonl",        // relatif à output_dir
  "retention_weeks": 26,
  "min_candidates": 20                         // seuil déclenchement filet B côté skill 3.5
}
```
- Fusion multi-sources : même normalize_id → un item, `sources[]` union found_via, description la plus longue gagne.
- Fiche summary : description tronquée 200 chars (1-2 phrases).

- [ ] **Step 1: Test failing** — golden partiel : 60 items synthétiques dont doublons multi-sources + 1 blocked + 10 déjà en mémoire ignored (signature identique) → assert : candidats ≤ top_n, blocked absent des candidates mais présent dans `"security_annex"` du résultat, dropped_memory=10, quotas respectés, tie-break stable sur 2 runs.
- [ ] **Step 2:** pytest → FAIL
- [ ] **Step 3:** Implémenter run() + `_cmd_watch_distill` (pattern `_cmd_watch_context`) + parser `watch-distill` + section config.
- [ ] **Step 4:** pytest → PASS + test CLI : `uv run python -m weekly_telemetry_aggregator watch-distill --help`
- [ ] **Step 5: Commit** `feat(watch): etape 2.2 distill deterministe - fiches top-N + quotas + memoire`

---

### Task 4: Sources étendues releases.py (radar MCP + version passthrough)

**Files:**
- Modify: `.opencode/plugins/weekly-advisor-engine/weekly_telemetry_aggregator/releases.py` (`_collect` lignes 736-769, `_finalize_items` ligne 802)
- Modify: `weekly-telemetry-config.json` (watch entries : repos Copilot/Codex/Gemini CLI + entrée radar)
- Create: `opencode.json` à la racine du kit (déjà existant avec `$schema` — ajouter clé `mcp`)
- Test: `.opencode/plugins/weekly-advisor-engine/tests/test_releases.py`

**Interfaces:**
- `_finalize_items` ajoute `"version": record.get("version")` (dispo lignes 340/582 mais perdu aujourd'hui) → alimente `signature.version` mémoire.
- Nouveau dispatch type radar dans `_collect` :
```python
radars = [w for w in watch_entries if w.get("type") == "radar"]
for r in radars:
    source_id = f"radar:{r['name']}"
    counts_by_source.setdefault(source_id, 0)
    run_source(source_id,
        lambda e=r: _fetch_radar(client, e, start, end, project_root=cfg.project_root),
        sink_item)

def _fetch_radar(client, entry: Mapping, start: datetime, end: datetime, *, project_root: Path) -> list[dict]:
    # 1. résoudre URL depuis <project_root>/opencode.json → mcp[entry["name"]].url (source unique vérité)
    # 2. JSON-RPC streamable-http brut : POST initialize → POST tools/call {"name": entry["tool"], "arguments": {}}
    #    headers: {"Content-Type":"application/json","Accept":"application/json, text/event-stream"}
    #    session id repris du header mcp-session-id de la réponse initialize
    # 3. texte markdown du résultat → regex liens [titre](url) (réutiliser _extract_markdown_links)
    # 4. fallback RSS entry["rss_fallback"] via _fetch_rss si MCP échoue ; les deux morts → SourceError
    # 5. fenêtre hebdo : garder items datés dans [start,end] ; non datés exclus (digest quotidien → toujours dedans si get_latest ramène le jour courant)
    # -> [{name, category:"repo", repo_url:url, npm_package:None, description, published_at, found_via:["radar"], new_repo:False}]
```
- `opencode.json` kit :
```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {"agents-radar": {"type": "remote", "url": "https://agents-radar-mcp.duanyytop.workers.dev/mcp", "enabled": true}}
}
```
- Watch entries config ajoutées :
```jsonc
{"type": "repo", "name": "github/copilot-cli"},
{"type": "repo", "name": "openai/codex"},
{"type": "repo", "name": "google-gemini/gemini-cli"},
{"type": "radar", "name": "agents-radar", "tool": "get_latest", "window_days": 7,
 "rss_fallback": "https://agents-radar.duanyytop.workers.dev/feed.xml"}
```

- [ ] **Step 1: Tests failing** — (a) `_finalize_items` préserve version ; (b) `_fetch_radar` contre httpx MockTransport : happy path MCP (initialize+tools/call → markdown 2 liens) ; fallback RSS quand tools/call renvoie 500 ; URL absente d'opencode.json → SourceError message clair ; (c) `_collect` route type radar (cfg factice avec 1 entrée radar, client mocké).
- [ ] **Step 2:** pytest tests/test_releases.py → FAIL
- [ ] **Step 3:** Implémenter (~120 lignes). SSE : si body commence par `event:`/`data:` lignes, parser la dernière ligne `data:` JSON.
- [ ] **Step 4:** pytest → PASS
- [ ] **Step 5: Commit** `feat(releases): source radar MCP agents-radar + fallback RSS + version passthrough`

---

### Task 5: Golden fixture run réel

**Files:**
- Create: `.opencode/plugins/weekly-advisor-engine/tests/fixtures/ecosystem-2026-08-23.json` (copie de `/home/benjamin/Dev/Adeo/reports/runs/2026-08-23-a179f984/weekly-ecosystem-*.json`)
- Modify: `.opencode/plugins/weekly-advisor-engine/tests/test_watch_distill.py`

**Interfaces:** Consumes Task 3 `run()`. Assert invariants mesurés.

- [ ] **Step 1:** Copier le fichier réel dans fixtures (`cp /home/benjamin/Dev/Adeo/reports/runs/2026-08-23-a179f984/weekly-ecosystem-*.json tests/fixtures/ecosystem-2026-08-23.json`). Vérifier taille ≈151 KB.
- [ ] **Step 2: Test failing** — distill sur fixture : ≥20 candidats ; taille JSON sortie < 20 KB ; tous ids uniques ; aucun security==blocked dans candidates ; runtime < 5 s.
- [ ] **Step 3:** Corriger jusqu'à PASS (ajuster troncature summary si > 20 KB).
- [ ] **Step 4: Commit** `test(watch): golden fixture run reel 2026-08-23 - garde taille fiches <20KB`

---

### Task 6: Inventaire local structuré + crosswalk candidats (watch_context.py)

**Files:**
- Modify: `.opencode/plugins/weekly-advisor-engine/weekly_telemetry_aggregator/watch_context.py` (`build_watch_context` ligne 635)
- Test: `.opencode/plugins/weekly-advisor-engine/tests/test_watch_context.py`

**Interfaces:**
```python
def build_local_inventory(project_root: Path) -> dict
    # {"items": [{"name","kind","path","description"}], "warnings": []}
    # kind ∈ skill|command|agent|plugin ; description = frontmatter (description:) ou première ligne
    # réutilise _markdown_records/_local_plugin_records existants
def hints_for(fiche: Mapping, inventory_items: Sequence[Mapping]) -> list[str]
    # tokens(normalisés) fiche(name+summary) ∩ tokens(description locale) ≥1 → noms locaux, cap 5
```
- `build_watch_context(cfg, ...)` : si `runs/current/watch-candidates-<date>.json` existe → crosswalk limité aux ids candidats + écrit `watch-candidates-enriched-<date>.json` (fiches + `existing_state` + `market_match` + `local_relevance_hints[]` remplies) + champ `"residual": [{id,name,description(200c),score}]` (bande sous cutoff, cap 50). Les ids résiduels sont AUSSI ajoutés au `market_matches` du `weekly-watch-context-<date>.json` (sans fiche) → leurs findings passent la validation 3.6. Sinon comportement actuel inchangé (compat).
- `weekly-watch-context-<date>.json` reste écrit (scope candidats) — 3.6 continue d'y lire market_matches.

- [ ] **Step 1: Tests failing** — inventaire : fixture worktree temporaire avec 1 SKILL.md frontmatter + 1 agent md → kinds/descriptions corrects ; hints : fiche "context-goblin cache" × skill local décrit "cache contexte" → hint rempli, cap 5 ; absence candidates → sortie legacy inchangée (test existant doit rester vert).
- [ ] **Step 2:** pytest tests/test_watch_context.py → FAIL nouveaux, anciens PASS
- [ ] **Step 3:** Implémenter (~80 lignes ajoutées). Matching tokens : lowercase, split non-alphanum, ignorer mots <3 chars.
- [ ] **Step 4:** pytest → PASS (suite complète)
- [ ] **Step 5: Commit** `feat(watch-context): inventaire local structure + crosswalk limite candidats + hints`

---

### Task 7: Tool `weekly_watch_distill` (glue TS)

**Files:**
- Modify: `.opencode/plugins/weekly-advisor.ts` (après `weekly_watch_context` ligne ~294)

**Interfaces:** Suit le pattern `anchorTool` existant. Description : « étape 2.2 — distillation déterministe de l'écosystème vers ~30 fiches candidates ; exécuter après weekly_releases, avant weekly_watch_context ; exit 2 = écosystème absent ».

- [ ] **Step 1:** Ajouter l'entrée `weekly_watch_distill: anchorTool(...)` miroir de `weekly_watch_context` (args : `anchor?`).
- [ ] **Step 2:** Vérification manuelle : redémarrer opencode (note agent md ligne 28 : plugin rechargé au boot uniquement), appeler le tool sur le run courant → JSON mode distill.
- [ ] **Step 3: Commit** `feat(plugin): tool weekly_watch_distill (etape 2.2)`

---

### Task 8: Skill 3.5 rewrite + filet B (triage borné)

**Files:**
- Modify: `.opencode/skills/weekly-watch-review/SKILL.md` (réécriture)

**Contenu cible (sections clés) :**
- Entrées : `watch-candidates-enriched-<date>.json` (~20 KB), `watch-memory-digest-<date>.json` (~3 KB), `weekly-quality-findings-<date>.json`. **Fallback explicite** : si enriched absent/mode=fallback → lire `weekly-ecosystem-<date>.json` + `weekly-watch-context-<date>.json` (comportement v6.1 documenté comme repli).
- Catégories : `install-new` (ex-adopt, seulement existing_state=absent) / `improve-existing` (**doit nommer une cible locale** existant dans l'inventaire ; privilégiée si `local_relevance_hints` non vide) / `ignore`.
- Flag obligatoire par finding : `"token_impact": "high|medium|low"` (remplace catégorie token-saver).
- Sécurité : fiche `security=suspicious` → mention risque OBLIGATOIRE dans evidence_summary ; fiches blocked jamais présentes dans l'enriched (garde amont).
- **Filet B (phase 0 conditionnelle)** : si nb fiches < `min_candidates` (config, défaut 20) ET `"residual"` non vide dans enriched (cap 50 items, déjà dans le contexte pour validation) → triage sur entrée ultra-compacte (id, name, description 200c, score) : garder ≤ 10 ids pertinents. Les findings peuvent alors porter un id keep du residual (leur subject est valide car présent dans market_matches). Écrire la décision dans le findings raw (`"filet": {"kept": [...], "dropped_reasons": {...}}`) — aucun re-calcul d'étape amont. Sinon phase 0 sautée, zéro token. Garde : le filet n'ajoute que des sujets, jamais en retirer.
- Schéma finding mis à jour (category/token_impact/target_local) + subject identique à v6.1.

- [ ] **Step 1:** Réécrire le SKILL.md selon le contenu cible ; conserver sections Sécurité/Schéma/Sortie structure v6.1 adaptée.
- [ ] **Step 2:** Self-check cohérence : chaque input cité existe dans les sorties Tasks 3/6 ; catégories alignées avec coercition Task 9.
- [ ] **Step 3: Commit** `docs(skill): weekly-watch-review v7 - fiches enrichies + filet B + categories ciblees`

---

### Task 9: Validation 3.6 + writer mémoire + annexe sécurité

**Files:**
- Modify: `.opencode/plugins/weekly-advisor-engine/weekly_telemetry_aggregator/watch_validation.py` (`validate_findings` ligne 291)
- Test: `.opencode/plugins/weekly-advisor-engine/tests/test_watch_validation.py`

**Interfaces:**
- Coercitions ajoutées (après existantes declared/observed/unknown) :
  - `improve-existing` dont `target_local` absent de `local-inventory.json` → coerci `install-new` (pas drop)
  - finding sur fiche `security=suspicious` sans mention risque → severity relevée `high` (pas rejet)
- Writer mémoire : après écriture du findings final → `watch_memory.append_entries` : chaque fiche candidate → statut final (`recommended` si finding install-new/improve-existing retenu, `ignored` si ignore, sinon `seen`) + signature ; raw findings rejetés → `seen` (sans raison).
- Annexe : `weekly-watch-findings-<date>.json` gagne `"security_annex": {"blocked_count": N, "ids": [...]}` lu depuis watch-candidates.

- [ ] **Step 1: Tests failing** — coercition target manquante ; severity suspicious ; writer appelé avec bons statuts (tmp_path memory file) ; annexe présente ; schéma champs existants inchangés (tests actuels restent verts).
- [ ] **Step 2:** pytest → FAIL
- [ ] **Step 3:** Implémenter (~70 lignes). `validate_findings` gagne params optionnels `memory_path: Path | None = None`, `candidates_path: Path | None = None` (None = comportement actuel, compat tests existants).
- [ ] **Step 4:** Suite complète → PASS
- [ ] **Step 5: Commit** `feat(watch-validate): coercion cible locale + writer memoire + annexe securite`

---

### Task 10: Orchestration agent + docs

**Files:**
- Modify: `.opencode/agents/weekly-advisor/weekly-advisor.md` (table déroulement ligne 61-78)
- Modify: `ARCHITECTURE.md` (section veille), `INSTALL.md` (nouvelle config `watch_distill` + opencode.json mcp)

**Changements agent md :**
- Table : insérer après ligne 65 (étape 2) : `| 2.2 | weekly_watch_distill — séquentiel après 2 (lit l'écosystème) ; exit 2 si écosystème absent ; exit 1 → continuer, 3.5 utilisera le fallback legacy | watch-candidates-<date>.json |`
- Ligne 66 (2.5) : noter qu'il consomme watch-candidates s'il existe et produit enriched.
- Ligne 68 (3.5) : noter fallback legacy + filet B phase 0 conditionnelle.
- Ligne 69 (3.6) : noter writer mémoire + annexe sécurité.
- Invariants : ajouter « fiches blocked-security jamais soumises au LLM ».

- [ ] **Step 1:** Éditer les 3 fichiers docs.
- [ ] **Step 2:** Relecture croisée : noms de fichiers/tools entre agent md ↔ ts plugin ↔ cli ↔ skills tous cohérents.
- [ ] **Step 3: Commit** `docs(weekly-advisor): ordre fige 2.2 distill + fallback + memoire`

---

### Task 11: Validation bout-en-bout + mesure tokens

**Files:** aucun nouveau — dry-run.

- [ ] **Step 1:** `uv run pytest` suite complète → PASS
- [ ] **Step 2:** Dry-run chaîne sur données réelles : `watch-distill` (fixture golden) → vérifier enriched vide OK ; simuler 3.5 : compter octets inputs (enriched + digest + quality findings) vs ancien (ecosystem + context) → assert ratio < 10 % dans un test marqueur `@pytest.mark.slow`.
- [ ] **Step 3:** Lancer un vrai run hebdo complet (cron manuel `/weekly-review`) ; comparer self-cost étape 3.5 avant/après ; reporter chiffres au rapport.
- [ ] **Step 4: Commit** (si ajustements) `perf(watch): boucle bout-en-bout validee - <10% tokens vs baseline`
