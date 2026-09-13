# 6. Sources de données & intégrations

Cette section décrit **ce que** le produit ingère et **ce qu'il** écrit, de façon sémantique —
suffisant pour reconstruire l'ingestion sans prescrire un stockage.

## 6.1 Données ingérées

| Écosystème | Ce que le produit lit |
|---|---|
| **OpenCode** | Les sessions (métadonnées : identifiant, parent, titre, modèle, agent, coûts, tokens, timestamps de création/mise à jour), les messages et les événements par étape (coût, tokens de cache, appels d'outils, tours texte, raisonnement, compactage). Lecture **en lecture seule**, y compris pendant que le harnais écrit (lecture concurrente sûre), avec gestion des verrous et des bases sans verrou accessible (jamais d'écriture, jamais de mode immuable incompatible avec un écrivain vivant). Requêtes ciblées par session, jamais de balayage complet ; parsing sélectif des données. |
| **Claude Code** | Les transcripts de sessions (un fichier = une session), localisés par le répertoire projet encodé. **Aucun coût n'y est enregistré** → coûts toujours **estimés** (C1 règle 9). |
| **Copilot VS Code** | Les sessions de chat, via le stockage de l'espace de travail (état optionnel). Les sessions vides fréquentes sont lues en métadonnées seules. |
| **Cœur du harnais principal** | Les versions publiées dans la fenêtre, leurs dates, descriptions, et mots-clés. |
| **Écosystème du marché** | Catalogue de paquets, dépôts par sujet, serveurs d'outils officiels, listes de curation, pages web, flux RSS/Atom (voir C2). |
| **Historique git du projet** | Les créations de documents auto-rédigés (message et corps), pour la traçabilité et l'annexe. |

**Périmètre d'accès exclu** : le produit n'accède jamais aux fichiers d'authentification / jetons
(OAuth) des harnais — ils ne sont ni lus ni listés.

**Codex** ne figure pas dans les données ingérées : il n'est supporté que comme cible de drafting
(§5.7), jamais comme source de sessions.

## 6.2 Données écrites vers les écosystèmes

| Cible | Ce que le produit écrit | Règles |
|---|---|---|
| **Skills (harnais cible de drafting)** | Documents de compétence de type skill, générés (création) | Périmètre autorisé, gate de portabilité, commit isolé, jamais de doublon (chevauchement → constat manuel). |
| **Commands (harnais cible)** | Documents de command générés (création) **ou améliorés** (pour `amélioration-command`) | idem. |
| **Historique git du projet** | Un commit par écriture, message construit, revert possible. | Un commit par écriture, jamais groupé. |
| **Rapport** | Page interactive + archive (voir §7.7). | Rendu best-effort pour la page. |

**Rien d'autre n'est écrit dans l'environnement des harnais** : ni configuration, ni agent, ni
intégration, ni code applicatif. L'auto-rédaction se limite aux documents de compétence, dans le
périmètre autorisé, et reste révocable par revert humain.

## 6.3 Mémoire et état

Le produit conserve, hors des harnais :
- l'**historique des résumés hebdo** et des **digests déclaratifs** (jamais purgé — mémoire des
  insights et de la tendance) ;
- la **mémoire de veille inter-run** (append-only, purge > 26 semaines sauf recommandé/bloqué
  sécurité) ;
- les **instantanés locaux** de veille (listes / pages web) pour le diff ;
- la **baseline des findings déclaratifs** (capturée une fois, jamais réécrite par les runs ;
  rafraîchie sur changement de règles) ;
- le **répertoire du run actif** (alias stable, observable : « le run actif ») — un comportement,
  pas une mécanique de lien.

---
