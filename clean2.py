#!/usr/bin/env python3
"""清理内部描述: Jenkins/op_platform 等字样脱敏"""
import os, re

files = ["backend.py", "sync_blackbox.py", "sync_service_map.py", "README.md"]
repl = [
    ("Jenkins 库", "项目部署数据库"),
    ("Jenkins", "项目数据源"),
    ("op_platform", "deploy_db"),
    ("query_jenkins", "query_db"),
]
for fn in files:
    if not os.path.exists(fn):
        continue
    s = open(fn, encoding="utf-8").read()
    orig = s
    for a, b in repl:
        s = s.replace(a, b)
    if s != orig:
        open(fn, "w", encoding="utf-8").write(s)
        print("已清理:", fn)
    else:
        print("无变更:", fn)

# 验证残留
print("\n=== 内部字样残留检查 ===")
for fn in files:
    if os.path.exists(fn):
        s = open(fn, encoding="utf-8").read()
        hit = [w for w in ["Jenkins", "op_platform", "bligou", "中锦", "huimin", "es-node", "bicore", "172.18", "172.21.195", "172.20.34"] if w.lower() in s.lower()]
        print(fn, "残留:", hit if hit else "无✅")
