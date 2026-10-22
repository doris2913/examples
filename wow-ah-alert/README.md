# wow-ah-alert：拍賣場價格波動提醒 bot

監控台服拍賣場的商品價格（預設是 undermine.exchange 上的 `cat=7.5`：材料 → 布料）。價格大漲、大跌或碰到你設的目標價時，會發通知到 Discord 或 Telegram。

資料來自 [Undermine Exchange API](https://undermine.exchange/api.html)。布料是「商品」，整個台服區共用一個拍賣場，所以就是你在 tw-whisperwind 上看到的價格。

## 安裝

需要 Python 3.11 以上，不用另外裝任何套件。

1. 到 <https://undermine.exchange/api.html> 用 Patreon 帳號登入，點「Click to reveal your key」取得 API key。免費帳號就能用，不用贊助。
2. 設定通知管道（擇一）：
   - **Discord**：頻道設定 → 整合 → Webhook → 新增 → 複製網址。
   - **Telegram**：跟 @BotFather 建立 bot 拿 token，再傳一則訊息給你的 bot，然後打開 `https://api.telegram.org/bot<token>/getUpdates` 找到 `chat.id`。
3. 複製設定檔並填好：
   ```
   cp config.example.toml config.toml
   ```
4. 測試通知：`python ah_alert.py --test-notify`

## 執行

```
python ah_alert.py --loop     # 常駐，每 10 分鐘檢查一次（沒有新快照時不會重複處理）
python ah_alert.py --once     # 只跑一次，可以搭配 cron 或 Windows 工作排程器
python ah_alert.py --list     # 列出最新價格
```

第一次執行時，程式會下載網站的物品資料來判斷哪些物品是布料，並抓每項物品過去 14 天的每小時價格當作歷史資料。所以「比 24 小時中位數」的提醒一開始就能用，不用等。

要長期跑的話，放在一台一直開著的電腦或便宜的 VPS 上。用 cron 的範例：
```
20 * * * * cd /path/to/wow-ah-alert && python3 ah_alert.py --once >> alert.log 2>&1
```

## 會追蹤哪些物品

- 預設只追蹤最新資料片的布料，目前是 16 項，例如日炎絲綢、奧紋布、鮮亮亞麻布和它們的布卷。同一種材料的不同品質（★1、★2）會分開追蹤。
- 想連舊資料片的物品一起追蹤，就設定 `min_expansion`。
- 想監控別的分類，就改 `item_class` / `item_subclass`，也就是 undermine.exchange 網址裡 `cat=` 後面的兩個數字。例如 `cat=7.9` 是草藥，`cat=7.7` 是金屬與石頭。
- 物品清單每 7 天自動更新一次。

## 提醒規則（都在 `config.toml` 裡調整）

| 規則 | 預設 | 說明 |
|---|---|---|
| `hourly_change_pct` | 15% | 跟上一次快照（約 1 小時前）相比的漲跌幅 |
| `baseline_change_pct` / `baseline_hours` | 25% / 24h | 跟過去 24 小時中位數相比，抓慢慢漲、慢慢跌的趨勢 |
| `[[target]]` `below` / `above` | — | 最低價跌破或漲破指定金額 |
| `buy_quantity` | 20 | 漲跌幅用「買 N 個的平均單價」計算，避免單筆特價掛單造成誤報 |
| `min_quantity` | 100 | 拍賣場庫存太少的冷門物品，不做 % 波動提醒 |
| `cooldown_hours` | 3 | 同一物品的同一種提醒，在這段時間內只發一次 |

## API 額度

Undermine API 每小時有 3000 點額度。以預設的 16 項布料計算：

| 什麼時候 | 呼叫 | 點數 |
|---|---|---|
| 每次檢查 | 商品摘要 | 3 點 |
| 每個新快照（約每小時） | 每項物品的掛單 | 每項 1 點，共 16 點 |
| 第一次執行 | 每項物品的 14 天歷史 | 每項 3 點，共 48 點 |

每小時大約只用 30～40 點，離上限很遠。把 `buy_quantity` 設成 1 就只看最低價，每小時只需要商品摘要那 3 點。

## 測試

```
python -m unittest test_ah_alert
```
測試會用假的 API 驗證價格計算、物品篩選、漲跌提醒、歷史資料、冷卻時間和目標價。
