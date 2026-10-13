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


# Whether the registered source has to be rebuilt rather than
# repackaged, when the caller already knows. The cast path looks at the
# stream before it tells the renderer anything, so it has the answer
# already; asking again here would be a second round trip to a CDN with
# the television sitting waiting on the other side.
_source_rebuild: bool | None = None

# How long the source runs, when the caller knows. Needed to answer a
# seek completely: a renderer told only where a stream begins knows
# nothing about how much is left, and has no timeline to rebuild from.
# The cast path reads this from the source before telling the renderer
# anything, so it is already known by the time a seek can arrive.
_source_duration: int | None = None


def set_source(url: str, url_path: str = "/stream.ts",
               rebuild: bool | None = None, duration: int | None = None) -> None:
    global _source_url, _source_url_path, _source_rebuild, _source_duration
    _source_url = url
    _source_url_path = url_path
    _source_rebuild = rebuild
    _source_duration = duration


# Every remux currently running, so that stopping the server can stop
# them too.
#
# The handler's own finally already kills one when its request ends, and
# that is enough while the application is running. It is not enough when
# the application quits: the server's threads are daemon threads, which
# the interpreter kills outright at exit without running their finally
# blocks, so the ffmpeg a thread owned is simply let go. Observed
# directly — one still resident, holding its memory, after the
# application that started it had gone.
_running: set[subprocess.Popen] = set()
_running_lock = threading.Lock()


def _remember(proc: subprocess.Popen) -> None:
    with _running_lock:
        _running.add(proc)


def _forget(proc: subprocess.Popen) -> None:
    with _running_lock:
        _running.discard(proc)


def _stop_all_remuxes() -> None:
    with _running_lock:
        procs, _running_copy = list(_running), None
        _running.clear()
    for proc in procs:
        try:
            proc.kill()
            proc.wait(timeout=2)
        except Exception:
            logger.exception("proxy: could not stop ffmpeg pid=%s", proc.pid)
    if procs:
        logger.info("proxy: stopped %d remux process(es)", len(procs))


# What to encode with when a stream has to be rebuilt rather than
# repackaged.
#
# This is the combination production DLNA servers settled on for these
# receivers — Universal Media Server, Serviio, Plex and Jellyfin all
# re-encode rather than trust an LG with an arbitrary source stream, and
# all of them aim at the same target. Two properties matter and both are
# properties of baseline:
#
#   no B-frames, so no frame waits for one that comes after it
#   no CABAC, which the same decoders are reported to struggle with
#
# "-profile:v baseline" gives both by definition; "-bf 0" states the
# first outright rather than relying on the profile to imply it.
#
# Not invented here, and found independently twice: the Miracast engine
# in this same project sets b-frames to 0 on its own encoder (patch 0045)
# with a comment saying a frame may not wait for one that comes after it.
# Same receiver family, same constraint, different route.
#
# Software rather than the graphics chip, deliberately. A VAAPI pipeline
# was tried first and did not produce output, and this is the line with
# production mileage behind it; ultrafast and zerolatency keep the cost
# moderate. Encoding on the processor's own graphics is worth revisiting
# once this is known to fix anything, not while it is still the thing
# being tested.
#
# Audio is never touched: it is already AAC, which this receiver plays,
# and re-encoding it would cost quality for nothing.
REBUILD_ARGS = [
    "-c:v", "libx264",
    "-preset", "ultrafast",
    "-tune", "zerolatency",
    "-bf", "0",
    "-profile:v", "baseline",
    "-c:a", "copy",
]


# What gives the stream a timeline of its own.
#
# A duration in the DIDL-Lite is a label on the item; it is not a
# timeline, and this television proved the difference. Asked while
# genuinely playing — the proxy pushing three megabytes every three
# seconds, the connection live — it reported LG_TRANSITIONING, RelTime
# frozen at 00:00:00 and a duration of zero. It was not confused about
# what it had. It had nothing in the bytes to count from.
#
# Two things were missing, and we are the ones who make these bytes, so
# both are ours to put there.
#
# The stream did not begin at zero. Timestamps were inherited from the
# source and the first frame arrived at 1.4 seconds, an origin that means
# something to the file it came from and nothing to a renderer. The mux
# delay and preload are what shift that, and make_zero moves the whole
# thing to start where a renderer would look for a start.
#
# The packets also carried no time of their own, and the timestamped
# variant of MPEG-TS was tried for it: 192 bytes with four bytes of
# arrival time on each, rather than a bare 188. It was emitted correctly
# — sync bytes measured at offsets 4, 196 and 388 — and it is not here,
# because the television answered the question twice over:
#
#   RelTime stayed at 00:00:00 across three polls while playing, so the
#   per-packet clock bought nothing at all
#   the picture played and the sound did not
#
# The sound was in the stream — AAC LC, stream 1, confirmed in the output
# — so nothing was dropped on our side. The television simply does not
# decode AAC inside that container, which is not surprising: 192-byte TS
# is the Blu-ray shape, where audio is expected to be AC3, DTS or LPCM.
# Trading the sound for a clock the renderer ignores is a bad trade, so
# only the start-at-zero half remains.
#
# Proxy-side only. protocolInfo is untouched, which is the field that
# broke casting three times and is best left alone.
TIMELINE_ARGS = [
    "-muxdelay", "0",
    "-muxpreload", "0",
    "-avoid_negative_ts", "make_zero",
]


def seek_seconds(headers) -> float | None:
    """Where a renderer asked us to start, in seconds, or ``None`` when it
    asked for the thing from the beginning.

    This television sends both a byte offset and a time, together:

        Range: bytes=11550720-
        TimeSeekRange.dlna.org: npt=0:09:25.662-

    The time is the one worth reading. It is exact and it means what it
    says, while the byte offset is the renderer's own guess at where that
    moment lives — a guess it has no way to make well, since nothing here
    ever told it how large the stream is or at what rate it runs.

    Plain ``Range: bytes=0-`` is not a seek. It is how this renderer opens
    anything at all, and it arrives on every ordinary fetch, so treating
    it as one would restart the stream constantly for no reason. Only a
    request carrying a time range is a request to move."""
    raw = headers.get("TimeSeekRange.dlna.org")
    if not raw:
        return None
    _, _, value = raw.partition("npt=")
    start, _, _end = value.partition("-")
    start = start.strip()
    if not start:
        return None
    # Either plain seconds, or hours:minutes:seconds with a fraction.
    try:
        if ":" in start:
            parts = [float(piece) for piece in start.split(":")]
            while len(parts) < 3:
                parts.insert(0, 0.0)
            hours, minutes, secs = parts[-3], parts[-2], parts[-1]
            return hours * 3600 + minutes * 60 + secs
        return float(start)
    except ValueError:
        return None


def _npt(seconds: float) -> str:
    hours, rest = divmod(float(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    return f"{int(hours)}:{int(minutes):02d}:{secs:06.3f}"


def ffmpeg_command(url: str, kind: sourcelib.SourceKind,
                   rebuild: bool = False, start: float | None = None) -> list[str]:
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
    # Before the input, not after: an input seek asks the demuxer to move
    # to the nearest keyframe and start decoding there, which is quick and
    # is what a renderer jumping to a point in a film wants. After the
    # input it would decode everything up to that point and throw it away.
    if start:
        command += ["-ss", f"{start:.3f}"]
    command += ["-user_agent", USER_AGENT, "-i", url]
    if rebuild:
        # Rebuilt rather than repackaged. No bitstream filter here: the
        # encoder emits what the muxer wants directly, and the filter
        # exists only to re-frame bytes that are being passed through
        # untouched.
        command += REBUILD_ARGS
    else:
        command += ["-c", "copy", "-bsf:v", "h264_mp4toannexb"]
    command += ["-mpegts_flags", "system_b", *TIMELINE_ARGS, "-f", "mpegts", "pipe:1"]
    return command


# Headers a renderer uses to ask for part of something rather than all of
# it. Logged whenever they appear, because what this TV actually asks for
# is the only way to know what it would need to be given: it draws a
# position bar from the duration in the DIDL-Lite but will not scrub, and
# whether that is because it never asks, or asks in a way nothing here
# answers, is not something to guess at.
SEEK_HEADERS = ("Range", "TimeSeekRange.dlna.org", "PlaySpeed.dlna.org",
                "getcontentFeatures.dlna.org", "transferMode.dlna.org")


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

        # Whether this has to be rebuilt instead of repackaged.
        #
        # Only asked of a direct source. Anything arriving as HLS is
        # already MPEG-TS and is only being re-wrapped, which has played
        # on this receiver every time — including a stream whose frames
        # do need reordering. A direct file is converted rather than
        # re-wrapped, and the two that have never played are both direct
        # files needing reordering, so that is the case worth paying to
        # avoid.
        #
        # A source that cannot be asked is left alone rather than rebuilt
        # on suspicion: repackaging is what works for almost everything,
        # and spending the graphics chip on a guess would make the common
        # case worse to protect the rare one.
        rebuild = False
        if _source_rebuild is not None:
            rebuild = _source_rebuild
        elif kind is sourcelib.SourceKind.DIRECT:
            # Nothing told us, so ask — this is the path a request that
            # set no source of its own arrives by.
            frames = sourcelib.has_b_frames(_source_url, kind)
            rebuild = bool(frames)
            logger.info("proxy: direct source has_b_frames=%s -> %s",
                        frames, "rebuilding" if rebuild else "repackaging")
        # Where the renderer asked us to start. Only a time range counts
        # as a request to move — see seek_seconds — so an ordinary fetch
        # behaves exactly as it did before any of this existed.
        start = seek_seconds(self.headers)
        logger.info("proxy: serving source kind=%s rebuild=%s start=%s url=%s",
                    kind, rebuild, start, _source_url)

        if start:
            # Answered as a partial response, because that is what was
            # asked for, and with the time echoed back: a renderer that
            # asked to begin at a point needs telling that is where this
            # begins, or it has no reason to believe anything moved.
            #
            # The byte range it also sent is not answered honestly here,
            # and cannot be: this stream is produced as it goes, so there
            # is no total length and no offset that means anything. What
            # is sent back is the time, which is exact, and which is the
            # half of the request this can actually satisfy.
            # The whole range, not just where it starts. A renderer told
            # only a beginning knows where to resume from and nothing
            # about how much is left, so it has no timeline to rebuild
            # and sits where it was dropped rather than running on —
            # observed exactly that, with a stop and start then playing
            # happily from the chosen point.
            #
            # The end and the total are the same number here: this is a
            # stream that runs to the end of the source once it starts,
            # and there is no later point it stops short of.
            if _source_duration:
                span = f"npt={_npt(start)}-{_npt(_source_duration)}/{_npt(_source_duration)}"
            else:
                span = f"npt={_npt(start)}-"
            self.send_response(206)
            self.send_header("TimeSeekRange.dlna.org", span)
        else:
            self.send_response(200)
        self.send_header("Content-Type", "video/vnd.dlna.mpeg-tts")
        self.end_headers()
        if _stream_started_trigger is not None:
            _stream_started_trigger()
        proc = subprocess.Popen(
            ffmpeg_command(_source_url, kind, rebuild=rebuild, start=start),
            stdout=subprocess.PIPE, stderr=sys.stderr,
        )
        _remember(proc)
        try:
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            _forget(proc)
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
    """Stop listening, and stop what is still being served.

    Both halves matter. Closing the socket ends nothing already in
    flight, and the threads serving those requests are daemon threads
    that the interpreter kills at exit without running their cleanup — so
    without this, quitting mid-cast leaves an ffmpeg behind with no
    parent and no reason to stop."""
    global _server, _thread
    _stop_all_remuxes()
    if _server is not None:
        _server.shutdown()
        _server.server_close()
        _server = None
        _thread = None