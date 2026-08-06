# 告警聚合看板 (Alert Board)

一个自托管的高颜值**告警聚合与运营看板**，用于集中展示 **Prometheus / Alertmanager** 的告警，按服务分组聚合，并提供**静默管理、告警确认、恢复历史**等运营能力。附带**服务名自动映射**（从 项目数据源/项目库同步 `ip:port → 项目名`），让告警显示"实例 IP + 端口 + 项目名称 + 报警时间"，而不是裸的 IP:端口。

> 适合：已经用 Prometheus + Alertmanager 做监控、但想要一个更直观的告警运营页面，且希望报警信息带上业务项目名的团队。

---

## ✨ 功能

- **按服务分组聚合**：告警按 `group`（如 cdh/es/other）分组，两级聚合（group → 告警类型 → 实例列表）
- **服务名映射**：自动把 `ip:port` 映射成 `服务名/项目名`（从项目库同步），告警推送显示项目名
- **告警操作**：确认（ack）、静默（silence，含时长/匹配规则）、查看详情、恢复历史
- **实时刷新**：前端 15s 自动刷新，支持搜索、折叠
- **企业微信告警推送**：告警/恢复按格式化文本推送到企业微信机器人，区分主机类/端口类告警
- **端口监控清单自动同步**：从项目库自动生成 Prometheus `blackbox_targets.json`（端口探测清单），过滤辅助端口、只保留实际监听的端口

---

## 🧠 业务逻辑

### 架构

```
┌─────────────────────────────────────────────────────────────┐
│  项目/服务库 (如 项目数据源 的 deploy_db 数据库)               │
│  存: 项目名 + IP + 端口                                       │
└──────────────────────────┬──────────────────────────────────┘
                           │ sync_service_map.py / sync_blackbox.py (定时同步)
                           ▼
┌─────────────────────────────────────────────────────────────┐
│  alertboard.db (SQLite)                                      │
│   - service_map: ip:port → 服务名/项目名                     │
│   - acks: 告警确认记录                                        │
│   - history: 已恢复告警历史                                   │
└──────────────────────────┬──────────────────────────────────┘
                           │ 读表 / 查询
                           ▼
┌─────────────────────────────────────────────────────────────┐
│  backend.py (FastAPI, 端口 9095)                             │
│  - /api/alerts   按 group 聚合告警(含服务名)                  │
│  - /api/alert/{fp}  告警详情                                 │
│  - /api/ack      确认/取消确认                               │
│  - /api/silence  静默管理                                    │
│  - /api/history  恢复历史                                    │
└───────────────┬──────────────────────────┬──────────────────┘
                │                          │
         Prometheus/Alertmanager     前端 index.html (单页)
         (9093 拉告警)               (15s 自动刷新, 卡片式UI)
```

### 告警数据流

```
Prometheus 规则 (如 PortDown)
   → Alertmanager (9093) 聚合内存告警
   → backend.py /api/alerts 按时拉取
   → 前端渲染 (分组卡片 + 每个告警显示 实例IP + 端口 + 项目名 + 时间)
        ↓ (可选)
   → wx-forwarder.py (9094) 转换后
   → 企业微信机器人推送
```

### 告警推送格式

**端口类告警（job=port_health）**
```
🔥 告警 【PortDown】
  实例: 10.10.10.163
  端口: 20002
  项目: report-task-prod
  级别: critical
  详情: 端口 10.10.10.163:20002 不可达
  报警时间: 2026-08-06 09:30:00
```

**主机类告警（job=node）**
```
🔥 告警 【HighCPU】
  主机: 10.10.10.175
  级别: warning
  详情: CPU 使用率已超过 90% (当前 92.3%)
  报警时间: 2026-08-06 09:30:00
```

> 恢复时(resolved) 显示"恢复时间"，状态前缀变 `✅ 恢复`。

### 告警聚合（两级）

1. **一级分组**：默认按 `labels.group`（cdh/es/other）分组
2. **二级聚合**：组内按 `alertname + severity` 聚合，每个实例（ip:port）一行

---

## 🚀 部署

### 环境
- Python 3.10+
- 依赖：`pip install -r requirements.txt`（fastapi, uvicorn）

### 1. 后端

```bash
# 目录
mkdir -p /data/monitor/alertboard
cd /data/monitor/alertboard
# 放 backend.py + index.html + requirements.txt

# 建虚拟环境(可选) 或直接用系统 python
pip install -r requirements.txt

# 启动
python -m uvicorn backend:app --host 0.0.0.0 --port 9095
```

### 2. 配置环境变量（可选）

| 变量 | 默认 | 说明 |
|---|---|---|
| `AM_URL` | `http://127.0.0.1:9093` | Alertmanager 地址 |
| `ALERTBOARD_DB` | `./alertboard.db` | 本库 SQLite 路径 |
| `DB_HOST/DB_USER/DB_PASS/DB_NAME` | — | 项目库连接（供服务名映射/端口同步）|

> 密码从环境变量读，不在代码里写明文。

### 3. 服务名映射自动同步（可选）

```bash
# sync_service_map.py: 从项目库读 ip:port → 服务名, 写入 alertboard.db
DB_PASS=xxx python3 sync_service_map.py

# 定时 (crontab 每5分钟)
*/5 * * * * cd /data/monitor/alertboard && DB_PASS=xxx python3 sync_service_map.py
```

### 4. 端口监控清单自动同步（可选）

```bash
# sync_blackbox.py: 从项目库生成 Prometheus blackbox_targets.json (端口探测清单)
# 过滤辅助端口(JMX等), 只保留实际监听的端口, 并按 group 分组
DB_PASS=xxx python3 sync_blackbox.py

# 定时 (crontab 每天01:00)
0 1 * * * cd /data/monitor/alertboard && DB_PASS=xxx python3 sync_blackbox.py
```

### 5. 企业微信告警推送（可选）

```bash
# wx_forwarder.py: 监听9094, 把 Alertmanager webhook 转企微格式推送到机器人
# 需设置企业微信 webhook key(不含明文, 从环境变量读):
#   export WX_URL="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=<你的企微机器人key>"
# 在 alertmanager.yml 配置 webhook 指向 http://127.0.0.1:9094/alert
python3 wx_forwarder.py
```

---

## 📂 目录结构

```
alertboard/
├── backend.py            # FastAPI 后端 (告警聚合/详情/确认/静默/历史)
├── index.html            # 前端单页 (卡片式告警看板)
├── wx_forwarder.py       # 企业微信告警推送转发 (可选)
├── sync_service_map.py   # 服务名映射 自动同步
├── sync_blackbox.py      # 端口监控清单 自动同步
├── README.md
├── requirements.txt
└── (运行后生成)
    ├── alertboard.db     # SQLite 数据(不入库, .gitignore)
    └── __pycache__/
```

---

## 🔒 安全说明

- **代码不含明文密码**：连接数据库的凭据从环境变量读取
- **SQLite 数据库、缓存不入库**（见 .gitignore），避免泄露运行时数据
- 告警看板**无内置认证**（适合内网使用），生产环境建议置于内网/VPN，或自行加认证层

---

## 📄 License

MIT
