# RAF — Reste À Faire (weekly-advisor-kit)

Date: 2026-11-15  
Session: swarmx audit + implémentation P0 (bloquée sur état de mission)

## 0. État actuel (déjà fait)

### Implémenté et vérifié (P0 + P1 partiel)
- P1.2 contrats d'artefacts centralisés dans `artifact_contracts.py` (dict + validateurs + `_json_file_state`), importés par `report.py`.
- P1.1 partiel : gate d'artefacts extraite dans `report_gates.py`; blocs LLM extraits dans `report_blocks.py`; contexte extrait dans `report_context.py`; rendu markdown extrait dans `report_render.py`.
- P1.4 `_json_file_state` réutilisé dans `_cmd_report_prep` (cohérence erreurs).
- P1.5 matrice MD/HTML documentée dans `html_report.py` + test de parité `test_quality_block_parity_matrix`.
- P1.7 : split complet de `tool-registry.ts` en `tool-registry-common.ts`, `tool-registry-specs.ts`, `tool-registry-tools.ts` (shim de réexport).
- P1.8 tests TS étendus : `tests/preflight.test.ts` (+3), total 13/13.
- P1.6 `blocks_min_words` exposé dans `validate_llm_blocks` (paramètre optionnel) et utilisé par cli/assembler.
- Swarmx : cellules bloquées nettoyées, hive disponible via shim, serveur hive démarré.
- Vérification : `uv run pytest tests/test_report.py` → 195 passed.

### Implémenté et vérifié (P0)
- **Python** (`report.py`, `tests/test_report.py`) :
  - Constantes `QUALITY_BLOCK_STATUS_*` pour unifier les placeholders section 4.
  - `report_prep` valide les artefacts requis avant d'écrire le draft (nettoyage du draft orphelin si la gate échoue).
  - Test smoke `test_report_prep_smoke_sections_and_quality_block` : 8 sections + marqueur `QUALITY_BLOCK` dans le draft, suppression dans le final.
  - Vérification : `uv run pytest tests/test_report.py` → tous passants ; `uv run ruff check` → OK.
- **TypeScript** (`weekly-advisor/`) :
  - `tsconfig.json` strict (`strict`, `nodenext`, `noEmit`, `skipLibCheck`).
  - `package.json` : scripts `tsc`/`typecheck`/`test` ; devDeps `typescript`, `@types/node`.
  - Tests unitaires : `tests/paths.test.ts` (6), `tests/runtime.test.ts` (4).
  - Vérification : `tsc --noEmit -p .` → rc=0 ; TS unit 10/10 ; contrats 197/197.
- **Formatage** (`report_template.html.j2`) :
  - Skip-link + `id="main"` / aliases.
  - `scope="col"` sur tous les `<th>`.
  - Garde `@media (prefers-reduced-motion: reduce)`.
  - `-webkit-backdrop-filter` ajouté.
  - Tokens CSS (pas de hex bruts) pour le skip-link.
  - Vérification : `uv run pytest tests/test_html_report.py` → 31 passed.

### Audit réalisé (lecture seule)
- Rapport d'audit consolidé produit dans la conversation (code quality, contenu, formatage, TS).
- Fichiers d'audit non écrits sur disque (`docs/audit/*.md`) car les workers n'avaient pas les outils d'écriture dans la session d'audit.

---

## 1. RAF — Code & qualité (P1/P2/P3)

### P1 (priorité haute)
1. **Découper `report.py`** en modules cohésifs :
   - `report_context.py` (`build_report_context` + helpers de projection)
   - `report_render.py` (templates)
   - `report_gates.py` (`validate_required_artifacts` + `assemble_*`)
   - `report_blocks.py` (`validate_llm_blocks` + number bands)
   - Conserver les exports publics via `report.py` (shim) pour ne pas casser cli/tests.
2. **Centraliser les contrats d'artefacts** dans `artifact_contracts.py` (table de schémas par nom d'artefact) ; conserver les messages d'erreur actuels (tests les asserent).
3. **Corriger l'ordre gate/draft dans `report_prep`** : valider les artefacts requis avant l'écriture du draft (ou nettoyer le draft si la gate échoue) — déjà partiellement fait, à finaliser.
4. **Réutiliser `_json_file_state`** dans `_cmd_report_prep` pour cohérence de gestion d'erreurs.
5. **Parité MD/HTML** : documenter la matrice de conversion dans `html_report.py` + ajouter un test de parité sur un bloc représentatif.
6. **Exposer `blocks_min_words`** dans `validate_llm_blocks` (paramètre optionnel) pour éviter la dérive avec `_cmd_report_blocks_check`.
7. **Découper `tool-registry.ts`** (973 l.) en modules plus petits.
8. **Tests unitaires TS** sur `paths`/`preflight`/`runtime` (étendre au-delà des 10 existants).

### P2 (priorité moyenne)
9. **Tokens CSS complets** : fait (hex restants uniquement dans `:root`).
10. **Pagination de l'annexe H** : `content-visibility: auto` ajouté pour limiter le coût DOM.
11. **Responsive** : fallback nav opaque ajouté; `.barh .name` déjà responsive.
12. **Contrastes WCAG** : garde `prefers-contrast: high` ajoutée.
13. **Migrer la section Sécurité** : fait (rendu dans le gabarit via `security_section`).
14. **`build_report_context` cache** : fait (paramètre `ctx` optionnel sur `report_assemble`/`_report_assemble_inner`).
15. **Couverture de tests** : config coverage Python (`pyproject.toml`) + script `test:coverage` TS.

### P3 (backlog)
16. **Réduction du bus factor** : documentation créée (`docs/audit/bus-factor.md`); `report.py` traité en P1.
17. **Performance I/O** : à optimiser (rechargements JSON/git) — reporté.
18. **Porte `tsc` en CI** : fait (workflow `ci.yml`, npm install + npm run typecheck).
19. **Placeholders** : standardiser les libellés restants (déjà partiellement fait).

---

## 2. RAF — Nettoyage & dette technique

- Supprimer le fichier test `docs/audit/test-write.md` (artefact de test).
- Supprimer `/tmp/opencode/hello.txt` (créé par l'agent TS).
- Nettoyer les cellules swarm bloquées dans Hive (une fois la session réinitialisée).
- Vérifier que les changements P0 sont bien commités (sans commit automatique — attendre la demande explicite).

---

## 3. RAF — Amélioration de swarmx (fork swarm-tools)

Fork cloné dans `/home/benjamin/Dev/swarm-tools-fork`.

Repo à forker : https://github.com/joelhooks/swarm-tools

### Objectifs du fork
1. **Rendre les workers autonomes** : garantir que tous les agents dispatchés disposent de `write`/`edit`/`shell`/`swarm_complete` ; sinon, restreindre les agents non-code à la lecture.
2. **Grader disponible** : configurer le grader pour utiliser un modèle réellement présent (ex. `opencode-go` preset) au lieu de `anthropic/claude-opus-5-5`.
3. **Staging automatique** : `swarm_complete` doit stage automatiquement les fichiers non trackés (`git add -N`) ou le worker doit le faire par défaut dans `swarm-worker-protocol`.
4. **Hive/beads disponibles** : installer/activer beads ou désactiver proprement le mode dégradé avec un fallback clair.
5. **Réinitialisation de mission** : ajouter `swarm_reset` ou rendre `swarm_finish` forcé après N cellules bloquées ; exposer `swarm_revoke_attempts`.
6. **Budget renouvelable** : autoriser un nouveau budget après clôture, ou exposer `swarm_new_mission`.
7. **Identité coordinateur persistée** : garantir que `swarm_set_budget` fonctionne tant que la session est la même, indépendamment des déplacements de worktree.
8. **Notification de verdict** : remplacer le polling manuel par une notification automatique quand `router_verify` est prêt.
9. **Vérification indépendante robuste** : si le grader est indisponible, marquer « unverifiable » mais ne pas bloquer `swarm_review_feedback` si les tests locaux passent.
10. **Diff pour fichiers non trackés** : `swarm_review` doit inclure le contenu des fichiers nouveaux non trackés.
11. **Itérations par cellule** : augmenter le plafond par défaut (3 → 5) ou permettre un reset après un succès partiel.
12. **Cellules de recherche ciblées** : ajouter un mode « inventaire repo » (repowise) pour les audits.
13. **Consolidation automatique** : dispatcher la cellule de consolidation automatiquement après les cellules dépendantes.
14. **Nettoyage automatique** : supprimer les fichiers temporaires après la mission.
15. **Guide de dépannage** : documenter les cas dégradés (hive indisponible, grader indisponible, fichiers non trackés).

### Étapes du fork
1. Cloner https://github.com/joelhooks/swarm-tools
2. Identifier le module de vérification grader (recherche `anthropic/claude-opus-5-5`).
3. Remplacer par un modèle disponible ou rendre le grader configurable.
4. Ajouter `git add -N` automatique dans `swarm_complete`.
5. Ajouter `swarm_reset` / `swarm_revoke_attempts`.
6. Tester avec une mission `dev` complète (audit → implémentation → review → clôture).
7. Publier le fork et mettre à jour la config locale (`opencode.json` plugins).

---

## 4. Prochaines étapes recommandées (nouvelle session)

1. Démarrer une nouvelle session OpenCode sur `/home/benjamin/Dev/weekly-advisor-kit`.
2. Exécuter `swarm_init` → vérifier la disponibilité des outils (hive, grader).
3. `swarm_set_budget({mode: "dev", max_spawns: 20, max_iterations: 3})`.
4. Lancer une mission pour traiter le P1 (découpage report.py + contrats artefacts + tool-registry.ts).
5. Parallèlement, forker et améliorer swarm-tools (section 3).
6. Vérifier que `swarm_finish` fonctionne en fin de mission.

---

## 5. Commandes de vérification (à rejouer)

```bash
cd /home/benjamin/Dev/weekly-advisor-kit/.opencode/plugins/weekly-advisor-engine
uv run pytest tests/test_report.py tests/test_html_report.py -q
uv run ruff check weekly_telemetry_aggregator/

cd /home/benjamin/Dev/weekly-advisor-kit/.opencode/plugins/weekly-advisor
npx tsc --noEmit -p .
node --test --experimental-strip-types tests/*.test.ts
```

---

## 6. Bloqueurs connus
- Mission swarm active non clôturable (tentatives non révoquées).
- Grader : preset actif `opencode-go` (heavy = `opencode-go/deepseek-v4-pro`) via overrides; vérification gate disponible.
- Hive : disponible via shim + serveur (port 4483).
- Budget épuisé (12 spawns) et non renouvelable dans la même session.
- Identité coordinateur : `swarm_set_budget`/`swarm_finish` refusés (canonical coordinator uniquement) — bloquant pour mission complète.