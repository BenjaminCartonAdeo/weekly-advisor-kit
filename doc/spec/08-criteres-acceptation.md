# 9. Critères d'acceptation de reconstruction

Cette checklist fermée permet de valider qu'un rebuild est **fonctionnellement équivalent** à la
présente spécification. Chaque critère est **testable par un humain**.

## A. Collecte télémetrique
- [ ] A1. Un run sur une fenêtre de 7 jours produit un résumé avec totaux (coût, tokens, cache,
      ratio) cohérents avec les sessions de la fenêtre.
- [ ] A2. La session de revue elle-même et les sessions actives (mise à jour < 10 min avant
      l'instant de référence) sont **exclues** des totaux et listées en avertissement.
- [ ] A3. Deux sources du même harnais ne produisent **aucun** double comptage.
- [ ] A4. Une session sans coût enregistré obtient un coût **estimé** (tokens × taux du harnais),
      distinct du coût facturé, jamais additionné à lui.
- [ ] A5. Les outliers (médiane+MAD, z ≥ 3, coût ≥ 0,50 $) sont identifiés ; avertissement si < 15
      sessions.
- [ ] A6. Deux runs à la même ancre et mêmes entrées produisent des sorties **identiques**.
- [ ] A7. Le ratio de cache = cache lu ÷ (cache lu + entrée fraîche) ; `null` sans entrée.

## B. Veille
- [ ] B1. Une source d'API en panne → avertissement, le run reste complet si une autre source a
      réussi ; partiel si toutes échouent.
- [ ] B2. La distillation produit ≤ 30 fiches scorées avec quotas (12/8/5) et screening sécurité
      (jamais de fiche « bloquée » soumise à l'analyse qualitative).
- [ ] B3. La mémoire inter-run purge au-delà de 26 semaines (sauf recommandé / bloqué sécurité) ;
      un item ignoré réapparaît si sa version ou sa date change.
- [ ] B4. La validation de veille applique les coercitions d'état (une identité déclarée/observée
      ne reste jamais `installer-nouveau`).

## C. Audit & drafting
- [ ] C1. La sélection des sessions à auditer est **déterministe** (signaux chiffrés), plafonnée à 8.
- [ ] C2. Chaque constat porte catégorie, sévérité, preuve **paraphrasée** (pas de citation
      verbatim), type de recommandation.
- [ ] C3. Le drafting cible **un harnais** résolu par (override > marqueurs > défaut), priorité
      claude-code > opencode > copilot-vscode > codex.
- [ ] C4. Au plus 3 documents générés par run ; un chevauchement détecté ne crée **jamais** de
      doublon (constat manuel).
- [ ] C5. Chaque document respecte les 5 règles de portabilité ; un document avec frontmatter
      invalide n'est **pas** publié ; un skill avec erreur de portabilité n'est **pas** commité.
- [ ] C6. Un commit par écriture, révocable par revert ; l'écriture cible le harnais résolu, jamais
      un autre projet.

## D. Contrôle déclaratif & insights
- [ ] D1. Le digest déclaratif distingue composants scannés et violations (jamais confondus) ;
      les surfaces hors périmètre sont signalées, non incluses.
- [ ] D2. La baseline est **créée au premier run puis réutilisée, jamais réécrite** ; seuls les
      findings nouveaux sont listés.
- [ ] D3. Les deltas sont `null` + avertissement si les fenêtres précédente et courante diffèrent
      de plus d'1 heure.
- [ ] D4. Les alertes et règles R1-R5 appliquent exactement les seuils du § C9 (25 / 100 / 3 (cap
      10) / 0,6 / 15 / 10 ; R1 ≥ 4 runs ; R2 = R1 + chevauchement ; R5 jamais automatique).
- [ ] D5. La remédiation n'applique jamais une règle de sécurité, ni une règle hors liste, ni plus
      d'1 fichier ; en cas d'échec de post-vérification, tout est restauré.
- [ ] D6. La dérive d'architecture (R6) produit un finding **observation-only** (`observation_only`,
      sévérité basse, action `recalibrate`) après le seuil de runs consécutifs, sans blocage du run
      et sans déclencher curation ni application.

## E. Rapport & signal
- [ ] E1. Le rapport est écrit en **dernier** ; son absence en fin d'exécution est un échec
      signalable.
- [ ] E2. Tous les chiffres du rapport proviennent du calcul (le LLM n'écrit aucun chiffre) ; la
      prose respecte le contrat anti-hallucination (zéro chiffre, sources closes, paraphrase).
- [ ] E3. Les statuts de sortie sont exacts : 0 = complet, 1 = partiel (rapport quand même produit,
      dégradations à l'annexe), 2 = bloquant (arrêt sans rapport).
- [ ] E4. Une panne de veille/contexte ne **tue jamais** le rapport (dégradé, jamais fatal).
- [ ] E5. L'utilisateur accède au rapport via une page interactive publiée dans un dossier connu,
      sans jamais ouvrir le répertoire interne des runs.

## F. Transverse
- [ ] F1. Aucun lien symbolique dans les artefacts projetés ; les fichiers liés sont ignorés.
- [ ] F2. Le diagnostic (C17) vérifie l'installation sans rien écrire, retourne 0/1/2, et signale
      une racine sans répertoire de configuration comme **bloquant**.
- [ ] F3. La revue est reproductible : même ancre + mêmes entrées → mêmes sorties.
- [ ] F4. Le snooze (`ignored_findings`) supprime les constats correspondants et reste édité dans la
      configuration.

---

*Fin de la spécification fonctionnelle.*
