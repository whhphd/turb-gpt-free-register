# -*- coding: utf-8 -*-
import unittest

from core.chatgpt_plan import _retryable_plan_error


class PlanCheckRetryTests(unittest.TestCase):
    def test_cf_403_is_retryable(self):
        self.assertTrue(_retryable_plan_error(403))
        self.assertTrue(_retryable_plan_error(429))
        self.assertTrue(_retryable_plan_error(503))

    def test_token_401_is_not_retryable(self):
        self.assertFalse(_retryable_plan_error(401))


if __name__ == "__main__":
    unittest.main()
