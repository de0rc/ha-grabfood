#!/bin/bash
set -e

# Read options from HA add-on config and export as env vars
OPTIONS_FILE="/data/options.json"
if [ -f "$OPTIONS_FILE" ]; then
    SHOW_DRIVER_MAP=$(python3 -c "import json; d=json.load(open('$OPTIONS_FILE')); print(str(d.get('show_driver_map', False)).lower())")
    export SHOW_DRIVER_MAP
    echo "[grab-login] show_driver_map=$SHOW_DRIVER_MAP"
else
    export SHOW_DRIVER_MAP=false
fi

echo "[grab-login] Launching app under xvfb-run..."
exec xvfb-run --server-num=99 \
     --server-args="-screen 0 1280x800x24 -ac" \
     bash -c "
        echo '[grab-login] Xvfb started on :99'
        x11vnc -display :99 -nopw -listen 0.0.0.0 -rfbport 5900 -forever -shared -quiet &
        sleep 1
        echo '[grab-login] x11vnc started'
        export DISPLAY=:99
        exec python3 /app/main.py
     "