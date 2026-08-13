#!/bin/sh
# RetroCam Rescue launcher (macOS / Linux).
#
# Finds a Python 3.9+ that can actually import tkinter, and explains clearly
# what to install when it cannot. Written in POSIX sh on purpose: macOS still
# ships bash 3.2, so anything bash-4-only silently breaks on the exact machines
# this tool exists for.
#
# Override the interpreter with:  RETROCAM_PYTHON=/path/to/python3 ./run.sh
set -eu

APP_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)

# Distinct exit codes let us tell three different situations apart:
#   10 = Python too old, 11 = no tkinter at all, 12 = tkinter but Tk 8.5.
# Apple's system Python has Tk 8.5, which starts but misdraws widgets; it is a
# usable last resort, not a first choice, so it must not shadow a good Python.
PROBE='
import sys
if sys.version_info < (3, 9):
    raise SystemExit(10)
try:
    import tkinter
except Exception:
    raise SystemExit(11)
raise SystemExit(0 if float(tkinter.TkVersion) >= 8.6 else 12)
'

BEST=""      # Python with a healthy Tk >= 8.6
FALLBACK=""  # Python that only has Tk 8.5

try_python() {
    [ -n "$BEST" ] && return 0
    # Accept either a name on PATH or an absolute path to a real binary.
    command -v "$1" >/dev/null 2>&1 || [ -x "$1" ] || return 0
    "$1" -c "$PROBE" >/dev/null 2>&1
    case $? in
        0)  BEST="$1" ;;
        12) [ -n "$FALLBACK" ] || FALLBACK="$1" ;;
        *)  : ;;   # 10 / 11 / anything else: not usable, keep looking
    esac
    return 0
}

# Order matters: explicit override, then modern interpreters by name, then the
# well-known Homebrew and python.org locations. The absolute paths are not
# redundant — a script started from Finder inherits a four-entry PATH that
# contains no Homebrew prefix at all.
for CAND in \
    "${RETROCAM_PYTHON:-}" \
    python3.14 python3.13 python3.12 python3.11 python3.10 python3.9 python3 \
    /opt/homebrew/bin/python3 /usr/local/bin/python3
do
    [ -n "$CAND" ] && try_python "$CAND"
done
for CAND in /Library/Frameworks/Python.framework/Versions/3.*/bin/python3; do
    [ -x "$CAND" ] && try_python "$CAND"
done

PY="$BEST"
if [ -z "$PY" ] && [ -n "$FALLBACK" ]; then
    PY="$FALLBACK"
    echo "WARNING: this Python only has Tk 8.5 (Apple's old, buggy build)." >&2
    echo "ATTENZIONE: questo Python ha solo Tk 8.5 (versione Apple obsoleta)." >&2
    echo "The app will start but may look or behave oddly. To upgrade:" >&2
    echo "  brew install python-tk@3.13" >&2
    echo "  or install Python from https://www.python.org/downloads/macos/" >&2
    echo >&2
fi

if [ -z "$PY" ]; then
    echo "RetroCam Rescue: no usable Python 3.9+ with Tkinter was found." >&2
    echo "RetroCam Rescue: nessun Python 3.9+ con Tkinter utilizzabile." >&2
    echo >&2
    if [ "$(uname -s)" = "Darwin" ]; then
        echo "macOS - install one of:" >&2
        echo "  brew install python-tk@3.13      # if you already use Homebrew Python" >&2
        echo "  brew install python@3.13 python-tk@3.13" >&2
        echo "  or download the installer from https://www.python.org/downloads/macos/" >&2
        echo "     (the python.org build already bundles Tcl/Tk 8.6)" >&2
        echo >&2
        echo "Note: /usr/bin/python3 (Apple's own) ships Tk 8.5 and is not recommended." >&2
    else
        echo "Linux - install the Tk bindings for your Python, e.g.:" >&2
        echo "  sudo apt install python3-tk        # Debian / Ubuntu" >&2
        echo "  sudo dnf install python3-tkinter   # Fedora" >&2
        echo "  sudo pacman -S tk                  # Arch" >&2
        echo "  sudo zypper install python3-tk     # openSUSE" >&2
    fi
    echo >&2
    echo "Then run this script again, or set RETROCAM_PYTHON=/path/to/python3" >&2
    exit 1
fi

# src layout: run straight from the checkout, no install step, no virtualenv.
PYTHONPATH="$APP_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONPATH

exec "$PY" -m retrocam "$@"
