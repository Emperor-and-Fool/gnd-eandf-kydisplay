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

"""Binds the DLNA proxy's listening socket, and remuxes a real source to
MPEG-TS for it.

The remux core recovered from an earlier session's prototype, proven
against the real TV there for a direct MP4 URL before being lost (never
committed — see the memory and mission docs from that night). ffmpeg's
own input demuxer handles a direct file, an HLS VOD playlist, and a
live HLS playlist identically — the same command remuxes any of the
three to MPEG-TS, confirmed in the pipeline-findings mission doc. What
:mod:`.source` classifies is not which ffmpeg command to run, but
whether ffmpeg exiting on its own is normal (direct, HLS VOD both have
a real end) or a fault worth reporting (HLS live only ever ends on an
explicit Stop).
"""

from __future__ import annotations

import http.server
import logging
import socket
import subprocess
import sys
import threading
import urllib.parse
from pathlib import Path

from . import firewall
from . import source as sourcelib

logger = logging.getLogger(__name__)

# Some CDNs refuse a request with no recognisable browser user agent.
# Confirmed necessary in the recovered prototype; kept exactly as it was
# proven there, not a guess at a value that might work.
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36")

# Real stream-serving does not exist yet; this is a stand-in for proving
# the whole path end to end — SOAP, TV-initiated fetch, actual decoded
# playback — with one real file, not a 404 that cannot tell "reached and
# rejected" apart from "never reached at all".
_trial_url_path: str | None = None
_trial_file: Path | None = None

# Lets an external request fire a real dlnaCast on the already-running
# app, the same call a real cast button will make later — this server
# is already running inside that process, so it is the trigger channel
# rather than building a separate one. Runs in this server's own
# thread; the callback (backend.py's signal) is what makes landing on
# the Qt/GUI thread safe, not anything here.
_cast_trigger = None


def set_cast_trigger(callback) -> None:
    global _cast_trigger
    _cast_trigger = callback


# The TV actually fetching the stream, confirmed tonight as a more
# trustworthy "did the cast really start" signal than the renderer's own
# SOAP response to Play — that response has been seen both timing out
# and returning a genuine SOAP fault while the TV played anyway. Runs in
# this server's own thread, same cross-thread-safety story as
# _cast_trigger above.
_stream_started_trigger = None


def set_stream_started_trigger(callback) -> None:
    global _stream_started_trigger
    _stream_started_trigger = callback


def set_trial_file(path: Path | str, url_path: str = "/trial") -> None:
    global _trial_url_path, _trial_file
    _trial_url_path = url_path
    _trial_file = Path(path)


# The real source: a URL ffmpeg remuxes to MPEG-TS on each request, not a
# file read from disk. Classified lazily, inside the request itself (the
# HTTP server's own thread), not here — classifying means fetching the
# URL, and set_source is called from the GUI thread.
_source_url: str | None = None
_source_url_path: str = "/stream.ts"


def set_source(url: str, url_path: str = "/stream.ts") -> None:
    global _source_url, _source_url_path
    _source_url = url
    _source_url_path = url_path


def ffmpeg_command(url: str, kind: sourcelib.SourceKind) -> list[str]:
    """The remux command for one source. A plain function taking the kind
    rather than reading it, so what each kind is sent can be checked
    without a network, a renderer, or a running server.

    Only the HLS kinds get ``-extension_picky 0``, and that distinction is
    not a tidiness choice — it is the whole point:

    ffmpeg's HLS demuxer decides whether a segment may be opened by the
    extension in its URL. Rumble serves segments from a ``.tar`` with the
    real name in the query string (``.../x.rec.tar?r_file=media-0.ts``),
    so the check sees "tar", refuses every variant, and ffmpeg produces no
    stream at all. The flag turns that check off.

    But the flag belongs to the HLS demuxer, so passing it to a source
    that is not HLS makes ffmpeg reject the input outright —
    "Option extension_picky not found", no output, and a renderer that
    reports the file as unplayable. That is not hypothetical: it was
    shipped, and it broke YouTube VOD and Odysee, both of which resolve to
    a progressive MP4.

    So this cannot be decided per site either, which is the other half of
    the lesson. YouTube is both: a live stream resolves to HLS, an
    ordinary video to a direct MP4. Only the kind of the source actually
    in hand can answer it.

    On the flag being harmless where it is used: a live YouTube stream was
    measured with and without it and produced byte-identical output, since
    its segments are ordinary ``.ts``. Rumble is the case that needs it.
    """
    command = ["ffmpeg", "-nostdin", "-loglevel", "warning"]
    if kind in (sourcelib.SourceKind.HLS_VOD, sourcelib.SourceKind.HLS_LIVE):
        command += ["-extension_picky", "0"]
    command += [
        "-user_agent", USER_AGENT, "-i", url,
        "-c", "copy", "-bsf:v", "h264_mp4toannexb",
        "-mpegts_flags", "system_b", "-f", "mpegts", "pipe:1",
    ]
    return command


# Headers a renderer uses to ask for part of something rather than all of
# it. Logged whenever they appear, because what this TV actually asks for
# is the only way to know what it would need to be given: it draws a
# position bar from the duration in the DIDL-Lite but will not scrub, and
# whether that is because it never asks, or asks in a way nothing here
# answers, is not something to guess at.
SEEK_HEADERS = ("Range", "TimeSeekRange.dlna.org", "PlaySpeed.dlna.org",
                "getcontentFeatures.dlna.org", "transferMode.dlna.org")


# How long a renderer may go without answering the kernel before its
# connection is called dead: idle a minute, then six probes ten seconds
# apart, so roughly two minutes of genuine silence.
#
# Keepalive rather than a socket timeout, and the difference matters.
# Serving a stream spends most of its time blocked in write, because a
# television that has buffered ahead stops reading until it needs more —
# and one paused by the person watching stops reading indefinitely. A
# timeout cannot tell that apart from an absent television and would end
# a perfectly good cast the moment somebody paused it. Keepalive asks:
# a set that is merely not reading still answers, a set that has gone
# does not, and only then does the write fail.
#
# Which is what was missing. A renderer that disappears without closing
# cleanly leaves the write blocked on a half-open socket forever, so the
# loop never ends, so the finally that kills ffmpeg never runs. Observed
# directly: a thirty-minute-old ffmpeg still resident, holding its memory,
# for a cast whose connection was long gone — one leaked per abandoned
# cast, in an application meant to sit in a tray for days.
KEEPALIVE_IDLE = 60
KEEPALIVE_INTERVAL = 10
KEEPALIVE_PROBES = 6


def _enable_keepalive(sock) -> None:
    """Ask the kernel to notice a renderer that has gone away. Best
    effort: the per-option constants are Linux names, and a platform
    without them simply keeps the behaviour it had."""
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except OSError:
        return
    for option, value in (("TCP_KEEPIDLE", KEEPALIVE_IDLE),
                          ("TCP_KEEPINTVL", KEEPALIVE_INTERVAL),
                          ("TCP_KEEPCNT", KEEPALIVE_PROBES)):
        number = getattr(socket, option, None)
        if number is None:
            continue
        try:
            sock.setsockopt(socket.IPPROTO_TCP, number, value)
        except OSError:
            logger.debug("proxy: could not set %s", option)


class _TrialHandler(http.server.BaseHTTPRequestHandler):
    """Serves the one registered trial file at its one registered path;
    404 for everything else, same as before this existed."""

    def do_HEAD(self):
        if _trial_file is not None and self.path == _trial_url_path:
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(_trial_file.stat().st_size))
            self.end_headers()
            return
        if _source_url is not None and self.path == _source_url_path:
            self._log_seek_headers("HEAD")
            # No ffmpeg spawn for HEAD — a remux has no fixed length to
            # report in the first place, and spawning one just to answer
            # a presence check was the exact bug the trial file's own
            # HEAD handler was added to fix, applied here too.
            self.send_response(200)
            self.send_header("Content-Type", "video/vnd.dlna.mpeg-tts")
            self.end_headers()
            return
        self.send_response(404)
        self.end_headers()

    def do_GET(self):
        if _trial_file is not None and self.path == _trial_url_path:
            data = _trial_file.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if _source_url is not None and self.path == _source_url_path:
            self._serve_source()
            return
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/__trigger-cast" and _cast_trigger is not None:
            query = urllib.parse.parse_qs(parsed.query)
            location = query.get("location", [""])[0]
            url = query.get("url", [""])[0]
            title = query.get("title", [""])[0]
            logger.info("proxy: /__trigger-cast location=%s url=%s", location, url)
            _cast_trigger(location, url, title)
            self.send_response(204)
            self.end_headers()
            return
        self.send_response(404)
        self.end_headers()

    def setup(self):
        super().setup()
        _enable_keepalive(self.connection)

    def _log_seek_headers(self, method: str):
        asked = {name: self.headers[name] for name in SEEK_HEADERS if self.headers.get(name)}
        if asked:
            logger.info("proxy: %s %s asked for %s", method, self.path, asked)
        else:
            logger.info("proxy: %s %s sent no seek headers (all of: %s)",
                        method, self.path, ", ".join(SEEK_HEADERS))

    def _serve_source(self):
        self._log_seek_headers("GET")
        kind = sourcelib.classify(_source_url)
        logger.info("proxy: serving source kind=%s url=%s", kind, _source_url)
        self.send_response(200)
        self.send_header("Content-Type", "video/vnd.dlna.mpeg-tts")
        self.end_headers()
        if _stream_started_trigger is not None:
            _stream_started_trigger()
        proc = subprocess.Popen(
            ffmpeg_command(_source_url, kind),
            stdout=subprocess.PIPE, stderr=sys.stderr,
        )
        try:
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except OSError as error:
            # Broader than the BrokenPipeError and ConnectionResetError a
            # cleanly-closed renderer raises, because the one this needed
            # to catch is neither: a set that vanished without closing is
            # noticed by the kernel's keepalive probes and surfaces as
            # ETIMEDOUT. Caught here only so the finally below runs; what
            # went wrong is the same either way, a renderer that stopped
            # listening.
            logger.info("proxy: renderer stopped listening (%s)", error)
        finally:
            proc.kill()
            proc.wait()
        # A direct source or a finite HLS VOD playlist ending is ffmpeg
        # completing its job; the same thing happening on a live playlist
        # means the source dropped unexpectedly, not that playback
        # finished — worth a log line distinguishing the two, nothing
        # more yet (no retry/recovery built here).
        if kind == sourcelib.SourceKind.HLS_LIVE:
            logger.warning("proxy: ffmpeg for a live source ended (code=%s) — "
                           "not a normal completion for live content", proc.returncode)

    def log_message(self, format, *args):
        logger.info("proxy: %s - %s", self.address_string(), format % args)


_server: http.server.ThreadingHTTPServer | None = None
_thread: threading.Thread | None = None


def start() -> bool:
    """Bind and listen on :data:`firewall.DLNA_PORT`, in a background
    thread. Idempotent — calling this again while already running does
    nothing. Returns whether a listening socket exists afterward."""
    global _server, _thread
    if _server is not None:
        return True
    try:
        _server = http.server.ThreadingHTTPServer(("0.0.0.0", firewall.DLNA_PORT), _TrialHandler)
    except OSError:
        logger.exception("proxy.start: could not bind port %s", firewall.DLNA_PORT)
        _server = None
        return False
    _thread = threading.Thread(target=_server.serve_forever, daemon=True)
    _thread.start()
    logger.info("proxy.start: listening on 0.0.0.0:%s", firewall.DLNA_PORT)
    return True


def stop() -> None:
    global _server, _thread
    if _server is not None:
        _server.shutdown()
        _server.server_close()
        _server = None
        _thread = None