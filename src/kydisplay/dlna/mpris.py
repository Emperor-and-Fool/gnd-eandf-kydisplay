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
integration extension publishes one too, for whichever tab is actually the
active media session. A player with no URL, such as a KDE Connect session
relaying a phone's media controls, is not a candidate and not an error: it
has nothing that could be cast.

These two are **not** the same thing said twice, which this module used to
assume. Seen directly on one session bus, with four players present:

  firefox.instance_1_227            Playing   a YouTube live stream, 6928s in
  plasma-browser-integration        Playing   the tab actually being watched
  brave.instance50668               Paused
  plasma-browser-integration-52536  Paused

The browser's own MPRIS went on calling a stream "Playing" long after its
owner had moved to a different tab, while the extension named the tab in
front of them. Taking the first name that said Playing therefore cast the
wrong thing — and which one came first was decided by D-Bus name
registration order, which means nothing at all.

So the extension is preferred where it is present, and a browser's own
MPRIS is the fallback for anyone who has not installed it. Note that a
browser sitting idle still publishes an extension instance of its own —
two of the four above — so having one is not evidence of being the browser
playing anything; the Playing-with-a-URL test is what sorts that out, and
it is applied before any preference is considered.
"""

from __future__ import annotations

from dataclasses import dataclass

MPRIS_PREFIX = "org.mpris.MediaPlayer2."
PLAYER_INTERFACE = "org.mpris.MediaPlayer2.Player"
PROPERTIES = "org.freedesktop.DBus.Properties"

# Matched as a substring of the bus name, since each browser publishes its
# own instance and only the first is unsuffixed: "plasma-browser-integration"
# alongside "plasma-browser-integration-52536".
PLASMA_INTEGRATION = "plasma-browser-integration"

# Binary names to the name yt-dlp knows that browser by. The two are not
# always the same word, which is the only reason this mapping exists
# rather than passing the binary through.
BROWSER_BINARIES = {
    "firefox": "firefox",
    "firefox-bin": "firefox",
    "librewolf": "firefox",
    "waterfox": "firefox",
    "brave": "brave",
    "brave-browser": "brave",
    "brave-browser-stable": "brave",
    "chrome": "chrome",
    "google-chrome": "chrome",
    "google-chrome-stable": "chrome",
    "chromium": "chromium",
    "chromium-browser": "chromium",
    "vivaldi": "vivaldi",
    "vivaldi-bin": "vivaldi",
    "opera": "opera",
    "microsoft-edge": "edge",
    "microsoft-edge-stable": "edge",
}


def _connection_pid(bus, name: str) -> int | None:
    import dbus

    try:
        proxy = bus.get_object("org.freedesktop.DBus", "/org/freedesktop/DBus")
        return int(dbus.Interface(proxy, "org.freedesktop.DBus").GetConnectionUnixProcessID(name))
    except Exception:
        return None


def _process_name(pid: int) -> str | None:
    """The binary a process was started from. Read from ``cmdline`` rather
    than ``comm``, which truncates at fifteen characters and would turn
    "brave-browser-stable" into something that matches nothing."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            argv0 = handle.read().split(b"\0")[0].decode(errors="replace")
    except OSError:
        return None
    return argv0.rsplit("/", 1)[-1] or None


def _parent_pid(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/status") as handle:
            for line in handle:
                if line.startswith("PPid:"):
                    return int(line.split()[1])
    except (OSError, ValueError):
        return None
    return None


def browser_for(bus_name: str, bus=None) -> str | None:
    """The name yt-dlp knows the browser behind this player by, or ``None``
    if it could not be worked out.

    The bus name itself does not say. A browser's own MPRIS name does
    ("firefox.instance_1_227"), but Plasma's extension publishes
    "plasma-browser-integration-100665", where the number is the *host
    process's* pid and nothing names the browser at all. What identifies it
    is the process tree: that host is a child of the browser that launched
    it, which on this machine distinguished an apt Brave at
    /opt/brave.com/brave/brave from a Firefox at /usr/bin/firefox, both
    publishing extension instances at once.

    So the owning process is asked of D-Bus and its ancestors walked until
    one of them is recognisably a browser. Walking rather than checking the
    parent alone because a browser may put more than one process between
    itself and the host, and the depth is not something to assume."""
    if bus is None:
        import dbus

        bus = dbus.SessionBus()
    pid = _connection_pid(bus, bus_name)
    seen = 0
    # Bounded so a cycle or a very deep tree cannot spin; nothing
    # legitimate puts a browser more than a few processes up.
    while pid and pid > 1 and seen < 12:
        name = _process_name(pid)
        if name and name in BROWSER_BINARIES:
            return BROWSER_BINARIES[name]
        pid = _parent_pid(pid)
        seen += 1
    return None


@dataclass(frozen=True)
class PlayingSource:
    bus_name: str
    url: str
    title: str


@dataclass(frozen=True)
class _Player:
    """One MPRIS player as it describes itself, playing or not. Kept
    separate from :class:`PlayingSource`, which is only ever something
    worth casting, because deciding what is worth casting sometimes needs
    to look at players that are not."""

    bus_name: str
    status: str
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

    def players(self) -> list[_Player]:
        """Every MPRIS player on the bus, as it describes itself."""
        found = []
        for name in _player_names(self.bus):
            try:
                status, metadata = _playback_state(self.bus, name)
            except Exception:
                continue
            found.append(_Player(
                bus_name=name,
                status=str(status),
                url=str(metadata.get("xesam:url") or ""),
                title=str(metadata.get("xesam:title") or ""),
            ))
        return found

    def playing(self) -> list[PlayingSource]:
        """Every player reporting ``Playing`` with a URL, in bus order.

        Only ``Playing`` counts: one left paused has nothing a cast
        started from it would show anybody watching. Split out from
        :meth:`current_source` so the gathering can be tested apart from
        the choosing, which is where the judgement is."""
        return [
            PlayingSource(bus_name=p.bus_name, url=p.url, title=p.title)
            for p in self.players() if p.status == "Playing" and p.url
        ]

    def _paired_source(self, players: list[_Player]) -> PlayingSource | None:
        """What to cast when no single player both claims to be playing
        and knows what it is playing.

        Seen on Rumble in Brave: the extension held the page URL but
        reported Paused, while Brave's own MPRIS reported Playing and
        published no URL at all. Each had half the answer and neither
        passed the test on its own, so a video plainly playing registered
        as nothing playing.

        They are halves of the same browser, so they are paired by it —
        the process tree already tells us which browser each player
        belongs to. A browser counts as playing if any of its players says
        so, and the URL is then taken from whichever of its players
        actually has one.

        Deliberately the fallback and not the rule: it trusts a URL from a
        player that says it is paused, which is only reasonable once no
        player has made the straightforward claim."""
        playing_browsers = set()
        for player in players:
            if player.status == "Playing":
                browser = browser_for(player.bus_name, bus=self.bus)
                if browser:
                    playing_browsers.add(browser)
        if not playing_browsers:
            return None

        candidates = []
        for player in players:
            if not player.url:
                continue
            if browser_for(player.bus_name, bus=self.bus) in playing_browsers:
                candidates.append(PlayingSource(
                    bus_name=player.bus_name, url=player.url, title=player.title))
        return self.choose(candidates)

    @staticmethod
    def choose(candidates: list[PlayingSource]) -> PlayingSource | None:
        """Which of several playing things to cast.

        Plasma's browser-integration instances win over a browser's own
        MPRIS, for the reason set out at the top of this module: the
        extension follows the tab actually being watched, while a
        browser's built-in name has been seen insisting on a stream its
        owner left behind. Among equals the first still wins, which is
        the honest answer — nothing on the bus says which of two genuinely
        playing tabs a person means, and guessing harder would not make it
        truer."""
        for candidate in candidates:
            if PLASMA_INTEGRATION in candidate.bus_name:
                return candidate
        return candidates[0] if candidates else None

    def current_source(self) -> PlayingSource | None:
        """The page a browser is playing right now, or ``None``.

        The straightforward case first — a player that says it is playing
        and says what — because it is the common one and costs only the
        D-Bus reads already made. Only when nothing makes that claim does
        this fall back to pairing players by browser, which walks process
        trees and is not worth doing on a timer for the usual answer."""
        players = self.players()
        direct = [
            PlayingSource(bus_name=p.bus_name, url=p.url, title=p.title)
            for p in players if p.status == "Playing" and p.url
        ]
        if direct:
            return self.choose(direct)
        return self._paired_source(players)


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
