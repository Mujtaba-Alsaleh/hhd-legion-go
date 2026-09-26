#!/usr/bin/env bash
# Vendor this plugin into an hhd source tree.
#
#   vendor-hhd.sh <hhd-source-tree>
#
# Copies legion_battery.py into <tree>/src/hhd/plugins/ and registers it in
# the [project.entry-points."hhd.plugins"] table of <tree>/pyproject.toml, so
# the wheel hhd builds - and therefore the installed package - ships the plugin
# and hhd's own entry-point discovery finds it. Idempotent: re-running is a
# no-op, so it is safe to call on every rebuild and across hhd version bumps.
#
# Nothing else in the tree is touched.

set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
NAME="legion_battery"
ENTRY="${NAME} = \"hhd.plugins.${NAME}:autodetect\""

if [ $# -ne 1 ]; then
	echo "usage: $(basename -- "$0") <hhd-source-tree>" >&2
	exit 2
fi

if [ ! -d "$1" ]; then
	echo "error: '$1' is not a directory" >&2
	exit 1
fi
tree="$(cd -- "$1" && pwd)"

if [ ! -f "$tree/pyproject.toml" ]; then
	echo "error: $tree/pyproject.toml not found - is this an hhd source tree?" >&2
	exit 1
fi
if [ ! -d "$tree/src/hhd/plugins" ]; then
	echo "error: $tree/src/hhd/plugins not found - unexpected hhd layout" >&2
	exit 1
fi

# 1. The plugin module itself.
install -m 644 "$HERE/legion_battery.py" "$tree/src/hhd/plugins/${NAME}.py"
echo "installed src/hhd/plugins/${NAME}.py"

# 2. The entry point, unless it is already registered.
if grep -qE "^${NAME}[[:space:]]*=" "$tree/pyproject.toml"; then
	echo "entry point already registered: ${ENTRY}"
else
	awk -v entry="$ENTRY" '
		/^\[project\.entry-points\."hhd\.plugins"\][[:space:]]*$/ {
			in_table = 1
			print
			print entry
			next
		}
		in_table && /^\[/ { in_table = 0 }
		{ print }
	' "$tree/pyproject.toml" >"$tree/pyproject.toml.tmp"
	mv "$tree/pyproject.toml.tmp" "$tree/pyproject.toml"
	echo "registered entry point: ${ENTRY}"
fi

# 3. Verify, so a silent miss can never become a mystery missing tab.
if ! grep -q "^${NAME} = " "$tree/pyproject.toml"; then
	echo "error: entry point not present in $tree/pyproject.toml" >&2
	echo "       check the [project.entry-points.\"hhd.plugins\"] table header" >&2
	exit 1
fi
if ! grep -q "hhd\.plugins\.${NAME}" "$tree/pyproject.toml"; then
	echo "error: entry point does not point at hhd.plugins.${NAME}" >&2
	exit 1
fi
