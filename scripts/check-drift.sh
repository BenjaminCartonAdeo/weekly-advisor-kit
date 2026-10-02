#!/usr/bin/env bash
#
# check-drift.sh — détecte la dérive entre le kit (source de vérité) et la cible
# du cron.
#
# VERDICT — exit ≠ 0 dès que, dans une unité comparée :
#   * un fichier diverge, disparaît (manquant côté cible) ou apparaît en trop ;
#   * une unité weekly* manque d'un côté — y compris un répertoire weekly*
#     présent SEULEMENT côté cible (compétence weekly orpheline).
#
# LIMITES — hors verdict, par conception :
#   * une compétence NON weekly*, ou un répertoire hors des deux périmètre
#     ci-dessous, n'est jamais comparé ;
#   * les exclusions WA_EXCLUDES et les TOLÉRANCES listées plus bas.
#   Une cible absente n'est pas une dérive : voir CIOC plus bas (exit 0).
#
# TOPOLOGIE
#   kit   : <ce repo>                    (source de vérité)
#   cible : dépôt qui exécute le cron    (ex. ../Adeo, cf. INSTALL.md §2.10)
#
# PÉRIMÈTRES COMPARÉS (identiques à scripts/sync-to-target.sh)
#   1. .opencode/plugins/weekly-advisor-engine
#   2. .opencode/skills/weekly*/
#
# UNITÉS = UNION kit ∪ cible. Un répertoire weekly* présent seulement dans la
# cible est comparé comme « en trop » : c'est précisément le cas que ce
# garde-fou existe pour attraper (compétence weekly laissée derrière après un
# renommage ou un archivage côté kit).
#
# TOLÉRANCES — signalées, jamais silencieuses
#   weekly-telemetry-config.json               valeurs par-deployment
#                                              (project_root, output_dir)
#   weekly_advisor.egg-info/SOURCES.txt        artefact de build
#   Elles sont exclues du verdict mais imprimées sur stdout à chaque run, pour
#   qu'une divergence anormale sur ces chemins reste visible.
#
# CIOC : cible absente = rien à comparer sur ce runner → SKIP propre, exit 0.
# Un répertoire cible PRÉSENT mais non déployé = dérive réelle, exit 1.
#
# USAGE
#   scripts/check-drift.sh
#   scripts/check-drift.sh --target /chemin/cible
#   WEEKLY_SYNC_TARGET=/chemin/cible scripts/check-drift.sh
#
# ENV
#   WEEKLY_SYNC_TARGET   racine de la cible (défaut : $(dirname racine-kit)/Adeo)
#
# SORTIE : 0 aligné (ou cible absente), 1 dérive, 2 usage invalide.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

# Fichiers temporaires : purgés en sortie, interruption comprise. Sans ce trap,
# un Ctrl-C au milieu d'une comparaison laisse des mktemp derrière. Le rm -f
# explicite en fin de check_unit reste (cas nominal) ; le trap est le filet.
TMPFILES=()
cleanup() {
  if [ "${#TMPFILES[@]}" -ne 0 ]; then rm -f -- "${TMPFILES[@]}"; fi
  return 0
}
trap cleanup EXIT

# ─────────────────────────────── configuration ───────────────────────────────
# Défaut UNIQUE de la cible : doit rester identique à celui de
# scripts/sync-to-target.sh (scripts/tests/sync-drift.test.mjs verrouille
# l'égalité des deux déclarations).
TARGET_DEFAULT="$(dirname -- "$ROOT")/Adeo"
ENGINE_REL=".opencode/plugins/weekly-advisor-engine"
SKILLS_REL=".opencode/skills"

# Mêmes noms exclus que sync-to-target.sh (WA_EXCLUDES côté sync).
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

# Chemins tolérés : "<relatif au périmètre>|<libellé>". Déclarés ici pour que le
# signal reste explicite même quand l'exclusion est appliquée en amont.
TOLERATED=(
  "weekly-telemetry-config.json|config par-deployment (project_root, output_dir)"
  "weekly_advisor.egg-info/SOURCES.txt|artefact de build"
)

usage() {
  cat <<'EOF'
check-drift.sh — détecte la dérive kit → cible du cron.

  scripts/check-drift.sh                    # cible par défaut (<dirname kit>/Adeo)
  scripts/check-drift.sh --target /chemin/cible

Codes : 0 aligné ou cible absente (skip), 1 dérive, 2 usage invalide.
EOF
}

# Fichiers sous $1 (racine absolue), chemin relatif, tri C déterministe.
# Les répertoires exclus sont élagués (-prune) : une exclusion de répertoire
# retire tout son contenu. LC_ALL=C sur sort ET comm (collation identique).
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

# Répertoires weekly* — UNION kit ∪ cible, un nom par ligne.
#   passe 1 : les répertoires du kit — ordre et libellés historiques conservés ;
#   passe 2 : les répertoires présents SEULEMENT côté cible. Ignorer ceux-là
#             rendrait le garde-fou aveugle au cas qu'il cible (compétence
#             orpheline), et --delete côté sync ne les supprimerait jamais.
# $TARGET est résolu à l'appel (défini plus bas), pas à la définition.
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

# ─────────────────────────────────── usage ───────────────────────────────────
TARGET="${WEEKLY_SYNC_TARGET:-$TARGET_DEFAULT}"
while [ $# -gt 0 ]; do
  case $1 in
    --target)
      [ $# -ge 2 ] || { printf 'ERREUR --target exige un chemin\n' >&2; exit 2; }
      TARGET=$2; shift ;;
    --target=*) TARGET=${1#--target=} ;;
    -h | --help) usage; exit 0 ;;
    *) printf 'ERREUR argument inconnu : %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done
[ -n "$TARGET" ] || { printf 'ERREUR cible vide\n' >&2; exit 2; }
TARGET="${TARGET%/}"

printf 'kit    : %s\n' "$ROOT"
printf 'cible  : %s\n' "$TARGET"

# Cible absente : rien à comparer (runner CI, poste non déployé) → skip, pas rouge.
if [ ! -d "$TARGET" ]; then
  printf 'SKIP : cible absente — rien à comparer (drift non applicable ici).\n'
  printf '      Pour comparer une vraie cible : --target /chemin/cible\n'
  exit 0
fi

drift=0

# Signale les tolérances (présence + divergence) sans les compter comme dérive.
signal_tolerated() {
  local unit_src=$1 unit_dst=$2 label entry rel
  for entry in "${TOLERATED[@]}"; do
    label=${entry#*|}
    rel=${entry%%|*}
    if [ -f "$unit_src/$rel" ] || [ -f "$unit_dst/$rel" ]; then
      if cmp -s "$unit_src/$rel" "$unit_dst/$rel"; then
        printf '   [toléré] %s — %s (identique)\n' "$rel" "$label"
      else
        printf '   [toléré] %s — %s (diffère, hors verdict)\n' "$rel" "$label"
      fi
    fi
  done
}

check_unit() {
  local src=$1 dst=$2 label sl dl common count=0
  label=${src#"$ROOT/"}
  printf '── %s\n' "$label"
  # Unité présente SEULEMENT côté cible : le kit ne connaît pas ce répertoire
  # weekly*. Rien à comparer fichier à fichier — c'est une dérive de structure.
  if [ ! -d "$src" ]; then
    printf '   DRIFT en trop  : %s\n' "$label"
    printf '      Ce répertoire weekly* n'"'"'existe pas dans le kit ; supprime-le ou ajoute la compétence côté kit.\n'
    drift=1
    printf '\n'
    return 0
  fi
  if [ ! -d "$dst" ]; then
    printf '   DRIFT absent : %s\n' "$label"
    printf '      La cible ne contient pas ce répertoire.\n'
    drift=1
    printf '\n'
    return 0
  fi
  sl=$(mktemp); dl=$(mktemp)
  TMPFILES+=("$sl" "$dl")
  list_files "$src" >"$sl"
  list_files "$dst" >"$dl"
  while read -r common; do
    printf '   DRIFT manquant : %s\n' "$common"
    count=$((count + 1))
  done < <(LC_ALL=C comm -23 "$sl" "$dl")
  while read -r common; do
    printf '   DRIFT en trop  : %s\n' "$common"
    count=$((count + 1))
  done < <(LC_ALL=C comm -13 "$sl" "$dl")
  while read -r common; do
    if ! cmp -s "$src/$common" "$dst/$common"; then
      printf '   DRIFT différent : %s\n' "$common"
      count=$((count + 1))
    fi
  done < <(LC_ALL=C comm -12 "$sl" "$dl")
  rm -f "$sl" "$dl"
  signal_tolerated "$src" "$dst"
  if [ "$count" -eq 0 ]; then
    printf '   aligné\n'
  else
    printf '   %d divergence(s)\n' "$count"
    drift=1
  fi
  printf '\n'
}

check_unit "$ROOT/$ENGINE_REL" "$TARGET/$ENGINE_REL"
while read -r name; do
  [ -n "$name" ] || continue
  check_unit "$ROOT/$SKILLS_REL/$name" "$TARGET/$SKILLS_REL/$name"
done < <(weekly_skill_names)

if [ "$drift" -eq 0 ]; then
  printf 'OK : kit et cible alignés.\n'
  exit 0
fi

cat >&2 <<EOF

DRIFT détecté : la cible ne reflète pas le kit.

  Corriger :  bash scripts/sync-to-target.sh --dry-run    # relire le plan
             bash scripts/sync-to-target.sh --apply      # transférer
             bash scripts/check-drift.sh                 # re-vérifier
EOF
if [ "$TARGET" != "$TARGET_DEFAULT" ]; then
  printf '  (cible : --target %s)\n' "$TARGET" >&2
fi
exit 1
