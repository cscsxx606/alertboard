"""
告警看板后端 - FastAPI
从 Alertmanager 拉取告警, 按 group + alertname + severity 聚合;
确认状态持久化到 SQLite; 提供静默管理、已恢复告警历史。
部署: aliyun-bj :9095
"""
import asyncio
import json
import os
import sqlite3
import time
import urllib.error
import urllib.request
from contextlib import asynccontextmanager, closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

AM_URL = os.environ.get("AM_URL", "http://127.0.0.1:9093")
BASE_DIR = Path(__file__).resolve().parent
INDEX_FILE = BASE_DIR / "index.html"
DB_PATH = Path(os.environ.get("ALERTBOARD_DB", str(BASE_DIR / "alertboard.db")))

# ---- 服务名映射 (ip:port -> service_name, 数据来自 Jenkins 库经 sync_service_map.py 定时同步) ----
_service_map = {}  # "ip:port" -> service_name


def _load_service_map():
    """从本库 alertboard.db 的 service_map 表加载映射(由 sync_service_map.py 定时同步)"""
    global _service_map
    try:
        import sqlite3
        conn = sqlite3.connect(DB_PATH, timeout=5)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT instance, service_name FROM service_map").fetchall()
        conn.close()
        _service_map = {r["instance"]: r["service_name"] for r in rows}
    except Exception:
        _service_map = {}


def _service_name_for(instance: str) -> str:
    """根据 instance(ip:port) 返回服务名; 无则返回空"""
    return _service_map.get(instance, "")

MAX_SILENCE_DAYS = 90          # 单次静默最长时长(天)
HISTORY_MAX = 500              # 最多保留的恢复记录条数
HISTORY_RETENTION_DAYS = 7     # 恢复记录保留天数
HISTORY_POLL_INTERVAL = 30     # 历史采集轮询间隔(秒)

SEV_ORDER = {"critical": 0, "warning": 1, "info": 2}


# ---------- db ----------
def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with closing(_conn()) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS acks (
              fingerprint TEXT PRIMARY KEY,
              ack_time   TEXT NOT NULL,
              ack_by     TEXT NOT NULL,
              comment    TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS history (
              id          INTEGER PRIMARY KEY AUTOINCREMENT,
              fingerprint TEXT NOT NULL DEFAULT '',
              alertname   TEXT NOT NULL DEFAULT '',
              group_name  TEXT NOT NULL DEFAULT '',
              instance    TEXT NOT NULL DEFAULT '',
              severity    TEXT NOT NULL DEFAULT '',
              summary     TEXT NOT NULL DEFAULT '',
              starts_at   TEXT NOT NULL DEFAULT '',
              resolved_at TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_history_resolved ON history(resolved_at DESC);
            """
        )
        conn.commit()


def query_acks():
    with closing(_conn()) as conn:
        rows = conn.execute(
            "SELECT fingerprint, ack_time, ack_by, comment FROM acks").fetchall()
    return {r["fingerprint"]: dict(r) for r in rows}


# ---------- helper ----------
def am_get(path, timeout=8):
    req = urllib.request.Request(AM_URL + path)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def am_post(path, data, timeout=8):
    req = urllib.request.Request(
        AM_URL + path, data=json.dumps(data).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _parse_dt(value: Optional[str], name: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(400, "%s 时间格式无效: %s" % (name, value))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ---------- 聚合 ----------
def _trim_alert(a, ack_map=None):
    labels = a.get("labels") or {}
    ann = a.get("annotations") or {}
    status = a.get("status") or {}
    fp = a.get("fingerprint", "")
    instance = labels.get("instance") or ""
    return {
        "fingerprint": fp,
        "labels": labels,
        "annotations": ann,
        "service_name": _service_name_for(instance),
        "startsAt": a.get("startsAt", ""),
        "endsAt": a.get("endsAt", ""),
        "status": {
            "state": status.get("state", ""),
            "silencedBy": status.get("silencedBy") or [],
            "inhibitedBy": status.get("inhibitedBy") or [],
        },
        "ack": (ack_map or {}).get(fp),
    }


def build_groups(alerts, ack_map=None):
    """group -> (alertname, severity) -> 实例列表, 两级聚合"""
    groups = {}
    for a in alerts:
        labels = a.get("labels") or {}
        g = labels.get("group") or "其他"
        alertname = labels.get("alertname") or "(未命名)"
        sev = labels.get("severity") or "info"
        groups.setdefault(g, {})
        key = (alertname, sev)
        entry = groups[g].setdefault(
            key, {"alertname": alertname, "severity": sev, "alerts": []})
        entry["alerts"].append(_trim_alert(a, ack_map))

    result = []
    for g, items in groups.items():
        gitems = []
        for (alertname, sev), entry in items.items():
            entry["alerts"].sort(key=lambda x: x.get("startsAt") or "")
            entry["count"] = len(entry["alerts"])
            gitems.append(entry)
        gitems.sort(key=lambda x: (SEV_ORDER.get(x["severity"], 9), x["alertname"]))
        crit = sum(i["count"] for i in gitems if i["severity"] == "critical")
        warn = sum(i["count"] for i in gitems if i["severity"] == "warning")
        info = sum(i["count"] for i in gitems if i["severity"] != "critical" and i["severity"] != "warning")
        result.append({
            "group": g,
            "count": sum(i["count"] for i in gitems),
            "critical": crit,
            "warning": warn,
            "info": info,
            "items": gitems,
        })
    result.sort(key=lambda x: (-x["critical"], -x["warning"], x["group"]))
    return result


# ---------- 恢复历史采集 ----------
_known_alerts: dict = {}          # fp -> info (上一次采集到的活跃告警)
_recorded: set = set()            # 已记录恢复事件的 fp (防止同一告警重复记录)


def _collect_resolved():
    """轮询采集: 记录已恢复(消失)的告警到历史, 每个 fp 只记一次"""
    try:
        alerts = am_get("/api/v2/alerts")
    except Exception:
        return
    now = _now_iso()
    current = {}
    for a in alerts:
        fp = a.get("fingerprint")
        if not fp:
            continue
        # addressed/resolved 状态的告警很快会从 AM 移除, 不作为活跃
        if (a.get("status") or {}).get("state") == "resolved":
            continue
        labels = a.get("labels") or {}
        current[fp] = (
            fp,
            labels.get("alertname") or "",
            labels.get("group") or "其他",
            labels.get("instance") or "",
            labels.get("severity") or "info",
            (a.get("annotations") or {}).get("summary") or "",
            a.get("startsAt") or "",
        )

    events = []
    # 上次活跃、这次不在活跃中 => 视为已恢复(用 _recorded 防重)
    for fp, info in _known_alerts.items():
        if fp not in current and fp not in _recorded:
            events.append((info, now))
            _recorded.add(fp)

    # 更新快照
    _known_alerts.clear()
    _known_alerts.update(current)

    # 清理 _recorded: 告警重新活跃(re-trigger)后, 允许未来再次记录其恢复
    for fp in set(_recorded):
        if fp in current:
            _recorded.discard(fp)

    if events:
        _insert_history([
            (info[0], info[1], info[2], info[3], info[4], info[5], info[6], resolved_at)
            for info, resolved_at in events
        ])


def _insert_history(entries):
    with closing(_conn()) as conn:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=HISTORY_RETENTION_DAYS)).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z")
        conn.execute("DELETE FROM history WHERE resolved_at < ?", (cutoff,))
        conn.executemany(
            "INSERT INTO history(fingerprint, alertname, group_name, instance, severity, summary, starts_at, resolved_at) "
            "VALUES(?,?,?,?,?,?,?,?)", entries)
        conn.execute(
            "DELETE FROM history WHERE id NOT IN (SELECT id FROM history ORDER BY id DESC LIMIT ?)",
            (HISTORY_MAX,))
        conn.commit()


async def _history_loop():
    while True:
        try:
            await asyncio.to_thread(_collect_resolved)
        except Exception:
            pass
        await asyncio.sleep(HISTORY_POLL_INTERVAL)


async def _service_refresh_loop():
    while True:
        await asyncio.sleep(300)  # 每5分钟重读一次 service_map(配合 sync_service_map.py)
        try:
            await asyncio.to_thread(_load_service_map)
        except Exception:
            pass


@asynccontextmanager
async def lifespan(_: FastAPI):
    _init_db()
    await asyncio.to_thread(_load_service_map)
    task = asyncio.create_task(_history_loop())
    svc_task = asyncio.create_task(_service_refresh_loop())
    try:
        yield
    finally:
        task.cancel()
        svc_task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        try:
            await svc_task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="告警看板", version="1.2", lifespan=lifespan)


# ---------- API ----------
@app.get("/")
def index():
    return FileResponse(INDEX_FILE)


@app.get("/api/status")
def status():
    am_up = False
    try:
        am_get("/api/v2/status")
        am_up = True
    except Exception:
        am_up = False
    return {"ok": True, "time": time.time(), "alertmanager": am_up}


@app.get("/api/alerts")
def get_alerts():
    """按 group + alertname + severity 聚合; Alertmanager 不可达时降级 503"""
    try:
        alerts = am_get("/api/v2/alerts")
    except Exception as e:
        return JSONResponse(status_code=503, content={
            "error": True, "message": "Alertmanager 暂不可达: %s" % e,
            "total": 0, "silenced": 0, "groups": []})

    # 看板只展示未恢复的告警, 已恢复的进历史
    alerts = [a for a in alerts if (a.get("status") or {}).get("state") != "resolved"]
    ack_map = query_acks()
    groups = build_groups(alerts, ack_map)
    silenced = sum(1 for a in alerts if (a.get("status") or {}).get("silencedBy"))
    return {"total": len(alerts), "silenced": silenced, "groups": groups}


@app.get("/api/alert/{fingerprint}")
def alert_detail(fingerprint: str):
    try:
        alerts = am_get("/api/v2/alerts")
    except Exception as e:
        raise HTTPException(503, "Alertmanager 不可达: %s" % e)
    ack_map = query_acks()
    for a in alerts:
        if a.get("fingerprint") == fingerprint:
            a["ack"] = ack_map.get(fingerprint)
            inst = (a.get("labels") or {}).get("instance") or ""
            a["service_name"] = _service_name_for(inst)
            return a
    raise HTTPException(404, "not found")


@app.post("/api/ack/{fingerprint}")
def ack_alert(fingerprint: str, body: Optional[dict] = None):
    body = body or {}
    ack_by = (body.get("by") or "admin").strip() or "admin"
    comment = (body.get("comment") or "").strip()
    ack = {"ack_time": _now_iso(), "ack_by": ack_by, "comment": comment}
    with closing(_conn()) as conn:
        conn.execute(
            "INSERT INTO acks(fingerprint, ack_time, ack_by, comment) VALUES(?,?,?,?) "
            "ON CONFLICT(fingerprint) DO UPDATE SET ack_time=excluded.ack_time, ack_by=excluded.ack_by, comment=excluded.comment",
            (fingerprint, ack["ack_time"], ack_by, comment))
        conn.commit()
    return {"ok": True, "ack": ack}


@app.delete("/api/ack/{fingerprint}")
def unack_alert(fingerprint: str):
    with closing(_conn()) as conn:
        conn.execute("DELETE FROM acks WHERE fingerprint = ?", (fingerprint,))
        conn.commit()
    return {"ok": True}


# ---------- 静默 ----------
class SilenceIn(BaseModel):
    matchers: Optional[List[dict]] = None
    startsAt: Optional[str] = None
    endsAt: Optional[str] = None
    createdBy: str = "admin"
    comment: str = ""


@app.post("/api/silence")
def create_silence(s: SilenceIn):
    if not s.matchers:
        raise HTTPException(400, "至少需要一个 matcher")
    for m in s.matchers:
        if not isinstance(m, dict) or not m.get("name") or "value" not in m:
            raise HTTPException(400, "matcher 需包含 name 和 value")

    starts_dt = _parse_dt(s.startsAt, "startsAt") or datetime.now(timezone.utc)
    ends_dt = _parse_dt(s.endsAt, "endsAt")
    if ends_dt is None:
        raise HTTPException(400, "endsAt 必填")
    if ends_dt <= starts_dt:
        raise HTTPException(400, "endsAt 必须晚于 startsAt")
    if ends_dt - starts_dt > timedelta(days=MAX_SILENCE_DAYS):
        raise HTTPException(400, "静默时长不能超过 %d 天" % MAX_SILENCE_DAYS)

    payload = {
        "matchers": [
            {"name": m["name"], "value": str(m["value"]),
             "isRegex": bool(m.get("isRegex", False))}
            for m in s.matchers
        ],
        "startsAt": starts_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "endsAt": ends_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "createdBy": s.createdBy or "admin",
        "comment": s.comment or "",
    }
    try:
        return am_post("/api/v2/silences", payload)
    except Exception as e:
        raise HTTPException(503, "Alertmanager 不可达: %s" % e)


@app.get("/api/silences")
def list_silences(active: bool = True):
    try:
        silences = am_get("/api/v2/silences?active=%s" % ("true" if active else "false"))
        return {"silences": silences}
    except Exception as e:
        return JSONResponse(status_code=503, content={
            "error": True, "message": "Alertmanager 暂不可达: %s" % e, "silences": []})


@app.delete("/api/silence/{silence_id}")
def delete_silence(silence_id: str):
    req = urllib.request.Request(AM_URL + "/api/v2/silence/" + silence_id, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return {"ok": True, "http": resp.status}
    except urllib.error.HTTPError as e:
        raise HTTPException(e.code, e.reason)
    except Exception as e:
        raise HTTPException(503, "Alertmanager 不可达: %s" % e)


@app.get("/api/history")
def get_history(limit: int = 100):
    limit = max(1, min(limit, HISTORY_MAX))
    with closing(_conn()) as conn:
        rows = conn.execute(
            "SELECT id, alertname, group_name, instance, severity, summary, starts_at, resolved_at "
            "FROM history ORDER BY resolved_at DESC, id DESC LIMIT ?", (limit,)).fetchall()
    return {"history": [dict(r) for r in rows]}


@app.get("/healthz")
def healthz():
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=9095)
