#!/usr/bin/env bash
#
# install-opencode-v2.sh — coexistence propre OpenCode V1 + V2 sur une même machine.
#
# POURQUOI CE SCRIPT EXISTE
#   L'installeur officiel V2 (https://opencode.ai/v2/install) n'a aucune option de
#   redirection : INSTALL_DIR est codé en dur à $HOME/.opencode/bin, et il fait
#       mv "$tmp/package/bin/opencode" "$INSTALL_DIR/opencode"
#   Il écrase donc le binaire V1. Pire, son install_legacy_shim() supprime opencode2
#   puis le réécrit comme un alias du binaire qu'il vient d'écraser — donc aucune
#   coexistence possible. Ce script fait le même travail SANS toucher à V1.
#
# INVARIANTS DE SÉCURITÉ (ne jamais relâcher)
#   1. Le binaire V1 n'est jamais modifié : lu, copié, jamais écrit.
#   2. ~/.config/opencode/ n'est jamais touché : il est PARTAGÉ volontairement.
#      Vérifié : opencode.json ne déclare aucune clé "plugin" (le plugin weekly est
#      en auto-découverte depuis .opencode/plugins/), donc le renommage V2
#      "plugin" -> "plugins" ne s'applique pas et le partage de config est sans risque.
#   3. Les DONNÉES sont isolées par version (XDG_DATA_HOME / XDG_STATE_HOME /
#      XDG_CACHE_HOME). Non négociable : opencode.db fait 23 Go et contient les
#      tables SQLite que le moteur weekly-advisor lit. Un schéma migré par V2
#      à la place briserait la revue hebdomadaire, pas OpenCode.
#   4. L'installeur officiel V2 n'est JAMAIS exécuté par ce script.
#
# V1 reste géré par son script historique (https://opencode.ai/install). Ce script
# se contente de FIGER la V1 (copie immuable versionnée) avant d'introduire V2, puis
# de figer le pointeur. Il ne met jamais V1 à jour.
#
# REJOUABLE : réexécuter à volonté pour suivre les mises à jour V2. Idempotent —
# une version déjà installée est réutilisée, les anciennes sont élaguées, V1 n'est
# jamais re-téléchargée si sa copie figée existe.
#
# USAGE
#   scripts/install-opencode-v2.sh                 # installe/aligne la dernière V2
#   scripts/install-opencode-v2.sh --dry-run       # affiche le plan, ne touche à rien
#   scripts/install-opencode-v2.sh --verify        # état courant + vérifications seules
#   scripts/install-opencode-v2.sh --version 2.0.18
#   scripts/install-opencode-v2.sh --force         # réinstalle même si déjà présent
#   scripts/install-opencode-v2.sh --keep 3        # garde 3 versions V2 (défaut 2)
#
# APRÈS INSTALLATION, pour prouver la compatibilité V1/V2 en conditions réelles :
#   . ~/.opencode/versions/opencode-dual.env
#   node scripts/smoke-dual-runtime.mjs --strict
#
set -euo pipefail

# ─────────────────────────────── configuration ───────────────────────────────
OPENCODE_HOME="${OPENCODE_HOME:-$HOME/.opencode}"
BIN_DIR="$OPENCODE_HOME/bin"
VERSIONS_DIR="$OPENCODE_HOME/versions"
V1_PREFIX="opencode-v1"
V2_PREFIX="opencode-v2"
UPDATE_API="https://opencode.ai/update/api/latest/cli/npm"
NPM_REGISTRY="https://registry.npmjs.org"
ENV_FILE="$VERSIONS_DIR/opencode-dual.env"

# Données V2 : chemins codés en dur, pas de ${VAR:-défaut}. Si XDG_DATA_HOME était
# défini globalement, un ${XDG_DATA_HOME:-...} ferait de nouveau partager les
# données avec V1 — exactement ce qu'on veut empêcher.
V2_DATA="$HOME/.local/share/opencode-v2"
V2_STATE="$HOME/.local/state/opencode-v2"
V2_CACHE="$HOME/.cache/opencode-v2"

# ─────────────────────────────── état d'exécution ─────────────────────────────
MODE="install"
FORCE=0
PINNED_VERSION=""
KEEP_VERSIONS="${OPENCODE_KEEP_VERSIONS:-2}"
TMPDIR_RUN=""

# ─────────────────────────────── utilitaires ─────────────────────────────────
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  C_RESET=$'\033[0m'; C_DIM=$'\033[2m'; C_RED=$'\033[31m'
  C_GREEN=$'\033[32m'; C_ORANGE=$'\033[38;5;214m'
else
  C_RESET=""; C_DIM=""; C_RED=""; C_GREEN=""; C_ORANGE=""
fi

step() { printf '%s==>%s %s\n' "$C_ORANGE" "$C_RESET" "$*"; }
info() { printf '    %s\n' "$*"; }
dim()  { printf '    %s%s%s\n' "$C_DIM" "$*" "$C_RESET"; }
ok()   { printf '    %s✓%s %s\n' "$C_GREEN" "$C_RESET" "$*"; }
warn() { printf '    %s!%s %s\n' "$C_ORANGE" "$C_RESET" "$*" >&2; }
die()  { printf '%s✗ %s%s\n' "$C_RED" "$*" "$C_RESET" >&2; exit 1; }

cleanup() { [ -n "$TMPDIR_RUN" ] && [ -d "$TMPDIR_RUN" ] && rm -rf "$TMPDIR_RUN"; return 0; }
trap cleanup EXIT

usage() { sed -n '2,/^set -euo/p' "$0" | sed 's/^# \{0,1\}//; $d'; exit 0; }

# "opencode v2.0.18" ou "1.18.32" -> "2.0.18" / "1.18.32"
normalize_version() {
  local raw="${1:-}"
  raw="${raw##* }"
  raw="${raw#v}"
  printf '%s' "$raw"
}

file_size_mtime() {  # portable, pas de stat -c
  local f="$1"
  [ -f "$f" ] || { echo "absent"; return 0; }
  if stat -c '%s:%Y' "$f" >/dev/null 2>&1; then
    stat -c '%s:%Y' "$f"
  else
    echo "$(wc -c < "$f" | tr -d ' '):$(stat -f '%m' "$f")"
  fi
}

# ─────────────────────────────── args ────────────────────────────────────────
while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) usage ;;
    --version) PINNED_VERSION="${2:-}"; [ -n "$PINNED_VERSION" ] || die "--version exige un argument"; shift 2 ;;
    --dry-run) MODE="dry-run"; shift ;;
    --verify)  MODE="verify"; shift ;;
    --force)   FORCE=1; shift ;;
    --keep)    KEEP_VERSIONS="${2:-}"; [ -n "$KEEP_VERSIONS" ] || die "--keep exige un argument"; shift 2 ;;
    *) die "option inconnue : $1 (voir --help)" ;;
  esac
done

case "$KEEP_VERSIONS" in (''|*[!0-9]*) die "--keep doit être un entier" ;; esac
[ "$KEEP_VERSIONS" -ge 1 ] || die "--keep doit être >= 1"

run() {  # exécute seulement en mode install
  if [ "$MODE" = "install" ]; then "$@"; fi
}

# ─────────────────────────────── 1. plateforme ────────────────────────────────
detect_target() {
  local raw_os arch is_musl=false needs_baseline=false os

  raw_os=$(uname -s)
  os=$(echo "$raw_os" | tr '[:upper:]' '[:lower:]')
  case "$raw_os" in
    Darwin*) os="darwin" ;;
    Linux*)  os="linux" ;;
    MINGW*|MSYS*|CYGWY*) os="windows" ;;
  esac
  arch=$(uname -m)
  case "$arch" in
    aarch64|arm64) arch="arm64" ;;
    x86_64|amd64)  arch="x64" ;;
  esac

  if [ "$os" = "linux" ]; then
    [ -f /etc/alpine-release ] && is_musl=true
    if command -v ldd >/dev/null 2>&1; then
      ldd --version 2>&1 | grep -qi musl && is_musl=true
    fi
  fi

  if [ "$arch" = "x64" ]; then
    if [ "$os" = "linux" ] && ! grep -qwi avx2 /proc/cpuinfo 2>/dev/null; then
      needs_baseline=true
    fi
    if [ "$os" = "darwin" ] && [ "$(sysctl -n sysctl.proc_translated 2>/dev/null || echo 0)" = "1" ]; then
      needs_baseline=true
    fi
  fi

  local target="${os}-${arch}"
  [ "$needs_baseline" = true ] && target="${target}-baseline"
  [ "$is_musl" = true ] && target="${target}-musl"
  printf '%s' "$target"
}

# ─────────────────────────────── 2. métadonnées V2 ───────────────────────────
resolve_v2_version() {
  if [ -n "$PINNED_VERSION" ]; then
    printf '%s' "${PINNED_VERSION#v}"
    return 0
  fi
  command -v curl >/dev/null 2>&1 || die "curl est requis"
  local meta
  meta=$(curl -fsSL "$UPDATE_API") || die "métadonnées V2 injoignables : $UPDATE_API"
  local version
  version=$(printf '%s' "$meta" | sed -n 's/.*"version":"\([^"]*\)".*/\1/p')
  [ -n "$version" ] || die "version absente de la réponse API"
  printf '%s' "$version"
}

resolve_v2_scope() {
  local meta
  meta=$(curl -fsSL "$UPDATE_API") || die "métadonnées V2 injoignables"
  local pkg
  pkg=$(printf '%s' "$meta" | sed -n 's/.*"package":"\([^"]*\)".*/\1/p')
  [ -n "$pkg" ] || pkg="@opencode/cli"
  printf '%s' "${pkg%/cli}"
}

# ─────────────────────────────── 3. V1 figée ─────────────────────────────────
# Ne met JAMAIS V1 à jour. Si la copie figée de la version courante existe, on
# la réutilise : la V1 reste pilotée par son propre script historique.
freeze_v1() {
  local current="$BIN_DIR/opencode"
  [ -e "$current" ] || die "$current absent — installe d'abord la V1 via https://opencode.ai/install"

  local real version frozen
  real=$(readlink -f "$current")
  version=$(normalize_version "$("$real" --version 2>/dev/null || echo '')")
  [ -n "$version" ] || die "impossible de lire la version de $real"
  frozen="$VERSIONS_DIR/$V1_PREFIX.$version"

  # $frozen est un FICHIER, pas un répertoire : tester -d ne matchait jamais et
  # le script tentait de se copier sur lui-même au second passage.
  if [ -f "$frozen" ] && [ "$FORCE" -eq 0 ]; then
    ok "V1 $version déjà figée ($frozen)"
  elif [ "$real" = "$frozen" ]; then
    ok "V1 $version déjà figée et câblée ($frozen)"
  else
    run mkdir -p "$VERSIONS_DIR"
    if [ "$MODE" = "dry-run" ]; then
      info "[dry-run] copierait $real -> $frozen"
    else
      cp -p "$real" "$frozen"
      chmod 755 "$frozen"
      if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$frozen" > "$frozen.sha256"
      fi
      ok "V1 $version figée ($frozen)"
    fi
  fi

  printf '%s\n' "$frozen" > "$TMPDIR_RUN/v1.path"
}

# ─────────────────────────────── 4. V2 installée ─────────────────────────────
install_v2() {
  local version="$1" target
  target=$(detect_target)
  local dest="$VERSIONS_DIR/$V2_PREFIX.$version"
  local destbin="$dest/opencode"

  if [ -x "$destbin" ] && [ "$FORCE" -eq 0 ]; then
    ok "V2 $version déjà installée ($destbin)"
  else
    local scope pkgname url http
    scope=$(resolve_v2_scope)
    pkgname="$scope/cli-$target"
    url="$NPM_REGISTRY/$pkgname/-/cli-$target-$version.tgz"

    http=$(curl -sI -o /dev/null -w '%{http_code}' "$url" || echo 000)
    # Repli pour les clients antérieurs au renommage de scope.
    if [ "$http" = "404" ] && [ -n "$PINNED_VERSION" ]; then
      pkgname="@opencode-ai/cli-$target"
      url="$NPM_REGISTRY/$pkgname/-/cli-$target-$version.tgz"
      http=$(curl -sI -o /dev/null -w '%{http_code}' "$url" || echo 000)
    fi
    [ "$http" = "200" ] || die "tarball introuvable (HTTP $http) : $url"

    local work="$TMPDIR_RUN/v2-$version"
    mkdir -p "$work"
    if [ "$MODE" = "dry-run" ]; then
      info "[dry-run] téléchargerait $url puis installerait dans $dest"
      printf '%s\n' "$dest" > "$TMPDIR_RUN/v2.path"
      return 0
    fi

    curl -fsSL "$url" -o "$work/pkg.tgz" || die "téléchargement échoué : $url"
    tar -xzf "$work/pkg.tgz" -C "$work" || die "extraction échouée"
    [ -f "$work/package/bin/opencode" ] || die "binaire absent dans le tarball : $work/package/bin/opencode"

    run mkdir -p "$dest"
    # mv (pas cp) : évite un double de 170 Mo et Writing dans le même fs.
    mv -f "$work/package/bin/opencode" "$destbin"
    chmod 755 "$destbin"

    local got
    got=$(normalize_version "$("$destbin" --version 2>/dev/null || echo '')")
    [ "$got" = "$version" ] || die "version incohérente : attendu $version, obtenu '${got:-inconnu}'"
    ok "V2 $version installée ($destbin)"
  fi

  printf '%s\n' "$dest" > "$TMPDIR_RUN/v2.path"
}

# ─────────────────────────────── 5. élagage ──────────────────────────────────
# Ne garde jamais moins de 2 : la version active + la précédente pour rollback.
# Ne touche jamais aux copies V1.
prune_v2() {
  local active
  active=$(basename "$(cat "$TMPDIR_RUN/v2.path")")
  local names=() dirs=() v
  for d in "$VERSIONS_DIR/$V2_PREFIX".*; do
    [ -d "$d" ] || continue
    names+=("$(basename "$d")")
    dirs+=("$d")
  done
  [ "${#names[@]}" -gt "$KEEP_VERSIONS" ] || { dim "rien à élaguer (${#names[@]} version(s))"; return 0; }

  local sorted
  mapfile -t sorted < <(printf '%s\n' "${names[@]}" | sed "s/^$V2_PREFIX\.//" | sort -V)

  local keep_start=$(( ${#sorted[@]} - KEEP_VERSIONS + 1 ))
  [ "$keep_start" -lt 1 ] && keep_start=1

  for i in "${!sorted[@]}"; do
    local name="$V2_PREFIX.${sorted[$i]}"
    if [ "$i" -ge "$keep_start" ] || [ "$name" = "$active" ]; then
      continue
    fi
    if [ "$MODE" = "install" ]; then
      rm -rf "$VERSIONS_DIR/$name"
      rm -f  "$VERSIONS_DIR/$name.sha256"
      info "élagué $name"
    else
      info "[dry-run] élaguerait $name"
    fi
  done
}

# ─────────────────────────────── 6. câblage ──────────────────────────────────
link_binaries() {
  local v1 v2
  v1=$(cat "$TMPDIR_RUN/v1.path")
  v2=$(cat "$TMPDIR_RUN/v2.path")

  # opencode -> V1 figée. Le cron et opencode-run-watchdog.sh continuent de voir
  # exactement la même chose. Si la V1 est un jour mise à jour par son script
  # historique, son `mv` remplace ce symlink sans toucher la copie figée.
  if [ "$MODE" = "install" ]; then
    ln -sfn "$v1" "$BIN_DIR/opencode"
    mkdir -p "$V2_DATA" "$V2_STATE" "$V2_CACHE"
  else
    info "[dry-run] opencode -> $v1"
  fi

  # opencode2 -> wrapper (PAS un symlink : il faut exporter les XDG avant exec).
  # Ce n'est volontairement pas le shim de l'installeur officiel, qui ferait
  # exec opencode et donc retomberait sur V1.
  local wrapper="$BIN_DIR/opencode2"
  if [ "$MODE" = "install" ]; then
    cat > "$wrapper" <<EOF
#!/bin/sh
# opencode2 — OpenCode V2, données isolées de V1.
# Généré par install-opencode-v2.sh — ne pas éditer à la main.
V2_BIN="$v2/opencode"
XDG_DATA_HOME="$V2_DATA"
XDG_STATE_HOME="$V2_STATE"
XDG_CACHE_HOME="$V2_CACHE"
export XDG_DATA_HOME XDG_STATE_HOME XDG_CACHE_HOME
exec "\$V2_BIN" "\$@"
EOF
    chmod 755 "$wrapper"
  else
    info "[dry-run] écrirait le wrapper $wrapper (XDG_DATA_HOME=$V2_DATA)"
  fi
  dim "opencode  -> $v1"
  dim "opencode2 -> $v2/opencode (XDG_DATA_HOME=$V2_DATA)"
}

write_env_file() {
  local v1 v2 version
  v1=$(cat "$TMPDIR_RUN/v1.path")
  v2=$(cat "$TMPDIR_RUN/v2.path")
  version=$(normalize_version "$("$v2/opencode" --version 2>/dev/null || echo '')")

  if [ "$MODE" = "install" ]; then
    cat > "$ENV_FILE" <<EOF
# Généré par install-opencode-v2.sh — à sourcer, pas à exécuter.
#   . "$ENV_FILE"
# Alimente scripts/smoke-dual-runtime.mjs --strict, qui exige deux binaires
# distincts ET les deux versions attendues.
export OPENCODE_V1_BIN="$BIN_DIR/opencode"
export OPENCODE_V2_BIN="$BIN_DIR/opencode2"
export V1_EXPECTED_VERSION="$(normalize_version "$("$v1" --version 2>/dev/null || echo '')")"
export V2_EXPECTED_VERSION="$version"
EOF
    dim "env écrit : $ENV_FILE"
  fi
}

# ─────────────────────────────── 7. vérifications ────────────────────────────
verify() {
  local failures=0

  local v1bin="$BIN_DIR/opencode" v2bin="$BIN_DIR/opencode2"
  [ -x "$v1bin" ] || { warn "opencode introuvable ou non exécutable"; failures=$((failures+1)); }
  [ -x "$v2bin" ] || { warn "opencode2 introuvable ou non exécutable"; failures=$((failures+1)); }

  local v1ver v2ver v1real v2real
  v1ver=$("$v1bin" --version 2>/dev/null | tr -d '\r' | awk '{print $NF}')
  v2ver=$("$v2bin" --version 2>/dev/null | tr -d '\r' | awk '{print $NF}')
  v1real=$(readlink -f "$v1bin" 2>/dev/null || echo '?')
  v2real=$(readlink -f "$v2bin" 2>/dev/null || echo '?')

  if [ "${v1ver#v}" = "${v2ver#v}" ]; then
    warn "les deux binaires rapportent la même version ($v1ver) — la coexistence est factice"
    failures=$((failures+1))
  else
    ok "deux versions distinctes : V1=$v1ver  V2=$v2ver"
  fi

  if [ -f "$v2bin" ] && ! grep -q 'XDG_DATA_HOME' "$v2bin" 2>/dev/null; then
    warn "opencode2 n'exporte pas XDG_DATA_HOME — c'est le shim de l'installeur officiel, il partagerait les données avec V1"
    failures=$((failures+1))
  else
    ok "opencode2 isole XDG_DATA_HOME / XDG_STATE_HOME / XDG_CACHE_HOME"
  fi

  # Preuve que la V2 n'a pas touché le magasin de données de V1.
  local db="$HOME/.local/share/opencode/opencode.db"
  if [ -f "$db" ]; then
    local before after
    before=$(file_size_mtime "$db")
    "$v2bin" --version >/dev/null 2>&1 || true
    after=$(file_size_mtime "$db")
    if [ "$before" = "$after" ]; then
      ok "opencode.db V1 intact (taille+mtime $after)"
    else
      warn "opencode.db V1 modifiée par l'appel V2 : $before -> $after"
      failures=$((failures+1))
    fi
  else
    dim "opencode.db absent, contrôle ignoré"
  fi

  dim "V1 réel : $v1real"
  dim "V2 réel : $v2real"
  return $failures
}

# ─────────────────────────────── programme ───────────────────────────────────
main() {
  TMPDIR_RUN=$(mktemp -d "${TMPDIR:-/tmp}/opencode-dual.XXXXXX")
  mkdir -p "$BIN_DIR" "$VERSIONS_DIR"

  case "$MODE" in
    verify)
      local target; target=$(detect_target)
      step "État courant"
      info "plateforme      : $target"
      info "répertoire     : $VERSIONS_DIR"
      for d in "$VERSIONS_DIR"/*; do
        [ -d "$d" ] || continue
        info "  $(basename "$d")  ($(du -sh "$d" 2>/dev/null | cut -f1))"
      done
      verify || die "vérifications en échec"
      ok "état conforme"
      return 0
      ;;
    dry-run)
      step "PLAN (dry-run, aucune écriture)"
      ;;
    install)
      step "Installation V2 en cohabitation avec V1"
      ;;
  esac

  local target v2ver
  target=$(detect_target)
  v2ver=$(resolve_v2_version)
  info "plateforme : $target"
  info "V2 cible   : $v2ver"

  step "V1"
  freeze_v1

  step "V2"
  install_v2 "$v2ver"

  step "Élagage"
  prune_v2

  step "Câblage"
  link_binaries

  if [ "$MODE" = "install" ]; then
    write_env_file
  fi

  step "Vérifications"
  if [ "$MODE" = "dry-run" ]; then
    dim "dry-run : les vérifications ne peuvent pas passer avant installation, rapport informatif seulement"
    verify || dim "l'état ne deviendra conforme qu'après une exécution sans --dry-run"
    return 0
  fi
  verify || die "vérifications en échec"
  ok "coexistence en place"

  if [ "$MODE" = "install" ]; then
    step "Suite"
    info ". \"$ENV_FILE\""
    info "node scripts/smoke-dual-runtime.mjs --strict"
  fi
}

main "$@"
