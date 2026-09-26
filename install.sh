#!/usr/bin/env bash
# Install the Legion Battery hhd plugin.
#
#   ./install.sh            fold the plugin into a rebuilt hhd package (recommended)
#   ./install.sh --local    install it next to hhd via PYTHONPATH, no rebuild
#   ./install.sh --check    report whether the running hhd has the plugin
#   ./install.sh --uninstall
#
# The default route rebuilds hhd from the AUR PKGBUILD with the plugin vendored
# into the source tree (vendor-hhd.sh) and any patches in patches/ applied, so
# the installed package ships the plugin and hhd's own entry-point discovery
# finds it. That survives hhd upgrades, unlike editing site-packages by hand.

set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
NAME="legion-battery-hhd"
MODULE="legion_battery"
BUILD_DIR="$HERE/build/hhd"
LOCAL_DIR="${LEGION_BATTERY_DIR:-$HOME/.local/share/$NAME}"
DROPIN_DIR="/etc/systemd/system/hhd@.service.d"
DROPIN="$DROPIN_DIR/zz-$MODULE.conf"
UNIT="hhd@$(id -un).service"
AUR_PKGBUILD_URL="https://aur.archlinux.org/cgit/aur.git/plain/PKGBUILD?h=hhd"

note() { printf '\033[1;36m::\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }
die() {
	printf '\033[1;31mxx\033[0m %s\n' "$*" >&2
	exit 1
}
have() { command -v "$1" >/dev/null 2>&1; }
as_root() { sudo "$@"; }

usage() {
	cat <<EOF
$NAME - Legion Go controller battery levels as an hhd plugin

usage: $(basename -- "$0") [route] [options]

routes (default: package)
  --local          install the plugin on hhd's PYTHONPATH instead of
                   rebuilding the package; survives hhd upgrades too, and is
                   the quickest way to try it
  --uninstall      undo the chosen route

options
  --check          report whether the running hhd has the plugin, and what
                   the tab is currently showing
  --refresh        re-fetch the AUR hhd PKGBUILD before building
  --dry-run        prepare the build directory and show the PKGBUILD changes
                   without building or installing anything
  -h, --help       this text

files
  legion_battery.py            the plugin
  vendor-hhd.sh                injects it into an hhd source tree
  patches/*.patch              applied to hhd during the package build
  build/hhd/                   the package build directory (gitignored)
EOF
}

# ---------------------------------------------------------------- route A ----

plugin_version() {
	sed -n 's/^version = "\(.*\)"$/\1/p' "$HERE/pyproject.toml" | head -1
}

# Write the prepare() step that vendors the plugin (and applies patches) into
# the hhd source tree. AUR's PKGBUILD is kept pristine as PKGBUILD.aur and
# PKGBUILD is regenerated from it every run, so this is idempotent and picks up
# upstream PKGBUILD changes on --refresh.
inject_prepare() {
	local pristine="$BUILD_DIR/PKGBUILD.aur"
	local body="$BUILD_DIR/.prepare-body"
	local has_patches=()
	local patch

	[ -f "$pristine" ] || die "missing $pristine (re-run with --refresh)"

	{
		echo "  # injected by $NAME: fold the Legion Battery plugin into hhd"
		echo "  for _src in \"\$srcdir\"/hhd-*; do"
		echo "    [ -f \"\$_src/pyproject.toml\" ] || continue"
		for patch in "$BUILD_DIR"/patches/*.patch; do
			[ -e "$patch" ] || continue
			has_patches+=1
			echo "    patch -p1 -d \"\$_src\" -i \"\$startdir/patches/$(basename -- "$patch")\""
		done
		echo "    \"\$startdir/vendor-hhd.sh\" \"\$_src\""
		echo "  done"
	} >"$body"

	if [ ${#has_patches[@]} -eq 0 ]; then
		warn "no patches/*.patch found - a rebuilt hhd will NOT include your touchpad fix"
	fi

	if grep -qE '^prepare\(\)[[:space:]]*\{' "$pristine"; then
		# Upstream already has a prepare(): add our steps to the end of it.
		awk -v body="$body" '
			/^prepare\(\)[[:space:]]*\{/ { in_fn = 1; print; next }
			in_fn && /^\}[[:space:]]*$/ {
				print ""
				while ((getline line < body) > 0) print line
				close(body)
				in_fn = 0
				print
				next
			}
			{ print }
		' "$pristine" >"$BUILD_DIR/PKGBUILD"
	else
		{
			cat "$pristine"
			echo ""
			echo "prepare() {"
			cat "$body"
			echo "}"
		} >"$BUILD_DIR/PKGBUILD"
	fi

	grep -q "injected by $NAME" "$BUILD_DIR/PKGBUILD" ||
		die "failed to inject prepare() into the PKGBUILD - inspect $BUILD_DIR/PKGBUILD"
	rm -f "$body"
}

package_build() {
	local refresh="${1:-}"

	have makepkg || die "makepkg not found - install base-devel (sudo pacman -S --needed base-devel)"
	have curl || die "curl not found"
	mkdir -p "$BUILD_DIR/patches"
	cp -f "$HERE/$MODULE.py" "$HERE/vendor-hhd.sh" "$BUILD_DIR/"
	rm -f "$BUILD_DIR"/patches/*.patch
	# shellcheck disable=SC2015
	[ "$(ls -1 "$HERE"/patches/*.patch 2>/dev/null | wc -l)" -gt 0 ] &&
		cp -f "$HERE"/patches/*.patch "$BUILD_DIR/patches/"

	if [ -n "$refresh" ] || [ ! -f "$BUILD_DIR/PKGBUILD.aur" ]; then
		note "fetching the AUR hhd PKGBUILD"
		curl -fsSL --retry 3 -o "$BUILD_DIR/PKGBUILD.aur.part" "$AUR_PKGBUILD_URL" ||
			die "could not fetch $AUR_PKGBUILD_URL"
		mv "$BUILD_DIR/PKGBUILD.aur.part" "$BUILD_DIR/PKGBUILD.aur"
	fi
	inject_prepare

	local installed aur
	installed="$(pacman -Q hhd 2>/dev/null | awk '{print $2}' || true)"
	aur="$(sed -n 's/^pkgver=//p' "$BUILD_DIR/PKGBUILD.aur" | head -1)"

	if [ -n "$dryrun" ]; then
		note "prepared $BUILD_DIR (dry run, nothing built or installed)"
		note "vendored files:"
		(cd "$BUILD_DIR" && ls -1 patches 2>/dev/null | sed 's/^/  patches\//'; ls -1 "$MODULE.py" vendor-hhd.sh | sed 's/^/  /')
		note "PKGBUILD changes:"
		diff -u "$BUILD_DIR/PKGBUILD.aur" "$BUILD_DIR/PKGBUILD" | sed 's/^/  /' || true
		return 0
	fi

	note "building hhd $aur (installed: ${installed:-none})"
	if [ -n "$installed" ] && [ "${installed%-*}" != "$aur" ]; then
		warn "this will upgrade hhd from $installed to $aur (the plugin is kept)"
	fi
	note "makepkg may ask for sudo to install build dependencies"

	(cd "$BUILD_DIR" && makepkg -s --noconfirm --clean --force) ||
		die "makepkg failed - see $BUILD_DIR"

	local pkg
	pkg="$(ls -t "$BUILD_DIR"/hhd-*.pkg.tar.* 2>/dev/null | head -1)"
	[ -n "$pkg" ] || die "no package produced in $BUILD_DIR"

	# Fail before touching the system if the plugin is not actually in it.
	if have bsdtar; then
		if ! bsdtar -tf "$pkg" | grep -q "site-packages/hhd/plugins/$MODULE.py"; then
			die "the built package does not contain the plugin - inspect $BUILD_DIR"
		fi
		note "package contains hhd/plugins/$MODULE.py"
	fi

	note "installing $(basename -- "$pkg")"
	as_root pacman -U --noconfirm "$pkg"
	as_root systemctl restart "$UNIT"
	sleep 1
	check_installed
}

# --------------------------------------------------------------- route B ----

# Install the module plus a hand-written dist-info (this box has no pip, and
# hhd only needs importlib.metadata to see the entry point).
install_local() {
	local version dist
	version="$(plugin_version)"
	dist="$LOCAL_DIR/${NAME//-/_}-$version.dist-info"

	mkdir -p "$LOCAL_DIR" "$dist"
	install -m 644 "$HERE/$MODULE.py" "$LOCAL_DIR/$MODULE.py"
	cat >"$dist/METADATA" <<-EOF
		Metadata-Version: 2.1
		Name: $NAME
		Version: $version
		Summary: Legion Go controller battery tab for hhd-ui
		Requires-Python: >=3.10
	EOF
	cat >"$dist/entry_points.txt" <<-EOF
		[hhd.plugins]
		$MODULE = $MODULE:autodetect
	EOF
	cat >"$dist/WHEEL" <<-EOF
		Wheel-Version: 1.0
		Generator: $NAME install.sh
		Root-Is-Purelib: true
		Tag: py3-none-any
	EOF
	printf '%s\n' "$NAME" >"$dist/INSTALLER"
	printf '%s\n' "$MODULE" >"$dist/top_level.txt"
	note "installed $MODULE $version into $LOCAL_DIR"
	write_dropin
}

# hhd's service already has a PYTHONPATH drop-in for the adjustor patch, and
# systemd lets the last assignment win, so our drop-in repeats every existing
# entry instead of clobbering them.
write_dropin() {
	local current ours existing entry
	current="$(systemctl show -p Environment --value "$UNIT" 2>/dev/null || true)"
	existing=""
	for entry in $(printf '%s\n' "$current" | tr ' ' '\n' | sed -n 's/^PYTHONPATH=//p' | tr ':' ' '); do
		[ "$entry" = "$LOCAL_DIR" ] && continue
		case " $existing " in
		*" $entry "*) continue ;;
		esac
		existing="${existing:+$existing:}$entry"
	done
	ours="$LOCAL_DIR"
	[ -n "$existing" ] && ours="$LOCAL_DIR:$existing"

	as_root mkdir -p "$DROPIN_DIR"
	printf '[Service]\n# Managed by %s (install.sh --local); remove to uninstall.\nEnvironment="PYTHONPATH=%s"\n' \
		"$NAME" "$ours" |
		as_root tee "$DROPIN" >/dev/null
	note "wrote $DROPIN"
	note "PYTHONPATH=$ours"
	as_root systemctl daemon-reload
	as_root systemctl restart "$UNIT"
	sleep 1
	check_installed
}

# ----------------------------------------------------------------- check ----

check_installed() {
	local module_paths
	module_paths="$(ls -1 /usr/lib/python*/site-packages/hhd/plugins/$MODULE.py 2>/dev/null | tr '\n' ' ')"
	if [ -z "$module_paths" ] && [ ! -f "$LOCAL_DIR/$MODULE.py" ]; then
		note "the plugin is NOT installed"
		return 1
	fi
	note "plugin module: ${module_paths:-$LOCAL_DIR/$MODULE.py}"

	if [ -n "$module_paths" ]; then
		pacman -Qo "$module_paths" 2>/dev/null | sed 's/^/  owner: /' || true
	fi

	local entry
	entry="$(sed -n '/^\[hhd\.plugins\]/,/^\[/p' /usr/lib/python*/site-packages/hhd-*.dist-info/entry_points.txt 2>/dev/null |
		grep "^$MODULE" || true)"
	if [ -n "$entry" ]; then
		note "entry point: $entry"
	elif [ -n "$module_paths" ]; then
		warn "entry point missing from hhd's dist-info - rerun ./install.sh to rebuild"
	else
		note "entry point comes from $LOCAL_DIR (PYTHONPATH route)"
	fi

	note "service: $(systemctl is-active "$UNIT" 2>/dev/null || echo unknown)"
	local logs
	logs="$(journalctl -u "$UNIT" -b --no-pager 2>/dev/null || true)"
	if [ -n "$logs" ]; then
		# hhd announces every provider it loaded, which is where a missing
		# plugin shows up.
		printf '%s\n' "$logs" | grep -m1 "plugin providers" | sed 's/^/  /' || true
		printf '%s\n' "$logs" | grep "legion-battery:" | tail -3 | sed 's/^/  /' || true
	fi
	note "current tab values (from hhd's API):"
	if curl -sf --max-time 3 --unix-socket /run/hhd/api http://localhost/api/v1/state 2>/dev/null |
		python3 -c '
import json, sys
try:
    rows = json.load(sys.stdin).get("legion_battery", {}).get("settings", {})
except Exception:
    sys.exit(1)
if not rows:
    print("  (the plugin is loaded but has published nothing yet)")
for key, value in rows.items():
    print(f"  {key:<11} {value}")
'; then
		:
	else
		warn "could not read hhd's API over /run/hhd/api - is $UNIT running?"
	fi
}

# ------------------------------------------------------------- uninstall ----

uninstall() {
	if [ -f "$DROPIN" ]; then
		as_root rm -f "$DROPIN"
		as_root systemctl daemon-reload
		note "removed $DROPIN"
	fi
	if [ -d "$LOCAL_DIR" ]; then
		# hhd runs as root and imports the module from here, so it owns the
		# __pycache__ it leaves behind.
		as_root rm -rf "$LOCAL_DIR"
		note "removed $LOCAL_DIR"
	fi
	note "to drop the plugin from the package too, reinstall upstream hhd:"
	note "  sudo pacman -S --noconfirm hhd   # note: this also reverts patches/"
	as_root systemctl restart "$UNIT" 2>/dev/null || true
}

# ------------------------------------------------------------------ main ----

route="package"
refresh=""
dryrun=""
while [ $# -gt 0 ]; do
	case "$1" in
	--local) route="local" ;;
	--check) route="check" ;;
	--uninstall) route="uninstall" ;;
	--refresh) refresh="1" ;;
	--dry-run) dryrun="1" ;;
	-h | --help)
		usage
		exit 0
		;;
	*) die "unknown argument '$1' (try --help)" ;;
	esac
	shift
done

case "$route" in
package) package_build "$refresh" ;;
local) install_local ;;
check) check_installed || true ;;
uninstall) uninstall ;;
esac
