# 虫巢工坊 · 分布式爬虫调度与反爬对抗平台

面向大规模网页采集的分布式爬虫调度、代理池管理、反爬策略对抗、内容抽取清洗与采集结果交付平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
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

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 采集站点 | `crawl_site` | 目标站点 | 站点编号、站点域名、站点类型 |
| 代理池 | `proxy_pool` | 代理节点 | 节点编号、代理地址、代理类型 |
| 反爬对抗 | `anti_block` | 对抗策略 | 策略编号、目标站点、对抗类型 |
| 采集任务 | `crawl_job` | 采集作业 | 作业编号、目标站点、入口URL |
| URL去重 | `url_dedup` | 去重指纹 | 指纹编号、源URL、指纹算法 |
| 内容抽取 | `content_extract` | 抽取规则 | 规则编号、目标站点、XPath选择器 |
| 字段清洗 | `field_clean` | 清洗规则 | 规则编号、目标字段、清洗类型 |
| 批次存储 | `batch_store` | 采集批次 | 批次编号、来源作业、数据行数 |
| 增量采集 | `incr_crawl` | 增量标记 | 标记编号、目标站点、增量字段 |
| 解析日志 | `parse_log` | 解析记录 | 记录编号、来源URL、解析规则 |
| 数据投递 | `data_delivery` | 投递任务 | 任务编号、数据来源、目标系统 |
| 质量抽样 | `quality_sample` | 抽样计划 | 计划编号、目标批次、抽样比例 |
| 站点监控 | `site_monitor` | 监控指标 | 指标编号、目标站点、监控类型 |
| 验证码识别 | `robot_verify` | 识别记录 | 记录编号、目标站点、验证码类型 |
| 采集限速 | `crawl_limit` | 限速策略 | 策略编号、目标站点、请求间隔 |
| 模板库 | `template_lib` | 采集模板 | 模板编号、站点类型、模板名称 |
| 调度编排 | `scheduler` | 调度计划 | 计划编号、关联作业、Cron表达式 |
| 采集计费 | `crawl_cost` | 用量记录 | 记录编号、采集站点、计费周期 |
| 数据归档 | `data_archive` | 归档策略 | 策略编号、数据范围、归档周期 |
| 差异检测 | `diff_check` | 差异记录 | 记录编号、目标站点、检测页面 |
| 导出任务 | `export_task` | 导出记录 | 记录编号、数据来源、导出格式 |
| 操作审计 | `audit_log` | 审计记录 | 记录编号、操作模块、操作人 |

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 字段校验回执与批次版本

清洗与存储之间用「字段校验回执」衔接：

- 值语义：数字 `0`、布尔 `false` 都是**合法值**，按字段类型保存（`int`/`float`/`bool`）；
  只有 `None`、空字符串、空容器才算**真实空项**（`app/models.py:is_missing`）。
- 每行回执带字段值、类型、来源（`raw:<抽取规则>` / `clean:<清洗规则>` / `correction:<来源修订>`），
  真实空项列在 `missing_fields`，原始输入保留在 `raw_fields`，可回查抽取规则。
- 批次摘要升级为 `manifest-v2/sha256`：把字段类型标签一起纳入，`0`、`"0"`、`false`、
  空字符串不会互相碰撞；摘要自包含，可脱离批次按清单独立重算。
- 历史批次仍保留旧算法摘要（`legacy_fields_digest`，16 位前缀），已确认的历史摘要可继续核对。

重验与来源更正会各自生成候选回执；确认动作在锁内做「幂等 / 基于已确认版本 / 唯一有效版本」
三重校验，同一批次只允许一个可入库版本，未选中的候选与冲突都留在历史里可查。
只有在有效回执被确认后，批次详情才给出可用于验收的摘要。

### 相关接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/field_clean/review` | 字段清洗页面复核输入，返回逐字段回执 |
| GET | `/api/field_clean/review/history` | 复核历史 |
| GET | `/api/batch_store/{批次编号}/detail` | 批次存储详情：清单 + 当前有效回执 + 历史版本/更正 |
| GET | `/api/batch_store/{批次编号}/receipts` | 回执版本与更正记录 |
| POST | `/api/batch_store/{批次编号}/revalidate` | 重验，生成候选回执 |
| POST | `/api/batch_store/{批次编号}/corrections` | 接收来源修订，生成候选回执 |
| POST | `/api/batch_store/{批次编号}/receipts/{version}/confirm` | 确认唯一可入库版本 |
| POST | `/api/batch_store/{批次编号}/verify` | 按清单独立校验摘要 |

前端入口：`/field_clean/review`（清洗规则页「输入复核」）、
`/batch_store/{批次编号}/detail`（批次列表「详情」）。

领域：数据工程
子方向：数据采集、爬虫与RPA
