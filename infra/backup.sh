#!/usr/bin/env bash
# infra/backup.sh — nightly Postgres dump to S3.
#
# Run from anywhere; it cds to its own directory. Reads infra/.env.prod.
# Scheduling and the IAM/S3 setup are in DEPLOY-AWS.md section 10.
#
#   ./backup.sh            # dump, verify, upload
#
# What it protects: the database (workflows, runs, audit trail, chunks and the
# ENCRYPTED credentials). What it does not: the uploaded files in the documents S3 bucket
# (re-uploadable; chunks already live in the DB) and INTEGRATION_ENCRYPTION_KEY,
# which must be stored somewhere this bucket is not — without it the restored
# credentials are permanently undecryptable.
#
# Fails loudly on purpose. `pipefail` matters: without it a failed pg_dump piped
# into gzip still yields a valid, tiny, useless .gz and an exit code of 0.

set -euo pipefail
cd "$(dirname "$0")"

set -a
# shellcheck disable=SC1091
. ./.env.prod
set +a

: "${BACKUP_S3_URI:?set BACKUP_S3_URI in .env.prod (e.g. s3://bucket/db/)}"
command -v aws >/dev/null || { echo "aws CLI not installed" >&2; exit 1; }

COMPOSE=(docker compose -f docker-compose.prod.yml -f docker-compose.aws.yml --env-file .env.prod)
# Below this a dump is an empty schema or an error message, not this database.
MIN_BYTES="${MIN_BYTES:-5000}"   # an empty-schema dump is ~8 KB; a failed one is under 1 KB

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
# GNU mktemp requires the X's at the END of the template (BSD tolerates a suffix).
tmp="$(mktemp "${TMPDIR:-/tmp}/orkest-backup-XXXXXX")"
trap 'rm -f "$tmp"' EXIT

echo "[$stamp] dumping ${POSTGRES_DB}"
"${COMPOSE[@]}" exec -T postgres pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB" | gzip -9 > "$tmp"

gzip -t "$tmp"
size="$(wc -c < "$tmp" | tr -d ' ')"
if [ "$size" -lt "$MIN_BYTES" ]; then
  echo "dump is only ${size} bytes (< ${MIN_BYTES}); refusing to upload" >&2
  exit 1
fi

dest="${BACKUP_S3_URI%/}/orkest-${POSTGRES_DB}-${stamp}.sql.gz"
aws s3 cp "$tmp" "$dest" --only-show-errors
echo "[$stamp] uploaded ${size} bytes -> ${dest}"

if [ -n "${BACKUP_PING_URL:-}" ]; then
  curl -fsS --retry 3 -m 15 "$BACKUP_PING_URL" >/dev/null || echo "ping failed (backup itself succeeded)" >&2
fi
