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

"""Find Digital Media Renderers on the LAN.

Generic, vendor-agnostic core. What is true of any UPnP renderer lives here:
a long-lived passive SSDP listener, a cache validated by plain HTTP rather
than by re-asking SSDP, and an active M-SEARCH on the standard
MediaRenderer type. What is true of LG specifically — its own second-screen
search target, its known port range, the scan fallback that stands in for
an SSDP responder proven tonight to go silent 22-24 seconds after boot —
lives in :mod:`.discovery_lg` and is called from here by name, as an
explicit extra step. Not behind a generic vendor-plugin interface: another
manufacturer's real quirks are not known yet, and guessing their shape
would be designing for a hypothetical.

Cheapest signal first, every time discovery is asked for an answer:

1. A previously-verified device's cached location — one HTTP GET away from
   confirming it is still there, which it usually is even when SSDP has
   gone completely quiet (confirmed directly tonight).
2. A sighting the passive listener already has but has not verified yet —
   the TV may have been turned on moments ago by someone not trying to
   cast; that sighting should not be wasted waiting for someone to ask.
3. An active M-SEARCH burst, generic and LG targets together.
4. The LG known-ports scan, called explicitly, only when asked for.
"""

from __future__ import annotations

import time
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass

import gi

gi.require_version("GUPnP", "1.6")
gi.require_version("GSSDP", "1.6")

from gi.repository import GLib, GSSDP, GUPnP  # noqa: E402

from . import discovery_lg
from .device_store import DeviceStore

MEDIA_RENDERER = "urn:schemas-upnp-org:device:MediaRenderer:1"
DEVICE_NS = "urn:schemas-upnp-org:device-1-0"


@dataclass(frozen=True)
class DmrDevice:
    udn: str
    name: str
    location: str
    is_lg: bool = False


def _parse_description(xml_text: str) -> dict | None:
    """Pull deviceType, friendlyName and UDN out of a device description.
    Plain ElementTree with the UPnP namespace, not GUPnP: every verification
    path here fetches the description as text anyway, and parsing it
    directly avoids needing a GUPnP.DeviceProxy for data already in hand."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    device = root.find(f"{{{DEVICE_NS}}}device")
    if device is None:
        return None
    device_type = device.findtext(f"{{{DEVICE_NS}}}deviceType", default="")
    friendly_name = device.findtext(f"{{{DEVICE_NS}}}friendlyName", default="")
    udn = device.findtext(f"{{{DEVICE_NS}}}UDN", default="")
    if not udn:
        return None
    return {"deviceType": device_type, "friendlyName": friendly_name, "udn": udn}


def _fetch_url(location: str, timeout: float = 3.0) -> str | None:
    try:
        with urllib.request.urlopen(location, timeout=timeout) as resp:
            return resp.read(8192).decode(errors="replace")
    except Exception:
        return None


class DmrDiscovery:
    """Digital Media Renderers on this LAN: cached, freshly sighted, or
    actively searched for, cheapest signal first."""

    def __init__(self, context=None, store=None, fetch=None):
        self._context = context or GUPnP.Context.new_full(
            None, None, 0, GSSDP.UDAVersion.VERSION_1_0)
        self._store = store if store is not None else DeviceStore()
        # How a candidate location is fetched. Injectable so tests do not
        # depend on a real network or a real renderer.
        self._fetch = fetch or _fetch_url

        # Long-lived, started once, never torn down between lookups: the
        # only way to catch a 22-24 second announcement window without
        # already knowing exactly when it will happen.
        self._generic_cp = GUPnP.ControlPoint.new(self._context, MEDIA_RENDERER)
        self._lg_cp = GUPnP.ControlPoint.new(
            self._context, discovery_lg.SECOND_SCREEN_TARGET)
        self._generic_cp.connect("device-proxy-available", self._on_sighting)
        self._lg_cp.connect("device-proxy-available", self._on_sighting)
        self._generic_cp.set_active(True)
        self._lg_cp.set_active(True)

        # UDN -> (location, monotonic seen-at). Raw, unverified; a lookup
        # tries these before deciding anything is missing.
        self._sightings: dict[str, tuple[str, float]] = {}

    def _on_sighting(self, control_point, proxy) -> None:
        udn = proxy.get_udn()
        location = proxy.get_location()
        self._sightings[udn] = (location, time.monotonic())
        self._store.record_sighting(udn, location, verified=False)

    def pump(self) -> None:
        """Drain whatever SSDP traffic is already waiting, without blocking
        for more. Call this on whatever timer already runs the app; nothing
        here starts a GLib main loop of its own."""
        context = GLib.MainContext.default()
        while context.iteration(False):
            pass

    def verify(self, udn: str, location: str) -> DmrDevice | None:
        """Confirm a candidate location is actually there and actually a
        MediaRenderer, right now, with one plain HTTP GET. ``None`` on
        anything that fails or is not a MediaRenderer — a failure here
        means "try something else", not an error worth raising."""
        xml_text = self._fetch(location)
        if xml_text is None:
            return None
        parsed = _parse_description(xml_text)
        if parsed is None or "MediaRenderer" not in parsed["deviceType"]:
            return None
        is_lg = discovery_lg.is_lg_description(xml_text)
        self._store.mark_verified(udn, location, parsed["friendlyName"], is_lg)
        return DmrDevice(udn=udn, name=parsed["friendlyName"] or udn,
                         location=location, is_lg=is_lg)

    def devices(self, active_search_seconds: float = 0) -> list[DmrDevice]:
        """Every renderer this lookup can confirm right now.

        Tries, cheapest first: the store's previously-verified devices: a
        fresh sighting the passive listener has but has not verified yet:
        then, only if ``active_search_seconds`` is given, waits that long
        pumping the context for an active M-SEARCH response before trying
        sightings once more. The LG known-ports scan is never run from
        here — call :meth:`lg_fallback` explicitly, since it is LG-specific
        and this method is not.
        """
        self.pump()
        found: dict[str, DmrDevice] = {}

        for stored in self._store.all():
            device = self.verify(stored.udn, stored.location)
            if device:
                found[device.udn] = device

        for udn, (location, _seen_at) in list(self._sightings.items()):
            if udn in found:
                continue
            device = self.verify(udn, location)
            if device:
                found[udn] = device

        if active_search_seconds > 0 and not found:
            deadline = time.monotonic() + active_search_seconds
            while time.monotonic() < deadline:
                GLib.MainContext.default().iteration(True)
            self.pump()
            for udn, (location, _seen_at) in list(self._sightings.items()):
                if udn in found:
                    continue
                device = self.verify(udn, location)
                if device:
                    found[udn] = device

        return list(found.values())

    def lg_fallback(self, ip: str) -> list[DmrDevice]:
        """LG's known-ports scan against one IP, called explicitly. Proven
        tonight at well under a second for one known IP; see
        :mod:`.discovery_lg` for the subnet-wide version when even the IP
        is not known."""
        found = []
        for port in discovery_lg.scan_known_ports(ip):
            xml_text = discovery_lg.fetch_description(ip, port)
            if not xml_text:
                continue
            parsed = _parse_description(xml_text)
            if parsed is None or "MediaRenderer" not in parsed["deviceType"]:
                continue
            location = f"http://{ip}:{port}/"
            self._store.mark_verified(parsed["udn"], location, parsed["friendlyName"], True)
            found.append(DmrDevice(udn=parsed["udn"], name=parsed["friendlyName"],
                                   location=location, is_lg=True))
        return found

    def stop(self) -> None:
        self._generic_cp.set_active(False)
        self._lg_cp.set_active(False)
