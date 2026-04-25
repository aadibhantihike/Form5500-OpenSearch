PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;

DROP TABLE IF EXISTS filings;
CREATE TABLE filings (
    ack_id TEXT PRIMARY KEY,
    plan_year INTEGER NOT NULL,
    plan_year_begin_date TEXT,
    form_tax_prd TEXT,
    plan_name TEXT,
    plan_num TEXT,
    sponsor_name TEXT,
    sponsor_dba_name TEXT,
    sponsor_address1 TEXT,
    sponsor_address2 TEXT,
    sponsor_city TEXT,
    sponsor_state TEXT,
    sponsor_zip TEXT,
    sponsor_ein TEXT,
    sponsor_phone TEXT,
    business_code TEXT,
    participants_active INTEGER,
    participants_total_boy INTEGER,
    type_pension_bnft_code TEXT,
    type_welfare_bnft_code TEXT,
    sch_a_attached_ind TEXT,
    num_sch_a_attached INTEGER,
    date_received TEXT
);
CREATE INDEX idx_filings_ein ON filings(sponsor_ein);
CREATE INDEX idx_filings_state ON filings(sponsor_state);
CREATE INDEX idx_filings_year ON filings(plan_year);
CREATE INDEX idx_filings_name ON filings(sponsor_name);

DROP TABLE IF EXISTS filings_fts;
CREATE VIRTUAL TABLE filings_fts USING fts5(
    sponsor_name,
    sponsor_dba_name,
    plan_name,
    ack_id UNINDEXED,
    tokenize = 'unicode61 remove_diacritics 2'
);

DROP TABLE IF EXISTS schedule_a;
CREATE TABLE schedule_a (
    ack_id TEXT NOT NULL,
    form_id INTEGER NOT NULL,
    plan_year INTEGER NOT NULL,
    plan_year_begin_date TEXT,
    plan_year_end_date TEXT,
    plan_num TEXT,
    ein TEXT,
    carrier_name TEXT,
    carrier_ein TEXT,
    carrier_naic_code TEXT,
    contract_num TEXT,
    persons_covered_eoy INTEGER,
    policy_from_date TEXT,
    policy_to_date TEXT,
    broker_comm_total REAL,
    broker_fees_total REAL,
    bnft_health_ind TEXT,
    bnft_dental_ind TEXT,
    bnft_vision_ind TEXT,
    bnft_life_ind TEXT,
    bnft_disability_ind TEXT,
    bnft_drug_ind TEXT,
    bnft_stop_loss_ind TEXT,
    PRIMARY KEY (ack_id, form_id)
);
CREATE INDEX idx_sch_a_ack ON schedule_a(ack_id);
CREATE INDEX idx_sch_a_carrier ON schedule_a(carrier_name);

DROP TABLE IF EXISTS schedule_a_brokers;
CREATE TABLE schedule_a_brokers (
    ack_id TEXT NOT NULL,
    form_id INTEGER NOT NULL,
    row_order INTEGER NOT NULL,
    plan_year INTEGER NOT NULL,
    broker_name TEXT,
    broker_name_canonical TEXT,
    broker_address1 TEXT,
    broker_address2 TEXT,
    broker_city TEXT,
    broker_state TEXT,
    broker_zip TEXT,
    broker_comm_paid REAL,
    broker_fees_paid REAL,
    broker_code TEXT,
    PRIMARY KEY (ack_id, form_id, row_order)
);
CREATE INDEX idx_brokers_ack ON schedule_a_brokers(ack_id);
CREATE INDEX idx_brokers_name ON schedule_a_brokers(broker_name);
CREATE INDEX idx_brokers_canonical ON schedule_a_brokers(broker_name_canonical);
CREATE INDEX idx_brokers_zip ON schedule_a_brokers(broker_zip);

DROP TABLE IF EXISTS zips;
CREATE TABLE zips (
    zip TEXT PRIMARY KEY,
    city TEXT,
    state TEXT,
    lat REAL,
    lon REAL
);
