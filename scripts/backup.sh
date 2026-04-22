#!/usr/bin/env bash
set -e

DEST=/opt/backups
mkdir -p "$DEST"
STAMP=$(date +%Y%m%d)

tar -czf "$DEST/calibrate-$STAMP.tar.gz" \
  /opt/calibrate/backend-data \
  /opt/calibrate/minio-data

# Keep last 7 days
find "$DEST" -name "calibrate-*.tar.gz" -mtime +7 -delete

echo "Backup done: calibrate-$STAMP.tar.gz"
