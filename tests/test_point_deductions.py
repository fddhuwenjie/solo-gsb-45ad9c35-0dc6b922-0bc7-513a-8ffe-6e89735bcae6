"""赛务争议扣分账本模块测试。

覆盖需求：
- 登记扣分：半分整数倍分值、理由；非半分倍数 422；
- 操作窗口：存在 open 轮或待处理赛果复核时 409 不落库；全部轮次
  结束（或尚无轮次）且无待处理复核时允许；
- expected_event_seq 乐观快照：不一致 409 不落库、不占事件序号；
- request_id 幂等：重放返回首次结果（无新事件），同键不同参数 409；
- 撤销：用新记录撤销仍生效的扣分（原行 revoked、追加 effective
  撤销行、有效扣分归零）；重复撤销 409 不落库；撤销撤销行 409；
  撤销重放返回首次结果；跨赛事引用 404；
- 排名：保留对局积分 game_points，返回有效扣分 deduction 与排名
  积分 points（可为负）；排序、Buchholz（随对手排名积分重算）与
  下一轮积分分组均以排名积分为准；
- 账本：按参赛者查询完整账本（含净额），赛事完整账本与单条查询；
- 事件：point_deduction.registered / .revoked 进入事件流，被拒绝
  请求不产生事件；
- 并发：并发重复撤销恰好一次成功（部分唯一索引 + 写锁兜底）。
"""
import concurrent.futures
import threading

import pytest


# ---------- 基础工具 ----------

def create_tournament(client, name="扣分测试赛"):
    r = client.post("/tournaments", json={"name": name})
    assert r.status_code == 201
    return r.json()["id"]


def add_players(client, tid, names):
    r = client.post(
        f"/tournaments/{tid}/participants/bulk", json={"names": names}
    )
    assert r.status_code == 201
    return r.json()


def next_round(client, tid):
    r = client.post(f"/tournaments/{tid}/rounds/next")
    assert r.status_code == 201, r.text
    return r.json()


def put_result(client, match, sw, sb):
    mid = match["id"] if isinstance(match, dict) else match
    r = client.put(
        f"/matches/{mid}/result",
        json={"score_white": sw, "score_black": sb},
    )
    assert r.status_code == 200, r.text
    return r.json()


def event_seq(client, tid):
    return client.get(f"/tournaments/{tid}/events").json()["last_seq"]


def events_of(client, tid, type_=None):
    items = client.get(f"/tournaments/{tid}/events?limit=200").json()["items"]
    return [e for e in items if type_ is None or e["type"] == type_]


def standings_map(client, tid):
    return {
        row["participant_id"]: row
        for row in client.get(f"/tournaments/{tid}/standings").json()
    }


def deduct(client, tid, participant_id, **over):
    body = {
        "request_id": "d-1",
        "amount": 0.5,
        "reason": "迟到",
    }
    if "expected_event_seq" not in over:
        body["expected_event_seq"] = event_seq(client, tid)
    body.update(over)
    return client.post(
        f"/tournaments/{tid}/participants/{participant_id}/deductions",
        json=body,
    )


def revoke(client, tid, deduction_id, **over):
    body = {
        "request_id": "r-1",
        "expected_event_seq": None,
        "reason": "申诉成立",
    }
    if "expected_event_seq" not in over:
        body["expected_event_seq"] = event_seq(client, tid)
    body.update(over)
    return client.post(
        f"/tournaments/{tid}/point-deductions/{deduction_id}/revoke",
        json=body,
    )

def ledger(client, tid, participant_id):
    return client.get(
        f"/tournaments/{tid}/participants/{participant_id}/deductions"
    )


def setup_closed_round(client, n=4, whites_win=True):
    """n 人赛事，第 1 轮全部以白胜（或全和）录入并封盘。"""
    tid = create_tournament(client)
    players = add_players(client, tid, [f"选手{i}" for i in range(1, n + 1)])
    rnd = next_round(client, tid)
    real = [m for m in rnd["matches"] if not m["is_bye"]]
    for m in real:
        put_result(client, m, 1.0, 0.0) if whites_win else put_result(
            client, m, 0.5, 0.5
        )
    rounds = client.get(f"/tournaments/{tid}/rounds").json()
    assert rounds[-1]["status"] == "closed"
    return tid, players, rnd, real


# ---------- 一、登记扣分 ----------

def test_register_deduction_returns_effective_entry(client):
    tid, players, rnd, real = setup_closed_round(client)
    w = real[0]["player_white"]["id"]
    seq = event_seq(client, tid)

    r = deduct(client, tid, w, amount=1.5)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["entry_type"] == "deduction"
    assert body["amount"] == 1.5
    assert body["status"] == "effective"
    assert body["reversed_entry_id"] is None
    assert body["participant_id"] == w
    assert body["expected_event_seq"] == seq

    # 事件同事务写入，序号恰好 +1
    assert event_seq(client, tid) == seq + 1
    evs = events_of(client, tid, "point_deduction.registered")
    assert len(evs) == 1
    assert evs[0]["payload"]["participant_id"] == w
    assert evs[0]["payload"]["amount"] == 1.5
    assert evs[0]["payload"]["entry_id"] == body["id"]


def test_deduction_amount_must_be_half_multiple(client):
    tid, players, rnd, real = setup_closed_round(client)
    w = real[0]["player_white"]["id"]
    # 0.3 不是半分整数倍 -> 422
    r = deduct(client, tid, w, request_id="bad-1", amount=0.3)
    assert r.status_code == 422
    # 必须为正
    r = deduct(client, tid, w, request_id="bad-2", amount=0)
    assert r.status_code == 422


def test_register_requires_closed_rounds_and_no_pending_review(client):
    tid = create_tournament(client)
    players = add_players(client, tid, ["甲", "乙"])
    p1 = players[0]["id"]

    # 尚无轮次：允许（无 open 轮、无待处理复核）
    r = deduct(client, tid, p1, request_id="early", amount=0.5)
    assert r.status_code == 201, r.text

    # 生成第 1 轮但未录完 -> 存在 open 轮，409 不落库
    rnd = next_round(client, tid)
    seq = event_seq(client, tid)
    r = deduct(client, tid, p1, request_id="open-1", amount=0.5)
    assert r.status_code == 409
    assert "尚未结束" in r.json()["detail"]
    assert event_seq(client, tid) == seq

    # 录入后封盘，但存在待处理复核 -> 仍 409
    m = rnd["matches"][0]
    put_result(client, m, 1.0, 0.0)
    rv = client.post(
        f"/matches/{m['id']}/result-reviews",
        json={
            "request_id": "rev-1",
            "expected_event_seq": event_seq(client, tid),
            "score_white": 0.0,
            "score_black": 1.0,
            "reason": "x",
        },
    )
    assert rv.status_code == 201
    seq = event_seq(client, tid)
    r = deduct(client, tid, p1, request_id="pend-1", amount=0.5)
    assert r.status_code == 409
    assert "复核" in r.json()["detail"]
    assert event_seq(client, tid) == seq

    # 复核关闭后允许
    client.post(
        f"/result-reviews/{rv.json()['id']}/decisions",
        json={"participant_id": p1, "vote": "reject"},
    )
    r = deduct(client, tid, p1, request_id="after-1", amount=0.5)
    assert r.status_code == 201, r.text


def test_register_nonexistent_tournament_or_participant_404(client):
    tid = create_tournament(client)
    add_players(client, tid, ["甲"])
    r = deduct(client, 999, 1, request_id="x", expected_event_seq=0)
    assert r.status_code == 404
    r = deduct(client, tid, 999, request_id="x")
    assert r.status_code == 404


def test_snapshot_conflict_rejects_without_write(client):
    tid, players, rnd, real = setup_closed_round(client)
    w = real[0]["player_white"]["id"]
    seq = event_seq(client, tid)

    r = deduct(client, tid, w, expected_event_seq=seq - 1, request_id="d-x")
    assert r.status_code == 409
    assert "快照" in r.json()["detail"]
    assert event_seq(client, tid) == seq
    # 赛事账本为空
    assert client.get(f"/tournaments/{tid}/point-deductions").json() == []


# ---------- 二、request_id 幂等 ----------

def test_register_idempotent_replay(client):
    tid, players, rnd, real = setup_closed_round(client)
    w = real[0]["player_white"]["id"]
    body = {
        "request_id": "d-rep",
        "expected_event_seq": event_seq(client, tid),
        "amount": 0.5,
        "reason": "迟到",
    }
    r1 = client.post(
        f"/tournaments/{tid}/participants/{w}/deductions", json=body
    )
    assert r1.status_code == 201
    eid = r1.json()["id"]
    seq = event_seq(client, tid)

    # 重放（即使带的快照已过时）仍返回首次结果，不产生写入/事件
    r2 = client.post(
        f"/tournaments/{tid}/participants/{w}/deductions", json=body
    )
    assert r2.status_code == 201
    assert r2.json()["id"] == eid
    assert event_seq(client, tid) == seq
    assert len(events_of(client, tid, "point_deduction.registered")) == 1


def test_register_same_request_id_different_params_conflict(client):
    tid, players, rnd, real = setup_closed_round(client)
    w = real[0]["player_white"]["id"]
    deduct(client, tid, w, request_id="dup", amount=0.5, reason="a")
    r = deduct(client, tid, w, request_id="dup", amount=1.0, reason="a")
    assert r.status_code == 409


def test_request_id_namespace_shared_with_revoke(client):
    tid, players, rnd, real = setup_closed_round(client)
    w = real[0]["player_white"]["id"]
    deduct(client, tid, w, request_id="shared", amount=0.5)
    eid = events_of(client, tid, "point_deduction.registered")[0]["payload"][
        "entry_id"
    ]
    # 撤销复用同一 request_id -> 409（赛事内统一命名空间）
    r = revoke(client, tid, eid, request_id="shared")
    assert r.status_code == 409


# ---------- 三、撤销 ----------

def test_revoke_appends_entry_and_clears_effective_deduction(client):
    tid, players, rnd, real = setup_closed_round(client)
    w = real[0]["player_white"]["id"]
    d = deduct(client, tid, w, amount=1.0).json()
    seq = event_seq(client, tid)

    r = revoke(client, tid, d["id"])
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["entry_type"] == "revocation"
    assert body["status"] == "effective"
    assert body["amount"] == 1.0
    assert body["reversed_entry_id"] == d["id"]

    # 原扣分行变为 revoked；撤销行为新行
    led = ledger(client, tid, w).json()
    assert led["effective_deduction"] == 0.0
    types = {(e["id"], e["entry_type"], e["status"]) for e in led["entries"]}
    assert (d["id"], "deduction", "revoked") in types
    assert (body["id"], "revocation", "effective") in types

    # 撤销事件
    assert event_seq(client, tid) == seq + 1
    evs = events_of(client, tid, "point_deduction.revoked")
    assert len(evs) == 1
    assert evs[0]["payload"]["reversed_entry_id"] == d["id"]
    assert evs[0]["payload"]["amount"] == 1.0

    # 排名积分恢复
    st = standings_map(client, tid)
    assert st[w]["points"] == 1.0
    assert st[w]["deduction"] == 0.0
    assert st[w]["game_points"] == 1.0


def test_double_revoke_conflicts_without_write(client):
    tid, players, rnd, real = setup_closed_round(client)
    w = real[0]["player_white"]["id"]
    d = deduct(client, tid, w, amount=0.5).json()
    assert revoke(client, tid, d["id"], request_id="rv-1").status_code == 201
    seq = event_seq(client, tid)

    r = revoke(client, tid, d["id"], request_id="rv-2")
    assert r.status_code == 409
    assert "重复撤销" in r.json()["detail"]
    assert event_seq(client, tid) == seq
    # 仍只有一条撤销行
    led = ledger(client, tid, w).json()
    assert sum(e["entry_type"] == "revocation" for e in led["entries"]) == 1


def test_cannot_revoke_revocation_entry(client):
    tid, players, rnd, real = setup_closed_round(client)
    w = real[0]["player_white"]["id"]
    d = deduct(client, tid, w).json()
    rev = revoke(client, tid, d["id"]).json()
    r = revoke(client, tid, rev["id"], request_id="rv-x")
    assert r.status_code == 409
    assert "只能撤销扣分" in r.json()["detail"]


def test_revoke_idempotent_replay(client):
    tid, players, rnd, real = setup_closed_round(client)
    w = real[0]["player_white"]["id"]
    d = deduct(client, tid, w).json()
    body = {
        "request_id": "rv-rep",
        "expected_event_seq": event_seq(client, tid),
        "reason": "申诉成立",
    }
    r1 = client.post(
        f"/tournaments/{tid}/point-deductions/{d['id']}/revoke", json=body
    )
    assert r1.status_code == 201
    rid = r1.json()["id"]
    seq = event_seq(client, tid)

    r2 = client.post(
        f"/tournaments/{tid}/point-deductions/{d['id']}/revoke", json=body
    )
    assert r2.status_code == 201
    assert r2.json()["id"] == rid
    assert event_seq(client, tid) == seq


def test_revoke_same_request_id_different_reason_conflicts(client):
    tid, players, rnd, real = setup_closed_round(client)
    w = real[0]["player_white"]["id"]
    d = deduct(client, tid, w).json()
    revoke(client, tid, d["id"], request_id="rv-d", reason="a")
    # 已撤销后再用同键不同理由：先命中幂等指纹冲突 -> 409
    r = revoke(client, tid, d["id"], request_id="rv-d", reason="b")
    assert r.status_code == 409


def test_revoke_respects_window_and_snapshot(client):
    tid, players, rnd, real = setup_closed_round(client)
    w = real[0]["player_white"]["id"]
    d = deduct(client, tid, w).json()

    # 快照冲突 409 不落库
    seq = event_seq(client, tid)
    r = revoke(
        client, tid, d["id"], request_id="rv-snap", expected_event_seq=seq - 1
    )
    assert r.status_code == 409
    assert event_seq(client, tid) == seq
    # 原扣分仍生效
    assert ledger(client, tid, w).json()["effective_deduction"] == 0.5

    # 生成新一轮且未封盘：撤销同样被窗口拒绝
    next_round(client, tid)
    r = revoke(client, tid, d["id"], request_id="rv-open")
    assert r.status_code == 409


def test_revoke_nonexistent_and_cross_tournament_404(client):
    t1, players, rnd, real = setup_closed_round(client)
    w = real[0]["player_white"]["id"]
    d = deduct(client, t1, w).json()
    t2 = create_tournament(client, name="另一赛事")

    assert revoke(client, t2, d["id"], request_id="x").status_code == 404
    assert revoke(client, t1, 9999, request_id="x").status_code == 404


# ---------- 四、排名：积分、负数、Buchholz、分组 ----------

def test_standings_keep_game_points_and_add_deduction_fields(client):
    tid, players, rnd, real = setup_closed_round(client)
    w, b = real[0]["player_white"]["id"], real[0]["player_black"]["id"]
    deduct(client, tid, w, amount=0.5)

    st = standings_map(client, tid)
    assert st[w]["game_points"] == 1.0
    assert st[w]["deduction"] == 0.5
    assert st[w]["points"] == 0.5  # 排名积分
    assert st[b]["game_points"] == 0.0
    assert st[b]["deduction"] == 0.0
    assert st[b]["points"] == 0.0


def test_ranking_points_can_be_negative_and_drive_order(client):
    tid, players, rnd, real = setup_closed_round(client)
    w = real[0]["player_white"]["id"]
    deduct(client, tid, w, request_id="d1", amount=1.0)
    deduct(client, tid, w, request_id="d2", amount=1.0)

    st = client.get(f"/tournaments/{tid}/standings").json()
    row = next(x for x in st if x["participant_id"] == w)
    assert row["points"] == -1.0
    assert row["game_points"] == 1.0
    assert row["deduction"] == 2.0
    # -1 分者排名垫底（原本 1 分应居首）
    assert st[-1]["participant_id"] == w


def test_buchholz_recomputes_from_opponents_ranking_points(client):
    # 6 人：R1 (1,2)(3,4)(5,6) 全白胜；扣选手 1 共 1 分 ->
    # 选手 1 排名积分 0；选手 2 的 Buchholz（对手=1）随之变为 0，
    # 而选手 4 的 Buchholz（对手=3，排名积分 1）仍为 1。
    tid, players, rnd, real = setup_closed_round(client, n=6)
    p1, p2 = players[0]["id"], players[1]["id"]
    p4 = players[3]["id"]
    deduct(client, tid, p1, amount=1.0)

    st = standings_map(client, tid)
    assert st[p2]["buchholz"] == 0.0
    assert st[p1]["buchholz"] == 0.0
    assert st[p4]["buchholz"] == 1.0


def test_next_round_groups_by_ranking_points(client):
    # 同上 6 人布局：扣选手 1 后其排名积分降到 0 分组；
    # 1 分组 {3,5} 需与 0 分组 {4,6,1,2} 中两人跨组配对，
    # 且 1 不能再与 2 交手。
    tid, players, rnd, real = setup_closed_round(client, n=6)
    p1 = players[0]["id"]
    deduct(client, tid, p1, amount=1.0)

    r2 = client.post(f"/tournaments/{tid}/rounds/next")
    assert r2.status_code == 201, r2.text
    pairs = [
        frozenset((m["player_white"]["number"], m["player_black"]["number"]))
        for m in r2.json()["matches"]
        if not m["is_bye"]
    ]
    # 编号字典序裁决下：(1,4)(2,6)(3,5)
    assert frozenset((1, 4)) in pairs
    assert frozenset((2, 6)) in pairs
    assert frozenset((3, 5)) in pairs


# ---------- 五、账本查询 ----------

def test_participant_ledger_full_audit_trail(client):
    tid, players, rnd, real = setup_closed_round(client, n=2)
    w = real[0]["player_white"]["id"]
    d1 = deduct(client, tid, w, request_id="a", amount=0.5, reason="r1").json()
    d2 = deduct(
        client, tid, w, request_id="b", amount=1.0, reason="r2"
    ).json()
    revoke(client, tid, d1["id"], request_id="ra")

    r = ledger(client, tid, w)
    assert r.status_code == 200
    body = r.json()
    assert body["participant_id"] == w
    # d1 撤销（-0.5）、d2 仍生效（1.0）-> 净额 1.0
    assert body["effective_deduction"] == 1.0
    assert [e["id"] for e in body["entries"]] == [d1["id"], d2["id"], body["entries"][2]["id"]]
    assert body["entries"][0]["status"] == "revoked"
    assert body["entries"][2]["entry_type"] == "revocation"

    # 无扣分的选手：空账本、净额 0
    b = real[0]["player_black"]["id"]
    empty = ledger(client, tid, b).json()
    assert empty["entries"] == []
    assert empty["effective_deduction"] == 0.0


def test_tournament_ledger_and_single_entry(client):
    tid, players, rnd, real = setup_closed_round(client)
    w = real[0]["player_white"]["id"]
    d = deduct(client, tid, w, amount=0.5).json()
    rev = revoke(client, tid, d["id"]).json()

    items = client.get(f"/tournaments/{tid}/point-deductions").json()
    assert [e["id"] for e in items] == [d["id"], rev["id"]]

    r = client.get(f"/point-deductions/{rev['id']}")
    assert r.status_code == 200
    assert r.json()["entry_type"] == "revocation"
    assert client.get("/point-deductions/9999").status_code == 404


# ---------- 六、并发：重复撤销 ----------

@pytest.fixture()
def live_server_factory():
    import socket
    import time

    import httpx
    import uvicorn

    from app.database import Base, engine
    from app.main import app

    def wait_until(cond, timeout=10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cond():
                return
            time.sleep(0.01)
        raise AssertionError("server not started")

    class Server:
        def __init__(self):
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
            sock.close()
            self.server = uvicorn.Server(
                uvicorn.Config(
                    app,
                    host="127.0.0.1",
                    port=self.port,
                    log_level="error",
                    access_log=False,
                )
            )
            self.thread = threading.Thread(target=self.server.run, daemon=True)

        def client(self):
            return httpx.Client(
                base_url=f"http://127.0.0.1:{self.port}", timeout=10.0
            )

        def __enter__(self):
            Base.metadata.drop_all(bind=engine)
            Base.metadata.create_all(bind=engine)
            self.thread.start()
            wait_until(lambda: self.server.started)
            return self

        def __exit__(self, *exc):
            self.server.should_exit = True
            self.thread.join(timeout=10)

    return Server


def test_concurrent_revoke_only_one_succeeds(live_server_factory):
    """并发重复撤销同一扣分：恰好一条撤销成功，其余 409。"""
    with live_server_factory() as srv:
        client = srv.client()
        tid, players, rnd, real = setup_closed_round(client)
        w = real[0]["player_white"]["id"]
        d = deduct(client, tid, w, amount=1.0).json()
        seq = event_seq(client, tid)
        n = 6
        barrier = threading.Barrier(n)

        def call(i):
            local = srv.client()
            try:
                barrier.wait(timeout=10)
                r = local.post(
                    f"/tournaments/{tid}/point-deductions/{d['id']}/revoke",
                    json={
                        "request_id": f"c-{i}",
                        "expected_event_seq": seq,
                        "reason": "x",
                    },
                )
                return r.status_code
            finally:
                local.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=n) as pool:
            futs = [pool.submit(call, i) for i in range(n)]
            concurrent.futures.wait(futs, timeout=30)
            statuses = [f.result() for f in futs]

        assert statuses.count(201) == 1
        assert statuses.count(409) == n - 1
        led = ledger(client, tid, w).json()
        revocations = [e for e in led["entries"] if e["entry_type"] == "revocation"]
        assert len(revocations) == 1
        assert led["effective_deduction"] == 0.0
        # 仅一次撤销占用一个事件序号
        assert event_seq(client, tid) == seq + 1
