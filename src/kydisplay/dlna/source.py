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

"""What kind of thing a resolved stream URL actually is.

The ffmpeg command that remuxes a source to MPEG-TS is identical whether
the source is a direct video file, an HLS VOD playlist, or a live HLS
playlist — ffmpeg's own input demuxer handles all three transparently.
What differs is everything *around* that command: whether the proxy can
ever treat ffmpeg exiting as "done, normal" (direct, HLS VOD) or must
treat it as a fault to report (HLS live, which only ever ends on an
explicit Stop). That distinction is what this module exists to answer;
it does not touch ffmpeg or HTTP serving at all.

Detection, per the documented approach: fetch the first bytes of the
URL. ``#EXTM3U`` means HLS; anything else is a direct source. For HLS,
fetch the full playlist and look for ``#EXT-X-ENDLIST`` — present means
VOD (a finite, known end), absent means live (a sliding window with no
end)."""

from __future__ import annotations

import subprocess
import urllib.parse
import urllib.request
from enum import Enum

HLS_SIGNATURE = "#EXTM3U"
VOD_END_MARKER = "#EXT-X-ENDLIST"

# A playlist that lists other playlists rather than segments. Its presence
# is why the end marker alone is not enough to answer VOD-or-live: that
# marker lives in the media playlist, and a master playlist never carries
# one no matter what it points at. Checking only for the marker therefore
# called every master playlist live — including a plainly finished Rumble
# recording, observed directly.
MASTER_MARKER = "#EXT-X-STREAM-INF"

# Sent on every request here, because a CDN that will not serve
# "Python-urllib/3.x" is common enough to have bitten this directly:
# Rumble's playlist refused the default agent, _fetch_text returned None,
# and a real HLS playlist was classified DIRECT — silently, since a
# failed fetch and a genuinely-not-HLS source were indistinguishable.
# The same string proxy.py sends ffmpeg, for the same reason.
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36")


class SourceKind(Enum):
    DIRECT = "direct"
    HLS_VOD = "hls_vod"
    HLS_LIVE = "hls_live"


def has_b_frames(url: str, timeout: float = 25.0) -> int | None:
    """How many frames a decoder must hold back to play this in order, or
    ``None`` if it could not be asked.

    Zero means every frame stands on its own. Anything above it means the
    stream is sent out of display order and has to be reordered, which is
    the thing this television has been seen unable to start on: two files
    from one uploader, both ``has_b_frames=2``, both accepted and then
    stuck part-way into starting, while everything that plays is either
    zero or arrives already in MPEG-TS.

    Asked of the source rather than guessed from the profile name, because
    the name does not answer it: one file that plays is Main profile with
    no reordering at all, and one that fails is Main profile with it. The
    profile says what is permitted, this says what is actually there."""
    try:
        done = subprocess.run(
            ["ffprobe", "-v", "error", "-user_agent", USER_AGENT,
             "-select_streams", "v:0",
             "-show_entries", "stream=has_b_frames",
             "-of", "default=noprint_wrappers=1:nokey=1", url],
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    first = done.stdout.strip().splitlines()
    try:
        return int(first[0])
    except (IndexError, ValueError):
        return None


def _fetch_text(url: str, max_bytes: int | None = None, timeout: float = 5.0) -> str | None:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            data = resp.read(max_bytes) if max_bytes else resp.read()
            return data.decode(errors="replace")
    except Exception:
        return None


def _first_variant(playlist: str, base_url: str) -> str | None:
    """The URL of the first media playlist a master playlist lists, made
    absolute. A variant's URL is the first non-comment line after its
    ``#EXT-X-STREAM-INF`` tag, and may be written relative to the master."""
    lines = playlist.splitlines()
    for index, line in enumerate(lines):
        if not line.startswith(MASTER_MARKER):
            continue
        for candidate in lines[index + 1:]:
            candidate = candidate.strip()
            if candidate and not candidate.startswith("#"):
                return urllib.parse.urljoin(base_url, candidate)
    return None


def classify(url: str, fetch=None) -> SourceKind:
    """Which :class:`SourceKind` ``url`` actually is, found out the only
    way that is reliable: asking it, not guessing from the URL's own
    spelling — a playlist does not have to end in ``.m3u8``, and
    something that does is not guaranteed to be one.

    A master playlist is followed one step down to its first variant
    before the VOD-or-live question is asked, since only the media
    playlist it points at can answer that one."""
    fetch = fetch or _fetch_text
    head = fetch(url, 512)
    if head is None or not head.lstrip().startswith(HLS_SIGNATURE):
        return SourceKind.DIRECT

    playlist = fetch(url, None)
    if playlist is None:
        # Known to be HLS from the first bytes, but unreadable in full.
        # Live is the safer of the two to assume: it is the one the proxy
        # treats as "must not end on its own", so a wrong guess here
        # produces a stray log line rather than a cast that quietly stops.
        return SourceKind.HLS_LIVE

    if MASTER_MARKER in playlist:
        variant = _first_variant(playlist, url)
        media = fetch(variant, None) if variant else None
        if media is not None:
            playlist = media

    return SourceKind.HLS_VOD if VOD_END_MARKER in playlist else SourceKind.HLS_LIVE