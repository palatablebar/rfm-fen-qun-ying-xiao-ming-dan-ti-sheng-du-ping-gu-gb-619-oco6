# RFM 分群与营销名单提升度评估（`rfmseg`）

> 数据工程 · 数据分析与数据应用 / 中型多模块。原生 HTML/CSS/JS 前端 + Python FastAPI 后端 + Celery worker + PostgreSQL 16 + Redis，5 个容器一键起。

## 1. 场景

- **模拟客户**：牧野茶饮（新中式茶饮连锁，32 万会员、8 个城市门店）会员运营组 6 人，季度发券预算 40 万。
- **现有工作流**：门店 POS 与小程序订单每天落到一张宽表；运营用 Excel 做周报，凭经验挑「最近买过的人」整群发券，
  券平台按手机号名单打包下发，核销率月底从券平台后台手工抄回来。
- **三个痛点**：
  1. **分群阈值拍脑袋**：R/F/M 的「高/低」全凭主观，换个人做一次结果就变，没人能解释为什么这么切。
  2. **名单导出后不知道能带来多少增量**：只知道发了多少张、核销多少张，不知道「不发也会买」的那部分占多少。
  3. **预算有限，先发谁没依据**：40 万分到 8 类人群，先发给谁、每人给多少券，全靠开会拍。
- **新增数据风险**：退款记录常跨周晚到回传，同一笔还可能重复回传；若直接按成交额分群，
  高退货会员会凭毛额进入高分群，活动预算与预计 GMV 随之虚高。因此正式分群前必须先做**退款核对**。
- **上下游**：POS 订单 / 小程序订单 → 本系统（会员与订单接入 → RFM 分位评分 → 八类分群 → 名单与预算 → 提升度与 ROI）→
  券平台（CSV 名单下发）/ 私域企微群。
- **一天的工作剧本**：
  1. 09:10 门店 POS 日结文件同步完成，订单表新增 3.2 万单（演示库 30,000 单可在 8,000 人抽样上重放）。
  2. 09:15 券平台回传上一周的退款明细（含跨周晚到与重试重复）。数据专员在「退款核对」导入批次：
     重复回传自动识别、不重复冲减；无法归属与累计超过原单的记录单独列出。
  3. 09:20 数据专员确认核对：批次从「待核对」进入「已核对」，分群按净额自动重算。
  4. 09:20 数据专员打开「RFM 分群」，看到快照日 2026-11-14、大盘购买率 2.81%、八个分群的 lift。
  5. 09:25 运营管理员把权重从 0.3/0.3/0.4 调到 0.6/0.2/0.2，点「重算分群」：分位切点移动，分群人数随之变化，页面直接给出新阈值。
  6. 09:40 在「名单与提升度」选「秋日会员日券」，把预算从 1,400 改成 2,000，试算看到哪几个分群被优先分配、哪几个「预算不足」。
  7. 10:00 点「生成名单」，得到 A2026111401：覆盖 X 人、券预算 Y 元、预计增量 Z 单、整体 ROI R。
  8. 10:10 数据专员点「确认名单」，管理员在「导出与审计」下载名单明细 CSV，交给券平台。
  9. 14:00 门店只读角色登录，只能看到脱敏手机号，导出按钮置灰；直接调 `/api/exports/members.csv` 返回 403。
  10. 次日 07:40 Celery worker 定时重算 RFM 并为进行中活动补一份名单；若有未核对退款批次则跳过，审计日志留痕。

## 2. 角色与权限

| 角色 | 账号 | 权限 | 典型动作 |
|---|---|---|---|
| 会员运营管理员 `admin` | `admin@rfmseg.io` | 配置 + 重算 + 生成名单 + 退款核对 + 导出 + 审计 | 调 RFM 权重/窗口、改分群券面额、导入并核对退款、生成名单、导出、看审计 |
| 会员数据专员 `analyst` | `analyst@rfmseg.io` | 看分群与 lift、退款核对、确认名单、导出 | 试算预算、导入退款批次并核对、确认名单、导出分群表 |
| 门店业务只读 `viewer` | `viewer@rfmseg.io` | 只读列表与详情（手机号脱敏） | 看分群、看会员、看退款核对清单、看名单；写/导出/审计一律 403 |

密码统一：`Demo@2026`。低权角色调用高权接口返回 **真实 HTTP 403**（`src/middlewares/rbac.py`，不靠前端隐藏）。
未登录 401（`AUTH_MISSING`）、参数非法 422（`VALIDATION`）、重复自然键 409（`CONFLICT`）。

## 3. 业务模块表（7 个）

| # | 模块 | 对应页面 | 主要 services / controller | 主要 API |
|---|---|---|---|---|
| ① | 会员与订单接入 | 会员与订单 `#/members` | `member_service` / `member_controller` | `GET /api/members`、`GET /api/members/{id}`、`GET /api/orders` |
| ② | RFM 评分（算法 A） | RFM 分群 `#/segments` | `rfm_service`、`segment_service` / `segment_controller` | `POST /api/segments/recompute`、`GET /api/segments` |
| ③ | 分群规则与券口径 | RFM 分群 `#/segments` | `segment_service` / `segment_controller` | `PATCH /api/segments/{id}` |
| ④ | 退款核对（晚到回传 / 异常隔离） | 退款核对 `#/reconciliation` | `reconciliation_service` / `reconciliation_controller` | `POST /api/reconciliations/batches`、`POST /api/reconciliations/{id}/confirm`、`GET /api/reconciliations/checklist` |
| ⑤ | 名单生成（算法 B 落库） | 名单与提升度 `#/audiences` | `audience_service` / `audience_controller` | `POST /api/audiences`、`GET /api/audiences`、`PATCH /api/audiences/{id}/review` |
| ⑥ | 提升度与预算分配（算法 B） | 名单与提升度 `#/audiences` | `lift_service`、`audience_service` / `campaign_controller` | `GET /api/campaigns`、`POST /api/campaigns`、`GET /api/campaigns/{id}/allocation` |
| ⑦ | 导出与审计 | 导出与审计 `#/exports` | `export_service`、`audit_service` / `export_controller` | `GET /api/exports/{members,segments,audiences}.csv`、`GET /api/audit` |

横切能力（不计入业务模块）：JWT + 三角色 RBAC、限流（Redis 固定窗口，写接口单独额度）、审计中间件、worker 心跳。
登录页与总览是横切入口，也不算业务模块。

## 4. 快速启动

```bash
cp .env.example .env
docker compose up -d --build
# 等 5/5 healthy（首次约 30~60s，含建表 + seed）
open http://localhost:18208
```

首次启动 backend 会自动建表并执行一次 seed（`SEED_DEMO=true`）。seed 幂等，可重复执行：

```bash
docker compose exec -T backend python -m scripts.seed_demo   # 打印 SEED_SUMMARY
docker compose exec -T backend python -m pytest tests -q
```

## 5. 本地开发

```bash
# backend
cd backend && python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn src.main:app --reload --port 8000
# 前端是静态站点，任意静态服务器即可；接口默认 /api -> 8000
cd frontend/site && python -m http.server 5173
```

## 6. 访问地址

| 服务 | 地址 |
|---|---|
| 前端 | http://localhost:18208 |
| 后端 API 文档 | http://localhost:19208/docs |
| 健康检查 | http://localhost:19208/api/health |
| 数据库映射 | localhost:17708 |

## 7. 技术栈

| 层 | 选型 |
|---|---|
| 前端 | 原生 HTML5 + CSS（oklch 令牌 / 弹性布局）+ ES Modules；无框架、无打包 |
| 后端 | Python 3.11、FastAPI 0.115、SQLAlchemy 2.0、PyJWT、passlib(bcrypt) |
| 异步 | Celery 5.4 + Redis（心跳、每日重算 RFM 并补名单） |
| 数据库 | PostgreSQL 16 |
| 部署 | Docker Compose，恰好 5 服务 |

## 8. 目录

```text
backend/
  src/{config,constants,controllers,middlewares,models,routes,schemas,services,utils}
  scripts/{seed_demo.py,demo_config.py,worker_healthcheck.sh}
  migrations/  tests/
frontend/
  site/{index.html,css/{tokens,app}.css,js/{config,api,store,ui,app}.js,js/pages/*.js}docker-compose.yml  .env  README.md
```

## 9. 环境变量

见 `.env.example`：`COMPOSE_PROJECT_NAME`、`FRONTEND_PORT`、`BACKEND_PORT`、`DB_PORT`、`DB_*`、`JWT_SECRET`、`REDIS_URL`、
`SEED_DEMO`、`RATE_LIMIT_*`、`APP_TIMEZONE_OFFSET_MINUTES`。

## 10. 核心算法与口径

### 算法 A｜RFM 评分与八类规则分群（`backend/src/services/rfm_service.py`）

1. 单人指标（快照日 `as_of` = 库内最新订单日期）：
   - `R` = `as_of − 最近一次下单日期`（天）；从未下单记 9999（分档必然最低）。
   - `F` = 窗口内订单数；`M` = 窗口内订单金额合计 **减去已核对退款（净额）**。**窗口默认 365 天，可调 30~730 天**。
   - 分群同时汇总订单毛额 / 退款额 / 净额：高退货会员按净额落档，不会凭成交额虚高进高分群。
2. 分位切档：全量会员的 R / F / M 各算 20/40/60/80 分位（线性插值），值与切点比较得到 1~5 分；
   **同值必然同档**（只比较切点，不做名次排序，避免并列被拆开）。R 越小越好 → 分档反向。
3. 综合分 = `0.3·R + 0.3·F + 0.4·M`（权重可配，三项之和必须为 1，否则 422）。
4. 八类规则分群（按顺序首次命中，规则文本见 `constants/enums.py` 与页面）：
   重要价值 `R≥4∧F≥4∧M≥4`、重要保持 `R≥3∧F≥4∧M≥3`、重要发展 `R≥4∧F≥3∧M≥4`、
   重要挽留 `R≤2∧F≥4∧M≥4`、新客养成 `R≥4∧F≤2`、流失预警 `R≤2∧F≥2∧M≥2`、
   一般价值 `R≥3∧M≤3`、沉睡会员（其余）。
5. 分群购买率 = 群内会员历史券响应率均值；大盘购买率 = 全量均值；`lift = 分群购买率 / 大盘购买率`。
6. 失败态：会员表为空 → 409；权重之和 ≠ 1 或窗口越界 → 422；空输入（算法层）→ `status=partial`。

> 为什么默认窗口是 365 天而不是 180 天：R 与 F/M 同窗口时，「最近一次下单在 180 天前」意味着窗口内几乎没有订单，
> 于是 `重要挽留 / 流失预警`（R 低但 F/M 高）在定义上永远为空。取一年窗口，这三类才有业务含义。
> 页面与 API 可以把窗口改成 180 天（`window_days=180`），行为可见、可解释。

### 算法 B｜提升度与券预算分配（`backend/src/services/lift_service.py`）

1. `lift_s = p_s / p0`，`p_s` = 分群购买率，`p0` = 大盘购买率（同一份数据算出来，不写死）。
2. 单人成本 `unit_cost_s = 触达成本 + p_s × 券面额`（券只在核销时产生成本，按期望核销率折算）。
3. 贪心排序：`eff_s = lift_s / unit_cost_s` 降序（同分按 lift 降序、编码升序，保证确定性）；`lift ≤ 1` 的群不投。
4. 预算分配：按序取 `A_s = min(群人数, ⌊剩余预算 / unit_cost_s⌋)`，扣减剩余预算。
5. **边际收益递减**：群内前 `core_share`（默认 0.6）为「高响应核心盘」按全效计算，超出部分乘 `tail_factor`（默认 0.6）。
6. 增量：`Δp_s = p_s − p0`；增量人数 = `(核心盘人数 + 0.6 × 溢出人数) × Δp_s`；
   增量单量 = 增量人数 × `orders_per_converter`（默认 1.4）；增量 GMV = 增量单量 × 分群客单价（订单事实算出）。
   **客单价按分群净额 / 毛额折算**，退款越多预计 GMV 越低。
7. `ROI = 增量 GMV / 该群券预算`；整体 ROI = 总增量 GMV / 总花费。页面对每个分群展示这一整条推导。

### 退款核对（`backend/src/services/reconciliation_service.py`）

晚到退款常跨周回传，同一笔还可能重复。核对口径：

1. **批次幂等**：批次以 `batch_key` 为自然键。同一批重试导入直接返回原批次，不新增退款记录、不重复冲减；
   确认动作本身也幂等（重复确认不再次落账）。
2. **重复回传**：退款笔以 `refund_no`（缺省时用「订单号 + 金额 + 日期」）为去重键。同一笔跨批次再次出现
   记为 `duplicate`，`applied_amount = 0`，**不冲减净额**，在核对页「重复回传」清单单列。
3. **异常单独列出**（`exceptions`，须逐条知晓才能确认）：
   - 无法归属 `unattributable`：订单号不存在、会员号与原单不一致、金额非法；
   - 累计超过原单 `over_refund`：历史已核对退款 + 本笔 > 订单毛额，只把剩余可退部分计入冲减，
     超出部分记入 `exception_amount` 并列入异常清单。
4. **核对门禁**：存在 `pending` 批次时，重算分群、名单试算/生成/确认、会员/分群/名单三类导出
   一律 409 `REFUND_UNRECONCILED`（`tasks.py` 定时任务也会跳过），**未核对结果不能进入正式分群**。
5. **确认落账**：批次从「待核对」进入「已核对」，随后按当前快照权重/窗口重算 R/F/M（M 与增量 GMV 用净额），
   并刷新仍为「待确认」的名单口径；已确认/已推送名单保持不动。
6. **逐项核对清单**（`GET /api/reconciliations/checklist`）逐项比对：订单毛额、退款额、净额、分群人数、
   名单人数、名单毛额/退款/净额、活动预计 GMV；已核对状态下还会真实生成名单 CSV 并按行求和复核，
   保证「页面 / 分群 / 导出」同源。待核对期间导出被冻结，导出列显式标注而不参与比对。

### seed 数量口径（`SEED_SUMMARY`）

`SEED_SUMMARY brand=rfmseg users=3 sources=8000 runs=30000 findings=8 pending=2`

| 键 | 含义 |
|---|---|
| `users` | 3 个演示账号 |
| `sources` | 会员数（8,000，32 万会员的 1/40 抽样） |
| `runs` | 订单数（30,000） |
| `findings` | RFM 分群数（8） |
| `pending` | 待确认名单数（2，对应 2 个活动） |

退款回传也由 seed 确定性生成两个批次（`scripts/demo_config.py` → `REFUND_CONFIRMED_BATCH / REFUND_PENDING_BATCH`）：

- `seed-refund-wk45`：36 笔，**已核对**，历史退款真实冲减；
- `seed-refund-wk46`：26 笔，**待核对**，含 22 笔正常冲减、2 笔跨批重复回传（不冲减）、
  1 笔订单号不存在（无法归属）、1 笔累计超过原单（只冲减剩余可退部分）。

因此 seed 完成后页面初始停在「退款核对 · 待核对」，分群重算 / 名单生成 / 导出被冻结；
在「退款核对」页确认核对（异常已列清单）后自动重算分群并刷新待确认名单，页面进入「已核对」。

## 11. 共享枚举出现位置

| 枚举 | 后端 | 前端 |
|---|---|---|
| 角色 `admin/analyst/viewer` | `backend/src/constants/{roles,enums}.py` | `frontend/site/js/config.js` → `ROLE_LABELS` / `PERMISSIONS` |
| 八类分群 `champion…hibernating` | `backend/src/constants/enums.py` → `SegmentCode/SEGMENT_LABELS/SEGMENT_RULES` | `frontend/site/js/config.js` → `ENUMS.segment/segmentRules/segmentOrder` |
| 会员卡等级 `normal/silver/gold/black` | `backend/src/constants/enums.py` → `MemberTier` | `frontend/site/js/config.js` → `ENUMS.tier` |
| 活动状态 `draft/active/closed` | `backend/src/constants/enums.py` → `CampaignStatus` | `frontend/site/js/config.js` → `ENUMS.campaignStatus` |
| 名单状态 `pending/confirmed/pushed` | `backend/src/constants/enums.py` → `AudienceStatus` | `frontend/site/js/config.js` → `ENUMS.audienceStatus` |
| 退款记录状态 `pending/applied/duplicate/unattributable/over_refund` | `backend/src/constants/enums.py` → `RefundStatus` | `frontend/site/js/config.js` → `ENUMS.refundStatus` |
| 核对批次状态 `pending/confirmed` | `backend/src/constants/enums.py` → `ReconciliationStatus` | `frontend/site/js/config.js` → `ENUMS.reconciliationStatus` |
| 订单渠道 `miniapp/store/delivery` | `backend/src/constants/enums.py` → `OrderChannel` | `frontend/site/js/config.js` → `ENUMS.channel` |
| 分配状态 `allocated/deferred/ineligible` | `backend/src/services/lift_service.py` | `frontend/site/js/config.js` → `ENUMS.allocationStatus` |

## 12. Docker 说明

- 5 个服务：`frontend`(Nginx 静态站) / `backend`(uvicorn) / `worker`(Celery) / `db`(PostgreSQL 16) / `redis`，全部带 healthcheck。
- 命名卷：`db_data`、`redis_data`；`backend`、`worker` 依赖 `db`/`redis` 的 `service_healthy`，`frontend` 依赖 `backend` 健康。
- Nginx：`location /api/ { proxy_pass http://backend:8000/; }` + SPA 回落；`/api` 前缀由 Nginx 剥掉，后端同时挂载两套路由。
- 收尾：`docker compose down -v --remove-orphans`。

## License

内部演示项目，仅用于评测与教学。
