#!/usr/bin/env python3
import json
import math
import re
import time
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
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

TWSE_MI_INDEX_URL = "https://www.twse.com.tw/exchangeReport/MI_INDEX"
TWSE_MI_MARGN_URL = "https://www.twse.com.tw/exchangeReport/MI_MARGN"
TPEX_HIGHLIGHT_URL = (
    "https://www.tpex.org.tw/web/stock/aftertrading/market_highlight/highlight_result.php"
)

# 第一次執行時回補約 3 個月交易日。之後只補缺少的日期。
INITIAL_BACKFILL_TRADING_DAYS = 66
HTTP_DELAY_SECONDS = 0.18
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

    for attempt in range(HTTP_RETRIES):
        try:
            request = Request(
                full_url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
                    "Referer": "https://www.twse.com.tw/",
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
    date_compact = date_iso.replace("-", "")
    payload = http_json(
        TWSE_MI_INDEX_URL,
        {"response": "json", "date": date_compact, "type": "ALLBUT0999"},
    )

    if payload.get("stat") not in {None, "OK"}:
        raise RuntimeError(f"TWSE MI_INDEX {date_iso}: {payload.get('stat')}")

    breadth: dict[str, Any] | None = None
    prices: dict[str, float] = {}

    for table in iter_twse_tables(payload):
        fields = [str(item).strip() for item in table.get("fields", [])]
        rows = table.get("data", [])
        title = str(table.get("title", ""))

        if "股票" in fields and ("類型" in fields or "漲跌證券數合計" in title):
            stock_index = fields.index("股票")
            temp: dict[str, Any] = {}
            for row in rows:
                if not isinstance(row, list) or len(row) <= stock_index:
                    continue
                label = str(row[0]).strip()
                count, limit = parse_count_with_limit(row[stock_index])
                if label.startswith("上漲"):
                    temp["advance"] = count
                    temp["limit_up"] = limit
                elif label.startswith("下跌"):
                    temp["decline"] = count
                    temp["limit_down"] = limit
                elif label.startswith("持平"):
                    temp["unchanged"] = count
                elif label.startswith("未成交"):
                    temp["unmatched"] = count
                elif label.startswith("無比價"):
                    temp["no_comparison"] = count

            if temp.get("advance") is not None and temp.get("decline") is not None:
                breadth = temp

        code_field = next((field for field in fields if field in {"證券代號", "代號"}), None)
        close_field = next((field for field in fields if field == "收盤價"), None)
        if code_field and close_field:
            code_index = fields.index(code_field)
            close_index = fields.index(close_field)
            for row in rows:
                if not isinstance(row, list) or len(row) <= max(code_index, close_index):
                    continue
                code = str(row[code_index]).strip()
                close = parse_number(row[close_index])
                if code and close is not None and close > 0:
                    prices[code] = close

    if breadth is None:
        raise RuntimeError(f"TWSE breadth not found for {date_iso}")
    if not prices:
        raise RuntimeError(f"TWSE close prices not found for {date_iso}")

    return breadth, prices


def fetch_twse_margin_summary(date_iso: str) -> dict[str, float]:
    date_compact = date_iso.replace("-", "")
    payload = http_json(
        TWSE_MI_MARGN_URL,
        {"response": "json", "date": date_compact, "selectType": "MS"},
    )

    for table in iter_twse_tables(payload):
        fields = [str(item).strip() for item in table.get("fields", [])]
        rows = table.get("data", [])
        if "前日餘額" not in fields or "今日餘額" not in fields:
            continue

        previous_index = fields.index("前日餘額")
        today_index = fields.index("今日餘額")
        for row in rows:
            if not isinstance(row, list) or not row:
                continue
            label = str(row[0]).replace(" ", "")
            if "融資金額" not in label:
                continue

            previous_thousand = parse_number(row[previous_index])
            today_thousand = parse_number(row[today_index])
            if previous_thousand is None or today_thousand is None:
                break

            return {
                "previous_thousand_ntd": previous_thousand,
                "today_thousand_ntd": today_thousand,
                "balance_100m": today_thousand / 100000.0,
                "change_100m": (today_thousand - previous_thousand) / 100000.0,
            }

    raise RuntimeError(f"TWSE margin summary not found for {date_iso}")


def fetch_twse_margin_positions(date_iso: str) -> tuple[dict[str, int], int | None]:
    date_compact = date_iso.replace("-", "")
    payload = http_json(
        TWSE_MI_MARGN_URL,
        {"response": "json", "date": date_compact, "selectType": "ALL"},
    )

    for table in iter_twse_tables(payload):
        fields = [str(item).strip() for item in table.get("fields", [])]
        rows = table.get("data", [])
        if len(fields) < 10 or "名稱" not in fields:
            continue

        code_index = next(
            (index for index, field in enumerate(fields) if field in {"代號", "股票代號", "證券代號"}),
            None,
        )
        today_indices = [index for index, field in enumerate(fields) if field == "今日餘額"]
        if code_index is None or len(today_indices) < 1:
            continue

        margin_today_index = today_indices[0]
        positions: dict[str, int] = {}
        total_lots: int | None = None

        for row in rows:
            if not isinstance(row, list) or len(row) <= margin_today_index:
                continue

            code = str(row[code_index]).strip()
            name = str(row[fields.index("名稱")]).strip()
            balance = parse_number(row[margin_today_index])
            if balance is None:
                continue

            lots = int(round(balance))
            if name == "合計" or code in {"", "　"}:
                total_lots = lots
                continue
            if code:
                positions[code] = lots

        if positions:
            return positions, total_lots

    raise RuntimeError(f"TWSE margin positions not found for {date_iso}")


def roc_date(date_iso: str) -> str:
    dt = datetime.strptime(date_iso, "%Y-%m-%d")
    return f"{dt.year - 1911:03d}/{dt.month:02d}/{dt.day:02d}"


def strip_html_to_text(raw_html: str) -> str:
    text = re.sub(r"<script\b[^>]*>.*?</script>", " ", raw_html, flags=re.I | re.S)
    text = re.sub(r"<style\b[^>]*>.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " | ", text)
    text = unescape(text)
    text = re.sub(r"\s+", " ", text)
    return text


def extract_labeled_int(text: str, label: str) -> int | None:
    match = re.search(rf"{re.escape(label)}\s*\|?\s*([0-9,]+)", text)
    if not match:
        return None
    return int(match.group(1).replace(",", ""))


def fetch_tpex_breadth(date_iso: str) -> dict[str, int]:
    raw_html = http_text(
        TPEX_HIGHLIGHT_URL,
        {"l": "zh-tw", "o": "htm", "d": roc_date(date_iso)},
    )
    text = strip_html_to_text(raw_html)

    values = {
        "advance": extract_labeled_int(text, "上漲家數"),
        "limit_up": extract_labeled_int(text, "漲停家數"),
        "decline": extract_labeled_int(text, "下跌家數"),
        "limit_down": extract_labeled_int(text, "跌停家數"),
        "unchanged": extract_labeled_int(text, "平盤家數"),
        "unmatched": extract_labeled_int(text, "未成交含暫停交易家數"),
    }

    if values["advance"] is None or values["decline"] is None:
        raise RuntimeError(f"TPEx breadth not found for {date_iso}")

    return {key: int(value or 0) for key, value in values.items()}


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
    for index, date_iso in enumerate(missing_dates, start=1):
        print(f"TW market {index}/{len(missing_dates)}: {date_iso}")
        try:
            existing[date_iso] = fetch_tw_market_day(date_iso)
        except Exception as exc:
            failed_dates.append(date_iso)
            print(f"WARNING: TW market {date_iso} skipped: {exc}")

    rows = [existing[date] for date in sorted(existing)]
    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "scope": "TWSE + TPEx stocks for breadth; TWSE listed market for margin balance/maintenance",
        "maintenance_formula": "sum(margin_balance_lots * 1000 * close) / financing_balance_ntd * 100",
        "maintenance_is_estimate": True,
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
