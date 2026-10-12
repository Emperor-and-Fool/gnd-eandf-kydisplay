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

Each site that needs a workaround gets it, and only it, by host. An
earlier version sent every site's workaround to every site, on the
theory that an option a site does not need is simply ignored. That is
true of what the option does, and beside the point for what it costs:
``cookiesfrombrowser`` reads the whole of the browser's cookie jar —
every site the person is signed in to, not just the one being cast —
and it did so on every single extraction, including for sites that have
no use for a cookie at all. Scoping it to the one site that cannot work
without it is not an optimisation; it is the difference between reading
a person's browser once for a reason and reading it constantly for none.
"""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass

import yt_dlp

from . import extractor_rumble

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

# What every extraction gets, regardless of site. Nothing here reaches
# outside this process or depends on anything beyond yt-dlp itself.
#
# Odysee/LBRY is deliberately absent from the per-site table below: it
# needs exactly this and nothing more, confirmed by it extracting
# successfully with no workaround at all. Its one requirement is already
# met by FORMAT's "original" term above.
BASE_OPTIONS = {
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "format": FORMAT,
}

# YouTube's "n challenge" blocks extraction unless a JS runtime and the
# remote challenge solver are available. Separate from its cookie
# requirement below, which is a different check with a different answer.
YOUTUBE_OPTIONS = {
    "js_runtimes": {"node": {}},
    "remote_components": ["ejs:github"],
}

# Hosts whose bot-check refuses an anonymous request outright, and so need
# a browser's cookies. Kept apart from the options tables because the
# value cannot be written down here: it depends on which browser is
# playing, which is only known at the moment of casting.
COOKIE_HOSTS = ("youtube.com", "youtu.be")

# Used when the playing browser could not be identified. Firefox because
# it was the hard-coded value before this was worked out at all, so it is
# the one with any history of working — not because it is more likely to
# be right. A machine with no Firefox profile will fail here with a
# yt-dlp error about a missing profile, which is worth knowing when this
# is packaged for machines that are not this one.
FALLBACK_BROWSER = "firefox"

# Host suffixes to the extra options that host needs. Matched against the
# URL's hostname, so a subdomain (m.youtube.com) resolves to the same
# entry as the bare domain. First match wins; a site not listed gets
# BASE_OPTIONS alone, which is the correct answer for every site that
# needs no workaround — Odysee among them.
#
# Rumble is deliberately not here. It does not reach yt-dlp at all: the
# Cloudflare challenge in front of its watch pages is not something a
# yt-dlp option can answer, so it is resolved by its own module instead.
# See :mod:`.extractor_rumble` for what that route is and why.
SITE_OPTIONS: tuple[tuple[tuple[str, ...], dict], ...] = (
    (("youtube.com", "youtu.be"), YOUTUBE_OPTIONS),
)


class ExtractionError(RuntimeError):
    """A page yt-dlp could not resolve to a direct stream."""


@dataclass(frozen=True)
class Stream:
    """What a page resolved to: the URL to fetch, and how long it runs.

    ``duration`` is in seconds, or ``None`` when there is no such answer —
    a live broadcast has no length, and not every site reports one. It is
    carried here rather than discarded because a renderer cannot work it
    out for itself: once a source is remuxed to MPEG-TS, nothing in the
    stream says how long it is, so a television that is not told shows no
    timeline and cannot be scrubbed. Both resolvers already know the
    number; the only reason it used to be lost was that this returned a
    bare string."""

    url: str
    duration: int | None = None


def _host_matches(host: str, suffixes: tuple[str, ...]) -> bool:
    return any(host == suffix or host.endswith("." + suffix) for suffix in suffixes)


def options_for(page_url: str, browser: str | None = None) -> dict:
    """The yt-dlp options this specific URL's site needs: the shared base,
    plus that host's own entry if it has one, plus the cookies of the
    browser actually playing if the host demands cookies at all.

    ``browser`` is yt-dlp's name for it, as :func:`.mpris.browser_for`
    works out from the playing player. Passing nothing falls back to
    :data:`FALLBACK_BROWSER`, which is what this did unconditionally
    before — reading one hard-coded browser's cookies no matter which
    browser was playing, so casting from a second browser worked only by
    the accident of the first being installed and signed in.

    Public, and taking the browser as an argument rather than finding it,
    so what each site is sent can be checked without a network, a session
    bus, or anyone's cookie jar."""
    host = (urllib.parse.urlsplit(page_url).hostname or "").lower()
    options = dict(BASE_OPTIONS)
    for suffixes, extra in SITE_OPTIONS:
        if _host_matches(host, suffixes):
            options.update(extra)
            break
    if _host_matches(host, COOKIE_HOSTS):
        options["cookiesfrombrowser"] = (browser or FALLBACK_BROWSER, None, None, None)
    return options


def extract(page_url: str, browser: str | None = None) -> Stream:
    """The :class:`Stream` a renderer can fetch for what is playing at
    ``page_url``. Raises :class:`ExtractionError` on anything that could
    not be resolved, such as a site yt-dlp does not support or one that
    refused the request.

    ``browser`` is which browser is playing it, in yt-dlp's naming, for
    the sites that need its cookies; see :func:`options_for`.

    Most sites are resolved by yt-dlp, with whatever that host needs added
    — see :func:`options_for` and the per-site tables above for which site
    needs what, and why. A site whose problem is not expressible as a
    yt-dlp option gets its own module and is dispatched to it here, which
    today means Rumble and its Cloudflare challenge."""
    if extractor_rumble.handles(page_url):
        try:
            url, duration = extractor_rumble.resolve(page_url)
            return Stream(url=url, duration=duration)
        except extractor_rumble.RumbleError as error:
            raise ExtractionError(str(error)) from error
        except OSError as error:
            # urllib raises these for a refused or timed-out endpoint;
            # the caller only cares that the page did not resolve.
            raise ExtractionError(f"could not reach Rumble for {page_url}: {error}") from error

    options = options_for(page_url, browser=browser)
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(page_url, download=False)
    except yt_dlp.utils.DownloadError as error:
        raise ExtractionError(str(error)) from error

    url = info.get("url") if info else None
    if not url:
        raise ExtractionError(f"yt-dlp found no playable stream at {page_url}")
    # Absent on a live broadcast, and on any site that does not report one.
    # Rounded because DIDL-Lite counts whole seconds and yt-dlp may answer
    # with a float.
    duration = info.get("duration")
    return Stream(url=url, duration=int(duration) if duration else None)