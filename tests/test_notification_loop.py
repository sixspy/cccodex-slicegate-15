
import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from app import app, init_db


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("app.DB_PATH", tmp_path / "t.db")
    init_db()
    yield


def _login(c):
    r = c.post("/api/login", json={"username": "admin", "password": "123456"})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _release_complete(c, h, slice_no="SL-N-1", consumer="ads_n"):
    r = c.post("/api/slices", headers=h, json={
        "slice_no": slice_no, "biz_date": "2026-11-05", "partition_key": "east",
        "row_count": 10, "expected_count": 10})
    assert r.status_code == 200
    sid = r.json()["id"]
    rel = c.post(f"/api/slices/{sid}/release", headers=h, json={"consumer": consumer})
    return sid, rel


def _notif_of(c, h, release_id):
    items = c.get("/api/releases", headers=h).json()["items"]
    return next(r for r in items if r["id"] == release_id)


def test_release_notification_watermark_same_tx():
    """口径甲：成功放行、水位、待投递通知一笔落账，成功必有通知记录。"""
    c = TestClient(app)
    h = _login(c)
    _, rel = _release_complete(c, h)
    assert rel.status_code == 200
    body = rel.json()
    assert body["ok"] and body["mode"] == "甲"
    nid = body["notification_id"]
    # 通知队列：待投递、口径旁注甲
    items = c.get("/api/notifications", headers=h).json()["items"]
    n = next(i for i in items if i["id"] == nid)
    assert n["status"] == "待投递" and n["mode"] == "甲"
    assert n["release_id"] == body["release_id"]
    # 放行详情带通知账，水位页有对应水位
    rel_row = _notif_of(c, h, body["release_id"])
    assert rel_row["notification_status"] == "待投递"
    wms = c.get("/api/watermarks", headers=h).json()["items"]
    assert any(w["source_slice"] == "SL-N-1" and w["consumer"] == "ads_n" for w in wms)


def test_incomplete_release_no_notification():
    """未完整拒绝放行：拒绝账在，但不得落通知账。"""
    c = TestClient(app)
    h = _login(c)
    before = len(c.get("/api/notifications", headers=h).json()["items"])
    bad = c.post("/api/slices/1/release", headers=h, json={"consumer": "x"})
    assert bad.status_code == 409
    after = c.get("/api/notifications", headers=h).json()["items"]
    assert len(after) == before


def test_double_release_blocked():
    """重复放行禁止：一刀切片的通知账只有一条。"""
    c = TestClient(app)
    h = _login(c)
    sid, rel = _release_complete(c, h)
    assert rel.status_code == 200
    again = c.post(f"/api/slices/{sid}/release", headers=h, json={"consumer": "ads_n"})
    assert again.status_code == 409
    assert "重复放行禁止" in again.json()["detail"]
    items = [i for i in c.get("/api/notifications", headers=h).json()["items"]
             if i["release_id"] == rel.json()["release_id"]]
    assert len(items) == 1


def test_dispatch_happy_and_reconcile():
    """值班投递：待投递→已投递，投递账一笔成功，四方对账相符。"""
    c = TestClient(app)
    h = _login(c)
    _, rel = _release_complete(c, h)
    nid = rel.json()["notification_id"]
    d = c.post(f"/api/notifications/{nid}/dispatch", headers=h, json={})
    assert d.status_code == 200
    assert d.json()["status"] == "已投递" and d.json()["attempt"] == 1
    dels = c.get(f"/api/notifications/{nid}/deliveries", headers=h).json()["items"]
    assert len(dels) == 1 and dels[0]["result"] == "成功" and dels[0]["actor"] == "admin"
    rc = c.get("/api/reconcile", headers=h).json()
    assert rc["summary"]["异常数"] == 0
    assert rc["summary"]["已投递"] == 1
    row = next(r for r in rc["rows"] if r["notification_id"] == nid)
    assert row["ok"] and row["notification_mode"] == "甲" and row["watermark"]


def test_redispatch_delivered_rejected():
    """已投递再投：禁止重复投递冒充一次成功。"""
    c = TestClient(app)
    h = _login(c)
    _, rel = _release_complete(c, h)
    nid = rel.json()["notification_id"]
    assert c.post(f"/api/notifications/{nid}/dispatch", headers=h, json={}).status_code == 200
    again = c.post(f"/api/notifications/{nid}/dispatch", headers=h, json={})
    assert again.status_code == 409
    assert "通知争用禁止默成" in again.json()["detail"]
    assert "禁止重复投递冒充一次成功" in again.json()["detail"]
    dels = c.get(f"/api/notifications/{nid}/deliveries", headers=h).json()["items"]
    assert len([d for d in dels if d["result"] == "成功"]) == 1


def test_concurrent_dispatch_mutex():
    """两名通知值班同时发起：排队互斥，恰一成功，后手通知争用禁止默成。"""
    c = TestClient(app)
    h = _login(c)
    _, rel = _release_complete(c, h)
    nid = rel.json()["notification_id"]
    clients = [TestClient(app), TestClient(app)]
    results, bodies = [], []

    def hit(cli):
        r = cli.post(f"/api/notifications/{nid}/dispatch", headers=h, json={})
        results.append(r.status_code)
        bodies.append(r.json().get("detail", ""))

    ts = [threading.Thread(target=hit, args=(cli,)) for cli in clients]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sorted(results) == [200, 409]
    loser = bodies[results.index(409)]
    assert "通知争用禁止默成" in loser or "禁止重复投递冒充一次成功" in loser
    dels = c.get(f"/api/notifications/{nid}/deliveries", headers=h).json()["items"]
    assert len([d for d in dels if d["result"] == "成功"]) == 1
    n = next(i for i in c.get("/api/notifications", headers=h).json()["items"] if i["id"] == nid)
    assert n["status"] == "已投递" and n["attempts"] == 1


def test_claimed_notification_contention():
    """通知在投递中被第二人抢占：后手得通知争用禁止默成。"""
    c = TestClient(app)
    h = _login(c)
    _, rel = _release_complete(c, h)
    nid = rel.json()["notification_id"]
    import app as app_mod
    conn = sqlite3.connect(app_mod.DB_PATH)
    conn.execute("UPDATE notifications SET status='投递中', claimed_by='值班乙' WHERE id=?", (nid,))
    conn.commit(); conn.close()
    r = c.post(f"/api/notifications/{nid}/dispatch", headers=h, json={})
    assert r.status_code == 409
    assert "通知争用禁止默成" in r.json()["detail"]


def test_failed_dispatch_retry_never_marked_notified():
    """下游故障：记失败账、状态投递失败（未标已通知），重投成功，成功账仍只一笔。"""
    c = TestClient(app)
    h = _login(c)
    _, rel = _release_complete(c, h)
    nid = rel.json()["notification_id"]
    fail = c.post(f"/api/notifications/{nid}/dispatch", headers=h, json={"simulate_failure": True})
    assert fail.status_code == 502
    n = next(i for i in c.get("/api/notifications", headers=h).json()["items"] if i["id"] == nid)
    assert n["status"] == "投递失败" and n["delivered_at"] is None
    ok = c.post(f"/api/notifications/{nid}/dispatch", headers=h, json={})
    assert ok.status_code == 200 and ok.json()["attempt"] == 2
    dels = c.get(f"/api/notifications/{nid}/deliveries", headers=h).json()["items"]
    assert [d["result"] for d in dels] == ["失败", "成功"]
    rc = c.get("/api/reconcile", headers=h).json()
    assert rc["summary"]["异常数"] == 0


def test_policy_locked_same_day():
    """策略簿当天不许换：改乙即拒并写明通知口径偏了；重申甲为幂等。"""
    c = TestClient(app)
    h = _login(c)
    p = c.get("/api/notifications/policy", headers=h).json()["policy"]
    assert p["mode"] == "甲" and p["decided_day"]
    bad = c.put("/api/notifications/policy", headers=h, json={"mode": "乙"})
    assert bad.status_code == 409
    assert "通知口径偏了" in bad.json()["detail"]
    same = c.put("/api/notifications/policy", headers=h, json={"mode": "甲"})
    assert same.status_code == 200 and same.json()["unchanged"]
    weird = c.put("/api/notifications/policy", headers=h, json={"mode": "丙"})
    assert weird.status_code == 400


def test_history_backfill_and_reconcile_flags():
    """历史成功放行启动补登待投递通知；对账四方可核对。"""
    c = TestClient(app)
    h = _login(c)
    items = c.get("/api/notifications", headers=h).json()["items"]
    seeded = [i for i in items if i["release_id"] == 1]
    assert len(seeded) == 1 and seeded[0]["status"] == "待投递" and seeded[0]["mode"] == "甲"
    rc = c.get("/api/reconcile", headers=h).json()
    row = next(r for r in rc["rows"] if r["release_id"] == 1)
    assert row["ok"] and row["watermark"] and row["notification_status"] == "待投递"
