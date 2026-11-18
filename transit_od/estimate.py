"""区段承载量测算：站网解析、等时最短路径精确分摊、测算事务与失效联动。

规则实现（与需求对应）：
- 站网 CSV：两站 + 正整数分钟；无向边；最多 80 站、160 边；
  自环或重复边（含反向重复）整份拒绝；任何行校验错误同样整份拒绝。
- 每条异站行程沿**所有**等时最短路线平均分摊 1 人次：在最短路 DAG 上
  正/反向动态规划统计路径条数，边份额 = 经过该边的最短路径数 / 总路径数，
  用 Fraction 精确累加——路径再多也完整计数，不枚举、不采样。
- 同站、站点缺失、不可达的行程分别计数，不计入边负载。
- 测算在单个 BEGIN IMMEDIATE 事务内完成（解析站网 → 校验来源发布 → 计算 → 落库），
  与上传/发布互斥；失败只留一条 failed 审计记录，不产生半份结果。
- 补传（推进 day_versions）或重发（取代旧发布）时，由调用方在同一事务内
  调 invalidate_estimations_for_days 把受影响日的测算标为 invalid。
"""
import csv
import heapq
import io
import re
from collections import Counter
from fractions import Fraction

from .db import get_day_version, now_iso

MAX_STATIONS = 80
MAX_EDGES = 160
MAX_MINUTES = 10 ** 9
MAX_STATION_LEN = 100

MINUTES_RE = re.compile(r"^[0-9]+$")

# 表头别名（不区分大小写）
COL_A = ("station_a", "from")
COL_B = ("station_b", "to")
COL_M = ("minutes", "minute", "min")


class NetworkFormatError(Exception):
    """站网 CSV 结构层面错误（缺列、无数据行等）。"""


class NetworkRejected(Exception):
    """站网内容违反规则（自环、重复边、超限等），整份拒绝。"""


# ---------- 站网 CSV 解析 ----------

def _norm_station(s, line_no):
    v = (s or "").strip()
    if not v:
        raise NetworkFormatError(f"第 {line_no} 行: 站点名为空")
    if len(v) > MAX_STATION_LEN:
        raise NetworkFormatError(f"第 {line_no} 行: 站点名超长（>{MAX_STATION_LEN} 字符）")
    return v


def _norm_minutes(s, line_no):
    v = (s or "").strip()
    if not MINUTES_RE.match(v):
        raise NetworkFormatError(f"第 {line_no} 行: 分钟数必须为正整数: {s!r}")
    n = int(v)
    if n < 1:
        raise NetworkFormatError(f"第 {line_no} 行: 分钟数必须为正整数: {s!r}")
    if n > MAX_MINUTES:
        raise NetworkFormatError(f"第 {line_no} 行: 分钟数超出合理范围（>{MAX_MINUTES}）: {s!r}")
    return n


def parse_network(text):
    """解析站网 CSV，返回 (stations, edges)。

    stations: 站点名列表（按出现顺序）；edges: [(station_a, station_b, minutes)]（文件顺序）。
    任何错误都抛异常，由调用方整份拒绝。
    """
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise NetworkFormatError("CSV 为空或缺少表头")
    header = {h.strip().lower() for h in reader.fieldnames if h is not None}

    def pick(cands):
        for c in cands:
            if c in header:
                return c
        return None

    ca, cb, cm = pick(COL_A), pick(COL_B), pick(COL_M)
    if ca is None or cb is None or cm is None:
        raise NetworkFormatError(
            "CSV 缺少必需列：需要两站列与分钟列，如 station_a,station_b,minutes"
            "（station_a/station_b 也可用 from/to）"
        )
    # 表头里原始大小写的列名 → 用于 DictReader 取值
    raw = {h.strip().lower(): h for h in reader.fieldnames if h is not None}

    edges = []
    stations = []
    seen_station = set()
    seen_edge = set()
    for line_no, row in enumerate(reader, start=2):
        a = _norm_station(row.get(raw[ca]), line_no)
        b = _norm_station(row.get(raw[cb]), line_no)
        m = _norm_minutes(row.get(raw[cm]), line_no)
        if a == b:
            raise NetworkRejected(f"第 {line_no} 行: 自环边（{a} → {b}），整份拒绝")
        key = (a, b) if a < b else (b, a)
        if key in seen_edge:
            raise NetworkRejected(f"第 {line_no} 行: 重复边（{key[0]} — {key[1]}），整份拒绝")
        seen_edge.add(key)
        for s in (a, b):
            if s not in seen_station:
                seen_station.add(s)
                stations.append(s)
        edges.append((a, b, m))

    if not edges:
        raise NetworkFormatError("CSV 中没有数据行")
    if len(stations) > MAX_STATIONS:
        raise NetworkRejected(f"站点数 {len(stations)} 超过上限 {MAX_STATIONS}，整份拒绝")
    if len(edges) > MAX_EDGES:
        raise NetworkRejected(f"边数 {len(edges)} 超过上限 {MAX_EDGES}，整份拒绝")
    return stations, edges


# ---------- 等时最短路径精确分摊 ----------

def _dijkstra(adj, s):
    """正权无向图单源最短路。adj[u] = [(v, w, edge_idx)]。返回 {node: dist}。"""
    dist = {s: 0}
    pq = [(0, s)]
    while pq:
        d, u = heapq.heappop(pq)
        if d != dist[u]:
            continue
        for v, w, _ in adj[u]:
            nd = d + w
            if v not in dist or nd < dist[v]:
                dist[v] = nd
                heapq.heappush(pq, (nd, v))
    return dist


def _edge_shares(adj, edges, dist, s, t):
    """s→t 所有等时最短路径中，每条边承担的人次份额（Fraction，合计路径数为 1 人）。

    在最短路 DAG（按 dist 递增定向）上：
    - np[v] = s→v 的最短路径条数（正推）；
    - nq[v] = v→t 的最短路径条数（反推）；
    - 边 e=(a,b) 若满足 dist[a]+w==dist[b]，经过它的 s→t 最短路径数 = np[a]*nq[b]。
    每条路径等概率分摊 1 人次，故边份额 = 经过条数 / 总条数。
    """
    order = sorted(dist, key=dist.__getitem__)
    np_ = {s: 1}
    for u in order:
        du = dist[u]
        for v, w, _ in adj[u]:
            if v in dist and dist[v] == du + w:
                np_[v] = np_.get(v, 0) + np_[u]
    total = np_[t]

    nq = {t: 1}
    for u in reversed(order):
        du = dist[u]
        for v, w, _ in adj[u]:
            if v in dist and dist[v] == du + w and v in nq:
                nq[u] = nq.get(u, 0) + nq[v]

    shares = {}
    for i, (a, b, m) in enumerate(edges):
        if a not in dist or b not in dist:
            continue
        through = 0
        if dist[b] == dist[a] + m and b in nq:
            through = np_.get(a, 0) * nq[b]
        elif dist[a] == dist[b] + m and a in nq:
            through = np_.get(b, 0) * nq[a]
        if through:
            shares[i] = Fraction(through, total)
    return shares


def compute_loads(stations, edges, od_counts):
    """对 OD 计数（Counter[(entry, exit) -> 行程数]）计算各边人次。

    返回 (loads, unreachable)：loads 为与 edges 等长的 Fraction 列表；
    unreachable 为站网内不可达的行程数。同站/站点缺失由调用方先行剔除。
    """
    adj = {s: [] for s in stations}
    for i, (a, b, m) in enumerate(edges):
        adj[a].append((b, m, i))
        adj[b].append((a, m, i))

    loads = [Fraction(0) for _ in edges]
    unreachable = 0
    for (s, t), cnt in od_counts.items():
        dist = _dijkstra(adj, s)
        if t not in dist:
            unreachable += cnt
            continue
        for i, share in _edge_shares(adj, edges, dist, s, t).items():
            loads[i] += cnt * share
    return loads, unreachable


# ---------- 测算事务与失效联动 ----------

def invalidate_estimations_for_days(conn, days):
    """把受影响日的 current 测算标为 invalid。须在调用方的事务内调用。"""
    days = sorted(days)
    if not days:
        return
    marks = ",".join("?" for _ in days)
    conn.execute(
        f"UPDATE estimations SET status='invalid' WHERE status='current' AND day IN ({marks})",
        days,
    )


def record_failed_estimation(conn, day, filename, error):
    """失败测算只落一条审计记录（无边数据），自动提交。"""
    cur = conn.execute(
        "INSERT INTO estimations(created_at, day, pub_id, filename, status, error)"
        " VALUES(?,?,?,?,?,?)",
        (now_iso(), day, None, filename, "failed", error[:4000]),
    )
    return cur.lastrowid


def run_estimation(conn, day, filename, text):
    """解析站网并对指定 UTC 日的最新有效发布做区段测算。返回 (est_id, ok, error)。

    整个测算（校验来源发布 → 计算 → 落库）在单个写事务内完成，
    与上传/发布互斥：来源发布在测算提交前失效的，测算不会以 current 落库。
    """
    try:
        stations, edges = parse_network(text)
    except (NetworkFormatError, NetworkRejected) as e:
        msg = str(e)
        return record_failed_estimation(conn, day, filename, msg), False, msg

    conn.execute("BEGIN IMMEDIATE")
    try:
        # 网页选择当日最新且未过期的发布
        pub = conn.execute(
            "SELECT * FROM publications WHERE day=? ORDER BY pub_id DESC LIMIT 1", (day,)
        ).fetchone()
        dv = get_day_version(conn, day)
        if pub is None or pub["status"] != "current" or pub["day_version"] < dv:
            if pub is None:
                msg = f"{day} 尚未发布，请先在日期页面发布"
            else:
                msg = f"{day} 最新发布 #{pub['pub_id']} 已过期，请重新发布后再测算"
            cur = conn.execute(
                "INSERT INTO estimations(created_at, day, pub_id, filename, status, error)"
                " VALUES(?,?,?,?,?,?)",
                (now_iso(), day, None, filename, "failed", msg),
            )
            est_id = cur.lastrowid
            conn.execute("COMMIT")
            return est_id, False, msg

        trips = conn.execute(
            "SELECT entry_station, exit_station FROM publication_trips WHERE pub_id=?",
            (pub["pub_id"],),
        ).fetchall()

        station_set = set(stations)
        same = missing = 0
        od = Counter()
        for t in trips:
            a, b = t["entry_station"], t["exit_station"]
            if a == b:
                same += 1
            elif a not in station_set or b not in station_set:
                missing += 1
            else:
                od[(a, b)] += 1

        loads, unreachable = compute_loads(stations, edges, od)
        assigned = sum(od.values()) - unreachable

        cur = conn.execute(
            "INSERT INTO estimations(created_at, day, pub_id, filename, status, trips_total,"
            " assigned, same_station, missing_station, unreachable, station_count, edge_count)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (now_iso(), day, pub["pub_id"], filename, "current", len(trips),
             assigned, same, missing, unreachable, len(stations), len(edges)),
        )
        est_id = cur.lastrowid
        for i, (a, b, m) in enumerate(edges):
            fr = loads[i]
            conn.execute(
                "INSERT INTO estimation_edges(est_id, seq, station_a, station_b, minutes, load_num, load_den)"
                " VALUES(?,?,?,?,?,?,?)",
                (est_id, i, a, b, m, str(fr.numerator), str(fr.denominator)),
            )
        conn.execute("COMMIT")
        return est_id, True, None
    except Exception:
        conn.execute("ROLLBACK")
        raise
