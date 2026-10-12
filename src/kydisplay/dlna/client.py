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

Takes a device's ``location`` — the plain description URL
:class:`kydisplay.dlna.discovery.DmrDevice` carries — not a
:class:`GUPnP.DeviceProxy`. Discovery stopped handing out GUPnP device
proxies because the ControlPoint path that would produce them was never
proven reliable; every real push against the actual TV all night went
through a plain HTTP POST built by hand, so that is what this does too,
deliberately, not as a stopgap.

The control URL is not assumed to follow any fixed pattern. It is read
fresh from the device's own description each call: the AVTransport
service's ``controlURL``, resolved against ``location``. This TV's own
pattern (``/AVTransport/<udn>/control.xml``) was seen directly, but nothing
here hard-codes it — a different renderer, or this one after a firmware
update, may shape it differently, and the description already says what it
actually is.

Two SOAP calls, made in order: SetAVTransportURI hands the renderer a
stream and a DIDL-Lite description of it, Play starts it. Nothing here
watches what happens after — no state, no GENA subscription, no retry. A
renderer that refuses either call raises, and the caller decides what a
person is told.

Many renderers refuse SetAVTransportURI outright without a DIDL-Lite
description, so one is always sent, built with only what matters for a
single pushed stream: a title, a class, and the URL again as a resource.

Waking the renderer before any of this is optional and belongs here, not
in discovery: discovery's job is finding what is already reachable, not
starting what is not. Pass ``mac`` only once it is known to do something —
see :mod:`.wake` for what is and is not verified about it.
"""

from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import xml.sax.saxutils as saxutils

import gi

gi.require_version("GUPnPAV", "1.0")

from gi.repository import GUPnPAV  # noqa: E402

from . import wake

DEVICE_NS = "urn:schemas-upnp-org:device-1-0"
SOAP_NS = "http://schemas.xmlsoap.org/soap/envelope/"
AVTRANSPORT_TYPE = "urn:schemas-upnp-org:service:AVTransport:1"

# Any instance works for a renderer that only ever has one, which is every
# renderer a television is. The specification allows more; nothing here casts
# to one that needs asking which.
INSTANCE_ID = 0


class NoAvTransport(RuntimeError):
    """Raised when a device's own description has no AVTransport service,
    or could not be read at all. Every MediaRenderer target has one by
    definition, so a missing service means the description lied or the
    renderer is unreachable, not that this call made a wrong assumption."""


class SoapFault(RuntimeError):
    """Raised when the renderer answered with a SOAP fault. ``code`` is
    the UPnP error code the renderer itself gave, such as "716" (resource
    not found — confirmed tonight as what an unreachable port looks like
    to the caller) or "701" ("Transition not available" — confirmed
    tonight as what this TV says about a *different* problem entirely,
    casting again before it finished processing the last one. The two
    are not interchangeable; a caller deciding what a fault means must
    look at which code this is, not just that a fault happened."""

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


def _fetch_url(url: str, timeout: float = 5.0) -> str | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.read().decode(errors="replace")
    except Exception:
        return None


def _post_soap(control_url: str, action: str, body_xml: str,
               timeout: float = 20.0) -> str:
    envelope = (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<s:Envelope xmlns:s="{SOAP_NS}" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        f'<s:Body>{body_xml}</s:Body></s:Envelope>'
    ).encode()
    request = urllib.request.Request(
        control_url, data=envelope, method="POST",
        headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPAction": f'"{AVTRANSPORT_TYPE}#{action}"',
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return resp.read().decode(errors="replace")
    except urllib.error.HTTPError as error:
        fault_body = error.read().decode(errors="replace")
        message, code = _describe_fault(action, error.code, fault_body)
        raise SoapFault(message, code=code) from error


def _describe_fault(action: str, status: int, fault_body: str) -> tuple[str, str | None]:
    try:
        root = ET.fromstring(fault_body)
    except ET.ParseError:
        return f"{action} failed: HTTP {status}", None
    code = root.findtext(".//{urn:schemas-upnp-org:control-1-0}errorCode")
    description = root.findtext(".//{urn:schemas-upnp-org:control-1-0}errorDescription")
    if code:
        return f"{action} failed: UPnP error {code} ({description or 'no description'})", code
    return f"{action} failed: HTTP {status}", None


def _metadata(title: str, uri: str, duration: int | None = None) -> str:
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
    # How long the thing runs, which is the only way a renderer can know:
    # the stream it is sent is MPEG-TS, which carries no length of its own,
    # so a television told nothing here has nothing to draw a position bar
    # from. Written as a duration attribute on the resource — not into
    # protocolInfo, which stays the bare wildcard it has to be: narrowing
    # that field broke casting outright once and was reverted.
    #
    # Omitted rather than guessed when unknown. A live broadcast has no
    # length, and claiming one would describe the stream wrongly.
    if duration:
        resource.set_duration(int(duration))

    return writer.get_string()


def find_avtransport_control_url(location: str, fetch=None) -> str:
    """The AVTransport control URL this device's own description names,
    resolved against ``location``. Read fresh every call, not cached or
    assumed — the one thing proven tonight to change between sightings of
    the same device is exactly this kind of per-boot detail."""
    fetch = fetch or _fetch_url
    xml_text = fetch(location)
    if xml_text is None:
        raise NoAvTransport(f"could not fetch the description at {location}")
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as error:
        raise NoAvTransport(f"{location} did not return a parseable description") from error
    for service in root.iter(f"{{{DEVICE_NS}}}service"):
        service_type = service.findtext(f"{{{DEVICE_NS}}}serviceType", default="")
        if service_type == AVTRANSPORT_TYPE:
            control_url = service.findtext(f"{{{DEVICE_NS}}}controlURL", default="")
            if not control_url:
                raise NoAvTransport(f"{location} lists AVTransport with no controlURL")
            return urllib.parse.urljoin(location, control_url)
    raise NoAvTransport(f"{location} has no AVTransport service")


def play(location: str, stream_url: str, title: str = "", duration: int | None = None,
         mac: str | None = None, fetch=None, post=None) -> None:
    """Push ``stream_url`` to the renderer at ``location`` and start it.

    ``location`` is a :class:`~kydisplay.dlna.discovery.DmrDevice`'s
    ``location`` — its plain description URL, not a GUPnP object. Raises
    :class:`NoAvTransport` if the device has none, :class:`SoapFault` if
    the renderer refuses either call. Nothing is rolled back on failure:
    nothing is sent until the call that fails.

    ``mac``, if given, sends a Wake-on-LAN packet first — see :mod:`.wake`
    for what that is and is not proven to do. Omitted by default; this
    function does not guess whether a renderer needs or supports it.
    """
    if mac:
        wake.wake(mac)

    fetch = fetch or _fetch_url
    post = post or _post_soap

    control_url = find_avtransport_control_url(location, fetch=fetch)

    set_uri_body = (
        f'<u:SetAVTransportURI xmlns:u="{AVTRANSPORT_TYPE}">'
        f'<InstanceID>{INSTANCE_ID}</InstanceID>'
        f'<CurrentURI>{saxutils.escape(stream_url)}</CurrentURI>'
        f'<CurrentURIMetaData>{saxutils.escape(_metadata(title, stream_url, duration))}</CurrentURIMetaData>'
        f'</u:SetAVTransportURI>'
    )
    post(control_url, "SetAVTransportURI", set_uri_body)

    play_body = (
        f'<u:Play xmlns:u="{AVTRANSPORT_TYPE}">'
        f'<InstanceID>{INSTANCE_ID}</InstanceID><Speed>1</Speed></u:Play>'
    )
    # Confirmed tonight: this TV can take close to 20s to answer Play once
    # it is actually pulling from the proxy (it answers only once playback
    # has really started, not on mere receipt) — the default timeout was
    # firing on a call that was slow, not failed, reporting a real success
    # as a failure and skipping everything castActive gates.
    post(control_url, "Play", play_body, timeout=90.0)


def stop(location: str, fetch=None, post=None) -> None:
    """Issue AVTransport Stop. Confirmed tonight as the fix for a renderer
    wedged in a state a Stop from the remote did not cleanly resolve —
    issuing this over SOAP before any teardown is not a precaution copied
    from somewhere else, it is what actually worked."""
    fetch = fetch or _fetch_url
    post = post or _post_soap
    control_url = find_avtransport_control_url(location, fetch=fetch)
    stop_body = (
        f'<u:Stop xmlns:u="{AVTRANSPORT_TYPE}">'
        f'<InstanceID>{INSTANCE_ID}</InstanceID></u:Stop>'
    )
    post(control_url, "Stop", stop_body)


