#!/usr/bin/env bash
# weekly-advisor v5.28 — cron wrapper (P7.1) : chaîne complète depuis project_root.
# Usage: scripts/weekly-advisor-cron.sh [--anchor ISO8601]
# Cron (hebdo, lundi 06:00): 0 6 * * 1 /home/benjamin/Dev/Adeo/weekly-advisor/scripts/weekly-advisor-cron.sh
# rc agrégé : 2 si une étape échoue réellement ; 0 sinon (warnings/partial acceptés, loggés).
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PKG="$ROOT/weekly-advisor"
PY="$PKG/.venv/bin/python"
CFG="$PKG/weekly-telemetry-config.json"
LOG_DIR="$PKG/logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/cron-$(date -u +%Y-%m-%dT%H%M%SZ).log"

# K9: ancre unique pour toute la chaîne — soit fournie, soit générée une fois
# (les étapes re-exécutées avec des ancres différentes mélangeraient les périodes).
ANCHOR_ARGS=()
if [ "${1:-}" = "--anchor" ]; then
    ANCHOR_ARGS=(--anchor "${2:-}")
else
    ANCHOR_ARGS=(--anchor "$(date -u +%Y-%m-%dT%H:%M:%SZ)")
fi

export PYTHONPATH="$PKG"
cd "$ROOT" || exit 2

STEPS=(doctor run harness releases insights report-prep report-blocks-draft report-assemble)
overall=0

{
    echo "== weekly-advisor cron $(date -u +%FT%TZ) (anchor: ${ANCHOR_ARGS[*]:-default now}) =="
    for step in "${STEPS[@]}"; do
        echo "--- $step ---"
        "$PY" -m weekly_telemetry_aggregator "$step" --config "$CFG" "${ANCHOR_ARGS[@]}"
        rc=$?
        echo "[$step] rc=$rc"
        if [ "$rc" -eq 2 ]; then
            overall=2
            echo "[$step] ÉCHEC TOTAL — chaîne interrompue"
            break
        fi
    done
    echo "== fin (overall rc=$overall) =="
} >> "$LOG" 2>&1

# rotation : garder les 12 logs les plus récents
ls -1t "$LOG_DIR"/cron-*.log 2>/dev/null | tail -n +13 | xargs -r rm -f

exit "$overall"
