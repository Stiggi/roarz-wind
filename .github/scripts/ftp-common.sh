# Shared FTPS settings for ftp-wind.sh (copy of .github/scripts/ftp-common.sh in the roarz app repository, which uses
# it for its own deployment). Source it, don't run it.
#
# Env: FTP_HOST, FTP_USER, FTP_PASSWORD, FTP_PATH (remote project dir, e.g. "/").
# Testing: DRY_RUN=1 prints the lftp commands instead of connecting.

# Run an lftp command file against the server.
lftp_run() {
  if [ "${DRY_RUN:-}" = 1 ]; then
    cat "$1"
    return 0
  fi
  # -f cannot be combined with a host argument, so the command file is sourced via -e.
  LFTP_PASSWORD="$FTP_PASSWORD" lftp -u "$FTP_USER" --env-password -e "source \"$1\"; bye" "ftp://$FTP_HOST"
}

# Connection settings and the project directory; every command file starts with these.
settings() {
  cat <<SET
set cmd:fail-exit yes
set ftp:ssl-force yes
set ftp:ssl-protect-data yes
set ssl:verify-certificate yes
set net:timeout 30
set net:max-retries 3
set net:reconnect-interval-base 5
cd "$FTP_PATH"
SET
}
