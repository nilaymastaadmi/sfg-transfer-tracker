"""SFG inter-plant transfer tracker: web pages and JSON API.

Run locally with ./run.sh, then open http://127.0.0.1:5050
"""

import csv
import io
import json
import os

from flask import (Flask, Response, abort, flash, g, jsonify, redirect, render_template,
                   request, url_for)

from werkzeug.middleware.proxy_fix import ProxyFix

from tracker import core, seed

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)  # correct links behind a host's proxy
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-only-not-secret")
DB_PATH = seed.default_db_path()
SLA_HOURS = float(os.environ.get("TRANSIT_SLA_HOURS", "4"))
STATUS_LABEL = {"IN_TRANSIT": "In transit", "RECEIVED": "Received",
                "RECEIVED_WITH_DISCREPANCY": "Received with discrepancy"}


def ensure_db():
    if not os.path.exists(DB_PATH):
        seed.build(DB_PATH, demo=True).close()


ensure_db()


def db():
    if "db" not in g:
        g.db = core.connect(DB_PATH)
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    con = g.pop("db", None)
    if con is not None:
        con.close()


@app.template_filter("local")
def local_time(s):
    return core.fmt_local(s)


@app.template_filter("qty")
def fmt_qty(x):
    return "" if x is None else f"{x:g}"


@app.context_processor
def common():
    con = db()
    return {
        "STATUS_LABEL": STATUS_LABEL,
        "plant_names": {r["plant_code"]: r["name"] for r in con.execute("SELECT * FROM plants")},
        "me": request.cookies.get("sfg_name", ""),
        "SLA_HOURS": SLA_HOURS,
    }


def remember_name(resp, name):
    if name:
        resp.set_cookie("sfg_name", name.strip(), max_age=60 * 60 * 24 * 30, samesite="Lax")
    return resp


def plants():
    return db().execute("SELECT * FROM plants ORDER BY plant_code").fetchall()


def transfer_or_404(tno):
    t = db().execute("SELECT * FROM transfers WHERE transfer_no = ?", (tno,)).fetchone()
    if not t:
        abort(404)
    return t


def lines_of(tno):
    return db().execute(
        "SELECT l.*, m.description FROM transfer_lines l JOIN materials m USING (material_no)"
        " WHERE transfer_no = ? ORDER BY line_no", (tno,)).fetchall()


def events_of(tno):
    rows = db().execute("SELECT * FROM events WHERE transfer_no = ? ORDER BY id", (tno,)).fetchall()
    return [dict(r, data=json.loads(r["payload"])) for r in rows]


# --- pages -----------------------------------------------------------------

@app.route("/")
def dashboard():
    con = db()
    counts = {
        "in_transit": con.execute("SELECT COUNT(*) FROM transfers WHERE status='IN_TRANSIT'").fetchone()[0],
        "overdue": len(core.overdue_transfers(con, SLA_HOURS)),
        "open_disc": con.execute("SELECT COUNT(*) FROM discrepancies WHERE status='OPEN'").fetchone()[0],
        "total": con.execute("SELECT COUNT(*) FROM transfers").fetchone()[0],
    }
    queue = con.execute("SELECT * FROM transfers WHERE status='IN_TRANSIT' ORDER BY sent_at").fetchall()
    overdue = {t["transfer_no"] for t in core.overdue_transfers(con, SLA_HOURS)}
    flagged = {r[0] for r in con.execute(
        "SELECT transfer_no FROM discrepancies WHERE kind='NOT_RECEIVED' AND status='OPEN'")}
    open_disc = con.execute(
        "SELECT d.*, l.material_no FROM discrepancies d LEFT JOIN transfer_lines l"
        " ON l.transfer_no = d.transfer_no AND l.line_no = d.line_no"
        " WHERE d.status='OPEN' ORDER BY d.raised_at DESC").fetchall()
    recent = [dict(r, data=json.loads(r["payload"])) for r in
              con.execute("SELECT * FROM events ORDER BY id DESC LIMIT 8")]
    jos = [core.job_order_status(con, r[0]) for r in
           con.execute("SELECT job_order_no FROM job_orders ORDER BY due_date")]
    return render_template("dashboard.html", counts=counts, queue=queue, overdue=overdue,
                           flagged=flagged, open_disc=open_disc, recent=recent, jos=jos)


@app.route("/send", methods=["GET", "POST"])
def send():
    con = db()
    if request.method == "POST":
        f = request.form
        lines = [{"material_no": f.get(f"material_{i}", ""), "qty": f.get(f"qty_{i}", ""),
                  "batch": f.get(f"batch_{i}", "")} for i in range(1, 6)]
        try:
            tno = core.send_transfer(
                con, job_order_no=f.get("job_order_no", ""), from_plant=f.get("from_plant", ""),
                from_dept=f.get("from_dept", ""), to_plant=f.get("to_plant", ""), lines=lines,
                sent_by=f.get("sent_by", ""), vehicle_ref=f.get("vehicle_ref", ""),
                note=f.get("note", ""))
        except core.TrackerError as e:
            flash(str(e), "error")
        else:
            flash(f"{tno} logged as sent. Print the slip and send it with the parts.", "ok")
            return remember_name(redirect(url_for("transfer_detail", tno=tno)), f.get("sent_by"))
    jos = con.execute("SELECT * FROM job_orders WHERE status IN ('RELEASED','IN_PROGRESS')"
                      " ORDER BY job_order_no").fetchall()
    bom = {}
    for r in con.execute("SELECT c.job_order_no, c.material_no, c.qty_required, c.supply_plant,"
                         " m.description, m.uom FROM job_order_components c JOIN materials m USING (material_no)"):
        bom.setdefault(r["job_order_no"], []).append(dict(r))
    jo_info = {j["job_order_no"]: {"assembly_plant": j["assembly_plant"],
                                   "description": j["description"]} for j in jos}
    depts = [dict(d) for d in con.execute("SELECT * FROM departments ORDER BY plant_code, dept_code")]
    return render_template("send.html", jos=jos, plants=plants(), depts=depts, bom=bom,
                           jo_info=jo_info, form=request.form)


@app.route("/receive")
def receive_queue():
    con = db()
    plant = request.args.get("plant", "")
    q = "SELECT * FROM transfers WHERE status='IN_TRANSIT'"
    args = []
    if plant:
        q += " AND to_plant = ?"
        args.append(plant)
    rows = con.execute(q + " ORDER BY sent_at", args).fetchall()
    overdue = {t["transfer_no"] for t in core.overdue_transfers(con, SLA_HOURS)}
    return render_template("receive_queue.html", rows=rows, plants=plants(), plant=plant,
                           overdue=overdue)


@app.route("/transfers/<tno>/receive", methods=["GET", "POST"])
def receive(tno):
    con = db()
    t = transfer_or_404(tno)
    lines = lines_of(tno)
    if request.method == "POST":
        f = request.form
        given = {ln["line_no"]: {"qty_received": f.get(f"rec_{ln['line_no']}", ""),
                                 "qty_damaged": f.get(f"dmg_{ln['line_no']}", "") or 0} for ln in lines}
        try:
            status = core.receive_transfer(con, tno, receiving_plant=f.get("receiving_plant", ""),
                                           received_by=f.get("received_by", ""), lines=given,
                                           note=f.get("note", ""))
        except core.TrackerError as e:
            flash(str(e), "error")
        else:
            if status == "RECEIVED":
                flash(f"{tno} received in full. Trail updated.", "ok")
            else:
                flash(f"{tno} received with a discrepancy. It has been logged for both plants to settle.",
                      "warn")
            return remember_name(redirect(url_for("transfer_detail", tno=tno)), f.get("received_by"))
    return render_template("receive.html", t=t, lines=lines, plants=plants(), form=request.form)


@app.route("/transfers")
def transfers():
    con = db()
    status = request.args.get("status", "")
    jo = request.args.get("jo", "")
    q, args = "SELECT * FROM transfers WHERE 1=1", []
    if status:
        q += " AND status = ?"
        args.append(status)
    if jo:
        q += " AND job_order_no = ?"
        args.append(jo)
    rows = con.execute(q + " ORDER BY sent_at DESC", args).fetchall()
    overdue = {t["transfer_no"] for t in core.overdue_transfers(con, SLA_HOURS)}
    return render_template("transfers.html", rows=rows, status=status, jo=jo, overdue=overdue)


@app.route("/transfers/<tno>")
def transfer_detail(tno):
    con = db()
    t = transfer_or_404(tno)
    disc = con.execute(
        "SELECT d.*, l.material_no FROM discrepancies d LEFT JOIN transfer_lines l"
        " ON l.transfer_no = d.transfer_no AND l.line_no = d.line_no"
        " WHERE d.transfer_no = ? ORDER BY d.id", (tno,)).fetchall()
    is_overdue = any(o["transfer_no"] == tno for o in core.overdue_transfers(con, SLA_HOURS))
    flagged = any(d["kind"] == "NOT_RECEIVED" and d["status"] == "OPEN" for d in disc)
    return render_template("transfer.html", t=t, lines=lines_of(tno), events=events_of(tno),
                           disc=disc, is_overdue=is_overdue, flagged=flagged)


@app.route("/transfers/<tno>/slip")
def slip(tno):
    t = transfer_or_404(tno)
    receive_url = url_for("receive", tno=tno, _external=True)
    return render_template("slip.html", t=t, lines=lines_of(tno), receive_url=receive_url)


@app.route("/transfers/<tno>/not-received", methods=["POST"])
def not_received(tno):
    transfer_or_404(tno)
    try:
        core.report_not_received(db(), tno, reported_by=request.form.get("reported_by", ""),
                                 note=request.form.get("note", ""))
    except core.TrackerError as e:
        flash(str(e), "error")
    else:
        flash(f"{tno} flagged as not received. Both plants can see it on the dashboard.", "warn")
    return remember_name(redirect(url_for("transfer_detail", tno=tno)), request.form.get("reported_by"))


@app.route("/discrepancies/<int:did>/resolve", methods=["POST"])
def resolve(did):
    d = db().execute("SELECT * FROM discrepancies WHERE id = ?", (did,)).fetchone()
    if not d:
        abort(404)
    try:
        core.resolve_discrepancy(db(), did, resolved_by=request.form.get("resolved_by", ""),
                                 resolution=request.form.get("resolution", ""))
    except core.TrackerError as e:
        flash(str(e), "error")
    else:
        flash("Discrepancy resolved and logged.", "ok")
    return remember_name(redirect(url_for("transfer_detail", tno=d["transfer_no"])),
                         request.form.get("resolved_by"))


@app.route("/job-orders")
def job_orders():
    con = db()
    jos = [core.job_order_status(con, r[0]) for r in
           con.execute("SELECT job_order_no FROM job_orders ORDER BY due_date")]
    synced = con.execute("SELECT MAX(synced_at) FROM job_orders").fetchone()[0]
    return render_template("job_orders.html", jos=jos, synced=synced)


@app.route("/job-orders/<jo>")
def job_order_detail(jo):
    con = db()
    s = core.job_order_status(con, jo)
    if not s:
        abort(404)
    rows = con.execute("SELECT * FROM transfers WHERE job_order_no = ? ORDER BY sent_at DESC",
                       (jo,)).fetchall()
    return render_template("job_order.html", s=s, rows=rows)


@app.route("/job-orders/sync", methods=["POST"])
def job_orders_sync():
    n = core.sync_job_orders(db(), seed.JOB_ORDERS, actor=request.cookies.get("sfg_name") or "system")
    flash(f"Re-read {n} job orders from data/job_orders.json.", "ok")
    return redirect(url_for("job_orders"))


@app.route("/trail")
def trail():
    con = db()
    ok, n, bad = core.verify_trail(con)
    problems = core.reconcile(con)
    rows = [dict(r, data=json.loads(r["payload"])) for r in
            con.execute("SELECT * FROM events ORDER BY id DESC")]
    return render_template("trail.html", rows=rows, ok=ok, n=n, bad=bad, problems=problems)


@app.route("/integration")
def integration():
    return render_template("integration.html")


# --- JSON API: the integration boundary ------------------------------------
# This is what the job order system, a handheld scanner app or an SAP
# middleware (for example SAP Integration Suite) would call.

def api_error(msg, code=400):
    return jsonify({"error": msg}), code


def transfer_json(tno):
    t = transfer_or_404(tno)
    return dict(t, lines=[dict(ln) for ln in lines_of(tno)],
                discrepancies=[dict(d) for d in db().execute(
                    "SELECT * FROM discrepancies WHERE transfer_no = ? ORDER BY id", (tno,))])


@app.get("/api/transfers")
def api_transfers():
    q, args = "SELECT transfer_no FROM transfers WHERE 1=1", []
    for key, col in (("status", "status"), ("job_order_no", "job_order_no"),
                     ("to_plant", "to_plant"), ("from_plant", "from_plant")):
        if request.args.get(key):
            q += f" AND {col} = ?"
            args.append(request.args[key])
    return jsonify([transfer_json(r[0]) for r in db().execute(q + " ORDER BY sent_at", args)])


@app.get("/api/transfers/<tno>")
def api_transfer(tno):
    return jsonify(transfer_json(tno))


@app.post("/api/transfers")
def api_send():
    b = request.get_json(silent=True)
    if not isinstance(b, dict):
        return api_error("Send a JSON object.")
    try:
        tno = core.send_transfer(db(), job_order_no=b.get("job_order_no", ""),
                                 from_plant=b.get("from_plant", ""), from_dept=b.get("from_dept", ""),
                                 to_plant=b.get("to_plant", ""), lines=b.get("lines", []),
                                 sent_by=b.get("sent_by", ""), vehicle_ref=b.get("vehicle_ref", ""),
                                 note=b.get("note", ""))
    except core.TrackerError as e:
        return api_error(str(e))
    return jsonify(transfer_json(tno)), 201


@app.post("/api/transfers/<tno>/receive")
def api_receive(tno):
    b = request.get_json(silent=True)
    if not isinstance(b, dict) or not isinstance(b.get("lines"), list):
        return api_error('Send JSON like {"receiving_plant": "1300", "received_by": "name",'
                         ' "lines": [{"line_no": 1, "qty_received": 20, "qty_damaged": 0}]}')
    lines = {}
    for ln in b["lines"]:
        try:
            n = int(ln["line_no"])
        except (TypeError, KeyError, ValueError):
            return api_error("each line needs a numeric line_no")
        if n in lines:
            return api_error(f"line_no {n} appears twice")
        lines[n] = ln
    try:
        core.receive_transfer(db(), tno, receiving_plant=b.get("receiving_plant", ""),
                              received_by=b.get("received_by", ""), lines=lines, note=b.get("note", ""))
    except core.TrackerError as e:
        return api_error(str(e), 404 if "does not exist" in str(e) else 409)
    return jsonify(transfer_json(tno))


@app.get("/api/job-orders/<jo>/sfg-status")
def api_jo_status(jo):
    s = core.job_order_status(db(), jo)
    return jsonify(s) if s else api_error("job order not found", 404)


@app.get("/api/events")
def api_events():
    """Event feed. A consumer remembers the last id it saw and asks for
    ?after_id=<that id>, so nothing is missed or read twice."""
    after = request.args.get("after_id", 0, type=int)
    limit = min(request.args.get("limit", 200, type=int), 1000)
    rows = db().execute("SELECT * FROM events WHERE id > ? ORDER BY id LIMIT ?", (after, limit)).fetchall()
    return jsonify([dict(r, payload=json.loads(r["payload"])) for r in rows])


@app.get("/api/trail/verify")
def api_verify():
    ok, n, bad = core.verify_trail(db())
    problems = core.reconcile(db())
    return jsonify({"ok": ok and not problems, "chain_ok": ok, "events_checked": n,
                    "first_bad_event_id": bad, "table_mismatches": problems})


def csv_response(rows, filename, fields=None):
    buf = io.StringIO()
    if rows:
        w = csv.DictWriter(buf, fieldnames=fields or list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})


@app.get("/export/sap-goods-movements.csv")
def export_sap():
    return csv_response(core.sap_goods_movements(db()), "sap_goods_movements.csv")


@app.get("/export/events.csv")
def export_events():
    rows = [dict(r) for r in db().execute("SELECT * FROM events ORDER BY id")]
    return csv_response(rows, "sfg_transfer_trail.csv")


@app.errorhandler(404)
def not_found(_e):
    if request.path.startswith("/api/"):
        return api_error("not found", 404)
    return render_template("404.html"), 404


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5050)), debug=False)
