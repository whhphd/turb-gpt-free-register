# Sogou 补发模型白名单与 Worker 启动设计

## 目标

1. 后续 Sogou 补发原位更新账号时保留当前自动补池配置中的模型白名单。
2. WebUI 进程启动时立即启动自动补池 worker，不再依赖用户打开补池页面。
3. 停用服务器独立审计定时器，保留只读审计脚本供手动排查。

现有 10 个已丢失模型白名单的账号不做批量回填。

## 补发凭据

补发领取成功并匹配到原账号后，构造 Sub2API 账号凭据时传入当前 `model_whitelist`。这样新凭据中的 `model_mapping` 与首次推池使用相同配置，再通过原有账号更新接口覆盖凭据。

新增回归测试，断言补发原位更新提交的 credentials 包含配置的完整 `model_mapping`。

## Worker 生命周期

在 Flask `create_app()` 完成路由和后台服务初始化时调用 `ensure_restock_monitor_started()`。保留状态接口中的幂等调用，作为线程意外退出后的兜底自愈。

新增 WebUI 测试，断言创建应用时会启动 Sogou 自动补池 worker。

## 审计定时器

服务器执行 `systemctl disable --now turb-gpt-sogou-audit.timer`，不删除 timer/service unit 和审计脚本。需要排查时仍可手动执行 `systemctl start turb-gpt-sogou-audit.service` 或直接运行脚本。
