#!/usr/bin/env bash
# Tests for .github/scripts/ftp-wind.sh (dry run, no server): the switch of latest.json happens only after a complete
# upload, and only old run folders are deleted. Run: bash tests/ftp_wind.test.sh
set -euo pipefail

script=$(cd "$(dirname "$0")/.." && pwd)/.github/scripts/ftp-wind.sh
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
export DRY_RUN=1 FTP_PATH=/ WIND_DIR=public/wind # a sub-folder of the FTP root; the default "." is tested below

build=$tmp/build
mkdir -p "$build/2026101212/map" "$build/2026101212/tiles/2_5"
for f in meta.json map/step_000.png map/step_001.png tiles/2_5/wind_000.png tiles/2_5/weather_000.png; do echo x > "$build/2026101212/$f"; done
echo '{"run": "2026101212", "previous": "2026101209"}' > "$build/latest.json"
(echo "public/wind/2026101212/"; cd "$build/2026101212" && find . -type f -printf 'public/wind/2026101212/%P\n') > "$tmp/remote.full"
printf '2026101206/\n2026101209/\n2026101212/\n.htaccess\nlatest.json\n' > "$tmp/remote.dirs"

fail() { echo "FAIL: $*" >&2; exit 1; }

# fetch creates the target folder (build/ does not exist yet in the workflow) and never fails without a latest.json.
"$script" fetch "$tmp/new/published-latest.json" > "$tmp/out" 2>&1 || fail "fetch failed: $(cat "$tmp/out")"
[ -d "$tmp/new" ] || fail 'fetch did not create the target folder'
[ ! -e "$tmp/new/published-latest.json" ] || fail 'fetch wrote a file without a published latest.json'

# Complete upload: mirror → verify → switch → cleanup, in this order.
DRY_RUN_REMOTE_LIST=$tmp/remote.full DRY_RUN_REMOTE_DIRS=$tmp/remote.dirs "$script" publish "$build" > "$tmp/out" 2>&1 || fail "complete upload failed: $(cat "$tmp/out")"
line() { grep -n -- "$1" "$tmp/out" | head -1 | cut -d: -f1; }
[ "$(line '/web/.htaccess" -o "public/wind/.htaccess"')" -lt "$(line 'latest.json.tmp')" ] || fail '.htaccess not uploaded before the switch'
[ "$(line 'mirror -R')" -lt "$(line '^find ')" ] || fail 'verify before upload'
[ "$(line '^find ')" -lt "$(line 'latest.json.tmp')" ] || fail 'switch before verify'
[ "$(line 'mv "public/wind/latest.json.tmp" "public/wind/latest.json"')" -lt "$(line 'rm -r -f')" ] || fail 'cleanup before switch'
grep -q 'rm -r -f "public/wind/2026101206"' "$tmp/out" || fail 'old run not deleted'
! grep -q 'rm -r -f "public/wind/2026101209"' "$tmp/out" || fail 'previous run deleted'
! grep -q 'rm -r -f "public/wind/2026101212"' "$tmp/out" || fail 'new run deleted'
! grep -q 'rm -r -f "public/wind/.htaccess"' "$tmp/out" || fail '.htaccess deleted'

# One file missing on the server: no switch, no cleanup, exit code ≠ 0.
grep -v 'step_001' "$tmp/remote.full" > "$tmp/remote.partial"
if DRY_RUN_REMOTE_LIST=$tmp/remote.partial DRY_RUN_REMOTE_DIRS=$tmp/remote.dirs "$script" publish "$build" > "$tmp/out" 2>&1; then
  fail 'incomplete upload was published'
fi
! grep -qE '^(put|mv) .*latest\.json' "$tmp/out" || fail 'latest.json touched after an incomplete upload'
! grep -q 'rm -r' "$tmp/out" || fail 'cleanup after an incomplete upload'

# Missing .htaccess: nothing happens.
if HTACCESS=$tmp/none DRY_RUN_REMOTE_LIST=$tmp/remote.full "$script" publish "$build" > "$tmp/out" 2>&1; then fail 'published without .htaccess'; fi
! grep -q 'mirror' "$tmp/out" || fail 'upload without .htaccess'

# Invalid run name in latest.json: nothing happens.
echo '{"run": "../x"}' > "$build/latest.json"
if "$script" publish "$build" > "$tmp/out" 2>&1; then fail 'invalid run accepted'; fi
! grep -q 'mirror' "$tmp/out" || fail 'upload despite invalid run'

# Default: the FTP account's root is the web folder of wind.roarz.net (WIND_DIR=.): paths relative to it, no mkdir.
echo '{"run": "2026101212", "previous": "2026101209"}' > "$build/latest.json"
sed 's|^public/wind/|./|' "$tmp/remote.full" > "$tmp/remote.root"
env -u WIND_DIR DRY_RUN_REMOTE_LIST=$tmp/remote.root DRY_RUN_REMOTE_DIRS=$tmp/remote.dirs "$script" publish "$build" > "$tmp/out" 2>&1 \
  || fail "upload with WIND_DIR=. failed: $(cat "$tmp/out")"
! grep -q '^mkdir' "$tmp/out" || fail 'mkdir with WIND_DIR=.'
grep -q 'mirror -R .* "./2026101212"' "$tmp/out" || fail 'wrong target with WIND_DIR=.'
grep -q '/web/.htaccess" -o "./.htaccess"' "$tmp/out" || fail '.htaccess not uploaded with WIND_DIR=.'
grep -q 'mv "./latest.json.tmp" "./latest.json"' "$tmp/out" || fail 'no switch with WIND_DIR=.'
grep -q 'rm -r -f "./2026101206"' "$tmp/out" || fail 'old run not deleted with WIND_DIR=.'
! grep -q 'rm -r -f "./2026101209"' "$tmp/out" || fail 'previous run deleted with WIND_DIR=.'

echo "ftp-wind.sh: all tests passed"
