"""Business logic for the SFG inter-plant transfer tracker.

Everything that changes data goes through a function in this file, and every
one of those functions writes an event to the append-only trail inside the
same database transaction. The web pages and the JSON API both call these
functions, so there is exactly one set of rules.
"""

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")
GENESIS = "GENESIS"
GST = timezone(timedelta(hours=4), "GST")  # Gulf Standard Time, no daylight saving


class TrackerError(ValueError):
    """A rule was broken. The message is shown to the user as is."""


# --- time helpers ----------------------------------------------------------

def utcnow():
    return datetime.now(timezone.utc).replace(microsecond=0)


def to_iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def from_iso(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def fmt_local(s):
    """'2026-11-14T13:05:00Z' -> '14 Nov 2026, 17:05 GST'."""
    if not s:
        return ""
    return from_iso(s).astimezone(GST).strftime("%d %b %Y, %H:%M GST")


# --- database --------------------------------------------------------------

def connect(path):
    con = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA busy_timeout = 5000")
    return con


def init_schema(con):
    with open(SCHEMA_PATH) as f:
        con.executescript(f.read())


@contextmanager
def tx(con):
    """One all-or-nothing transaction. BEGIN IMMEDIATE takes the write lock up
    front, so two people receiving the same transfer at once cannot both win."""
    con.execute("BEGIN IMMEDIATE")
    try:
        yield con
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise


# --- the trail -------------------------------------------------------------

def _event_hash(prev_hash, ts, event_type, transfer_no, plant_code, actor, payload):
    body = "|".join([prev_hash, ts, event_type, transfer_no or "", plant_code or "", actor, payload])
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def append_event(con, event_type, actor, payload, ts, transfer_no=None, plant_code=None):
    row = con.execute("SELECT hash FROM events ORDER BY id DESC LIMIT 1").fetchone()
    prev_hash = row["hash"] if row else GENESIS
    payload_s = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    h = _event_hash(prev_hash, ts, event_type, transfer_no, plant_code, actor, payload_s)
    cur = con.execute(
        "INSERT INTO events (ts, event_type, transfer_no, plant_code, actor, payload, prev_hash, hash)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (ts, event_type, transfer_no, plant_code, actor, payload_s, prev_hash, h),
    )
    return cur.lastrowid


def verify_trail(con):
    """Recompute every hash. Returns (ok, events_checked, first_bad_event_id)."""
    prev = GENESIS
    n = 0
    for r in con.execute("SELECT * FROM events ORDER BY id"):
        n += 1
        expected = _event_hash(prev, r["ts"], r["event_type"], r["transfer_no"],
                               r["plant_code"], r["actor"], r["payload"])
        if r["prev_hash"] != prev or r["hash"] != expected:
            return False, n, r["id"]
        if r["id"] != n:  # ids are 1, 2, 3... with AUTOINCREMENT; a gap means a removed row
            return False, n, r["id"]
        prev = r["hash"]
    seq = con.execute("SELECT seq FROM sqlite_sequence WHERE name = 'events'").fetchone()
    if seq and seq["seq"] != n:  # the newest event(s) were removed
        return False, n, n + 1
    return True, n, None


def reconcile(con):
    """Replay the trail and compare it with the working tables (transfers,
    lines, discrepancies). The trail is the source of truth: if someone edits
    a quantity or a status directly in the database, it shows up here.
    Returns a list of mismatches; empty means everything agrees."""
    tr, ln, ds = {}, {}, {}
    for e in con.execute("SELECT * FROM events ORDER BY id"):
        p, t = json.loads(e["payload"]), e["transfer_no"]
        if e["event_type"] == "TRANSFER_SENT":
            tr[t] = (p["job_order_no"], p["from_plant"], p["to_plant"], "IN_TRANSIT")
            for x in p["lines"]:
                ln[(t, x["line_no"])] = (x["material_no"], x["qty_sent"], None, None)
        elif e["event_type"] == "TRANSFER_RECEIVED":
            tr[t] = tr[t][:3] + (p["status"],)
            for x in p["lines"]:
                mat, sent, _, _ = ln[(t, x["line_no"])]
                ln[(t, x["line_no"])] = (mat, sent, x["qty_received"], x["qty_damaged"])
        elif e["event_type"] == "DISCREPANCY_RAISED":
            ds[p["discrepancy_id"]] = (t, p["kind"], p.get("qty"), "OPEN")
        elif e["event_type"] == "DISCREPANCY_RESOLVED":
            ds[p["discrepancy_id"]] = ds[p["discrepancy_id"]][:3] + ("RESOLVED",)
    problems = []
    db_tr = {r["transfer_no"]: (r["job_order_no"], r["from_plant"], r["to_plant"], r["status"])
             for r in con.execute("SELECT * FROM transfers")}
    db_ln = {(r["transfer_no"], r["line_no"]): (r["material_no"], r["qty_sent"], r["qty_received"], r["qty_damaged"])
             for r in con.execute("SELECT * FROM transfer_lines")}
    db_ds = {r["id"]: (r["transfer_no"], r["kind"], r["qty"], r["status"])
             for r in con.execute("SELECT * FROM discrepancies")}
    for name, trail, table in (("transfer", tr, db_tr), ("line", ln, db_ln), ("discrepancy", ds, db_ds)):
        for k in sorted(set(trail) | set(table), key=str):
            if trail.get(k) != table.get(k):
                problems.append(f"{name} {k}: trail says {trail.get(k)}, table says {table.get(k)}")
    return problems


# --- master data and the job order file ------------------------------------

def load_master_data(con, path):
    with open(path) as f:
        md = json.load(f)
    with tx(con):
        for p in md["plants"]:
            con.execute("INSERT OR REPLACE INTO plants VALUES (?, ?, ?)",
                        (p["plant_code"], p["name"], p.get("location")))
        for d in md["departments"]:
            con.execute("INSERT OR REPLACE INTO departments VALUES (?, ?, ?)",
                        (d["dept_code"], d["name"], d["plant_code"]))
        for m in md["materials"]:
            con.execute("INSERT OR REPLACE INTO materials VALUES (?, ?, ?, ?, ?)",
                        (m["material_no"], m["description"], m.get("uom", "EA"),
                         m.get("material_type", "SFG"), m.get("dept_code")))


def sync_job_orders(con, path, actor="system", now=None):
    """Import (or refresh) job orders from the job order system's export file.

    This is the inbound half of the job order link. The file format is the
    contract: swap the file for an API call later and nothing else changes.
    """
    ts = to_iso(now or utcnow())
    with open(path) as f:
        data = json.load(f)
    orders = data["job_orders"]
    with tx(con):
        for jo in orders:
            con.execute(
                "INSERT INTO job_orders (job_order_no, parent_material, description, qty, uom,"
                " assembly_plant, due_date, status, synced_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(job_order_no) DO UPDATE SET parent_material=excluded.parent_material,"
                " description=excluded.description, qty=excluded.qty, uom=excluded.uom,"
                " assembly_plant=excluded.assembly_plant, due_date=excluded.due_date,"
                " status=excluded.status, synced_at=excluded.synced_at",
                (jo["job_order_no"], jo["parent_material"], jo["description"], jo["qty"],
                 jo.get("uom", "EA"), jo["assembly_plant"], jo.get("due_date"), jo["status"], ts),
            )
            con.execute("DELETE FROM job_order_components WHERE job_order_no = ?", (jo["job_order_no"],))
            for c in jo["components"]:
                con.execute("INSERT INTO job_order_components VALUES (?, ?, ?, ?)",
                            (jo["job_order_no"], c["material_no"], c["qty_required"], c["supply_plant"]))
        append_event(con, "JOB_ORDERS_SYNCED", actor,
                     {"source_file": os.path.basename(path),
                      "source_system": data.get("source_system"),
                      "exported_at": data.get("exported_at"),
                      "job_orders": [jo["job_order_no"] for jo in orders]}, ts)
    return len(orders)


# --- numbering -------------------------------------------------------------

def _next_transfer_no(con, year):
    key = f"transfer-{year}"
    con.execute("INSERT OR IGNORE INTO counters VALUES (?, 0)", (key,))
    con.execute("UPDATE counters SET value = value + 1 WHERE name = ?", (key,))
    n = con.execute("SELECT value FROM counters WHERE name = ?", (key,)).fetchone()["value"]
    return f"TRF-{year}-{n:05d}"


# --- the two core actions --------------------------------------------------

def _num(v, field):
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise TrackerError(f"{field} must be a number.")
    if x != x or x in (float("inf"), float("-inf")):
        raise TrackerError(f"{field} must be a number.")
    return x


def send_transfer(con, *, job_order_no, from_plant, from_dept, to_plant, lines,
                  sent_by, vehicle_ref="", note="", now=None):
    """Log that a plant has sent SFGs to another plant. Returns the transfer number.

    lines: list of {"material_no", "qty", "batch" (optional)}
    """
    now = now or utcnow()
    ts = to_iso(now)
    vehicle_ref = _s(vehicle_ref)
    note = _s(note)
    # Validate inside the write transaction so a job order sync can't change
    # the job order between the check and the insert.
    with tx(con):
        clean = _validate_send(con, job_order_no, from_plant, from_dept, to_plant, lines, sent_by)
        sent_by = _s(sent_by)
        job_order_no, from_plant, from_dept, to_plant = map(_s, (job_order_no, from_plant, from_dept, to_plant))
        tno = _next_transfer_no(con, now.year)
        con.execute(
            "INSERT INTO transfers (transfer_no, job_order_no, from_plant, from_dept, to_plant, status,"
            " sent_by, sent_at, vehicle_ref, send_note) VALUES (?, ?, ?, ?, ?, 'IN_TRANSIT', ?, ?, ?, ?)",
            (tno, job_order_no, from_plant, from_dept, to_plant, sent_by, ts, vehicle_ref, note),
        )
        for n, c in enumerate(clean, start=1):
            con.execute(
                "INSERT INTO transfer_lines (transfer_no, line_no, material_no, batch, uom, qty_sent)"
                " VALUES (?, ?, ?, ?, ?, ?)", (tno, n, c["material_no"], c["batch"], c["uom"], c["qty"]))
        append_event(con, "TRANSFER_SENT", sent_by, {
            "job_order_no": job_order_no, "from_plant": from_plant, "from_dept": from_dept,
            "to_plant": to_plant, "vehicle_ref": vehicle_ref, "note": note,
            "lines": [{"line_no": n, "material_no": c["material_no"], "batch": c["batch"],
                       "qty_sent": c["qty"], "uom": c["uom"]} for n, c in enumerate(clean, start=1)],
        }, ts, transfer_no=tno, plant_code=from_plant)
    return tno


def _s(v):
    """Ids from forms or JSON as plain strings, so odd input gets a clear message, not a crash."""
    return v.strip() if isinstance(v, str) else ""


def _validate_send(con, job_order_no, from_plant, from_dept, to_plant, lines, sent_by):
    job_order_no, from_plant, from_dept, to_plant = map(_s, (job_order_no, from_plant, from_dept, to_plant))
    sent_by = _s(sent_by)
    if not sent_by:
        raise TrackerError("Enter the name of the person sending the parts.")
    if from_plant == to_plant:
        raise TrackerError("Sending and receiving plant must be different.")

    plants = {r["plant_code"] for r in con.execute("SELECT plant_code FROM plants")}
    if from_plant not in plants or to_plant not in plants:
        raise TrackerError("Unknown plant.")
    dept = con.execute("SELECT * FROM departments WHERE dept_code = ?", (from_dept,)).fetchone()
    if not dept:
        raise TrackerError("Unknown department.")
    if dept["plant_code"] != from_plant:
        raise TrackerError(f"Department {dept['name']} is not in plant {from_plant}.")

    jo = con.execute("SELECT * FROM job_orders WHERE job_order_no = ?", (job_order_no,)).fetchone()
    if not jo:
        raise TrackerError("Pick a job order. Every transfer must belong to a job order.")
    if jo["status"] not in ("RELEASED", "IN_PROGRESS"):
        raise TrackerError(f"Job order {job_order_no} is {jo['status']}; parts can't be sent against it.")
    if to_plant != jo["assembly_plant"]:
        raise TrackerError(f"Job order {job_order_no} is assembled at plant {jo['assembly_plant']}; "
                           f"send its parts there, not to {to_plant}.")
    bom = {r["material_no"]: r for r in con.execute(
        "SELECT * FROM job_order_components WHERE job_order_no = ?", (job_order_no,))}

    clean = []
    if not isinstance(lines, list):
        raise TrackerError("lines must be a list.")
    for i, ln in enumerate(lines, start=1):
        if not isinstance(ln, dict):
            raise TrackerError(f"Line {i} is not valid.")
        mat = str(ln.get("material_no") or "").strip()
        if not mat:
            continue
        qty = _num(ln.get("qty"), f"Line {i} quantity")
        if qty <= 0:
            raise TrackerError(f"Line {i}: quantity must be more than zero.")
        m = con.execute("SELECT * FROM materials WHERE material_no = ?", (mat,)).fetchone()
        if not m:
            raise TrackerError(f"Line {i}: unknown material {mat}.")
        if mat not in bom:
            raise TrackerError(f"Line {i}: {mat} is not on job order {job_order_no}'s bill of materials.")
        if m["uom"] == "EA" and qty != int(qty):
            raise TrackerError(f"Line {i}: {mat} is counted in whole pieces.")
        comp = bom[mat]
        if from_plant != comp["supply_plant"]:
            raise TrackerError(f"Line {i}: for {job_order_no}, {mat} comes from plant {comp['supply_plant']}, "
                               f"not {from_plant}.")
        # Allowed = required minus what is already good at the assembly plant or on the way.
        # A short or damaged receipt frees up room, so the missing pieces can be re-sent.
        done = con.execute(
            "SELECT COALESCE(SUM(CASE WHEN t.status = 'IN_TRANSIT' THEN l.qty_sent"
            " ELSE l.qty_received - COALESCE(l.qty_damaged, 0) END), 0)"
            " FROM transfer_lines l JOIN transfers t USING (transfer_no)"
            " WHERE t.job_order_no = ? AND l.material_no = ? AND t.to_plant = ?",
            (job_order_no, mat, jo["assembly_plant"])).fetchone()[0]
        room = comp["qty_required"] - done
        if qty > room:
            raise TrackerError(f"Line {i}: {job_order_no} needs {comp['qty_required']:g} x {mat}; "
                               f"{done:g} are already received or on the way, so at most {max(room, 0):g} more can be sent.")
        clean.append({"material_no": mat, "qty": qty, "uom": m["uom"],
                      "batch": str(ln.get("batch") or "").strip()})
    if not clean:
        raise TrackerError("Add at least one part to send.")
    mats = [c["material_no"] for c in clean]
    if len(mats) != len(set(mats)):
        raise TrackerError("The same material appears twice; put the total on one line.")

    return clean


def receive_transfer(con, transfer_no, *, receiving_plant, received_by, lines, note="", now=None):
    """Confirm receipt at the other plant.

    lines: {line_no: {"qty_received": x, "qty_damaged": y}} for every line.
    Any difference against what was sent becomes a discrepancy automatically.
    """
    received_by = _s(received_by)
    receiving_plant = _s(receiving_plant)
    note = _s(note)
    if not received_by:
        raise TrackerError("Enter the name of the person receiving the parts.")
    now = now or utcnow()
    ts = to_iso(now)

    with tx(con):
        t = con.execute("SELECT * FROM transfers WHERE transfer_no = ?", (transfer_no,)).fetchone()
        if not t:
            raise TrackerError(f"Transfer {transfer_no} does not exist.")
        if t["status"] != "IN_TRANSIT":
            raise TrackerError(f"{transfer_no} was already received by {t['received_by']} "
                               f"on {fmt_local(t['received_at'])}.")
        if receiving_plant != t["to_plant"]:
            raise TrackerError(f"{transfer_no} is addressed to plant {t['to_plant']}, "
                               f"not {receiving_plant}. Only the destination plant can receive it.")

        db_lines = con.execute("SELECT * FROM transfer_lines WHERE transfer_no = ? ORDER BY line_no",
                               (transfer_no,)).fetchall()
        result, discrepancies = [], []
        for dl in db_lines:
            given = (lines or {}).get(dl["line_no"]) or (lines or {}).get(str(dl["line_no"]))
            if not isinstance(given, dict):
                raise TrackerError(f"Enter the received quantity for line {dl['line_no']}.")
            rec = _num(given.get("qty_received"), f"Line {dl['line_no']} received quantity")
            dmg = _num(given.get("qty_damaged") or 0, f"Line {dl['line_no']} damaged quantity")
            if rec < 0 or dmg < 0:
                raise TrackerError(f"Line {dl['line_no']}: quantities can't be negative.")
            if dmg > rec:
                raise TrackerError(f"Line {dl['line_no']}: damaged can't be more than received.")
            con.execute("UPDATE transfer_lines SET qty_received = ?, qty_damaged = ?"
                        " WHERE transfer_no = ? AND line_no = ?", (rec, dmg, transfer_no, dl["line_no"]))
            diff = rec - dl["qty_sent"]
            if diff < 0:
                discrepancies.append((dl["line_no"], dl["material_no"], "SHORT", -diff))
            elif diff > 0:
                discrepancies.append((dl["line_no"], dl["material_no"], "EXCESS", diff))
            if dmg > 0:
                discrepancies.append((dl["line_no"], dl["material_no"], "DAMAGED", dmg))
            result.append({"line_no": dl["line_no"], "material_no": dl["material_no"],
                           "qty_sent": dl["qty_sent"], "qty_received": rec, "qty_damaged": dmg})

        status = "RECEIVED_WITH_DISCREPANCY" if discrepancies else "RECEIVED"
        con.execute("UPDATE transfers SET status = ?, received_by = ?, received_at = ?, receive_note = ?"
                    " WHERE transfer_no = ?", (status, received_by, ts, note.strip(), transfer_no))
        append_event(con, "TRANSFER_RECEIVED", received_by,
                     {"status": status, "note": note.strip(), "lines": result},
                     ts, transfer_no=transfer_no, plant_code=receiving_plant)
        # A "not received" flag raised earlier is settled by the receipt itself.
        for d in con.execute("SELECT id FROM discrepancies WHERE transfer_no = ? AND kind = 'NOT_RECEIVED'"
                             " AND status = 'OPEN'", (transfer_no,)).fetchall():
            _resolve(con, d["id"], received_by, "Parts arrived and were received.", ts)
        for line_no, mat, kind, qty in discrepancies:
            _raise(con, transfer_no, line_no, kind, qty, received_by, ts, receiving_plant,
                   {"material_no": mat})
    return status


# --- discrepancies ---------------------------------------------------------

def _raise(con, transfer_no, line_no, kind, qty, actor, ts, plant_code, extra=None):
    cur = con.execute(
        "INSERT INTO discrepancies (transfer_no, line_no, kind, qty, raised_by, raised_at)"
        " VALUES (?, ?, ?, ?, ?, ?)", (transfer_no, line_no, kind, qty, actor, ts))
    payload = {"discrepancy_id": cur.lastrowid, "kind": kind, "line_no": line_no, "qty": qty}
    payload.update(extra or {})
    append_event(con, "DISCREPANCY_RAISED", actor, payload, ts,
                 transfer_no=transfer_no, plant_code=plant_code)
    return cur.lastrowid


def _resolve(con, disc_id, actor, resolution, ts):
    d = con.execute("SELECT * FROM discrepancies WHERE id = ?", (disc_id,)).fetchone()
    con.execute("UPDATE discrepancies SET status = 'RESOLVED', resolution = ?, resolved_by = ?,"
                " resolved_at = ? WHERE id = ?", (resolution, actor, ts, disc_id))
    append_event(con, "DISCREPANCY_RESOLVED", actor,
                 {"discrepancy_id": disc_id, "kind": d["kind"], "resolution": resolution},
                 ts, transfer_no=d["transfer_no"])


def report_not_received(con, transfer_no, *, reported_by, note="", now=None):
    """Either plant flags that a transfer has not arrived. It stays in transit,
    so it can still be received if the parts turn up."""
    reported_by = _s(reported_by)
    note = _s(note)
    if not reported_by:
        raise TrackerError("Enter your name.")
    ts = to_iso(now or utcnow())
    with tx(con):
        t = con.execute("SELECT * FROM transfers WHERE transfer_no = ?", (transfer_no,)).fetchone()
        if not t:
            raise TrackerError(f"Transfer {transfer_no} does not exist.")
        if t["status"] != "IN_TRANSIT":
            raise TrackerError(f"{transfer_no} has already been received.")
        if con.execute("SELECT 1 FROM discrepancies WHERE transfer_no = ? AND kind = 'NOT_RECEIVED'"
                       " AND status = 'OPEN'", (transfer_no,)).fetchone():
            raise TrackerError(f"{transfer_no} is already flagged as not received.")
        return _raise(con, transfer_no, None, "NOT_RECEIVED", None, reported_by, ts, t["to_plant"],
                      {"note": note.strip()})


def resolve_discrepancy(con, disc_id, *, resolved_by, resolution, now=None):
    resolved_by = _s(resolved_by)
    resolution = _s(resolution)
    if not resolved_by or not resolution:
        raise TrackerError("Enter your name and how the discrepancy was settled.")
    ts = to_iso(now or utcnow())
    with tx(con):
        d = con.execute("SELECT * FROM discrepancies WHERE id = ?", (disc_id,)).fetchone()
        if not d:
            raise TrackerError("Discrepancy not found.")
        if d["status"] != "OPEN":
            raise TrackerError("This discrepancy is already resolved.")
        _resolve(con, disc_id, resolved_by, resolution, ts)


# --- read side -------------------------------------------------------------

def overdue_transfers(con, sla_hours, now=None):
    cutoff = to_iso((now or utcnow()) - timedelta(hours=sla_hours))
    return con.execute("SELECT * FROM transfers WHERE status = 'IN_TRANSIT' AND sent_at < ?"
                       " ORDER BY sent_at", (cutoff,)).fetchall()


def job_order_status(con, job_order_no):
    """What the job order system needs back: for each SFG on the job order,
    how many are required, sent, in transit and received good at the assembly
    plant, and what is still missing. This is the outbound half of the link."""
    jo = con.execute("SELECT * FROM job_orders WHERE job_order_no = ?", (job_order_no,)).fetchone()
    if not jo:
        return None
    comps = []
    for c in con.execute(
            "SELECT c.*, m.description, m.uom FROM job_order_components c"
            " JOIN materials m USING (material_no) WHERE job_order_no = ? ORDER BY material_no",
            (job_order_no,)):
        agg = con.execute(
            "SELECT"
            " COALESCE(SUM(l.qty_sent), 0) AS sent,"
            " COALESCE(SUM(CASE WHEN t.status = 'IN_TRANSIT' THEN l.qty_sent END), 0) AS in_transit,"
            " COALESCE(SUM(CASE WHEN t.status <> 'IN_TRANSIT' AND t.to_plant = ?"
            "   THEN l.qty_received - COALESCE(l.qty_damaged, 0) END), 0) AS received_good"
            " FROM transfer_lines l JOIN transfers t USING (transfer_no)"
            " WHERE t.job_order_no = ? AND l.material_no = ?",
            (jo["assembly_plant"], job_order_no, c["material_no"])).fetchone()
        missing = max(c["qty_required"] - agg["received_good"], 0)
        comps.append({
            "material_no": c["material_no"], "description": c["description"], "uom": c["uom"],
            "supply_plant": c["supply_plant"], "qty_required": c["qty_required"],
            "qty_sent": agg["sent"], "qty_in_transit": agg["in_transit"],
            "qty_received_good": agg["received_good"], "qty_missing": missing,
        })
    missing_total = sum(c["qty_missing"] for c in comps)
    return {
        "job_order_no": jo["job_order_no"], "parent_material": jo["parent_material"],
        "description": jo["description"], "qty": jo["qty"], "assembly_plant": jo["assembly_plant"],
        "due_date": jo["due_date"], "job_order_status": jo["status"],
        "sfg_ready": missing_total == 0, "qty_missing_total": missing_total,
        "components": comps,
    }


def sap_goods_movements(con):
    """Turn the trail into SAP-style goods movement lines.

    Send = movement type 303 (out of the issuing plant, booked as stock in
    transfer at the receiving plant). Receive = 305 (from stock in transfer
    into unrestricted stock at the receiving plant). STGE_LOC is left for the
    SAP team; MOVE_PLANT on the 305 line is to be confirmed. Column names follow the item fields of SAP's standard goods
    movement BAPI so the file maps one-to-one; to be confirmed with Indus's
    SAP team. Damaged quantities are still posted as received and flagged.
    """
    rows = []
    for e in con.execute("SELECT * FROM events WHERE event_type IN ('TRANSFER_SENT', 'TRANSFER_RECEIVED')"
                         " ORDER BY id"):
        p = json.loads(e["payload"])
        t = con.execute("SELECT * FROM transfers WHERE transfer_no = ?", (e["transfer_no"],)).fetchone()
        for ln in p["lines"]:
            sent = e["event_type"] == "TRANSFER_SENT"
            # SAP can't receive more than is in transfer: cap the 305 at the sent
            # quantity and leave the excess for a separate, checked posting.
            qty = ln["qty_sent"] if sent else min(ln["qty_received"], ln["qty_sent"])
            notes = [t["job_order_no"]]
            if not sent and ln["qty_received"] > ln["qty_sent"]:
                notes.append(f"{ln['qty_received'] - ln['qty_sent']:g} excess not posted")
            if not sent and ln.get("qty_damaged"):
                notes.append(f"{ln['qty_damaged']:g} damaged")
            batch = con.execute("SELECT batch, uom FROM transfer_lines WHERE transfer_no = ? AND line_no = ?",
                                (e["transfer_no"], ln["line_no"])).fetchone()
            rows.append({
                "EVENT_ID": e["id"],
                "PSTNG_DATE": from_iso(e["ts"]).astimezone(GST).strftime("%Y%m%d"),
                "REF_DOC_NO": e["transfer_no"],
                "MOVE_TYPE": "303" if sent else "305",
                "MATERIAL": ln["material_no"],
                "PLANT": t["from_plant"] if sent else t["to_plant"],
                "STGE_LOC": "",
                "BATCH": batch["batch"] or "",
                "ENTRY_QNT": qty,
                "ENTRY_UOM": batch["uom"],
                "MOVE_PLANT": t["to_plant"] if sent else t["from_plant"],
                "ITEM_TEXT": "; ".join(notes),
                "POSTED_IN_SAP": "N",
            })
    return rows
