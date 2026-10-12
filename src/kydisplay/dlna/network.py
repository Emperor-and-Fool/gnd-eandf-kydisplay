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

"""Which network interface is the LAN, on whatever machine this runs on.

Every test tonight hardcoded ``wlp0s20f3`` because that is what is in
front of me; a machine with Ethernet instead, or a different Wi-Fi card
name, needs a real answer instead of a guess.

The first version of this probed ``ip route get`` against a fixed
target address. Confirmed broken under Mullvad: Mullvad installs an
``ip rule`` (``not from all fwmark <mark> lookup <table>``) that sends
*any* unmarked traffic into its own table, carving out a route back to
the already-connected LAN subnet as the only exception — so a probe
target picks up the VPN tunnel unless it happens to land inside that
one carved-out subnet, which cannot be known in advance. No probe
target is safe, public or private.

The fix is to not use ``ip rule`` resolution at all: ``ip route show
table main default`` reads the default route directly out of the
``main`` table, which is where the kernel's real DHCP-assigned LAN
route lives regardless of what policy routing layers on top of it.
"""

from __future__ import annotations

import re
import subprocess

_ROUTE_RE = re.compile(r"\bdev (\S+).*\bsrc (\S+)")


def default_lan_interface_and_ip() -> tuple[str, str] | None:
    """The (interface, local IP) of the LAN default route in the
    kernel's ``main`` routing table — immune to VPN policy routing,
    since that lives in ``ip rule``/separate tables, never in ``main``
    itself. ``None`` if there is no default route there at all."""
    try:
        result = subprocess.run(
            ["ip", "route", "show", "table", "main", "default"],
            capture_output=True, text=True, timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    match = _ROUTE_RE.search(result.stdout)
    if not match:
        return None
    return match.group(1), match.group(2)
