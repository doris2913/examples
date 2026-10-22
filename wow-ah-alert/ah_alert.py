#!/usr/bin/env python3
"""WoW 拍賣場價格波動提醒 bot（台服商品／commodities），資料來自 Undermine Exchange API。

布料、草藥、礦石這類「商品」是全區共用的拍賣場，所以 tw-whisperwind
看到的布料價格，其實就是整個台服區的商品價格。

用法：
    python ah_alert.py --once      # 跑一次（給 cron / 工作排程器用）
    python ah_alert.py --loop      # 常駐，每隔 poll_minutes 檢查一次
    python ah_alert.py --list      # 列出目前追蹤的物品和價格
    python ah_alert.py --test-notify  # 送一則測試訊息
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import sqlite3
import statistics
import sys
import time
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
USER_AGENT = "wow-ah-alert/2.0"
API_BASE = "https://api.undermine.exchange"
# 網站前端用的靜態物品資料（API 本身不提供物品名稱和分類）
SITE_JSON = "https://undermine.exchange/json/mainline"
LOCALES = {"zh_TW": "zhtw", "en_US": "enus", "ko_KR": "kokr", "de_DE": "dede", "fr_FR": "frfr"}
METADATA_MAX_AGE = 7 * 86400


# ---------------------------------------------------------------- 設定

@dataclass
class Config:
    api_key: str
    region: str
    realm_slug: str
    locale: str
    item_class: int | None
    item_subclass: int | None
    min_expansion: int | None
    item_ids: list[int]
    buy_quantity: int
    poll_minutes: int
    hourly_change_pct: float
    baseline_change_pct: float
    baseline_hours: int
    cooldown_hours: float
    min_quantity: int
    targets: dict[int, dict]
    discord_webhook: str
    telegram_token: str
    telegram_chat_id: str
    db_path: Path


def load_config(path: Path) -> Config:
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    um = raw.get("undermine", {})
    watch = raw.get("watch", {})
    alert = raw.get("alert", {})
    notify = raw.get("notify", {})
    targets = {int(t["item_id"]): t for t in raw.get("target", [])}
    return Config(
        # 環境變數優先，避免把金鑰寫進檔案
        api_key=os.environ.get("UNDERMINE_API_KEY", um.get("api_key", "")),
        region=um.get("region", "tw"),
        realm_slug=um.get("realm_slug", "whisperwind"),
        locale=um.get("locale", "zh_TW"),
        item_class=watch.get("item_class"),
        item_subclass=watch.get("item_subclass"),
        min_expansion=watch.get("min_expansion"),
        item_ids=[int(i) for i in watch.get("item_ids", [])] + list(targets),
        buy_quantity=int(watch.get("buy_quantity", 20)),
        poll_minutes=int(watch.get("poll_minutes", 10)),
        hourly_change_pct=float(alert.get("hourly_change_pct", 15)),
        baseline_change_pct=float(alert.get("baseline_change_pct", 25)),
        baseline_hours=int(alert.get("baseline_hours", 24)),
        cooldown_hours=float(alert.get("cooldown_hours", 3)),
        min_quantity=int(alert.get("min_quantity", 0)),
        targets=targets,
        discord_webhook=os.environ.get("DISCORD_WEBHOOK", notify.get("discord_webhook", "")),
        telegram_token=os.environ.get("TELEGRAM_TOKEN", notify.get("telegram_token", "")),
        telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", str(notify.get("telegram_chat_id", ""))),
        db_path=HERE / raw.get("db_path", "ah_history.sqlite3"),
    )


# ---------------------------------------------------------------- HTTP

def http(url: str, *, data: bytes | None = None, headers: dict | None = None,
         timeout: int = 60) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status, body, encoding = resp.status, resp.read(), resp.headers.get("Content-Encoding")
    except urllib.error.HTTPError as e:
        status, body, encoding = e.code, e.read(), e.headers.get("Content-Encoding")
    if encoding == "gzip":
        body = gzip.decompress(body)
    return status, body


def get_json(url: str, headers: dict | None = None):
    status, body = http(url, headers={"Accept-Encoding": "gzip", **(headers or {})})
    if status != 200:
        raise RuntimeError(f"{url} 回應 {status}: {body[:200]!r}")
    return json.loads(body)


def parse_ts(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


class Undermine:
    """Undermine Exchange API。每小時有 3000 點額度，商品相關的呼叫每次 1～3 點。"""

    def __init__(self, cfg: Config):
        if not cfg.api_key:
            sys.exit("缺少 Undermine API key（用 Patreon 登入 https://undermine.exchange/api.html 後取得）")
        self.cfg = cfg
        self.headers = {"Authorization": f"ApiKey {cfg.api_key}"}

    def _get(self, path: str):
        return get_json(f"{API_BASE}{path}", self.headers)["result"]

    def commodities(self) -> tuple[int, dict[int, dict]]:
        """全區商品摘要（3 點）。回傳 (Blizzard 快照時間, {item_id: {price, quantity}})。"""
        r = self._get(f"/v1/region/{self.cfg.region}/commodities.json")
        return parse_ts(r["snapshot"]), {int(k): v for k, v in r["commodities"].items()}

    def auctions(self, item_id: int) -> list[dict]:
        """單一商品目前的所有掛單（1 點）。"""
        return self._get(f"/v1/region/{self.cfg.region}/commodities/{item_id}/now.json").get("auctions", [])

    def hourly(self, item_id: int) -> list[dict]:
        """單一商品過去約 14 天的每小時價格（3 點）。"""
        return self._get(f"/v1/region/{self.cfg.region}/commodities/{item_id}/hourly.json").get("hourly", [])


def fetch_item_metadata(locale: str) -> dict[int, dict]:
    """從網站前端的靜態資料取得物品分類、資料片和名稱。"""
    items = get_json(f"{SITE_JSON}/items.unbound.json")
    names = get_json(f"{SITE_JSON}/names.unbound.{LOCALES.get(locale, 'enus')}.json")
    qualities = get_json(f"{SITE_JSON}/craftingQualities.json")
    out = {}
    for k, v in items.items():
        name = names.get(k, k)
        tier = qualities.get(str(v.get("craftingQualityId")), {}).get("tier")
        if tier:
            name = f"{name} ★{tier}"
        out[int(k)] = {"class": v.get("class"), "subclass": v.get("subclass"),
                       "expansion": v.get("expansion", 0), "name": name}
    return out


# ---------------------------------------------------------------- 價格計算

@dataclass
class Price:
    item_id: int
    min_price: int      # 最便宜一單位（銅），和網站上顯示的價格相同
    buy_price: int      # 買 buy_quantity 個的平均單價（銅），比最低價不容易被單筆掛單影響
    quantity: int       # 拍賣場總數量


def buy_price(auctions: list[dict], buy_quantity: int) -> int | None:
    need, cost = buy_quantity, 0
    for a in sorted(auctions, key=lambda a: a["price"]):
        take = min(need, a.get("quantity", 1))
        cost += take * a["price"]
        need -= take
        if need == 0:
            break
    bought = buy_quantity - need
    return round(cost / bought) if bought else None


def fmt_gold(copper: float) -> str:
    g, rest = divmod(int(round(copper)), 10000)
    s, c = divmod(rest, 100)
    if g >= 100:
        return f"{g:,}g {s:02d}s"
    return f"{g}g {s:02d}s {c:02d}c" if g else f"{s}s {c:02d}c"


# ---------------------------------------------------------------- 資料庫

class Store:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS snapshot (
                ts INTEGER, item_id INTEGER, min_price INTEGER, buy_price INTEGER, quantity INTEGER,
                PRIMARY KEY (ts, item_id));
            CREATE TABLE IF NOT EXISTS item (item_id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE IF NOT EXISTS alert_log (item_id INTEGER, rule TEXT, ts INTEGER,
                PRIMARY KEY (item_id, rule));
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        """)

    def meta(self, key: str, default: str = "") -> str:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))
        self.db.commit()

    def names(self) -> dict[int, str]:
        return dict(self.db.execute("SELECT item_id, name FROM item"))

    def replace_names(self, names: dict[int, str]) -> None:
        self.db.execute("DELETE FROM item")
        self.db.executemany("INSERT INTO item VALUES (?, ?)", names.items())
        self.db.commit()

    def has_history(self, item_id: int) -> bool:
        return self.db.execute("SELECT 1 FROM snapshot WHERE item_id=? LIMIT 1", (item_id,)).fetchone() is not None

    def last(self, item_id: int, before_ts: int) -> tuple[int, int] | None:
        return self.db.execute(
            "SELECT ts, buy_price FROM snapshot WHERE item_id=? AND ts<? ORDER BY ts DESC LIMIT 1",
            (item_id, before_ts)).fetchone()

    def history(self, item_id: int, since_ts: int, before_ts: int) -> list[int]:
        return [r[0] for r in self.db.execute(
            "SELECT buy_price FROM snapshot WHERE item_id=? AND ts>=? AND ts<?",
            (item_id, since_ts, before_ts))]

    def save(self, ts: int, prices: dict[int, Price]) -> None:
        self.db.executemany(
            "INSERT OR REPLACE INTO snapshot VALUES (?, ?, ?, ?, ?)",
            [(ts, p.item_id, p.min_price, p.buy_price, p.quantity) for p in prices.values()])
        self.db.commit()

    def cooled_down(self, item_id: int, rule: str, now: int, hours: float) -> bool:
        row = self.db.execute("SELECT ts FROM alert_log WHERE item_id=? AND rule=?",
                              (item_id, rule)).fetchone()
        return row is None or now - row[0] >= hours * 3600

    def mark_alert(self, item_id: int, rule: str, now: int) -> None:
        self.db.execute("INSERT OR REPLACE INTO alert_log VALUES (?, ?, ?)", (item_id, rule, now))
        self.db.commit()


# ---------------------------------------------------------------- 提醒規則

def pct(new: float, old: float) -> float:
    return (new - old) / old * 100 if old else 0.0


def check_alerts(cfg: Config, store: Store, ts: int, prices: dict[int, Price],
                 names: dict[int, str]) -> list[str]:
    """比對歷史資料，回傳要送出的提醒訊息。"""
    messages = []
    for iid, p in sorted(prices.items()):
        name = names.get(iid, str(iid))
        hits: list[tuple[str, str]] = []

        if p.quantity >= cfg.min_quantity:
            prev = store.last(iid, ts)
            # 只跟大約一小時內的上一筆比，避免停機很久後誤報
            if prev and ts - prev[0] <= 3 * 3600:
                change = pct(p.buy_price, prev[1])
                if abs(change) >= cfg.hourly_change_pct:
                    hits.append((f"hourly_{'up' if change > 0 else 'down'}",
                                 f"比上次 {fmt_gold(prev[1])} {'漲' if change > 0 else '跌'} {abs(change):.1f}%"))

            hist = store.history(iid, ts - cfg.baseline_hours * 3600, ts)
            if len(hist) >= 6:
                base = statistics.median(hist)
                change = pct(p.buy_price, base)
                if abs(change) >= cfg.baseline_change_pct:
                    hits.append((f"baseline_{'up' if change > 0 else 'down'}",
                                 f"比 {cfg.baseline_hours}h 中位數 {fmt_gold(base)} "
                                 f"{'高' if change > 0 else '低'} {abs(change):.1f}%"))

        t = cfg.targets.get(iid)
        if t:
            if "below" in t and p.min_price <= t["below"] * 10000:
                hits.append(("target_below", f"最低價跌破目標 {t['below']}g"))
            if "above" in t and p.min_price >= t["above"] * 10000:
                hits.append(("target_above", f"最低價漲破目標 {t['above']}g"))

        reasons = []
        for rule, text in hits:
            if store.cooled_down(iid, rule, ts, cfg.cooldown_hours):
                store.mark_alert(iid, rule, ts)
                reasons.append(text)
        if reasons:
            messages.append(
                f"**{name}** ({iid})\n"
                f"　買 {cfg.buy_quantity} 個均價 {fmt_gold(p.buy_price)}｜最低 {fmt_gold(p.min_price)}"
                f"｜庫存 {p.quantity:,}\n"
                + "\n".join(f"　• {r}" for r in reasons)
                + f"\n　https://undermine.exchange/#{cfg.region}-{cfg.realm_slug}/{iid}")
    return messages


# ---------------------------------------------------------------- 通知

def notify(cfg: Config, text: str) -> None:
    sent = False
    if cfg.discord_webhook:
        # Discord 單則訊息上限 2000 字
        for chunk in split(text, 1900):
            status, body = http(cfg.discord_webhook, data=json.dumps({"content": chunk}).encode(),
                                headers={"Content-Type": "application/json"})
            if status >= 300:
                print(f"Discord 傳送失敗 ({status}): {body[:200]!r}", file=sys.stderr)
        sent = True
    if cfg.telegram_token and cfg.telegram_chat_id:
        for chunk in split(text.replace("**", ""), 4000):
            status, body = http(
                f"https://api.telegram.org/bot{cfg.telegram_token}/sendMessage",
                data=json.dumps({"chat_id": cfg.telegram_chat_id, "text": chunk,
                                 "disable_web_page_preview": True}).encode(),
                headers={"Content-Type": "application/json"})
            if status >= 300:
                print(f"Telegram 傳送失敗 ({status}): {body[:200]!r}", file=sys.stderr)
        sent = True
    if not sent:
        print(text)


def split(text: str, size: int) -> list[str]:
    chunks, cur = [], ""
    for block in text.split("\n\n"):
        if cur and len(cur) + len(block) + 2 > size:
            chunks.append(cur)
            cur = ""
        cur = f"{cur}\n\n{block}" if cur else block
    return chunks + ([cur] if cur else [])


# ---------------------------------------------------------------- 主流程

def tracked_items(cfg: Config, store: Store, commodities: dict[int, dict]) -> dict[int, str]:
    """決定要追蹤哪些物品。物品資料每 7 天重新下載一次。"""
    key = f"items:{cfg.item_class}.{cfg.item_subclass}:{cfg.min_expansion}:{cfg.locale}:{sorted(cfg.item_ids)}"
    names = store.names()
    if names and store.meta("items_key") == key and \
            time.time() - float(store.meta("items_fetched", "0")) < METADATA_MAX_AGE:
        return names

    meta = fetch_item_metadata(cfg.locale)
    chosen = set(cfg.item_ids)
    if cfg.item_class is not None:
        in_cat = [i for i, m in meta.items() if i in commodities and m["class"] == cfg.item_class
                  and (cfg.item_subclass is None or m["subclass"] == cfg.item_subclass)]
        # 沒指定資料片時，預設只追最新資料片的物品（舊材料通常沒人在乎）
        min_exp = cfg.min_expansion
        if min_exp is None and in_cat:
            min_exp = max(meta[i]["expansion"] for i in in_cat)
        chosen |= {i for i in in_cat if meta[i]["expansion"] >= (min_exp or 0)}
    names = {i: meta[i]["name"] if i in meta else str(i) for i in chosen}
    store.replace_names(names)
    store.set_meta("items_key", key)
    store.set_meta("items_fetched", str(time.time()))
    print(f"追蹤 {len(names)} 項物品：{'、'.join(sorted(set(names.values())))}")
    return names


def seed_history(api: Undermine, store: Store, item_id: int) -> None:
    """新追蹤的物品先抓過去 14 天的每小時價格，讓「比中位數」的提醒馬上可以用。
    歷史資料只有最低價，所以先拿最低價當作均價。"""
    rows = {}
    for h in api.hourly(item_id):
        if h.get("price"):
            ts = parse_ts(h["snapshot"])
            rows[ts] = Price(item_id, h["price"], h["price"], h.get("quantity", 0))
    for ts, p in rows.items():
        store.save(ts, {item_id: p})


def run_once(cfg: Config, api: Undermine, store: Store) -> bool:
    """檢查一次。有新快照時回傳 True。"""
    ts, commodities = api.commodities()
    if str(ts) == store.meta("last_snapshot"):
        return False
    names = tracked_items(cfg, store, commodities)

    prices = {}
    for iid in names:
        c = commodities.get(iid)
        if not c or not c.get("quantity"):
            continue
        if not store.has_history(iid):
            seed_history(api, store, iid)
        bp = buy_price(api.auctions(iid), cfg.buy_quantity) if cfg.buy_quantity > 1 else None
        prices[iid] = Price(iid, c["price"], bp or c["price"], c["quantity"])

    messages = check_alerts(cfg, store, ts, prices, names)
    store.save(ts, prices)
    store.set_meta("last_snapshot", str(ts))
    snap = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    print(f"[快照 {snap}] {len(prices)} 項物品有掛單，{len(messages)} 則提醒")
    if messages:
        notify(cfg, f"📈 拍賣場價格波動（{cfg.region.upper()}）\n\n" + "\n\n".join(messages))
    return True


def list_prices(store: Store) -> None:
    rows = store.db.execute("""
        SELECT s.item_id, COALESCE(i.name, s.item_id), s.min_price, s.buy_price, s.quantity
        FROM snapshot s JOIN item i USING (item_id)
        WHERE s.ts = (SELECT MAX(ts) FROM snapshot) ORDER BY i.name""").fetchall()
    if not rows:
        print("還沒有資料，先跑一次 --once")
    for iid, name, mn, buy, qty in rows:
        print(f"{iid:>7}  {name:<16} 最低 {fmt_gold(mn):>14}  均價 {fmt_gold(buy):>14}  庫存 {qty:>9,}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-c", "--config", type=Path, default=HERE / "config.toml")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--loop", action="store_true")
    mode.add_argument("--list", action="store_true")
    mode.add_argument("--test-notify", action="store_true")
    args = ap.parse_args()

    cfg = load_config(args.config)
    store = Store(cfg.db_path)
    if args.list:
        return list_prices(store)
    if args.test_notify:
        return notify(cfg, "✅ wow-ah-alert 測試訊息：通知設定正常")

    api = Undermine(cfg)
    if args.once:
        run_once(cfg, api, store)
        return
    print(f"每 {cfg.poll_minutes} 分鐘檢查一次（資料大約每小時更新）。Ctrl+C 結束。")
    while True:
        try:
            run_once(cfg, api, store)
        except Exception as e:  # 網路錯誤不要讓常駐程式掛掉
            print(f"[{time.strftime('%H:%M')}] 錯誤：{e}", file=sys.stderr)
        time.sleep(cfg.poll_minutes * 60)


if __name__ == "__main__":
    main()
