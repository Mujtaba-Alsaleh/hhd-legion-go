# hhd latest power fixes — journey log

Work session: 2026-10-13, Legion Go (archlinux, kernel 7.2.9, GNOME 51 Wayland).
Two issues handled: (A) hhd-ui SIGTRAP coredumps in the journal at every boot,
(B) power profile flips from `performance` back to `balanced` after a while.

---

## Phase A — hhd-ui coredump (FIXED)

### Symptom
`coredumpctl` / journal showed `Process N (hhd-ui) of user 0 dumped core` —
SIGTRAP, uid 0, PID ~740, at every boot right at session start, plus some
uid-1000 SIGTRAP/SIGBUS entries from manual runs. Present since 2026-08-29
(the day hhd 4.1.12 was installed) — **not** caused by the 2026-10-09 system
update.

### Who launches it as root
The hhd daemon itself. At startup it probes the overlay version to store
`hhd.settings.version_ui`:

- `hhd/__main__.py:444-448` → `get_overlay_version(exe)`
- `hhd/plugins/overlay/overlay.py` → `subprocess.run([fn, "--version"], ...)`

runs `~/.local/bin/hhd-ui --version` **as root, with no display env**.
(`install.sh` merely downloads the release AppImage into `~/.local/bin` as the
user; the daemon executes it. Real overlay launches drop privileges via
`user=uid, group=gid` — those are the uid-1000 runs.)

### Exact crash cause (baseline repro, 2026-10-13)
```
$ sudo env -u DISPLAY -u WAYLAND_DISPLAY ~/.local/bin/hhd-ui --version
FATAL: Running as root without --no-sandbox is not supported.  (electron_main_delegate.cc:224)
→ Trace/breakpoint trap, rc=133
```
Electron refuses to run as root and hard-aborts **before app code can print
the version**. As the user (oma, with display) `--version` worked fine
(printed `3.4.0`, rc=0) — only the root probe path was fatal.

### Upstream already fixed it (post-v3.4.2)
Commit `59a6cb6` (2026-08-07, hhd-ui) *"do not start overlay to report
version, avoiding the coredump"*:

- adds `electron/scripts/after-pack.js`: renames the Electron binary to
  `hhd-ui-bin` and installs a tiny `#!/bin/sh` wrapper as `hhd-ui` that
  answers `--version` with the version string **without ever starting
  Electron**, otherwise `exec`s `hhd-ui-bin`;
- removes the (useless-for-root) `--version` handler from `electron/main.js`.

The installed AppImage was **v3.4.2** (release asset, built ~2026-07-11) which
predates the fix. `main` also carries QAM/keyboard fixes and Electron ^43.

### Build (no system/global package installs)
- Clone: `~/Git/hhd-ui` @ `c6bdb2a` (main, 2026-09-14). Pristine — the
  build-time version sed was reverted after building, so the clone stays in
  `$HOME/Git` (no local modifications; last 3 upstream commits touch COPR
  `.spec` files only and don't affect the electron-builder path).
- Recipe (mirrors `.github/workflows/publish-electron.yml`):
  ```
  VERSION=$(grep -E '"version": "[0-9\.]+"' package.json | head -1 | grep -Eo '[0-9\.]+')
  sed -i "s|\"version\": \"1.0.0\"|\"version\": \"$VERSION\"|" electron/package.json
  npm ci && npm run electron-build        # root: vite → electron/static/build
  cd electron && npm ci && npm run build  # electron-builder --linux → AppImage
  ```
- npm 12 notes (node 26.11.1 / npm 12.2.0 already on system, nothing
  installed globally):
  - postinstall scripts for `@swc/core`/`esbuild` are blocked by npm 12's
    allow-scripts gate — harmless, their binaries ship via optional
    dependencies (`@swc/core-linux-x64-gnu`, `@esbuild/linux-x64`) and work;
  - blocked `electron-winstaller` script is Windows-only, irrelevant;
  - `electron@43` has no postinstall anymore — electron-builder downloads the
    Electron zip itself.
- Result: `~/Git/hhd-ui/electron/dist/hhd-ui.AppImage` (131,013,890 bytes).

### Test results
| Test | v3.4.2 (old) | built main |
|---|---|---|
| `--version` as user | 3.4.0, rc=0 | 3.4.0, rc=0 |
| `--version` as root, no display | **SIGTRAP rc=133** | **3.4.0, rc=0** |
| Full UI launch as user | ok | ok (stayed up, connected `/tmp/hhd/token`) |

### Install & verification
- Backup: `~/.local/bin/hhd-ui.v3.4.2-release.bak`
- New: `~/.local/bin/hhd-ui` (built AppImage, oma:oma, +x)
- `sudo systemctl restart hhd@oma` → journal now logs
  `Handheld Daemon Version: 4.1.12` / **`Overlay Version: 3.4.0`** and **no
  new hhd-ui coredump** (only pre-existing, known-benign errors remain:
  ADJL ACPI calls — `acpi_call` module not installed; SDL mappings; evdev
  monkey-patch warning). `legion_battery` plugin loads normally.
- Remaining manual check: open the new UI once and confirm all tabs render,
  especially **Legion Battery** (fed by our vendored daemon plugin — no
  hhd-ui-side patch required; our repo's only patch,
  `patches/0001-legion-go-touchpad.patch`, targets the hhd daemon and is
  unaffected).

### Rollback
```
mv ~/.local/bin/hhd-ui ~/.local/bin/hhd-ui.new.bak
mv ~/.local/bin/hhd-ui.v3.4.2-release.bak ~/.local/bin/hhd-ui
sudo systemctl restart hhd@oma
```

---

## Phase B — profile flips performance → balanced (CLOSED — no recurrence)

**Outcome (2026-10-13 evening):** several hours of normal use after the
hhd-ui replacement — **no flip reproduced**. Working conclusion (user's
call): the flip was a side effect of the old hhd-ui (launched/crashing in the
same sessions where the flip was observed), not an independent GNOME/daemon
regression. Watchers stopped; logs kept at `/tmp/hhd-profile-watch/` until
reboot. If it ever recurs, the original analysis below still applies and the
same three-way monitoring (dbus `Set` on PowerProfiles + sysfs poll + hhd
journal) is the way to catch it.

### Established facts
- Any D-Bus `Properties.Set(ActiveProfile, …)` on `/org/freedesktop/PowerProfiles`
  (from GNOME Settings, quick settings, `powerprofilesctl`, …) reaches hhd's
  PPD shim (`adjustor/drivers/gpu/ppd.py`) → `{"type":"ppd"}` event →
  `drivers/lenovo/__init__.py` → `set_tdp_mode()`. Internal energy pushes do
  not re-enter this path (no loop).
- This boot's flip: 05:13:54 hhd applied `performance`; 05:14:12
  gnome-control-center opened; ~05:14:23 `Setting tdp mode to 'balanced'`;
  05:14:25 `performance` again; Settings closed 05:14:26.
- Ruled out: tlp/tuned/auto-cpufreq (not installed), power-profiles-daemon
  (masked). Sysfs at flip time: gamezone=`performance`, amd-pmf=`balanced`,
  `/sys/firmware/acpi/platform_profile`=`custom`.
- Leading suspects: GNOME 51 power panel sending a stray `Set('balanced')`
  when opened (regression candidates: gnome-control-center 51.1 / glib2
  2.90.1, upgraded 2026-10-09); or hhd reading a transient platform-profile
  state (`get_tdp_mode()` fallback `_last_mode or "balanced"`).

### Monitoring (live)
`/tmp/hhd-profile-watch/` — three background logs:
- `dbus.log` — `dbus-monitor` for `Set` calls with
  `arg0=/org/freedesktop/PowerProfiles` (captures sender unique name);
- `sysfs.log` — 1 s poll of gamezone + acpi platform_profile;
- `hhd.log` — `journalctl -f -u hhd@oma.service`.

Reproduction in progress: open GNOME Settings → Power with `performance`
active and observe whether/when the flip occurs; sender name maps to a
process via `sudo busctl --system status :1.xx`.

*(Watchers stopped 2026-10-13 after several hours of clean operation — see
outcome at the top of this section.)*

## Phase C — nested gamescope black screen after QAM close (FIXED)

**Symptom (2026-11-02):** inside a nested `gamescope --backend wayland`
session (Skyrim SE via Proton, Steam client on the host), opening the hhd
QAM/big-picture menu and closing it left the game on a permanent black
screen with audio continuing. Focus cycles, window resize,
minimize/maximize, and dropping `-f` never recovered it; `vkcube` under
gamescope was unaffected.

**Diagnosis highlights:**
- X-side state after the black screen was all healthy: game window
  viewable, `GAMESCOPE_FOCUSED_WINDOW`/`BASELAYER`/input focus all on the
  game — yet `SkyrimSE.exe` sat at ~7% CPU (present loop starved).
- `GAMESCOPE_FOCUSED_APP_GFX` was never set: gamescope never re-named the
  game as the active app after the overlay closed.
- Root cause: the old nested hide path used `_NET_WM_WINDOW_OPACITY=0` and
  tagged the hhd window `STEAM_GAME=5335` + `STEAM_BIGPICTURE=1`. gamescope
  keys its app/scene handling off those, and the state desynced from the
  real game — DXVK's paced present (`presentTiming: on`) then waits on
  refresh cycles gamescope stops feeding → permanent black. vkcube is
  immune (no steam appid, unpaced present).
- First fix attempt (unmap-on-close only) was insufficient; the property
  games themselves were half the problem, and Electron re-showing its
  window after startup (once opacity=0 no longer masked it) covered the
  game with a black rectangle from boot.

**Fix — `patches/0002-overlay-nested-map-unmap-only.patch`** (replaces the
non-working `0002-overlay-unmap-on-close-nested.patch`): nested path is
pure map/unmap — no opacity, no `STEAM_GAME`/`STEAM_BIGPICTURE`, no
`STEAM_INPUT_FOCUS`/`STEAM_OVERLAY` writes (those only run when a Steam
client exists); `prepare_hhd` unmaps Electron's initially-mapped window
and selects `StructureNotify` so a later Electron re-show wakes the loop;
the main loop re-unmaps every tick while the UI is closed.

**Verified:** game renders from boot; QAM open/close no longer
blackscreens (tested with Skyrim SE / Proton / nested gamescope 3.17.0).
Patch applies cleanly to pristine `v4.1.12` alongside `0001`, and
pristine+patch is byte-identical to the verified installed copy.
