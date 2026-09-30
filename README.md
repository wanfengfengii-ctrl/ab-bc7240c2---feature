# 同步辐射束线 · 探测器曝光排程系统

值班工程师在页面录入 5–10 项探测器曝光（持续时间、最早/最晚开始时刻、设备、结束后冷却时间）
与曝光间的最小/最大衔接间隔，服务端用 CP-SAT 联合求出全部**整数**开始时刻，并按以下顺序
**字典序**优化：

1. 最终结束时刻（所有曝光结束时刻的最大值）最小；
2. 开始时刻总和最小；
3. 按录入顺序展开的开始时刻序列字典序最小。

约束：

- 每项曝光满足其可开始时间窗；
- 同一设备同一时刻只能执行一项曝光，且后续曝光不得早于前序“曝光 + 冷却”结束；
- 每条衔接约束同时满足 `min_gap ≤ s_b − (s_a + d_a) ≤ max_gap`（最大间隔可留空）。

输入合法但无解时返回 `feasible=false`，**绝不返回部分曝光方案，也不沿用旧方案**；
草稿一经修改，前端立即作废旧结果并提示重新求解。

## 目录结构

```
.
├── docker-compose.yml        # api + web + verify（一次性）
├── .env.example              # 宿主机端口 API_PORT / WEB_PORT
├── backend/
│   ├── Dockerfile            # FastAPI + uvicorn，含 HEALTHCHECK
│   ├── requirements.txt
│   ├── app/
│   │   ├── main.py           # /health、/api/schedule
│   │   ├── scheduler.py      # CP-SAT 三阶段字典序优化
│   │   └── schemas.py        # 请求/响应模型
│   ├── scripts/verify.py     # 一次性校验脚本（退出码位掩码）
│   └── tests/                # pytest：调度器 13 项 + API 8 项
└── web/
    ├── Dockerfile            # nginx 静态站 + 反代 /api，含 HEALTHCHECK
    ├── nginx.conf            # /health 与 /api 反代
    └── src/                  # index.html / app.js / styles.css（无构建步骤）
```

## 启动

```bash
cp .env.example .env          # 可选：修改 API_PORT / WEB_PORT
docker compose build
docker compose up -d
```

- 页面：`http://localhost:${WEB_PORT:-8080}`
- API 健康检查：`http://localhost:${API_PORT:-8000}/health`
- Web 健康检查：`http://localhost:${WEB_PORT:-8080}/health`

宿主机端口通过环境变量配置，例如 `API_PORT=9000 WEB_PORT=9090 docker compose up -d`。

## 一次性校验服务

```bash
docker compose run --rm verify
```

`verify` 服务**自行退出**，退出码为位掩码（0 表示全部通过）：

| 位 | 值 | 检查内容 |
|----|----|----------|
| 0 | 1  | 代码测试（pytest，21 项） |
| 1 | 2  | 构建/完整性（字节码编译、应用导入、静态资源非空） |
| 2 | 4  | 可行排程（独立复核时间窗、设备占用含冷却、衔接间隔） |
| 3 | 8  | 无解 API 冒烟（200 + feasible=false 且无部分解；对比 400 输入错误；Web/API 健康） |

## HTTP 约定

| 场景 | 状态码 | 响应 |
|------|--------|------|
| 可行 | 200 | `feasible=true` + starts/finishes/makespan/sum_starts/slacks/equipment_orders |
| 输入合法但无可行时序 | 200 | `feasible=false, reason="no_schedule"`，所有方案字段为 `null` |
| 输入错误（编号重复、时间窗倒置、未知引用等） | 400 | `reason="input_error"` + `field_errors[]` |
| 字段级 schema 错误 | 422 | FastAPI 校验明细 |
| 求解器超时未能判定 | 503 | `reason="solver_timeout"`（不谎称为无解） |

页面区分三类结果面板：**输入错误**（红）、**无可执行时序**（黄）、**最优排程**，
可行时展示 SVG 时间轴（曝光段/冷却段/时间窗）、各设备执行顺序及每条约束的实际间隔与对最大间隔的余量。

## 本地开发（无 Docker）

```bash
cd backend
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
cd ../web/src && python3 -m http.server 8080   # 仅静态预览；API 需同源反代
python3 -m pytest backend/tests -q
python3 backend/scripts/verify.py              # 默认访问 http://api:8000，用 API_URL 覆盖
```
