
import hashlib, secrets, sqlite3
from datetime import date, datetime
from pathlib import Path
from fastapi import Header, HTTPException
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DB_PATH = DATA / "app.db"
FRONTEND = ROOT / "frontend"
SESSIONS: dict[str, str] = {}

# 通知口径只准二选一，选定写入通知策略簿，当天不许换，两边不得混通。
# 本仓选定「甲」：成功放行、推水位、落待投递通知走同一数据库事务，任一步失败整笔回滚。
MODE_A = "甲"
MODE_B = "乙"
MODE_DESC = {
    MODE_A: "同事务落账：成功放行、推水位、落待投递通知同一数据库事务，任一步失败整笔回滚；待投递通知由通知值班投递，失败可重试，禁止未投递标已通知、禁止重复投递冒充一次成功",
    MODE_B: "独立投递队列：放行与水位先提交，通知进独立投递队列，失败可重试，禁止把未投递标成已通知",
}
NOTIF_PENDING = "待投递"
NOTIF_CLAIMED = "投递中"
NOTIF_DELIVERED = "已投递"
NOTIF_FAILED = "投递失败"


def now_str() -> str:
    return datetime.now().isoformat(timespec="seconds")


def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def sha(p: str) -> str:
    return hashlib.sha256(p.encode("utf-8")).hexdigest()


def ensure_users(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS users(
        username TEXT PRIMARY KEY, password_sha TEXT NOT NULL, role TEXT NOT NULL)""")
    if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0:
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("admin", sha("123456"), "管理员"))
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("viewer", sha("view123"), "只读"))


class LoginIn(BaseModel):
    username: str
    password: str


def require_user(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "未登录")
    u = SESSIONS.get(authorization.removeprefix("Bearer ").strip())
    if not u:
        raise HTTPException(401, "登录已失效")
    return u


from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

PRODUCT = "日切放行"
SKELETON = "业务日分区切片登记后裁决完整度，完整才放行下游，并与消费水位对账"

app = FastAPI(title=PRODUCT)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


def init_db():
    with connect() as conn:
        ensure_users(conn)
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS slices(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          slice_no TEXT UNIQUE NOT NULL,
          biz_date TEXT NOT NULL,
          partition_key TEXT NOT NULL,
          row_count INTEGER NOT NULL,
          expected_count INTEGER NOT NULL,
          completeness TEXT NOT NULL,
          release_status TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS releases(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          slice_id INTEGER NOT NULL,
          consumer TEXT NOT NULL,
          status TEXT NOT NULL,
          note TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS watermarks(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          consumer TEXT NOT NULL,
          biz_date TEXT NOT NULL,
          watermark TEXT NOT NULL,
          source_slice TEXT NOT NULL
        );
        -- 通知策略簿：单例行，记选定口径；当天不许换，两边不得混通
        CREATE TABLE IF NOT EXISTS notification_policy(
          id INTEGER PRIMARY KEY CHECK (id = 1),
          mode TEXT NOT NULL,
          mode_desc TEXT NOT NULL,
          decided_by TEXT NOT NULL,
          decided_day TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        -- 通知队列：一次放行成功只落一条通知账（release_id 唯一），策略旁注随行
        CREATE TABLE IF NOT EXISTS notifications(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          release_id INTEGER NOT NULL UNIQUE,
          slice_id INTEGER NOT NULL,
          consumer TEXT NOT NULL,
          status TEXT NOT NULL,
          mode TEXT NOT NULL,
          dedupe_key TEXT NOT NULL UNIQUE,
          attempts INTEGER NOT NULL DEFAULT 0,
          claimed_by TEXT,
          claimed_at TEXT,
          delivered_at TEXT,
          last_error TEXT,
          created_at TEXT NOT NULL
        );
        -- 投递账：每次投递尝试一笔；成功账全表至多一笔（部分唯一索引），重复投递冒充不了一次成功
        CREATE TABLE IF NOT EXISTS notification_deliveries(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          notification_id INTEGER NOT NULL,
          attempt_no INTEGER NOT NULL,
          actor TEXT NOT NULL,
          result TEXT NOT NULL,
          detail TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL,
          UNIQUE(notification_id, attempt_no)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_notification_deliveries_success
          ON notification_deliveries(notification_id) WHERE result='成功';
        """)
        if conn.execute("SELECT COUNT(*) c FROM slices").fetchone()["c"] == 0:
            rows = [
                ("SL-2026-11-01-east", "2026-11-01", "east", 980, 1000, "部分完整", "未放行"),
                ("SL-2026-11-01-west", "2026-11-01", "west", 1000, 1000, "完整", "已放行"),
                ("SL-2026-11-02-east", "2026-11-02", "east", 0, 1000, "待检", "未放行"),
            ]
            for r in rows:
                conn.execute("INSERT INTO slices(slice_no,biz_date,partition_key,row_count,expected_count,completeness,release_status) VALUES (?,?,?,?,?,?,?)", r)
            conn.execute("INSERT INTO releases(slice_id,consumer,status,note,created_at) VALUES (2,'ads_order','成功','完整切片放行','2026-11-01T18:00:00')")
            conn.execute("INSERT INTO watermarks(consumer,biz_date,watermark,source_slice) VALUES ('ads_order','2026-11-01','2026-11-01T18:00:00','SL-2026-11-01-west')")
        # 口径选定「甲」，写进通知策略簿；仅在策略簿为空时落定，之后当天不许换
        if conn.execute("SELECT COUNT(*) c FROM notification_policy").fetchone()["c"] == 0:
            conn.execute(
                "INSERT INTO notification_policy(id,mode,mode_desc,decided_by,decided_day,created_at) VALUES (1,?,?,?,?,?)",
                (MODE_A, MODE_DESC[MODE_A], "系统预置", date.today().isoformat(), now_str()),
            )
        # 历史成功放行补登通知账：禁止回报整单成功却无通知记录（补登为待投递，等值班投递）
        conn.execute("""
          INSERT INTO notifications(release_id,slice_id,consumer,status,mode,dedupe_key,created_at)
          SELECT r.id, r.slice_id, r.consumer, ?, ?, 'release#'||r.id, r.created_at
          FROM releases r
          WHERE r.status='成功'
            AND NOT EXISTS(SELECT 1 FROM notifications n WHERE n.release_id=r.id)
        """, (NOTIF_PENDING, MODE_A))


@app.on_event("startup")
def _start():
    init_db()


def get_policy(conn) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM notification_policy WHERE id=1").fetchone()
    if not row:
        raise HTTPException(500, "通知策略簿缺失")
    return row


def begin_exclusive(conn, busy_ms: int, conflict_msg: str):
    """手动 BEGIN IMMEDIATE 排队互斥；拿不到写锁即拒，禁止默成。"""
    conn.isolation_level = None
    conn.execute(f"PRAGMA busy_timeout={int(busy_ms)}")
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError:
        conn.close()
        raise HTTPException(409, conflict_msg)


@app.get("/api/health")
def health():
    with connect() as c:
        n = c.execute("SELECT COUNT(*) c FROM slices").fetchone()["c"]
    return {"ok": True, "product": PRODUCT, "skeleton": SKELETON, "slices": n, "db": str(DB_PATH)}


@app.post("/api/login")
def login(body: LoginIn):
    with connect() as c:
        row = c.execute("SELECT username, role FROM users WHERE username=? AND password_sha=?", (body.username, sha(body.password))).fetchone()
    if not row:
        raise HTTPException(401, "账号或密码错误")
    tok = secrets.token_hex(16); SESSIONS[tok] = row["username"]
    return {"token": tok, "username": row["username"], "role": row["role"]}


@app.get("/api/slices")
def list_slices(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        rows = c.execute("SELECT * FROM slices ORDER BY id").fetchall()
    return {"items": [dict(r) for r in rows]}


class SliceIn(BaseModel):
    slice_no: str
    biz_date: str
    partition_key: str
    row_count: int
    expected_count: int


@app.post("/api/slices")
def add_slice(body: SliceIn, authorization: str | None = Header(default=None)):
    require_user(authorization)
    comp = "完整" if body.row_count >= body.expected_count and body.expected_count > 0 else ("部分完整" if body.row_count > 0 else "待检")
    with connect() as c:
        try:
            cur = c.execute(
                "INSERT INTO slices(slice_no,biz_date,partition_key,row_count,expected_count,completeness,release_status) VALUES (?,?,?,?,?,?,?)",
                (body.slice_no, body.biz_date, body.partition_key, body.row_count, body.expected_count, comp, "未放行"),
            )
            return {"id": cur.lastrowid, "completeness": comp}
        except sqlite3.IntegrityError:
            raise HTTPException(409, "切片号已存在")


@app.post("/api/slices/{sid}/evaluate")
def evaluate(sid: int, authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        row = c.execute("SELECT * FROM slices WHERE id=?", (sid,)).fetchone()
        if not row:
            raise HTTPException(404, "切片不存在")
        comp = "完整" if row["row_count"] >= row["expected_count"] and row["expected_count"] > 0 else ("部分完整" if row["row_count"] > 0 else "待检")
        c.execute("UPDATE slices SET completeness=? WHERE id=?", (comp, sid))
    return {"id": sid, "completeness": comp}


class ReleaseIn(BaseModel):
    consumer: str


@app.post("/api/slices/{sid}/release")
def release(sid: int, body: ReleaseIn, authorization: str | None = Header(default=None)):
    require_user(authorization)
    now = now_str()
    conn = connect()
    # 口径甲：成功放行、推水位、落待投递通知同一事务，任一步失败整笔回滚
    begin_exclusive(conn, 3000, "放行争用：另一笔放行正在落账，请稍后重试")
    try:
        policy = get_policy(conn)
        if policy["mode"] != MODE_A:
            raise HTTPException(409, f"通知口径偏了：策略簿为「{policy['mode']}」，本链路按「甲」同事务落账，两边不得混通")
        row = conn.execute("SELECT * FROM slices WHERE id=?", (sid,)).fetchone()
        if not row:
            raise HTTPException(404, "切片不存在")
        if row["completeness"] != "完整":
            conn.execute("INSERT INTO releases(slice_id,consumer,status,note,created_at) VALUES (?,?,?,?,?)",
                         (sid, body.consumer, "拒绝", "完整度未达标", now))
            conn.commit()
            raise HTTPException(409, "未完整禁止放行")
        if row["release_status"] == "已放行":
            raise HTTPException(409, "重复放行禁止：该切片已放行，通知账已存在，禁止重复投递冒充一次成功")
        conn.execute("UPDATE slices SET release_status='已放行' WHERE id=?", (sid,))
        cur = conn.execute("INSERT INTO releases(slice_id,consumer,status,note,created_at) VALUES (?,?,?,?,?)",
                           (sid, body.consumer, "成功", "完整切片放行", now))
        rid = cur.lastrowid
        conn.execute("INSERT INTO watermarks(consumer,biz_date,watermark,source_slice) VALUES (?,?,?,?)",
                     (body.consumer, row["biz_date"], now, row["slice_no"]))
        ncur = conn.execute(
            "INSERT INTO notifications(release_id,slice_id,consumer,status,mode,dedupe_key,created_at) VALUES (?,?,?,?,?,?,?)",
            (rid, sid, body.consumer, NOTIF_PENDING, MODE_A, f"release#{rid}", now),
        )
        nid = ncur.lastrowid
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"ok": True, "release_id": rid, "notification_id": nid,
            "notification_status": NOTIF_PENDING, "mode": MODE_A}


@app.get("/api/releases")
def list_releases(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        rows = c.execute("""SELECT r.*, s.slice_no,
                                   n.id AS notification_id, n.status AS notification_status, n.mode AS notification_mode
                            FROM releases r
                            JOIN slices s ON s.id=r.slice_id
                            LEFT JOIN notifications n ON n.release_id=r.id
                            ORDER BY r.id DESC""").fetchall()
    return {"items": [dict(r) for r in rows]}


@app.get("/api/watermarks")
def list_wm(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        rows = c.execute("SELECT * FROM watermarks ORDER BY id DESC").fetchall()
    return {"items": [dict(r) for r in rows]}


# ---------- 放行成功通知投递闭环 ----------

@app.get("/api/notifications/policy")
def read_policy(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        policy = get_policy(c)
    return {"policy": dict(policy), "options": [{"mode": m, "mode_desc": d} for m, d in MODE_DESC.items()]}


class PolicyIn(BaseModel):
    mode: str


@app.put("/api/notifications/policy")
def write_policy(body: PolicyIn, authorization: str | None = Header(default=None)):
    require_user(authorization)
    mode = body.mode.strip()
    if mode not in (MODE_A, MODE_B):
        raise HTTPException(400, "通知口径只准选「甲」或「乙」")
    with connect() as c:
        policy = get_policy(c)
        if mode == policy["mode"]:
            return {"ok": True, "unchanged": True, "policy": dict(policy), "note": f"口径既定「{mode}」，未变更"}
        if policy["decided_day"] == date.today().isoformat():
            raise HTTPException(409, f"通知口径偏了：策略簿今日已定「{policy['mode']}」，当天不许换")
        raise HTTPException(409, f"通知口径偏了：策略簿已定「{policy['mode']}」，两边不得混通")


@app.get("/api/notifications")
def list_notifications(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        rows = c.execute("""SELECT n.*, s.slice_no
                            FROM notifications n JOIN slices s ON s.id=n.slice_id
                            ORDER BY n.id DESC""").fetchall()
        policy = get_policy(c)
    return {"items": [dict(r) for r in rows], "policy": dict(policy)}


@app.get("/api/notifications/{nid}/deliveries")
def list_deliveries(nid: int, authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        rows = c.execute("SELECT * FROM notification_deliveries WHERE notification_id=? ORDER BY id", (nid,)).fetchall()
    return {"items": [dict(r) for r in rows]}


class DispatchIn(BaseModel):
    simulate_failure: bool = False  # 演练开关：模拟下游无回执，验证失败可重试、不标已通知


@app.post("/api/notifications/{nid}/dispatch")
def dispatch(nid: int, body: DispatchIn, authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    now = now_str()
    conn = connect()
    # 两名通知值班同时发起：写锁排队互斥，后手禁止默成
    begin_exclusive(conn, 0, "通知争用禁止默成：另一值班正在投递，本次不计成功")
    outcome: tuple[str, int] | None = None
    try:
        policy = get_policy(conn)
        n = conn.execute("SELECT * FROM notifications WHERE id=?", (nid,)).fetchone()
        if not n:
            raise HTTPException(404, "通知不存在")
        if n["mode"] != policy["mode"]:
            raise HTTPException(409, f"通知口径偏了：通知旁注「{n['mode']}」与策略簿「{policy['mode']}」不一致")
        if n["status"] == NOTIF_DELIVERED:
            raise HTTPException(409, "通知争用禁止默成：该通知已投递，禁止重复投递冒充一次成功")
        if n["status"] == NOTIF_CLAIMED:
            raise HTTPException(409, f"通知争用禁止默成：{n['claimed_by'] or '他人'}正在投递，本次不计成功")
        attempt_no = n["attempts"] + 1
        cur = conn.execute(
            "UPDATE notifications SET status=?, claimed_by=?, claimed_at=?, attempts=? WHERE id=? AND status IN (?,?)",
            (NOTIF_CLAIMED, user, now, attempt_no, nid, NOTIF_PENDING, NOTIF_FAILED),
        )
        if cur.rowcount == 0:
            raise HTTPException(409, "通知争用禁止默成：投递权被他人取得，本次不计成功")
        if body.simulate_failure:
            # 失败账照记、状态置投递失败：失败可重试，但禁止把未投递标成已通知
            conn.execute("INSERT INTO notification_deliveries(notification_id,attempt_no,actor,result,detail,created_at) VALUES (?,?,?,?,?,?)",
                         (nid, attempt_no, user, "失败", "下游无回执（演练故障）", now))
            conn.execute("UPDATE notifications SET status=?, last_error=? WHERE id=?",
                         (NOTIF_FAILED, "下游无回执（演练故障）", nid))
            conn.commit()
            outcome = ("失败", attempt_no)
        else:
            conn.execute("INSERT INTO notification_deliveries(notification_id,attempt_no,actor,result,detail,created_at) VALUES (?,?,?,?,?,?)",
                         (nid, attempt_no, user, "成功", "下游已回执", now))
            conn.execute("UPDATE notifications SET status=?, delivered_at=?, last_error=NULL WHERE id=?",
                         (NOTIF_DELIVERED, now, nid))
            conn.commit()
            outcome = ("成功", attempt_no)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    if outcome and outcome[0] == "失败":
        raise HTTPException(502, "下游投递失败：已记失败账，未标已通知，可重试")
    return {"ok": True, "notification_id": nid, "status": NOTIF_DELIVERED,
            "attempt": outcome[1], "delivered_at": now, "mode": n["mode"]}


@app.get("/api/reconcile")
def reconcile(authorization: str | None = Header(default=None)):
    """账实对账：通知队列、策略旁注、放行详情、水位页四方可核对。"""
    require_user(authorization)
    with connect() as c:
        policy = get_policy(c)
        rels = c.execute("""SELECT r.*, s.slice_no FROM releases r JOIN slices s ON s.id=r.slice_id
                            WHERE r.status='成功' ORDER BY r.id""").fetchall()
        rows = []
        for r in rels:
            ns = c.execute("SELECT * FROM notifications WHERE release_id=?", (r["id"],)).fetchall()
            wm = c.execute("SELECT * FROM watermarks WHERE consumer=? AND source_slice=? ORDER BY id DESC LIMIT 1",
                           (r["consumer"], r["slice_no"])).fetchone()
            issues = []
            n = ns[0] if ns else None
            if not ns:
                issues.append("缺通知账")
            if len(ns) > 1:
                issues.append("通知账重复")
            if n:
                if n["mode"] != policy["mode"]:
                    issues.append("口径不符")
                succ = c.execute("SELECT COUNT(*) c FROM notification_deliveries WHERE notification_id=? AND result='成功'",
                                 (n["id"],)).fetchone()["c"]
                if n["status"] == NOTIF_DELIVERED and succ != 1:
                    issues.append("已投递但成功投递账不等于1")
                if n["status"] != NOTIF_DELIVERED and succ > 0:
                    issues.append("未投递却有成功投递账")
            if not wm:
                issues.append("缺水位账")
            rows.append({
                "release_id": r["id"], "slice_no": r["slice_no"], "consumer": r["consumer"],
                "release_at": r["created_at"],
                "notification_id": n["id"] if n else None,
                "notification_status": n["status"] if n else None,
                "notification_mode": n["mode"] if n else None,
                "delivered_at": n["delivered_at"] if n else None,
                "watermark": wm["watermark"] if wm else None,
                "issues": issues, "ok": not issues,
            })
        cnt = lambda st: c.execute("SELECT COUNT(*) c FROM notifications WHERE status=?", (st,)).fetchone()["c"]
        summary = {
            "成功放行数": len(rels),
            "通知账数": c.execute("SELECT COUNT(*) c FROM notifications").fetchone()["c"],
            "已投递": cnt(NOTIF_DELIVERED),
            "待投递": cnt(NOTIF_PENDING),
            "投递失败": cnt(NOTIF_FAILED),
            "投递中": cnt(NOTIF_CLAIMED),
            "水位账数": c.execute("SELECT COUNT(*) c FROM watermarks").fetchone()["c"],
            "无放行通知数": c.execute("""SELECT COUNT(*) c FROM notifications n
                                          JOIN releases r ON r.id=n.release_id WHERE r.status<>'成功'""").fetchone()["c"],
            "异常数": sum(1 for r in rows if not r["ok"]),
        }
    return {"policy": dict(policy), "summary": summary, "rows": rows}


@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")


app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")
