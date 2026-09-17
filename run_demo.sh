#!/bin/sh
# Starts a hub and the simulator so you can try SARSense without hardware.
cd "$(dirname "$0")"
[ -x .venv/bin/python ] || ./install.sh --no-menu || exit 1
PY=.venv/bin/python
$PY -m sarsense --pin 1234 --data-dir "${TMPDIR:-/tmp}/sarsense-demo" &
HUB=$!
trap 'kill $HUB 2>/dev/null' EXIT INT TERM
sleep 2
echo "Open http://localhost:8080/  (operator PIN 1234)"
$PY tools/simulate.py --setup --pin 1234
if [ -n "${SARSENSE_HOLD:-}" ]; then
    printf '\nThe demo has stopped. Press Enter to close this window. '
    read -r _ || true
fi
