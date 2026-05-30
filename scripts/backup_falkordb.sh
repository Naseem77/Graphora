#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${S3_BUCKET:-}" ]]; then
  echo "S3_BUCKET is required" >&2
  exit 1
fi

redis-cli -h "${FALKORDB_HOST:-falkordb}" -p "${FALKORDB_PORT:-6379}" BGSAVE
sleep 5
aws s3 cp /data/dump.rdb "s3://${S3_BUCKET}/falkordb/dump-$(date +%Y%m%d-%H%M).rdb"
