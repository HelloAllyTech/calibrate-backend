#!/usr/bin/env bash
set -e

if [ -z "$MINIO_PASSWORD" ]; then
  echo "Error: MINIO_PASSWORD env var must be set"
  exit 1
fi

cd /opt/calibrate

docker compose -f calibrate-backend/docker-compose.yml exec minio \
  mc alias set local http://localhost:9000 calibrate "$MINIO_PASSWORD"

docker compose -f calibrate-backend/docker-compose.yml exec minio \
  mc mb local/calibrate-output --ignore-existing

docker compose -f calibrate-backend/docker-compose.yml exec minio \
  mc anonymous set none local/calibrate-output

echo "MinIO bucket ready."
