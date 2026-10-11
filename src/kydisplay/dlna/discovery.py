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

GUPnP's ControlPoint sends the SSDP M-SEARCH and keeps listening for
receivers announcing themselves unasked, but it only does either while
something is turning the crank on the GLib main context it was given: that is
where the UDP socket actually gets read. Nothing here runs a main loop of its
own, because there already is one, Qt's, and a second one competing for the
same sockets is not wanted. ``pump()`` asks the context for whatever is
already waiting, never blocking, so a caller already polling on its own timer
only has to call it there.
"""

from __future__ import annotations

from dataclasses import dataclass

import gi

gi.require_version("GUPnP", "1.6")
gi.require_version("GSSDP", "1.6")

from gi.repository import GLib, GSSDP, GUPnP  # noqa: E402

MEDIA_RENDERER = "urn:schemas-upnp-org:device:MediaRenderer:1"


@dataclass(frozen=True)
class DmrDevice:
    udn: str
    name: str
    location: str


class DmrDiscovery:
    """Digital Media Renderers answering SSDP on this link, right now."""

    def __init__(self, context=None):
        self._context = context or GUPnP.Context.new_full(
            None, None, 0, GSSDP.UDAVersion.VERSION_1_0)
        self._control_point = GUPnP.ControlPoint.new(self._context, MEDIA_RENDERER)
        self._control_point.set_active(True)

    def pump(self) -> None:
        """Process whatever SSDP traffic or SOAP replies are already waiting
        on the shared GLib context, without waiting for more. Call this
        often: a renderer answering M-SEARCH, or an AVTransport call made
        through :mod:`.client`, is invisible until something does."""
        context = GLib.MainContext.default()
        while context.iteration(False):
            pass

    def devices(self) -> list[DmrDevice]:
        self.pump()
        return [
            DmrDevice(
                udn=proxy.get_udn(),
                name=proxy.get_friendly_name() or proxy.get_udn(),
                location=proxy.get_location(),
            )
            for proxy in self._control_point.list_device_proxies()
        ]

    def device_proxy(self, udn: str):
        """The live :class:`GUPnP.DeviceProxy` for a UDN :meth:`devices`
        listed, or ``None`` if it has gone since. Resolved by UDN rather than
        kept from the last listing, because discovery replaces its proxies
        as receivers come and go and a kept one can already be stale by the
        time a cast is started."""
        self.pump()
        for proxy in self._control_point.list_device_proxies():
            if proxy.get_udn() == udn:
                return proxy
        return None

    def stop(self) -> None:
        self._control_point.set_active(False)
