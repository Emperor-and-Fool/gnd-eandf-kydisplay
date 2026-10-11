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

"""LG webOS-specific discovery help.

LG's SSDP responder is reactive and short-cached: it announces itself for
roughly 22-24 seconds after boot and otherwise ignores M-SEARCH, confirmed by
direct observation against a UN74006LB — a documented, decade-long pattern
across other DLNA/casting projects targeting LG TVs, not a quirk of this
code. This module holds everything that is true of LG specifically and
nothing that is assumed to generalise to another vendor: discovery.py calls
it by name as an explicit extra step, never through a generic plugin
interface, since a different vendor's real quirks are not yet known and
guessing their shape here would be designing for a hypothetical.
"""

from __future__ import annotations

import concurrent.futures
import socket
import urllib.request

# LG's own second-screen service. Connect SDK, LG's official discovery
# framework, targets this before the generic MediaRenderer type — the single
# highest-impact finding of tonight's research. Kept here, not in the generic
# target list, because it is LG's own target, not a UPnP standard one.
SECOND_SCREEN_TARGET = "urn:lge-com:service:webos-second-screen:1"

# Every port this UN74006LB has answered a UPnP description on tonight,
# across five-plus reboots and one 30-minute idle session: the MediaRenderer,
# ConnectionManager and RenderingControl root (rotates per boot), the
# webos-second-screen/Basic device, the lge:device:tv control service, and
# DIAL. Not a guess at LG's port allocation scheme — every value here was
# seen answering a real HTTP GET on the real device.
KNOWN_PORTS = [1073, 1094, 1169, 1184, 1266, 1392, 1485, 1507,
               1735, 1760, 1770, 1830, 1869, 1915, 1981]

# The SSAP WebSocket control channel. Present but answers only a WebSocket
# upgrade, not a plain GET — useful as a presence signal (open vs not), not
# as a source of a parseable description.
SSAP_PORTS = [3000, 3001]

SCAN_TIMEOUT = 0.4
SCAN_WORKERS = 300


def is_lg_description(xml_text: str) -> bool:
    """Whether a fetched device description identifies an LG webOS TV.

    Checked against the actual response this UN74006LB gives: manufacturer
    is always "LG Electronics" or "LG", friendlyName always carries
    "webOS TV". Either alone is enough; neither is specific to one service,
    so this works the same whichever of the TV's several descriptions was
    fetched.
    """
    lowered = xml_text.lower()
    return "lg electronics" in lowered or "webos tv" in lowered or ">lg<" in lowered


def _port_open(ip: str, port: int) -> int | None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(SCAN_TIMEOUT)
    try:
        if sock.connect_ex((ip, port)) == 0:
            return port
    except OSError:
        pass
    finally:
        sock.close()
    return None


def scan_known_ports(ip: str) -> list[int]:
    """Which of LG's known ports answer a plain TCP connect on this IP,
    right now. Proven tonight: ~9 open ports on a live TV found in under a
    second against one known IP, and the same known-ports list swept across
    an entire /24 in 3.2 seconds with zero false positives — this is cheap
    enough to not need to be a last resort."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=SCAN_WORKERS) as pool:
        results = pool.map(lambda p: _port_open(ip, p), KNOWN_PORTS)
    return sorted(p for p in results if p is not None)


def scan_subnet(subnet_hosts) -> list[tuple[str, int]]:
    """Which (ip, port) pairs among LG's known ports answer anywhere in
    `subnet_hosts` (an iterable of IP strings, e.g. from
    ``ipaddress.ip_network(...).hosts()``). Used only when there is no
    known IP at all to scan directly — confirmed tonight at 3.2s for a
    full /24, versus 20 minutes for a blind full-port-range sweep of the
    same subnet that found nothing extra."""
    targets = [(ip, port) for ip in subnet_hosts for port in KNOWN_PORTS]
    with concurrent.futures.ThreadPoolExecutor(max_workers=SCAN_WORKERS) as pool:
        results = pool.map(lambda t: (t[0], _port_open(t[0], t[1])), targets)
    return sorted((ip, port) for ip, port in results if port is not None)


def fetch_description(ip: str, port: int, timeout: float = 3.0) -> str | None:
    """The raw device description at this candidate, or ``None`` if it is
    not one — several of LG's known ports are not UPnP descriptions at all
    (the SSAP WebSocket ports reset a plain GET; a couple of others 403 or
    412). A failure here is an expected outcome, not an error."""
    url = f"http://{ip}:{port}/"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.read(4096).decode(errors="replace")
    except Exception:
        return None
