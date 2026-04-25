#!/bin/sh
set -e

# Persistent volume mount in Fly: /data. Ingest writes the DB into ./data/efast.db
# relative to the project root, so symlink ./data → /data so existing paths work.
mkdir -p /data
if [ ! -e /app/data ]; then
    ln -s /data /app/data
fi

# First-boot ingest: if no DB on the volume yet, build it.
if [ ! -f /data/efast.db ]; then
    echo "[entrypoint] No DB found at /data/efast.db — running ingest. This takes several minutes."
    cd /app && python ingest.py
fi

exec "$@"
