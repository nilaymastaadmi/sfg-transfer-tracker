"""Create a fresh database with master data, the dummy job orders and a short,
realistic history (made through the same functions the app uses, so the trail
is genuine and its hash chain verifies).

Run:  python -m tracker.seed            (demo history)
      python -m tracker.seed --empty    (master data and job orders only)
"""

import os
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

    core.sync_job_orders(con, JOB_ORDERS, now=ago(hours=27))
    if not demo:
        return con

    # 1. Clean transfer: headers to assembly, fully received yesterday.
    t1 = core.send_transfer(
        con, job_order_no="JO-2026-1041", from_plant="1100", from_dept="HDR", to_plant="1300",
        lines=[{"material_no": "SFG-HDR-1001", "qty": 20, "batch": "H261113-01"},
               {"material_no": "SFG-HDR-1002", "qty": 20, "batch": "H261113-02"}],
        sent_by="Ravi K. (Headers)", vehicle_ref="Forklift trolley T-04", now=ago(hours=26))
    core.receive_transfer(
        con, t1, receiving_plant="1300", received_by="Ahmed S. (Assembly stores)",
        lines={1: {"qty_received": 20}, 2: {"qty_received": 20}}, now=ago(hours=25, minutes=20))

    # 2. Quantity mismatch: 2 side channels short, 2 end plates dented.
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

    # 3. Sent but never received: the classic dispute. Plant 3 has flagged it.
    t3 = core.send_transfer(
        con, job_order_no="JO-2026-1042", from_plant="1100", from_dept="HDR", to_plant="1300",
        lines=[{"material_no": "SFG-HDR-1003", "qty": 12, "batch": "H261114-03"},
               {"material_no": "SFG-HDR-1004", "qty": 12, "batch": "H261114-04"}],
        sent_by="Ravi K. (Headers)", vehicle_ref="Forklift trolley T-02", now=ago(hours=9))
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


if __name__ == "__main__":
    path = default_db_path()
    build(path, demo="--empty" not in sys.argv)
    print(f"Database ready: {path}")
