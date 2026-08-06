#!/usr/bin/env python3
"""同步 Jenkins 库(op_platform) 服务+端口到告警看板数据库(alertboard.db)"""
import os
import sqlite3
from datetime import datetime

TARGET_IPS = [
    "172.21.195.169", "172.21.195.175", "172.21.195.171", "172.21.195.178",
    "172.21.195.168", "172.21.195.166", "172.21.195.180", "172.21.195.179",
    "172.21.195.177", "172.21.195.170", "172.21.195.165", "172.21.195.162",
    "172.21.195.167", "172.21.195.176", "172.21.195.172", "172.21.195.173",
    "172.21.195.164", "172.21.195.163", "172.21.195.174", "172.21.195.161",
    "172.21.195.182", "172.21.195.181", "172.20.34.241", "172.20.34.240",
    "172.20.34.242", "172.18.158.142",
]
DB_HOST = os.environ.get("DB_HOST", "127.0.0.1")
DB_USER = os.environ.get("DB_USER", "root")
DB_PASS = os.environ.get("DB_PASS", "")       # 密码从环境变量读
DB_NAME = os.environ.get("DB_NAME", "op_platform")
ALERT_DB = os.environ.get("ALERTBOARD_DB", "/data/monitor/alertboard/alertboard.db")


def sync():
    import pymysql
    rows = []
    try:
        conn = pymysql.connect(host=DB_HOST, user=DB_USER, password=DB_PASS,
                               database=DB_NAME, charset="utf8mb4", connect_timeout=10)
        cur = conn.cursor()
        ph = ",".join(["%s"] * len(TARGET_IPS))
        cur.execute(
            "SELECT domain_name, internal_ip, port, gitlab FROM basedata_basebusinessinfo "
            "WHERE internal_ip IN (%s) AND port IS NOT NULL AND domain_name IS NOT NULL" % ph,
            TARGET_IPS)
        for name, ip, port, gl in cur.fetchall():
            if ip and port and name:
                rows.append((name, ip, int(port), gl or ""))
        conn.close()
    except Exception as e:
        print("读 Jenkins 库失败:", e)
        return

    c = sqlite3.connect(ALERT_DB, timeout=10)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("""CREATE TABLE IF NOT EXISTS service_map(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        instance TEXT NOT NULL UNIQUE,
        service_name TEXT NOT NULL,
        ip TEXT NOT NULL, port INTEGER, gitlab TEXT DEFAULT '',
        updated_at TEXT NOT NULL)""")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    c.execute("DELETE FROM service_map")
    for name, ip, port, gl in rows:
        c.execute("INSERT OR REPLACE INTO service_map(instance,service_name,ip,port,gitlab,updated_at) "
                  "VALUES(?,?,?,?,?,?)",
                  ("%s:%d" % (ip, port), name, ip, port, gl, now))
    c.commit()
    cnt = c.execute("SELECT COUNT(*) FROM service_map").fetchone()[0]
    c.close()
    print("同步完成: %d 条服务映射 | 覆盖IP: %d 台" % (cnt, len(TARGET_IPS)))


if __name__ == "__main__":
    sync()
