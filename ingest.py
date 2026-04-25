"""
Download Form 5500 / Schedule A / Schedule A Part 1 datasets from DOL EBSA
and load them into the local SQLite DB.

Re-running this script reloads the targeted years from scratch (DROP+CREATE
in schema.sql then bulk INSERT). Downloaded zips are cached on disk in
data/raw/ so re-runs without --refresh-downloads skip the network hop.
"""
from __future__ import annotations

import argparse
import csv
import io
import sqlite3
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RAW_DIR = ROOT / "data" / "raw"
DB_PATH = ROOT / "data" / "efast.db"
SCHEMA_PATH = ROOT / "schema.sql"

URL_TMPL = "https://www.askebsa.dol.gov/FOIA%20Files/{year}/Latest/{name}_{year}_Latest.zip"
GEONAMES_URL = "https://download.geonames.org/export/zip/US.zip"
USER_AGENT = "efast-broker-search/0.1 (+local research tool)"

# ---------- Broker name normalization ----------
# Goal: collapse spelling variants of the same broker (LLC, Inc., abbreviations,
# punctuation, "and" vs "&") to a single canonical form for grouping. The raw
# name is preserved alongside.

import re as _re
_ENTITY_SUFFIX_RE = _re.compile(
    r"\b(L\.?L\.?C\.?|L\.?L\.?P\.?|L\.?P\.?|INC|INCORPORATED|CORP|CORPORATION|"
    r"CO|COMPANY|LTD|LIMITED|GROUP|GRP|HOLDINGS)\b\.?",
    _re.IGNORECASE,
)
_ABBREV_MAP = [
    (_re.compile(r"\bINS\b\.?", _re.IGNORECASE), "INSURANCE"),
    (_re.compile(r"\bSVC\b\.?", _re.IGNORECASE), "SERVICES"),
    (_re.compile(r"\bSVCS\b\.?", _re.IGNORECASE), "SERVICES"),
    (_re.compile(r"\bAGCY\b\.?", _re.IGNORECASE), "AGENCY"),
    (_re.compile(r"\bASSOC\b\.?", _re.IGNORECASE), "ASSOCIATES"),
    (_re.compile(r"\bMGMT\b\.?", _re.IGNORECASE), "MANAGEMENT"),
    (_re.compile(r"\bBNFTS?\b\.?", _re.IGNORECASE), "BENEFITS"),
]
_PREFIX_RE = _re.compile(r"^(AGENT-?|AGENT/|AGENT,?\s+)", _re.IGNORECASE)
_PUNCT_RE = _re.compile(r"[.,'`\"()\[\]/&]")
_AND_RE = _re.compile(r"\bAND\b", _re.IGNORECASE)
_WS_RE = _re.compile(r"\s+")


def canonical_broker(name: str | None) -> str | None:
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

# Allow large fields (Schedule A has 1000-char text columns)
csv.field_size_limit(sys.maxsize)


def download(year: int, name: str, force: bool) -> Path:
    """Download {name}_{year}_Latest.zip into data/raw/, return local path."""
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    local = RAW_DIR / f"{name}_{year}_Latest.zip"
    if local.exists() and not force:
        print(f"  cached  {local.name} ({local.stat().st_size / 1024 / 1024:.1f} MB)")
        return local
    url = URL_TMPL.format(year=year, name=name)
    print(f"  fetch   {url}")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=300) as resp, open(local, "wb") as f:
        while chunk := resp.read(1 << 20):
            f.write(chunk)
    print(f"          -> {local.name} ({local.stat().st_size / 1024 / 1024:.1f} MB, {time.time()-t0:.1f}s)")
    return local


def open_csv_in_zip(zip_path: Path):
    """Yield csv.DictReader over the single CSV inside the given zip."""
    zf = zipfile.ZipFile(zip_path)
    csv_name = next(n for n in zf.namelist() if n.lower().endswith(".csv"))
    raw = zf.open(csv_name, "r")
    text = io.TextIOWrapper(raw, encoding="latin-1", newline="")
    return zf, csv.DictReader(text)


def to_int(v):
    if v is None or v == "":
        return None
    try:
        return int(float(v))
    except ValueError:
        return None


def to_float(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except ValueError:
        return None


def s(v):
    """Trim and normalize empty strings to None."""
    if v is None:
        return None
    v = v.strip()
    return v or None


def load_filings(conn: sqlite3.Connection, year: int, zip_path: Path) -> int:
    zf, reader = open_csv_in_zip(zip_path)
    rows = []
    BATCH = 5000
    n = 0
    sql = """
    INSERT OR REPLACE INTO filings (
        ack_id, plan_year, plan_year_begin_date, form_tax_prd, plan_name, plan_num,
        sponsor_name, sponsor_dba_name, sponsor_address1, sponsor_address2,
        sponsor_city, sponsor_state, sponsor_zip, sponsor_ein, sponsor_phone,
        business_code, participants_active, participants_total_boy,
        type_pension_bnft_code, type_welfare_bnft_code,
        sch_a_attached_ind, num_sch_a_attached, date_received
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    for r in reader:
        rows.append((
            s(r.get("ACK_ID")),
            year,
            s(r.get("FORM_PLAN_YEAR_BEGIN_DATE")),
            s(r.get("FORM_TAX_PRD")),
            s(r.get("PLAN_NAME")),
            s(r.get("SPONS_DFE_PN")),
            s(r.get("SPONSOR_DFE_NAME")),
            s(r.get("SPONS_DFE_DBA_NAME")),
            s(r.get("SPONS_DFE_MAIL_US_ADDRESS1")),
            s(r.get("SPONS_DFE_MAIL_US_ADDRESS2")),
            s(r.get("SPONS_DFE_MAIL_US_CITY")),
            s(r.get("SPONS_DFE_MAIL_US_STATE")),
            s(r.get("SPONS_DFE_MAIL_US_ZIP")),
            s(r.get("SPONS_DFE_EIN")),
            s(r.get("SPONS_DFE_PHONE_NUM")),
            s(r.get("BUSINESS_CODE")),
            to_int(r.get("TOT_ACTIVE_PARTCP_CNT")),
            to_int(r.get("TOT_PARTCP_BOY_CNT")),
            s(r.get("TYPE_PENSION_BNFT_CODE")),
            s(r.get("TYPE_WELFARE_BNFT_CODE")),
            s(r.get("SCH_A_ATTACHED_IND")),
            to_int(r.get("NUM_SCH_A_ATTACHED_CNT")),
            s(r.get("DATE_RECEIVED")),
        ))
        if len(rows) >= BATCH:
            conn.executemany(sql, rows)
            n += len(rows)
            rows.clear()
    if rows:
        conn.executemany(sql, rows)
        n += len(rows)
    zf.close()
    return n


def load_schedule_a(conn: sqlite3.Connection, year: int, zip_path: Path) -> int:
    zf, reader = open_csv_in_zip(zip_path)
    rows = []
    BATCH = 5000
    n = 0
    sql = """
    INSERT OR REPLACE INTO schedule_a (
        ack_id, form_id, plan_year, plan_year_begin_date, plan_year_end_date,
        plan_num, ein, carrier_name, carrier_ein, carrier_naic_code,
        contract_num, persons_covered_eoy, policy_from_date, policy_to_date,
        broker_comm_total, broker_fees_total,
        bnft_health_ind, bnft_dental_ind, bnft_vision_ind, bnft_life_ind,
        bnft_disability_ind, bnft_drug_ind, bnft_stop_loss_ind
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    for r in reader:
        # Disability: collapse temp + long-term into one indicator.
        disab = "1" if (r.get("WLFR_BNFT_TEMP_DISAB_IND") == "1"
                        or r.get("WLFR_BNFT_LONG_TERM_DISAB_IND") == "1") else None
        rows.append((
            s(r.get("ACK_ID")),
            to_int(r.get("FORM_ID")),
            year,
            s(r.get("SCH_A_PLAN_YEAR_BEGIN_DATE")),
            s(r.get("SCH_A_PLAN_YEAR_END_DATE")),
            s(r.get("SCH_A_PLAN_NUM")),
            s(r.get("SCH_A_EIN")),
            s(r.get("INS_CARRIER_NAME")),
            s(r.get("INS_CARRIER_EIN")),
            s(r.get("INS_CARRIER_NAIC_CODE")),
            s(r.get("INS_CONTRACT_NUM")),
            to_int(r.get("INS_PRSN_COVERED_EOY_CNT")),
            s(r.get("INS_POLICY_FROM_DATE")),
            s(r.get("INS_POLICY_TO_DATE")),
            to_float(r.get("INS_BROKER_COMM_TOT_AMT")),
            to_float(r.get("INS_BROKER_FEES_TOT_AMT")),
            s(r.get("WLFR_BNFT_HEALTH_IND")),
            s(r.get("WLFR_BNFT_DENTAL_IND")),
            s(r.get("WLFR_BNFT_VISION_IND")),
            s(r.get("WLFR_BNFT_LIFE_INSUR_IND")),
            disab,
            s(r.get("WLFR_BNFT_DRUG_IND")),
            s(r.get("WLFR_BNFT_STOP_LOSS_IND")),
        ))
        if len(rows) >= BATCH:
            conn.executemany(sql, rows)
            n += len(rows)
            rows.clear()
    if rows:
        conn.executemany(sql, rows)
        n += len(rows)
    zf.close()
    return n


def load_schedule_a_part1(conn: sqlite3.Connection, year: int, zip_path: Path) -> int:
    zf, reader = open_csv_in_zip(zip_path)
    rows = []
    BATCH = 5000
    n = 0
    sql = """
    INSERT OR REPLACE INTO schedule_a_brokers (
        ack_id, form_id, row_order, plan_year,
        broker_name, broker_name_canonical,
        broker_address1, broker_address2,
        broker_city, broker_state, broker_zip,
        broker_comm_paid, broker_fees_paid, broker_code
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    for r in reader:
        broker_name = s(r.get("INS_BROKER_NAME"))
        # Normalize zip to 5 digits for joins to the zips table.
        raw_zip = s(r.get("INS_BROKER_US_ZIP"))
        zip5 = raw_zip[:5] if raw_zip and raw_zip[:5].isdigit() else None
        rows.append((
            s(r.get("ACK_ID")),
            to_int(r.get("FORM_ID")),
            to_int(r.get("ROW_ORDER")),
            year,
            broker_name,
            canonical_broker(broker_name),
            s(r.get("INS_BROKER_US_ADDRESS1")),
            s(r.get("INS_BROKER_US_ADDRESS2")),
            s(r.get("INS_BROKER_US_CITY")),
            s(r.get("INS_BROKER_US_STATE")),
            zip5,
            to_float(r.get("INS_BROKER_COMM_PD_AMT")),
            to_float(r.get("INS_BROKER_FEES_PD_AMT")),
            s(r.get("INS_BROKER_CODE")),
        ))
        if len(rows) >= BATCH:
            conn.executemany(sql, rows)
            n += len(rows)
            rows.clear()
    if rows:
        conn.executemany(sql, rows)
        n += len(rows)
    zf.close()
    return n


def load_zips(conn: sqlite3.Connection, force: bool) -> int:
    """Download and load GeoNames US zip-code coordinates into the zips table."""
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    local = RAW_DIR / "geonames_US.zip"
    if force or not local.exists():
        print(f"  fetch   {GEONAMES_URL}")
        req = urllib.request.Request(GEONAMES_URL, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=120) as resp, open(local, "wb") as f:
            while chunk := resp.read(1 << 20):
                f.write(chunk)
        print(f"          -> {local.name} ({local.stat().st_size / 1024:.0f} KB)")
    else:
        print(f"  cached  {local.name}")

    # GeoNames US.txt is tab-separated, columns:
    # 0=country, 1=zip, 2=place, 3=admin1 (state name), 4=admin1_code (state abbr),
    # 5=admin2 (county name), 6=admin2_code, 7=admin3, 8=admin3_code,
    # 9=lat, 10=lon, 11=accuracy
    zf = zipfile.ZipFile(local)
    txt_name = next(n for n in zf.namelist() if n.endswith("US.txt"))
    rows = []
    with zf.open(txt_name) as fh:
        text = io.TextIOWrapper(fh, encoding="utf-8")
        for line in text:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 11:
                continue
            zip_code, place, _, state_abbr = parts[1], parts[2], parts[3], parts[4]
            try:
                lat = float(parts[9])
                lon = float(parts[10])
            except (ValueError, IndexError):
                continue
            rows.append((zip_code, place, state_abbr, lat, lon))
    zf.close()
    conn.executemany(
        "INSERT OR REPLACE INTO zips (zip, city, state, lat, lon) VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    return len(rows)


def rebuild_fts(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM filings_fts")
    conn.execute("""
        INSERT INTO filings_fts (sponsor_name, sponsor_dba_name, plan_name, ack_id)
        SELECT sponsor_name, sponsor_dba_name, plan_name, ack_id FROM filings
        WHERE sponsor_name IS NOT NULL
    """)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", nargs="+", type=int, default=[2024, 2025])
    ap.add_argument("--refresh-downloads", action="store_true",
                    help="Re-download zips even if already cached on disk.")
    args = ap.parse_args()

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    print("=== schema ===")
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA_PATH.read_text())
    conn.commit()

    for year in args.years:
        print(f"\n=== {year} ===")

        print("downloads:")
        f5500 = download(year, "F_5500", args.refresh_downloads)
        scha = download(year, "F_SCH_A", args.refresh_downloads)
        scha_p1 = download(year, "F_SCH_A_PART1", args.refresh_downloads)

        t0 = time.time()
        n = load_filings(conn, year, f5500)
        conn.commit()
        print(f"filings loaded: {n:>9,} rows in {time.time()-t0:.1f}s")

        t0 = time.time()
        n = load_schedule_a(conn, year, scha)
        conn.commit()
        print(f"sch_a loaded:   {n:>9,} rows in {time.time()-t0:.1f}s")

        t0 = time.time()
        n = load_schedule_a_part1(conn, year, scha_p1)
        conn.commit()
        print(f"brokers loaded: {n:>9,} rows in {time.time()-t0:.1f}s")

    print("\n=== zips (GeoNames US) ===")
    t0 = time.time()
    n = load_zips(conn, args.refresh_downloads)
    conn.commit()
    print(f"zips loaded:    {n:>9,} rows in {time.time()-t0:.1f}s")

    print("\n=== rebuild FTS ===")
    t0 = time.time()
    rebuild_fts(conn)
    conn.commit()
    print(f"FTS rebuilt in {time.time()-t0:.1f}s")

    print("\n=== summary ===")
    for tbl in ("filings", "schedule_a", "schedule_a_brokers", "zips"):
        c = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        print(f"  {tbl:<22} {c:>10,}")
    conn.close()


if __name__ == "__main__":
    main()
