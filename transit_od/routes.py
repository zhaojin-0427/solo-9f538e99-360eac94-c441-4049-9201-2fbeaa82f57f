"""页面路由：上传、按 UTC 日发布/查看、批次与事件浏览、区段承载量测算。"""
import json
import re
from fractions import Fraction

from flask import (Blueprint, abort, current_app, flash, g, redirect,
                   render_template, request, url_for)

from .db import connect, get_day_version, now_iso
from .estimate import record_failed_estimation, run_estimation
from .ingest import commit_batch, record_failed_batch
from .publish import publish_day

bp = Blueprint("main", __name__)

DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

REASONS = {
    "dangling_entry": "进站悬空（无匹配出站）",
    "orphan_exit": "孤立出站（4 小时内无匹配进站）",
}


def get_db():
    if "db" not in g:
        g.db = connect(current_app.config["DB_PATH"])
    return g.db


def _pub_state(pub, day_version):
    """发布状态：当前有效 / 已过期（被取代或底层数据已变更）。"""
    if pub["status"] != "current":
        return "已过期"
    return "当前有效" if pub["day_version"] >= day_version else "已过期"


def _pub_rows(conn, pub_id):
    trips = conn.execute(
        "SELECT * FROM publication_trips WHERE pub_id=? ORDER BY entry_ts, card_id", (pub_id,)
    ).fetchall()
    anomalies = conn.execute(
        "SELECT * FROM publication_anomalies WHERE pub_id=? ORDER BY ts_utc, card_id", (pub_id,)
    ).fetchall()
    return trips, anomalies


@bp.route("/")
def index():
    return redirect(url_for("main.days"))


@bp.route("/upload", methods=["GET", "POST"])
def upload():
    if request.method == "POST":
        f = request.files.get("file")
        if f is None or not f.filename:
            flash("请选择要上传的 CSV 文件", "err")
            return redirect(url_for("main.upload"))
        raw = f.read()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            msg = "文件不是有效的 UTF-8 编码"
            bid = record_failed_batch(get_db(), f.filename, now_iso(), msg)
            flash(f"上传失败：{msg}", "err")
            return redirect(url_for("main.batch_detail", batch_id=bid))
        batch_id, ok, err = commit_batch(get_db(), f.filename, text)
        if ok:
            flash(f"批次 #{batch_id} 已提交", "ok")
        else:
            flash(f"批次 #{batch_id} 失败：{err[:300]}", "err")
        return redirect(url_for("main.batch_detail", batch_id=batch_id))
    return render_template("upload.html")


@bp.route("/batches")
def batches():
    rows = get_db().execute("SELECT * FROM batches ORDER BY batch_id DESC LIMIT 500").fetchall()
    return render_template("batches.html", rows=rows)


@bp.route("/batches/<int:batch_id>")
def batch_detail(batch_id):
    conn = get_db()
    b = conn.execute("SELECT * FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
    if b is None:
        abort(404)
    details = json.loads(b["details"]) if b["details"] else []
    days = [r["day"] for r in conn.execute(
        "SELECT day FROM batch_affected_days WHERE batch_id=? ORDER BY day", (batch_id,))]
    return render_template("batch_detail.html", b=b, details=details, days=days)


@bp.route("/events")
def events():
    rows = get_db().execute(
        "SELECT * FROM events ORDER BY card_id, ts_epoch_us, event_id LIMIT 2000").fetchall()
    return render_template("events.html", rows=rows)


@bp.route("/days")
def days():
    conn = get_db()
    ev_counts = {r["day"]: r["n"] for r in conn.execute(
        "SELECT substr(ts_utc,1,10) AS day, COUNT(*) AS n FROM events GROUP BY day")}
    latest = {}
    for r in conn.execute(
        "SELECT p.* FROM publications p"
        " JOIN (SELECT day, MAX(pub_id) AS m FROM publications GROUP BY day) t ON t.m = p.pub_id"
    ):
        latest[r["day"]] = r
    dvs = {r["day"]: r["version"] for r in conn.execute("SELECT day, version FROM day_versions")}
    all_days = sorted(set(ev_counts) | set(latest), reverse=True)
    rows = []
    for d in all_days:
        pub = latest.get(d)
        state = "未发布" if pub is None else _pub_state(pub, dvs.get(d, 0))
        rows.append({"day": d, "events": ev_counts.get(d, 0), "pub": pub, "state": state})
    return render_template("days.html", rows=rows)


@bp.route("/days/<day>")
def day_detail(day):
    if not DAY_RE.match(day):
        abort(404)
    conn = get_db()
    pubs = conn.execute(
        "SELECT * FROM publications WHERE day=? ORDER BY pub_id DESC", (day,)).fetchall()
    dv = get_day_version(conn, day)
    ev_count = conn.execute(
        "SELECT COUNT(*) AS n FROM events WHERE substr(ts_utc,1,10)=?", (day,)).fetchone()["n"]
    latest = pubs[0] if pubs else None
    trips, anomalies = ([], [])
    if latest:
        trips, anomalies = _pub_rows(conn, latest["pub_id"])
    pub_list = [(p, _pub_state(p, dv)) for p in pubs]
    return render_template(
        "day_detail.html", day=day, pubs=pub_list, latest=latest,
        latest_state=(_pub_state(latest, dv) if latest else None),
        trips=trips, anomalies=anomalies, ev_count=ev_count, reasons=REASONS)


@bp.route("/days/<day>/publish", methods=["POST"])
def publish(day):
    if not DAY_RE.match(day):
        abort(404)
    pub_id = publish_day(get_db(), day)
    flash(f"已发布 {day}：发布号 #{pub_id}（基于同一已提交事件集快照）", "ok")
    return redirect(url_for("main.day_detail", day=day))


@bp.route("/publications/<int:pub_id>")
def publication(pub_id):
    conn = get_db()
    pub = conn.execute("SELECT * FROM publications WHERE pub_id=?", (pub_id,)).fetchone()
    if pub is None:
        abort(404)
    dv = get_day_version(conn, pub["day"])
    trips, anomalies = _pub_rows(conn, pub_id)
    return render_template("publication.html", pub=pub, state=_pub_state(pub, dv),
                           trips=trips, anomalies=anomalies, reasons=REASONS)


# ---------- 区段承载量测算 ----------

def _est_state(est, pubs, dvs):
    """测算状态：失败 / 当前有效 / 已失效（来源发布被取代或底层数据已变更）。"""
    if est["status"] == "failed":
        return "失败"
    if est["status"] != "current":
        return "已失效"
    pub = pubs.get(est["pub_id"])
    if pub is None or pub["status"] != "current":
        return "已失效"
    return "当前有效" if pub["day_version"] >= dvs.get(pub["day"], 0) else "已失效"


def _est_context(conn):
    """测算页公共数据：各测算记录及其状态。"""
    pubs = {r["pub_id"]: r for r in conn.execute("SELECT * FROM publications")}
    dvs = {r["day"]: r["version"] for r in conn.execute("SELECT day, version FROM day_versions")}
    return pubs, dvs


def _fmt_load(load_num, load_den):
    """精确分数人次的展示：整数直接显示，否则显示分数与近似小数。"""
    fr = Fraction(int(load_num), int(load_den))
    if fr.denominator == 1:
        return str(fr.numerator)
    return f"{fr.numerator}/{fr.denominator} ≈ {float(fr):.4f}"


@bp.route("/estimate", methods=["GET", "POST"])
def estimate():
    conn = get_db()
    if request.method == "POST":
        day = request.form.get("day", "")
        if not DAY_RE.match(day):
            flash("请选择要测算的 UTC 日期", "err")
            return redirect(url_for("main.estimate"))
        f = request.files.get("file")
        if f is None or not f.filename:
            flash("请选择要上传的站网 CSV 文件", "err")
            return redirect(url_for("main.estimate", day=day))
        raw = f.read()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            msg = "文件不是有效的 UTF-8 编码"
            eid = record_failed_estimation(conn, day, f.filename, msg)
            flash(f"测算失败：{msg}", "err")
            return redirect(url_for("main.estimate_detail", est_id=eid))
        est_id, ok, err = run_estimation(conn, day, f.filename, text)
        if ok:
            flash(f"测算 #{est_id} 已完成", "ok")
        else:
            flash(f"测算 #{est_id} 失败：{err[:300]}", "err")
        return redirect(url_for("main.estimate_detail", est_id=est_id))

    # GET：日期选项（各日最新发布及状态）+ 测算历史
    latest = {}
    for r in conn.execute(
        "SELECT p.* FROM publications p"
        " JOIN (SELECT day, MAX(pub_id) AS m FROM publications GROUP BY day) t ON t.m = p.pub_id"
    ):
        latest[r["day"]] = r
    dvs = {r["day"]: r["version"] for r in conn.execute("SELECT day, version FROM day_versions")}
    day_opts = [
        {"day": d, "pub": latest[d], "state": _pub_state(latest[d], dvs.get(d, 0))}
        for d in sorted(latest, reverse=True)
    ]
    pubs, dvs2 = _est_context(conn)
    ests = [
        {"e": e, "state": _est_state(e, pubs, dvs2)}
        for e in conn.execute("SELECT * FROM estimations ORDER BY est_id DESC LIMIT 200")
    ]
    return render_template("estimate.html", day_opts=day_opts, ests=ests,
                           selected_day=request.args.get("day", ""))


@bp.route("/estimate/<int:est_id>")
def estimate_detail(est_id):
    conn = get_db()
    est = conn.execute("SELECT * FROM estimations WHERE est_id=?", (est_id,)).fetchone()
    if est is None:
        abort(404)
    pubs, dvs = _est_context(conn)
    state = _est_state(est, pubs, dvs)
    pub = pubs.get(est["pub_id"]) if est["pub_id"] is not None else None
    rows = conn.execute(
        "SELECT * FROM estimation_edges WHERE est_id=? ORDER BY seq", (est_id,)
    ).fetchall()
    # 按人次降序展示，便于调度查看高负载区段
    edges = sorted(
        (
            {
                "station_a": r["station_a"],
                "station_b": r["station_b"],
                "minutes": r["minutes"],
                "load": Fraction(int(r["load_num"]), int(r["load_den"])),
                "load_text": _fmt_load(r["load_num"], r["load_den"]),
            }
            for r in rows
        ),
        key=lambda e: (-e["load"], e["station_a"], e["station_b"]),
    )
    return render_template("estimate_detail.html", est=est, state=state, pub=pub, edges=edges)
