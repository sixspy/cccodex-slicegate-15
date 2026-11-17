# 日切放行

> 遵守 `ops/底座生成前必读_效率与技术栈.md`。FastAPI + Vue 3 + SQLite，交付须 docker compose 一键起；库用 SQLite，不挂外置 Postgres。  
> **禁止同构换皮**：本仓主链路、表结构、接口与页面均独立，不得与其它仓共用同一套 `/api/items` 壳。

## 骨架一句话

业务日分区切片登记后裁决完整度，完整才放行下游，并与消费水位对账

## 放行成功通知投递闭环（口径：甲 · 同事务落账）

- **口径选定「甲」**：成功放行、推水位、落待投递通知在**同一数据库事务**落账，任一步失败整笔回滚；已写入通知策略簿（`notification_policy` 单例表），**当天不许换**，两边不得混通。偏离策略簿的请求一律拒绝并写明 `通知口径偏了`（`PUT /api/notifications/policy`）。
- **禁止回报整单成功却无通知记录**：通知账与放行同生同灭；历史成功放行启动时补登为待投递。
- **通知值班投递**：`POST /api/notifications/{id}/dispatch`。两名值班同时发起时写锁排队互斥，后手得 `通知争用禁止默成`（409），禁止默成。
- **禁止重复投递冒充一次成功**：一次放行只落一条通知账（`notifications.release_id` 唯一），成功投递账全表至多一笔（`notification_deliveries` 部分唯一索引）。
- **失败可重试**：投递失败记失败账、状态 `投递失败`，绝不把未投递标成已通知；重投直至成功。`simulate_failure` 为演练开关。
- **四方可核对**：`GET /api/notifications`（队列+策略旁注）、`GET /api/releases`（放行详情带通知账）、`GET /api/watermarks`（水位）、`GET /api/reconcile`（账实对账，异常即亮）。
- 文件库本体沿用 `data/app.db`（启动时原地建表迁移），站点登录沿用。

## 基本信息

| 项 | 值 |
|----|-----|
| 产品名（首页） | 日切放行 |
| 目录 | `04-slicegate` |
| 领域建议 | 数仓与数据湖 |
| 接口 | http://127.0.0.1:10625 |
| 库 | `data/app.db` |
| 账号 | admin / 123456 |

## 为何能出多题

迟到切片、重切、跨分区依赖、部分完整冻结、消费水位回拨

## 避开

别做成作业拓扑编排换皮；别做成纯水位大盘

## 互斥切口候选

- 迟到切片整批拒收 vs 进待补齐冻结区
- 重切失败整笔回滚 vs 保留已完整分区
- 放行与水位同步闸 vs 异步放行后纠偏

## 启动（一键）

`powershell
cd d:\document\cccodex\workspace\slicegate\slicegate-15
docker compose up -d --build
`

浏览器：http://127.0.0.1:10625/  
健康检查：GET http://127.0.0.1:10625/api/health

本机开发（可选）：

`powershell
python -m pip install -r requirements.txt
python -m uvicorn app:app --app-dir backend --host 127.0.0.1 --port 10625
python -m pytest -q
`
