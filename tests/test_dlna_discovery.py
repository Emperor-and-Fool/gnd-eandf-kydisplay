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

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kydisplay.dlna import discovery_lg
from kydisplay.dlna.device_store import DeviceStore
from kydisplay.dlna.discovery import DmrDevice, DmrDiscovery, _parse_description

LG_RENDERER_XML = """<?xml version="1.0" encoding="UTF-8"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <device>
    <deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>
    <friendlyName>[LG] webOS TV UN74006LB</friendlyName>
    <manufacturer>LG Electronics.</manufacturer>
    <UDN>uuid:aa355f7e-06d0-d5d9-ee6a-f85ef0ce276c</UDN>
  </device>
</root>"""

OTHER_RENDERER_XML = """<?xml version="1.0" encoding="UTF-8"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <device>
    <deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>
    <friendlyName>Some Other Renderer</friendlyName>
    <manufacturer>Acme Corp</manufacturer>
    <UDN>uuid:11111111-2222-3333-4444-555555555555</UDN>
  </device>
</root>"""

NOT_A_RENDERER_XML = """<?xml version="1.0" encoding="UTF-8"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <device>
    <deviceType>urn:schemas-upnp-org:device:Basic:1</deviceType>
    <friendlyName>Some Basic Device</friendlyName>
    <UDN>uuid:99999999-8888-7777-6666-555555555555</UDN>
  </device>
</root>"""


class DeviceStoreTests(unittest.TestCase):
    """Persistence across restarts, which is the entire point of keeping a
    cache at all — a store that forgets on process exit is just a dict."""

    def make(self):
        path = Path(tempfile.mktemp())
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        return DeviceStore(path)

    def test_unknown_device_is_none(self):
        store = self.make()
        self.assertIsNone(store.get("uuid:unknown"))

    def test_verified_device_is_retrievable(self):
        store = self.make()
        store.mark_verified("uuid:a", "http://1.2.3.4:1485/", "Test TV", True)
        found = store.get("uuid:a")
        self.assertEqual(found.location, "http://1.2.3.4:1485/")
        self.assertEqual(found.friendly_name, "Test TV")
        self.assertTrue(found.is_lg)

    def test_reverifying_updates_location_not_first_seen(self):
        store = self.make()
        store.mark_verified("uuid:a", "http://1.2.3.4:1111/", "Test TV", True)
        first_seen = store.get("uuid:a").first_seen
        store.mark_verified("uuid:a", "http://1.2.3.4:2222/", "Test TV", True)
        updated = store.get("uuid:a")
        self.assertEqual(updated.location, "http://1.2.3.4:2222/")
        self.assertEqual(updated.first_seen, first_seen, "first_seen must not move on a later sighting")

    def test_closing_and_reopening_the_same_path_keeps_the_record(self):
        path = Path(tempfile.mktemp())
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        store = DeviceStore(path)
        store.mark_verified("uuid:a", "http://1.2.3.4:1485/", "Test TV", True)
        store.close()

        reopened = DeviceStore(path)
        self.assertIsNotNone(reopened.get("uuid:a"))

    def test_raw_sighting_does_not_create_a_trusted_device(self):
        store = self.make()
        store.record_sighting("uuid:a", "http://1.2.3.4:1485/", verified=False)
        self.assertIsNone(store.get("uuid:a"), "an unverified sighting is not a trusted device yet")


class LgIdentificationTests(unittest.TestCase):
    def test_lg_description_is_recognised(self):
        self.assertTrue(discovery_lg.is_lg_description(LG_RENDERER_XML))

    def test_other_manufacturer_is_not_recognised_as_lg(self):
        self.assertFalse(discovery_lg.is_lg_description(OTHER_RENDERER_XML))


class ParseDescriptionTests(unittest.TestCase):
    def test_parses_device_type_name_and_udn(self):
        parsed = _parse_description(LG_RENDERER_XML)
        self.assertEqual(parsed["deviceType"], "urn:schemas-upnp-org:device:MediaRenderer:1")
        self.assertEqual(parsed["friendlyName"], "[LG] webOS TV UN74006LB")
        self.assertEqual(parsed["udn"], "uuid:aa355f7e-06d0-d5d9-ee6a-f85ef0ce276c")

    def test_malformed_xml_is_none_not_an_exception(self):
        self.assertIsNone(_parse_description("not xml at all"))

    def test_description_without_a_udn_is_none(self):
        no_udn = LG_RENDERER_XML.replace(
            "<UDN>uuid:aa355f7e-06d0-d5d9-ee6a-f85ef0ce276c</UDN>", "")
        self.assertIsNone(_parse_description(no_udn))


class DmrDiscoveryVerifyTests(unittest.TestCase):
    """verify() with an injected fetch, so these never touch a real network
    or a real renderer."""

    def make(self, responses):
        store = DeviceStore(Path(tempfile.mktemp()))
        self.addCleanup(store.close)
        discovery = DmrDiscovery.__new__(DmrDiscovery)
        discovery._store = store
        discovery._fetch = lambda location, timeout=3.0: responses.get(location)
        return discovery, store

    def test_verify_succeeds_for_a_real_media_renderer(self):
        location = "http://192.168.2.60:1485/"
        discovery, store = self.make({location: LG_RENDERER_XML})
        device = discovery.verify("uuid:aa355f7e-06d0-d5d9-ee6a-f85ef0ce276c", location)
        self.assertEqual(device, DmrDevice(
            udn="uuid:aa355f7e-06d0-d5d9-ee6a-f85ef0ce276c",
            name="[LG] webOS TV UN74006LB",
            location=location,
            is_lg=True,
        ))

    def test_verify_marks_the_store_so_a_later_lookup_is_cache_only(self):
        location = "http://192.168.2.60:1485/"
        discovery, store = self.make({location: LG_RENDERER_XML})
        discovery.verify("uuid:aa355f7e-06d0-d5d9-ee6a-f85ef0ce276c", location)
        self.assertIsNotNone(store.get("uuid:aa355f7e-06d0-d5d9-ee6a-f85ef0ce276c"))

    def test_verify_is_none_when_the_fetch_fails(self):
        discovery, _store = self.make({})
        self.assertIsNone(discovery.verify("uuid:x", "http://unreachable/"))

    def test_verify_is_none_for_a_device_that_is_not_a_media_renderer(self):
        location = "http://192.168.2.60:1266/"
        discovery, _store = self.make({location: NOT_A_RENDERER_XML})
        self.assertIsNone(discovery.verify("uuid:99999999-8888-7777-6666-555555555555", location))

    def test_a_non_lg_renderer_is_flagged_correctly(self):
        location = "http://10.0.0.5:4000/"
        discovery, _store = self.make({location: OTHER_RENDERER_XML})
        device = discovery.verify("uuid:11111111-2222-3333-4444-555555555555", location)
        self.assertFalse(device.is_lg)


if __name__ == "__main__":
    unittest.main()
