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

"""Wake-on-LAN, for a renderer that is off when a cast is started.

Not LG-specific: this is the same magic packet for any brand, and it is
what LG's own SDK uses for TurnOn too (confirmed directly in
sweharris/go-lgtv's control/tv.go — the function calls nothing but this).

Unverified end-to-end on this machine: WoL needs the target to be on
Ethernet with it enabled in its network settings, and this TV has only
ever been reachable over Wi-Fi here. The packet this builds is correct
against the Wake-on-LAN specification regardless — that part is testable
without a wired TV; whether anything here actually wakes a given set is
not, and is not claimed.

Not called from the discovery or cast flow yet. Sending a packet proven
to do nothing on this hardware, on every cast attempt, would be dead
weight — this exists to be wired in and tested once a wired TV is
available, not to run speculatively before then.
"""

from __future__ import annotations

import socket

MAC_OCTETS = 6
MAGIC_PREAMBLE = b"\xff" * 6
WOL_PORT = 9


class InvalidMacAddress(ValueError):
    pass


def _parse_mac(mac: str) -> bytes:
    cleaned = mac.replace(":", "").replace("-", "")
    if len(cleaned) != MAC_OCTETS * 2:
        raise InvalidMacAddress(f"{mac!r} is not a 6-octet MAC address")
    try:
        return bytes.fromhex(cleaned)
    except ValueError as error:
        raise InvalidMacAddress(f"{mac!r} is not valid hex") from error


def magic_packet(mac: str) -> bytes:
    """The Wake-on-LAN magic packet for ``mac``: six bytes of ``0xff``
    followed by the MAC address repeated sixteen times, per the
    specification. Pure construction, no network — testable without any
    device at all."""
    address = _parse_mac(mac)
    return MAGIC_PREAMBLE + address * 16


def wake(mac: str, broadcast_address: str = "255.255.255.255", port: int = WOL_PORT) -> None:
    """Send the magic packet for ``mac`` to ``broadcast_address``. Fires the
    packet and returns; UDP gives no delivery confirmation, and even a
    delivered packet only wakes a target with WoL enabled on Ethernet. This
    is "sent", not "worked"."""
    packet = magic_packet(mac)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(packet, (broadcast_address, port))
    finally:
        sock.close()
