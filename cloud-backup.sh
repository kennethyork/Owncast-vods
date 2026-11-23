#!/bin/sh
# Copies finished VODs to any rclone remote (Dropbox, Google Drive, S3, B2,
# OneDrive, SFTP, ...). Runs in the "backup" container.
# Only COPIES: VODs deleted locally (RETENTION_DAYS) stay in the cloud.

REMOTE_NAME="${BACKUP_REMOTE:-dropbox}"
DEST="$REMOTE_NAME:${BACKUP_FOLDER:-Owncast VODs}"
INTERVAL=$(( ${BACKUP_INTERVAL_MIN:-15} * 60 ))
KEEP_DAYS="${BACKUP_RETENTION_DAYS:-0}"

echo "cloud backup: /vods -> $DEST every ${BACKUP_INTERVAL_MIN:-15} min"
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
    echo "rclone remote '$REMOTE_NAME' is not set up yet - see README (Cloud backup)"
  fi
  sleep "$INTERVAL"
done
