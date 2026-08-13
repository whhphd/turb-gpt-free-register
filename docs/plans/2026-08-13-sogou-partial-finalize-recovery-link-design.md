# Sogou 部分结算与补发关联设计

## 目标

1. Sogou API 提货订单进入 `ready_partial` 后不再无限等待整单备货；部分备货持续 5 分钟后自动结算当前已预留账号。
2. Sogou 补发记录优先关联并更新原 Sub2API 账号，避免为同一邮箱创建重复账号。
3. 已经产生的历史重复账号保持现状，只修复今后的订单和补发。

## 部分结算

- 首次观察到 `ready_partial` 且 `reserved > 0` 时，在当前订单状态中记录 `partial_ready_since`。
- 状态暂时回到 `waiting_inventory` 时保留计时，只要订单仍有预留账号；预留数归零、订单失败或完成时清除计时。
- 满 5 分钟后，只有再次观察到 `ready_partial` 且 `reserved > 0` 才调用
  `POST /api/customer/manual/orders/{order_id}/finalize`。
- `finalize` 成功后不直接解析交付文件，仍由既有 `completed -> take -> push` 流程取货和推池，避免形成第二套交付逻辑。
- 网络或供应商错误保留当前订单，下一轮重试；不会创建新订单，也不会调用取消接口。

## 补发关联

- 补发记录缺少邮箱时，根据 `source_order_id` 查询对应订单详情。
- 从订单 `items` 建立两类索引：`recovery_id -> email` 和 `inventory_account_id/inventory_id -> email`。
- 优先按 `recovery_id` 关联，其次按 `inventory_id`；领取补发文件后，再用文件中的邮箱做二次校验和兜底。
- 匹配到 `import_source=sogouedu_auto_restock` 的原账号时，更新原账号凭据、恢复 `status=active` 并清空错误。
- 确实找不到原账号时，继续遵循既定规则新建账号。
- 不处理或删除 `68525`、`68618` 已产生的历史重复账号。

## 测试

- `ready_partial` 未满 5 分钟不得调用 `finalize`。
- 状态在 `ready_partial` 与 `waiting_inventory` 间变化时计时不重置。
- 满 5 分钟且再次 `ready_partial` 时只结算一次，随后沿既有取货流程推池。
- 订单详情能通过 `recovery_id` 和 `inventory_id` 两种方式补齐邮箱。
- 补发匹配到原账号时更新凭据，不调用新建推池。
- 补发无法匹配时仍按原规则新建。

## 实施拆分

1. 扩展 Sogou 客户端和补池编排状态，完成部分结算及单元测试。
2. 扩展补发关联索引和补发单元测试。
3. 运行全量测试，提交并部署服务器；通过定时巡检观察真实订单。
