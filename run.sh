#!/bin/sh
# Runs owncast-vods without Docker: the recorder, plus the cloud backup when
# COMPOSE_PROFILES=backup is set in .env.
#
#   ./run.sh           start whatever isn't running (safe any time; cron uses this)
#   ./run.sh restart   restart everything (ends the VOD being recorded - do it offline)
#   ./run.sh stop      stop everything
#   ./run.sh status    show what's running
#
# Keep it running after reboots and crashes (crontab -e):
#   @reboot     $HOME/owncast-vods/run.sh >/dev/null 2>&1
#   */5 * * * * $HOME/owncast-vods/run.sh >/dev/null 2>&1

cd "$(dirname "$0")" || exit 1
[ -f .env ] || { echo "no .env - run: cp .env.example .env" >&2; exit 1; }
set -a; . ./.env; set +a

export PATH="$HOME/bin:/usr/local/bin:/usr/bin:/bin:$PATH"  # cron has a bare PATH
export VOD_DIR="${VOD_PATH:-./vods}"
# Full path on purpose: a broken static ffmpeg in ~/bin once crashed every recording.
[ -z "$FFMPEG" ] && [ -x /usr/bin/ffmpeg ] && FFMPEG=/usr/bin/ffmpeg
export FFMPEG="${FFMPEG:-ffmpeg}"
export BACKUP_SRC="$VOD_DIR"
export RCLONE_CONFIG="${RCLONE_CONFIG:-$PWD/rclone/rclone.conf}"  # same place Docker uses
mkdir -p .run "$VOD_DIR"
ME=$(id -u)

# alive NAME PATTERN: the process in .run/NAME.pid is running and is ours. Sets $pid.
alive() {
  pid=$(cat ".run/$1.pid" 2>/dev/null) || return 1
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null &&
    tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -q "$2"
}

rotate() {  # keep logs under ~20 MB
  [ -f "$1" ] && [ "$(wc -c < "$1")" -gt 20000000 ] && mv -f "$1" "$1.1"
  return 0
}

backup_on() {
  case ",$COMPOSE_PROFILES," in *,backup,*) return 0 ;; esac
  return 1
}

start_recorder() {
  alive recorder recorder.py && return
  # A recorder started by hand (before run.sh): adopt it instead of starting a second one.
  # The [r] keeps pgrep from matching a shell whose command line mentions recorder.py.
  pid=$(pgrep -u "$ME" -f '[r]ecorder\.py' | head -1)
  if [ -n "$pid" ]; then echo "$pid" > .run/recorder.pid; return; fi
  rotate recorder.log
  setsid python3 -u recorder.py >> recorder.log 2>&1 < /dev/null &
  echo $! > .run/recorder.pid
  echo "recorder started (pid $!, ffmpeg: $FFMPEG)"
}

start_backup() {
  backup_on || return
  alive backup cloud-backup && return
  if ! command -v rclone > /dev/null; then
    echo "cloud backup is on but rclone isn't installed (README -> Cloud backup)"
    return
  fi
  rotate backup.log
  # Own process group, so stopping it also stops its WebDAV server.
  setsid sh ./cloud-backup.sh >> backup.log 2>&1 < /dev/null &
  echo $! > .run/backup.pid
  echo "cloud backup started (pid $!)"
}

stop_all() {
  if alive backup cloud-backup; then kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid"; fi
  rm -f .run/backup.pid
  # SIGTERM lets the recorder stop ffmpeg cleanly (it waits up to 20 s).
  if alive recorder recorder.py; then kill -TERM "$pid"; fi
  rm -f .run/recorder.pid
  pkill -TERM -u "$ME" -f '[r]ecorder\.py' 2>/dev/null
  i=0
  while pgrep -u "$ME" -f '[r]ecorder\.py' > /dev/null && [ $i -lt 25 ]; do sleep 1; i=$((i + 1)); done
  pkill -KILL -u "$ME" -f '[r]ecorder\.py' 2>/dev/null
  echo "stopped"
}

case "${1:-start}" in
  start) start_recorder; start_backup ;;
  stop) stop_all ;;
  restart) stop_all; start_recorder; start_backup ;;
  status)
    if alive recorder recorder.py; then echo "recorder: running (pid $pid)"; else echo "recorder: stopped"; fi
    if ! backup_on; then echo "cloud backup: off (COMPOSE_PROFILES=backup turns it on)"
    elif alive backup cloud-backup; then echo "cloud backup: running (pid $pid)"
    else echo "cloud backup: stopped"; fi ;;
  *) echo "usage: $0 [start|stop|restart|status]" >&2; exit 1 ;;
esac
