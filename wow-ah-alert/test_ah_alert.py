"""離線測試：用假的 Blizzard API 驗證價格計算和提醒規則。執行：python -m unittest test_ah_alert"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ah_alert


def auction(item_id, unit_price, quantity):
    return {"item": {"id": item_id}, "unit_price": unit_price, "quantity": quantity}


class FakeApi:
    def __init__(self):
        self.auctions = []
        self.modified = "t0"

    def commodities(self, since):
        return None if since == self.modified else (self.modified, self.auctions)

    def search_items(self, cls, sub):
        return {1: "絲綢", 2: "亞麻布"}

    def item_name(self, iid):
        return f"item{iid}"


class AlertTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg_path = Path(self.tmp.name) / "config.toml"
        cfg_path.write_text("""
[battlenet]
client_id = "x"
client_secret = "y"
[watch]
item_class = 7
item_subclass = 5
buy_quantity = 10
[alert]
hourly_change_pct = 15
cooldown_hours = 3
min_quantity = 0
[[target]]
item_id = 2
below = 1
""")
        self.cfg = ah_alert.load_config(cfg_path)
        self.cfg.db_path = Path(self.tmp.name) / "db.sqlite3"
        self.store = ah_alert.Store(self.cfg.db_path)
        self.api = FakeApi()
        self.sent = []
        patcher = mock.patch.object(ah_alert, "notify", lambda cfg, text: self.sent.append(text))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def run_at(self, ts, modified, auctions):
        self.api.modified, self.api.auctions = modified, auctions
        with mock.patch.object(ah_alert.time, "time", return_value=ts):
            return ah_alert.run_once(self.cfg, self.api, self.store)

    def test_summarize_buy_price(self):
        prices = ah_alert.summarize(
            [auction(1, 100, 4), auction(1, 200, 100), auction(9, 1, 1)], {1}, 10)
        self.assertEqual(prices[1].min_price, 100)
        self.assertEqual(prices[1].buy_price, (4 * 100 + 6 * 200) // 10)
        self.assertEqual(prices[1].quantity, 104)
        self.assertNotIn(9, prices)

    def test_hourly_spike_alert_and_cooldown(self):
        base = [auction(1, 10000, 500), auction(2, 50000, 500)]
        self.assertTrue(self.run_at(0, "a", base))
        self.assertEqual(self.sent, [])

        # 資料沒更新 → 不處理
        self.assertFalse(self.run_at(600, "a", base))

        # 絲綢漲 50%
        self.run_at(3600, "b", [auction(1, 15000, 500), auction(2, 50000, 500)])
        self.assertEqual(len(self.sent), 1)
        self.assertIn("絲綢", self.sent[0])
        self.assertIn("漲 50.0%", self.sent[0])
        self.assertNotIn("亞麻布", self.sent[0])

        # 再漲一次，但還在冷卻時間內 → 不重複提醒
        self.run_at(7200, "c", [auction(1, 22500, 500), auction(2, 50000, 500)])
        self.assertEqual(len(self.sent), 1)

    def test_target_below(self):
        self.run_at(0, "a", [auction(2, 9000, 500)])  # 0.9g < 目標 1g
        self.assertEqual(len(self.sent), 1)
        self.assertIn("跌破目標 1g", self.sent[0])

    def test_fmt_gold(self):
        self.assertEqual(ah_alert.fmt_gold(1234567), "123g 45s")
        self.assertEqual(ah_alert.fmt_gold(54321), "5g 43s 21c")
        self.assertEqual(ah_alert.fmt_gold(321), "3s 21c")


if __name__ == "__main__":
    unittest.main()
