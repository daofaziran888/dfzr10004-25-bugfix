# 智能采集质量管控平台

面向数据采集团队的质量管控与效率优化平台。覆盖采集任务编排、抓取结果校验、采集链路监控、数据格式转换、去重比对、异常重试、采集审计与合规管理的完整采集质控链路。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

后端回归测试：

```bash
cd backend
.venv/bin/python -m unittest discover -s tests -v
```

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 采集任务编排 | `taskplan` | 采集计划 | 计划编号、目标站点、采集频率 |
| 采集结果校验 | `verify` | 校验规则 | 规则编号、校验对象、校验类型 |
| 采集链路监控 | `pipeline` | 链路节点 | 节点编号、节点名称、上游节点 |
| 采集格式转换 | `transform` | 转换模板 | 模板编号、输入格式、输出格式 |
| 采集去重比对 | `dedup` | 去重策略 | 策略编号、业务键列、排序字段 |
| 异常重试管理 | `retry` | 重试规则 | 规则编号、异常类型、重试上限 |
| 采集审计日志 | `audit` | 审计记录 | 记录编号、操作类型、采集计划 |
| 采集合规配置 | `compliance` | 合规规则 | 规则编号、站点域名、协议标识 |
| 代理池管理 | `proxy` | 代理节点 | 节点编号、代理地址、代理类型 |
| 采集存储策略 | `storage` | 存储配置 | 配置编号、存储路径、文件格式 |
| 采集告警中心 | `alert` | 告警规则 | 规则编号、监控指标、告警级别 |
| 采集统计报表 | `report` | 统计报表 | 报表编号、统计维度、统计周期 |
| 采集频率控制 | `throttle` | 频率策略 | 策略编号、目标站点、请求间隔 |
| 采集数据标注 | `tag` | 标注记录 | 标注编号、数据记录、标注类型 |
| 采集配置版本 | `version` | 配置版本 | 版本编号、配置名称、版本号 |
| 采集沙箱测试 | `sandbox` | 沙箱实例 | 实例编号、测试脚本、目标站点 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。
- 仓库读接口 `store.find` 返回行副本，服务层写流转用 `store.find_live`，
  避免外部通过查询结果意外改写仓库内部状态。

### 采集频率控制（throttle）与采集任务编排（taskplan）的联动口径

- 频率策略状态机单向流转：`待配置 → 候选 → 生效 → 停用`，重复到达的动作命令幂等，只推进一次。
- 保存（登记 `POST` / 修改 `PUT`）前统一校验，不符合条件不允许保存或启用：
  - 目标站点：必须在 `app/catalog.py` 站点目录内（去首尾空白后精确匹配）；
  - 请求间隔：100~60000 毫秒整数，且不低于站点最小间隔；
  - 日请求上限：1~100000 正整数，且不得超过站点日硬配额（额度冲突口径）；
  - 并发数量：1~50 正整数，且不得超过站点并发硬配额；
  - 优先级权重：1~100 的正整数。
- 同站点同时只允许一条「生效」策略。启用冲突时拒绝并在消息里指出现有策略编号；
  显式携带 `replace_policy=true` 时以本次候选为准，旧生效策略自动转停用。
- 策略停用（含被替换隐式停用、修改生效策略导致退回待配置）后，引用其额度的
  采集计划自动进入「待重新排队」，旧额度快照作废；排队/重新排队只按
  `active_policy_for_site` 解析当前唯一生效策略，站点无生效策略时拒绝排队。
- 待重新排队清单接口：`GET /api/taskplan/requeue-queue`；站点配额目录：
  `GET /api/throttle/sites`。

领域：数据工程
子方向：数据采集、爬虫与RPA
