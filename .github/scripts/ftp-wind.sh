#!/usr/bin/env bash
# Publishes the wind data built by pipeline/icon_wind.py in public/wind/ of the roarz live webspace (called by
# .github/workflows/wind.yml). FTPS connection as in the roarz app deployment (ftp-common.sh), with an FTP account that
# reaches only that folder (the subdomain wind.roarz.net points there for it).
#
#   ftp-wind.sh fetch <file>     download the published latest.json to <file> (no file if there is none yet)
#   ftp-wind.sh publish <dir>    upload <dir>/<run>/ and switch <dir>/latest.json to it
#
# publish, in this order – a run that fails anywhere leaves the published data untouched:
#   1. mirror <dir>/<run>/ to <WIND_DIR>/<run>/ (a new folder: no client reads it yet)
#   2. compare the remote file list of that folder with the local one – any difference aborts
#   3. upload latest.json as latest.json.tmp and rename it to latest.json (the switch)
#   4. delete run folders other than the new run and the previous one (still used by open apps): on the server by the
#      app (POST WIND_CLEANUP_URL, seconds); over FTP (one request per file, ~13 min per run) only if that is not
#      configured or fails
#
# Env: FTP_HOST, FTP_USER, FTP_PASSWORD, FTP_PATH (see ftp-common.sh); WIND_DIR (the wind folder relative to FTP_PATH,
# default "." = the FTP account's root is that folder). The folder's .htaccess (CORS, cache headers) belongs to the
# app repository and is deployed from there; this script never touches it. WIND_CLEANUP_URL, WIND_CLEANUP_TOKEN: the
# app's cleanup endpoint (roarz: public/_deploy/wind-cleanup.php) and its token (header X-Wind-Token).
# Testing: DRY_RUN=1 prints the lftp commands; DRY_RUN_REMOTE_LIST=<file> fakes the remote listing of step 2,
# DRY_RUN_REMOTE_DIRS=<file> the run folders for step 4, DRY_RUN_CLEANUP=ok|fail the answer of the cleanup endpoint.
set -euo pipefail

. "$(dirname "$0")/ftp-common.sh"

WIND_DIR=${WIND_DIR:-.}
PARALLEL=${PARALLEL:-8}
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

cmd=${1:?usage: ftp-wind.sh fetch <file> | publish <dir>}
arg=${2:?usage: ftp-wind.sh fetch <file> | publish <dir>}

# Output of an lftp command file (a fake one when testing).
lftp_out() {
  local file=$1 fake=${2:-}
  if [ "${DRY_RUN:-}" = 1 ]; then
    cat "$file" >&2
    [ -n "$fake" ] && cat "$fake"
    return 0
  fi
  lftp_run "$file"
}

# Step 4 on the server: the app deletes the old run folders. Non-zero if not configured or the call fails.
server_cleanup() {
  [ -n "${WIND_CLEANUP_URL:-}" ] && [ -n "${WIND_CLEANUP_TOKEN:-}" ] || return 1
  if [ "${DRY_RUN:-}" = 1 ]; then
    echo "POST $WIND_CLEANUP_URL" >&2
    [ "${DRY_RUN_CLEANUP:-ok}" = ok ]
    return
  fi
  local out
  if out=$(curl --fail-with-body -sS --max-time 120 -X POST -H "X-Wind-Token: $WIND_CLEANUP_TOKEN" "$WIND_CLEANUP_URL" 2>&1); then
    echo "Server cleanup: $out"
  else
    echo "::warning::Server cleanup failed ($out) – deleting old runs over FTP." >&2
    return 1
  fi
}

case "$cmd" in
fetch)
  mkdir -p "$(dirname "$arg")" # the workflow fetches into build/, which does not exist yet
  rm -f "$arg"
  { settings; echo "get \"$WIND_DIR/latest.json\" -o \"$work/latest.json\""; } > "$work/fetch.lftp"
  if [ "${DRY_RUN:-}" != 1 ] && lftp_run "$work/fetch.lftp" > /dev/null 2>&1 && [ -s "$work/latest.json" ]; then
    cp "$work/latest.json" "$arg"
    echo "Published: $(jq -c . "$arg")"
  else
    echo "No published latest.json found."
  fi
  ;;

publish)
  dir=$arg
  run=$(jq -r '.run' "$dir/latest.json")
  previous=$(jq -r '.previous // empty' "$dir/latest.json")
  [[ "$run" =~ ^[0-9]{10}$ ]] || { echo "::error::invalid run in latest.json: $run" >&2; exit 1; }
  [ -z "$previous" ] || [[ "$previous" =~ ^[0-9]{10}$ ]] || { echo "::error::invalid previous run: $previous" >&2; exit 1; }
  [ -f "$dir/$run/meta.json" ] || { echo "::error::$dir/$run/meta.json missing" >&2; exit 1; }

  (cd "$dir/$run" && find . -type f -printf '%P\n' | LC_ALL=C sort) > "$work/local.list"
  count=$(wc -l < "$work/local.list")
  bytes=$(du -sb "$dir/$run" | cut -f1)
  echo "Uploading run $run: $count files, $((bytes / 1000000)) MB → $WIND_DIR/$run/"
  start=$(date +%s)

  # 1. Upload the run folder.
  {
    settings
    [ "$WIND_DIR" = . ] || echo "mkdir -p -f \"$WIND_DIR\""
    echo "mirror -R --parallel=$PARALLEL --no-perms --no-umask \"$dir/$run\" \"$WIND_DIR/$run\""
  } > "$work/upload.lftp"
  lftp_run "$work/upload.lftp"

  # 2. Verify: every local file must be on the server.
  { settings; echo "find \"$WIND_DIR/$run\""; } > "$work/list.lftp"
  lftp_out "$work/list.lftp" "${DRY_RUN_REMOTE_LIST:-}" > "$work/remote.raw"
  grep -v '/$' "$work/remote.raw" | sed "s|^$WIND_DIR/$run/||" | LC_ALL=C sort > "$work/remote.list" || true
  if ! cmp -s "$work/local.list" "$work/remote.list"; then
    echo "::error::Upload incomplete: $(comm -23 "$work/local.list" "$work/remote.list" | wc -l) file(s) missing on the server – latest.json not switched." >&2
    comm -23 "$work/local.list" "$work/remote.list" | head -20 >&2
    exit 1
  fi
  echo "Verified $count files in $(( $(date +%s) - start )) s."

  # 3. Switch latest.json. Renaming onto an existing file is not allowed on every FTP server, so the old one is
  #    moved aside first; clients that hit the millisecond gap keep their cached copy.
  {
    settings
    echo "put \"$dir/latest.json\" -o \"$WIND_DIR/latest.json.tmp\""
    echo "set cmd:fail-exit no"
    echo "rm -f \"$WIND_DIR/latest.json.old\""
    echo "mv \"$WIND_DIR/latest.json\" \"$WIND_DIR/latest.json.old\""
    echo "set cmd:fail-exit yes"
    echo "mv \"$WIND_DIR/latest.json.tmp\" \"$WIND_DIR/latest.json\""
    echo "set cmd:fail-exit no"
    echo "rm -f \"$WIND_DIR/latest.json.old\""
  } > "$work/switch.lftp"
  lftp_run "$work/switch.lftp"
  echo "latest.json → $run"

  # 4. Delete old run folders (only folders named like a run; .htaccess and latest.json stay) – on the server if possible.
  if ! server_cleanup; then
    { settings; echo "cd \"$WIND_DIR\""; echo "cls -1"; } > "$work/dirs.lftp"
    lftp_out "$work/dirs.lftp" "${DRY_RUN_REMOTE_DIRS:-}" | sed 's|/$||' | grep -E '^[0-9]{10}$' > "$work/dirs" || true
    {
      settings
      echo "set cmd:fail-exit no"
      while IFS= read -r d; do
        if [ "$d" != "$run" ] && [ "$d" != "$previous" ]; then
          echo "rm -r -f \"$WIND_DIR/$d\""
          echo "Deleting old run $d" >&2
        fi
      done < "$work/dirs"
    } > "$work/cleanup.lftp"
    lftp_run "$work/cleanup.lftp"
  fi
  echo "Done in $(( $(date +%s) - start )) s (kept: $run${previous:+, $previous})."
  if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
    echo "- Uploaded: $count files, $((bytes / 1000000)) MB in $(( $(date +%s) - start )) s" >> "$GITHUB_STEP_SUMMARY"
  fi
  ;;

*)
  echo "unknown command: $cmd" >&2
  exit 1
  ;;
esac
