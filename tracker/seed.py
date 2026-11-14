"""Create a fresh database with master data, the dummy job orders and a short,
realistic history (made through the same functions the app uses, so the trail
is genuine and its hash chain verifies).

Run:  python -m tracker.seed            (demo history)
      python -m tracker.seed --empty    (master data and job orders only)
"""

import json
import os
import random
import sys
from datetime import timedelta

from . import core

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
MASTER = os.path.join(DATA, "master_data.json")
JOB_ORDERS = os.path.join(DATA, "job_orders.json")


def default_db_path():
    return os.path.abspath(os.environ.get("SFG_DB", os.path.join(ROOT, "instance", "sfg_tracker.db")))


def build(db_path, demo=True, now=None):
    db_path = os.path.abspath(db_path)
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(db_path + suffix):
            os.remove(db_path + suffix)
    con = core.connect(db_path)
    core.init_schema(con)
    core.load_master_data(con, MASTER)
    now = now or core.utcnow()
    ago = lambda **kw: now - timedelta(**kw)  # noqa: E731

    if demo:
        _history(con, now)
    core.sync_job_orders(con, JOB_ORDERS, now=ago(hours=27))
    if not demo:
        return con

    # Events are created in time order, so the trail reads naturally.
    # 1. Clean transfer: headers to assembly, fully received yesterday.
    t1 = core.send_transfer(
        con, job_order_no="JO-2026-1041", from_plant="1100", from_dept="HDR", to_plant="1300",
        lines=[{"material_no": "SFG-HDR-1001", "qty": 20, "batch": "H261113-01"},
               {"material_no": "SFG-HDR-1002", "qty": 20, "batch": "H261113-02"}],
        sent_by="Ravi K. (Headers)", vehicle_ref="Forklift trolley T-04", now=ago(hours=26))
    core.receive_transfer(
        con, t1, receiving_plant="1300", received_by="Ahmed S. (Assembly stores)",
        lines={1: {"qty_received": 20}, 2: {"qty_received": 20}}, now=ago(hours=25, minutes=20))

    # 2. Sent but never received: the classic dispute (Plant 3 flags it below).
    t3 = core.send_transfer(
        con, job_order_no="JO-2026-1042", from_plant="1100", from_dept="HDR", to_plant="1300",
        lines=[{"material_no": "SFG-HDR-1003", "qty": 12, "batch": "H261114-03"},
               {"material_no": "SFG-HDR-1004", "qty": 12, "batch": "H261114-04"}],
        sent_by="Ravi K. (Headers)", vehicle_ref="Forklift trolley T-02", now=ago(hours=9))

    # 3. Quantity mismatch: 2 side channels short, 2 end plates dented.
    t2 = core.send_transfer(
        con, job_order_no="JO-2026-1041", from_plant="1200", from_dept="SM", to_plant="1300",
        lines=[{"material_no": "SFG-SM-2001", "qty": 40, "batch": "S261114-07"},
               {"material_no": "SFG-SM-2002", "qty": 40, "batch": "S261114-08"}],
        sent_by="Joseph M. (Sheet Metal)", vehicle_ref="Pickup DXB-K-55210", now=ago(hours=6))
    core.receive_transfer(
        con, t2, receiving_plant="1300", received_by="Ahmed S. (Assembly stores)",
        lines={1: {"qty_received": 40, "qty_damaged": 2}, 2: {"qty_received": 38}},
        note="2 end plates dented on the corner. Side channels counted twice: 38.",
        now=ago(hours=5, minutes=10))
    dmg = con.execute("SELECT id FROM discrepancies WHERE transfer_no = ? AND kind = 'DAMAGED'",
                      (t2,)).fetchone()["id"]
    core.resolve_discrepancy(
        con, dmg, resolved_by="Joseph M. (Sheet Metal)",
        resolution="2 dented end plates returned to Plant 2 for rework; replacements go on the next trip.",
        now=ago(hours=3))

    # Plant 3 flags transfer 2 as not received.
    core.report_not_received(
        con, t3, reported_by="Ahmed S. (Assembly stores)",
        note="Not on the receiving dock at shift change. Checked bay 3 and bay 4.", now=ago(hours=1))

    # 4 and 5. Fresh transfers, in transit right now, ready to be received in the demo.
    core.send_transfer(
        con, job_order_no="JO-2026-1044", from_plant="1200", from_dept="SM", to_plant="1100",
        lines=[{"material_no": "SFG-SM-2001", "qty": 60, "batch": "S261114-11"},
               {"material_no": "SFG-SM-2002", "qty": 60, "batch": "S261114-12"}],
        sent_by="Joseph M. (Sheet Metal)", vehicle_ref="Pickup DXB-K-55210", now=ago(minutes=40))
    core.send_transfer(
        con, job_order_no="JO-2026-1043", from_plant="1100", from_dept="HDR", to_plant="1300",
        lines=[{"material_no": "SFG-HDR-1005", "qty": 6, "batch": "H261114-09"}],
        sent_by="Ravi K. (Headers)", vehicle_ref="Hand trolley", now=ago(minutes=20))
    return con


def _history(con, now):
    """Two weeks of past builds (the COMPLETED job orders in the file), so the
    metrics page has something real to measure. Made through the same rules
    as everything else, with a fixed random seed so it is the same every time."""
    rnd = random.Random(2026)
    with open(JOB_ORDERS) as f:
        data = json.load(f)
    past = [jo for jo in data["job_orders"] if jo["status"] == "COMPLETED"]
    # Back then these job orders were still open in the job order system.
    core.sync_job_orders(con, JOB_ORDERS, now=now - timedelta(days=15),
                         data=dict(data, job_orders=[dict(jo, status="IN_PROGRESS") for jo in past]))
    sender = {"1100": ("HDR", "Ravi K. (Headers)"), "1200": ("SM", "Joseph M. (Sheet Metal)")}
    receiver = {"1300": "Ahmed S. (Assembly stores)", "1100": "Priya N. (Plant 1 stores)"}
    late_slots = {1, 4}  # two sheet-metal trips to Plant 3 that sat at the gate
    trip = 0
    for i, jo in enumerate(past):
        day = now - timedelta(days=14 - i, hours=-rnd.randint(0, 2))
        by_plant = {}
        for c in jo["components"]:
            by_plant.setdefault(c["supply_plant"], []).append(c)
        for k, (plant, comps) in enumerate(sorted(by_plant.items())):
            dept, who = sender[plant]
            t_send = day + timedelta(hours=k * 2, minutes=rnd.randint(0, 40))
            tno = core.send_transfer(
                con, job_order_no=jo["job_order_no"], from_plant=plant, from_dept=dept,
                to_plant=jo["assembly_plant"], sent_by=who, now=t_send,
                vehicle_ref=rnd.choice(["Forklift trolley T-02", "Forklift trolley T-04", "Pickup DXB-K-55210"]),
                lines=[{"material_no": c["material_no"], "qty": c["qty_required"],
                        "batch": f"{dept[0]}{t_send:%y%m%d}-{n + 1:02d}"} for n, c in enumerate(comps)])
            if plant == "1200" and jo["assembly_plant"] == "1300":
                hours_in_transit = rnd.uniform(5.0, 7.5) if trip in late_slots else rnd.uniform(0.8, 3.0)
                trip += 1
            else:
                hours_in_transit = rnd.uniform(0.3, 1.2)
            got, shorts = {}, []
            for n, c in enumerate(comps, start=1):
                rec, dmg = c["qty_required"], 0
                p_short = 0.5 if c["material_no"] == "SFG-SM-2002" else (0.15 if plant == "1200" else 0.08)
                if rnd.random() < p_short:
                    miss = rnd.randint(1, 3)
                    rec -= miss
                    shorts.append((c["material_no"], miss))
                if c["material_no"] == "SFG-SM-2001" and rnd.random() < 0.15:
                    dmg = rnd.randint(1, 2)
                got[n] = {"qty_received": rec, "qty_damaged": dmg}
            t_rec = t_send + timedelta(hours=hours_in_transit)
            core.receive_transfer(con, tno, receiving_plant=jo["assembly_plant"],
                                  received_by=receiver[jo["assembly_plant"]], lines=got, now=t_rec)
            for d in con.execute("SELECT * FROM discrepancies WHERE transfer_no = ? AND status = 'OPEN'",
                                 (tno,)).fetchall():
                text = {"SHORT": "Recounted at dispatch: pieces were left on the rack. Re-sent on the next trip.",
                        "DAMAGED": "Dented parts sent back to Plant 2 for rework; replacements sent.",
                        "EXCESS": "Extra pieces returned to the sending plant."}[d["kind"]]
                core.resolve_discrepancy(con, d["id"], resolved_by=who, resolution=text,
                                         now=t_rec + timedelta(hours=rnd.uniform(1.5, 6)))
            if shorts or any(g["qty_damaged"] for g in got.values()):
                redo = [{"material_no": comps[n - 1]["material_no"],
                         "qty": comps[n - 1]["qty_required"] - g["qty_received"] + g["qty_damaged"]}
                        for n, g in got.items()
                        if comps[n - 1]["qty_required"] - g["qty_received"] + g["qty_damaged"] > 0]
                t2 = t_rec + timedelta(hours=7)
                tno2 = core.send_transfer(con, job_order_no=jo["job_order_no"], from_plant=plant, from_dept=dept,
                                          to_plant=jo["assembly_plant"], sent_by=who, lines=redo, now=t2,
                                          note="Make-up for the shortfall on " + tno)
                core.receive_transfer(con, tno2, receiving_plant=jo["assembly_plant"],
                                      received_by=receiver[jo["assembly_plant"]],
                                      lines={n: {"qty_received": r["qty"]} for n, r in enumerate(redo, start=1)},
                                      now=t2 + timedelta(minutes=rnd.randint(25, 70)))


if __name__ == "__main__":
    path = default_db_path()
    build(path, demo="--empty" not in sys.argv)
    print(f"Database ready: {path}")
