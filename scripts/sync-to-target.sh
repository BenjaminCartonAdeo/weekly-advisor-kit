#!/usr/bin/env bash
#
# sync-to-target.sh — copie le kit (source de vérité) vers la cible du cron.
#
# TOPOLOGIE
#   kit   : <ce repo>                    (source de vérité, où l'on code)
#   cible : dépôt qui exécute le cron    (ex. ../Adeo, cf. INSTALL.md §2.10)
#
# PÉRIMÈTRES SYNCHRONISÉS (2, volontairement étroits)
#   1. .opencode/plugins/weekly-advisor-engine   (moteur Python)
#   2. .opencode/skills/weekly*/                 (une skill par répertoire)
#
# UNITÉS = UNION kit ∪ cible : un répertoire weekly* présent SEULEMENT côté
# cible est planifié en DEL puis supprimé par --apply. Sans cela une
# compétence weekly orpheline (renommage/archivage côté kit) resterait
# indéfiniment dans le dépôt du cron.
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
ENGINE_REL=".opencode/plugins/weekly-advisor-engine"
SKILLS_REL=".opencode/skills"

# Noms exclus du périmètre (arborescence + contenu). doit rester aligné avec
# RSYNC_EXCLUDES ci-dessous et avec check-drift.sh.
WA_EXCLUDES=(
  __pycache__
  .ruff_cache
  .pytest_cache
  "*.egg-info"
  .venv
  dist
  reports
  .git
  node_modules
  weekly-telemetry-config.json
)

usage() {
  cat <<'EOF'
sync-to-target.sh — copie le kit (source de vérité) vers la cible du cron.

  scripts/sync-to-target.sh            # plan seul (dry-run, DÉFAUT, n'écrit rien)
  scripts/sync-to-target.sh --dry-run  # idem, explicite
  scripts/sync-to-target.sh --apply    # écrit réellement (cible + rsync requis)
  scripts/sync-to-target.sh --apply --target /chemin/cible

Env : WEEKLY_SYNC_TARGET (racine de la cible ; défaut : <dirname du kit>/Adeo)
Périmètres : .opencode/plugins/weekly-advisor-engine et .opencode/skills/weekly*/
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

# Plan de transfert $1 (src) → $2 (dst) : "ADD|UPD|DEL <chemin relatif>".
# LC_ALL=C sur chaque comm : les listes sont triées en collation C (list_files),
# comm doit comparer dans la même collation.
transfer_plan() {
  local src=$1 dst=$2 sl dl common
  # Réarmé localement : transfer_plan tourne dans $( ... ), or bash n'y propage
  # pas le trap du script parent.
  local TMPFILES=()
  trap cleanup EXIT
  sl=$(mktemp); dl=$(mktemp)
  TMPFILES+=("$sl" "$dl")
  # Source absente = unité orpheline : liste vide côté kit, donc tout le contenu
  # cible sort en DEL (pas de find sur un chemin inexistant → pas de bruit).
  if [ -d "$src" ]; then list_files "$src" >"$sl"; else : >"$sl"; fi
  if [ -d "$dst" ]; then list_files "$dst" >"$dl"; else : >"$dl"; fi
  LC_ALL=C comm -13 "$dl" "$sl" | sed 's/^/ADD /'
  LC_ALL=C comm -23 "$dl" "$sl" | sed 's/^/DEL /'
  LC_ALL=C comm -12 "$dl" "$sl" | while read -r common; do
    if ! cmp -s "$src/$common" "$dst/$common"; then
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
# Une « unité » = un répertoire source et son homologue cible.
units=()
units+=("$ROOT/$ENGINE_REL" "$TARGET/$ENGINE_REL")
while read -r name; do
  [ -n "$name" ] || continue
  units+=("$ROOT/$SKILLS_REL/$name" "$TARGET/$SKILLS_REL/$name")
done < <(weekly_skill_names)

total=0
for ((i = 0; i < ${#units[@]}; i += 2)); do
  src=${units[i]}; dst=${units[i + 1]}
  label=${src#"$ROOT/"}
  printf '── %s\n' "$label"
  plan=$(transfer_plan "$src" "$dst" || true)
  if [ -z "$plan" ]; then
    printf '   (déjà aligné)\n\n'
    continue
  fi
  # Unité orpheline : l'action utile est la suppression du répertoire, pas la
  # liste de ses fichiers. On l'annonce explicitement (les lignes « - » suivent).
  if [ ! -d "$src" ]; then
    printf '   DEL %s (répertoire weekly* absent du kit)\n' "$label"
  fi
  while read -r action rel; do
    case $action in
      ADD) printf '   + %s\n' "$rel" ;;
      UPD) printf '   M %s\n' "$rel" ;;
      DEL) printf '   - %s\n' "$rel" ;;
    esac
    total=$((total + 1))
  done <<<"$plan"
  if [ "$MODE" = "apply" ]; then
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
    fi
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
