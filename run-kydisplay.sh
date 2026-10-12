#!/usr/bin/env bash
# Run Kydisplay from its own Python, not the system one.
#
# The interpreter lives in Pysolated beside every other isolated
# environment on this machine, and is built with --system-site-packages
# because Kydisplay imports PyQt6, gi (PyGObject, GUPnPAV) and dbus,
# which come from apt and are impractical to pip-install. Only the
# dependencies that are genuinely ours live inside it: yt-dlp and
# curl_cffi, installed as "yt-dlp[default,curl-cffi]", which is the
# combination yt-dlp's own project recommends and the one that gives
# impersonation for the sites that need it.
#
# PYTHONNOUSERSITE matters more than it looks. A venv built with
# --system-site-packages still has ~/.local on its path, ahead of the
# system but behind itself, so a stale copy of yt-dlp or curl_cffi
# sitting there can be picked up when the venv's own copy is removed or
# fails to import. Switching user site off makes "which copy am I
# running" answerable without inspecting anything.
set -euo pipefail

VENV="${KYDISPLAY_VENV:-/home/skdj/Pysolated/penv_kydisplay}"
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if [[ ! -x "$VENV/bin/python" ]]; then
    echo "No interpreter at $VENV — see docs, or set KYDISPLAY_VENV." >&2
    exit 1
fi

export PYTHONNOUSERSITE=1
export PYTHONPATH="$HERE/src${PYTHONPATH:+:$PYTHONPATH}"

exec "$VENV/bin/python" -m kydisplay.app "$@"
