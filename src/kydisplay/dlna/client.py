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


# The content type this proxy actually serves, which is also what its
# HTTP responses say.
DLNA_MIME = "video/vnd.dlna.mpeg-tts"

# Whether to admit the stream has been converted.
#
# "1" is the truthful answer: everything leaving this proxy is remuxed
# and some of it re-encoded, so it is not the original bytes. It was sent
# that way first, and the television accepted the stream and played it —
# while still reporting no position, no duration and no way to scrub.
#
# "0" is here because of what that admission is understood to mean.
# Reports going back to 2012 have LG sets treating converted content as
# unseekable on principle, regardless of what else the declaration says:
# the scrubber is disabled because the content is tagged converted, not
# because of anything about the content itself. Universal Media Server
# and Serviio both document living with exactly this.
#
# So the flag is flipped to see whether the refusal is the tag rather
# than the stream. It is not a free lie: a renderer that believes it has
# original bytes may try to seek by byte offset, and this proxy cannot
# answer that honestly — it has no total length and no index, because it
# is producing the stream as it goes. If seeking then appears and
# breaks playback the moment it is used, that is the answer, and the
# truthful "1" should come back.
DLNA_CONVERSION = "0"

# Streaming transfer, background transfer, DLNA 1.5 — the value
# production servers send for a transcoded stream. The remaining bits are
# reserved and stay zero.
DLNA_FLAGS = "01500000000000000000000000000000"


def protocol_info(profile: str | None) -> str:
    """A complete description of what is being offered, or none at all.

    Nothing in between, because in between is what has failed here three
    times: a content type narrowed with nothing to explain it, a profile
    name on its own, and a seek capability advertised on content declared
    unknown. The last is the clearest of the three — it says "I will not
    tell you what this is, but do seek through it by time", and no
    renderer can honour the second half without the first.

    So with a profile in hand all five pieces travel together: the real
    content type, the profile naming it, time-seek, the conversion
    indicator and the flags. Without one, the bare wildcard that has
    always worked, rather than a partial description that never has."""
    if not profile:
        return "http-get:*:*:*"
    return (
        f"http-get:*:{DLNA_MIME}:"
        f"DLNA.ORG_PN={profile};"
        f"DLNA.ORG_OP=10;"
        f"DLNA.ORG_CI={DLNA_CONVERSION};"
        f"DLNA.ORG_FLAGS={DLNA_FLAGS}"
    )


def _duration_text(seconds: int) -> str:
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}.000"


def _metadata(title: str, uri: str, duration: int | None = None,
              profile: str | None = None) -> str:
    """The DIDL-Lite that travels with SetAVTransportURI.

    Written out here rather than through gupnp-av's writer, which cannot
    express what this needs. Handed a fourth protocolInfo field holding
    both an operation and flags, it keeps the operation and silently
    drops the flags; it emits flags at all only when a profile name sits
    beside them. The result read correctly in the source and arrived on
    the wire missing pieces. The element is small enough to write
    honestly instead."""
    attributes = f'protocolInfo="{saxutils.escape(protocol_info(profile), {chr(34): "&quot;"})}"'
    if duration:
        attributes += f' duration="{_duration_text(duration)}"'
    return (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"'
        ' xmlns:dc="http://purl.org/dc/elements/1.1/"'
        ' xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
        '<item id="0" parentID="-1" restricted="1">'
        f'<dc:title>{saxutils.escape(title or uri)}</dc:title>'
        '<upnp:class>object.item.videoItem</upnp:class>'
        f'<res {attributes}>{saxutils.escape(uri)}</res>'
        '</item></DIDL-Lite>'
    )


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
         profile: str | None = None, mac: str | None = None,
         fetch=None, post=None) -> None:
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
        f'<CurrentURIMetaData>{saxutils.escape(_metadata(title, stream_url, duration, profile))}</CurrentURIMetaData>'
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


def transport_info(location: str, fetch=None, post=None) -> dict[str, str]:
    """What the renderer says about its own transport: at least
    ``CurrentTransportState`` (STOPPED, PLAYING, TRANSITIONING…) and
    ``CurrentTransportStatus`` (OK, ERROR_OCCURRED).

    Worth having because the renderer's answer to an action is close to
    useless on its own. This television has returned 501 "Action Failed"
    while playing perfectly, returned 501 while failing, and simply not
    answered at all — three outcomes, one code, no information. Its own
    view of its state is a different question with a real answer.

    Read-only: it asks and changes nothing, so it is safe to call after a
    failure without disturbing whatever the renderer is doing."""
    fetch = fetch or _fetch_url
    post = post or _post_soap
    control_url = find_avtransport_control_url(location, fetch=fetch)
    body = (
        f'<u:GetTransportInfo xmlns:u="{AVTRANSPORT_TYPE}">'
        f'<InstanceID>{INSTANCE_ID}</InstanceID></u:GetTransportInfo>'
    )
    response = post(control_url, "GetTransportInfo", body, timeout=10.0)
    try:
        root = ET.fromstring(response)
    except ET.ParseError:
        return {}
    found = {}
    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1]
        if tag.startswith("Current") and element.text:
            found[tag] = element.text
    return found


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


