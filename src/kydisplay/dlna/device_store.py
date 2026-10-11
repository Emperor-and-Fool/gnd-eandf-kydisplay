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

"""Where a renderer was last reached, kept across restarts.

A UDN is the one thing proven stable across this TV's reboots; its port is
not, confirmed to change on every one tonight. What is worth keeping is not
just the latest location but the history of them — the same shape of data
that made tonight's own debugging possible — so a table, not a single
overwritten field, and SQLite over a flat file because that history is worth
being able to ask questions of later.
"""

from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path


def default_path() -> Path:
    data_home = Path(os.environ.get("XDG_DATA_HOME", "~/.local/share")).expanduser()
    return data_home / "kydisplay" / "devices.db"


SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    udn TEXT PRIMARY KEY,
    location TEXT NOT NULL,
    friendly_name TEXT NOT NULL,
    is_lg INTEGER NOT NULL DEFAULT 0,
    first_seen REAL NOT NULL,
    last_verified REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sightings (
    udn TEXT NOT NULL,
    location TEXT NOT NULL,
    seen_at REAL NOT NULL,
    verified INTEGER NOT NULL
);
"""


@dataclass(frozen=True)
class StoredDevice:
    udn: str
    location: str
    friendly_name: str
    is_lg: bool
    first_seen: float
    last_verified: float


class DeviceStore:
    """Persistent record of renderers this machine has reached before."""

    def __init__(self, path: Path | None = None):
        self.path = path or default_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def get(self, udn: str) -> StoredDevice | None:
        row = self._conn.execute(
            "SELECT udn, location, friendly_name, is_lg, first_seen, last_verified "
            "FROM devices WHERE udn = ?", (udn,)
        ).fetchone()
        if row is None:
            return None
        return StoredDevice(row[0], row[1], row[2], bool(row[3]), row[4], row[5])

    def all(self) -> list[StoredDevice]:
        rows = self._conn.execute(
            "SELECT udn, location, friendly_name, is_lg, first_seen, last_verified FROM devices"
        ).fetchall()
        return [StoredDevice(r[0], r[1], r[2], bool(r[3]), r[4], r[5]) for r in rows]

    def record_sighting(self, udn: str, location: str, verified: bool) -> None:
        """A raw or confirmed sighting, kept for history regardless of
        whether it becomes the device's new trusted location."""
        self._conn.execute(
            "INSERT INTO sightings (udn, location, seen_at, verified) VALUES (?, ?, ?, ?)",
            (udn, location, time.time(), int(verified)),
        )
        self._conn.commit()

    def mark_verified(self, udn: str, location: str, friendly_name: str, is_lg: bool) -> None:
        """A location that was just confirmed reachable. Updates the trusted
        record; creates it on first contact."""
        now = time.time()
        existing = self.get(udn)
        first_seen = existing.first_seen if existing else now
        self._conn.execute(
            "INSERT INTO devices (udn, location, friendly_name, is_lg, first_seen, last_verified) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(udn) DO UPDATE SET "
            "location=excluded.location, friendly_name=excluded.friendly_name, "
            "is_lg=excluded.is_lg, last_verified=excluded.last_verified",
            (udn, location, friendly_name, int(is_lg), first_seen, now),
        )
        self._conn.commit()
        self.record_sighting(udn, location, verified=True)

    def close(self) -> None:
        self._conn.close()
