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

import urllib.request
from enum import Enum

HLS_SIGNATURE = "#EXTM3U"
VOD_END_MARKER = "#EXT-X-ENDLIST"


class SourceKind(Enum):
    DIRECT = "direct"
    HLS_VOD = "hls_vod"
    HLS_LIVE = "hls_live"


def _fetch_text(url: str, max_bytes: int | None = None, timeout: float = 5.0) -> str | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = resp.read(max_bytes) if max_bytes else resp.read()
            return data.decode(errors="replace")
    except Exception:
        return None


def classify(url: str, fetch=None) -> SourceKind:
    """Which :class:`SourceKind` ``url`` actually is, found out the only
    way that is reliable: asking it, not guessing from the URL's own
    spelling — a playlist does not have to end in ``.m3u8``, and
    something that does is not guaranteed to be one."""
    fetch = fetch or _fetch_text
    head = fetch(url, 512)
    if head is None or not head.lstrip().startswith(HLS_SIGNATURE):
        return SourceKind.DIRECT
    playlist = fetch(url, None)
    if playlist is not None and VOD_END_MARKER in playlist:
        return SourceKind.HLS_VOD
    return SourceKind.HLS_LIVE