#!/bin/sh
# Copies finished VODs to any rclone remote (Dropbox, Google Drive, S3, B2,
# OneDrive, SFTP, ...). Runs in the "backup" container.
# Only COPIES: VODs deleted locally (RETENTION_DAYS) stay in the cloud.
#
# Also serves a read-only WebDAV endpoint at 127.0.0.1:CLOUD_PORT (default
# 8091) so the recorder can list and stream cloud-only VODs on the /vods page.

REMOTE_NAME="${BACKUP_REMOTE:-dropbox}"
DEST="$REMOTE_NAME:${BACKUP_FOLDER:-Owncast VODs}"
INTERVAL=$(( ${BACKUP_INTERVAL_MIN:-15} * 60 ))
KEEP_DAYS="${BACKUP_RETENTION_DAYS:-0}"
CLOUD_PORT="${CLOUD_PORT:-8091}"

echo "cloud backup: /vods -> $DEST every ${BACKUP_INTERVAL_MIN:-15} min"

# Start the WebDAV server in the background (read-only, local only).
# Restarts automatically if it crashes.
start_webdav() {
  while true; do
    rclone serve webdav "$DEST" \
      --addr "127.0.0.1:${CLOUD_PORT}" \
      --read-only \
      --no-modtime \
      --vfs-cache-mode off \
      2>&1 | sed 's/^/[webdav] /' || true
    echo "[webdav] restarting in 10s..."
    sleep 10
  done
}
start_webdav &

while true; do
  if rclone listremotes 2>/dev/null | grep -qx "$REMOTE_NAME:"; then
    # --min-age 3m skips the file that is still being recorded.
    # Thumbnails (.jpg) go too, so cloud-only VODs keep their preview image.
    rclone copy /vods "$DEST" \
      --include 'vod_*.mp4' --include 'vod_*.jpg' --min-age 3m \
      --transfers 2 --dropbox-chunk-size 64M \
      --retries 5 --low-level-retries 20 \
      --stats 10m --stats-one-line -v
    if [ "$KEEP_DAYS" -gt 0 ] 2>/dev/null; then
      rclone delete "$DEST" --include 'vod_*.mp4' --include 'vod_*.jpg' \
        --min-age "${KEEP_DAYS}d" -v
    fi
  else
    echo "rclone remote '$REMOTE_NAME' is not set up yet - see README (Cloud backup)"
  fi
  sleep "$INTERVAL"
done
