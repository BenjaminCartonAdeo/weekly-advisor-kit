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
#   * une compétence NON weekly*, ou un répertoire hors du périmètre ci-dessous,
#     n'est jamais comparé ;
#   * les exclusions WA_EXCLUDES et les TOLÉRANCES listées plus bas.
#   Une cible absente n'est pas une dérive : voir CIOC plus bas (exit 0).
#
# TOPOLOGIE
#   kit   : <ce repo>                    (source de vérité)
#   cible : dépôt qui exécute le cron    (ex. ../Adeo, cf. INSTALL.md §2.10)
#
# PÉRIMÈTRES COMPARÉS (identiques à scripts/sync-to-target.sh — 6 unités)
#   1. .opencode/plugins/weekly-advisor-engine   (moteur Python)
#   2. .opencode/skills/weekly*/                 (une skill par répertoire)
#   3. .opencode/plugins/weekly-advisor          (plugin TypeScript)
#   4. .opencode/agents/weekly-advisor           (agents markdown)
#   5. .opencode/commands/weekly-review.md       (commande — unité FICHIER)
#   6. .opencode/plugins/weekly-advisor.ts       (point d'entrée — unité FICHIER)
#   Ces 6 unités = tout ce que le cron exécute pour la revue hebdomadaire.
#   Un périmètre qui en oublie une ne se voit pas : le verdict reste vert sur
#   du code périmé. C'est ce qui est arrivé avec les unités 3, 4 et 5, jamais
#   gardées depuis l'introduction de ce script — puis avec l'unité 6, le
#   FICHIER qu'OpenCode charge pour enregistrer ces outils. Une entrée périmée
#   ne casse pas les 5 autres unités : elle casse le plugin entier, en silence.
#
# TYPE D'UNITÉ : `d` répertoire (comparaison récursive via find) ou `f` un seul
# fichier. L'unité fichier est traitée explicitement : la faire passer par les
# helpers directory-shaped (`[ -d ]`, find) comparerait deux listes vides et
# renverrait « aligné » — un garde-fou qui ment.
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

# Mêmes noms exclus que sync-to-target.sh (WA_EXCLUDES côté sync).
# `node_modules` et `dist` valent aussi pour le plugin TypeScript (dépendances
# installées en local, sortie de build) : les motifs sont lus par `find -name`,
# donc sur le NOM DE BASE à n'importe quelle profondeur — jamais ancrés sur le
# chemin relatif, sinon `^dist$` ne matche pas `dist/weekly_advisor-0.4.1-…whl`
# et le wheel part en dérive à chaque build (régression déjà payée ici).
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

# Les 6 unités du périmètre cron (cf. en-tête + unit_lines). Chemins relatifs à
# la racine kit ET à la racine cible : mêmes noms des deux côtés.
ENGINE_REL=".opencode/plugins/weekly-advisor-engine"
PLUGIN_REL=".opencode/plugins/weekly-advisor"
AGENTS_REL=".opencode/agents/weekly-advisor"
COMMAND_REL=".opencode/commands/weekly-review.md"
ENTRYPOINT_REL=".opencode/plugins/weekly-advisor.ts"
SKILLS_REL=".opencode/skills"

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

# Les unités du périmètre, une par ligne : "<type><TAB><chemin relatif>", où
# <type> vaut `d` (répertoire) ou `f` (fichier unique). SOURCE UNIQUE de la
# liste d'unités : ce corps doit être identique dans scripts/sync-to-target.sh
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
}

# Fichiers à comparer pour une unité, chemin relatif à l'unité, tri C :
#   unité `d` → list_files (récursif, exclusions appliquées) ;
#   unité `f` → le nom du fichier, ou RIEN s'il n'existe pas. Une absence
#   produit une liste vide, donc l'autre côté apparaît en manquant : c'est ce qui
#   rend la suppression du fichier visible au lieu d'être un verdict vert.
unit_file_list() {
  local unit=$1 kind=$2
  case $kind in
    d) list_files "$unit" ;;
    f) [ -f "$unit" ] && printf '%s\n' "$(basename -- "$unit")" || : ;;
    *) printf 'type d'"'"'unité inconnu : %s\n' "$kind" >&2; return 2 ;;
  esac
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

# Compare une unité : $1 = src kit, $2 = dst cible, $3 = type (`d`|`f`).
# L'en-tête nomme l'unité, les lignes de dérive nomment le fichier RELATIF à
# l'unité — sauf pour une unité fichier, dont le chemin relatif est l'unité.
check_unit() {
  local src=$1 dst=$2 kind=$3 label sl dl common count=0
  label=${src#"$ROOT/"}
  printf '── %s\n' "$label"

  # ── unité fichier ────────────────────────────────────────────────────────
  # Les quatre issues sont énumérées explicitement : aucune ne peut produire un
  # verdict vert. C'est le point de rupture entre les helpers directory-shaped
  # (`[ -d ]`, find) et une unité qui est un fichier.
  if [ "$kind" = "f" ]; then
    if [ ! -f "$src" ]; then
      printf '   DRIFT en trop  : %s\n' "$label"
      printf '      La cible contient ce fichier mais le kit ne l'"'"'a plus ; supprime-le côté cible ou restaure-le côté kit.\n'
      drift=1; printf '\n'; return 0
    fi
    if [ ! -f "$dst" ]; then
      printf '   DRIFT absent : %s\n' "$label"
      printf '      La cible ne contient pas ce fichier (absent, ou pas un fichier).\n'
      printf '      Corriger : bash scripts/sync-to-target.sh --apply\n'
      drift=1; printf '\n'; return 0
    fi
    if cmp -s "$src" "$dst"; then
      printf '   aligné\n\n'
    else
      printf '   DRIFT différent : %s\n' "$label"
      printf '      Le fichier est présent des deux côtés mais son contenu diffère.\n'
      drift=1; printf '\n'
    fi
    return 0
  fi

  # ── unité répertoire ─────────────────────────────────────────────────────
  # Unité présente SEULEMENT côté cible : le kit ne connaît pas ce répertoire
  # weekly*. Rien à comparer fichier à fichier — c'est une dérive de structure.
  if [ ! -d "$src" ]; then
    printf '   DRIFT en trop  : %s\n' "$label"
    if [ "${label#"$SKILLS_REL"/}" != "$label" ]; then
      printf '      Ce répertoire weekly* n'"'"'existe pas dans le kit ; supprime-le ou ajoute la compétence côté kit.\n'
    else
      printf '      Cette unité n'"'"'existe pas dans le kit ; supprime-la côté cible ou ajoute-la au kit.\n'
    fi
    drift=1
    printf '\n'
    return 0
  fi
  if [ ! -d "$dst" ]; then
    printf '   DRIFT absent : %s\n' "$label"
    printf '      La cible ne contient pas ce répertoire.\n'
    printf '      Corriger : bash scripts/sync-to-target.sh --apply\n'
    drift=1
    printf '\n'
    return 0
  fi
  sl=$(mktemp); dl=$(mktemp)
  TMPFILES+=("$sl" "$dl")
  # Par unit_file_list, pas list_files direct : la liste comparée est alors
  # produite par la même fonction que celle du plan de sync.
  unit_file_list "$src" "$kind" >"$sl"
  unit_file_list "$dst" "$kind" >"$dl"
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

while IFS=$'\t' read -r kind rel; do
  [ -n "$rel" ] || continue
  check_unit "$ROOT/$rel" "$TARGET/$rel" "$kind"
done < <(unit_lines)

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
