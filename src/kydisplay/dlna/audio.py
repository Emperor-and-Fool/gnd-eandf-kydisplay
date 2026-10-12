# Kydisplay — share a Plasma desktop on a wireless display
# Copyright (C) 2026 Kydisplay contributors
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option)
# any later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for
# more details.
#
# You should have received a copy of the GNU General Public License along
# with this program. If not, see <https://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Mute the local browser's audio while its video is cast, so the same
sound is not heard twice — once from the TV, once from this machine's
own speakers.

MPRIS has no mute of its own to use for this. PipeWire/PulseAudio does,
one layer down: every app producing sound is a "sink input" there,
mutable individually without touching the app itself — the browser
keeps playing (so nothing suspends the tab), only the local speaker
output stops. Confirmed directly: ``pactl set-sink-input-mute <index>
1`` flips exactly that index's ``Mute`` field, nothing else's.

This mutes whichever sink inputs look like a browser process, not a
specific tab — a second tab playing something else in the same browser
goes silent too. For a single cast-while-browsing session that is the
right trade-off; nothing here tries to be more selective than the
browser itself allows from outside.
"""

from __future__ import annotations

import json
import logging
import subprocess

logger = logging.getLogger(__name__)

BROWSER_BINARIES = {
    "firefox", "firefox-bin",
    "chromium", "chromium-browser",
    "brave", "brave-browser",
    "google-chrome", "google-chrome-stable",
}


def _list_sink_inputs() -> list[dict]:
    try:
        result = subprocess.run(
            ["pactl", "-f", "json", "list", "sink-inputs"],
            capture_output=True, text=True, timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return []


def _set_mute(index: int, mute: bool) -> bool:
    try:
        result = subprocess.run(
            ["pactl", "set-sink-input-mute", str(index), "1" if mute else "0"],
            capture_output=True, timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def mute_browser_audio() -> list[int]:
    """Mute every currently-unmuted sink input that looks like a
    browser. Returns exactly the indices this call muted, not every
    browser sink input that exists — one the user had already muted
    themselves is left alone, and is not in the returned list either,
    so a later :func:`unmute` of that list cannot un-mute it for them."""
    muted = []
    for entry in _list_sink_inputs():
        binary = entry.get("properties", {}).get("application.process.binary", "")
        if binary not in BROWSER_BINARIES or entry.get("mute", False):
            continue
        index = entry.get("index")
        if index is None:
            continue
        if _set_mute(index, True):
            muted.append(index)
            logger.info("audio: muted sink input %s (%s)", index, binary)
    return muted


def unmute(indices: list[int]) -> None:
    for index in indices:
        _set_mute(index, False)
        logger.info("audio: unmuted sink input %s", index)