# hhd-legion-go

Everything that keeps [hhd](https://github.com/hhd-dev/hhd) happy on the
Lenovo Legion Go (Arch, mostly) — one repo, two jobs:

1. **Legion Battery** — a plugin that adds a live controller-battery tab to
   hhd / hhd-ui, on the desktop *and* in the in-game overlay.
2. **A grab-latest-and-patch pipeline** — `./install.sh` pulls the *current*
   hhd PKGBUILD from the AUR, folds the plugin into its source, applies
   whatever is sitting in `patches/`, builds the package and installs it.

So you always end up with the newest hhd + the battery tab + the Legion Go
fixes, in one command. No hand-editing `site-packages` (an hhd upgrade blows
that away anyway) — just re-run `./install.sh` and it all comes back.

## The battery tab

The kernel's `hid-lenovo-go` driver never exposes the two controllers'
battery through evdev, but the dock's raw HID interface does. The plugin
reads it there and publishes a **Legion Battery** tab:

```
Left          100%
Right         99%
Left state    attached
Right state   detached
Stream        live · updated 1s ago
```

The tab is always there. When a controller powers off or the rail goes quiet
it keeps the last reading and tells you how old it is, instead of blanking or
vanishing. A controller that's *out of its dock* still reports its real level
(example: the right one above is switched on and sitting on a desk) — so it's
marked `detached` rather than hidden.

## Install

```sh
git clone https://github.com/Mujtaba-Alsaleh/hhd-legion-go.git
cd hhd-legion-go
./install.sh
```

That one command:

- fetches the latest AUR PKGBUILD for hhd (so you're not stuck on a pinned
  version),
- copies `legion_battery.py` into the hhd source tree (`vendor-hhd.sh`),
- applies every `patches/*.patch` in filename order,
- builds with `makepkg` (it may ask for sudo to install build deps),
- refuses to install if the plugin somehow isn't in the finished package,
- installs with `pacman -U`, restarts hhd, and runs a self-check.

Useful variations:

```sh
./install.sh --dry-run     # prepare everything, show the PKGBUILD diff, build nothing
./install.sh --refresh     # re-fetch the AUR PKGBUILD first (new hhd release?)
./install.sh --check       # is the plugin loaded? what's the tab showing?
./install.sh --local       # skip the package build: drop the plugin on hhd's
                           # PYTHONPATH via a systemd drop-in instead
./install.sh --uninstall   # undo the --local route
```

Pick one route: package (default) or `--local`, not both — with the package
installed, the plugin is already in hhd's site-packages.

## Patches

This repo doesn't just ship the plugin — it carries fixes for hhd itself and
keeps them applied across rebuilds:

| patch | what it fixes |
| --- | --- |
| `0001-legion-go-touchpad.patch` | touchpad dead on 2025+ firmware: the profile didn't match `BTN_TOUCH` + `BTN_0` |
| `0002-overlay-nested-map-unmap-only.patch` | hhd's overlay left the game on a black screen after closing the QAM in a nested gamescope session — the window is now hidden by unmapping instead of opacity/`STEAM_*` property games |

Adding your own: drop a `.patch` into `patches/` (paths like
`--- a/src/hhd/...`, applied with `-p1`), and it rides along on the next
`./install.sh`. Filename order = apply order. That's the whole workflow for
keeping a fix alive across hhd upgrades.

## After an hhd upgrade

`paru -Syu` (or any stock hhd reinstall) replaces the package and the plugin
and patches go with it. Just run `./install.sh` again — `--check` will tell
you when that's needed. Battery values live in `/etc/hhd/state.yml`, so they
survive the reinstall.

## Uninstall

```sh
./install.sh --uninstall    # removes the --local drop-in and directory
sudo pacman -S --noconfirm hhd   # back to stock hhd (also drops the patches)
```

## How the battery reading works

The dock's raw (config) interface passes a 64-byte status report through to
hidraw. Of each report:

| byte        | meaning                                 |
| ----------- | --------------------------------------- |
| 5           | left controller battery, 0-100          |
| 7           | right controller battery, 0-100         |
| 12 bit 0    | left controller docked                  |
| 13 bit 0    | right controller docked                 |

This layout was measured on one unit, not read from docs, and the tests use
frames captured off the rail as fixtures. An undocked-but-powered-on
controller keeps reporting its real level over radio, which is why a
`detached` controller still shows a percentage — only the re-dock transition
frames are unreliable. Other 64-byte messages on the same endpoint are
button/axis traffic, not status; they're dropped by a check on the attachment
bytes (a transition burst once carried `1%` and `129%` in the battery
positions).

The details that make it robust:

- The node is found by walking `/sys/class/hidraw` for Lenovo's `17EF` vendor
  and picking the interface whose report descriptor declares vendor usage page
  `0xFFA0` — nothing hard-coded to a device node. Re-docking renumbers
  `hidraw`, so the plugin re-checks and re-discovers the node (within
  `REOPEN_INTERVAL`) instead of hammering one that's gone, which also covers
  the window where a fresh node is briefly root-only while udev catches up.
- hidraw gives every open reader its own copy of each report, so the plugin
  opens a second descriptor alongside hhd's own — read-only.
- The rail is a firehose while hhd updates about every 2s, so the node is
  `O_NONBLOCK` and pending reports are drained per call. `update()` never
  blocks.
- Values are published as hhd `display` settings at
  `<plugin>.<section>.<row>` — exactly where hhd-ui reads them — and only
  when they change.
- The tab is only offered on a Legion Go (DMI check), so the plugin stays
  inert on other hardware.

A powered-off controller reads 0%, and that sticks on screen with the stream
row saying how long ago it was taken, instead of blanking.

## Requirements

- Arch with pacman + `base-devel` (makepkg) for the package route — the
  plugin itself has no dependencies beyond hhd (`python-build`,
  `python-installer`, `python-babel` may be pulled in as build deps).
- hhd 4.1.12+ plugin API (`display` setting type); in practice the install
  script just builds whatever hhd is current on the AUR.
- A Legion Go for the tab to appear; everything else (patches) is
  device-independent.

## Development

```sh
python3 tests/test_legion_battery.py   # 16 tests, no hardware needed
./probe.py                             # live readings straight from the rail
./vendor-hhd.sh /path/to/hhd-source    # vendor into any hhd tree (idempotent)
```

`tests/` covers the report parser, row formatting and stream framing, and
binds the plugin hooks against hhd's real `HHDPlugin` class — so an hhd
upgrade that changes a hook signature fails loudly instead of silently
dropping the plugin at startup. One test also compares the paths the plugin
writes against what hhd's own `parse_defaults()` generates.

`vendor-hhd.sh` is the whole integration: it copies the module into
`src/hhd/plugins/` and adds one line to the entry-points table, so any hhd
packaging (PKGBUILD `prepare()`, distro package, pip) can carry it.

## Notes

`hhd-latest-power-fixes.md` is the long-form debugging journal: hhd-ui
boot coredumps, the performance→balanced profile flip chase, and the nested
gamescope blackscreen hunt that ended in patch 0002. Worth a read if you're
chasing similar weirdness.

## License

GPL-3.0 — see [LICENSE](LICENSE).
