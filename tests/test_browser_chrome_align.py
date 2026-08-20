# -*- coding: utf-8 -*-
import unittest

from config import browser as browser_cfg


class BrowserChromeAlignTests(unittest.TestCase):
    def test_http_profile_matches_curl_cffi_impersonate(self):
        self.assertEqual(browser_cfg.IMPERSONATE, "chrome146")
        self.assertEqual(browser_cfg.CHROME_MAJOR, "146")
        self.assertEqual(browser_cfg.CHROME_FULL_VERSION, "146.0.0.0")
        self.assertIn("Chrome/146.0.0.0", browser_cfg.USER_AGENT)
        self.assertIn('v="146"', browser_cfg.SEC_CH_UA)
        self.assertIn("146.0.0.0", browser_cfg.SEC_CH_UA_FULL_VERSION_LIST)
        self.assertNotIn("149", browser_cfg.CHROME_FULL_VERSION)
        self.assertNotIn('v="149"', browser_cfg.SEC_CH_UA)


if __name__ == "__main__":
    unittest.main()
