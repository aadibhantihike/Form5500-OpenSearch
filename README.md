# efast-broker-search

Open search interface over U.S. Department of Labor (DOL/EBSA) public Form 5500
filings, with broker-name normalization and Schedule A commission rollups.

Live: https://efast.vercel.app

The DOL publishes every Form 5500 filing every employer-sponsored health,
welfare, and pension plan with 100+ participants makes annually. Schedule A
attachments report what was paid to which insurance broker. This project loads
those datasets into SQLite, canonicalizes broker names across spelling
variants, and exposes a search UI plus JSON/CSV API.

All data here is public.

## Stack

- **Backend**: FastAPI + SQLite (FTS5) on Fly.io
- **Frontend**: static HTML on Vercel, with `/api/*` rewritten to Fly
- **Ingest**: `ingest.py` downloads from `askebsa.dol.gov`, runs monthly via GitHub Actions

## Run locally

```sh
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python ingest.py            # downloads ~60MB, builds ~350MB DB, takes a few minutes
uvicorn api:app --port 8787
```

Open `http://localhost:8787`.

## Deploy

### Backend on Fly.io

```sh
fly launch --no-deploy --copy-config       # accept the existing fly.toml
fly volumes create efast_data --region iad --size 3
fly secrets set EFAST_CORS_ORIGINS="https://efast.vercel.app"
fly deploy
```

First boot runs `ingest.py` automatically (entrypoint detects no DB on the
volume). Expect 5–10 minutes for the first deploy to come up.

### Frontend on Vercel

```sh
vercel --prod
```

Vercel serves `public/index.html` and rewrites `/api/*` to the Fly app per
[vercel.json](vercel.json). Update the destination URL if your Fly app name
differs from `efast-broker-search`.

### Monthly data refresh

Set `FLY_API_TOKEN` in repo secrets. The
[refresh-data workflow](.github/workflows/refresh-data.yml) runs the 15th of
each month and re-ingests on the live machine.

## API

| Endpoint | Description |
| --- | --- |
| `GET /api/meta` | Coverage years and filing counts |
| `GET /api/companies?q=` | Search sponsors by name (FTS) |
| `GET /api/companies/by-ein/{ein}` | All filings + Schedule A rollup for one EIN |
| `GET /api/brokers?q=` | Search brokers by canonical name |
| `GET /api/brokers/clients?canonical=` | Sponsor contracts paid to a broker |
| `GET /api/brokers/map?canonical=` | Broker office locations + clients-by-state |
| `GET /api/brokers/switchers` | Sponsors who changed primary broker year-over-year |
| `POST /api/lookup/bulk` | Bulk resolve names/EINs to primary broker |
| `GET /api/clients/large-map` | Sponsors >10k participants with coordinates |

All read endpoints accept `format=csv` for download.

## Rate limits

Default: 60 req/min/IP. Bulk lookup: 10 req/min/IP. Tune via `EFAST_RATE_LIMIT`
and `EFAST_BULK_RATE_LIMIT` env vars on Fly.

## Data caveats

- DOL publishes filings 7–10 months after each plan-year end the most recent
  year is always partial.
- Broker name canonicalization is heuristic see `canonical_broker()` in
  `ingest.py`. Edge cases get reported as issues; PRs welcome.
- Schedule A commission/fee fields are self-reported by carriers and known to
  be inconsistent across filers.

## License

MIT. See [LICENSE](LICENSE).
