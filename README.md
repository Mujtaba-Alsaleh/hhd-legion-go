# legion-battery-hhd

Live Legion Go controller battery levels as a tab in
[hhd](https://github.com/hhd-dev/hhd)/hhd-ui.

hhd has nothing to say about the two Legion Go controllers: the stock
`hid-lenovo-go` kernel driver never surfaces their battery state through
evdev. This plugin reads it where it does exist - the dock's raw HID
interface - and publishes it as a **Legion Battery** tab, so the levels show
up in hhd-ui on the desktop *and* in the in-game overlay:

```
Left          100%
Right         99%
Left state    attached
Right state   detached
Stream        live · updated 1s ago
```

The tab is always there. When a controller powers off or the rail goes quiet
it keeps the last reading and says how old it is, instead of blanking or
vanishing the way a Quick Settings row can. A controller that is *out of its
dock* still reports its real level — the example above is a controller left
switched on and sitting on the desk — so it is marked `detached` rather than
hidden.

## Requirements

- hhd 4.1.12 or newer (uses the `display` setting type and hhd's plugin entry
  points) with hhd-ui 3.4.2 or newer
- no new dependencies: the plugin is stdlib plus hhd's own `hhd.plugins`
- no new udev rule and no file access changes: hhd already runs as root and
  opens the dock's hidraw node on its own

## Install

### Folded into hhd (default)

```sh
./install.sh
```

This fetches the AUR `hhd` PKGBUILD, vendors the plugin into the hhd source
tree (`vendor-hhd.sh`), builds the package with `makepkg` and installs it with
`pacman -U`. The plugin then ships *inside* the hhd package, registered in
hhd's own entry points, so there is nothing to keep in sync and an hhd upgrade
does not silently remove it.

Two details worth knowing:

- Every `patches/*.patch` in this repo is applied during the build. The repo
  ships `0001-legion-go-touchpad.patch`, which is the touchpad fix that
  previously had to be hand-edited into
  `/usr/lib/python3.14/site-packages/hhd` (and lost on every hhd upgrade). With
  the patch in the build, a rebuilt hhd keeps the fix. Add further patches the
  same way - the build applies them in filename order.
- `makepkg` may install hhd's build dependencies (`python-build`,
  `python-installer`, `python-babel`) with sudo.

Inspect what the build will do without running it:

```sh
./install.sh --dry-run     # fetch + inject, print the PKGBUILD diff
./install.sh --refresh     # re-fetch the AUR PKGBUILD first
```

### Without rebuilding (alternative)

```sh
./install.sh --local
```

Copies the module and a small hand-written `dist-info` into
`~/.local/share/legion-battery-hhd` and adds that directory to hhd's
`PYTHONPATH` through a systemd drop-in. No package build, so this also works
where hhd is not a pacman package. The drop-in *repeats* the `PYTHONPATH`
entries your other drop-ins already set (systemd lets the last assignment win,
so a naive drop-in would break the adjustor/TDP one).

Pick one route, not both: with the package route active the module is already
in hhd's site-packages.

## Verify

```sh
./install.sh --check
```

prints where the plugin lives, the entry point hhd found, whether the service
is up, and the values currently published to the tab.

To watch the rail directly, without hhd:

```sh
./probe.py 30
```

## After an hhd upgrade

`paru -Syu` replaces the package with a stock build, so the plugin (and the
touchpad patch) go away. Re-run `./install.sh` to get them back; `--check`
tells you when that is needed. Values live in `/etc/hhd/state.yml` and survive
the reinstall.

## Uninstall

```sh
./install.sh --uninstall    # removes the --local drop-in and directory
sudo pacman -S --noconfirm hhd   # back to stock hhd (also reverts the patches)
```

## How it works

The dock's raw (config) interface passes a 64-byte status report through to
hidraw. Of each report:

| byte         | meaning                                        |
| ------------ | ---------------------------------------------- |
| 5            | left controller battery, 0-100                |
| 7            | right controller battery, 0-100               |
| 12 bit 0     | left controller docked (bit 0 = docked)       |
| 13 bit 0     | right controller docked (bit 0 = docked)      |

This layout was measured on this unit, not read from documentation, and the
tests use frames captured off the rail as fixtures. Undocking the right
controller was observed to move byte 13 from `0x02` to `0x01` while byte 12
stayed at `0x02` - so an undocked controller and one that is merely powered
off are told apart by that bit. Bytes 10 and 11 read `0x01` in every captured
report and are not interpreted.

A docked controller is not the only one the dock reports on: one left switched
on and sitting on the desk kept reporting its real level (`99%`, well after it
was undocked), because it stays in radio contact with the tablet. The tab
therefore shows a level for a detached controller and marks it detached, rather
than hiding it. Only the frames during the re-dock transition are unreliable.

The same endpoint also carries other 64-byte messages with the same report id,
which are button/axis traffic rather than status. They are dropped by a check
on the attachment bytes, because a transition burst once carried `1%` and
`129%` in the battery positions.

- The node is found by walking `/sys/class/hidraw` for Lenovo's `17EF` vendor
  and picking the interface whose report descriptor declares the vendor usage
  page `0xFFA0` (the other two are Generic Desktop), so nothing is hard-coded
  to a device node. Docking and undocking the controllers makes the kernel
  create fresh hid devices and renumber `hidraw`, so the plugin re-checks that
  the node it holds is still the rail and re-discovers it (within
  `REOPEN_INTERVAL`) instead of retrying a node that no longer exists. A fresh
  node can also briefly be root-only while udev catches up, which the same
  retry handles.
- hidraw gives every open reader its own copy of each report, so the plugin
  opens a second descriptor alongside hhd's own. It only ever reads.
- The rail is a firehose while hhd's update loop runs about every 2s, so the
  node is opened `O_NONBLOCK` and whatever reports are pending are drained per
  call. `update()` never blocks.
- Values are written into hhd's config as `display` settings, addressed as
  `<plugin>.<section>.<row>` - the exact path hhd-ui reads out of the polled
  state - and only when they actually change.
- The tab is only offered on a Legion Go (checked via DMI), so the plugin is
  inert on other hardware.

Battery levels are what the dock reports at that moment, which includes while
a controller is undocked but in radio range. A controller that is powered off
reads 0%, and that reading stays on screen rather than blanking, with the
stream row saying how long ago it was taken.

## Development

```sh
python3 tests/test_legion_battery.py   # 16 tests, no hardware needed
./probe.py                             # live readings
./vendor-hhd.sh /path/to/hhd-source    # vendor into any hhd tree (idempotent)
```

`tests/` covers the report parser, row formatting and stream framing, and
binds the plugin hooks against hhd's real `HHDPlugin` class so an hhd upgrade
that changes a hook signature fails loudly instead of the plugin being dropped
at startup. One test compares the paths the plugin writes with the ones hhd's
own `parse_defaults()` generates for the plugin's settings.

`vendor-hhd.sh` is the whole integration: it copies the module into
`src/hhd/plugins/` and adds one line to the `[project.entry-points."hhd.plugins"]`
table, so any hhd packaging (a PKGBUILD `prepare()`, a distro package, a
`pip install`) can carry the plugin with it.
