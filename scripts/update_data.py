#!/usr/bin/env python3
import json
import math
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

import pandas as pd
import yfinance as yf

MARKETS = {
    "TWII": {"name": "台灣加權", "symbol": "^TWII"},
    "N225": {"name": "日經 225", "symbol": "^N225"},
    "KOSPI": {"name": "KOSPI", "symbol": "^KS11"},
    "SP500": {"name": "S&P 500", "symbol": "^GSPC"},
    "NASDAQ": {"name": "Nasdaq", "symbol": "^IXIC"},
    "SOX": {"name": "費城半導體", "symbol": "^SOX"},
}

ROOT = Path(__file__).resolve().parents[1]
MARKETS_OUT_FILE = ROOT / "data" / "markets.json"
TW_MARKET_OUT_FILE = ROOT / "data" / "tw_market.json"

TWSE_MI_INDEX_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
TWSE_MI_MARGN_URL = "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN"
TPEX_HIGHLIGHT_URL = "https://www.tpex.org.tw/www/zh-tw/afterTrading/highlight"

# 第一次執行時回補約 3 個月交易日。之後只補缺少的日期。
INITIAL_BACKFILL_TRADING_DAYS = 66
HTTP_DELAY_SECONDS = 0.35
HTTP_RETRIES = 3
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151 Safari/537.36"
)


def extract_close_series(df: pd.DataFrame, symbol: str) -> pd.Series:
    if df.empty:
        raise RuntimeError(f"{symbol}: empty dataframe")

    if isinstance(df.columns, pd.MultiIndex):
        if ("Close", symbol) in df.columns:
            close = df[("Close", symbol)]
        elif "Close" in df.columns.get_level_values(0):
            close = df["Close"].iloc[:, 0]
        else:
            raise RuntimeError(f"{symbol}: Close column missing")
    else:
        if "Close" not in df.columns:
            raise RuntimeError(f"{symbol}: Close column missing")
        close = df["Close"]

    return close.dropna()


def download_market(symbol: str) -> list[dict[str, Any]]:
    df = yf.download(
        symbol,
        period="10y",
        interval="1d",
        auto_adjust=False,
        progress=False,
        threads=False,
    )

    close = extract_close_series(df, symbol)
    points: list[dict[str, Any]] = []
    for timestamp, value in close.items():
        date = pd.Timestamp(timestamp).date().isoformat()
        points.append({"date": date, "close": round(float(value), 4)})

    if not points:
        raise RuntimeError(f"{symbol}: no usable data")

    return points


def http_text(url: str, params: dict[str, str]) -> str:
    full_url = f"{url}?{urlencode(params)}"
    last_error: Exception | None = None
    host = urlparse(url).netloc.lower()
    referer = "https://www.tpex.org.tw/" if "tpex.org.tw" in host else "https://www.twse.com.tw/"

    for attempt in range(HTTP_RETRIES):
        try:
            request = Request(
                full_url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json,text/plain,*/*",
                    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.7",
                    "Referer": referer,
                    "Connection": "keep-alive",
                },
            )
            with urlopen(request, timeout=30) as response:
                raw = response.read()
                text = raw.decode("utf-8-sig", errors="replace")
            time.sleep(HTTP_DELAY_SECONDS)
            return text
        except Exception as exc:
            last_error = exc
            if attempt + 1 < HTTP_RETRIES:
                time.sleep(1.5 * (attempt + 1))

    raise RuntimeError(f"HTTP failed: {full_url}: {last_error}")


def http_json(url: str, params: dict[str, str]) -> dict[str, Any]:
    text = http_text(url, params)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON from {url}: {text[:160]!r}") from exc

    if not isinstance(data, dict):
        raise RuntimeError(f"Unexpected JSON type from {url}: {type(data)!r}")
    return data


def parse_number(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None

    text = str(value).strip().replace(",", "").replace("=", "")
    if text in {"", "-", "--", "---", "N/A", "nan", "None"}:
        return None

    match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    if not match:
        return None

    try:
        number = float(match.group(0))
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def parse_count_with_limit(value: Any) -> tuple[int | None, int | None]:
    text = str(value or "").replace(",", "")
    match = re.search(r"(\d+)\s*(?:\((\d+)\))?", text)
    if not match:
        return None, None
    count = int(match.group(1))
    limit = int(match.group(2)) if match.group(2) is not None else None
    return count, limit


def iter_twse_tables(payload: dict[str, Any]):
    tables = payload.get("tables")
    if isinstance(tables, list):
        for table in tables:
            if isinstance(table, dict):
                yield table

    for index in range(1, 30):
        fields = payload.get(f"fields{index}")
        data = payload.get(f"data{index}")
        if isinstance(fields, list) and isinstance(data, list):
            yield {
                "title": payload.get(f"title{index}") or payload.get(f"subtitle{index}") or "",
                "fields": fields,
                "data": data,
            }

    fields = payload.get("fields")
    data = payload.get("data")
    if isinstance(fields, list) and isinstance(data, list):
        yield {
            "title": payload.get("title", ""),
            "fields": fields,
            "data": data,
        }


def fetch_twse_day(date_iso: str) -> tuple[dict[str, Any], dict[str, float]]:
    """取得上市股票市場廣度與當日收盤價。

    目前 TWSE RWD MI_INDEX 的市場廣度位於 tables[7]，個股行情位於 tables[8]。
    同時保留欄位名稱 fallback，避免欄位順序小幅調整時整批失效。
    """
    date_compact = date_iso.replace("-", "")
    payload = http_json(
        TWSE_MI_INDEX_URL,
        {"response": "json", "date": date_compact, "type": "ALLBUT0999"},
    )

    if payload.get("stat") != "OK":
        raise RuntimeError(f"TWSE MI_INDEX {date_iso}: {payload.get('stat')!r}")

    tables = payload.get("tables")
    if not isinstance(tables, list) or len(tables) < 9:
        raise RuntimeError(f"TWSE MI_INDEX {date_iso}: unexpected tables layout")

    # 市場廣度：上漲、下跌、持平、未成交、無比價；第三欄為「股票」。
    breadth_rows = tables[7].get("data", []) if isinstance(tables[7], dict) else []
    if len(breadth_rows) < 3:
        raise RuntimeError(f"TWSE breadth rows missing for {date_iso}")

    raw_counts: list[Any] = []
    for row in breadth_rows:
        if isinstance(row, list) and len(row) >= 3:
            raw_counts.append(row[2])

    if len(raw_counts) < 3:
        raise RuntimeError(f"TWSE stock breadth column missing for {date_iso}")

    advance, limit_up = parse_count_with_limit(raw_counts[0])
    decline, limit_down = parse_count_with_limit(raw_counts[1])
    unchanged, _ = parse_count_with_limit(raw_counts[2])
    unmatched, _ = parse_count_with_limit(raw_counts[3]) if len(raw_counts) > 3 else (0, None)
    no_comparison, _ = parse_count_with_limit(raw_counts[4]) if len(raw_counts) > 4 else (0, None)

    if advance is None or decline is None:
        raise RuntimeError(f"TWSE breadth parse failed for {date_iso}: {raw_counts[:5]!r}")

    breadth = {
        "advance": advance,
        "limit_up": int(limit_up or 0),
        "decline": decline,
        "limit_down": int(limit_down or 0),
        "unchanged": int(unchanged or 0),
        "unmatched": int(unmatched or 0),
        "no_comparison": int(no_comparison or 0),
    }

    quote_table = tables[8] if isinstance(tables[8], dict) else {}
    fields = [str(item).strip() for item in quote_table.get("fields", [])]
    rows = quote_table.get("data", [])
    if not isinstance(rows, list) or not rows:
        raise RuntimeError(f"TWSE close-price rows missing for {date_iso}")

    try:
        code_index = fields.index("證券代號")
    except ValueError:
        code_index = 0
    try:
        close_index = fields.index("收盤價")
    except ValueError:
        close_index = 8

    prices: dict[str, float] = {}
    for row in rows:
        if not isinstance(row, list) or len(row) <= max(code_index, close_index):
            continue
        code = str(row[code_index]).strip()
        close = parse_number(row[close_index])
        if code and close is not None and close > 0:
            prices[code] = close

    if not prices:
        raise RuntimeError(f"TWSE close prices not found for {date_iso}")

    return breadth, prices


def fetch_twse_margin_summary(date_iso: str) -> dict[str, float]:
    """取得集中市場融資金額（仟元）。"""
    date_compact = date_iso.replace("-", "")
    payload = http_json(
        TWSE_MI_MARGN_URL,
        {"response": "json", "date": date_compact, "selectType": "MS"},
    )

    if payload.get("stat") != "OK":
        raise RuntimeError(f"TWSE MI_MARGN summary {date_iso}: {payload.get('stat')!r}")

    tables = payload.get("tables")
    if not isinstance(tables, list) or not tables or not isinstance(tables[0], dict):
        raise RuntimeError(f"TWSE margin summary table missing for {date_iso}")

    rows = tables[0].get("data", [])
    for row in rows:
        if not isinstance(row, list) or len(row) < 3:
            continue
        label = str(row[0]).replace(" ", "")
        if "融資金額" not in label:
            continue

        # 官方表格最後兩欄為「前日餘額 / 今日餘額」，單位：仟元。
        previous_thousand = parse_number(row[-2])
        today_thousand = parse_number(row[-1])
        if previous_thousand is None or today_thousand is None:
            continue

        return {
            "previous_thousand_ntd": previous_thousand,
            "today_thousand_ntd": today_thousand,
            "balance_100m": today_thousand / 100000.0,
            "change_100m": (today_thousand - previous_thousand) / 100000.0,
        }

    raise RuntimeError(f"TWSE margin summary not found for {date_iso}")


def fetch_twse_margin_positions(date_iso: str) -> tuple[dict[str, int], int | None]:
    """取得集中市場逐檔融資今日餘額（張）。"""
    date_compact = date_iso.replace("-", "")
    payload = http_json(
        TWSE_MI_MARGN_URL,
        {"response": "json", "date": date_compact, "selectType": "ALL"},
    )

    if payload.get("stat") != "OK":
        raise RuntimeError(f"TWSE MI_MARGN positions {date_iso}: {payload.get('stat')!r}")

    tables = payload.get("tables")
    if not isinstance(tables, list) or len(tables) < 2 or not isinstance(tables[1], dict):
        raise RuntimeError(f"TWSE margin positions table missing for {date_iso}")

    rows = tables[1].get("data", [])
    positions: dict[str, int] = {}
    for row in rows:
        # schema: 代號, 名稱, 融資買進, 賣出, 現償, 前日餘額, 今日餘額, ...
        if not isinstance(row, list) or len(row) < 7:
            continue
        code = str(row[0]).strip()
        balance = parse_number(row[6])
        if not code or balance is None:
            continue
        positions[code] = int(round(balance))

    if not positions:
        raise RuntimeError(f"TWSE margin positions not found for {date_iso}")

    return positions, sum(positions.values())


def fetch_tpex_breadth(date_iso: str) -> dict[str, int]:
    """取得上櫃市場廣度。

    TPEx afterTrading/highlight 回傳 tables[0].data[0]；
    第 8~13 欄依序為上漲、漲停、下跌、跌停、平盤、未成交。
    """
    date_slash = date_iso.replace("-", "/")
    payload = http_json(
        TPEX_HIGHLIGHT_URL,
        {"date": date_slash, "response": "json"},
    )

    if str(payload.get("stat", "")).lower() != "ok":
        raise RuntimeError(f"TPEx highlight {date_iso}: {payload.get('stat')!r}")

    tables = payload.get("tables")
    if not isinstance(tables, list) or not tables or not isinstance(tables[0], dict):
        raise RuntimeError(f"TPEx highlight table missing for {date_iso}")

    rows = tables[0].get("data", [])
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], list) or len(rows[0]) < 13:
        raise RuntimeError(f"TPEx highlight row layout changed for {date_iso}")

    row = rows[0]
    values = {
        "advance": parse_number(row[7]),
        "limit_up": parse_number(row[8]),
        "decline": parse_number(row[9]),
        "limit_down": parse_number(row[10]),
        "unchanged": parse_number(row[11]),
        "unmatched": parse_number(row[12]),
    }
    if values["advance"] is None or values["decline"] is None:
        raise RuntimeError(f"TPEx breadth parse failed for {date_iso}: {row!r}")

    return {key: int(round(value or 0)) for key, value in values.items()}


def calculate_margin_maintenance(
    positions: dict[str, int],
    total_lots: int | None,
    prices: dict[str, float],
    financing_thousand_ntd: float,
) -> tuple[float | None, float | None]:
    if financing_thousand_ntd <= 0:
        return None, None

    market_value_ntd = 0.0
    matched_lots = 0
    all_lots = total_lots if total_lots and total_lots > 0 else sum(positions.values())

    for code, lots in positions.items():
        close = prices.get(code)
        if close is None or close <= 0 or lots <= 0:
            continue
        matched_lots += lots
        market_value_ntd += lots * 1000.0 * close

    coverage = (matched_lots / all_lots * 100.0) if all_lots > 0 else None
    maintenance = market_value_ntd / (financing_thousand_ntd * 1000.0) * 100.0

    # 資料覆蓋太低或數值明顯不合理時不發布，避免誤導。
    if coverage is None or coverage < 96.0 or not (100.0 <= maintenance <= 400.0):
        return None, coverage

    return maintenance, coverage


def merge_breadth(twse: dict[str, Any], tpex: dict[str, int]) -> dict[str, Any]:
    advance = int(twse.get("advance") or 0) + int(tpex.get("advance") or 0)
    decline = int(twse.get("decline") or 0) + int(tpex.get("decline") or 0)
    unchanged = int(twse.get("unchanged") or 0) + int(tpex.get("unchanged") or 0)
    limit_up = int(twse.get("limit_up") or 0) + int(tpex.get("limit_up") or 0)
    limit_down = int(twse.get("limit_down") or 0) + int(tpex.get("limit_down") or 0)

    ad_ratio = (advance / decline) if decline > 0 else None
    active = advance + decline
    breadth_pct = ((advance - decline) / active * 100.0) if active > 0 else None

    return {
        "advance": advance,
        "decline": decline,
        "unchanged": unchanged,
        "limit_up": limit_up,
        "limit_down": limit_down,
        "ad_ratio": round(ad_ratio, 4) if ad_ratio is not None else None,
        "breadth_pct": round(breadth_pct, 4) if breadth_pct is not None else None,
        "twse": {
            "advance": int(twse.get("advance") or 0),
            "decline": int(twse.get("decline") or 0),
            "unchanged": int(twse.get("unchanged") or 0),
            "limit_up": int(twse.get("limit_up") or 0),
            "limit_down": int(twse.get("limit_down") or 0),
        },
        "tpex": tpex,
    }


def load_existing_tw_market() -> dict[str, dict[str, Any]]:
    if not TW_MARKET_OUT_FILE.exists():
        return {}

    try:
        payload = json.loads(TW_MARKET_OUT_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    rows = payload.get("data", []) if isinstance(payload, dict) else []
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if isinstance(row, dict) and row.get("date"):
            result[str(row["date"])] = row
    return result


def fetch_tw_market_day(date_iso: str) -> dict[str, Any]:
    twse_breadth, prices = fetch_twse_day(date_iso)
    margin_summary = fetch_twse_margin_summary(date_iso)
    tpex_breadth = fetch_tpex_breadth(date_iso)

    maintenance_pct: float | None = None
    coverage_pct: float | None = None
    try:
        positions, total_lots = fetch_twse_margin_positions(date_iso)
        maintenance_pct, coverage_pct = calculate_margin_maintenance(
            positions,
            total_lots,
            prices,
            margin_summary["today_thousand_ntd"],
        )
    except Exception as exc:
        print(f"WARNING: maintenance rate unavailable for {date_iso}: {exc}")

    breadth = merge_breadth(twse_breadth, tpex_breadth)

    return {
        "date": date_iso,
        "margin_balance_100m": round(margin_summary["balance_100m"], 4),
        "margin_change_100m": round(margin_summary["change_100m"], 4),
        "margin_maintenance_pct": round(maintenance_pct, 4) if maintenance_pct is not None else None,
        "maintenance_coverage_pct": round(coverage_pct, 4) if coverage_pct is not None else None,
        **breadth,
    }


def update_global_markets() -> dict[str, Any]:
    payload: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "latest_date": None,
        "markets": {},
    }

    latest_dates: list[str] = []
    failures: list[str] = []
    for key, meta in MARKETS.items():
        print(f"Downloading {key} {meta['symbol']}...")
        try:
            points = download_market(meta["symbol"])
            payload["markets"][key] = {
                "name": meta["name"],
                "symbol": meta["symbol"],
                "data": points,
            }
            latest_dates.append(points[-1]["date"])
        except Exception as exc:
            failures.append(f"{key}: {exc}")
            print(f"WARNING: {failures[-1]}")

    if "TWII" not in payload["markets"]:
        raise RuntimeError("TWII download failed; refusing to publish incomplete core data")

    payload["latest_date"] = max(latest_dates)
    MARKETS_OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    MARKETS_OUT_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    print(f"Wrote {MARKETS_OUT_FILE}")

    if failures:
        print("Some optional markets failed:")
        for failure in failures:
            print(f"  - {failure}")

    return payload


def update_tw_market(global_payload: dict[str, Any]) -> None:
    twii = global_payload["markets"]["TWII"]["data"]
    trading_dates = [str(point["date"]) for point in twii]
    target_dates = trading_dates[-INITIAL_BACKFILL_TRADING_DAYS:]

    existing = load_existing_tw_market()
    missing_dates = [date for date in target_dates if date not in existing]

    if missing_dates:
        print(f"TW market data: {len(missing_dates)} missing date(s) to fetch")
    else:
        print("TW market data is already up to date")

    failed_dates: list[str] = []
    success_count = 0
    for index, date_iso in enumerate(missing_dates, start=1):
        print(f"TW market {index}/{len(missing_dates)}: {date_iso}")
        try:
            existing[date_iso] = fetch_tw_market_day(date_iso)
            success_count += 1
            row = existing[date_iso]
            print(
                "  OK "
                f"margin={row['margin_balance_100m']:.2f}億 "
                f"A/D={row['advance']}/{row['decline']} "
                f"ratio={row['ad_ratio']}"
            )
        except Exception as exc:
            failed_dates.append(date_iso)
            print(f"WARNING: TW market {date_iso} skipped: {exc}")

    rows = [existing[date] for date in sorted(existing)]
    if not rows:
        raise RuntimeError(
            "TW market fetch produced zero rows; refusing to publish an empty tw_market.json"
        )
    if missing_dates and success_count == 0:
        raise RuntimeError(
            "All missing TW market dates failed; existing file would be stale, refusing silent success"
        )
    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "scope": "TWSE + TPEx stock breadth; TWSE listed-market financing balance and estimated maintenance",
        "maintenance_formula": "sum(margin_balance_lots * 1000 * close) / financing_balance_ntd * 100",
        "maintenance_is_estimate": True,
        "sources": {
            "twse_breadth_prices": TWSE_MI_INDEX_URL,
            "twse_margin": TWSE_MI_MARGN_URL,
            "tpex_breadth": TPEX_HIGHLIGHT_URL,
        },
        "data": rows,
    }

    TW_MARKET_OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    TW_MARKET_OUT_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    print(f"Wrote {TW_MARKET_OUT_FILE} ({len(rows)} rows)")

    if failed_dates:
        print("Some dates were not available and will be retried on a future run:")
        for date_iso in failed_dates:
            print(f"  - {date_iso}")


def main() -> None:
    global_payload = update_global_markets()
    update_tw_market(global_payload)


if __name__ == "__main__":
    main()
