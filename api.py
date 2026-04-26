"""
FastAPI search server over the local Form 5500 SQLite DB.

Run:
    uvicorn api:app --reload --port 8787
"""
from __future__ import annotations

import csv
import io
import os
import re
import sqlite3
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address

import plan_codes

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("EFAST_DB_PATH", ROOT / "data" / "efast.db"))
STATIC_DIR = ROOT / "public"

# Comma-separated list of allowed CORS origins. Set in production to the
# Vercel domain(s); defaults to "*" for local dev convenience.
CORS_ORIGINS = [o.strip() for o in os.environ.get("EFAST_CORS_ORIGINS", "*").split(",") if o.strip()]
DEFAULT_RATE_LIMIT = os.environ.get("EFAST_RATE_LIMIT", "60/minute")
BULK_RATE_LIMIT = os.environ.get("EFAST_BULK_RATE_LIMIT", "10/minute")

# Plan years covered by the loaded data; surfaced to the UI for the disclaimer.
DATA_YEARS = [2024, 2025]
DATA_SOURCE_NOTE = (
    "Source: U.S. Department of Labor (DOL/EBSA) public Form 5500 datasets. "
    "Data is published by EBSA and refreshed monthly; filings appear ~7-10 months "
    "after each plan-year end. The most recent year is therefore partial."
)

limiter = Limiter(key_func=get_remote_address, default_limits=[DEFAULT_RATE_LIMIT])

app = FastAPI(title="Form 5500 Broker Search")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


# ---------- shared helpers ----------

def fts_query(q: str) -> str:
    tokens = re.findall(r"[A-Za-z0-9]+", q)
    if not tokens:
        return ""
    return " ".join(f'"{t}"*' for t in tokens)


# Same canonicalization as ingest.py, kept in sync. Used to look up a broker
# by user-typed name and resolve to the canonical key the data is stored under.
_ENTITY_SUFFIX_RE = re.compile(
    r"\b(L\.?L\.?C\.?|L\.?L\.?P\.?|L\.?P\.?|INC|INCORPORATED|CORP|CORPORATION|"
    r"CO|COMPANY|LTD|LIMITED|GROUP|GRP|HOLDINGS)\b\.?",
    re.IGNORECASE,
)
_ABBREV_MAP = [
    (re.compile(r"\bINS\b\.?", re.IGNORECASE), "INSURANCE"),
    (re.compile(r"\bSVC\b\.?", re.IGNORECASE), "SERVICES"),
    (re.compile(r"\bSVCS\b\.?", re.IGNORECASE), "SERVICES"),
    (re.compile(r"\bAGCY\b\.?", re.IGNORECASE), "AGENCY"),
    (re.compile(r"\bASSOC\b\.?", re.IGNORECASE), "ASSOCIATES"),
    (re.compile(r"\bMGMT\b\.?", re.IGNORECASE), "MANAGEMENT"),
    (re.compile(r"\bBNFTS?\b\.?", re.IGNORECASE), "BENEFITS"),
]
_PREFIX_RE = re.compile(r"^(AGENT-?|AGENT/|AGENT,?\s+)", re.IGNORECASE)
_PUNCT_RE = re.compile(r"[.,'`\"()\[\]/&]")
_AND_RE = re.compile(r"\bAND\b", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")


def canonical_broker(name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    s = name.upper().strip()
    s = _PREFIX_RE.sub("", s).strip()
    for pat, repl in _ABBREV_MAP:
        s = pat.sub(repl, s)
    s = _PUNCT_RE.sub(" ", s)
    s = _AND_RE.sub(" ", s)
    s = _ENTITY_SUFFIX_RE.sub("", s)
    s = _WS_RE.sub(" ", s).strip()
    return s or None


def benefit_types(row) -> list[str]:
    flags = [
        ("Health", row["bnft_health_ind"]),
        ("Dental", row["bnft_dental_ind"]),
        ("Vision", row["bnft_vision_ind"]),
        ("Life", row["bnft_life_ind"]),
        ("Disability", row["bnft_disability_ind"]),
        ("Rx", row["bnft_drug_ind"]),
        ("Stop-loss", row["bnft_stop_loss_ind"]),
    ]
    return [name for name, v in flags if v == "1"]


def csv_response(rows: list[dict], filename: str) -> StreamingResponse:
    if not rows:
        rows = [{}]
    buf = io.StringIO()
    fieldnames = list(rows[0].keys())
    w = csv.DictWriter(buf, fieldnames=fieldnames)
    w.writeheader()
    for r in rows:
        w.writerow({k: r.get(k, "") for k in fieldnames})
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------- health ----------

@app.get("/healthz")
@limiter.exempt
def healthz(request: Request):
    return {"ok": True, "db_present": DB_PATH.exists()}


# ---------- glossary ----------

@app.get("/api/glossary")
def get_glossary():
    """Code → human label + tooltip text. Frontend caches this once and
    uses it to render any 5500 plan code in plain English."""
    return plan_codes.glossary()


# ---------- meta ----------

@app.get("/api/meta")
def meta():
    """Stats for the home-page disclaimer."""
    with db() as conn:
        max_received = conn.execute("SELECT MAX(date_received) FROM filings").fetchone()[0]
        per_year = conn.execute("""
            SELECT plan_year, COUNT(*) AS filings,
                   SUM(CASE WHEN sch_a_attached_ind='1' THEN 1 ELSE 0 END) AS with_sch_a
            FROM filings GROUP BY plan_year ORDER BY plan_year
        """).fetchall()
        n_brokers = conn.execute(
            "SELECT COUNT(DISTINCT broker_name_canonical) FROM schedule_a_brokers "
            "WHERE broker_name_canonical IS NOT NULL"
        ).fetchone()[0]
    return {
        "data_years": DATA_YEARS,
        "source_note": DATA_SOURCE_NOTE,
        "latest_filing_date_received": max_received,
        "filings_by_year": [dict(r) for r in per_year],
        "distinct_canonical_brokers": n_brokers,
    }


# ---------- companies ----------

@app.get("/api/companies")
def search_companies(
    q: str = Query(..., min_length=1),
    state: Optional[str] = Query(None, min_length=2, max_length=2),
    min_active: Optional[int] = Query(None, ge=0),
    plan_year: Optional[int] = None,
    limit: int = Query(20, ge=1, le=500),
    format: str = Query("json", pattern="^(json|csv)$"),
):
    match = fts_query(q)
    if not match:
        return {"results": []}

    where = ["filings_fts MATCH ?"]
    params: list = [match]
    if state:
        where.append("f.sponsor_state = ?")
        params.append(state.upper())
    if min_active is not None:
        where.append("f.participants_active >= ?")
        params.append(min_active)
    if plan_year is not None:
        where.append("f.plan_year = ?")
        params.append(plan_year)
    params.append(limit)

    sql = f"""
        SELECT
            f.sponsor_ein,
            f.sponsor_name,
            f.sponsor_city,
            f.sponsor_state,
            COUNT(DISTINCT f.ack_id) AS filing_count,
            MAX(f.plan_year) AS latest_year,
            MAX(f.participants_active) AS max_active,
            SUM(CASE WHEN f.sch_a_attached_ind='1' THEN 1 ELSE 0 END) AS sch_a_count
        FROM filings_fts ft
        JOIN filings f ON f.ack_id = ft.ack_id
        WHERE {' AND '.join(where)}
        GROUP BY f.sponsor_ein, f.sponsor_name
        ORDER BY max_active DESC NULLS LAST, latest_year DESC, filing_count DESC
        LIMIT ?
    """
    with db() as conn:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]

    if format == "csv":
        return csv_response(rows, f"companies_{q}.csv")
    return {"query": q, "results": rows}


@app.get("/api/companies/by-ein/{ein}")
def company_by_ein(ein: str, format: str = Query("json", pattern="^(json|csv)$")):
    """All filings for a sponsor EIN, with broker rollup grouped by canonical name."""
    with db() as conn:
        filings = conn.execute("""
            SELECT ack_id, plan_year, plan_year_begin_date, plan_name, plan_num,
                   sponsor_name, sponsor_dba_name, sponsor_address1, sponsor_city,
                   sponsor_state, sponsor_zip, sponsor_ein, business_code,
                   participants_active, type_pension_bnft_code, type_welfare_bnft_code,
                   sch_a_attached_ind, num_sch_a_attached, date_received
            FROM filings WHERE sponsor_ein = ?
            ORDER BY plan_year DESC, plan_num
        """, (ein,)).fetchall()
        if not filings:
            raise HTTPException(404, "No filings for that EIN")

        ack_ids = [f["ack_id"] for f in filings]
        ph = ",".join("?" * len(ack_ids))

        sch_a = conn.execute(
            f"SELECT * FROM schedule_a WHERE ack_id IN ({ph})", ack_ids
        ).fetchall()
        brokers = conn.execute(
            f"SELECT * FROM schedule_a_brokers WHERE ack_id IN ({ph})", ack_ids
        ).fetchall()

        brokers_by_form: dict[tuple, list] = defaultdict(list)
        for b in brokers:
            brokers_by_form[(b["ack_id"], b["form_id"])].append(dict(b))

        sch_a_by_filing: dict[str, list] = defaultdict(list)
        for s in sch_a:
            d = dict(s)
            d["benefit_types"] = benefit_types(s)
            d["brokers"] = sorted(
                brokers_by_form.get((s["ack_id"], s["form_id"]), []),
                key=lambda r: -(r.get("broker_comm_paid") or 0),
            )
            sch_a_by_filing[s["ack_id"]].append(d)
        for entries in sch_a_by_filing.values():
            entries.sort(key=lambda r: -(r.get("broker_comm_total") or 0))

        result_filings = []
        for f in filings:
            d = dict(f)
            d["schedule_a"] = sch_a_by_filing.get(f["ack_id"], [])
            d["plan_profile"] = plan_codes.plan_profile(d, d["schedule_a"])
            result_filings.append(d)

    # Canonical-grouped broker rollup. Display name = the most-frequent raw spelling
    # we saw for that canonical key (so "AON INSURANCE AGENCY LLC" stays human).
    canon_data: dict[str, dict] = {}
    for b in brokers:
        c = b["broker_name_canonical"]
        if not c:
            continue
        bt = canon_data.setdefault(c, {
            "broker_canonical": c,
            "display_name": b["broker_name"],
            "name_counter": defaultdict(int),
            "offices": set(),
            "total_commission": 0.0,
            "total_fees": 0.0,
            "appearances": 0,
            "years": set(),
        })
        bt["name_counter"][b["broker_name"]] += 1
        bt["offices"].add((b["broker_city"], b["broker_state"]))
        bt["total_commission"] += b["broker_comm_paid"] or 0
        bt["total_fees"] += b["broker_fees_paid"] or 0
        bt["appearances"] += 1
        bt["years"].add(b["plan_year"])

    rollup = []
    for v in canon_data.values():
        # Pick the most common raw spelling as the display label.
        v["display_name"] = max(v["name_counter"].items(), key=lambda kv: kv[1])[0]
        v["years"] = sorted(v["years"], reverse=True)
        v["offices"] = [
            {"city": c, "state": s}
            for (c, s) in sorted(v["offices"]) if c or s
        ]
        v.pop("name_counter")
        rollup.append(v)
    rollup.sort(key=lambda r: -r["total_commission"])

    if format == "csv":
        flat = []
        for f in result_filings:
            for sa in f.get("schedule_a", []):
                for b in sa.get("brokers", []):
                    flat.append({
                        "plan_year": f["plan_year"],
                        "plan_name": f["plan_name"],
                        "carrier": sa["carrier_name"],
                        "contract_num": sa["contract_num"],
                        "covered": sa["persons_covered_eoy"],
                        "benefits": "|".join(sa.get("benefit_types", [])),
                        "broker_name": b["broker_name"],
                        "broker_canonical": b["broker_name_canonical"],
                        "broker_address1": b["broker_address1"],
                        "broker_city": b["broker_city"],
                        "broker_state": b["broker_state"],
                        "broker_zip": b["broker_zip"],
                        "broker_comm_paid": b["broker_comm_paid"],
                        "broker_fees_paid": b["broker_fees_paid"],
                    })
        return csv_response(flat, f"company_{ein}.csv")

    return {
        "ein": ein,
        "sponsor_name": filings[0]["sponsor_name"],
        "filings": result_filings,
        "broker_rollup": rollup,
    }


# ---------- brokers ----------

@app.get("/api/brokers")
def search_brokers(
    q: str = Query(..., min_length=2),
    limit: int = Query(20, ge=1, le=200),
    format: str = Query("json", pattern="^(json|csv)$"),
):
    """Search brokers, grouped by canonical name."""
    pattern = f"%{q.upper()}%"
    sql = """
        SELECT broker_name_canonical AS canonical,
               COUNT(DISTINCT ack_id) AS distinct_filings,
               SUM(COALESCE(broker_comm_paid, 0)) AS total_commission,
               COUNT(DISTINCT broker_state) AS state_count,
               COUNT(DISTINCT broker_city || '|' || broker_state) AS office_count
        FROM schedule_a_brokers
        WHERE broker_name_canonical IS NOT NULL
          AND (UPPER(broker_name) LIKE ? OR broker_name_canonical LIKE ?)
        GROUP BY broker_name_canonical
        ORDER BY distinct_filings DESC
        LIMIT ?
    """
    with db() as conn:
        rows = conn.execute(sql, (pattern, pattern, limit)).fetchall()
        out = []
        for r in rows:
            # Most common raw display name for this canonical bucket
            disp = conn.execute("""
                SELECT broker_name, COUNT(*) AS n
                FROM schedule_a_brokers
                WHERE broker_name_canonical = ?
                GROUP BY broker_name ORDER BY n DESC LIMIT 1
            """, (r["canonical"],)).fetchone()
            d = dict(r)
            d["display_name"] = disp["broker_name"] if disp else r["canonical"]
            out.append(d)

    if format == "csv":
        return csv_response(out, f"brokers_{q}.csv")
    return {"query": q, "results": out}


@app.get("/api/brokers/clients")
def broker_clients(
    canonical: str = Query(..., description="Canonical broker name (e.g. 'LOCKTON COMPANIES')"),
    state: Optional[str] = None,
    plan_year: Optional[int] = None,
    min_lives: Optional[int] = Query(None, ge=0),
    benefit: Optional[str] = Query(None, description="One of: health, dental, vision, life, disability, rx, stop_loss"),
    limit: int = Query(100, ge=1, le=2000),
    format: str = Query("json", pattern="^(json|csv)$"),
):
    """List sponsor contract lines paid to a given canonical broker, with filters."""
    where = ["b.broker_name_canonical = ?"]
    params: list = [canonical.upper()]
    if state:
        where.append("f.sponsor_state = ?")
        params.append(state.upper())
    if plan_year is not None:
        where.append("f.plan_year = ?")
        params.append(plan_year)
    if min_lives is not None:
        where.append("sa.persons_covered_eoy >= ?")
        params.append(min_lives)

    benefit_col_map = {
        "health": "bnft_health_ind", "dental": "bnft_dental_ind",
        "vision": "bnft_vision_ind", "life": "bnft_life_ind",
        "disability": "bnft_disability_ind", "rx": "bnft_drug_ind",
        "stop_loss": "bnft_stop_loss_ind",
    }
    if benefit:
        col = benefit_col_map.get(benefit.lower())
        if not col:
            raise HTTPException(400, f"Unknown benefit '{benefit}'")
        where.append(f"sa.{col} = '1'")
    params.append(limit)

    sql = f"""
        SELECT f.sponsor_name, f.sponsor_ein, f.sponsor_state, f.sponsor_city,
               f.plan_year, f.plan_name, f.participants_active,
               b.broker_name, b.broker_address1, b.broker_city, b.broker_state, b.broker_zip,
               b.broker_comm_paid, b.broker_fees_paid,
               sa.carrier_name, sa.contract_num, sa.persons_covered_eoy,
               sa.bnft_health_ind, sa.bnft_dental_ind, sa.bnft_vision_ind,
               sa.bnft_life_ind, sa.bnft_disability_ind, sa.bnft_drug_ind,
               sa.bnft_stop_loss_ind
        FROM schedule_a_brokers b
        JOIN filings f ON f.ack_id = b.ack_id
        LEFT JOIN schedule_a sa ON sa.ack_id = b.ack_id AND sa.form_id = b.form_id
        WHERE {' AND '.join(where)}
        ORDER BY b.broker_comm_paid DESC NULLS LAST
        LIMIT ?
    """
    with db() as conn:
        rows = conn.execute(sql, params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["benefit_types"] = benefit_types(r)
        out.append(d)

    if format == "csv":
        for d in out:
            d["benefits"] = "|".join(d.pop("benefit_types"))
        return csv_response(out, f"clients_{canonical}.csv")
    return {"broker_canonical": canonical.upper(), "results": out}


@app.get("/api/brokers/map")
def broker_map(
    canonical: str = Query(..., description="Canonical broker name"),
):
    """Geographic data for one broker:
    - offices: list of {lat, lon, city, state, zip, count} for the broker's own offices
    - clients_by_state: {state: count} of sponsor filings served, for choropleth
    """
    canon = canonical.upper()
    with db() as conn:
        # Office locations: zip-level dots, count of broker rows at that zip.
        offices = conn.execute("""
            SELECT z.lat, z.lon, z.city, z.state, b.broker_zip AS zip,
                   COUNT(*) AS n
            FROM schedule_a_brokers b
            JOIN zips z ON z.zip = b.broker_zip
            WHERE b.broker_name_canonical = ?
            GROUP BY b.broker_zip
            ORDER BY n DESC
        """, (canon,)).fetchall()
        # Clients by sponsor state.
        states = conn.execute("""
            SELECT f.sponsor_state AS state, COUNT(DISTINCT f.ack_id) AS clients
            FROM schedule_a_brokers b
            JOIN filings f ON f.ack_id = b.ack_id
            WHERE b.broker_name_canonical = ? AND f.sponsor_state IS NOT NULL
            GROUP BY f.sponsor_state
        """, (canon,)).fetchall()
    return {
        "broker_canonical": canon,
        "offices": [dict(r) for r in offices],
        "clients_by_state": {r["state"]: r["clients"] for r in states},
    }


# ---------- broker switchers (high-signal sales prospects) ----------

@app.get("/api/brokers/switchers")
def broker_switchers(
    state: Optional[str] = None,
    min_lives: Optional[int] = Query(None, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    format: str = Query("json", pattern="^(json|csv)$"),
):
    """
    Sponsors whose primary broker (by commission) changed between consecutive plan years.
    Useful for prospecting — these companies are actively shopping their benefits relationship.
    """
    sql_filter = []
    params: list = []
    if state:
        sql_filter.append("f.sponsor_state = ?")
        params.append(state.upper())
    if min_lives is not None:
        sql_filter.append("f.participants_active >= ?")
        params.append(min_lives)
    extra_where = (" AND " + " AND ".join(sql_filter)) if sql_filter else ""

    # Compute (sponsor, year, broker) commission totals once via single GROUP BY,
    # then pick the top broker per (sponsor, year) using a window function, then
    # self-join consecutive years. Avoids the per-row correlated subquery that
    # was timing out at 200k+ rows.
    sql = f"""
        WITH broker_year_totals AS (
            SELECT f.sponsor_ein, f.plan_year, b.broker_name_canonical,
                   SUM(COALESCE(b.broker_comm_paid, 0)) AS total_comm
            FROM schedule_a_brokers b
            JOIN filings f ON f.ack_id = b.ack_id
            WHERE b.broker_name_canonical IS NOT NULL
            GROUP BY f.sponsor_ein, f.plan_year, b.broker_name_canonical
        ),
        ranked AS (
            SELECT sponsor_ein, plan_year, broker_name_canonical,
                   ROW_NUMBER() OVER (
                       PARTITION BY sponsor_ein, plan_year
                       ORDER BY total_comm DESC
                   ) AS rn
            FROM broker_year_totals
        ),
        primary_per_year AS (
            SELECT sponsor_ein, plan_year, broker_name_canonical AS primary_broker
            FROM ranked WHERE rn = 1
        ),
        sponsor_summary AS (
            SELECT sponsor_ein,
                   MAX(sponsor_name) AS sponsor_name,
                   MAX(sponsor_state) AS sponsor_state,
                   MAX(participants_active) AS participants_active
            FROM filings
            GROUP BY sponsor_ein
        )
        SELECT s.sponsor_ein, s.sponsor_name, s.sponsor_state, s.participants_active,
               a.plan_year AS year_a, a.primary_broker AS broker_a,
               b.plan_year AS year_b, b.primary_broker AS broker_b
        FROM primary_per_year a
        JOIN primary_per_year b
          ON a.sponsor_ein = b.sponsor_ein
         AND b.plan_year = a.plan_year + 1
         AND a.primary_broker <> b.primary_broker
        JOIN sponsor_summary s ON s.sponsor_ein = a.sponsor_ein
        WHERE 1=1 {extra_where.replace("f.", "s.")}
        ORDER BY s.participants_active DESC NULLS LAST
        LIMIT ?
    """
    params.append(limit)
    with db() as conn:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    if format == "csv":
        return csv_response(rows, "broker_switchers.csv")
    return {"results": rows}


# ---------- bulk lookup ----------

@app.post("/api/lookup/bulk")
@limiter.limit(BULK_RATE_LIMIT)
def bulk_lookup(
    request: Request,
    payload: dict = Body(..., example={"items": ["STARBUCKS CORPORATION", "911325671"]}),
    format: str = Query("json", pattern="^(json|csv)$"),
):
    """
    Bulk resolve a list of company names or EINs to their primary broker info.
    Each input item is matched against EIN exact, then sponsor_name FTS.
    Returns one row per input with the latest-year primary broker (by commission).
    """
    items = payload.get("items") or []
    if not isinstance(items, list) or not items:
        raise HTTPException(400, "Provide 'items' as a non-empty list of names or EINs.")
    if len(items) > 1000:
        raise HTTPException(400, "Max 1000 items per request.")

    results = []
    with db() as conn:
        for raw in items:
            term = (raw or "").strip()
            if not term:
                results.append({"input": raw, "match_type": "empty", "sponsor_name": None})
                continue

            # 1) try EIN exact match (9 digits)
            ein_clean = re.sub(r"\D", "", term)
            sponsor = None
            match_type = None
            if len(ein_clean) == 9:
                sponsor = conn.execute("""
                    SELECT sponsor_ein, sponsor_name, sponsor_state, plan_year, ack_id, participants_active
                    FROM filings WHERE sponsor_ein = ?
                    ORDER BY plan_year DESC LIMIT 1
                """, (ein_clean,)).fetchone()
                if sponsor:
                    match_type = "ein"

            # 2) fall back to FTS
            if sponsor is None:
                m = fts_query(term)
                if m:
                    sponsor = conn.execute("""
                        SELECT f.sponsor_ein, f.sponsor_name, f.sponsor_state,
                               f.plan_year, f.ack_id, f.participants_active
                        FROM filings_fts ft JOIN filings f ON f.ack_id = ft.ack_id
                        WHERE filings_fts MATCH ?
                        ORDER BY f.participants_active DESC NULLS LAST, f.plan_year DESC
                        LIMIT 1
                    """, (m,)).fetchone()
                    if sponsor:
                        match_type = "name"

            if sponsor is None:
                results.append({"input": raw, "match_type": "none", "sponsor_name": None})
                continue

            # Find latest-year primary broker for this sponsor
            primary = conn.execute("""
                SELECT b.broker_name_canonical AS broker_canonical,
                       SUM(COALESCE(b.broker_comm_paid,0)) AS commission
                FROM schedule_a_brokers b
                JOIN filings f ON f.ack_id = b.ack_id
                WHERE f.sponsor_ein = ? AND b.broker_name_canonical IS NOT NULL
                GROUP BY b.broker_name_canonical, f.plan_year
                ORDER BY f.plan_year DESC, commission DESC
                LIMIT 1
            """, (sponsor["sponsor_ein"],)).fetchone()

            display_name = None
            if primary:
                disp = conn.execute("""
                    SELECT broker_name, COUNT(*) AS n FROM schedule_a_brokers
                    WHERE broker_name_canonical = ?
                    GROUP BY broker_name ORDER BY n DESC LIMIT 1
                """, (primary["broker_canonical"],)).fetchone()
                display_name = disp["broker_name"] if disp else primary["broker_canonical"]

            results.append({
                "input": raw,
                "match_type": match_type,
                "sponsor_ein": sponsor["sponsor_ein"],
                "sponsor_name": sponsor["sponsor_name"],
                "sponsor_state": sponsor["sponsor_state"],
                "latest_plan_year": sponsor["plan_year"],
                "participants_active": sponsor["participants_active"],
                "primary_broker_canonical": primary["broker_canonical"] if primary else None,
                "primary_broker_display": display_name,
                "primary_broker_commission": primary["commission"] if primary else None,
            })

    if format == "csv":
        return csv_response(results, "bulk_lookup.csv")
    return {"results": results}


# ---------- large-client map ----------

@app.get("/api/clients/large-map")
def large_clients_map(
    min_active: int = Query(10000, ge=1),
    plan_year: Optional[int] = None,
):
    """
    Returns one record per large sponsor (by EIN) with coordinates, latest filing,
    and primary broker. Used to power the >10k participant client map.
    """
    where = ["f.participants_active >= ?", "f.sch_a_attached_ind = '1'"]
    params: list = [min_active]
    if plan_year is not None:
        where.append("f.plan_year = ?")
        params.append(plan_year)

    sql = f"""
        WITH ranked AS (
            SELECT f.*, ROW_NUMBER() OVER (
                PARTITION BY f.sponsor_ein
                ORDER BY f.plan_year DESC, f.participants_active DESC
            ) AS rn
            FROM filings f
            WHERE {' AND '.join(where)}
        )
        SELECT r.sponsor_ein, r.sponsor_name, r.sponsor_city, r.sponsor_state,
               r.sponsor_zip, r.plan_year, r.plan_name, r.participants_active,
               z.lat, z.lon
        FROM ranked r
        JOIN zips z ON z.zip = SUBSTR(COALESCE(r.sponsor_zip,''), 1, 5)
        WHERE r.rn = 1
        ORDER BY r.participants_active DESC
    """
    with db() as conn:
        rows = conn.execute(sql, params).fetchall()
        # Attach primary broker for each (latest filing's top broker by commission)
        out = []
        for r in rows:
            primary = conn.execute("""
                SELECT b.broker_name_canonical AS canonical,
                       SUM(COALESCE(b.broker_comm_paid,0)) AS commission
                FROM schedule_a_brokers b
                JOIN filings f ON f.ack_id = b.ack_id
                WHERE f.sponsor_ein = ? AND b.broker_name_canonical IS NOT NULL
                GROUP BY b.broker_name_canonical, f.plan_year
                ORDER BY f.plan_year DESC, commission DESC
                LIMIT 1
            """, (r["sponsor_ein"],)).fetchone()
            d = dict(r)
            d["primary_broker_canonical"] = primary["canonical"] if primary else None
            out.append(d)
    return {"results": out}


# ---------- static UI ----------

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def root():
    return FileResponse(STATIC_DIR / "index.html")
