#!/usr/bin/with-contenv bashio
set -e

# Read options from HA add-on config and export as env vars
OPTIONS_FILE="/data/options.json"
if [ -f "$OPTIONS_FILE" ]; then
    SHOW_DRIVER_MAP=$(python3 -c "import json; d=json.load(open('$OPTIONS_FILE')); print(str(d.get('show_driver_map', False)).lower())")
    export SHOW_DRIVER_MAP
    echo "[grab-login] show_driver_map=$SHOW_DRIVER_MAP"
    LOG_LEVEL=$(python3 -c "import json; d=json.load(open('$OPTIONS_FILE')); print(d.get('log_level', 'info').lower())")
    export LOG_LEVEL
    echo "[grab-login] log_level=$LOG_LEVEL"
else
    export SHOW_DRIVER_MAP=false
    export LOG_LEVEL=info
fi

# Xvfb and x11vnc are started on demand by browser.py when login is triggered.
export DISPLAY=:99

exec python3 /app/main.py