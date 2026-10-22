#!/usr/bin/env python3
"""WoW 拍賣場價格波動提醒 bot（台服商品／commodities）。

資料來源和 undermine.exchange 相同：Blizzard 官方 Battle.net API。
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
import base64
import json
import os
import sqlite3
import statistics
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
USER_AGENT = "wow-ah-alert/1.0"


# ---------------------------------------------------------------- 設定

@dataclass
class Config:
    client_id: str
    client_secret: str
    region: str
    realm_slug: str
    locale: str
    item_class: int | None
    item_subclass: int | None
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
    bnet = raw.get("battlenet", {})
    watch = raw.get("watch", {})
    alert = raw.get("alert", {})
    notify = raw.get("notify", {})
    targets = {int(t["item_id"]): t for t in raw.get("target", [])}
    return Config(
        # 環境變數優先，避免把金鑰寫進檔案
        client_id=os.environ.get("BNET_CLIENT_ID", bnet.get("client_id", "")),
        client_secret=os.environ.get("BNET_CLIENT_SECRET", bnet.get("client_secret", "")),
        region=bnet.get("region", "tw"),
        realm_slug=bnet.get("realm_slug", "whisperwind"),
        locale=bnet.get("locale", "zh_TW"),
        item_class=watch.get("item_class"),
        item_subclass=watch.get("item_subclass"),
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
         timeout: int = 120) -> tuple[int, dict, bytes]:
    req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


class Blizzard:
    def __init__(self, cfg: Config):
        if not cfg.client_id or not cfg.client_secret:
            sys.exit("缺少 Battle.net client_id / client_secret（到 https://develop.battle.net 申請）")
        self.cfg = cfg
        self.base = f"https://{cfg.region}.api.blizzard.com"
        self._token = ""
        self._token_expires = 0.0

    def token(self) -> str:
        if time.time() < self._token_expires - 60:
            return self._token
        basic = base64.b64encode(f"{self.cfg.client_id}:{self.cfg.client_secret}".encode()).decode()
        status, _, body = http(
            "https://oauth.battle.net/token",
            data=b"grant_type=client_credentials",
            headers={"Authorization": f"Basic {basic}",
                     "Content-Type": "application/x-www-form-urlencoded"},
        )
        if status != 200:
            raise RuntimeError(f"取得 token 失敗 ({status}): {body[:200]!r}")
        payload = json.loads(body)
        self._token = payload["access_token"]
        self._token_expires = time.time() + payload.get("expires_in", 3600)
        return self._token

    def get(self, path: str, params: dict, extra_headers: dict | None = None) -> tuple[int, dict, bytes]:
        url = f"{self.base}{path}?{urllib.parse.urlencode(params)}"
        headers = {"Authorization": f"Bearer {self.token()}", "Accept-Encoding": "identity"}
        return http(url, headers={**headers, **(extra_headers or {})})

    def commodities(self, if_modified_since: str = "") -> tuple[str, list[dict]] | None:
        """回傳 (Last-Modified, auctions)；資料沒更新時回傳 None。"""
        headers = {"If-Modified-Since": if_modified_since} if if_modified_since else {}
        status, resp_headers, body = self.get(
            "/data/wow/auctions/commodities",
            {"namespace": f"dynamic-{self.cfg.region}", "locale": self.cfg.locale},
            headers,
        )
        if status == 304:
            return None
        if status != 200:
            raise RuntimeError(f"讀取商品拍賣失敗 ({status}): {body[:200]!r}")
        return resp_headers.get("Last-Modified", ""), json.loads(body)["auctions"]

    def search_items(self, item_class: int, item_subclass: int | None) -> dict[int, str]:
        """用物品搜尋 API 找出某分類的所有物品 id 和名稱。"""
        result: dict[int, str] = {}
        page = 1
        while True:
            params = {"namespace": f"static-{self.cfg.region}", "item_class.id": item_class,
                      "orderby": "id", "_pageSize": 1000, "_page": page}
            if item_subclass is not None:
                params["item_subclass.id"] = item_subclass
            status, _, body = self.get("/data/wow/search/item", params)
            if status != 200:
                raise RuntimeError(f"物品搜尋失敗 ({status}): {body[:200]!r}")
            payload = json.loads(body)
            for r in payload.get("results", []):
                d = r["data"]
                name = d.get("name", {})
                result[d["id"]] = name.get(self.cfg.locale) or name.get("en_US") or str(d["id"])
            if page >= payload.get("pageCount", 1):
                return result
            page += 1

    def item_name(self, item_id: int) -> str:
        status, _, body = self.get(f"/data/wow/item/{item_id}",
                                   {"namespace": f"static-{self.cfg.region}", "locale": self.cfg.locale})
        if status != 200:
            return str(item_id)
        return json.loads(body).get("name") or str(item_id)


# ---------------------------------------------------------------- 價格計算

@dataclass
class Price:
    item_id: int
    min_price: int      # 最便宜一單位（銅）
    buy_price: int      # 買 buy_quantity 個的平均單價（銅），比最低價不容易被單筆掛單影響
    quantity: int       # 拍賣場總數量


def summarize(auctions: list[dict], item_ids: set[int], buy_quantity: int) -> dict[int, Price]:
    listings: dict[int, list[tuple[int, int]]] = {}
    for a in auctions:
        iid = a["item"]["id"]
        if iid in item_ids:
            listings.setdefault(iid, []).append((a["unit_price"], a["quantity"]))
    out = {}
    for iid, rows in listings.items():
        rows.sort()
        need, cost = buy_quantity, 0
        for price, qty in rows:
            take = min(need, qty)
            cost += take * price
            need -= take
            if need == 0:
                break
        bought = buy_quantity - need
        out[iid] = Price(iid, rows[0][0], round(cost / bought), sum(q for _, q in rows))
    return out


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

    def save_names(self, names: dict[int, str]) -> None:
        self.db.executemany("INSERT OR REPLACE INTO item VALUES (?, ?)", names.items())
        self.db.commit()

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
            status, _, body = http(cfg.discord_webhook, data=json.dumps({"content": chunk}).encode(),
                                   headers={"Content-Type": "application/json"})
            if status >= 300:
                print(f"Discord 傳送失敗 ({status}): {body[:200]!r}", file=sys.stderr)
        sent = True
    if cfg.telegram_token and cfg.telegram_chat_id:
        for chunk in split(text.replace("**", ""), 4000):
            status, _, body = http(
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

def tracked_items(cfg: Config, api: Blizzard, store: Store) -> dict[int, str]:
    names = store.names()
    key = f"category:{cfg.item_class}.{cfg.item_subclass}"
    if cfg.item_class is not None and store.meta(key) != "done":
        print(f"第一次執行：搜尋分類 {cfg.item_class}.{cfg.item_subclass} 的物品…")
        found = api.search_items(cfg.item_class, cfg.item_subclass)
        store.save_names(found)
        store.set_meta(key, "done")
        names.update(found)
    missing = [i for i in cfg.item_ids if i not in names]
    if missing:
        fetched = {i: api.item_name(i) for i in missing}
        store.save_names(fetched)
        names.update(fetched)
    if cfg.item_class is None:
        return {i: names[i] for i in cfg.item_ids}
    return names


def run_once(cfg: Config, api: Blizzard, store: Store) -> bool:
    """檢查一次。有新資料時回傳 True。"""
    result = api.commodities(store.meta("last_modified"))
    if result is None:
        return False
    last_modified, auctions = result
    names = tracked_items(cfg, api, store)
    prices = summarize(auctions, set(names), cfg.buy_quantity)
    ts = int(time.time())
    messages = check_alerts(cfg, store, ts, prices, names)
    store.save(ts, prices)
    store.set_meta("last_modified", last_modified)
    print(f"[{time.strftime('%Y-%m-%d %H:%M')}] 新快照：{len(prices)} 項物品，{len(messages)} 則提醒")
    if messages:
        notify(cfg, f"📈 拍賣場價格波動（{cfg.region.upper()}）\n\n" + "\n\n".join(messages))
    return True


def list_prices(store: Store) -> None:
    rows = store.db.execute("""
        SELECT s.item_id, COALESCE(i.name, s.item_id), s.min_price, s.buy_price, s.quantity
        FROM snapshot s LEFT JOIN item i USING (item_id)
        WHERE s.ts = (SELECT MAX(ts) FROM snapshot) ORDER BY s.buy_price DESC""").fetchall()
    if not rows:
        print("還沒有資料，先跑一次 --once")
    for iid, name, mn, buy, qty in rows:
        print(f"{iid:>7}  {name:<24} 最低 {fmt_gold(mn):>14}  均價 {fmt_gold(buy):>14}  庫存 {qty:>9,}")


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

    api = Blizzard(cfg)
    if args.once:
        run_once(cfg, api, store)
        return
    print(f"每 {cfg.poll_minutes} 分鐘檢查一次（Blizzard 大約每小時更新一次商品資料）。Ctrl+C 結束。")
    while True:
        try:
            run_once(cfg, api, store)
        except Exception as e:  # 網路錯誤不要讓常駐程式掛掉
            print(f"[{time.strftime('%H:%M')}] 錯誤：{e}", file=sys.stderr)
        time.sleep(cfg.poll_minutes * 60)


if __name__ == "__main__":
    main()
