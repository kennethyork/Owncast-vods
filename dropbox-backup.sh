#!/bin/sh
# Copies finished VODs to Dropbox with rclone. Runs in the "dropbox" container.
# Only COPIES: VODs deleted locally (retention) stay in Dropbox.

REMOTE_NAME="${DROPBOX_REMOTE:-dropbox}"
DEST="$REMOTE_NAME:${DROPBOX_FOLDER:-Owncast VODs}"
INTERVAL=$(( ${DROPBOX_INTERVAL_MIN:-15} * 60 ))
KEEP_DAYS="${DROPBOX_RETENTION_DAYS:-0}"

echo "dropbox backup: /vods -> $DEST every ${DROPBOX_INTERVAL_MIN:-15} min"
while true; do
  if rclone listremotes 2>/dev/null | grep -qx "$REMOTE_NAME:"; then
    # --min-age 3m skips the file that is still being recorded
    rclone copy /vods "$DEST" \
      --include 'vod_*.mp4' --min-age 3m \
      --transfers 2 --dropbox-chunk-size 64M \
      --retries 5 --low-level-retries 20 \
      --stats 10m --stats-one-line -v
    if [ "$KEEP_DAYS" -gt 0 ] 2>/dev/null; then
      rclone delete "$DEST" --include 'vod_*.mp4' --min-age "${KEEP_DAYS}d" -v
    fi
  else
    echo "rclone remote '$REMOTE_NAME' is not set up yet - see README (Dropbox backup)"
  fi
  sleep "$INTERVAL"
done
