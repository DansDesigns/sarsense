#!/bin/sh
# Start a SARSense hub using the local .venv (created by ./install.sh).
# Any options are passed to the hub, for example:
#   ./run_hub.sh --pin 4821 --hub-id A
#   ./run_hub.sh --config deploy/sarsense.json
set -eu
cd "$(dirname "$0")"
[ -x .venv/bin/python ] || ./install.sh --no-menu

case " $* " in
    *" --pin "*|*" --pin="*|*" --config "*|*" --help "*|*" -h "*) ;;
    *)
        if [ -z "${SARSENSE_PIN:-}" ] && [ -t 0 ]; then
            printf 'Choose an operator PIN (blank for none): '
            stty -echo 2>/dev/null || true
            read -r SARSENSE_PIN || SARSENSE_PIN=""
            stty echo 2>/dev/null || true
            printf '\n'
            export SARSENSE_PIN
        fi
        ;;
esac

DATA="${SARSENSE_DATA:-$(pwd)/sarsense-data}"
if [ -z "${SARSENSE_HOLD:-}" ]; then
    exec .venv/bin/python -m sarsense --data-dir "$DATA" "$@"
fi
# started from the app menu: keep the window open so messages can be read
set +e
trap : INT    # Ctrl+C stops the hub but not this script, so the prompt below still shows
.venv/bin/python -m sarsense --data-dir "$DATA" "$@"
printf '\nThe hub has stopped. Press Enter to close this window. '
read -r _ || true
