"""Hardware-free tests for the Legion Go battery plugin.

    python3 -m unittest discover -s tests -v      # or: python3 tests/test_legion_battery.py

Covers the report parser, the row formatting and the report framing, so the
plugin can be checked without a docked controller. Live readings are what
probe.py is for.
"""

import inspect
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import legion_battery as lb  # noqa: E402

# The dock's three report descriptors, verbatim as the kernel exposes them:
# the config (raw) interface is the only vendor-page (0xFFA0) one.
DESCRIPTOR_RAW = bytes.fromhex("06a0ff0901a101090385041580257f350045ff7508953f81")
DESCRIPTOR_GAMEPAD = bytes.fromhex("05010906a1018506050719e029e715002501750195088102")
DESCRIPTOR_POINTER = bytes.fromhex("05590901a10185010902a1020903150027ffff0000950175")

# Real frames captured off the rail, as the first 20 bytes plus a zero tail.
# The status frames share the constant header 04 3c 74 01; the two others are
# different 64-byte messages the dock interleaves on the same endpoint, one of
# them carrying 1% and 129% in the battery bytes. Each entry is
# (report id, left, right, byte 12, byte 13) as observed, so a mistyped hex
# string cannot silently change what a test asserts.
CAPTURED = {
    "both_docked": (0x04, 100, 100, 0x02, 0x02),
    "right_out": (0x04, 100, 0, 0x02, 0x01),
    "right_out_re_enumerated": (0x04, 100, 0, 0x02, 0x03),
    "not_status": (0x04, 1, 129, 0x80, 0x00),
    "all_zero_state": (0x04, 1, 0, 0x00, 0x00),
}


def captured(name):
    """One of the frames above, as it came off the rail."""
    rid, left, right, b12, b13 = CAPTURED[name]
    buf = bytearray(lb.REPORT_BYTES)
    buf[0:2] = bytes((0x04, 0x3C if b12 or b13 else 0x06))
    buf[5], buf[7], buf[12], buf[13] = left, right, b12, b13
    return bytes(buf)


def report(left=87, right=92, attL=0, attR=0, rid=0x04, size=64):
    """A synthetic status report.

    attL/attR are the wire bits of bytes 12/13, which are inverted: 0 means
    docked. Byte 12/13 get bit 0 cleared so the frame passes the status check.
    """
    buf = bytearray(size)
    buf[0] = rid
    buf[5] = left
    buf[7] = right
    buf[12] = 0x02 | (attL & 1)
    buf[13] = 0x02 | (attR & 1)
    return bytes(buf)


class FakeConf(dict):
    """Just enough of hhd's Config for the plugin's write path."""

    def get(self, key, default=None):
        return dict.get(self, key, default)

    def __setitem__(self, key, value):
        dict.__setitem__(self, key, value)


class TestParseReport(unittest.TestCase):
    def test_reads_both_controllers(self):
        values = lb.parse_report(report(left=87, right=92))
        self.assertEqual(values["left"], 87)
        self.assertEqual(values["right"], 92)
        self.assertEqual(values["attL"], 1)  # wire bit was 0 -> docked
        self.assertEqual(values["attR"], 1)

    def test_reports_undocking(self):
        values = lb.parse_report(report(attR=1))
        self.assertEqual(values["attL"], 1)
        self.assertEqual(values["attR"], 0)

    def test_rejects_foreign_and_short_reports(self):
        self.assertIsNone(lb.parse_report(report(rid=0x01)))
        self.assertIsNone(lb.parse_report(report()[:32]))

    def test_clamps_nonsense_percentages(self):
        self.assertEqual(lb.parse_report(report(left=255))["left"], 100)


class TestCapturedFrames(unittest.TestCase):
    """The frames these tests decode were captured off the rail on this unit,
    which is the only evidence for what the bytes mean."""

    def test_both_docked(self):
        values = lb.parse_report(captured("both_docked"))
        self.assertEqual((values["left"], values["right"]), (100, 100))
        self.assertEqual((values["attL"], values["attR"]), (1, 1))

    def test_undocking_the_right_controller_is_visible(self):
        """The bug this fixes: the old bit-7 decode read 0 in every one of
        these bytes, so an undocked controller still showed as attached."""
        for name in ("right_out", "right_out_re_enumerated"):
            with self.subTest(frame=name):
                values = lb.parse_report(captured(name))
                self.assertEqual(values["left"], 100)
                self.assertEqual(values["right"], 0)  # dock reports 0 when out
                self.assertEqual(values["attL"], 1)  # left never left the dock
                self.assertEqual(values["attR"], 0)  # right is out

    def test_other_messages_on_the_endpoint_are_not_status_reports(self):
        # Both carry report id 0x04 and are 64 bytes long, but their battery
        # bytes are nonsense (1% and 129%) and their state bytes are 0x80/0x00.
        for name in ("not_status", "all_zero_state"):
            with self.subTest(frame=name):
                frame = captured(name)
                self.assertEqual(frame[0], 0x04)  # same report id
                self.assertEqual(len(frame), 64)  # same length
                self.assertFalse(lb.is_status_report(frame))
                self.assertIsNone(lb.parse_report(frame))

    def test_a_burst_of_them_cannot_overwrite_the_reading(self):
        """The reader keeps the newest report of a drain, so an interleaved
        message would otherwise become the published level."""
        rail = lb._Rail()
        rail._buf += captured("both_docked") * 3
        rail._buf += captured("not_status")
        rail._buf += captured("all_zero_state")
        rail._buf += captured("right_out") * 2
        rail._frame(time.time())
        self.assertEqual(rail.values["left"], 100)
        self.assertEqual(rail.values["right"], 0)
        self.assertEqual(rail.values["attR"], 0)

    def test_the_last_reading_survives_a_drain_of_only_junk(self):
        rail = lb._Rail()
        rail.values = lb.parse_report(captured("both_docked"))
        rail._buf += captured("not_status") * 4
        rail._frame(time.time())
        self.assertEqual(rail.values["right"], 100)


class TestRows(unittest.TestCase):
    def snap(self, **kw):
        now = time.time()
        snap = {
            "values": None,
            "last_seen": 0.0,
            "seen": False,
            "open": False,
            "path": None,
        }
        snap.update(kw)
        return snap, now

    def test_no_data_keeps_the_tab_readable(self):
        snap, now = self.snap()
        rows = lb.rows(snap, now)
        self.assertEqual(rows["left"], "—")
        self.assertEqual(rows["left_state"], "no data")
        self.assertIn("no legion controller dock", rows["stream"])

    def test_live_reading(self):
        snap, now = self.snap(
            values=lb.parse_report(report(left=64, right=31)),
            seen=True,
            open=True,
            path="/dev/hidraw1",
        )
        snap["last_seen"] = now - 2
        rows = lb.rows(snap, now)
        self.assertEqual(rows["left"], "64%")
        self.assertEqual(rows["right"], "31%")
        self.assertEqual(rows["left_state"], "attached")
        self.assertIn("live", rows["stream"])

    def test_powered_off_and_undocked_are_distinguished(self):
        # Both read 0%, so only the dock bit can tell them apart.
        values = lb.parse_report(report(left=0, right=55, attL=0, attR=1))
        snap, now = self.snap(values=values, seen=True, open=True, path="/dev/hidraw1")
        snap["last_seen"] = now
        rows = lb.rows(snap, now)
        self.assertEqual(rows["left"], "0%")
        self.assertEqual(rows["left_state"], "attached · powered off")
        self.assertEqual(rows["right_state"], "detached")
        self.assertEqual(rows["right"], "55%")

    def test_silent_rail_reports_its_age(self):
        values = lb.parse_report(report())
        snap, now = self.snap(values=values, seen=True, open=True, path="/dev/hidraw1")
        snap["last_seen"] = now - 90
        rows = lb.rows(snap, now)
        self.assertEqual(rows["left"], "87%")  # the last reading is kept
        self.assertIn("rail silent", rows["stream"])
        self.assertIn("90s", rows["stream"])


class TestFraming(unittest.TestCase):
    def test_frames_a_stream_in_order(self):
        rail = lb._Rail()
        rail._buf += report(left=10) + report(left=11)
        rail._frame(time.time())
        self.assertEqual(rail.values["left"], 11)  # the newest report wins
        self.assertEqual(len(rail._buf), 0)

    def test_drains_a_backlog_in_one_poll(self):
        # Opening the node can hand us a ring full of reports; only the last
        # one matters, and the rest must not be replayed for minutes.
        rail = lb._Rail()
        rail._buf += b"".join(report(left=50, right=(i % 100) + 1) for i in range(500))
        rail._frame(time.time())
        self.assertEqual(rail.values["right"], 100)  # the 500th report
        self.assertEqual(len(rail._buf), 0)

    def test_keeps_a_partial_report_for_the_next_poll(self):
        rail = lb._Rail()
        frame = report(left=42)
        rail._buf += frame[:30]
        rail._frame(time.time())
        self.assertIsNone(rail.values)
        rail._buf += frame[30:]
        rail._frame(time.time())
        self.assertEqual(rail.values["left"], 42)
        self.assertEqual(len(rail._buf), 0)

    def test_skips_foreign_reports_then_resyncs(self):
        rail = lb._Rail()
        # A stream of something else must not wedge the framing.
        rail._buf += report(rid=0x01) * (lb.MAX_FOREIGN_FRAMES + 4)
        rail._buf += report(left=77)
        rail._frame(time.time())
        self.assertEqual(rail.values["left"], 77)
        self.assertEqual(len(rail._buf), 0)


class TestSettings(unittest.TestCase):
    def test_section_is_display_only(self):
        if not lb.on_legion_go():
            self.skipTest("not a Legion Go")
        children = lb.LegionBattery().settings()["legion_battery"]["settings"]["children"]
        self.assertEqual(children["left"]["type"], "display")
        self.assertIn("bold", children["left"]["tags"])
        self.assertIn("slim", children["stream"]["tags"])
        # Every row needs a default, so hhd always has a string to render.
        for row in children.values():
            self.assertIn("default", row)

    def test_update_writes_only_changed_values(self):
        conf = FakeConf()
        plugin = lb.LegionBattery()
        plugin.update(conf)
        first = dict(conf)
        self.assertTrue(any(k.startswith("legion_battery.") for k in first))
        plugin.update(conf)
        self.assertEqual(first, dict(conf))  # nothing changed -> no writes

    def test_writes_exactly_the_paths_hhd_expects(self):
        """The UI reads <plugin>.<section>.<row> out of the polled state, so
        every path the plugin writes must be one hhd itself generates for the
        plugin's settings - otherwise the tab renders the seeded defaults."""
        try:
            from hhd.plugins.settings import parse_defaults
        except ImportError:
            self.skipTest("hhd is not importable here")
        plugin = lb.LegionBattery()
        conf = FakeConf()
        plugin.update(conf)
        self.assertEqual(set(conf), set(parse_defaults(plugin.settings())))


class TestApiCompatibility(unittest.TestCase):
    """hhd calls the plugin hooks positionally, so the signatures must match.

    This is the check that catches an hhd version whose hook signatures moved
    (hhd 4.1.x calls open(emit, context), while newer trees pass conf too).
    """

    CALLS = {
        "open": (object(), object()),
        "settings": (),
        "validate": ([], None, None),
        "prepare": (FakeConf(),),
        "update": (FakeConf(),),
        "notify": ([],),
        "close": (),
    }

    def test_hooks_accept_what_hhd_passes(self):
        try:
            from hhd.plugins import HHDPlugin
        except ImportError:
            self.skipTest("hhd is not importable here")
        plugin = lb.LegionBattery()
        for name, args in self.CALLS.items():
            with self.subTest(hook=name):
                # hhd's call: plugin.<hook>(*args); ours must accept it.
                inspect.signature(getattr(HHDPlugin, name)).bind(plugin, *args)
                inspect.signature(getattr(plugin, name)).bind(*args)


class TestUsagePage(unittest.TestCase):
    def test_reads_the_page_of_a_short_item(self):
        # 0x06: global, Usage Page, two bytes -> 0xFFA0; 0x05 is the same item
        # with a one-byte value.
        self.assertEqual(lb._first_usage_page(DESCRIPTOR_RAW), 0xFFA0)
        self.assertEqual(lb._first_usage_page(DESCRIPTOR_GAMEPAD), 0x0001)  # Generic Desktop
        self.assertEqual(lb._first_usage_page(DESCRIPTOR_POINTER), 0x0059)
        self.assertNotEqual(
            lb._first_usage_page(DESCRIPTOR_GAMEPAD), lb.LEGION_RAW_USAGE_PAGE
        )

    def test_handles_one_and_four_byte_items_and_long_items(self):
        self.assertEqual(lb._first_usage_page(bytes([0x05, 0x0A])), 0x0A)  # 1 byte
        self.assertEqual(lb._first_usage_page(bytes([0x06, 0xA0, 0xFF, 0x00])), 0xFFA0)
        # A long item (0xFE, own data size) before the usage page must be skipped.
        self.assertEqual(lb._first_usage_page(bytes([0xFE, 0x02, 0xAA, 0xBB, 0x06, 0xA0, 0xFF])), 0xFFA0)

    def test_empty_descriptor_has_no_page(self):
        self.assertIsNone(lb._first_usage_page(b""))


class FakeSysfs:
    """A throwaway /sys with hidraw nodes, so discovery can be tested offline.

    `add` takes the node name, HID id, HID_PHYS and descriptor; the HID_PHYS
    index is deliberately allowed to differ from the node name because that is
    exactly what a re-dock does.
    """

    def __init__(self, testcase):
        self.root = tempfile.mkdtemp()
        testcase.addCleanup(shutil.rmtree, self.root, True)
        self.hidraw = os.path.join(self.root, "class", "hidraw")
        self.hid = os.path.join(self.root, "bus", "hid", "devices")
        os.makedirs(self.hidraw)
        os.makedirs(self.hid)
        testcase.addCleanup(self._restore)
        self._real = (lb.HIDRAW_SYSFS, lb.HID_SYSFS)
        lb.HIDRAW_SYSFS, lb.HID_SYSFS = self.hidraw, self.hid

    def _restore(self):
        lb.HIDRAW_SYSFS, lb.HID_SYSFS = self._real

    def add(self, node, hid, phys, descriptor=DESCRIPTOR_RAW, name="Legion Controller"):
        device = os.path.join(self.hid, hid)
        os.makedirs(device, exist_ok=True)
        with open(os.path.join(device, "uevent"), "w") as f:
            f.write(f"HID_NAME={name}\nHID_PHYS={phys}\nHID_ID={hid}\n")
        with open(os.path.join(device, "report_descriptor"), "wb") as f:
            f.write(descriptor)
        os.makedirs(os.path.join(self.hidraw, node), exist_ok=True)
        link = os.path.join(self.hidraw, node, "device")
        if not os.path.islink(link):
            os.symlink(device, link)

    def dock(self, minor="hidraw1", index=2):
        """The dock's three interfaces, with the raw one at HID_PHYS /inputN."""
        stem = "usb-0000:c2:00.3-3"
        self.add(minor, f"0003:17EF:0005:0001.000{index}", f"{stem}/input{index}", DESCRIPTOR_RAW)
        self.add(f"hidraw{int(minor[-1]) - 1}", f"0003:17EF:0005:0001.000{index - 1}",
                 f"{stem}/input{index - 1}", DESCRIPTOR_GAMEPAD)
        self.add(f"hidraw{int(minor[-1]) + 1}", f"0003:17EF:0005:0001.000{index + 1}",
                 f"{stem}/input{index + 1}", DESCRIPTOR_POINTER)


class TestDiscovery(unittest.TestCase):
    def test_finds_the_raw_interface_whatever_its_number_and_index(self):
        for minor, index in (("hidraw1", 2), ("hidraw15", 2), ("hidraw9", 7)):
            with self.subTest(node=minor, index=index):
                sysfs = FakeSysfs(self)
                sysfs.dock(minor=minor, index=index)
                self.assertEqual(lb.discover_rail(), f"/dev/{minor}")

    def test_ignores_gamepad_pointer_and_other_vendors(self):
        sysfs = FakeSysfs(self)
        sysfs.add("hidraw0", "0003:17EF:0005:0001.0001", "usb-x/input1", DESCRIPTOR_GAMEPAD)
        sysfs.add("hidraw3", "0003:17EF:0005:0001.0002", "usb-x/input2", DESCRIPTOR_RAW)
        sysfs.add("hidraw4", "0003:17EF:0005:0001.0003", "usb-x/input3", DESCRIPTOR_POINTER)
        # Another device's vendor page must not be mistaken for the dock.
        sysfs.add("hidraw5", "0003:046D:0B00:0001.0000", "usb-y/input1", DESCRIPTOR_RAW,
                  name="Some Other Vendor Device")
        self.assertEqual(lb.discover_rail(), "/dev/hidraw3")

    def test_falls_back_to_phys_when_no_descriptor_can_be_read(self):
        sysfs = FakeSysfs(self)
        sysfs.add("hidraw1", "0003:17EF:0005:0001.0001", "usb-x/input1", DESCRIPTOR_GAMEPAD)
        sysfs.add("hidraw2", "0003:17EF:0005:0001.0002", "usb-x/input2", b"")
        self.assertEqual(lb.discover_rail(), "/dev/hidraw2")

    def test_no_dock_returns_none(self):
        FakeSysfs(self)
        self.assertIsNone(lb.discover_rail())

    def test_presence_check_notices_a_vanished_or_reused_node(self):
        sysfs = FakeSysfs(self)
        sysfs.dock(minor="hidraw15", index=2)
        self.assertTrue(lb.rail_still_present("/dev/hidraw15"))
        # The dock is gone: the node disappeared with it.
        shutil.rmtree(os.path.join(sysfs.hidraw, "hidraw15"))
        self.assertFalse(lb.rail_still_present("/dev/hidraw15"))
        # Another interface now answers to that number.
        sysfs.add("hidraw15", "0003:17EF:0005:0001.0009", "usb-x/input9", DESCRIPTOR_GAMEPAD)
        self.assertFalse(lb.rail_still_present("/dev/hidraw15"))
        # A non-hidraw path, or nothing at all, is never the rail.
        self.assertFalse(lb.rail_still_present("/dev/null"))
        self.assertFalse(lb.rail_still_present(None))


class TestRecovery(unittest.TestCase):
    """Re-docking makes the kernel create new hidraw nodes, so a held
    descriptor and the path that produced it both go stale. These are the
    paths that used to wedge the plugin on a frozen reading forever."""

    def setUp(self):
        # `os` is a shared module, so keep the real function to delegate to.
        self.real_open = os.open
        # A pipe with its write end open is readable-but-empty, i.e. exactly
        # "the rail went quiet" - unlike an empty file, which is at EOF.
        self.read_fd, self.write_fd = os.pipe()
        for fd in (self.read_fd, self.write_fd):
            self.addCleanup(os.close, fd)

    def fake_open(self, path, flags, *args, **kwargs):
        """Stand in for the hidraw node only, with a quiet pipe."""
        if str(path).startswith("/dev/hidraw"):
            return os.dup(self.read_fd)
        return self.real_open(path, flags, *args, **kwargs)

    def test_failed_open_forgets_the_path_so_discovery_runs_again(self):
        rail = lb._Rail()
        rail.path = "/dev/hidraw1"  # stale: this node no longer exists
        with mock.patch.object(lb.os, "open", side_effect=FileNotFoundError(2, "gone")):
            rail._open()
        self.assertIsNone(rail.path)
        self.assertIsNone(rail.fd)

    def test_read_failure_forgets_the_path(self):
        rail = lb._Rail()
        with mock.patch.object(lb, "discover_rail", return_value="/dev/hidraw1"), \
                mock.patch.object(lb.os, "open", self.fake_open):
            rail.poll()
            self.assertEqual(rail.path, "/dev/hidraw1")
            with mock.patch.object(lb.os, "read", side_effect=OSError(19, "no such device")):
                rail.poll()
        self.assertIsNone(rail.fd)
        self.assertIsNone(rail.path)

    def test_renumbered_node_is_dropped_and_rediscovered(self):
        """The real failure: the rail came back as /dev/hidraw15 while the
        plugin still held /dev/hidraw1, and never noticed."""
        sysfs = FakeSysfs(self)
        sysfs.dock(minor="hidraw1", index=2)
        rail = lb._Rail()
        found = ["/dev/hidraw1"]
        with mock.patch.object(lb, "discover_rail", side_effect=lambda: found[0]), \
                mock.patch.object(lb.os, "open", self.fake_open):
            snap = rail.poll()
            self.assertTrue(snap["open"])
            self.assertEqual(snap["path"], "/dev/hidraw1")

            # Re-dock: the old node disappears, a new one takes its place.
            shutil.rmtree(os.path.join(sysfs.hidraw, "hidraw1"))
            sysfs.dock(minor="hidraw15", index=2)
            found[0] = "/dev/hidraw15"

            # Recovery is bounded by the retry interval, not by a restart.
            snap = rail.poll(time.time() + lb.REOPEN_INTERVAL + 1)
        self.assertEqual(snap["path"], "/dev/hidraw15")
        self.assertTrue(snap["open"])
        self.assertIsNotNone(rail.fd)
        self.assertEqual(rail._logged_path, "/dev/hidraw15")

    def test_frozen_reading_survives_a_renumber(self):
        """Losing the node must not blank the tab: the last reading stays."""
        sysfs = FakeSysfs(self)
        sysfs.dock(minor="hidraw1", index=2)
        rail = lb._Rail()
        rail.values = lb.parse_report(report(left=100, right=0))
        rail.last_seen = time.time()
        with mock.patch.object(lb, "discover_rail", side_effect=lambda: "/dev/hidraw1"), \
                mock.patch.object(lb.os, "open", self.fake_open):
            rail.poll()
            shutil.rmtree(os.path.join(sysfs.hidraw, "hidraw1"))
            snap = rail.poll()
        self.assertEqual(snap["values"]["left"], 100)
        self.assertEqual(snap["values"]["right"], 0)
        self.assertTrue(snap["seen"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
