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

"""Resolve a Rumble page to a stream, around the challenge in front of it.

Rumble's watch pages sit behind a Cloudflare managed challenge. Read off
the refused response directly: ``server: cloudflare``,
``cf-mitigated: challenge``, and a body titled "Just a moment...". yt-dlp
reports that interstitial as a 403, which reads like a refusal but is
really a challenge nobody solved.

Nothing we can send passes it. Seven concrete TLS fingerprints were tried
— chrome-150 and chrome-146, newer than anything a generic "chrome"
resolves to, plus chrome-136, chrome-133, a Firefox profile, a Safari/iOS
profile and an Edge profile — and every one got the same challenge, as did
plain urllib with a browser User-Agent. Handing yt-dlp the browser's own
cookies did work once and then refused on the next three attempts minutes
later, with nothing changed. A path that works only sometimes, only while
a browser is signed in, and only until Cloudflare raises its mitigation
level, is not a path to build casting on.

So this does not go through the watch page at all. Two endpoints Rumble
has to keep open, because other people's websites depend on them, are not
challenged — measured, not assumed, both returning 200 with
``cf-mitigated`` absent entirely, using plain urllib with no cookies and
no impersonation:

  1. the oEmbed API, which any site embedding a Rumble video calls to
     discover what to embed, and which names the embed URL; and
  2. the embed page itself, which has to render inside a third party's
     iframe, and which carries the full media table the player uses.

The media table is the point. It is also where the obvious shortcut is a
trap: the only plain ``.mp4`` on that page is the scrubbing-preview strip
(320x180 at 11 kbps, under a "timeline" key), and a regex looking for an
mp4 finds exactly that and nothing else. What the player actually plays is
under "hls", and that is what this returns — one master playlist listing
every real variant up to 1080p, which :mod:`.source` already classifies and
:mod:`.proxy` already remuxes.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

HOSTS = ("rumble.com",)

OEMBED_API = "https://rumble.com/api/Media/oembed.json"

# A browser User-Agent, because these endpoints are meant for browsers and
# there is no reason to look like something else. It is not load-bearing:
# the unchallenged endpoints answered a default Python agent too. Nothing
# here depends on a fingerprint, which is the whole point of the route.
USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64; rv:133.0) "
              "Gecko/20100101 Firefox/133.0")

TIMEOUT = 20.0

_EMBED_SRC = re.compile(r'src="([^"]+/embed/[^"]+?)"')


class RumbleError(RuntimeError):
    """A Rumble page this module could not resolve to a stream."""


def handles(page_url: str) -> bool:
    """Whether this module is the right one for ``page_url``."""
    host = (urllib.parse.urlsplit(page_url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in HOSTS)


def _fetch(url: str, fetch=None) -> bytes:
    if fetch is not None:
        return fetch(url)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return response.read()


def embed_url(page_url: str, fetch=None) -> str:
    """The embed URL for a watch page, via the oEmbed API.

    The embed id is not derivable from the watch URL: this page's watch id
    is ``v7gjc60`` and its embed id is ``v7ecz8i``. Guessing one from the
    other returns 410, which is how that was established rather than
    assumed."""
    query = urllib.parse.urlencode({"url": page_url})
    raw = _fetch(f"{OEMBED_API}?{query}", fetch=fetch)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise RumbleError(f"Rumble's oEmbed API did not return JSON for {page_url}") from error
    match = _EMBED_SRC.search(payload.get("html") or "")
    if not match:
        raise RumbleError(f"Rumble's oEmbed API named no embed URL for {page_url}")
    return match.group(1)


def _json_object_at(text: str, key: str) -> dict:
    """The JSON object following ``"key":`` in ``text``.

    Scanned brace by brace rather than matched with a pattern, because the
    object is embedded in a script among other JSON and a regex cannot find
    its end. Quoted sections are skipped so a brace inside a string — a URL
    with one, a title with one — does not close the object early."""
    marker = f'"{key}"'
    start = text.find(marker)
    if start == -1:
        raise RumbleError(f"no {marker} media table on the embed page")
    opening = text.find("{", start)
    if opening == -1:
        raise RumbleError(f"{marker} on the embed page is not an object")

    depth = 0
    in_string = False
    escaped = False
    for index in range(opening, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[opening:index + 1])
                except json.JSONDecodeError as error:
                    raise RumbleError(f"{marker} on the embed page is not valid JSON") from error
    raise RumbleError(f"{marker} on the embed page never closes")


def media_table(embed_page_url: str, fetch=None) -> dict:
    """The embed page's own table of what it can play, keyed by container
    ("hls", "tar", "audio", "timeline")."""
    html = _fetch(embed_page_url, fetch=fetch).decode("utf-8", "replace")
    return _json_object_at(html, "ua")


def choose_stream(table: dict) -> str:
    """The one URL worth handing a renderer.

    "hls" is taken and the rest ignored, deliberately. It is a master
    playlist naming every real variant with its bandwidth and resolution,
    so ffmpeg picks the best one itself and the audio arrives with it.

    The other containers are not alternatives to it. "timeline" is the
    scrubbing-preview strip — 320x180 at 11 kbps, and the only plain mp4
    on the page, which makes it exactly what a naive search for one finds.
    "audio" is an audio-only rendition. "tar" holds the real video tiers,
    but each entry there is a base URL the master playlist decorates with
    query parameters (``r_file``, ``r_range``) before it is fetchable, and
    constructing those by hand has not been shown to work — so this raises
    rather than guessing at a URL nobody has verified."""
    hls = table.get("hls")
    if isinstance(hls, dict):
        for entry in hls.values():
            url = entry.get("url") if isinstance(entry, dict) else entry
            if isinstance(url, str) and url:
                return url
    raise RumbleError(
        "the Rumble embed page offered no HLS playlist; only the "
        f"containers {sorted(table)} were present"
    )


def resolve(page_url: str, fetch=None) -> str:
    """The direct stream URL for a Rumble watch page. Raises
    :class:`RumbleError` if any step of the unchallenged route fails."""
    embed = embed_url(page_url, fetch=fetch)
    logger.info("rumble: %s -> embed %s", page_url, embed)
    table = media_table(embed, fetch=fetch)
    stream = choose_stream(table)
    logger.info("rumble: embed %s -> stream %s", embed, stream)
    return stream