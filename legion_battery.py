"""Legion Go controller battery levels as an hhd plugin.

Adds a "Legion Battery" tab to hhd-ui (both the desktop app and the in-game
overlay) showing the live battery level of the left and right Legion Go
controllers.

Why a plugin is needed: the stock `hid-lenovo-go` kernel driver does not
surface controller battery state through evdev, so hhd has nothing to report
for the controllers. It does pass the dock's reports straight through to
hidraw, and the status report carries everything we need:

    byte  5         left controller battery (0-100)
    byte  7         right controller battery (0-100)
    byte 12 bit 0   left controller docked   (bit = 0 -> docked)
    byte 13 bit 0   right controller docked  (bit = 0 -> docked)

That is measured, not documented: undocking the right controller was observed to
move byte 13 from 0x02 to 0x01 while byte 12 stayed at 0x02. The flag is the low
bit, not bit 7, which answers "docked" in every real frame. Bytes 10 and 11
read 0x01 in every captured report and are deliberately not interpreted.

A docked controller is not the only one the dock reports on: one left switched
on on the desk kept reporting its real level (99%, well after it was
undocked), since it stays in radio contact with the tablet. So the tab shows a
level for a detached controller and marks it detached, rather than hiding it -
the only frames that lie are the ones during the re-dock transition.

The same endpoint also carries other 64-byte messages with the same report id
(0x04) - button and axis traffic, distinguishable only by a different constant
header in bytes 1-3 and by their attachment bytes being 0x00 or 0x80. They are
filtered out by `is_status_report`; a transition burst once showed 1% and 129%
where no controller had reported that.

Implementation notes:

* hidraw hands every open reader its own copy of each report, so this plugin
  opens a second descriptor on the same node hhd already uses. Nothing is ever
  written to the device and hhd's own descriptor is untouched.
* Re-docking the controllers makes the kernel create fresh hid devices, so the
  hidraw node is neither stable nor unique over time: it is located by its
  vendor usage page (0xFFA0) and re-checked every poll, and any failure to open
  or read it drops the cached path so the next attempt looks again. Without
  that, one re-dock is enough to freeze the tab on a stale reading forever.
* The rail is a firehose (several reports per millisecond) while hhd's update
  loop runs roughly every 2s, so `update()` never blocks: the node is opened
  non-blocking and whatever reports are pending are drained per call.
* Readings are published as `display` settings, which hhd-ui renders read-only
  and keeps up to date by polling. A rail that goes quiet freezes the last
  reading and reports its age instead of blanking, and a controller that is
  powered off simply reads 0%, which is what the dock reports for it.
* No dependencies beyond hhd itself, and no extra udev rule: hhd runs as root
  and can open the node on its own.
"""

from __future__ import annotations

import logging
import os
import select
import time
from typing import Any, Sequence

from hhd.plugins import Config, Context, Emitter, HHDSettings, HHDPlugin

logger = logging.getLogger("legion-battery")

# Geometry of the dock's raw (config) interface; see the module docstring.
REPORT_BYTES = 64
# Only the status report's own id is accepted. 0x74 turned up in an older draft
# of this file, but it never appears on the wire (110s of traffic plus a
# re-dock cycle) and hhd's own map for this dock keys buttons and axes on it,
# so treating it as a reading would mean publishing button traffic as a level.
ACCEPTED_IDS = (0x04,)
LEGION_VID_PREFIX = "0003:17EF:"  # Lenovo
# The dock exposes three interfaces; the config (raw) one is the only one whose
# report descriptor declares the vendor page 0xFFA0 (the gamepad and pointer
# interfaces use 0x0001 and 0x0059). Identifying it by usage page rather than
# by HID_PHYS "/input2" matters because hidraw numbers are not stable across
# a re-dock.
LEGION_RAW_USAGE_PAGE = 0xFFA0
LEGION_RAW_PHYS_SUFFIX = "/input2"  # fallback if the descriptor is unreadable

HIDRAW_SYSFS = "/sys/class/hidraw"
HID_SYSFS = "/sys/bus/hid/devices"

# A rail that stops talking (dock yanked, controller hard-off mid-report) must
# not be mistaken for a live reading, so freshness is reported separately.
SILENCE_TIMEOUT = 15.0
REOPEN_INTERVAL = 3.0
READ_CHUNK = REPORT_BYTES * 32
# Reports on this interface are 64 bytes, so a foreign one usually still frames
# fine; a run of them means a shorter report threw the stride off, and the
# stream is re-aligned on the next plausible header.
MAX_FOREIGN_FRAMES = 8

_UNKNOWN = "—"
_DOCK_NAME = "legion controller"
# hhd addresses a setting as "<plugin>.<section>.<row>" and the UI reads that
# exact path out of the polled state, so the live values have to be written
# there (not at "<plugin>.<row>").
SECTION = "settings"


def _read_dmi(field: str) -> str:
    try:
        with open(f"/sys/class/dmi/id/{field}") as f:
            return f.read().strip()
    except OSError:
        return ""


_is_legion_go: bool | None = None


def on_legion_go() -> bool:
    """Whether this machine is a Legion Go, so the tab is only offered there."""
    global _is_legion_go
    if _is_legion_go is None:
        version = _read_dmi("product_version").lower()
        vendor = _read_dmi("sys_vendor").lower()
        name = _read_dmi("product_name").upper()
        # "83E1"/"83E0" are the Legion Go model codes; product_version reads
        # "Legion Go 8APU1" on this generation.
        _is_legion_go = "legion" in version or (vendor == "lenovo" and name.startswith("83E"))
    return _is_legion_go


def _first_usage_page(descriptor: bytes) -> int | None:
    """Usage page of the first usage in a HID report descriptor.

    Short items are one prefix byte plus a 0/1/2/4 byte little-endian value,
    with the size in the prefix's low two bits. Long items (0xFE) carry their
    own data-size byte instead and are skipped.
    """
    i = 0
    while i < len(descriptor):
        prefix = descriptor[i]
        i += 1
        if prefix == 0xFE:  # long item: next byte is the data size
            if i >= len(descriptor):
                return None
            i += 1 + descriptor[i]
            continue
        size = (0, 1, 2, 4)[prefix & 0x03]
        value = int.from_bytes(descriptor[i : i + size], "little")
        i += size
        if prefix & 0xFC == 0x04:  # Global, tag 0: Usage Page
            return value
    return None


def _hid_node(node: str) -> tuple[str, bytes] | None:
    """(HID_PHYS, report descriptor) for a hidraw node of the dock, or None.

    Returns None for anything that is not one of the dock's own interfaces, so
    a missing or unreadable sysfs entry is indistinguishable from "not ours".
    """
    device = os.path.realpath(os.path.join(HIDRAW_SYSFS, node, "device"))
    hid = os.path.basename(device)
    if not hid.startswith(LEGION_VID_PREFIX):
        return None
    base = os.path.join(HID_SYSFS, hid)
    try:
        with open(f"{base}/uevent") as f:
            uevent = dict(line.split("=", 1) for line in f.read().splitlines() if "=" in line)
    except OSError:
        return None
    if "legion" not in uevent.get("HID_NAME", "").lower():
        return None
    try:
        with open(f"{base}/report_descriptor", "rb") as f:
            descriptor = f.read()
    except OSError:
        descriptor = b""
    return uevent.get("HID_PHYS", ""), descriptor


def _is_raw_interface(phys: str, descriptor: bytes) -> bool:
    page = _first_usage_page(descriptor)
    if page is not None:
        return page == LEGION_RAW_USAGE_PAGE
    return phys.endswith(LEGION_RAW_PHYS_SUFFIX)


def discover_rail() -> str | None:
    """Return /dev/hidrawN of the dock's raw interface, or None if absent."""
    try:
        nodes = sorted(os.listdir(HIDRAW_SYSFS))
    except OSError:
        return None

    fallback = None
    for node in nodes:
        info = _hid_node(node)
        if info is None:
            continue
        phys, descriptor = info
        if _first_usage_page(descriptor) == LEGION_RAW_USAGE_PAGE:
            return os.path.join("/dev", node)
        if fallback is None and phys.endswith(LEGION_RAW_PHYS_SUFFIX):
            fallback = os.path.join("/dev", node)  # descriptor was unreadable
    return fallback


def rail_still_present(path: str | None) -> bool:
    """Whether `path` is still the dock's raw interface.

    Re-docking the controllers makes the kernel create fresh hid devices, so a
    node that was valid a minute ago can be gone - or, since hidraw numbers
    get recycled, point at a completely different device. Callers use this to
    notice that the descriptor they hold is dead and to look again.
    """
    if not path:
        return False
    node = os.path.basename(path)
    info = _hid_node(node) if node.startswith("hidraw") else None
    return info is not None and _is_raw_interface(*info)


def is_status_report(buf: bytes) -> bool:
    """Whether a 64-byte frame is a status report and not another message.

    The dock puts more than one kind of 64-byte message on this endpoint, all
    of them with report id 0x04, so the id cannot tell them apart. Measured on
    this unit, a status report's attachment bytes (12 and 13) are a small
    value whose low bit is the present flag; the other messages use 0x00
    (nothing) or 0x80 (top bit set) there, and their bytes 5 and 7 are not a
    battery level at all - 1% and 129% were observed. Since the reader keeps
    the newest report of a drain, accepting one of those would publish a level
    no controller ever reported, so they are dropped instead.
    """
    if len(buf) < REPORT_BYTES or buf[0] not in ACCEPTED_IDS:
        return False
    return all(0 < byte < 0x80 for byte in (buf[12], buf[13]))


def parse_report(buf: bytes) -> dict[str, int] | None:
    """Battery/attachment state from one status report, or None if not ours.

    Bytes 10 and 11 are not interpreted: they read 0x01 in every status report
    captured on this unit, so nothing here claims to know what they mean.
    """
    if not is_status_report(buf):
        return None
    return {
        "left": min(100, buf[5]),
        "right": min(100, buf[7]),
        # Attachment flag is inverted: bit 0 clear means docked.
        "attL": 1 - (buf[12] & 1),
        "attR": 1 - (buf[13] & 1),
    }


class _Rail:
    """Non-blocking reader for the dock's raw interface."""

    def __init__(self) -> None:
        self.path: str | None = None
        self.fd: int | None = None
        self.values: dict[str, int] | None = None
        self.last_seen: float = 0.0
        self._buf = bytearray()
        self._foreign = 0
        self._retry_at = 0.0
        self._logged_path: str | None = None
        self._rejected_logged = False

    def poll(self, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        if self.fd is not None and not rail_still_present(self.path):
            # Re-docking recreates the dock's hid devices, so the descriptor
            # held here can belong to a device that no longer exists.
            logger.info("legion-battery: %s is no longer the dock's rail", self.path)
            self._close_fd()
            self.path = None
        if self.fd is None and now >= self._retry_at:
            self._retry_at = now + REOPEN_INTERVAL
            self._open()
        if self.fd is not None:
            self._drain(now)
        return {
            "values": self.values,
            "last_seen": self.last_seen,
            "seen": self.last_seen > 0.0,
            "open": self.fd is not None,
            "path": self.path,
        }

    def _open(self) -> None:
        if self.path is None:
            self.path = discover_rail()
        if self.path is None:
            return
        try:
            self.fd = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError as e:
            # The node went away or was renumbered since it was found; forget it
            # so the next attempt looks for the interface again instead of
            # retrying a path that cannot work.
            logger.debug("legion-battery: cannot open %s: %s", self.path, e)
            self.path = None
            self.fd = None
            return
        self._buf.clear()
        self._foreign = 0
        self._rejected_logged = False
        if self._logged_path != self.path:
            if self._logged_path is not None:
                logger.info(
                    "legion-battery: rail moved from %s to %s", self._logged_path, self.path
                )
            self._logged_path = self.path
            logger.info("legion-battery: reading controller battery from %s", self.path)

    def _close_fd(self) -> None:
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
        self.fd = None
        self._buf.clear()

    def close(self) -> None:
        self._close_fd()

    def _drain(self, now: float) -> None:
        assert self.fd is not None
        while True:
            try:
                ready, _, _ = select.select([self.fd], [], [], 0)
            except (OSError, ValueError):
                self._close_fd()
                return
            if not ready:
                return
            try:
                chunk = os.read(self.fd, READ_CHUNK)
            except BlockingIOError:
                return
            except OSError as e:
                # The interface is torn down when the dock is yanked or the
                # controllers are re-docked; forget the node so the next poll
                # discovers whatever replaced it.
                logger.debug("legion-battery: read failed on %s: %s", self.path, e)
                self._close_fd()
                self.path = None
                return
            if not chunk:
                self._close_fd()
                self.path = None
                return
            self._buf += chunk
            self._frame(now)

    def _skip_to_header(self) -> int:
        """How many buffered bytes to drop to reach the next battery report."""
        for offset in range(0, len(self._buf) - REPORT_BYTES + 1):
            if self._buf[offset] not in ACCEPTED_IDS:
                continue
            # A window that merely happens to start with a report id is usually
            # not a report; both battery bytes of a real one are 0-100.
            if self._buf[offset + 5] <= 100 and self._buf[offset + 7] <= 100:
                return offset
        return len(self._buf) - REPORT_BYTES + 1

    def _frame(self, now: float) -> None:
        """Consume every complete report in the buffer, keeping the newest.

        Draining the whole buffer matters after a backlog builds up (right
        after opening the node, or after the poll was late): only the last
        report is interesting, and anything left behind would be replayed one
        report per update, trailing reality by minutes.
        """
        while len(self._buf) >= REPORT_BYTES:
            if self._buf[0] not in ACCEPTED_IDS:
                self._foreign += 1
                if self._foreign >= MAX_FOREIGN_FRAMES:
                    # Re-align on the next plausible header, so real reports
                    # queued behind the foreign ones are not thrown away.
                    del self._buf[: self._skip_to_header()]
                    self._foreign = 0
                else:
                    del self._buf[:REPORT_BYTES]
                continue

            frame = bytes(self._buf[:REPORT_BYTES])
            del self._buf[:REPORT_BYTES]
            self._foreign = 0
            values = parse_report(frame)
            if values is not None:
                self.values = values
                self.last_seen = now
            elif not self._rejected_logged:
                # The dock interleaves other 64-byte messages here; log one so a
                # future firmware that shifts the layout leaves a trace.
                self._rejected_logged = True
                logger.debug(
                    "legion-battery: ignoring a non-status frame on %s: %s",
                    self.path,
                    frame[:20].hex(" "),
                )


def _describe(pct: int | None, attached: int) -> str:
    if pct is None:
        return "no data"
    if not attached:
        # Undocking sets this bit, and the battery byte goes to 0 as well, so
        # the level alone cannot tell "off" from "not in the dock".
        return "detached"
    return "attached · powered off" if pct == 0 else "attached"


def _freshness(snap: dict[str, Any], now: float) -> str:
    if snap["path"] is None:
        return f"no {_DOCK_NAME} dock found"
    if not snap["open"]:
        return "dock found, waiting for access"
    if not snap["seen"]:
        return f"listening on {snap['path']}"
    age = max(0, int(now - snap["last_seen"]))
    if age > SILENCE_TIMEOUT:
        return f"rail silent · last update {age}s ago"
    return f"live · updated {age}s ago"


def rows(snap: dict[str, Any], now: float | None = None) -> dict[str, str]:
    """The display rows, as `{key: text}` for the settings below."""
    now = time.time() if now is None else now
    values = snap["values"] or {}
    left = values.get("left")
    right = values.get("right")
    return {
        "left": _UNKNOWN if left is None else f"{left}%",
        "right": _UNKNOWN if right is None else f"{right}%",
        "left_state": _describe(left, values.get("attL", 0)),
        "right_state": _describe(right, values.get("attR", 0)),
        "stream": _freshness(snap, now),
    }


class LegionBattery(HHDPlugin):
    name = "legion_battery"
    log = "legion-battery"
    priority = 10
    location = "misc"

    def __init__(self) -> None:
        self.rail = _Rail()

    def settings(self) -> HHDSettings:
        if not on_legion_go():
            return {}
        return {
            "legion_battery": {
                SECTION: {
                    "type": "container",
                    "title": "Legion Controller Battery",
                    "children": {
                        "left": {
                            "type": "display",
                            "tags": ["bold"],
                            "title": "Left",
                            "default": _UNKNOWN,
                        },
                        "right": {
                            "type": "display",
                            "tags": ["bold"],
                            "title": "Right",
                            "default": _UNKNOWN,
                        },
                        "left_state": {
                            "type": "display",
                            "title": "Left state",
                            "default": "no data",
                        },
                        "right_state": {
                            "type": "display",
                            "title": "Right state",
                            "default": "no data",
                        },
                        "stream": {
                            "type": "display",
                            "tags": ["slim"],
                            "title": "Stream",
                            "default": "starting",
                        },
                    },
                }
            }
        }

    def open(self, emit: Emitter, context: Context) -> None:
        # Opens the node if it is there; the poll itself is non-blocking.
        self.rail.poll()

    def prepare(self, conf: Config) -> None:
        pass

    def update(self, conf: Config) -> None:
        now = time.time()
        snap = self.rail.poll(now)
        for key, value in rows(snap, now).items():
            path = f"{self.name}.{SECTION}.{key}"
            if conf.get(path, None) != value:
                conf[path] = value

    def close(self) -> None:
        self.rail.close()


def autodetect(existing: Sequence[HHDPlugin]) -> Sequence[HHDPlugin]:
    if any(getattr(p, "name", None) == LegionBattery.name for p in existing):
        return []
    return [LegionBattery()]
