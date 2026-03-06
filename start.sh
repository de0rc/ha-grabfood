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

# Start Xvfb directly as background process
echo "[grab-login] Starting Xvfb on :99..."
Xvfb :99 -screen 0 1280x800x24 -ac &
XVFB_PID=$!
sleep 1

export DISPLAY=:99
echo "[grab-login] Xvfb started (PID $XVFB_PID)"

# Start x11vnc as background process
echo "[grab-login] Starting x11vnc..."
x11vnc -display :99 -nopw -listen 127.0.0.1 -rfbport 5900 -forever -shared -quiet &
sleep 1
echo "[grab-login] x11vnc started"

exec python3 /app/main.py