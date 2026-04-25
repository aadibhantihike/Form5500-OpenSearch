#!/bin/sh
set -e

# Persistent volume mount in Fly: /data. Ingest writes the DB into ./data/efast.db
# relative to the project root, so symlink ./data → /data so existing paths work.
mkdir -p /data
if [ ! -e /app/data ]; then
    ln -s /data /app/data
fi

# First-boot (or post-interrupted-ingest) bootstrap. The marker file is
# written only after ingest completes; checking for the DB file alone is
# unsafe because schema-only DBs exist briefly during ingest.
if [ ! -f /data/.ingest-complete ]; then
    echo "[entrypoint] No completed ingest marker — running ingest. This takes several minutes."
    rm -f /data/efast.db /data/efast.db-shm /data/efast.db-wal
    cd /app && python -u ingest.py && touch /data/.ingest-complete
fi

exec "$@"
