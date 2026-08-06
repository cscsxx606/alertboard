#!/usr/bin/env python3
"""代码脱敏: 把真实IP替换成假IP, 清理内部描述"""
import re, os

# 假 IP 映射: 保持可读, 区分不同网段
FAKE = {}
# 172.21.195.x -> 10.10.10.x
for i in range(0, 256):
    FAKE["172.21.195.%d" % i] = "10.10.10.%d" % i
# 172.20.34.x -> 10.10.11.x
for i in range(0, 256):
    FAKE["172.20.34.%d" % i] = "10.10.11.%d" % i
# 172.18.158.x -> 10.10.12.x
for i in range(0, 256):
    FAKE["172.18.158.%d" % i] = "10.10.12.%d" % i

files = ["sync_blackbox.py", "sync_service_map.py", "README.md"]

for fn in files:
    if not os.path.exists(fn):
        continue
    s = open(fn, encoding="utf-8").read()
    orig = s
    # 1. IP 替换
    for real, fake in FAKE.items():
        s = s.replace(real, fake)
    # 2. 清理注释里的内部描述
    s = s.replace("Jenkins 库(op_platform)", "项目部署数据库")
    s = s.replace("Jenkins 库", "项目部署数据库")
    s = s.replace("26台IP中排除CDH", "指定主机中排除大数据集群")
    s = s.replace("cron 每天执行", "")
    s = s.replace("cron 每天", "")
    if s != orig:
        open(fn, "w", encoding="utf-8").write(s)
        print("已脱敏:", fn, "| 变更字节:", len(orig)-len(s))
    else:
        print("无变更:", fn)

# 验证: 是否还有真实IP残留
print("\n=== 残留检查 ===")
for fn in files:
    if os.path.exists(fn):
        s = open(fn, encoding="utf-8").read()
        real = re.findall(r"172\.(?:21\.195|20\.34|18\.158)\.\d+", s)
        print(fn, "残留真实IP:", real if real else "无✅")
