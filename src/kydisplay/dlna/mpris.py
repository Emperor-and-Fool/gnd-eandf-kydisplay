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

"""What a browser is playing, read from its MPRIS2 session interface.

A browser that supports the W3C Media Session API — every major video site
asks for it — publishes an ``org.mpris.MediaPlayer2.*`` name on the session
bus with the playing page's URL under ``xesam:url``. Plasma's own browser
integration extension publishes the same thing again under its own name,
``org.mpris.MediaPlayer2.plasma-browser-integration``, for whichever tab is
playing; either is read the same way here, and nothing here prefers one over
the other. A player with no URL, such as a KDE Connect session relaying a
phone's media controls, is not a candidate and not an error: it has nothing
that could be cast.
"""

from __future__ import annotations

from dataclasses import dataclass

MPRIS_PREFIX = "org.mpris.MediaPlayer2."
PLAYER_INTERFACE = "org.mpris.MediaPlayer2.Player"
PROPERTIES = "org.freedesktop.DBus.Properties"


@dataclass(frozen=True)
class PlayingSource:
    bus_name: str
    url: str
    title: str


def _player_names(bus) -> list[str]:
    import dbus

    proxy = bus.get_object("org.freedesktop.DBus", "/org/freedesktop/DBus")
    names = dbus.Interface(proxy, "org.freedesktop.DBus").ListNames()
    return [str(name) for name in names if name.startswith(MPRIS_PREFIX)]


def _playback_state(bus, name: str):
    import dbus

    properties = dbus.Interface(bus.get_object(name, "/org/mpris/MediaPlayer2"), PROPERTIES)
    status = str(properties.Get(PLAYER_INTERFACE, "PlaybackStatus"))
    metadata = properties.Get(PLAYER_INTERFACE, "Metadata")
    return status, metadata


class MprisWatcher:
    """Reads every media player on the session bus for one playing a URL."""

    def __init__(self, bus=None):
        if bus is None:
            import dbus

            bus = dbus.SessionBus()
        self.bus = bus

    def current_source(self) -> PlayingSource | None:
        """The page a browser is playing right now, or ``None``.

        Only a player reporting ``Playing`` counts: one left paused has
        nothing a cast started from it would show anybody watching. The
        first match wins where more than one qualifies, which in the
        ordinary case of one browser playing one tab is the only one there
        is."""
        for name in _player_names(self.bus):
            try:
                status, metadata = _playback_state(self.bus, name)
            except Exception:
                continue
            url = str(metadata.get("xesam:url") or "")
            if status != "Playing" or not url:
                continue
            title = str(metadata.get("xesam:title") or "")
            return PlayingSource(bus_name=name, url=url, title=title)
        return None


class MprisListener:
    """Notices a newly-started playable source, without re-announcing the one
    already seen.

    Polling rather than subscribing to D-Bus signals, on purpose: every other
    live-state read in this project — the stream helper's state, the Wi-Fi
    P2P device, now this — is read by being asked on a timer rather than by
    subscribing, so nothing here ever needs a second main loop competing with
    Qt's for the same D-Bus connection.

    Call :meth:`poll` on a timer. It is silent as long as nothing has
    changed, including while the same source keeps playing, and speaks again
    once a different source starts — stopping and restarting the same one
    counts as a change, since that is a new thing to offer casting, not a
    continuation of the old offer."""

    def __init__(self, watcher=None):
        self._watcher = watcher if watcher is not None else MprisWatcher()
        self._last_url = None

    def poll(self) -> PlayingSource | None:
        """The source that just started, or ``None`` if nothing is new."""
        source = self._watcher.current_source()
        url = source.url if source else None
        if url == self._last_url:
            return None
        self._last_url = url
        return source
