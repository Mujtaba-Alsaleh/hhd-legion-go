#!/usr/bin/env python3
"""Poll the Legion Go controller rail and print what the hhd tab will show.

Useful to confirm the dock's raw interface is readable (and that levels move)
without restarting hhd. Reads only, never writes to the device.

    ./probe.py            # 20 s
    ./probe.py 60         # a minute
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import legion_battery
except ImportError:  # folded into hhd's own package
    from hhd.plugins import legion_battery  # type: ignore[no-redef]

DURATION = float(sys.argv[1]) if len(sys.argv) > 1 else 20.0


def main() -> int:
    if not legion_battery.on_legion_go():
        print("not a Legion Go (DMI says otherwise) - the plugin stays hidden")
        return 1

    print("discovering the legion controller dock interface ...")
    print(f"rail interface: {legion_battery.discover_rail() or 'not found'}")
    print(f"polling for {DURATION:.0f}s, Ctrl-C to stop\n")

    rail = legion_battery._Rail()
    shown: dict[str, str] = {}
    snap: dict = {}
    end = time.time() + DURATION
    try:
        while time.time() < end:
            now = time.time()
            snap = rail.poll(now)
            for key, value in legion_battery.rows(snap, now).items():
                if shown.get(key) != value:
                    shown[key] = value
                    print(f"  {key:<11} {value}")
            time.sleep(0.5)
    except KeyboardInterrupt:
        print()
    finally:
        rail.close()

    if not snap.get("seen"):
        print("\nno battery reports received")
        return 1
    print(f"\nlast report: {snap['values']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
