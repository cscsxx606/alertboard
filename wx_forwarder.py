#!/usr/bin/env python3
"""
Alertmanager -> 企业微信 webhook 转换转发服务
监听 9094, 接收 Alertmanager 的 webhook 告警, 转换为企业微信消息格式后 POST 到企业微信机器人
加入: 项目名称/服务名, 端口, 报警时间/恢复时间
"""
import json
import os
import sqlite3
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

WX_URL = os.environ.get("WX_URL", "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=")
# 服务名映射库(由 sync_service_map.py 定时从 Jenkins 库同步)
SVC_DB = os.environ.get("ALERTBOARD_DB", "/data/monitor/alertboard/alertboard.db")

# 内存缓存服务名映射, 减少频繁读库
_svc_map = {}
_svc_map_loaded = 0


def _load_svc_map():
    global _svc_map, _svc_map_loaded
    try:
        c = sqlite3.connect(SVC_DB, timeout=5)
        _svc_map = dict(c.execute("SELECT instance, service_name FROM service_map"))
        c.close()
    except Exception:
        _svc_map = {}
    _svc_map_loaded = datetime.now().timestamp()


def _svc_name(instance):
    """根据 ip:port 返回服务名, 无则返回空"""
    now = datetime.now().timestamp()
    if (now - _svc_map_loaded) > 300:  # 每5分钟刷新
        _load_svc_map()
    return _svc_map.get(instance, "")


def _ts_str(iso):
    """ISO 时间 -> 本地可读时间; 空或无效返回 '-'"""
    if not iso:
        return "-"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return iso


def send_wx(content):
    payload = {"msgtype": "text", "text": {"content": content}}
    req = urllib.request.Request(
        WX_URL, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    try:
        resp = urllib.request.urlopen(req, timeout=8)
        return resp.read().decode()
    except Exception as e:
        return "ERROR: %s" % e


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            data = json.loads(body)
        except Exception:
            data = {}
        alerts = data.get("alerts", [])
        if not alerts:
            self._reply(200)
            return

        # 按 alertname 分组
        grouped = {}
        for a in alerts:
            labels = a.get("labels", {})
            key = labels.get("alertname", "unknown")
            status = a.get("status", "firing")
            grouped.setdefault(key, []).append(a)

        lines = []
        for key, items in grouped.items():
            all_firing = all(a.get("status") == "firing" for a in items)
            state = "🔥 告警" if all_firing else "✅ 恢复"
            lines.append("%s 【%s】" % (state, key))
            for a in items:
                labels = a.get("labels", {})
                ann = a.get("annotations", {})
                inst = labels.get("instance", "?")
                sev = labels.get("severity", "info")
                job = labels.get("job", "")
                # 解析 实例 ip:port
                ip = inst
                port = ""
                if ":" in inst:
                    ip, port = inst.rsplit(":", 1)
                # 服务名
                svc = _svc_name(inst)
                # 时间
                if all_firing:
                    time_label = "报警时间"
                    time_val = _ts_str(a.get("startsAt"))
                else:
                    time_label = "恢复时间"
                    time_val = _ts_str(a.get("endsAt") or a.get("startsAt"))
                summary = ann.get("summary", ann.get("description", ""))
                is_host = (job == "node" and inst.endswith(":9100"))
                if is_host:
                    # 主机类告警: 用 description(带具体值), 显示 IP + 级别 + 详情 + 时间
                    detail = ann.get("description", ann.get("summary", ""))
                    lines.append("  主机: %s" % ip)
                    lines.append("  级别: %s" % sev)
                    if detail and detail != inst:
                        lines.append("  详情: %s" % detail)
                    lines.append("  %s: %s" % (time_label, time_val))
                else:
                    # 端口类告警: IP + 端口 + 项目名 + 级别 + 详情 + 时间
                    lines.append("  实例: %s" % ip)
                    if port:
                        lines.append("  端口: %s" % port)
                    if svc:
                        lines.append("  项目: %s" % svc)
                    lines.append("  级别: %s" % sev)
                    if summary and summary != inst:
                        lines.append("  详情: %s" % summary)
                    lines.append("  %s: %s" % (time_label, time_val))
        content = "\n".join(lines)
        if not content:
            self._reply(200)
            return
        for i in range(0, len(content), 1800):
            send_wx(content[i:i + 1800])
        self._reply(200)

    def do_GET(self):
        self._reply(200, b"alertmanager->wecom forwarder running")

    def _reply(self, code, body=b"ok"):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    _load_svc_map()
    server = HTTPServer(("0.0.0.0", 9094), Handler)
    print("Alertmanager->WeCom forwarder listening on :9094")
    server.serve_forever()
