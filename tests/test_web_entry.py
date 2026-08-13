import unittest
from unittest.mock import patch

import web


class WebEntryTests(unittest.TestCase):
    def test_start_background_workers_starts_sogou_restock(self):
        with patch("core.sogouedu_restock.ensure_restock_monitor_started") as started:
            web._start_background_workers()

        started.assert_called_once_with()

    def test_start_background_workers_does_not_crash_webui(self):
        with patch(
            "core.sogouedu_restock.ensure_restock_monitor_started",
            side_effect=RuntimeError("boom"),
        ), patch.object(web.logging.getLogger(web.__name__), "exception") as logged:
            web._start_background_workers()

        logged.assert_called_once()


if __name__ == "__main__":
    unittest.main()
