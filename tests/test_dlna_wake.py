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

"""Packet construction only. Whether a sent packet wakes anything is not
testable without a wired device, and is not claimed anywhere here."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kydisplay.dlna.wake import InvalidMacAddress, magic_packet


class MagicPacketTests(unittest.TestCase):
    def test_packet_is_six_0xff_bytes_then_the_mac_sixteen_times(self):
        packet = magic_packet("01:23:45:67:89:ab")
        self.assertEqual(packet[:6], b"\xff" * 6)
        self.assertEqual(len(packet), 6 + 6 * 16)
        mac_bytes = bytes.fromhex("0123456789ab")
        for i in range(16):
            offset = 6 + i * 6
            self.assertEqual(packet[offset:offset + 6], mac_bytes)

    def test_colon_and_hyphen_separators_both_accepted(self):
        self.assertEqual(magic_packet("01:23:45:67:89:ab"), magic_packet("01-23-45-67-89-ab"))

    def test_bare_hex_without_separators_accepted(self):
        self.assertEqual(magic_packet("0123456789ab"), magic_packet("01:23:45:67:89:ab"))

    def test_wrong_length_is_rejected(self):
        with self.assertRaises(InvalidMacAddress):
            magic_packet("01:23:45:67:89")

    def test_non_hex_is_rejected(self):
        with self.assertRaises(InvalidMacAddress):
            magic_packet("zz:23:45:67:89:ab")


if __name__ == "__main__":
    unittest.main()
