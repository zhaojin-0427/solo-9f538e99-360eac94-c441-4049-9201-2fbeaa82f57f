# -*- coding: utf-8 -*-
"""区段承载量测算：站网校验、等时最短路径精确分摊、失效联动、原子性、持久化。"""
import io
import os
import sys
from fractions import Fraction

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from transit_od import create_app  # noqa: E402
from transit_od.db import connect  # noqa: E402

HEADER = "card_id,event_id,revision,ts_utc,station,direction\n"

# 覆盖：普通异站行程 ×2、同站行程、站点缺失行程、不可达行程
EVENTS = HEADER + """\
C1,V1,1,2026-11-16T08:00:00Z,S1,in
C1,V2,1,2026-11-16T08:30:00Z,S2,out
C2,V3,1,2026-11-16T09:00:00Z,S1,in
C2,V4,1,2026-11-16T09:40:00Z,S3,out
C3,V5,1,2026-11-16T10:00:00Z,S2,in
C3,V6,1,2026-11-16T10:05:00Z,S2,out
C4,V7,1,2026-11-16T11:00:00Z,S1,in
C4,V8,1,2026-11-16T11:20:00Z,SX,out
C5,V9,1,2026-11-16T12:00:00Z,S1,in
C5,V10,1,2026-11-16T12:30:00Z,S4,out
"""

# S1→S3 有两条等时最短路：直达 20 = 经 S2 的 10+10
NETWORK = """\
station_a,station_b,minutes
S1,S2,10
S2,S3,10
S1,S3,20
S4,S5,5
"""

DAY = "2026-11-16"


@pytest.fixture()
def app(tmp_path):
    return create_app(str(tmp_path / "test.db"))


@pytest.fixture()
def client(app):
    return app.test_client()


def upload(client, text, filename="b.csv"):
    return client.post(
        "/upload",
        data={"file": (io.BytesIO(text.encode("utf-8")), filename)},
        content_type="multipart/form-data",
    )


def publish(client, day=DAY):
    return client.post(f"/days/{day}/publish")


def estimate(client, text, day=DAY, filename="net.csv"):
    return client.post(
        "/estimate",
        data={"day": day, "file": (io.BytesIO(text.encode("utf-8")), filename)},
        content_type="multipart/form-data",
    )


def q(app, sql, args=()):
    conn = connect(app.config["DB_PATH"])
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def q1(app, sql, args=()):
    rows = q(app, sql, args)
    return rows[0] if rows else None


def edge_loads(app, est_id):
    """{无序边 (小站, 大站): Fraction 人次}"""
    rows = q(app, "SELECT * FROM estimation_edges WHERE est_id=?", (est_id,))
    out = {}
    for r in rows:
        key = tuple(sorted((r["station_a"], r["station_b"])))
        out[key] = Fraction(int(r["load_num"]), int(r["load_den"]))
    return out


def prepare(client, day=DAY):
    upload(client, EVENTS)
    publish(client, day)


# ---------- 分摊正确性 ----------

def test_estimate_basic_split_and_counts(app, client):
    prepare(client)
    estimate(client, NETWORK)
    est = q1(app, "SELECT * FROM estimations")
    assert est["status"] == "current"
    assert est["pub_id"] == q1(app, "SELECT pub_id FROM publications")["pub_id"]
    assert est["trips_total"] == 5
    assert est["assigned"] == 2
    assert est["same_station"] == 1      # C3 同站
    assert est["missing_station"] == 1   # SX 不在站网
    assert est["unreachable"] == 1       # S4 与 S1 不连通
    assert est["station_count"] == 5 and est["edge_count"] == 4

    loads = edge_loads(app, est["est_id"])
    assert loads == {
        ("S1", "S2"): Fraction(3, 2),   # S1→S2 全程 1 人 + S1→S3 分摊 1/2
        ("S2", "S3"): Fraction(1, 2),
        ("S1", "S3"): Fraction(1, 2),
        ("S4", "S5"): Fraction(0),
    }
    page = client.get(f"/estimate/{est['est_id']}").get_data(as_text=True)
    assert "3/2" in page and "1/2" in page
    assert "同站 1" in page and "站点缺失 1" in page and "不可达 1" in page
    assert f"发布 #{est['pub_id']}" in page


def test_many_equal_time_paths_exact_full_apportionment(app, client):
    """8×10 网格（80 站、142 边）：对角共 C(16,7)=11440 条等时最短路，
    必须完整精确分摊——与暴力枚举全部路径的结果逐边比对。"""
    cols, rows_n = 8, 10  # 站点 (x,y)：x∈[0,7], y∈[0,9]

    def name(x, y):
        return f"N{x}_{y}"

    net_lines = ["station_a,station_b,minutes"]
    for x in range(cols):
        for y in range(rows_n):
            if x + 1 < cols:
                net_lines.append(f"{name(x,y)},{name(x+1,y)},1")
            if y + 1 < rows_n:
                net_lines.append(f"{name(x,y)},{name(x,y+1)},1")
    net = "\n".join(net_lines) + "\n"

    s, t = name(0, 0), name(cols - 1, rows_n - 1)
    upload(client, HEADER +
           f"C1,G1,1,{DAY}T08:00:00Z,{s},in\nC1,G2,1,{DAY}T09:00:00Z,{t},out\n")
    publish(client)
    estimate(client, net)
    est = q1(app, "SELECT * FROM estimations")
    assert est["status"] == "current"
    assert est["station_count"] == 80 and est["edge_count"] == 142
    assert est["assigned"] == 1

    # 暴力枚举全部 11440 条最短路径，逐边计数 / 总条数
    adj = {}
    for line in net_lines[1:]:
        a, b, _ = line.split(",")
        adj.setdefault(a, []).append(b)
        adj.setdefault(b, []).append(a)
    expected = {}
    n_paths = 0

    def dfs(u, path_edges):
        nonlocal n_paths
        if u == t:
            n_paths += 1
            for e in path_edges:
                expected[e] = expected.get(e, 0) + 1
            return
        ux, uy = map(int, u[1:].split("_"))
        for v in adj[u]:
            vx, vy = map(int, v[1:].split("_"))
            if vx + vy == ux + uy + 1:  # 单位权网格：最短路径每步坐标和 +1
                dfs(v, path_edges + [tuple(sorted((u, v)))])

    dfs(s, [])
    assert n_paths == 11440  # C(16,7)：7 步横向 + 9 步纵向

    loads = edge_loads(app, est["est_id"])
    assert len(loads) == 142
    for e, fr in loads.items():
        assert fr == Fraction(expected.get(e, 0), n_paths), e
    # 确实有多条路径分摊（不是单一整数），且各人次合计 = 平均路径长 × 1 人
    assert any(fr.denominator != 1 for fr in loads.values())
    assert sum(loads.values()) == 16  # 每条路径 16 条边 × 1 人次


def test_repeated_trips_accumulate(app, client):
    """同一 OD 多条行程：人次按行程数累加。"""
    upload(client, HEADER +
           f"C1,R1,1,{DAY}T08:00:00Z,S1,in\nC1,R2,1,{DAY}T08:30:00Z,S3,out\n"
           f"C2,R3,1,{DAY}T09:00:00Z,S1,in\nC2,R4,1,{DAY}T09:30:00Z,S3,out\n")
    publish(client)
    estimate(client, NETWORK)
    est = q1(app, "SELECT * FROM estimations")
    assert est["assigned"] == 2
    loads = edge_loads(app, est["est_id"])
    assert loads[("S1", "S3")] == 1      # 2 行程 × 1/2
    assert loads[("S1", "S2")] == 1
    assert loads[("S2", "S3")] == 1


# ---------- 站网 CSV 校验（整份拒绝） ----------

def _assert_network_rejected(app, client, text, needle):
    prepare(client)
    estimate(client, text)
    est = q1(app, "SELECT * FROM estimations ORDER BY est_id DESC")
    assert est["status"] == "failed"
    assert needle in est["error"]
    assert q1(app, "SELECT COUNT(*) n FROM estimation_edges")["n"] == 0
    page = client.get(f"/estimate/{est['est_id']}").get_data(as_text=True)
    assert "测算失败" in page


def test_network_self_loop_rejected(app, client):
    _assert_network_rejected(app, client, "station_a,station_b,minutes\nS1,S1,5\n", "自环")


def test_network_duplicate_edge_rejected(app, client):
    _assert_network_rejected(app, client,
                             "station_a,station_b,minutes\nS1,S2,5\nS1,S2,7\n", "重复边")
    # 反向也算重复（无向边）
    _assert_network_rejected(app, client,
                             "station_a,station_b,minutes\nS1,S2,5\nS2,S1,7\n", "重复边")


def test_network_limits_rejected(app, client):
    # 81 站（80 条链边）
    lines = ["station_a,station_b,minutes"] + [f"A{i},A{i+1},1" for i in range(80)]
    _assert_network_rejected(app, client, "\n".join(lines) + "\n", "超过上限")
    # 161 边（161 个站点的链）
    lines = ["station_a,station_b,minutes"] + [f"B{i},B{i+1},1" for i in range(161)]
    _assert_network_rejected(app, client, "\n".join(lines) + "\n", "超过上限")


def test_network_bad_minutes_rejected(app, client):
    for bad in ("0", "-5", "1.5", "abc", ""):
        _assert_network_rejected(app, client,
                                 f"station_a,station_b,minutes\nS1,S2,{bad}\n", "正整数")


def test_network_structure_rejected(app, client):
    _assert_network_rejected(app, client, "", "为空")
    _assert_network_rejected(app, client, "a,b\n1,2\n", "缺少必需列")
    _assert_network_rejected(app, client, "station_a,station_b,minutes\n", "没有数据行")
    _assert_network_rejected(app, client, "station_a,station_b,minutes\n,S2,5\n", "站点名为空")


def test_network_header_aliases_and_case_insensitive(app, client):
    prepare(client)
    estimate(client, "FROM,TO,MIN\nS1,S2,10\nS2,S3,10\n")
    est = q1(app, "SELECT * FROM estimations ORDER BY est_id DESC")
    assert est["status"] == "current"


# ---------- 来源发布选择 ----------

def test_estimate_uses_latest_valid_publication(app, client):
    prepare(client)
    publish(client)  # 重发，产生更新的发布
    latest_pub = q1(app, "SELECT * FROM publications ORDER BY pub_id DESC")
    estimate(client, NETWORK)
    est = q1(app, "SELECT * FROM estimations")
    assert est["status"] == "current"
    assert est["pub_id"] == latest_pub["pub_id"]


def test_estimate_without_publication_fails(app, client):
    upload(client, EVENTS)  # 不上传发布
    estimate(client, NETWORK)
    est = q1(app, "SELECT * FROM estimations")
    assert est["status"] == "failed" and est["pub_id"] is None
    assert "尚未发布" in est["error"]
    assert q1(app, "SELECT COUNT(*) n FROM estimation_edges")["n"] == 0


def test_estimate_with_stale_publication_fails(app, client):
    prepare(client)
    # 补传使当日发布过期
    upload(client, HEADER + f"C9,X1,1,{DAY}T13:00:00Z,S1,in\nC9,X2,1,{DAY}T14:00:00Z,S2,out\n")
    estimate(client, NETWORK)
    est = q1(app, "SELECT * FROM estimations")
    assert est["status"] == "failed" and "已过期" in est["error"]
    assert q1(app, "SELECT COUNT(*) n FROM estimation_edges")["n"] == 0


# ---------- 失效联动：补传 / 重发与测算交错 ----------

def test_correction_invalidates_estimation(app, client):
    prepare(client)
    estimate(client, NETWORK)
    est = q1(app, "SELECT * FROM estimations")
    assert est["status"] == "current"
    assert "当前有效" in client.get(f"/estimate/{est['est_id']}").get_data(as_text=True)

    # 补传（影响当日）→ 测算失效
    upload(client, HEADER + f"C9,X1,1,{DAY}T13:00:00Z,S1,in\nC9,X2,1,{DAY}T14:00:00Z,S2,out\n")
    assert q1(app, "SELECT status FROM estimations")["status"] == "invalid"
    page = client.get(f"/estimate/{est['est_id']}").get_data(as_text=True)
    assert "已失效" in page
    assert "已失效" in client.get("/estimate").get_data(as_text=True)

    # 重新发布 → 旧测算保持失效；新测算基于新发布且当前有效
    publish(client)
    assert q1(app, "SELECT status FROM estimations WHERE est_id=?", (est["est_id"],))["status"] == "invalid"
    estimate(client, NETWORK)
    est2 = q1(app, "SELECT * FROM estimations ORDER BY est_id DESC")
    assert est2["status"] == "current"
    assert est2["pub_id"] == q1(app, "SELECT pub_id FROM publications ORDER BY pub_id DESC")["pub_id"]
    assert "当前有效" in client.get(f"/estimate/{est2['est_id']}").get_data(as_text=True)


def test_republish_alone_invalidates_estimation(app, client):
    """无数据变化的重发也会取代来源发布，旧测算不能留下当前结果。"""
    prepare(client)
    estimate(client, NETWORK)
    est = q1(app, "SELECT * FROM estimations")
    publish(client)  # 重发，来源发布被取代
    assert q1(app, "SELECT status FROM estimations WHERE est_id=?", (est["est_id"],))["status"] == "invalid"
    assert "已失效" in client.get(f"/estimate/{est['est_id']}").get_data(as_text=True)


def test_failed_or_noop_batch_keeps_estimation_current(app, client):
    prepare(client)
    estimate(client, NETWORK)
    est = q1(app, "SELECT * FROM estimations")
    # 失败批次（同版异文冲突）不影响测算
    upload(client, HEADER + f"C1,V2,1,{DAY}T08:30:00Z,S9,out\n")
    assert q1(app, "SELECT status FROM estimations WHERE est_id=?", (est["est_id"],))["status"] == "current"
    # 无实际变更的原样重传也不影响
    upload(client, EVENTS)
    assert q1(app, "SELECT status FROM estimations WHERE est_id=?", (est["est_id"],))["status"] == "current"


def test_correction_for_other_day_keeps_estimation_current(app, client):
    day2 = "2026-11-18"
    upload(client, EVENTS)
    upload(client, HEADER +
           f"D1,W1,1,{day2}T08:00:00Z,S1,in\nD1,W2,1,{day2}T08:30:00Z,S2,out\n")
    publish(client, DAY)
    publish(client, day2)
    estimate(client, NETWORK, day=DAY)
    estimate(client, NETWORK, day=day2)
    est1 = q1(app, "SELECT * FROM estimations WHERE day=?", (DAY,))
    est2 = q1(app, "SELECT * FROM estimations WHERE day=?", (day2,))
    # 只影响 day2 的补传：day2 测算失效，DAY 测算保持有效
    upload(client, HEADER + f"D2,W3,1,{day2}T09:00:00Z,S1,in\nD2,W4,1,{day2}T09:30:00Z,S3,out\n")
    assert q1(app, "SELECT status FROM estimations WHERE est_id=?", (est1["est_id"],))["status"] == "current"
    assert q1(app, "SELECT status FROM estimations WHERE est_id=?", (est2["est_id"],))["status"] == "invalid"


# ---------- 原子性与持久化 ----------

def test_failed_estimation_leaves_no_partial_results(app, client):
    prepare(client)
    estimate(client, "station_a,station_b,minutes\nS1,S2,10\nS2,S2,5\n")  # 自环 → 整份拒绝
    assert q1(app, "SELECT COUNT(*) n FROM estimation_edges")["n"] == 0
    assert q1(app, "SELECT COUNT(*) n FROM estimations WHERE status='current'")["n"] == 0
    # 随后成功的测算完整落库
    estimate(client, NETWORK)
    est = q1(app, "SELECT * FROM estimations WHERE status='current'")
    assert q1(app, "SELECT COUNT(*) n FROM estimation_edges WHERE est_id=?",
              (est["est_id"],))["n"] == 4


def test_estimate_pages_render(app, client):
    prepare(client)
    estimate(client, NETWORK)
    est = q1(app, "SELECT * FROM estimations")
    for path in ("/estimate", f"/estimate/{est['est_id']}", "/days",
                 f"/days/{DAY}", "/estimate/99999"):
        resp = client.get(path)
        assert resp.status_code == (404 if path.endswith("99999") else 200), path
    assert "区段测算" in client.get(f"/days/{DAY}").get_data(as_text=True)


def test_estimate_persistence_across_restart(app, client, tmp_path):
    prepare(client)
    estimate(client, NETWORK)
    est = q1(app, "SELECT * FROM estimations")
    app2 = create_app(app.config["DB_PATH"])
    client2 = app2.test_client()
    page = client2.get(f"/estimate/{est['est_id']}").get_data(as_text=True)
    assert "当前有效" in page and "3/2" in page
    conn = connect(app.config["DB_PATH"])
    try:
        assert conn.execute("SELECT COUNT(*) n FROM estimation_edges").fetchone()["n"] == 4
    finally:
        conn.close()
