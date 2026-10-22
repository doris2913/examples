"""離線測試：用假的 Undermine API 驗證價格計算和提醒規則。執行：python -m unittest test_ah_alert"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ah_alert

HOUR = 3600
META = {
    1: {"class": 7, "subclass": 5, "expansion": 12, "name": "絲綢 ★1"},
    2: {"class": 7, "subclass": 5, "expansion": 12, "name": "亞麻布"},
    3: {"class": 7, "subclass": 5, "expansion": 1, "name": "舊布"},
    4: {"class": 7, "subclass": 9, "expansion": 12, "name": "草藥"},
}


class FakeApi:
    def __init__(self):
        self.snapshot = 0
        self.items = {}       # item_id -> 掛單 [{price, quantity}]
        self.history = {}     # item_id -> hourly 回應
        self.calls = []

    def commodities(self):
        self.calls.append("commodities")
        return self.snapshot, {
            i: {"item": i, "price": min(a["price"] for a in aucs), "quantity": sum(a["quantity"] for a in aucs)}
            for i, aucs in self.items.items()}

    def auctions(self, iid):
        self.calls.append(f"now:{iid}")
        return self.items[iid]

    def hourly(self, iid):
        self.calls.append(f"hourly:{iid}")
        return self.history.get(iid, [])


def iso(ts):
    return ah_alert.datetime.fromtimestamp(ts, ah_alert.datetime.now().astimezone().tzinfo).isoformat()


class AlertTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg_path = Path(self.tmp.name) / "config.toml"
        cfg_path.write_text("""
[undermine]
api_key = "k"
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
        self.store = ah_alert.Store(Path(self.tmp.name) / "db.sqlite3")
        self.api = FakeApi()
        self.sent = []
        for target, value in [("notify", lambda cfg, text: self.sent.append(text)),
                              ("fetch_item_metadata", lambda locale: META)]:
            patcher = mock.patch.object(ah_alert, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def run_at(self, ts, items):
        self.api.snapshot, self.api.items = ts, items
        return ah_alert.run_once(self.cfg, self.api, self.store)

    def test_tracks_latest_expansion_of_category(self):
        names = ah_alert.tracked_items(self.cfg, self.store, {1: {}, 2: {}, 3: {}, 4: {}})
        self.assertEqual(names, {1: "絲綢 ★1", 2: "亞麻布"})

    def test_buy_price(self):
        aucs = [{"price": 200, "quantity": 100}, {"price": 100, "quantity": 4}]
        self.assertEqual(ah_alert.buy_price(aucs, 10), (4 * 100 + 6 * 200) // 10)
        self.assertEqual(ah_alert.buy_price(aucs, 1000), round((400 + 20000) / 104))
        self.assertIsNone(ah_alert.buy_price([], 10))

    def test_hourly_spike_alert_and_cooldown(self):
        base = {1: [{"price": 10000, "quantity": 500}], 2: [{"price": 50000, "quantity": 500}]}
        self.assertTrue(self.run_at(0, base))
        self.assertEqual(self.sent, [])

        # 同一個快照 → 不處理，也不再花點數查單一物品
        self.api.calls.clear()
        self.assertFalse(self.run_at(0, base))
        self.assertEqual(self.api.calls, ["commodities"])

        # 絲綢漲 50%
        self.run_at(HOUR, {1: [{"price": 15000, "quantity": 500}], 2: base[2]})
        self.assertEqual(len(self.sent), 1)
        self.assertIn("絲綢 ★1", self.sent[0])
        self.assertIn("漲 50.0%", self.sent[0])
        self.assertNotIn("亞麻布", self.sent[0])

        # 再漲一次，但還在冷卻時間內 → 不重複提醒
        self.run_at(2 * HOUR, {1: [{"price": 22500, "quantity": 500}], 2: base[2]})
        self.assertEqual(len(self.sent), 1)

    def test_seeded_history_enables_baseline_alert(self):
        now = 100 * HOUR
        # 過去 24 小時穩定在 1g，最後一小時 1.1g（只漲 10%，不觸發每小時提醒）
        self.api.history[1] = [{"snapshot": iso(now - h * HOUR), "price": 10000, "quantity": 500}
                               for h in range(24, 1, -1)]
        self.api.history[1].append({"snapshot": iso(now - HOUR), "price": 11000, "quantity": 500})
        self.run_at(now, {1: [{"price": 12500, "quantity": 500}]})
        self.assertIn("hourly:1", self.api.calls)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("比 24h 中位數 1g 00s 00c 高 25.0%", self.sent[0])
        self.assertNotIn("比上次", self.sent[0])

    def test_target_below(self):
        self.run_at(0, {2: [{"price": 9000, "quantity": 500}]})  # 0.9g < 目標 1g
        self.assertEqual(len(self.sent), 1)
        self.assertIn("跌破目標 1g", self.sent[0])

    def test_fmt_gold(self):
        self.assertEqual(ah_alert.fmt_gold(1234567), "123g 45s")
        self.assertEqual(ah_alert.fmt_gold(54321), "5g 43s 21c")
        self.assertEqual(ah_alert.fmt_gold(321), "3s 21c")


if __name__ == "__main__":
    unittest.main()
