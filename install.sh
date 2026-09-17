#!/bin/sh
# SARSense: set up a private Python environment in ./.venv
#
#   ./install.sh                  normal install
#   ./install.sh --recreate       delete .venv and start again
#   ./install.sh --test           also run the test suite afterwards
#   ./install.sh --no-menu        skip the app menu entries
#   ./install.sh --remove-menu    remove the app menu entries and stop
#
# Works on Devuan, Debian, Armbian, Raspberry Pi OS, Ubuntu, Fedora, Arch and
# macOS. POSIX sh only, no root needed (unless the system is missing
# python3-venv, in which case it tells you what to install).
set -eu

usage() {
    cat <<'EOF'
Usage: ./install.sh [options]

  --python PATH        use this Python interpreter (3.9 or newer)
  --system-numpy       reuse the system NumPy (for example from apt)
  --no-system-numpy    always install NumPy inside .venv
  --recreate           delete the existing .venv first
  --test               run the test suite when done
  --no-menu            do not add app menu entries
  --remove-menu        remove the app menu entries and stop
  -h, --help           show this help

By default the system NumPy is reused if one is already installed. That
avoids a slow source build on 32-bit ARM boards such as the NanoPi NEO Air.
If a ./wheels folder exists, packages are installed from it without
touching the internet (see README, "Offline install").
EOF
}

cd "$(dirname "$0")"
HERE=$(pwd)
VENV="$HERE/.venv"
PY=""
SYSTEM_NUMPY=auto
RECREATE=0
RUN_TESTS=0
MENU=1
REMOVE_MENU=0

while [ $# -gt 0 ]; do
    case "$1" in
        --python) [ $# -ge 2 ] || { echo "--python needs a path"; exit 2; }; PY=$2; shift 2 ;;
        --system-numpy) SYSTEM_NUMPY=yes; shift ;;
        --no-system-numpy) SYSTEM_NUMPY=no; shift ;;
        --recreate) RECREATE=1; shift ;;
        --test) RUN_TESTS=1; shift ;;
        --no-menu) MENU=0; shift ;;
        --remove-menu) REMOVE_MENU=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $1"; echo; usage; exit 2 ;;
    esac
done

say()  { printf '%s\n' "$*"; }
fail() { printf '\nInstall stopped: %s\n' "$*" >&2; exit 1; }

version_ok() {
    "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null
}

# --------------------------------------------------------- app menu entries
# Freedesktop .desktop files: GNOME, KDE, XFCE, LXQt, MATE, Cinnamon and most
# other Linux desktops read them. Per-user, or system-wide when run as root.
if [ "$(id -u)" = 0 ]; then
    APPDIR=/usr/local/share/applications
else
    APPDIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
fi
MENU_FILES="sarsense-hub.desktop sarsense-demo.desktop sarsense-open.desktop"

# Quote a path for an Exec= line. The desktop entry spec wants ", `, $ and \
# escaped with a backslash inside the quotes, and then every backslash
# doubled again because the value is also a string; % becomes %%.
exec_quote() {
    q=$(printf '%s' "$1" | sed -e 's/\\/\\\\\\\\/g' -e 's/"/\\\\"/g' -e 's/`/\\\\`/g' -e 's/\$/\\\\$/g' -e 's/%/%%/g')
    printf '"%s"' "$q"
}
# Escape a plain string value (Path=, Icon=)
str_escape() { printf '%s' "$1" | sed -e 's/\\/\\\\/g'; }

update_menu_cache() {
    if command -v update-desktop-database >/dev/null 2>&1; then
        update-desktop-database "$APPDIR" >/dev/null 2>&1 || true
    fi
}

remove_menu() {
    for f in $MENU_FILES; do rm -f "$APPDIR/$f"; done
    update_menu_cache
}

write_entry() {  # file, name, comment, exec, terminal
    {
        printf '[Desktop Entry]\n'
        printf 'Type=Application\n'
        printf 'Version=1.0\n'
        printf 'Name=%s\n' "$2"
        printf 'GenericName=Search and rescue Wi-Fi sensing\n'
        printf 'Comment=%s\n' "$3"
        printf 'Exec=%s\n' "$4"
        printf 'Path=%s\n' "$(str_escape "$HERE")"
        printf 'Icon=%s\n' "$(str_escape "$HERE/sarsense/static/icon-512.png")"
        printf 'Terminal=%s\n' "$5"
        printf 'Categories=Network;\n'
        printf 'Keywords=search;rescue;wifi;sensing;csi;\n'
        printf 'StartupNotify=false\n'
    } > "$APPDIR/$1"
    chmod 644 "$APPDIR/$1"
}

install_menu() {
    case "$(uname -s)" in
        Linux|*BSD) ;;
        *) say "App menu entries are only made on Linux and BSD desktops. Start SARSense with ./run_hub.sh."
           return 0 ;;
    esac
    if ! mkdir -p "$APPDIR" 2>/dev/null; then
        say "Could not create $APPDIR, skipping app menu entries."
        return 0
    fi
    write_entry sarsense-hub.desktop "SARSense Hub" "Start the SARSense search and rescue hub" \
        "env SARSENSE_HOLD=1 $(exec_quote "$HERE/run_hub.sh")" true
    write_entry sarsense-demo.desktop "SARSense Demo" "Try SARSense with simulated sensors" \
        "env SARSENSE_HOLD=1 $(exec_quote "$HERE/run_demo.sh")" true
    write_entry sarsense-open.desktop "SARSense Web App" "Open the SARSense map in a browser (the hub must be running)" \
        "xdg-open http://localhost:8080/" false
    update_menu_cache
    say "App menu entries added to $APPDIR (look under Internet or Network)."
}

if [ "$REMOVE_MENU" = 1 ]; then
    remove_menu
    say "Removed the SARSense app menu entries from $APPDIR. The SARSense folder is untouched."
    exit 0
fi

# ------------------------------------------------------------ find Python
if [ -n "$PY" ]; then
    command -v "$PY" >/dev/null 2>&1 || fail "$PY was not found."
    version_ok "$PY" || fail "$PY is older than Python 3.9."
else
    for c in python3.13 python3.12 python3.11 python3.10 python3.9 python3 python; do
        if command -v "$c" >/dev/null 2>&1 && version_ok "$c"; then
            PY=$(command -v "$c")
            break
        fi
    done
    [ -n "$PY" ] || fail "Python 3.9 or newer is needed. On Debian or Devuan: sudo apt install python3 python3-venv"
fi
PYVER=$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
say "Using Python $PYVER at $PY"

if ! "$PY" -c 'import venv, ensurepip' >/dev/null 2>&1; then
    fail "this Python cannot create virtual environments.
On Debian, Devuan, Armbian or Ubuntu run:
    sudo apt install python3-venv
(if that is not enough, also: sudo apt install python$PYVER-venv)
then run ./install.sh again."
fi

# ------------------------------------------------------- NumPy strategy
ARCH=$(uname -m 2>/dev/null || echo unknown)
HAVE_SYS_NUMPY=0
"$PY" -c 'import numpy' >/dev/null 2>&1 && HAVE_SYS_NUMPY=1

if [ "$SYSTEM_NUMPY" = auto ]; then
    if [ "$HAVE_SYS_NUMPY" = 1 ]; then SYSTEM_NUMPY=yes; else SYSTEM_NUMPY=no; fi
fi
if [ "$SYSTEM_NUMPY" = yes ] && [ "$HAVE_SYS_NUMPY" = 0 ]; then
    fail "--system-numpy was given but $PY cannot import numpy. Install it first, for example: sudo apt install python3-numpy"
fi
case "$ARCH" in
    armv6*|armv7*|armhf)
        if [ "$SYSTEM_NUMPY" = no ] && [ ! -d "$HERE/wheels" ]; then
            say ""
            say "Note: this is a 32-bit ARM board ($ARCH). PyPI has no ready-made NumPy for it,"
            say "so pip may spend a long time compiling. The quicker route is:"
            say "    sudo apt install python3-numpy && ./install.sh --recreate"
            say ""
        fi
        ;;
esac

# ------------------------------------------------------------ create venv
if [ "$RECREATE" = 1 ] && [ -d "$VENV" ]; then
    say "Removing old .venv"
    rm -rf "$VENV"
fi

VPY="$VENV/bin/python"
if [ -x "$VPY" ] && ! "$VPY" -c 'import sys' >/dev/null 2>&1; then
    say "The existing .venv is broken (was the folder moved?). Recreating it."
    rm -rf "$VENV"
fi

if [ ! -x "$VPY" ]; then
    if [ "$SYSTEM_NUMPY" = yes ]; then
        say "Creating .venv (sharing the system NumPy)"
        "$PY" -m venv --system-site-packages "$VENV"
    else
        say "Creating .venv"
        "$PY" -m venv "$VENV"
    fi
else
    say "Reusing existing .venv"
fi

# ------------------------------------------------------- install packages
if [ -d "$HERE/wheels" ]; then
    say "Installing from ./wheels (offline)"
    "$VPY" -m pip install --no-index --find-links "$HERE/wheels" -r "$HERE/requirements.txt" \
        || fail "offline install failed. Check that ./wheels matches this Python ($PYVER) and machine ($ARCH)."
else
    say "Updating pip"
    "$VPY" -m pip install --quiet --upgrade pip >/dev/null 2>&1 \
        || say "  (could not update pip, carrying on with the bundled one)"
    say "Installing requirements"
    if ! "$VPY" -m pip install -r "$HERE/requirements.txt"; then
        case "$ARCH" in
            armv6*|armv7*|armhf|aarch64|arm64)
                fail "NumPy could not be installed. Use the system package instead:
    sudo apt install python3-numpy
    ./install.sh --recreate --system-numpy" ;;
            *)
                fail "pip could not install the requirements. Check the internet connection and try again." ;;
        esac
    fi
fi

# ------------------------------------------------------------------ check
"$VPY" -c 'import numpy, sarsense; print("SARSense %s ready, NumPy %s" % (sarsense.__version__, numpy.__version__))' \
    || fail "the environment was created but SARSense does not import. Run ./install.sh --recreate"

chmod +x "$HERE/run_hub.sh" "$HERE/run_demo.sh" 2>/dev/null || true

if [ "$MENU" = 1 ]; then
    install_menu
fi

if [ "$RUN_TESTS" = 1 ]; then
    say ""
    say "Running tests (about 20 seconds)"
    "$VPY" -m unittest discover -s "$HERE/tests" || fail "some tests failed, see above."
fi

say ""
say "Done. Next:"
say "  SARSense Demo in the app menu, or ./run_demo.sh    try it with simulated sensors (PIN 1234)"
say "  SARSense Hub in the app menu, or ./run_hub.sh      run a real hub"
say "The web app is then at http://localhost:8080 and on this machine's network address."
