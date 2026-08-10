# 台股大盤溫度計 Dashboard - 初版

純 GitHub Pages + GitHub Actions，設計目標為額外月費 NT$0。

## 初版功能

- 台灣加權指數預設顯示
- 可疊加：日經 225、KOSPI、S&P 500、Nasdaq Composite、費城半導體 SOX
- 預設 3 個月
- 週期：1M / 3M / 6M / 1Y / 3Y / 5Y / 10Y
- 報酬率比較模式：各市場區間起點正規化為 100
- 原始指數模式
- Crosshair / Tooltip / Data Zoom
- GitHub Actions 每週一至週五台灣時間 22:30 更新
- 可在 Actions 頁面手動更新

## 架構

```text
GitHub Pages
    └── index.html
          └── data/markets.json
                   ↑
            GitHub Actions
                   ↑
               yfinance
```

## 零付費原則

此版本沒有 Cloudflare、付費 API、付費行情、付費 Server 或付費資料庫。

資料下載目前使用開源 `yfinance` 讀取 Yahoo Finance 公開資料。這不是 Yahoo 官方 API 服務，可靠度與格式可能改變；若來源日後失效，應替換為其他免費來源，而不是改用付費方案。

## 部署

1. 在 GitHub 建立 **Public repository**，例如 `market-temperature`。
2. 將本專案全部檔案上傳到 repo 根目錄。
3. 到 **Settings → Pages**。
4. Build and deployment：
   - Source: `Deploy from a branch`
   - Branch: `main`
   - Folder: `/(root)`
   - Save
5. 到 **Actions → Update market data → Run workflow**，手動執行第一次市場資料更新。
6. Workflow 完成後會建立 `data/markets.json` 並 commit 回 `main`。
7. 等 GitHub Pages 重新部署完成即可開啟網站。

一般 project Pages 網址格式：

```text
https://<GitHub帳號>.github.io/<repo名稱>/
```

## 本機測試

先安裝 Python 套件並抓資料：

```bash
pip install -r requirements.txt
python scripts/update_data.py
```

再啟動靜態 HTTP Server：

```bash
python -m http.server 8000
```

瀏覽器開啟：

```text
http://localhost:8000
```
