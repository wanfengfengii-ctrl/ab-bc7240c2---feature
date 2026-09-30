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

## 读出模式校准（可选）

探测器在不同读出模式间切换时需要独占设备完成校准。在页面勾选“启用模式切换排程”
（或在请求中提供 `modes`）后，可为**每台设备**登记：

- `initial_mode`：该设备开始执行前的初始模式；
- `transitions[]`：有向切换 `(from_mode, to_mode, duration)`，表示独占校准耗时。

每项曝光通过 `mode` 选择所需读出模式。求解时，**设备执行顺序与校准段联合求解**（CP-SAT
circuit 与所有时刻约束在同一模型内）：

- 设备上的首项从 `initial_mode` 切换；后续每项只允许从同设备**紧邻前项**的模式切换，
  不会对非相邻曝光重复计入切换耗时；
- 未登记的不同模式转换**不可达**，绝不按零耗时处理；同模式相邻则无需校准；
- 校准为独占区间：首项校准从时刻 0 起，其余校准在前项“曝光 + 冷却”结束时**立即连续占机**
  （`start = s_prev + d_prev + cooling_prev`），并须在该项开始前完成；
- 原有时间窗、冷却占用与曝光间衔接必须同时成立——不先定顺序再补校准。

响应在可行时额外返回 `calibrations[]`，按设备执行顺序列出每段校准的 `start/finish`、
`from_mode/to_mode`、`predecessor_id`（首项为 `null`）、`successor_id` 与完成后到后项
开始的 `margin`。模式引用未声明、已启用设备未选模式、切换表重复登记等属于 **400 输入错误**；
登记合法但没有可达转换/无法完成校准则返回 200 `feasible=false`，不含任何部分方案。
未提供 `modes`（或为空）时，请求字段、三级优化顺序与响应结构与旧版完全兼容。

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
│   └── tests/                # pytest：调度器/模式 24 项 + API 11 项
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
| 0 | 1  | 代码测试（pytest，35 项） |
| 1 | 2  | 构建/完整性（字节码编译、应用导入、静态资源非空） |
| 2 | 4  | 可行排程（独立复核时间窗、设备占用含冷却、衔接间隔） |
| 3 | 8  | 无解 API 冒烟（200 + feasible=false 且无部分解；对比 400 输入错误；Web/API 健康） |
| 4 | 16 | 读出模式校准（兼容输入、首项切换、方向不对称、冷却后连续占机、无可达转换） |

## HTTP 约定

| 场景 | 状态码 | 响应 |
|------|--------|------|
| 可行 | 200 | `feasible=true` + starts/finishes/makespan/sum_starts/slacks/equipment_orders/calibrations |
| 输入合法但无可行时序 | 200 | `feasible=false, reason="no_schedule"`，所有方案字段为 `null` |
| 输入错误（编号重复、时间窗倒置、未知引用、模式引用错误/切换表冲突等） | 400 | `reason="input_error"` + `field_errors[]` |
| 字段级 schema 错误 | 422 | FastAPI 校验明细 |
| 求解器超时未能判定 | 503 | `reason="solver_timeout"`（不谎称为无解） |

页面区分三类结果面板：**输入错误**（红）、**无可执行时序**（黄）、**最优排程**，
可行时展示 SVG 时间轴（曝光段/冷却段/模式切换校准段/时间窗）、各设备执行顺序、每段切换
校准的起止、前后模式与对后项开始的余量，以及每条约束的实际间隔与对最大间隔的余量。

## 本地开发（无 Docker）

```bash
cd backend
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
cd ../web/src && python3 -m http.server 8080   # 仅静态预览；API 需同源反代
python3 -m pytest backend/tests -q
python3 backend/scripts/verify.py              # 默认访问 http://api:8000，用 API_URL 覆盖
```
