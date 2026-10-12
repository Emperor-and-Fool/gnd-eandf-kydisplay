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

"""Turn a page a browser is playing into a URL a renderer can fetch.

A renderer is a plain HTTP client, not a browser: it cannot run the page's
own player, so what is pushed has to be one file with the picture and the
sound already together. yt-dlp's own default answer to "best quality" is
often two separate files, the picture from one CDN URL and the sound from
another, meant for a downloader to join afterwards. Nothing here can join
them, so the format asked for is restricted to the ones that are already
one file.
"""

from __future__ import annotations

import yt_dlp

# A single file with both a picture and sound codec, which is what a
# renderer fetching one URL needs.
#
# "original" is tried first because on LBRY/Odysee it is the plain progressive
# file the upload actually is, served from a CDN that needs no session state
# a renderer could never provide; the codec filter that follows picks it
# second-best on sites with no such format, since yt-dlp's own "best" ranks a
# codec-tagged HLS variant above an opaque passthrough format whose codec it
# cannot report, even when the HLS variant is the one a renderer struggles
# with. Falling back to "best" rather than failing when nothing qualifies: a
# site with only separate streams would otherwise extract nothing at all,
# where offering the better of two imperfect options at least gives a
# receiver something to try. A site with no format literally named "original"
# skips that term and falls through unaffected.
FORMAT = "original/best[acodec!=none][vcodec!=none]/best"


class ExtractionError(RuntimeError):
    """A page yt-dlp could not resolve to a direct stream."""


def extract(page_url: str) -> str:
    """The direct URL a renderer can fetch for what is playing at
    ``page_url``. Raises :class:`ExtractionError` on anything yt-dlp itself
    could not resolve, such as a site it does not support or one that
    refused the request.

    YouTube specifically needs two more things, confirmed directly
    tonight: the browser's own cookies (``cookiesfrombrowser``, without
    which YouTube's bot-check refuses the request outright), and a JS
    runtime plus its remote challenge-solver component (``js_runtimes``,
    ``remote_components``), without which YouTube's own "n challenge"
    blocks extraction even past the bot-check.

    Rumble needs something else again, confirmed directly tonight:
    its very first page fetch returns a plain 403 regardless of headers
    or cookies — a TLS/browser-fingerprint check, not a header check,
    which only ``impersonate`` (via ``curl_cffi``) can pass, since
    plain ``urllib``/``requests`` cannot fake a real browser's TLS
    handshake. Requires ``curl_cffi`` to actually be installed; this
    project now depends on it for that reason.

    None of this is needed by every site extractor.py was written for
    — Odysee needed neither YouTube's nor Rumble's workaround — but all
    of it is harmless to pass unconditionally; a site that does not
    need an option ignores it."""
    from yt_dlp.networking.impersonate import ImpersonateTarget

    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": FORMAT,
        "cookiesfrombrowser": ("firefox", None, None, None),
        "js_runtimes": {"node": {}},
        "remote_components": ["ejs:github"],
        "impersonate": ImpersonateTarget.from_str("chrome"),
    }
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(page_url, download=False)
    except yt_dlp.utils.DownloadError as error:
        raise ExtractionError(str(error)) from error

    url = info.get("url") if info else None
    if not url:
        raise ExtractionError(f"yt-dlp found no playable stream at {page_url}")
    return url
