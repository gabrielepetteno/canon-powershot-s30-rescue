#!/bin/sh
# Double-clickable wrapper for macOS Finder.
#
# Finder runs a .command file in Terminal with the working directory set to
# $HOME, not to the file's own folder, so this must resolve its own location
# before it can find run.sh. Kept separate from run.sh so that file stays a
# plain, terminal-friendly script on Linux too.
DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
exec "$DIR/run.sh" "$@"
