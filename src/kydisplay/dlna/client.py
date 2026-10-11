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

"""Tell a renderer to play a URL.

Two SOAP calls, made in order: SetAVTransportURI hands the renderer a stream
and a DIDL-Lite description of it, Play starts it. Nothing here watches what
happens after — no state, no GENA subscription, no retry. A renderer that
refuses either call raises, and the caller decides what a person is told.

Many renderers refuse SetAVTransportURI outright without a DIDL-Lite
description, so one is always sent, built with only what matters for a
single pushed stream: a title, a class, and the URL again as a resource.
"""

from __future__ import annotations

import gi

gi.require_version("GUPnP", "1.6")
gi.require_version("GUPnPAV", "1.0")

from gi.repository import GUPnP, GUPnPAV  # noqa: E402

AVTRANSPORT = "urn:schemas-upnp-org:service:AVTransport:1"

# Any instance works for a renderer that only ever has one, which is every
# renderer a television is. The specification allows more; nothing here casts
# to one that needs asking which.
INSTANCE_ID = 0


class NoAvTransport(RuntimeError):
    """Raised when a device discovery found has no AVTransport service.

    Every MediaRenderer target has one by definition, so this is the UPnP
    description lying rather than an ordinary failure."""


def _metadata(title: str, uri: str) -> str:
    writer = GUPnPAV.DIDLLiteWriter.new(None)
    item = writer.add_item()
    item.set_id("0")
    item.set_parent_id("-1")
    item.set_restricted(True)
    item.set_title(title or uri)
    item.set_upnp_class("object.item.videoItem")

    resource = item.add_resource()
    resource.set_uri(uri)
    resource.set_protocol_info(GUPnPAV.ProtocolInfo.new_from_string("http-get:*:*:*"))

    return writer.get_string()


def _avtransport(device_proxy) -> GUPnP.ServiceProxy:
    service = device_proxy.get_service(AVTRANSPORT)
    if service is None:
        raise NoAvTransport(f"{device_proxy.get_friendly_name()} has no AVTransport service")
    return service


def play(device_proxy, stream_url: str, title: str = "") -> None:
    """Push ``stream_url`` to ``device_proxy`` and start it.

    ``device_proxy`` is a live :class:`GUPnP.DeviceProxy`, as
    :meth:`kydisplay.dlna.discovery.DmrDiscovery.device_proxy` returns. Raises
    on any SOAP fault or transport error; there is nothing to roll back, since
    nothing has been sent to the renderer until the call that fails.
    """
    service = _avtransport(device_proxy)

    set_uri = GUPnP.ServiceProxyAction.new_from_list(
        "SetAVTransportURI",
        ["InstanceID", "CurrentURI", "CurrentURIMetaData"],
        [INSTANCE_ID, stream_url, _metadata(title, stream_url)],
    )
    service.call_action(set_uri)

    play_action = GUPnP.ServiceProxyAction.new_from_list(
        "Play", ["InstanceID", "Speed"], [INSTANCE_ID, "1"])
    service.call_action(play_action)
