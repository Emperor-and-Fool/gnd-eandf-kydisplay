# Kydisplay — share a Plasma desktop on a wireless display
# Copyright (C) 2026 Kydisplay contributors
# Copyright (C) 2026 Mirdispo contributors
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

from __future__ import annotations

import logging
import os
from pathlib import Path
import signal
import subprocess
import time

from .models import DisplayDevice

logger = logging.getLogger(__name__)


# The engine is built with this project's bus prefix (patch 0010), so it never
# competes with GNOME Network Displays for a name. The object path and the
# interface are upstream's.
BUS_PREFIX = "nl.eandf.Kydisplay"
SERVICE = BUS_PREFIX + ".Manager"
PATH = "/org/gnome/NetworkDisplays/Manager"
INTERFACE = "org.gnome.NetworkDisplays.Manager"
PROPERTIES = "org.freedesktop.DBus.Properties"

STATE_NAMES = {
    0x0: "available",
    0x50: "configuring firewall",
    0x100: "connecting Wi-Fi Direct",
    0x110: "waiting for receiver",
    0x120: "starting stream",
    0x1000: "streaming",
    0x10000: "error",
}

PROTOCOL_NAMES = {
    0: "Network display",
    1: "Miracast test receiver",
    2: "Chromecast test receiver",
    3: "Miracast (Wi-Fi Direct)",
    4: "Miracast over your network",
    5: "Chromecast",
}

# Which way to reach a receiver that offers more than one.
#
# Casting over the network needs no Wi-Fi Direct group, so there is no channel
# for the receiver to choose badly and no handshake to time out. A session over
# the network starts in about a second, while Wi-Fi Direct commonly takes from
# several seconds to half a minute on the same receiver. Wi-Fi Direct remains
# the fallback for receivers that are not on the network.
PROTOCOL_PREFERENCE = {
    4: 0,   # over the network
    3: 1,   # Wi-Fi Direct
    0: 2,
    5: 3,   # Chromecast, which does not mirror the screen on every receiver
    1: 4,
    2: 5,
}


def protocol_rank(protocol: int) -> int:
    return PROTOCOL_PREFERENCE.get(protocol, 9)


def display_from_dbus(values: dict) -> DisplayDevice:
    uuid = str(values.get("uuid") or "")
    protocol = int(values.get("protocol") or 0)
    state = int(values.get("state") or 0)
    return DisplayDevice(
        path=uuid,
        name=str(values.get("display-name") or "Unnamed display"),
        manufacturer=PROTOCOL_NAMES.get(protocol, "Network display"),
        model=STATE_NAMES.get(state, "available"),
        # The daemon's priority is for protocol de-duplication, not RF signal.
        strength=0,
        wfd_capable=True,
        status=STATE_NAMES.get(state, "available"),
        protocol=protocol,
    )


class GnomeNetworkDisplaysService:
    """Client for the UI-independent GNOME Network Displays 0.99 daemon."""

    def __init__(self, bus=None, daemon_path=None):
        if bus is None:
            import dbus

            bus = dbus.SessionBus()
        self.bus = bus
        self.daemon_path = daemon_path or self._find_daemon()
        self._process = None

    @staticmethod
    def _find_daemon(here=None):
        here = Path(here or __file__).resolve()
        candidates = [
            os.environ.get("KYDISPLAY_DAEMON"),
            # Installed: <prefix>/lib/kydisplay/kydisplay/gnd.py sits beside
            # <prefix>/libexec, whatever the prefix. This covers the .deb,
            # the Flatpak under /app and make install into /usr/local or
            # ~/.local.
            here.parents[3] / "libexec" / "kydisplay-daemon",
            # A checkout, after make backend.
            here.parents[2] / "vendor" / "amd64" / "kydisplay-daemon",
            "/usr/libexec/kydisplay-daemon",
        ]
        return next((str(item) for item in candidates if item and Path(item).is_file()), None)

    def _dbus(self):
        import dbus

        obj = self.bus.get_object(SERVICE, PATH)
        return dbus.Interface(obj, INTERFACE), dbus.Interface(obj, PROPERTIES)

    def running(self) -> bool:
        import dbus

        proxy = self.bus.get_object("org.freedesktop.DBus", "/org/freedesktop/DBus")
        result = bool(dbus.Interface(proxy, "org.freedesktop.DBus").NameHasOwner(SERVICE))
        # Only when it changes. See displays() below for why.
        if result != getattr(self, "_last_running", None):
            logger.info("running: NameHasOwner(%s) -> %s", SERVICE, result)
            self._last_running = result
        return result

    def _owner_pid(self) -> int | None:
        """Which process currently holds the engine's bus name."""
        import dbus

        try:
            proxy = self.bus.get_object("org.freedesktop.DBus", "/org/freedesktop/DBus")
            interface = dbus.Interface(proxy, "org.freedesktop.DBus")
            return int(interface.GetConnectionUnixProcessID(SERVICE))
        except Exception:
            return None

    @staticmethod
    def _is_orphan(pid: int) -> bool:
        """Whether a process has outlived whatever started it. Reparenting
        to init is what being orphaned looks like from outside."""
        try:
            with open(f"/proc/{pid}/status") as handle:
                for line in handle:
                    if line.startswith("PPid:"):
                        return int(line.split()[1]) <= 1
        except (OSError, ValueError):
            return False
        return False

    def _stranded_daemon(self) -> int | None:
        """An engine left over from a previous run that is no use to this
        one, or ``None`` if what is running is worth keeping.

        Both halves have to be true. Orphaned, because a daemon this run
        started is ours and fine. And finding nothing, because an orphan
        that can still see receivers is doing its job — that is the engine
        deliberately left alive so a cast survives the window closing, and
        killing it would end somebody's cast.

        An orphan that sees nothing is the case worth acting on, and it is
        not hypothetical: an application that crashes never runs its
        shutdown, so the engine it started keeps the bus name. The next
        launch then finds the name owned, attaches to that engine rather
        than starting one, and inherits its blindness — so the window comes
        up healthy, lists no receivers, and restarting it changes nothing,
        because the restart attaches to the same dead engine again."""
        pid = self._owner_pid()
        if pid is None or not self._is_orphan(pid):
            return None
        try:
            if self.displays():
                return None
        except Exception:
            # Cannot even be asked, which is its own kind of no use.
            pass
        return pid

    def ensure_running(self):
        if self.running():
            stranded = self._stranded_daemon() if self._process is None else None
            if stranded is None:
                logger.info("ensure_running: already running, daemon not (re)started")
                return
            logger.warning("ensure_running: engine pid=%s is orphaned and sees nothing; "
                           "replacing it", stranded)
            try:
                os.kill(stranded, signal.SIGTERM)
            except OSError:
                logger.exception("ensure_running: could not stop pid=%s", stranded)
                return
            # Give it a moment to drop the bus name, or the start below
            # races it and the new engine finds the name taken.
            for _ in range(20):
                time.sleep(0.1)
                if not self.running():
                    break
            else:
                logger.warning("ensure_running: pid=%s still holds the bus name", stranded)
                return
        if not self.daemon_path:
            logger.error("ensure_running: no daemon_path found, cannot start")
            raise RuntimeError("Network display backend is not installed")
        # The engine's warnings go where this process's go, the journal for an
        # application started from the menu. Discarding them hid why a
        # receiver was missing from the network route.
        logger.info("ensure_running: starting daemon at %s", self.daemon_path)
        self._process = subprocess.Popen(
            [self.daemon_path],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            start_new_session=True,
        )
        logger.info("ensure_running: daemon started, pid=%s", self._process.pid)

    def release(self):
        """Stop the engine if this process started it.

        The engine is started in its own session so that a cast survives the
        window being closed. With nothing being cast it has no reason to
        outlive the window, and inside Flatpak it would keep its sandbox,
        and the version it was started from, alive after an update.
        """
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._process.kill()
        self._process = None

    def displays(self) -> list[DisplayDevice]:
        _, properties = self._dbus()
        raw = properties.Get(INTERFACE, "Displays")
        result = [display_from_dbus(dict(item)) for item in raw]
        # Only when the answer is different from last time. This is
        # polled every two seconds and nearly always says exactly what it
        # said before: 478 lines of one unchanged television in an
        # 838-line session, with the handful of lines that explained a
        # real fault buried somewhere in the middle of them. What is
        # worth recording is a display appearing, going, or changing
        # state, not that it is still there.
        if result != getattr(self, "_last_displays", ...):
            logger.info("displays: raw Displays property -> %s", raw)
            logger.info("displays: parsed -> %s", result)
            self._last_displays = result
        return result

    @staticmethod
    def remembered_source() -> bool:
        """Whether the desktop is holding a choice to hand back.

        The engine writes down what it was given after a cast has started,
        and hands it back on the next one, which is what makes a second cast
        share the same thing without asking. So the question of whether there
        is anything to reuse is the question of whether that is written down.

        It lives where things last a sitting rather than a machine's life, so
        it is gone after a restart, and so is the reuse it stands for.
        """
        runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
        if not runtime_dir:
            return False
        return Path(runtime_dir, "kydisplay", "screencast-restore-token").is_file()

    def start_stream(self, uuid: str, choose_source: bool = False,
                     slow_link: bool = False) -> str:
        """Start a cast to this receiver.

        A desktop that remembers what was shared last time hands it back on
        every cast. Asking for it to be chosen instead is said here, for one
        cast; what is then chosen becomes what is remembered next.

        Which band the receiver put the group on is said here too, because
        only this side can see it, and the engine sends a mode and a rate a
        poor link will carry rather than one it will not."""
        import dbus

        manager, _ = self._dbus()
        result = manager.StartStream(uuid, dbus.Boolean(choose_source),
                                     dbus.Boolean(slow_link))
        return str(result)

    def set_discover(self, discover: bool):
        """Pause or resume the search for receivers (patch 0029).

        A Wi-Fi Direct search scans other channels on the radio that carries
        the stream, so it is paused while a picture is being sent. An engine
        without the property is left as it is.
        """
        import dbus

        try:
            _, properties = self._dbus()
            properties.Set(INTERFACE, "Discover", dbus.Boolean(discover))
        except Exception:
            pass

    def stop_stream(self, unit_name: str):
        manager, _ = self._dbus()
        manager.StopStream(unit_name)

    def set_stream_report(self, unit_name: str, wanted: bool) -> None:
        """Ask a running helper to publish its figures, or to stop. Totalling
        and sending them costs a little every second, so it is not asked for
        until somebody is reading them."""
        import dbus

        name = stream_bus_name(unit_name)
        if not name:
            return
        try:
            if not self._name_has_owner(name):
                return
            actions = dbus.Interface(self.bus.get_object(name, "/" + name.replace(".", "/")),
                                     "org.gtk.Actions")
            actions.Activate("report", [dbus.Boolean(wanted)], {})
        except Exception:
            pass

    def stream_figures(self, unit_name: str) -> str | None:
        """What the cast is doing now, as the helper publishes it: frames a
        second, megabits a second, dropped altogether, dropped lately, and the
        average gap in milliseconds, separated by bars. None while reporting
        is off or nothing has been totalled yet."""
        import dbus

        name = stream_bus_name(unit_name)
        if not name:
            return None
        try:
            if not self._name_has_owner(name):
                return None
            actions = dbus.Interface(self.bus.get_object(name, "/" + name.replace(".", "/")),
                                     "org.gtk.Actions")
            _enabled, _parameter, state = actions.Describe("figures")
            figures = str(state[0]) if state else ""
            return figures or None
        except Exception:
            return None

    def stream_picture(self, unit_name: str) -> str | None:
        """What the helper settled on with the receiver: the size of the
        picture, how often it is sent, what the rate is held to and which
        encoder is doing it. None while there is nothing settled yet, or
        where the helper is older than the action."""
        import dbus

        name = stream_bus_name(unit_name)
        if not name:
            return None
        try:
            if not self._name_has_owner(name):
                return None
            actions = dbus.Interface(self.bus.get_object(name, "/" + name.replace(".", "/")),
                                     "org.gtk.Actions")
            _enabled, _parameter, state = actions.Describe("picture")
            picture = str(state[0]) if state else ""
            return picture or None
        except Exception:
            return None

    def stream_notice(self, unit_name: str) -> str | None:
        """Something the helper needs a person to read, in words rather than
        as a state: what a virtual display needs, for one. None while there
        is nothing to read, or where the helper is older than the action."""
        import dbus

        name = stream_bus_name(unit_name)
        if not name:
            return None
        try:
            if not self._name_has_owner(name):
                return None
            actions = dbus.Interface(self.bus.get_object(name, "/" + name.replace(".", "/")),
                                     "org.gtk.Actions")
            _enabled, _parameter, state = actions.Describe("notice")
            notice = str(state[0]) if state else ""
            return notice or None
        except Exception:
            return None

    def stream_state(self, unit_name: str) -> str | None:
        """What the stream helper reports: "wait-p2p", "wait-socket",
        "wait-streaming", "streaming" and so on (patch 0012), or None when
        the helper cannot be asked."""
        import dbus

        name = stream_bus_name(unit_name)
        if not name:
            return None
        try:
            if not self._name_has_owner(name):
                return None
            actions = dbus.Interface(self.bus.get_object(name, "/" + name.replace(".", "/")),
                                     "org.gtk.Actions")
            _enabled, _parameter, state = actions.Describe("state")
            return str(state[0]) if state else None
        except Exception:
            return None

    def _name_has_owner(self, name: str) -> bool:
        import dbus

        proxy = self.bus.get_object("org.freedesktop.DBus", "/org/freedesktop/DBus")
        return bool(dbus.Interface(proxy, "org.freedesktop.DBus").NameHasOwner(name))

    def stream_active(self, unit_name: str) -> bool | None:
        """Whether the stream helper is running, or None if that is unknown.

        The distinction matters: treating a failed lookup as "not running"
        started a second helper alongside a live one, and two helpers
        negotiating with the same receiver is a guaranteed collision.
        """
        # A helper holding its D-Bus name is running, wherever it runs. This
        # is the only check that sees a helper started by an engine in another
        # Flatpak sandbox instance, such as one left from an earlier start.
        name = stream_bus_name(unit_name)
        try:
            if name and self._name_has_owner(name):
                return True
        except Exception:
            pass

        if in_sandbox():
            return _stream_process_running(unit_name)

        import dbus

        try:
            systemd = dbus.Interface(
                self.bus.get_object("org.freedesktop.systemd1", "/org/freedesktop/systemd1"),
                "org.freedesktop.systemd1.Manager",
            )
            unit_path = systemd.GetUnit(unit_name)
            properties = dbus.Interface(
                self.bus.get_object("org.freedesktop.systemd1", unit_path),
                PROPERTIES,
            )
            return str(properties.Get("org.freedesktop.systemd1.Unit", "ActiveState")) in {
                "activating",
                "active",
            }
        except dbus.exceptions.DBusException as error:
            # systemd reports a unit it has already cleaned up as NoSuchUnit,
            # which does mean the helper is gone.
            if "NoSuchUnit" in str(error) or "not loaded" in str(error):
                return False
            return None
        except Exception:
            return None


def stream_bus_name(unit_name: str) -> str | None:
    """The D-Bus name a stream helper takes for its unit (patch 0012):
    gnome-network-displays-stream-<sink>-<connection>.service becomes
    <prefix>.Stream_<connection>, with dashes as underscores."""
    suffix = ".service"
    if not unit_name or not unit_name.endswith(suffix):
        return None
    connection = unit_name[: -len(suffix)][-36:]
    try:
        import uuid

        uuid.UUID(connection)
    except ValueError:
        return None
    if connection.count("-") != 4:
        return None
    return f"{BUS_PREFIX}.Stream_{connection.replace('-', '_')}"


def in_sandbox() -> bool:
    return Path("/.flatpak-info").exists()


def _stream_process_running(unit_name: str, proc=Path("/proc")) -> bool | None:
    """Inside a sandbox the engine runs each stream as its own child and marks
    it with ND_STREAM_UNIT (patch 0009), so /proc answers what systemd would."""
    marker = f"ND_STREAM_UNIT={unit_name}".encode()
    try:
        entries = list(proc.iterdir())
    except OSError:
        return None
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            if marker in (entry / "environ").read_bytes().split(b"\0"):
                return True
        except OSError:
            continue
    return False
