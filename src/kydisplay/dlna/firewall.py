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

"""Make sure the TV can reach this machine, once, the way GND already does
for Miracast — not on every cast.

There is no single Linux API for this. firewalld and ufw are genuinely
different tools: firewalld exposes a D-Bus service with a free, unauthenticated
read tier and an authenticated write tier; ufw is a plain CLI with no daemon,
no D-Bus service, and no unprivileged read path at all — confirmed directly,
`ufw status` itself refuses to run as a normal user. Nothing is assumed;
:func:`detect_backend` checks what is actually active before anything else
runs.

The firewalld path mirrors GND's own ``nd_firewalld_ensure_wfd_zone``
exactly, confirmed by reading its source: a free permanent-config read
(``queryPort``/``queryService``) first, and only if that says the rule is
missing, the one authenticated write (``addPort``/``addService`` + reload)
that can ever prompt for a password. Call :func:`ensure_port_open` and
:func:`ensure_service_enabled` every time a cast starts, the same as GND
calls its ensure function on every stream start — the cost is paid once,
the first time either is actually missing, never again after that.

The ufw path is real code, not a stub, but it carries a real gap: since
even checking ufw's status needs root, there is no free check possible —
every call needs a privileged subprocess, and nothing here wraps that in
an actual privilege-escalation prompt (pkexec or otherwise). That wrapping
does not exist yet and is not pretended to here.

If neither is detected, nothing is attempted. Guessing at an unknown
iptables/nft setup risks breaking it rather than fixing it; the honest
behaviour is to say so and let the real symptom — the TV failing to fetch
— be what tells the user, rather than silently doing nothing and claiming
success.
"""

from __future__ import annotations

import logging
import socket
import subprocess

from . import network

logger = logging.getLogger(__name__)

DLNA_PORT = 7235
DLNA_PROTOCOL = "tcp"


def dlna_port_reachable(timeout: float = 1.0) -> bool:
    """Whether port 7235 actually answers a plain TCP connect from this
    machine's own LAN address right now — the same test as ``nc -z
    <lan-ip> 7235``, confirmed directly as the thing that proved the
    port was closed when firewalld's own read APIs could not be trusted
    to say so without authentication. No D-Bus, no polkit, no privilege
    of any kind: a closed port here is ground truth, whatever the cause
    actually is."""
    address = network.default_lan_interface_and_ip()
    if address is None:
        logger.info("dlna_port_reachable: no LAN interface found, reporting closed")
        return False
    _, ip = address
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((ip, DLNA_PORT))
        logger.info("dlna_port_reachable: connected to %s:%s -> OPEN", ip, DLNA_PORT)
        return True
    except OSError as error:
        logger.info("dlna_port_reachable: %s:%s -> CLOSED (%s)", ip, DLNA_PORT, error)
        return False
    finally:
        sock.close()

# Zones that exist but are not a network location a person ever picks for a
# real LAN connection — each is auto-managed by the subsystem that owns it,
# for that subsystem's own virtual networking, confirmed directly against
# this machine (all three ship as stock zone files, same as home/work, so
# "stock" alone does not separate them). Excluded here, not because they are
# rare, but because they belong to Docker, libvirt, and NetworkManager's own
# connection-sharing/hotspot mode respectively, never to a NIC a human
# associates with a Wi-Fi or Ethernet network.
#
# "P2P-WiFi-Display" is this same kind of zone, just owned by this project's
# own GND engine instead of a different one: confirmed in
# ``nd-firewalld.h``'s ``ND_WFD_ZONE`` as the dedicated zone Miracast's P2P
# connection gets reassigned into, never a location a human associates a NIC
# with either.
_NON_LAN_ZONES = frozenset({"docker", "libvirt", "libvirt-routed", "nm-shared", "P2P-WiFi-Display"})

# Zones that are a real human-assignable network location, but not a
# sensible choice to add a cast-access rule to — each for its own reason,
# confirmed against this system's own zone definitions (target="..." in
# /usr/lib/firewalld/zones/*.xml):
#   drop  (target DROP):        silently rejects all unsolicited incoming by
#                                design; picking it already means "give this
#                                network nothing," so adding one is the
#                                opposite of why someone is on it.
#   block (target %%REJECT%%):  the same deny-everything intent as drop,
#                                just an explicit rejection instead of
#                                silence — not a different case.
#   trusted (target ACCEPT):    already accepts everything, so the
#                                checkbox would be a no-op with nothing to
#                                add.
#   dmz:                        a role for a deliberately-exposed server,
#                                not a zone a laptop's own Wi-Fi/Ethernet
#                                connection is normally assigned to.
#   external / internal:        designed as a pair for a machine acting as
#                                a router/NAT gateway between two network
#                                segments — confirmed directly in their own
#                                zone XML: external has <masquerade/> and
#                                <forward/>, internal has <forward/> with
#                                no masquerade. That pairing only means
#                                something for a machine forwarding traffic
#                                between networks, not a single-NIC laptop
#                                picking which network it is on.
_ZONES_NOT_WORTH_OFFERING = frozenset({"drop", "block", "trusted", "dmz", "external", "internal"})

_mainloop_ready = False


def _ensure_dbus_mainloop() -> None:
    """Must run before the first ``dbus.SystemBus()`` call anywhere in this
    process -- confirmed directly, setting it later (even before
    FirewallClient specifically) is too late once any connection already
    exists without one, since dbus-python shares the underlying
    connection."""
    global _mainloop_ready
    if _mainloop_ready:
        return
    import dbus.mainloop.glib

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    _mainloop_ready = True


def _firewalld_active() -> bool:
    try:
        _ensure_dbus_mainloop()
        import dbus

        bus = dbus.SystemBus()
        proxy = bus.get_object("org.freedesktop.DBus", "/org/freedesktop/DBus")
        return bool(
            dbus.Interface(proxy, "org.freedesktop.DBus")
            .NameHasOwner("org.fedoraproject.FirewallD1")
        )
    except Exception:
        return False


def _ufw_active() -> bool:
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "ufw"],
            capture_output=True, text=True, timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.stdout.strip() == "active"


def detect_backend() -> str | None:
    """``"firewalld"``, ``"ufw"``, or ``None`` if neither is active.
    Checked in this order because firewalld is this project's primary
    target platform, confirmed active by default on it, with ufw present
    but masked — not because firewalld is assumed everywhere."""
    if _firewalld_active():
        return "firewalld"
    if _ufw_active():
        return "ufw"
    return None


def current_zone(interface: str | None = None) -> str | None:
    """The zone firewalld already has the LAN interface in right now.
    This is the one case a selection dialog does not need to ask about —
    whatever is actually associated is already in exactly one zone."""
    try:
        _ensure_dbus_mainloop()
        from firewall.client import FirewallClient

        fc = FirewallClient()
        iface = interface or (network.default_lan_interface_and_ip() or (None, None))[0]
        result = fc.getZoneOfInterface(iface) if iface else None
        logger.info("current_zone: interface=%s -> %s", iface, result)
        return result
    except Exception:
        logger.exception("current_zone: failed")
        return None


def list_zones() -> list[str]:
    """Every zone firewalld has configured that a person could actually
    mean as a network location, not just the one currently bound to an
    interface. "home" and "work" are common defaults, not a guarantee —
    a laptop that travels needs the real list, the same way GND's own
    ``getZones`` asks firewalld what it actually has rather than
    assuming a name exists before acting on it. Confirmed directly: this
    lists zones such as "work" even while nothing is bound to them,
    which is exactly what lets a zone be prepared ahead of travelling to
    it, not only reacted to once already there.

    :data:`_NON_LAN_ZONES` is filtered out — zones that exist but belong
    to another subsystem's own virtual networking, never to a NIC a
    human associates with a real network. :data:`_ZONES_NOT_WORTH_OFFERING`
    is filtered out too — real, human-assignable zones that are each a
    bad fit for a cast-access checkbox for their own reason; see that
    constant for which and why."""
    try:
        _ensure_dbus_mainloop()
        from firewall.client import FirewallClient

        excluded = _NON_LAN_ZONES | _ZONES_NOT_WORTH_OFFERING
        result = [z for z in FirewallClient().config().getZoneNames()
                  if z not in excluded]
        logger.info("list_zones: -> %s", result)
        return result
    except Exception:
        logger.exception("list_zones: failed, returning empty list")
        return []


def _firewalld_zone_config(zone: str | None, interface: str | None):
    from firewall.client import FirewallClient

    _ensure_dbus_mainloop()
    fc = FirewallClient()
    zone_name = zone or current_zone(interface)
    if not zone_name:
        logger.info("_firewalld_zone_config: no zone name resolved (zone=%s interface=%s)", zone, interface)
        return None, None
    try:
        return fc, fc.config().getZoneByName(zone_name)
    except Exception:
        logger.exception("_firewalld_zone_config: getZoneByName(%r) failed", zone_name)
        return None, None


def port_already_open(port: int = DLNA_PORT, zone: str | None = None,
                      interface: str | None = None) -> bool | None:
    """Whether ``port`` is already permanently open in ``zone`` — a plain
    ``queryPort``, never ``addPort``. For showing a dialog's current
    state before anyone has pressed anything: opening the dialog must
    never itself be the thing that writes a rule. ``None`` means
    undeterminable (no firewalld, or the zone could not be resolved),
    not "closed" — the caller must tell those apart."""
    if detect_backend() != "firewalld":
        return None
    fc, zone_config = _firewalld_zone_config(zone, interface)
    if zone_config is None:
        return None
    try:
        result = bool(zone_config.queryPort(str(port), DLNA_PROTOCOL))
        logger.info("port_already_open: zone=%s port=%s -> %s", zone, port, result)
        return result
    except Exception:
        logger.exception("port_already_open: queryPort failed (zone=%s port=%s)", zone, port)
        return None


def service_already_enabled(service: str, zone: str | None = None,
                            interface: str | None = None) -> bool | None:
    """Same as :func:`port_already_open`, for a named service — a plain
    ``queryService``, never ``addService``."""
    if detect_backend() != "firewalld":
        return None
    fc, zone_config = _firewalld_zone_config(zone, interface)
    if zone_config is None:
        return None
    try:
        result = bool(zone_config.queryService(service))
        logger.info("service_already_enabled: zone=%s service=%s -> %s", zone, service, result)
        return result
    except Exception:
        logger.exception("service_already_enabled: queryService failed (zone=%s service=%s)", zone, service)
        return None


def _ensure_firewalld_port(port: int, zone: str | None, interface: str | None) -> bool:
    fc, zone_config = _firewalld_zone_config(zone, interface)
    if zone_config is None:
        logger.info("_ensure_firewalld_port: no zone_config for zone=%s, giving up", zone)
        return False
    try:
        if zone_config.queryPort(str(port), DLNA_PROTOCOL):
            logger.info("_ensure_firewalld_port: zone=%s port=%s already open", zone, port)
            return True
        logger.info("_ensure_firewalld_port: zone=%s port=%s missing, calling addPort (may prompt)", zone, port)
        zone_config.addPort(str(port), DLNA_PROTOCOL)
        fc.reload()
        logger.info("_ensure_firewalld_port: zone=%s port=%s added and reloaded", zone, port)
        return True
    except Exception:
        logger.exception("_ensure_firewalld_port: failed (zone=%s port=%s)", zone, port)
        raise


def _ensure_firewalld_service(service: str, zone: str | None, interface: str | None) -> bool:
    fc, zone_config = _firewalld_zone_config(zone, interface)
    if zone_config is None:
        logger.info("_ensure_firewalld_service: no zone_config for zone=%s, giving up", zone)
        return False
    try:
        if zone_config.queryService(service):
            logger.info("_ensure_firewalld_service: zone=%s service=%s already enabled", zone, service)
            return True
        logger.info("_ensure_firewalld_service: zone=%s service=%s missing, calling addService (may prompt)", zone, service)
        zone_config.addService(service)
        fc.reload()
        logger.info("_ensure_firewalld_service: zone=%s service=%s added and reloaded", zone, service)
        return True
    except Exception:
        logger.exception("_ensure_firewalld_service: failed (zone=%s service=%s)", zone, service)
        raise


def _ensure_ufw_port(port: int) -> bool:
    """No free check exists for ufw — confirmed directly, ``ufw status``
    itself requires root. This calls ``ufw allow`` unconditionally, which
    needs a privileged subprocess this function does not provide; it will
    fail with a permission error under a normal user until something
    wraps it in pkexec or equivalent. Real, not a stub — but honestly
    incomplete, not pretending otherwise."""
    try:
        result = subprocess.run(
            ["ufw", "allow", f"{port}/{DLNA_PROTOCOL}"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def ensure_port_open(port: int = DLNA_PORT, zone: str | None = None,
                     interface: str | None = None) -> bool:
    """Make sure ``port`` is permanently reachable in ``zone`` (or, if
    ``zone`` is not given, whichever zone the LAN interface is in right
    now), using whichever backend is actually active. ``True`` if
    confirmed open (whether it already was or was just added); ``False``
    if nothing could be determined or done — the caller must tell the
    user, not assume success. ``zone`` is how a selection dialog reaches
    a zone that is not the current one, such as "work" prepared ahead of
    travelling there — see :func:`list_zones`. ufw has no concept of
    zones at all, so ``zone`` is ignored on that backend."""
    backend = detect_backend()
    if backend == "firewalld":
        return _ensure_firewalld_port(port, zone, interface)
    if backend == "ufw":
        return _ensure_ufw_port(port)
    return False


def ensure_service_enabled(service: str, zone: str | None = None,
                           interface: str | None = None) -> bool:
    """Same as :func:`ensure_port_open`, for a named firewalld service —
    such as ``ssdp``, needed for discovery to receive replies, confirmed
    missing from ``home``'s defaults earlier this project. firewalld-only:
    ufw has no equivalent named-service concept, only ports, so this
    returns ``False`` on ufw and the caller treats that the same as
    "nothing to verify" rather than a failure specific to this call."""
    if detect_backend() == "firewalld":
        return _ensure_firewalld_service(service, zone, interface)
    return False
