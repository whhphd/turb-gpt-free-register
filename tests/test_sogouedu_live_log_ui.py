from pathlib import Path
import unittest


class SogouRestockLiveLogUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = (
            Path(__file__).resolve().parents[1] / "webui" / "templates" / "index.html"
        ).read_text(encoding="utf-8")

    def test_log_panel_is_visible_and_named(self) -> None:
        self.assertIn('id="sogouRestockLogTitleV2">最近日志', self.source)
        self.assertIn('id="sogouRestockLogsV2" role="log"', self.source)
        self.assertIn("暂无自动补池日志", self.source)

    def test_polling_runs_every_two_seconds_without_overlap(self) -> None:
        self.assertIn("const SOGOU_RESTOCK_LOG_INTERVAL_MS = 2000;", self.source)
        self.assertIn("if (SOGOU_RESTOCK_LOG_LOADING) return;", self.source)
        self.assertIn("logs?lines=100", self.source)
        self.assertIn("setInterval(loadSogouRestockLogs, SOGOU_RESTOCK_LOG_INTERVAL_MS)", self.source)

    def test_polling_tracks_page_visibility(self) -> None:
        self.assertIn("document.visibilityState === 'visible'", self.source)
        self.assertGreaterEqual(self.source.count("syncSogouRestockLogPolling();"), 2)
        self.assertIn("document.addEventListener('visibilitychange', syncSogouRestockLogPolling)", self.source)
        self.assertIn("window.addEventListener('pagehide', stopSogouRestockLogPolling)", self.source)

    def test_log_formatter_exposes_partial_settlement_and_countdown(self) -> None:
        self.assertIn("partial_finalized: '部分结算完成'", self.source)
        self.assertIn("function formatSogouRestockDuration(seconds)", self.source)
        self.assertIn("部分备货已等 ${formatSogouRestockDuration(elapsed)}", self.source)
        self.assertIn("距超时处理 ${formatSogouRestockDuration(Math.max(0, 60 - elapsed))}", self.source)
        self.assertIn("已预留 ${row.reserved}", self.source)

    def test_log_formatter_exposes_push_and_recovery_totals(self) -> None:
        self.assertIn("推池成功 ${result.success || 0} / 失败 ${result.failed || 0}", self.source)
        self.assertIn("补发原位修复 ${recovery.repaired || 0} / 新建 ${recovery.recreated || 0}", self.source)

    def test_multi_provider_controls_and_fallback_logs_are_visible(self) -> None:
        self.assertIn('id="restockProviderPriorityV2"', self.source)
        self.assertIn('id="bugteamRestockProductV2"', self.source)
        self.assertIn('id="restockPartialRetryLimitV2"', self.source)
        self.assertIn("provider_retry_scheduled: '同供应商重试'", self.source)
        self.assertIn("provider_fallback_scheduled: '切换兜底供应商'", self.source)

    def test_partial_delivery_and_timeout_logs_are_human_readable(self) -> None:
        self.assertIn("partial_delivery_retry: '部分取货待重试'", self.source)
        self.assertIn("partial_cancel_retry: '取消剩余待重试'", self.source)
        self.assertIn("部分交付 ${delivered}/${total}", self.source)
        self.assertIn("推池成功 ${delivered} / 失败 0", self.source)
        self.assertIn("剩余 ${remaining} 已取消并继续补单", self.source)
        self.assertIn("等待库存满 60 秒", self.source)
        self.assertIn("原订单已取消", self.source)

    def test_status_polling_does_not_overwrite_unsaved_config(self) -> None:
        self.assertIn("let SOGOU_RESTOCK_CONFIG_DIRTY = false;", self.source)
        self.assertIn("if (refreshConfig && !SOGOU_RESTOCK_CONFIG_DIRTY)", self.source)
        self.assertIn("renderSogouRestockStatus(status, { refreshConfig: false });", self.source)
        self.assertIn("el.addEventListener('input', markSogouRestockConfigDirty);", self.source)
        self.assertIn("SOGOU_RESTOCK_CONFIG_DIRTY = false;", self.source)

    def test_order_table_polls_without_touching_config(self) -> None:
        self.assertIn("const SOGOU_RESTOCK_ORDERS_INTERVAL_MS = 5000;", self.source)
        self.assertIn("if (SOGOU_RESTOCK_ORDERS_LOADING || !isSogouRestockLogVisible()) return;", self.source)
        self.assertIn("const orders = await api('/api/pool-admin/sogou-restock/orders?limit=30');", self.source)
        self.assertIn("renderSogouRestockOrders(orders.items || []);", self.source)
        self.assertIn("setInterval(loadSogouRestockOrders, SOGOU_RESTOCK_ORDERS_INTERVAL_MS)", self.source)
        self.assertIn("window.addEventListener('pagehide', stopSogouRestockOrdersPolling)", self.source)


if __name__ == "__main__":
    unittest.main()
