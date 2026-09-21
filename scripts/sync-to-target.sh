#!/usr/bin/env bash
#
# sync-to-target.sh — copie le kit (source de vérité) vers la cible du cron.
#
# TOPOLOGIE
#   kit   : <ce repo>                    (source de vérité, où l'on code)
#   cible : dépôt qui exécute le cron    (ex. ../Adeo, cf. INSTALL.md §2.10)
#
# PÉRIMÈTRES SYNCHRONISÉS (7 unités : 5 répertoires + 2 fichiers)
#   1. .opencode/plugins/weekly-advisor-engine   (moteur Python)
#   2. .opencode/skills/weekly*/                 (une skill par répertoire)
#   3. .opencode/plugins/weekly-advisor          (plugin TypeScript)
#   4. .opencode/agents/weekly-advisor           (agents markdown)
#   5. .opencode/commands/weekly-review.md       (commande — unité FICHIER)
#   6. .opencode/plugins/weekly-advisor.ts       (point d'entrée — unité FICHIER)
#   7. .harness-eval/rules                       (jeu de règles harness-eval)
#   Les 6 premières constituent le périmètre du cron : tout ce que le cron
#   exécute pour la revue hebdomadaire. Hors périmètre, le cron tourne sur du
#   code que ce kit ne garantit pas.
#
#   L'unité 7 est le seul répertoire du périmètre hors `.opencode/`. Elle n'est
#   pas exécutée par le cron : elle est lue par la gate de portabilité
#   (`harness-eval skill-verify`), qui ne charge les règles que depuis la racine
#   qu'elle résout — `WEEKLY_KIT_ROOT`, donc la CIBLE côté cron
#   (plugin/paths.ts resolveKitRoot). Un jeu de règles périmé côté cible fait
#   passer la gate sur des critères qui n'ont plus rien à voir avec le kit :
#   commit-draft refusé à tort, ou accepté à tort. Ni ce script ni le moteur
#   (weekly_telemetry_aggregator/harness_scope.py harness_rules_fingerprint)
#   ne lisaient ce répertoire. C'est un MANQUE DE COUVERTURE, pas une divergence
#   observée : au moment de l'ajout, les deux copies de portability.yaml étaient
#   identiques octet pour octet (sha256 8d65289f…). Rien n'était cassé ; c'est
#   l'absence de garde qui aurait laissé dériver sans signal.
#
# UNITÉS = UNION kit ∪ cible : un répertoire weekly* présent SEULEMENT côté
# cible est planifié en DEL puis supprimé par --apply. Sans cela une
# compétence weekly orpheline (renommage/archivage côté kit) resterait
# indéfiniment dans le dépôt du cron.
#
# TYPE D'UNITÉ. Une unité `d` est un répertoire (comparaison récursive via
# find), une unité `f` est UN SEUL fichier. Les deux existent : l'unité fichier
# ne peut pas passer par les helpers directory-shaped (`find`, `[ -d ]`) — une
# garde qui ne compare rien faute de fichier est pire que pas de garde, elle
# certifie un alignement inexistant.
#
# SÛRETÉ
#   --dry-run est le DÉFAUT : sans argument, ce script n'écrit RIEN. --apply est
#   obligatoire pour transférer, et refuse de s'exécuter si la cible n'existe pas
#   (on ne crée pas un dépôt par accident).
#
# EXCLUSIONS (partagées avec scripts/check-drift.sh — voir ENUMÉRATION CI-DESSOUS)
#   caches Python, venv, rapports produits par le run, et le fichier de config
#   par-deployment `weekly-telemetry-config.json` (il contient project_root et
#   output_dir : ces valeurs sont propres à chaque poste).
#   ATTENTION rsync : un motif --exclude protège la copie cible de --delete
#   (seul --delete-excluded supprimerait un fichier exclu côté cible). Le
#   `weekly-telemetry-config.json` de la cible est donc conservé, pas écrasé.
#
# USAGE
#   scripts/sync-to-target.sh                     # plan (dry-run, défaut)
#   scripts/sync-to-target.sh --dry-run           # idem, explicite
#   scripts/sync-to-target.sh --apply             # écrit réellement
#   scripts/sync-to-target.sh --apply --target /chemin/cible
#   WEEKLY_SYNC_TARGET=/chemin/cible scripts/sync-to-target.sh --apply
#
# ENV
#   WEEKLY_SYNC_TARGET   racine de la cible (défaut : $(dirname racine-kit)/Adeo)
#
# SORTIE : 0 succès (dry-run ou apply), 1 erreur d'exécution, 2 usage invalide.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

# Fichiers temporaires : purgés en sortie, interruption comprise. Bash remet les
# traps à leur valeur héritée dans un sous-shell de substitution de commande, le
# trap est donc aussi réarmé localement dans transfer_plan (voir plus bas).
TMPFILES=()
cleanup() {
  if [ "${#TMPFILES[@]}" -ne 0 ]; then rm -f -- "${TMPFILES[@]}"; fi
  return 0
}
trap cleanup EXIT

# ─────────────────────────────── configuration ───────────────────────────────
# Défaut UNIQUE de la cible : toute modification doit être répercutée à
# l'identique dans scripts/check-drift.sh (scripts/tests/sync-drift.test.mjs
# verrouille l'égalité des deux déclarations).
# Noms exclus du périmètre (arborescence + contenu). Une seule liste alimente le
# plan (find) et les --exclude du rsync : toute modification doit être
# répercutée à l'identique dans scripts/check-drift.sh (le test
# « les deux scripts déclarent la même cible par défaut et les mêmes exclusions »
# verrouille l'égalité des deux déclarations).
#
# `dist` est ici parce que `uv build` écrit wheel + sdist dans l'arborescence du
# moteur. Ces fichiers sont ignorés par git — donc invisibles dans un diff — mais
# bien présents sur le disque, donc candidats au transfert. Déployer un wheel
# dans la cible du cron doublait la version embarquée et aurait créé une fausse
# dérive au premier nettoyage de dist/.
TARGET_DEFAULT="$(dirname -- "$ROOT")/Adeo"

# Noms exclus du périmètre (arborescence + contenu). doit rester aligné avec
# RSYNC_EXCLUDES ci-dessous et avec check-drift.sh.
# `node_modules` et `dist` couvrent aussi le plugin TypeScript (dépendances
# installées en local, sortie de build) : les motifs sont Lus par `find -name`,
# donc sur le NOM DE BASE à n'importe quelle profondeur — jamais ancrés sur le
# chemin relatif, sinon `^dist$` ne matche pas `dist/weekly_advisor-0.4.1-…whl`.
WA_EXCLUDES=(
  __pycache__
  .ruff_cache
  .pytest_cache
  "*.egg-info"
  .venv
  venv
  dist
  reports
  .git
  node_modules
  weekly-telemetry-config.json
)

# Les 7 unités du périmètre cron (cf. en-tête + unit_lines). Les chemins sont
# relatifs à la racine kit ET à la racine cible : même nom des deux côtés.
ENGINE_REL=".opencode/plugins/weekly-advisor-engine"
PLUGIN_REL=".opencode/plugins/weekly-advisor"
AGENTS_REL=".opencode/agents/weekly-advisor"
COMMAND_REL=".opencode/commands/weekly-review.md"
ENTRYPOINT_REL=".opencode/plugins/weekly-advisor.ts"
SKILLS_REL=".opencode/skills"
RULES_REL=".harness-eval/rules"

usage() {
  cat <<'EOF'
sync-to-target.sh — copie le kit (source de vérité) vers la cible du cron.

  scripts/sync-to-target.sh            # plan seul (dry-run, DÉFAUT, n'écrit rien)
  scripts/sync-to-target.sh --dry-run  # idem, explicite
  scripts/sync-to-target.sh --apply    # écrit réellement (cible + rsync requis)
  scripts/sync-to-target.sh --apply --target /chemin/cible

Env : WEEKLY_SYNC_TARGET (racine de la cible ; défaut : <dirname du kit>/Adeo)
Périmètres (7 unités) :
  .opencode/plugins/weekly-advisor-engine   .opencode/skills/weekly*/
  .opencode/plugins/weekly-advisor          .opencode/agents/weekly-advisor
  .opencode/commands/weekly-review.md       (unité fichier)
  .opencode/plugins/weekly-advisor.ts       (unité fichier — point d'entrée)
  .harness-eval/rules                       (règles harness-eval — gate portabilité)
Codes : 0 succès, 1 erreur d'exécution, 2 usage invalide.
EOF
}

die() {
  printf 'ERREUR %s\n' "$*" >&2
  exit "${2:-1}"
}

# Fichiers sous $1 (racine absolue), chemin relatif, tri C déterministe.
# Les répertoires exclus sont élagués (-prune) : une exclusion de répertoire
# retire tout son contenu, pas seulement son nom. LC_ALL=C sur sort ET comm :
# sinon collations divergentes et comm signale « not sorted ».
list_files() {
  local root=$1 expr=() name sep=""
  for name in "${WA_EXCLUDES[@]}"; do
    if [ -n "$sep" ]; then expr+=("$sep"); fi
    expr+=(-name "$name"); sep="-o"
  done
  find "$root" \( "${expr[@]}" \) -prune -o -type f -print \
    | sed "s|^${root}/||" \
    | LC_ALL=C sort
}

# Répertoires weekly* — UNION kit ∪ cible, un nom par ligne. Même implémentation
# et même raison que scripts/check-drift.sh : sans la passe 2, --delete n'a
# aucune unité sur laquelle s'appliquer et l'orphelin survit au --apply.
weekly_skill_names() {
  local dir name
  local -A seen=()
  for dir in "$ROOT/$SKILLS_REL"/weekly*/; do
    [ -d "$dir" ] || continue
    name=$(basename "${dir%/}")
    seen["$name"]=1
    printf '%s\n' "$name"
  done
  for dir in "$TARGET/$SKILLS_REL"/weekly*/; do
    [ -d "$dir" ] || continue
    name=$(basename "${dir%/}")
    if [ -n "${seen["$name"]+set}" ]; then continue; fi
    printf '%s\n' "$name"
  done
}

# Les unités du périmètre, une par ligne : "<type><TAB><chemin relatif>", où
# <type> vaut `d` (répertoire) ou `f` (fichier unique). SOURCE UNIQUE de la
# liste d'unités : ce corps doit être identique dans scripts/check-drift.sh
# (verrouillé par le test « les deux scripts déclarent les mêmes unités »).
unit_lines() {
  printf 'd\t%s\n' "$ENGINE_REL"
  local name
  while read -r name; do
    [ -n "$name" ] || continue
    printf 'd\t%s\n' "$SKILLS_REL/$name"
  done < <(weekly_skill_names)
  printf 'd\t%s\n' "$PLUGIN_REL"
  printf 'd\t%s\n' "$AGENTS_REL"
  printf 'f\t%s\n' "$COMMAND_REL"
  printf 'f\t%s\n' "$ENTRYPOINT_REL"
  printf 'd\t%s\n' "$RULES_REL"
}

# Fichiers à comparer pour une unité, chemin relatif à l'unité, tri C :
#   unité `d` → list_files (récursif, exclusions appliquées) ;
#   unité `f` → le nom du fichier, ou RIEN s'il n'existe pas. Une absence
#   produit une liste vide, donc l'autre côté apparaît en ADD/DEL : c'est ce qui
#   rend la suppression du fichier visible au lieu d'être un verdict vert.
unit_file_list() {
  local unit=$1 kind=$2
  case $kind in
    d) list_files "$unit" ;;
    f) [ -f "$unit" ] && printf '%s\n' "$(basename -- "$unit")" || : ;;
    *) printf 'type d'"'"'unité inconnu : %s\n' "$kind" >&2; return 2 ;;
  esac
}

# Plan de transfert $1 (src) → $2 (dst) : "ADD|UPD|DEL <chemin relatif>".
# $3 = type d'unité (`d`|`f`).
# LC_ALL=C sur chaque comm : les listes sont triées en collation C (unit_file_list),
# comm doit comparer dans la même collation.
transfer_plan() {
  local src=$1 dst=$2 kind=$3 sl dl common sbase dbase
  # Réarmé localement : transfer_plan tourne dans $( ... ), or bash n'y propage
  # pas le trap du script parent.
  local TMPFILES=()
  trap cleanup EXIT
  # Base de comparaison = le répertoire qui CONTIENT les noms listés. Pour une
  # unité `d` c'est l'unité elle-même ; pour une unité `f` c'est son parent —
  # sinon `$src/$common` vaudrait `…/weekly-review.md/weekly-review.md`,
  # inexistant, et TOUTE unité fichier serait annoncée UPD à chaque run.
  sbase=$src; dbase=$dst
  if [ "$kind" = "f" ]; then
    sbase=$(dirname -- "$src"); dbase=$(dirname -- "$dst")
  fi
  sl=$(mktemp); dl=$(mktemp)
  TMPFILES+=("$sl" "$dl")
  # Unité absente du kit = orpheline : liste vide côté kit, donc tout le contenu
  # cible sort en DEL (pas de list_files sur un chemin inexistant → pas de bruit).
  unit_file_list "$src" "$kind" >"$sl" || : >"$sl"
  unit_file_list "$dst" "$kind" >"$dl" || : >"$dl"
  LC_ALL=C comm -13 "$dl" "$sl" | sed 's/^/ADD /'
  LC_ALL=C comm -23 "$dl" "$sl" | sed 's/^/DEL /'
  LC_ALL=C comm -12 "$dl" "$sl" | while read -r common; do
    if ! cmp -s "$sbase/$common" "$dbase/$common"; then
      printf 'UPD %s\n' "$common"
    fi
  done
  rm -f "$sl" "$dl"
}

rsync_excludes() {
  local name
  for name in "${WA_EXCLUDES[@]}"; do printf -- "--exclude=%s\n" "$name"; done
}

# ─────────────────────────────────── usage ───────────────────────────────────
MODE="dry-run"
TARGET="${WEEKLY_SYNC_TARGET:-$TARGET_DEFAULT}"
while [ $# -gt 0 ]; do
  case $1 in
    --dry-run) MODE="dry-run" ;;
    --apply) MODE="apply" ;;
    --target)
      [ $# -ge 2 ] || die "--target exige un chemin" 2
      TARGET=$2; shift ;;
    --target=*) TARGET=${1#--target=} ;;
    -h | --help) usage; exit 0 ;;
    *) printf 'ERREUR argument inconnu : %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

[ -n "$TARGET" ] || die "cible vide" 2
TARGET="${TARGET%/}"

printf 'kit    : %s\n' "$ROOT"
printf 'cible  : %s\n' "$TARGET"
printf 'mode   : %s\n' "$MODE"
if [ "$MODE" = "apply" ]; then
  [ -d "$TARGET" ] || die "cible absente : $TARGET (crée-la ou passe --dry-run)"
  command -v rsync >/dev/null 2>&1 || die "rsync requis pour --apply (apt install rsync / dnf install rsync) ; --dry-run fonctionne sans rsync"
else
  command -v rsync >/dev/null 2>&1 || printf 'note   : rsync absent — plan uniquement, --apply indisponible\n'
fi
printf '\n'

# ──────────────────────────────── plan / apply ───────────────────────────────
# Une « unité » = une entrée "<type><TAB><relatif>" et ses homologues kit/cible.
# Types : `d` répertoire, `f` fichier unique.
units=()
while IFS=$'\t' read -r kind rel; do
  [ -n "$rel" ] || continue
  units+=("$kind"$'\t'"$rel")
done < <(unit_lines)

total=0
for ((i = 0; i < ${#units[@]}; i += 1)); do
  entry=${units[i]}
  kind=${entry%%$'\t'*}
  rel=${entry#*$'\t'}
  src=$ROOT/$rel; dst=$TARGET/$rel; label=$rel
  printf '── %s\n' "$label"
  plan=$(transfer_plan "$src" "$dst" "$kind" || true)
  if [ -z "$plan" ]; then
    printf '   (déjà aligné)\n\n'
    continue
  fi
  # Unité orpheline : l'action utile est la suppression, pas la liste de ses
  # fichiers. On l'annonce explicitement (les lignes « - » suivent).
  case $kind in
    d)
      [ -d "$src" ] || printf '   DEL %s (répertoire weekly* absent du kit)\n' "$label" ;;
    f)
      [ -f "$src" ] || printf '   DEL %s (fichier absent du kit)\n' "$label" ;;
  esac
  while read -r action rel_file; do
    case $action in
      ADD) printf '   + %s\n' "$rel_file" ;;
      UPD) printf '   M %s\n' "$rel_file" ;;
      DEL) printf '   - %s\n' "$rel_file" ;;
    esac
    total=$((total + 1))
  done <<<"$plan"
  if [ "$MODE" = "apply" ]; then
    case $kind in
      d)
        if [ -d "$src" ]; then
          mkdir -p "$dst"
          # shellcheck disable=SC2046  # RSYNC_EXCLUDES est une liste d'arguments
          rsync -a --delete $(rsync_excludes) "$src/" "$dst/"
          printf '   → synchronisé\n'
        else
          # $dst est construit depuis le nom d'un répertoire weekly* trouvé sous
          # $TARGET/$SKILLS_REL : le rm ne peut pas sortir de ce répertoire.
          rm -rf -- "$dst"
          printf '   → répertoire supprimé (absent du kit)\n'
        fi ;;
      f)
        # Unité fichier : ni rsync ni --delete. Un `cp` idempotent suffit, et il
        # n'a pas les hypothèses directory-shaped (`src/` récursif, --delete) à
        # contourner — un `rsync -a src/ dst/` sur un fichier ne transférerait
        # rien et laisserait la dérive invisible.
        if [ -f "$src" ]; then
          mkdir -p -- "$(dirname -- "$dst")"
          cp -p -- "$src" "$dst"
          printf '   → synchronisé\n'
        else
          rm -f -- "$dst"
          printf '   → fichier supprimé (absent du kit)\n'
        fi ;;
    esac
  fi
  printf '\n'
done

if [ "$MODE" = "apply" ]; then
  printf 'APPLY terminé (%d fichier(s) transféré(s)). Vérifie : bash scripts/check-drift.sh\n' "$total"
else
  printf "DRY-RUN : %d fichier(s) à transférer. Rien n'a été écrit.\n" "$total"
  printf 'Pour appliquer : bash scripts/sync-to-target.sh --apply'
  [ "$TARGET" = "$TARGET_DEFAULT" ] || printf ' --target %s' "$TARGET"
  printf '\n'
fi
