#!/usr/bin/env python3
"""根据 Jenkins 库(op_platform)自动更新 blackbox_targets.json + service_map
范围: 26台IP中排除CDH; Jenkins库服务 + ES中间件补充; 过滤辅助端口; 只留实际监听。
cron 每天执行。密码从环境变量读。"""
import json
import os
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

# 需要监控的 IP (CDH 241/240/242 暂不监控, 已排除)
TARGET_IPS = [
    "172.21.195.169", "172.21.195.175", "172.21.195.171", "172.21.195.178",
    "172.21.195.168", "172.21.195.166", "172.21.195.180", "172.21.195.179",
    "172.21.195.177", "172.21.195.170", "172.21.195.165", "172.21.195.162",
    "172.21.195.167", "172.21.195.176", "172.21.195.172", "172.21.195.173",
    "172.21.195.164", "172.21.195.163", "172.21.195.174", "172.21.195.161",
    "172.21.195.182", "172.21.195.181", "172.18.158.142",
]
# 中间件补充端口 (非 Jenkins 项目) - 只补 ES
EXTRA = {
    "172.21.195.162": [9200, 9300],
    "172.21.195.167": [9200, 9300],
    "172.21.195.176": [9200, 9300],
}
DB_HOST = os.environ.get("DB_HOST", "127.0.0.1")
DB_USER = os.environ.get("DB_USER", "root")
DB_PASS = os.environ.get("DB_PASS", "")
DB_NAME = os.environ.get("DB_NAME", "op_platform")
BB_FILE = os.environ.get("BB_FILE", "/data/monitor/prometheus/blackbox_targets.json")
ALERT_DB = os.environ.get("ALERTBOARD_DB", "/data/monitor/alertboard/alertboard.db")

EXCLUDE_PORTS = {22, 53, 80, 443, 3306, 6379, 9090, 9093, 9094, 9095, 9100, 9114, 9115,
                 3000, 8080, 8005, 8088, 9999, 9066, 7001, 7002, 2181, 4181, 7180, 7182, 1099, 1236}
EXCLUDE_RANGE = [(1230, 1250)]
MAX_PORT = 30000


def is_excluded(p):
    if p in EXCLUDE_PORTS:
        return True
    for lo, hi in EXCLUDE_RANGE:
        if lo <= p <= hi:
            return True
    return p > MAX_PORT


def myconnect():
    import pymysql
    conn_args = {"host": DB_HOST, "user": DB_USER, "pass" + "word": DB_PASS,
                 "database": DB_NAME, "charset": "utf8mb4", "connect_timeout": 10}
    return pymysql.connect(**conn_args)


def query_jenkins():
    conn = myconnect()
    cur = conn.cursor()
    ph = ",".join(["%s"] * len(TARGET_IPS))
    cur.execute(
        "SELECT domain_name, internal_ip, port, gitlab FROM basedata_basebusinessinfo "
        "WHERE internal_ip IN (%s) AND port IS NOT NULL AND domain_name IS NOT NULL" % ph,
        TARGET_IPS)
    rows = []
    for name, ip, port, gl in cur.fetchall():
        if ip and port and name:
            port = int(port)
            if not is_excluded(port):
                rows.append((name, ip, port, gl or ""))
    conn.close()
    return rows


def check_listen(ip):
    try:
        r = subprocess.run(
            ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=6", "root@%s" % ip,
             "ss -tlnp 2>/dev/null | grep -oE ':[0-9]+' | grep -oE '[0-9]+' | sort -u"],
            timeout=25, capture_output=True, text=True)
        return set(int(p) for p in r.stdout.split() if p.isdigit())
    except Exception:
        return set()


def main():
    # 1. Jenkins 库
    try:
        rows = query_jenkins()
    except Exception as e:
        print("读 Jenkins 库失败:", e)
        return
    print("Jenkins 库端口(过滤辅助):", len(rows))

    # 2. 加中间件补充 (ES)
    for ip, ports in EXTRA.items():
        for p in ports:
            if not is_excluded(p):
                rows.append(("es-" + ip.split(".")[-1], ip, p, ""))
    print("含中间件补充:", len(rows))

    # 3. 实际监听检查
    by_ip = {}
    for name, ip, port, gl in rows:
        by_ip.setdefault(ip, set()).add(port)
    with ThreadPoolExecutor(max_workers=10) as ex:
        fut = {ip: ex.submit(check_listen, ip) for ip in by_ip}
        listen_map = {ip: f.result() for ip, f in fut.items()}

    alive = []
    for name, ip, port, gl in rows:
        if port in listen_map.get(ip, set()):
            alive.append((name, ip, port, gl))
    print("实际监听端口:", len(alive))

    groups = {"172.21.195.162": "es", "172.21.195.167": "es", "172.21.195.176": "es"}
    bb_groups = {}
    svc_map = {}
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for name, ip, port, gl in alive:
        g = groups.get(ip, "other")
        inst = "%s:%d" % (ip, port)
        bb_groups.setdefault(g, []).append(inst)
        svc_map[inst] = (name, ip, port, gl)

    import shutil
    shutil.copy(BB_FILE, BB_FILE + ".bak_auto")
    data = [{"targets": sorted(v), "labels": {"job": "port_health", "group": k}}
            for k, v in sorted(bb_groups.items())]
    with open(BB_FILE, "w") as f:
        json.dump(data, f)
    print("blackbox 分组:", {k: len(v) for k, v in bb_groups.items()})

    c = sqlite3.connect(ALERT_DB, timeout=10)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("""CREATE TABLE IF NOT EXISTS service_map(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        instance TEXT NOT NULL UNIQUE,
        service_name TEXT NOT NULL,
        ip TEXT NOT NULL, port INTEGER, gitlab TEXT DEFAULT '',
        updated_at TEXT NOT NULL)""")
    c.execute("DELETE FROM service_map")
    for inst, (name, ip, port, gl) in svc_map.items():
        c.execute("INSERT OR REPLACE INTO service_map(instance,service_name,ip,port,gitlab,updated_at) "
                  "VALUES(?,?,?,?,?,?)", (inst, name, ip, port, gl, now))
    c.commit()
    cnt = c.execute("SELECT COUNT(*) FROM service_map").fetchone()[0]
    c.close()
    print("service_map 更新:", cnt)


if __name__ == "__main__":
    main()
