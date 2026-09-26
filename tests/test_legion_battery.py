"""Hardware-free tests for the Legion Go battery plugin.

    python3 -m unittest discover -s tests -v      # or: python3 tests/test_legion_battery.py

Covers the report parser, the row formatting and the report framing, so the
plugin can be checked without a docked controller. Live readings are what
probe.py is for.
"""

import inspect
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import legion_battery as lb  # noqa: E402


def report(left=87, right=92, connL=1, connR=1, attL=0, attR=0, rid=0x04, size=64):
    """A raw dock report.

    attL/attR are the wire bits, which are inverted: 0 means attached.
    """
    buf = bytearray(size)
    buf[0] = rid
    buf[5] = left
    buf[7] = right
    buf[10] = connL << 7
    buf[11] = connR << 7
    buf[12] = attL << 7
    buf[13] = attR << 7
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
        self.assertEqual(values["attL"], 1)  # wire bit was 0 -> attached
        self.assertEqual(values["attR"], 1)
        self.assertEqual(values["connL"], 1)

    def test_reports_detachment(self):
        values = lb.parse_report(report(attL=1))
        self.assertEqual(values["attL"], 0)
        self.assertEqual(values["attR"], 1)

    def test_accepts_hhds_own_report_id(self):
        self.assertIsNotNone(lb.parse_report(report(rid=0x74)))

    def test_rejects_foreign_and_short_reports(self):
        self.assertIsNone(lb.parse_report(report(rid=0x01)))
        self.assertIsNone(lb.parse_report(report()[:32]))

    def test_clamps_nonsense_percentages(self):
        self.assertEqual(lb.parse_report(report(left=255))["left"], 100)


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
        self.assertEqual(rows["left_state"], "attached · radio-linked")
        self.assertIn("live", rows["stream"])

    def test_powered_off_and_detached_are_distinguished(self):
        values = lb.parse_report(report(left=0, right=55, connL=0, connR=0, attL=0, attR=1))
        snap, now = self.snap(values=values, seen=True, open=True, path="/dev/hidraw1")
        snap["last_seen"] = now
        rows = lb.rows(snap, now)
        self.assertEqual(rows["left"], "0%")
        self.assertEqual(rows["left_state"], "attached · powered off")
        self.assertEqual(rows["right_state"], "detached")

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
