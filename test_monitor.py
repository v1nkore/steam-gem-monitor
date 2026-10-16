import io
import unittest
import urllib.error
from unittest.mock import patch
import monitor as m


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.target = m.Target("https://steamcommunity.com/market/listings/570/Unusual%20Northern%20Blight%20Style")

    def test_rendered_gem_and_escaped_markup(self):
        for text in ("<span>Frostbloom</span>", r"\u003cspan\u003eFrostbloom\u003c/span\u003e", r"\\u003cspan\\u003eFrostbloom\\u003c/span\\u003e"):
            self.assertTrue(self.target.matches(text.lower()))
        self.assertFalse(self.target.matches("''unusual effect gem frostbloom''"))
        self.assertFalse(self.target.matches("<span>frostbloom fake</span>"))

    def test_rate_limit_stops_without_retries(self):
        error = urllib.error.HTTPError("https://steamcommunity.com", 429, "Too Many Requests", {}, io.BytesIO())
        with patch.object(m.urllib.request, "urlopen", side_effect=error) as request, patch.object(m.time, "sleep") as sleep:
            with self.assertRaises(m.RateLimitError):
                m.http_get("https://steamcommunity.com")
            self.assertEqual(request.call_count, 1)
            sleep.assert_not_called()

    def test_hourly_gate_avoids_requests(self):
        with patch.object(m, "load_state", return_value={"last_scan": 9900}), patch.object(m.time, "time", return_value=10000), patch.object(m, "scan_target") as scan:
            m._run()
            scan.assert_not_called()

    def test_rate_limit_preserves_other_targets(self):
        state = {"targets": {"other": {"seen": ["existing"]}}}
        targets = [self.target, m.Target("https://steamcommunity.com/market/listings/570/Onibi")]
        with patch.object(m, "load_targets", return_value=targets), patch.object(m, "load_state", return_value=state), patch.object(m, "save_state"), patch.object(m, "DISPLAY_RUB", False), patch.object(m, "scan_target", side_effect=m.RateLimitError(7200)) as scan:
            m._run()
            self.assertEqual(scan.call_count, 1)
            self.assertEqual(state["targets"]["other"]["seen"], ["existing"])
            self.assertGreater(state["steam_retry_after"], state["last_scan"] + 7199)

    def test_incomplete_scan_is_error(self):
        with patch.object(m, "fetch_page", side_effect=[(40, [("1", "")], 1), (None, [], None)]), patch.object(m.time, "sleep"):
            with self.assertRaises(m.MonitorError):
                m.scan_target(self.target)

    def test_empty_market_is_valid(self):
        with patch.object(m, "fetch_page", return_value=(0, [], 1)):
            self.assertEqual(m.scan_target(self.target)[:3], ({}, None, 0))


if __name__ == "__main__":
    unittest.main()
