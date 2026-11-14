-- SFG inter-plant transfer tracker: data model
-- Field names are kept close to SAP terms (plant, material, batch, UoM,
-- movement type) so a later SAP link is a mapping exercise, not a redesign.

PRAGMA foreign_keys = ON;

-- Master data -------------------------------------------------------------

CREATE TABLE IF NOT EXISTS plants (
    plant_code   TEXT PRIMARY KEY,          -- SAP: WERKS (4 chars)
    name         TEXT NOT NULL,
    location     TEXT
);

CREATE TABLE IF NOT EXISTS departments (
    dept_code    TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    plant_code   TEXT NOT NULL REFERENCES plants(plant_code)
);

CREATE TABLE IF NOT EXISTS materials (
    material_no   TEXT PRIMARY KEY,         -- SAP: MATNR
    description   TEXT NOT NULL,
    uom           TEXT NOT NULL DEFAULT 'EA',  -- SAP: MEINS
    material_type TEXT NOT NULL DEFAULT 'SFG', -- SAP: MTART (HALB for semi-finished)
    dept_code     TEXT REFERENCES departments(dept_code)
);

-- Copy of the job order system's data, refreshed from its export file.
-- The tracker never edits these rows; the job order system owns them.

CREATE TABLE IF NOT EXISTS job_orders (
    job_order_no    TEXT PRIMARY KEY,
    parent_material TEXT NOT NULL,
    description     TEXT NOT NULL,
    qty             REAL NOT NULL,
    uom             TEXT NOT NULL DEFAULT 'EA',
    assembly_plant  TEXT NOT NULL REFERENCES plants(plant_code),
    due_date        TEXT,
    status          TEXT NOT NULL,
    synced_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS job_order_components (
    job_order_no  TEXT NOT NULL REFERENCES job_orders(job_order_no),
    material_no   TEXT NOT NULL REFERENCES materials(material_no),
    qty_required  REAL NOT NULL,
    supply_plant  TEXT NOT NULL REFERENCES plants(plant_code),
    PRIMARY KEY (job_order_no, material_no)
);

-- Transactions ------------------------------------------------------------

CREATE TABLE IF NOT EXISTS counters (
    name   TEXT PRIMARY KEY,
    value  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS transfers (
    transfer_no   TEXT PRIMARY KEY,          -- e.g. TRF-2026-00001, printed on the slip
    job_order_no  TEXT NOT NULL REFERENCES job_orders(job_order_no),
    from_plant    TEXT NOT NULL REFERENCES plants(plant_code),
    from_dept     TEXT NOT NULL REFERENCES departments(dept_code),
    to_plant      TEXT NOT NULL REFERENCES plants(plant_code),
    status        TEXT NOT NULL CHECK (status IN
                    ('IN_TRANSIT', 'RECEIVED', 'RECEIVED_WITH_DISCREPANCY')),
    sent_by       TEXT NOT NULL,
    sent_at       TEXT NOT NULL,
    vehicle_ref   TEXT,
    send_note     TEXT,
    received_by   TEXT,
    received_at   TEXT,
    receive_note  TEXT,
    sap_doc_out   TEXT,                       -- SAP material doc for the 303 posting, filled once SAP is linked
    sap_doc_in    TEXT,                       -- SAP material doc for the 305 posting
    CHECK (from_plant <> to_plant)
);

CREATE TABLE IF NOT EXISTS transfer_lines (
    transfer_no   TEXT NOT NULL REFERENCES transfers(transfer_no),
    line_no       INTEGER NOT NULL,
    material_no   TEXT NOT NULL REFERENCES materials(material_no),
    batch         TEXT,                       -- SAP: CHARG
    uom           TEXT NOT NULL,
    qty_sent      REAL NOT NULL CHECK (qty_sent > 0),
    qty_received  REAL,                       -- NULL until the receiving plant confirms
    qty_damaged   REAL,
    PRIMARY KEY (transfer_no, line_no)
);

CREATE TABLE IF NOT EXISTS discrepancies (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    transfer_no   TEXT NOT NULL REFERENCES transfers(transfer_no),
    line_no       INTEGER,                    -- NULL for a whole-transfer issue (not received)
    kind          TEXT NOT NULL CHECK (kind IN ('SHORT', 'EXCESS', 'DAMAGED', 'NOT_RECEIVED')),
    qty           REAL,
    status        TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN', 'RESOLVED')),
    raised_by     TEXT NOT NULL,
    raised_at     TEXT NOT NULL,
    resolution    TEXT,
    resolved_by   TEXT,
    resolved_at   TEXT
);

-- The trail ---------------------------------------------------------------
-- Every action writes one row here. Rows can never be edited or deleted
-- (triggers below), and each row carries a hash of the previous one, so a
-- changed or removed row breaks the chain and shows up on /trail.

CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    event_type   TEXT NOT NULL,
    transfer_no  TEXT,
    plant_code   TEXT,
    actor        TEXT NOT NULL,
    payload      TEXT NOT NULL,               -- JSON, canonical (sorted keys)
    prev_hash    TEXT NOT NULL,
    hash         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_transfer ON events(transfer_no);
CREATE INDEX IF NOT EXISTS idx_transfers_status ON transfers(status);
CREATE INDEX IF NOT EXISTS idx_transfers_jo ON transfers(job_order_no);

CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'events are append-only');
END;

CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'events are append-only');
END;
