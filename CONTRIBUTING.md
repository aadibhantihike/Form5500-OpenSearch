# Contributing

Thanks for taking a look. The most useful contributions are:

1. **Broker canonicalization fixes.** If you see two display names that should
   be one (or one that lumps two real brokers together), open an issue with
   the canonical key from `/api/brokers?q=...` and what the right grouping
   should be.
2. **New endpoints / aggregations.** The DB is rich; the UI exposes a slice.
   Open an issue first to align on shape.
3. **Schema additions** from other Form 5500 schedules (Schedule C, Schedule
   H). Touch `schema.sql` and `ingest.py` together.

## Local setup

See [README.md](README.md). `python ingest.py` downloads ~60 MB and builds a
~350 MB SQLite DB; the raw zips are cached in `data/raw/` so re-runs without
`--refresh-downloads` skip the network.

## Style

- Python: standard library + FastAPI/uvicorn/slowapi only. No ORMs.
- SQL: keep queries readable; window functions are fine.
- Comments explain *why*, not *what*.

## Reporting data issues

DOL data has plenty of quirks (missing EINs, malformed zips, multi-row brokers
where some are blank). When filing an issue, include the `ack_id` so others
can reproduce.
