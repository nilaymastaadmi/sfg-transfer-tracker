"""End-to-end checks of the rules that matter. Run: ./run.sh test"""

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tracker import core, seed  # noqa: E402


@pytest.fixture
def con(tmp_path):
    c = seed.build(str(tmp_path / "t.db"), demo=False)
    yield c
    c.close()


def send(con, **kw):
    args = dict(job_order_no="JO-2026-1041", from_plant="1100", from_dept="HDR", to_plant="1300",
                lines=[{"material_no": "SFG-HDR-1001", "qty": 20}], sent_by="Tester")
    args.update(kw)
    return core.send_transfer(con, **args)


def test_send_then_receive_in_full(con):
    tno = send(con)
    t = con.execute("SELECT * FROM transfers WHERE transfer_no=?", (tno,)).fetchone()
    assert t["status"] == "IN_TRANSIT"
    status = core.receive_transfer(con, tno, receiving_plant="1300", received_by="R",
                                   lines={1: {"qty_received": 20}})
    assert status == "RECEIVED"
    types = [r[0] for r in con.execute("SELECT event_type FROM events WHERE transfer_no=? ORDER BY id", (tno,))]
    assert types == ["TRANSFER_SENT", "TRANSFER_RECEIVED"]
    assert con.execute("SELECT COUNT(*) FROM discrepancies").fetchone()[0] == 0


def test_short_and_damaged_create_discrepancies(con):
    tno = send(con, lines=[{"material_no": "SFG-HDR-1001", "qty": 20},
                           {"material_no": "SFG-HDR-1002", "qty": 20}])
    status = core.receive_transfer(con, tno, receiving_plant="1300", received_by="R",
                                   lines={1: {"qty_received": 18}, 2: {"qty_received": 20, "qty_damaged": 1}})
    assert status == "RECEIVED_WITH_DISCREPANCY"
    kinds = sorted((r["kind"], r["qty"]) for r in con.execute("SELECT kind, qty FROM discrepancies"))
    assert kinds == [("DAMAGED", 1), ("SHORT", 2)]


def test_cannot_receive_twice_or_at_wrong_plant(con):
    tno = send(con)
    with pytest.raises(core.TrackerError, match="addressed to plant 1300"):
        core.receive_transfer(con, tno, receiving_plant="1200", received_by="R", lines={1: {"qty_received": 20}})
    core.receive_transfer(con, tno, receiving_plant="1300", received_by="R", lines={1: {"qty_received": 20}})
    with pytest.raises(core.TrackerError, match="already received"):
        core.receive_transfer(con, tno, receiving_plant="1300", received_by="R", lines={1: {"qty_received": 20}})


def test_send_validation(con):
    with pytest.raises(core.TrackerError, match="different"):
        send(con, to_plant="1100")
    with pytest.raises(core.TrackerError, match="bill of materials"):
        send(con, lines=[{"material_no": "SFG-HDR-1003", "qty": 5}])
    with pytest.raises(core.TrackerError, match="more than zero"):
        send(con, lines=[{"material_no": "SFG-HDR-1001", "qty": 0}])
    with pytest.raises(core.TrackerError, match="not in plant"):
        send(con, from_dept="SM")
    assert con.execute("SELECT COUNT(*) FROM transfers").fetchone()[0] == 0


def test_trail_is_append_only_and_tamper_evident(con):
    tno = send(con)
    core.receive_transfer(con, tno, receiving_plant="1300", received_by="R", lines={1: {"qty_received": 20}})
    assert core.verify_trail(con)[0]
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE events SET actor='someone else' WHERE id=2")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("DELETE FROM events WHERE id=2")
    # Simulate someone bypassing the app: drop the guard, edit a row, check the chain.
    con.execute("DROP TRIGGER events_no_update")
    con.execute("UPDATE events SET payload=replace(payload, '20.0', '2.0') WHERE event_type='TRANSFER_SENT'")
    ok, _, bad = core.verify_trail(con)
    assert not ok and bad == 2


def test_removing_the_newest_event_is_caught(con):
    tno = send(con)
    core.receive_transfer(con, tno, receiving_plant="1300", received_by="R", lines={1: {"qty_received": 20}})
    con.execute("DROP TRIGGER events_no_delete")
    con.execute("DELETE FROM events WHERE id = (SELECT MAX(id) FROM events)")
    assert not core.verify_trail(con)[0]


def test_not_received_flag_then_late_receipt_settles_it(con):
    tno = send(con)
    core.report_not_received(con, tno, reported_by="Plant 3")
    with pytest.raises(core.TrackerError, match="already flagged"):
        core.report_not_received(con, tno, reported_by="Plant 3")
    core.receive_transfer(con, tno, receiving_plant="1300", received_by="R", lines={1: {"qty_received": 20}})
    d = con.execute("SELECT * FROM discrepancies WHERE kind='NOT_RECEIVED'").fetchone()
    assert d["status"] == "RESOLVED"


def test_job_order_status_counts_good_parts_at_assembly_plant(con):
    tno = send(con, lines=[{"material_no": "SFG-HDR-1001", "qty": 20}])
    s = core.job_order_status(con, "JO-2026-1041")
    c = next(c for c in s["components"] if c["material_no"] == "SFG-HDR-1001")
    assert (c["qty_in_transit"], c["qty_received_good"], c["qty_missing"]) == (20, 0, 20)
    core.receive_transfer(con, tno, receiving_plant="1300", received_by="R",
                          lines={1: {"qty_received": 20, "qty_damaged": 3}})
    s = core.job_order_status(con, "JO-2026-1041")
    c = next(c for c in s["components"] if c["material_no"] == "SFG-HDR-1001")
    assert (c["qty_in_transit"], c["qty_received_good"], c["qty_missing"]) == (0, 17, 3)
    assert not s["sfg_ready"]


def test_sap_export_has_303_and_305(con):
    tno = send(con)
    core.receive_transfer(con, tno, receiving_plant="1300", received_by="R", lines={1: {"qty_received": 19}})
    rows = core.sap_goods_movements(con)
    assert [(r["MOVE_TYPE"], r["PLANT"], r["ENTRY_QNT"]) for r in rows] == [("303", "1100", 20), ("305", "1300", 19)]


def test_demo_seed_builds_and_verifies(tmp_path):
    c = seed.build(str(tmp_path / "demo.db"), demo=True)
    assert core.verify_trail(c)[0]
    assert len(core.overdue_transfers(c, 4)) == 1
    c.close()


def test_web_send_receive_and_api(tmp_path, monkeypatch):
    db = str(tmp_path / "web.db")
    seed.build(db, demo=True).close()
    import app as appmod
    monkeypatch.setattr(appmod, "DB_PATH", db)
    client = appmod.app.test_client()
    for path in ["/", "/send", "/receive", "/transfers", "/job-orders", "/trail", "/integration",
                 "/job-orders/JO-2026-1041", "/transfers/TRF-2026-00001", "/transfers/TRF-2026-00001/slip",
                 "/export/sap-goods-movements.csv", "/api/events", "/api/trail/verify"]:
        assert client.get(path).status_code == 200, path
    r = client.post("/send", data={"job_order_no": "JO-2026-1043", "from_plant": "1200", "from_dept": "SM",
                                   "to_plant": "1300", "sent_by": "Web", "material_1": "SFG-SM-2003",
                                   "qty_1": "6"})
    assert r.status_code == 302
    tno = r.headers["Location"].rsplit("/", 1)[-1]
    r = client.post(f"/transfers/{tno}/receive", data={"receiving_plant": "1300", "received_by": "Web",
                                                      "rec_1": "6", "dmg_1": "0"})
    assert r.status_code == 302
    assert client.get(f"/api/transfers/{tno}").get_json()["status"] == "RECEIVED"
    r = client.post(f"/api/transfers/{tno}/receive", json={"receiving_plant": "1300", "received_by": "x",
                                                          "lines": [{"line_no": 1, "qty_received": 6}]})
    assert r.status_code == 409
    for bad in ({"lines": [{"line_no": "abc", "qty_received": 1}]}, {"lines": None}, [1, 2]):
        assert client.post("/api/transfers/TRF-2026-00004/receive", json=bad).status_code == 400
    assert client.post("/api/transfers", json=[1]).status_code == 400
    assert client.post("/api/transfers", json={"lines": [5]}).status_code == 400
    assert client.post("/api/transfers", json={"from_plant": ["1200"], "job_order_no": {"a": 1}}).status_code == 400
    slip = client.get("/transfers/TRF-2026-00001/slip").get_data(as_text=True)
    assert "Counted at receipt" in slip and "20 EA" not in slip
    v = client.get("/api/trail/verify").get_json()
    assert v["ok"] and v["table_mismatches"] == []
    assert client.get("/api/trail/verify").get_json()["ok"]


def test_route_and_quantity_follow_the_job_order(con):
    with pytest.raises(core.TrackerError, match="assembled at plant 1300"):
        send(con, from_plant="1200", from_dept="SM", to_plant="1100",
             lines=[{"material_no": "SFG-SM-2001", "qty": 4}])
    with pytest.raises(core.TrackerError, match="comes from plant 1100"):
        send(con, from_plant="1200", from_dept="SM", lines=[{"material_no": "SFG-HDR-1001", "qty": 4}])
    with pytest.raises(core.TrackerError, match="whole pieces"):
        send(con, lines=[{"material_no": "SFG-HDR-1001", "qty": 2.5}])
    with pytest.raises(core.TrackerError, match="at most 20 more"):
        send(con, lines=[{"material_no": "SFG-HDR-1001", "qty": 21}])
    tno = send(con, lines=[{"material_no": "SFG-HDR-1001", "qty": 20}])
    with pytest.raises(core.TrackerError, match="at most 0 more"):
        send(con, lines=[{"material_no": "SFG-HDR-1001", "qty": 1}])
    # 2 arrive short: those 2 can be re-sent, but no more.
    core.receive_transfer(con, tno, receiving_plant="1300", received_by="R", lines={1: {"qty_received": 18}})
    with pytest.raises(core.TrackerError, match="at most 2 more"):
        send(con, lines=[{"material_no": "SFG-HDR-1001", "qty": 3}])
    send(con, lines=[{"material_no": "SFG-HDR-1001", "qty": 2}])


def test_odd_input_types_get_a_message_not_a_crash(con):
    with pytest.raises(core.TrackerError):
        send(con, from_plant=["1100"])
    with pytest.raises(core.TrackerError):
        send(con, job_order_no={"a": 1})


def test_reconcile_catches_direct_table_edits(con):
    tno = send(con, lines=[{"material_no": "SFG-HDR-1001", "qty": 20}])
    core.receive_transfer(con, tno, receiving_plant="1300", received_by="R", lines={1: {"qty_received": 18}})
    assert core.reconcile(con) == []
    con.execute("UPDATE transfer_lines SET qty_received = 20")      # hide the shortage
    con.execute("UPDATE discrepancies SET status = 'RESOLVED'")
    problems = core.reconcile(con)
    assert len(problems) == 2 and core.verify_trail(con)[0]          # chain fine, tables caught


def test_sap_receipt_is_capped_at_sent_quantity(con):
    tno = send(con, lines=[{"material_no": "SFG-HDR-1001", "qty": 10}])
    core.receive_transfer(con, tno, receiving_plant="1300", received_by="R", lines={1: {"qty_received": 12}})
    r305 = [r for r in core.sap_goods_movements(con) if r["MOVE_TYPE"] == "305"][0]
    assert r305["ENTRY_QNT"] == 10 and "2 excess not posted" in r305["ITEM_TEXT"]
