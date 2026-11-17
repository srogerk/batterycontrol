#!/usr/bin/with-contenv bashio
# shellcheck shell=bash
#
# Startskript för BatteryControl-add-onen.
#
# Läser options ur /data/options.json (skrivs av Supervisor) och exponerar dem
# som BC_*-miljövariabler. Se kommentaren vid OPTIONS nedan: run_loop.py läser
# dem ännu INTE — konstanterna är hårdkodade i src/optimizer_v2.py. Startar:
#   1. ingress-servern på 8099 (dashboarden i sidopanelen)
#   2. kvartsloopen internt — ingen cron behövs, det är en av poängerna
#
set -e

echo "[batterycontrol] startar $(date '+%Y-%m-%d %H:%M:%S %Z')"

OPTIONS=/data/options.json

if [ -f "$OPTIONS" ]; then
    echo "[batterycontrol] läser options"
    # OBS: run_loop.py läser INTE BC_*-variablerna. Optimeringens konstanter
    # (battery_capacity_kwh, soc_min/max_pct, max_grid_hour_kw, wear, ...) är
    # hårdkodade i src/optimizer_v2.py. Variablerna exporteras ändå så att de
    # finns tillgängliga den dag konstanterna flyttas hit — men att ändra ett
    # värde i add-onens konfigurationsdialog påverkar INTE styrningen ännu.
    # Enda undantaget är BC_LOOP_INTERVAL_SECONDS, som används längre ner här.
    export BATTERYCONTROL_CONFIG="$(cat "$OPTIONS")"
    for key in battery_capacity_kwh soc_min_pct soc_max_pct battery_max_kw \
               max_grid_hour_kw tariff_rate_kr_per_kw tariff_floor_kw \
               wear_kr_per_kwh ev_battery_kwh dry_run loop_interval_seconds; do
        value=$(bashio::config "$key" 2>/dev/null || true)
        if [ -n "$value" ] && [ "$value" != "null" ]; then
            export "BC_$(echo "$key" | tr '[:lower:]' '[:upper:]')=$value"
        fi
    done
else
    echo "[batterycontrol] /data/options.json saknas — använder standardvärden"
fi

# Dialogen med Supervisor kräver token; den finns redan som SUPERVISOR_TOKEN.
if [ -z "$SUPERVISOR_TOKEN" ]; then
    echo "[batterycontrol] VARNING: SUPERVISOR_TOKEN saknas"
fi

export BATTERYCONTROL_DATA_DIR=/data
export BATTERYCONTROL_CONFIG_DIR=/config
export BATTERYCONTROL_HA_BACKEND=supervisor
export PYTHONPATH=/app

INTERVAL="${BC_LOOP_INTERVAL_SECONDS:-900}"

# --- ingress-server (dashboard) i bakgrunden
python3 /app/ingress_server.py &
INGRESS_PID=$!
echo "[batterycontrol] ingress-server pid=$INGRESS_PID på port 8099"

_term() {
    echo "[batterycontrol] tar emot signal, stänger av"
    kill "$INGRESS_PID" 2>/dev/null || true
    exit 0
}
trap _term TERM INT

# --- kvartsloopen
while true; do
    echo "[batterycontrol] --- kor kvartsoptimering ---"
    # Spara exitkoden i en variabel FORE echo: "$?" avlases annars efter echo,
    # vilket alltid ger 0 och doljer att optimeringen faktiskt misslyckades.
    if python3 /app/src/run_loop.py; then
        echo "[batterycontrol] optimering klar"
    else
        rc=$?
        echo "[batterycontrol] FEL: optimering misslyckades (exit $rc)"
    fi
    sleep "$INTERVAL"
done
