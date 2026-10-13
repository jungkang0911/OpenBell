# 📈 orb-ntfy：台股開盤區間突破（ORB）監控

盯著開盤 15 分鐘的高低點，**帶量突破就推播到手機**：附進場價、停損、目標價，點一下就開 K 線圖。
收盤後再推一則總結，告訴你每筆訊號最後是達標、停損，還是收盤平倉。

![終端機即時監控畫面](docs/demo.gif)

<sub>↑ `python orb_multi_yf_ntfy.py --demo`：用 `data/` 的測試資料快轉重播一個早盤</sub>

---

## ✨ 功能

- **終端機即時表格**：每檔的現價、漲跌、開盤區間、目前位置、VWAP、量比、訊號與即時損益（紅漲綠跌）
- **手機推播（ntfy）**：中文標題、進場／停損／目標價與風險報酬，點通知直接開 Yahoo 股市技術線圖
- **收盤總結**：當天每筆訊號的結果與合計 R 值
- **雲端自動執行**：GitHub Actions 每個交易日 08:40 自動啟動、收盤後結束，電腦不用開著
- **回測**：用跟盤中完全相同的邏輯回測最近 30 天，比較不同參數
- 不會重複推播（每檔每方向每天一次）、開盤前不會誤用昨天的資料、遇到假日自動判定休市

---

## 🚀 快速開始

```bash
pip install -r requirements.txt
```

### 1. 先看看畫面（不用等開盤、不會推播）

```bash
python orb_multi_yf_ntfy.py --demo
```

### 2. 盤中實際監控

```bash
python orb_multi_yf_ntfy.py --symbols-file symbols.txt --ntfy-topic 你的主題名稱
```

- 手機安裝 [ntfy App](https://ntfy.sh)（iOS / Android），訂閱同一個主題名稱就會收到通知
- 主題名稱等於密碼，**任何知道名稱的人都看得到通知**，請取一個別人猜不到的名字
- 只想在終端機看、不推播：把 `--ntfy-topic ...` 換成 `--notify print`
- 開盤前啟動會自動等到 09:00；不加 `--exit-after-close` 就會一直跑，隔天自動接著監控

### 3. 試推一則通知到手機

```bash
python replay_min.py --csv data/2330.TW.csv --symbol 2330.TW --speed 0.1 --ntfy-topic 你的主題名稱
```

---

## 🔔 通知長這樣

```
2330.TW 多頭突破 102.14
做多 102.14（09:30）
停損 98.80（-3.3%）
目標 108.83（2R，+6.6%）
ORH 100.70 / ORL 98.80 ｜ VWAP 99.86 ｜ 量 3.3x
```

收盤總結：

```
ORB 收盤總結 10/13
合計 3 筆，-0.68R
2317.TW 空 98.61→98.14 收盤 +0.19R（+0.5%）
2330.TW 多 102.14→102.57 收盤 +0.13R（+0.4%）
2454.TW 多 1009.70→995.30 停損 -1.00R（-1.4%）
```

---

## ☁️ 雲端自動執行（GitHub Actions，免費）

repo 已經附好 [`.github/workflows/orb-monitor.yml`](.github/workflows/orb-monitor.yml)：

1. 到 GitHub repo 的 **Settings → Secrets and variables → Actions**，新增一個 secret：名稱 `NTFY_TOPIC`，值填你的主題名稱
2. 編輯 [`symbols.txt`](symbols.txt) 設定要監控的股票
3. 完成。每週一到週五 08:40（台北時間）會自動啟動，收盤推完總結後結束；國定假日沒有行情時會自動判定休市並結束
4. 想手動測試：到 **Actions → ORB 盤中監控 → Run workflow**

> 注意：
> - GitHub 的排程可能延遲幾分鐘到十幾分鐘才啟動，所以設定在 08:40，留了緩衝
> - repo 連續 60 天沒有任何 commit，GitHub 會自動停用排程，需要到 Actions 頁面重新啟用
> - Yahoo 偶爾會限制雲端機房的 IP；如果 log 一直出現抓資料失敗，改在自己電腦上跑

---

## 🧪 回測

**先回測，再相信通知。** 回測用的訊號邏輯跟盤中監控完全相同。

```bash
# Yahoo 最近 30 天 1 分 K（Yahoo 只提供最近 30 天）
python backtest.py --symbols-file symbols.txt

# 比較不同量能門檻
python backtest.py --symbols-file symbols.txt --vol-factor 1.0,1.5,2.0

# 列出每筆交易、存成 CSV
python backtest.py --symbols 2330.TW --trades --out trades.csv

# 用自己的 CSV（欄位：Datetime,Open,High,Low,Close,Volume）
python backtest.py --csv data/*.csv --trades
```

```
量能門檻  筆數 勝率 平均R 累計R 平均報酬  達標  停損  收盤
    1.5x     3  67% -0.23  -0.7   -0.18%     0     1     2
```

出場規則：先碰到停損或目標就出場（同一根 K 棒兩者都碰到，保守算停損），都沒碰到就收盤平倉。**不含手續費、證交稅與滑價**；30 天的樣本也不大，結果請當參考。

---

## 📐 策略規則

1. **開盤區間**：09:00 起前 `--open-mins` 分鐘（預設 15）的最高價 ORH、最低價 ORL，區間完整走完才確定
2. **多頭突破**：1 分 K 收盤價 > ORH，且 ≥ VWAP，且成交量 ≥ 前 10 根平均量 × `--vol-factor`
3. **空頭跌破**：收盤價 < ORL，且 ≤ VWAP，且量能條件同上
4. **停損**：區間另一端（`--stop-mode range`，預設）或區間中線（`mid`）
5. **目標**：進場價 ± 風險 × `--target-r`（預設 2R）
6. 只用**已收完**的 K 棒判斷（進行中那根成交量不完整）

---

## ⚙️ 參數

`orb_multi_yf_ntfy.py`：

| 參數 | 說明 | 預設 |
|---|---|---|
| `--symbols` | 逗號分隔代號，例如 `2330.TW,2317.TW`（上櫃用 `.TWO`） | — |
| `--symbols-file` | 一行一檔的清單檔，`#` 後面是註解 | — |
| `--ntfy-topic` | ntfy 主題名稱（也可用環境變數 `NTFY_TOPIC`） | — |
| `--notify` | `ntfy` 推播；`print` 只顯示不推播 | `ntfy` |
| `--open-mins` | 開盤區間分鐘數 | 15 |
| `--vol-factor` | 量能門檻倍數 | 1.5 |
| `--stop-mode` | `range` 區間另一端／`mid` 區間中線 | `range` |
| `--target-r` | 目標價的風險倍數 | 2 |
| `--poll-secs` | 抓資料間隔秒數 | 30 |
| `--every-bar` | 每根新 K 棒符合條件都推播（預設每檔每方向每天一次） | 關 |
| `--max-signal-age` | 中途啟動時，超過幾分鐘的舊訊號不推播 | 5 |
| `--no-summary` | 收盤不推總結 | — |
| `--exit-after-close` | 收盤或判定休市後結束程式（排程用） | — |
| `--no-tui` | 不用即時表格，改成一行一行的 log（輸出導到檔案時自動切換） | — |
| `--market-open` / `--market-close` | 開收盤時間 | 09:00 / 13:30 |
| `--timezone` | 時區 | Asia/Taipei |
| `--ntfy-server` | 自架 ntfy 伺服器網址 | https://ntfy.sh |
| `--demo` | 用 `data/*.csv` 快轉重播一個早盤 | — |

---

## ⚠️ 注意事項

- 資料來自 Yahoo Finance，**有延遲**，加上只判斷已收完的 K 棒，通知大約晚 1 分鐘以上，不適合搶快
- 程式只會通知、**不會下單**；要即時行情或自動下單，可以改接永豐 Shioaji API
- 本專案僅供研究與學習，不構成任何投資建議

---

## 🗂 專案結構

```
orb-ntfy/
├── orb_multi_yf_ntfy.py     # 盤中監控（即時表格 + 推播 + 收盤總結）
├── orb_core.py              # 共用策略邏輯：開盤區間、訊號、出場模擬、ntfy
├── backtest.py              # 回測
├── replay_min.py            # 重播單一 CSV，測試手機通知
├── symbols.txt              # 監控清單
├── data/                    # 測試用 1 分 K（2454 是假突破被停損的例子）
├── tests/                   # pytest 測試
├── tools/make_demo_gif.py   # 重新產生 docs/demo.gif
└── .github/workflows/       # 盤中自動監控、測試
```

## 🛠 開發

```bash
pip install pytest pillow
pytest -q                      # 跑測試
python tools/make_demo_gif.py  # 改了畫面後重錄 README 動畫
```

## 📄 License

MIT
