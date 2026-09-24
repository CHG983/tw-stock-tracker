# 台股追蹤網站 (TW Stock Tracker)

一個以**臺灣證券交易所（TWSE）公開資料**為唯一資料來源的台股追蹤單頁儀表板。

- 單一 HTML 檔、CSS 全內嵌、**零外部依賴**（除 TWSE API 外）、**零 JavaScript 框架或圖表函式庫**
- 所有圖表皆為**手算座標的 inline SVG**
- 符合台股慣例的**紅漲綠跌**配色
- 響應式設計（RWD），桌機／平板／手機皆可閱讀

---

## 🔗 線上瀏覽

啟用 GitHub Pages 後，可直接開啟：

```
https://<你的帳號>.github.io/tw-stock-tracker/
```

---

## 📁 專案結構

```
.
├── index.html              # 最新版（v6）— GitHub Pages 首頁，由 GitHub Actions 每日自動更新
├── versions/
│   ├── v1/index.html       # v1：加權指數概況 + 自選股清單（靜態快照）
│   ├── v2/index.html       # v2：+ MA5／MA20 均線 + 成交量柱狀圖
│   ├── v3/index.html       # v3：+ 重新整理／自動更新（前端 fetch TWSE API）
│   ├── v4/index.html       # v4：+ 漲幅前十名／跌幅前十名／2330 台積電
│   ├── v5/index.html       # v5：+ MA5／MA20 黃金交叉／死亡交叉標示
│   └── v6/index.html       # v6：+ 每日自動更新快照（與根目錄 index.html 同內容）
├── scripts/
│   └── update_snapshot.py  # 抓取 TWSE 公開資料並重寫 index.html 的內建快照
├── tests/
│   ├── fixtures/           # 從 TWSE 實際擷取的原始回應樣本（讓測試可離線重播）
│   ├── capture_fixtures.py # 重新擷取樣本（只連網一次，手動執行）
│   ├── _harness.py         # FakeAPI 假取樣器 + 逐端點故障注入（測試不連網）
│   └── test_update_snapshot.py  # 單元／整合測試
├── .github/workflows/
│   ├── update-snapshot.yml # 排程：台北 15:30（週一至週五）自動更新快照（先跑測試）
│   └── tests.yml           # push／PR 時自動執行測試（Python 3.9 / 3.11 / 3.13）
├── LICENSE                 # MIT
├── .nojekyll               # 停用 Jekyll，確保 GitHub Pages 直接原樣輸出
└── README.md
```

各版本皆為**完整可獨立開啟的單檔**，方便逐版對照功能演進。
`versions/vN/index.html` 也可直接以網址瀏覽，例如 `/versions/v5/index.html`。

---

## 🔄 自動更新（GitHub Actions）

`index.html` 的內建快照由 **GitHub Actions 每日自動更新**，不需人工維護。

### 排程時間

| 項目 | 內容 |
|---|---|
| Cron | `30 7 * * 1-5` |
| 台北時間 | **15:30（UTC+8）**，週一至週五 |
| 說明 | 台股 13:30 收盤後執行，確保當日資料已由證交所端點釋出 |

### 手動觸發

1. 前往 repo 的 **Actions** 頁籤
2. 左側選擇 **「Update snapshot」**
3. 點 **「Run workflow」** → 選 `main` 分支 → 執行

也可以用 GitHub CLI：

```bash
github workflow run update-snapshot.yml --repo <你的帳號>/tw-stock-tracker
```

### 腳本

`scripts/update_snapshot.py`（**僅使用 Python 標準函式庫**，無需 `pip install`）

```bash
# 在本機執行，更新 index.html 的內建快照
python3 scripts/update_snapshot.py --file index.html

# 只檢查、不寫入檔案
python3 scripts/update_snapshot.py --file index.html --dry-run
```

| 步驟 | 動作 |
|---|---|
| 1 | 抓取 `STOCK_DAY_ALL`（全市場每日收盤行情），以其回傳資料自帶的日期為基準 |
| 2 | 抓取 `MI_INDEX?type=MS`（成交金額、漲跌家數） |
| 3 | 抓取 `FMTQIK`（近 14 個月加權指數歷史日資料） |
| 4 | 抓取 `MI_5MINS_HIST` 並做**一致性驗證**（收盤指數、漲跌點數兩來源比對） |
| 5 | 計算 MA5／MA20、交叉事件、漲跌幅排行榜與全部圖表座標 |
| 6 | 只重寫標記之間的區塊（HTML 區塊用 `<!--SNAP:…-->`；位於 `<script>` 內的區塊用 `/*SNAP:…*/`，因為 HTML 註解在 `<script>` 內會被當成單行註解而破圖 JS），其餘 HTML／CSS／JS 完全不動 |

### 更新範圍

腳本會重寫以下內建快照區塊：加權指數概況、漲跌家數、走勢圖（收盤／MA5／MA20／成交量柱）、MA 交叉事件清單、漲幅前十名／跌幅前十名／2330 台積電排行，以及頁尾資料時間。

### 資料來源

全部取自**臺灣證券交易所（TWSE）公開資料**，與頁面前端使用的端點相同（見下一節）。

### 非交易日與盤中行為

- **`STOCK_DAY_ALL` 會忽略 `date` 參數，永遠回傳「最後交易日」的資料。** 腳本因此不依賴請求參數，而是**以回傳資料自帶的日期欄位為準**，再用該日期去查其他端點，避免兩個端點落在不同交易日的錯配。
- 排程僅在**週一至週五**執行；遇國定假日時，端點回傳的仍是前一個交易日資料，腳本會據實標註該日期（**不會寫入空白或假資料**）。
- 因此**週末與假日執行是安全的**：結果要嘛無變化（不建立 commit），要嘛正確標註為前一交易日。

### 失敗處理

任一抓取、解析或一致性驗證失敗，腳本會以**非 0 結束且不寫入任何檔案** —— workflow 隨之失敗（會寄送通知），`index.html` 完整保留原樣，**絕不寫入空值或示意數字**。僅在檔案內容確實改變時才建立 commit（無變化則直接結束）。

---

## 📊 資料來源（臺灣證券交易所公開資料）

| 顯示項目 | 端點 | 更新頻率／延遲 |
|---|---|---|
| 加權指數（值） | `www.twse.com.tw/rwd/zh/TAIEX/MI_5MINS_INDEX` | **約 5 秒** |
| 加權指數（漲跌、開高低） | `www.twse.com.tw/rwd/zh/TAIEX/MI_5MINS_HIST` | 當日，隨盤中更新 |
| 成交金額、漲跌家數 | `www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?type=MS` | 盤中約 1 分鐘級 |
| 個股股價／漲跌／成交量（全市場） | `www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY_ALL` | 盤後為當日收盤價 |
| 加權指數歷史日資料（走勢圖／均線／交叉） | `www.twse.com.tw/rwd/zh/exchangeReport/FMTQIK` | 盤後 |

> 上列 `www.twse.com.tw` 端點皆回傳 `Access-Control-Allow-Origin: *`，因此可**由瀏覽器前端直接 fetch**。

### 未採用的來源與原因（誠實揭露）

| 端點 | 原因 |
|---|---|
| `mis.twse.com.tw` / `getStockInfo.jsp`（即時報價 API） | 經真實瀏覽器實測**被 CORS 阻擋**（無 `Access-Control-Allow-Origin`），且加上 `&callback=` **不會回傳 JSONP 包裝**，故無法以 JSONP 繞過。**因此在瀏覽器端不可用。** |
| `openapi.twse.com.tw`（OpenAPI 公開資料） | 同樣**無 CORS 標頭**，瀏覽器端不可用（已於沙盒端實測抓取成功，但無法前端直連）。 |

---

## ✨ 功能清單

### 大盤概況
- 加權指數大字顯示、漲跌點數與漲跌幅
- 開盤／最高／最低／振幅
- 四格 KPI：成交金額、一般股票成交金額、上漲家數、下跌家數
- 市場漲跌家數數字卡 + 堆疊比例條（上漲／下跌／持平百分比）

### 走勢圖（v2 起）
- 近 **60 個交易日**加權指數收盤折線（含面積填色）
- **MA5／MA20 雙均線**（不同顏色 + 圖例 + 各自最新數值）
- **成交量柱狀圖**副圖（與主圖共用 X 軸，紅漲綠跌）
- 格線、雙軸刻度標籤、終點值標註

### 黃金交叉／死亡交叉標示（v5 起）
- 由 `FMTQIK` **260 個交易日**完整歷史計算 MA5／MA20，程式判定交叉（差值由負轉正＝黃金交叉、由正轉負＝死亡交叉）
- 走勢圖上以**垂直虛線 + 圓點 + 日期膠囊**標示，採**交錯高度**避免標籤重疊
- 圖下列出**完整交叉事件清單**（日期／類型／當時指數點位），並標註哪些落在圖表區間內
- 附黃金交叉／死亡交叉的意義說明

### 漲跌幅排行（v4 起）
- **當日漲幅前十名**（依漲跌幅由高到低，紅字）
- **當日跌幅前十名**（依漲跌幅由低到高，綠字）
- **2330 台積電**單獨一檔，固定顯示
- 每檔顯示股票代號、名稱、收盤價、漲跌、漲跌幅，並附漲跌幅比例條
- 另附自選股漲跌幅雙向比較圖

### 即時更新（v3 起）
- 「**重新整理**」按鈕：點擊後即時抓取最新報價
- 「**自動更新**」開關：每 60 秒自動更新（可分頁切回前景時立即重抓）
- 顯示**最後更新時間**與載入中／成功／失敗狀態提示
- **失敗時自動退回內建快照**，並以紅色提示標示目前顯示的是「即時資料」還是「快照資料」

### 視覺與可達性
- 紅漲綠跌（多頭 `rgb(214,43,28)` / 空頭 `rgb(11,143,76)`）符合台股慣例
- 文字對比度全部通過 **WCAG AA（≥ 4.5:1）**
- `prefers-reduced-motion` 無障礙處理、圖表具 `aria-label`、`lang="zh-Hant"`
- 三段 RWD 斷點（1180 / 980 / 760 / 400px）

---

## 🚀 使用方式

1. **直接開啟**：下載 `index.html`，用瀏覽器雙擊即可（無需伺服器、無需建置）
2. **線上瀏覽**：開啟 GitHub Pages 網址
3. **重新整理即時資料**：點右上角「重新整理」按鈕（需網路連線至 `www.twse.com.tw`）

> ⚠️ 若在**離線**或**被封鎖 TWSE 網域**的環境開啟，頁面仍可正常顯示，並會顯示內建的當日快照資料（狀態標示為「快照資料」）。

---

## ⚠️ 已知限制

1. **個股無法取得盤中逐筆即時報價**：`mis.twse.com.tw` 被 CORS 阻擋（見上），個股改走 `STOCK_DAY_ALL`，**盤後即為當日收盤價，盤中該端點可能尚未含當日資料**。真正 5 秒級的即時性**僅適用於大盤指數**。
2. **MA5／MA20 均線、成交量副圖與交叉事件為內建快照**，**不隨「重新整理」變動**（歷史日資料端點為盤後更新）。
3. **均線為落後指標**：訊號必在趨勢形成後才出現，盤整時易反覆交叉產生假訊號（本資料區間即出現連續兩日反轉、連續三次交叉的情形），**僅供參考**。
4. **窄螢幕隱藏圖上日期標籤**：1180px 以下會隱藏 SVG 內的交叉日期膠囊（避免文字縮到不可讀），改以圖下彩色摘要列與交叉事件清單呈現。
5. **本頁為靜態快照**：重新整理僅更新大盤即時值與漲跌幅排行，不重算走勢圖與均線。

---

## 🧪 資料正確性驗證（開發過程實際執行）

- **個股收盤價**：`STOCK_DAY_ALL` 與 `MI_INDEX type=ALL` 兩來源**逐檔比對全市場 1,380 檔，0 筆不符**
- **加權指數**：`MI_5MINS_INDEX`（即時）與 `MI_5MINS_HIST`（當日）與 `FMTQIK`（歷史）三來源交叉驗證一致
- **漲跌家數母體**：以排除規則篩出 1,075 檔，其**上漲 386／下跌 546／持平 140** 與官方股票統計**完全吻合**（差額 9 檔正是官方列為「未成交 6 家」與「無比價 3 家」）
- **均線與交叉**：MA5／MA20 尾點與快照一致；4 個交叉點座標、三組均線數值與原始 `FMTQIK` 重算結果**零誤差**，且皆通過符號翻轉斷言

---

## 🧪 執行測試

`scripts/update_snapshot.py` 附有單元／整合測試，**測試期間完全不連網**：所有 HTTP
請求都會被攔截，改以 `tests/fixtures/` 內、從臺灣證券交易所**實際擷取**的原始回應
樣本重播，並可逐端點注入故障（HTTP 500、timeout、空回應、HTML 錯誤頁、壞 JSON、
欄位缺失、非交易日、欄位格式異常…）。

```bash
python3 -m unittest discover -s tests -t . -v      # 全部執行（建議）
python3 -m unittest tests.test_update_snapshot -v  # 同上，指定模組
python3 -m unittest tests.test_update_snapshot.TestNoNetwork -v  # 只驗證「不連網」
```

> 亦可使用 pytest（`pip install pytest && python3 -m pytest tests/ -v`），但
> **CI 不依賴 pytest**，只使用 Python 標準函式庫的 `unittest`，因此無需 `pip install`。

### 涵蓋範圍

| 情境 | 驗證重點 |
|---|---|
| **正常交易日** | 24 組 SNAP 區塊全部由本次資料重建（以哨兵字串證明）、輸出含 `<svg>` wrapper／折線／量柱／交叉標記、數字與樣本逐一相符 |
| **非交易日／假日** | 資料未變 → `CHANGED=0`、**不寫檔**、檔案位元組不變、時間戳沿用 |
| **缺資料** | 任一端點空陣列／null／缺欄位／HTTP 500／timeout／空回應／HTML 錯誤頁 → **非 0 結束且原檔位元組完全不變**，且不留暫存檔 |
| **資料格式異常** | 千分位逗號、空字串、`--`、Unicode 負號、非數字、HTML 標籤、欄位數不足、除以零 → 個股層級「排除」而非崩潰 |
| **時間戳邏輯** | 資料未變沿用舊時間戳；資料有變改用本次擷取時間（避免新資料配舊時間戳） |
| **交叉事件計算** | 以可控合成資料驗證 MA5／MA20 黃金／死亡交叉的判定與日期，並確認每一筆都是真實的差值變號 |
| **不連網保證** | 將 `socket.socket` 換成會拋例外者，完整流程仍須成功 |

### 重新擷取樣本

日後若 TWSE 回應格式變動，可重新擷取（**只有這一步會連網**）：

```bash
python3 tests/capture_fixtures.py
```

---

## ⚖️ 授權

[MIT License](LICENSE)

---

## ⚠️ 投資免責聲明

As an AI Agent, I cannot provide legally protected, personalized investment advice.
This analysis is for informational purposes only and does not constitute investment advice or recommendations.
Consult a licensed financial advisor before making investment decisions.

本專案僅為資料視覺化技術示範，所有內容僅供資訊參考，**不構成任何投資建議或推薦**。
資料雖取自臺灣證券交易所公開資料，仍可能存在延遲、缺漏或解析誤差，**請勿作為交易決策的唯一依據**。
使用者應自行查證並諮詢合格財務顧問，投資決策及其風險由使用者自行承擔。
