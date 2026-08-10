#!/usr/bin/env python3
import json
from datetime import datetime, timezone
from pathlib import Path

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

OUT_FILE = Path(__file__).resolve().parents[1] / "data" / "markets.json"


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


def download_market(symbol: str) -> list[dict]:
    df = yf.download(
        symbol,
        period="10y",
        interval="1d",
        auto_adjust=False,
        progress=False,
        threads=False,
    )

    close = extract_close_series(df, symbol)
    points = []
    for timestamp, value in close.items():
        date = pd.Timestamp(timestamp).date().isoformat()
        points.append({"date": date, "close": round(float(value), 4)})

    if not points:
        raise RuntimeError(f"{symbol}: no usable data")

    return points


def main() -> None:
    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "latest_date": None,
        "markets": {},
    }

    latest_dates = []
    failures = []

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

    # Different countries have different holidays/time zones. Use the latest date
    # available among all markets only as the dashboard's overall reference date.
    payload["latest_date"] = max(latest_dates)

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    print(f"Wrote {OUT_FILE}")
    if failures:
        print("Some optional markets failed:")
        for failure in failures:
            print(f"  - {failure}")


if __name__ == "__main__":
    main()
