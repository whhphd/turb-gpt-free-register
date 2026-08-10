# SogouEdu 自动补池设计

日期：2026-08-10

## 1. 目标与边界

在“号池管理”中增加独立的“自动补池”业务流，使用 `https://sogouedu.cc` 客户 API 购买 OAuth 账号，并将账号推入指定的 sub2api 分组。

本业务流只负责 SogouEdu 购入账号的订单、推池和 401 恢复。它不处理本地注册账号或其他来源的账号修复、删除和本地 Codex 重授权。

库存触发统计目标分组内全部 `openai/oauth` 账号，因此手动从其他来源补入的健康账号也会占用库存。只有带有 `import_source=sogouedu_auto_restock` 标记的账号进入 SogouEdu 恢复流程。

## 2. 已确认规则

- 监测分组和推入分组可配置，默认可使用同一分组。
- 商品为单选，可配置 `oauth_7d` 或 `oauth_30d`，不自动混买。
- 健康库存只统计 `status=active` 且 `schedulable=true` 的账号；排除 `error`、`inactive`、不可调度和过期账号。
- 不检查 `refresh_token` 字段；号池 OAuth 账号默认具备 RT。
- 健康数量低于最低保有量时立即下单。
- 购买数量为 `min(目标保有量 - 健康数量, 单轮最大购买数)`。
- 不设置连续扫描防抖、冷却时间或每日购买上限。
- 同时只允许一个未完成订单；通过持久化幂等键防止重复扣款。
- 每轮先处理恢复记录和未完成订单，再扫描库存和创建新订单。
- 恢复成功但原账号已从 sub2api 删除时，按当前导入配置新建账号入池。

## 3. 架构

### 3.1 供应商客户端

新增 `SogouEduClient`，封装：

- `POST /api/customer/login`
- `GET /api/customer/inventory`
- `POST /api/customer/pickup/orders`
- `GET /api/customer/pickup/orders/<order_id>`
- `POST /api/customer/pickup/orders/<order_id>/take`
- `GET /api/customer/recoveries`
- `GET /api/customer/recoveries/<recovery_id>`
- `POST claim_url`
- `GET /api/customer/balance`

客户端缓存 Token。供应商 API 返回 401 时重新登录并只重试当前请求一次；429 遵循 `Retry-After`。网络错误只允许有限的请求级重试，创建订单必须复用持久化的 `Idempotency-Key`。

### 3.2 补池编排服务

新增 `SogouRestockService`，独立于现有 `sub2api_pool_monitor` 的本地账号自动巡检，负责：

1. 获取任务锁。
2. 轮询恢复记录并领取新版凭据。
3. 继续处理持久化的未完成订单和待推账号。
4. 拉取目标分组全部 OpenAI OAuth 账号并统计健康数量。
5. 计算缺口、查询库存/余额、创建订单。
6. 轮询订单、取货、持久化交付文件并推入 sub2api。
7. 写入脱敏日志、运行记录和下次运行时间。

现有 sub2api 查询、账号批量推送、调度开关恢复逻辑尽量复用；推池时需支持附加 Sogou 来源标记、订单关联字段和模型白名单。

### 3.3 持久化

非敏感业务配置和运行状态保存在独立目录 `data/sogou_restock/`，至少包含：

- `config.json`：开关、分组、商品、阈值、轮询间隔、推池调度参数、模型白名单。
- `state.json`：当前任务、幂等键、订单 ID、订单状态、下次运行时间。
- `orders/<order_id>.json`：订单快照、状态历史、账号交付和推池结果。
- `recoveries/<recovery_id>.json`：恢复状态、凭据版本、认领结果和关联 pool ID。
- `runs/<run_id>.json` 与 `auto.log`：脱敏运行明细。

Sogou 用户名、密码写入 `.env`。配置 GET 接口只返回是否已配置，不返回原值；空密码提交表示保持原值。

## 4. 单轮数据流

1. 加载配置并获取进程级任务锁。
2. 查询 Sogou recoveries，筛选本客户的可恢复记录；`claimable` 时领取新版文件。
3. 按保存的 Sogou 订单/账号标识和邮箱查找原 pool ID。
4. 原账号存在时只合并 OAuth 凭据、有效期和凭据版本，保留分组、并发、优先级、负载、倍率、模型映射及来源标记。
5. 原账号不存在时按当前导入配置新建，并写入 `recreated=true` 与 `replacement_of_pool_id`。
6. 如果存在未完成订单，查询同一订单；202 按 `retry_after_seconds` 等待，200 后先持久化 `payload.accounts` 再推池。
7. 待推账号失败时保留原始凭据和推池状态，下轮只重试推池，不重新购买。
8. 无未完成订单后扫描分组，健康数不足时计算购买数。
9. 查询库存和余额，保存幂等键后创建订单；现货不足但供应商允许异步备货时仍按缺口下单。
10. 保存订单 URL、状态和快照，后续轮次继续跟进。

每个新账号写入类似以下 `extra`：

```json
{
  "import_source": "sogouedu_auto_restock",
  "sogou_order_id": "...",
  "sogou_item_id": "...",
  "sogou_recovery_id": "...",
  "credential_version": "..."
}
```

## 5. 推池配置

自动补池页面可编辑：

- 监测分组 ID、推入分组 ID；
- 商品；
- 最低保有量、目标保有量、单轮最大购买数；
- 监测间隔、订单轮询间隔、恢复轮询间隔；
- 账号 `concurrency`、`priority`、`load_factor`、`rate_multiplier`；
- `auto_pause_on_expired`；
- 模型白名单。

模型白名单使用 sub2api 的 `credentials.model_mapping`：每个模型写成 `{model: model}`；空列表写空对象，表示允许全部模型。订单创建时保存导入配置快照，避免用户在订单处理中修改配置导致同一订单的账号参数不一致。

## 6. 异常与安全

- 供应商 401：重新登录一次；仍失败则本轮标记认证错误。
- 429：按 Retry-After 等待，不并发创建订单。
- 402：标记余额不足并停止本轮下单。
- 订单 202：持续查询原订单；不得重复创建。
- 订单取消或业务失败：记录原因，下一轮按实际缺口重新判断。
- 取货 500：保留票据和原始订单，不丢弃待领取状态。
- recovery 409：重新读取恢复记录取得新 claim URL。
- 单账号推池失败：保存待推状态，下轮重试。
- 原账号不存在：自动新建，不静默丢弃恢复结果。
- 日志、状态 API 和前端不输出密码、Token 或完整账号凭据。
- 使用持久化订单锁、幂等键和原子状态写入，服务重启后可恢复。

## 7. 页面

号池管理增加“自动补池”子页，包含：

- 开关、连接状态、立即执行、刷新状态；
- 总账号数、健康数、错误数、不可调度数、预计购买数；
- Sogou 余额、库存和当前订单进度；
- 补池规则配置；
- 推池调度和模型白名单配置；
- 用户名/密码输入框只用于覆盖，不回显已保存值；
- 最近订单、恢复记录、运行日志和脱敏错误。

现有“号池巡视”保持原有本地账号逻辑，不被 Sogou 自动补池调用。

## 8. 测试与验收

### 8.1 客户端单元测试

- 登录、Token 缓存、401 重登、429 Retry-After；
- inventory、订单幂等、202/200 轮询、take；
- recoveries 分页、claimable 领取、409/500；
- 敏感字段不进入日志和返回对象。

### 8.2 编排服务测试

- 全来源库存统计和健康口径；
- 缺口数量及单轮上限；
- 未完成订单阻止重复下单；
- 重启恢复订单和待推账号；
- Sogou-only recovery；
- 原账号更新与孤立恢复自动新建；
- 模型映射、分组、并发等参数覆盖；
- 推池失败重试且不重复购买。

### 8.3 Web API 与验收

- 配置保存、字段校验、敏感字段不回显；
- 状态、订单、日志和立即执行接口；
- 未授权访问拦截；
- 使用真实小数量完成登录、下单、取货、推池；
- 使用模拟数据完成一次 401 恢复和孤立恢复重建。

