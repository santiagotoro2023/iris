#!/usr/bin/env bash
# Restore IRiS from an encrypted backup (SPEC.md 26).
#
#   ./restore.sh                       restore the most recent backup
#   ./restore.sh backup/iris-....enc   restore a specific one
#   ./restore.sh --list                show what is available
#
# This overwrites the live databases. It asks first.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

log()  { printf '\033[38;5;208m::\033[0m %s\n' "$*"; }
warn() { printf '\033[33m!!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31mXX\033[0m %s\n' "$*" >&2; exit 1; }

dc() { docker compose "$@"; }

if [ "${1:-}" = "--list" ]; then
  ls -lh backup/iris-*.tar.gz.enc 2>/dev/null || echo "No backups yet."
  exit 0
fi

ARCHIVE="${1:-$(ls -1t backup/iris-*.tar.gz.enc 2>/dev/null | head -1 || true)}"
[ -n "$ARCHIVE" ] && [ -f "$ARCHIVE" ] || die "No backup found. Try: ./restore.sh --list"

[ -f .env ] || die ".env is missing, and the decryption key lives in it."
# shellcheck disable=SC1091
IRIS_BACKUP_KEY="$(grep -E '^IRIS_BACKUP_KEY=' .env | cut -d= -f2-)"
[ -n "$IRIS_BACKUP_KEY" ] || die "IRIS_BACKUP_KEY is not in .env. Without it this archive cannot be read."
export IRIS_BACKUP_KEY

POSTGRES_USER="$(grep -E '^POSTGRES_USER=' .env | cut -d= -f2-)"
POSTGRES_DB="$(grep -E '^POSTGRES_DB=' .env | cut -d= -f2-)"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

log "Decrypting $ARCHIVE..."
openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -pass env:IRIS_BACKUP_KEY \
  -in "$ARCHIVE" | tar -xzf - -C "$TMP" \
  || die "Could not decrypt. Wrong IRIS_BACKUP_KEY, or the file is damaged."

log "This archive contains:"
sed 's/^/     /' "$TMP/manifest.json" 2>/dev/null || true
echo
warn "Restoring REPLACES the current memories, conversations, settings and accounts."
read -r -p "Type RESTORE to confirm: " reply
[ "$reply" = "RESTORE" ] || die "Aborted — nothing was changed."

log "Starting the databases..."
dc up -d postgres qdrant redis >/dev/null
for _ in $(seq 1 30); do
  dc exec -T postgres pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null 2>&1 && break
  sleep 2
done

if [ -f "$TMP/postgres.sql" ]; then
  log "Restoring Postgres (accounts, settings, audit log)..."
  dc exec -T postgres psql -q -U "$POSTGRES_USER" -d "$POSTGRES_DB" < "$TMP/postgres.sql" >/dev/null
fi

if [ -f "$TMP/qdrant.snapshot" ]; then
  log "Restoring Qdrant (memories)..."
  curl -sf -X POST "http://127.0.0.1:6333/collections/memories/snapshots/upload?priority=snapshot" \
    -H 'Content-Type: multipart/form-data' \
    -F "snapshot=@$TMP/qdrant.snapshot" >/dev/null \
    || warn "Qdrant restore failed; memories may be incomplete."
fi

if [ -f "$TMP/redis-chat.json" ]; then
  log "Restoring conversations..."
  # Redis is the only store whose dump is our own JSON rather than the engine's
  # native format, so it is replayed key by key.
  dc exec -T -e RJSON="$(cat "$TMP/redis-chat.json")" api python - <<'PY'
import json, os, asyncio, redis.asyncio as aioredis
async def main():
    r = aioredis.from_url(os.environ.get("REDIS_URL", "redis://redis:6379"),
                          decode_responses=True)
    data = json.loads(os.environ["RJSON"])
    for key, value in data.items():
        await r.delete(key)
        if isinstance(value, dict):
            if value:
                await r.hset(key, mapping=value)
        else:
            await r.set(key, value)
    print(f"  restored {len(data)} conversation keys")
    await r.aclose()
asyncio.run(main())
PY
fi

log "Restarting IRiS..."
dc up -d >/dev/null
log "Restored from $ARCHIVE."
