
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "tickers.json"
HISTORY_DIR = ROOT / "data" / "history"
SNAPSHOT_DIR = ROOT / "data" / "snapshots"

HISTORY_DIR.mkdir(parents=True, exist_ok=True)
SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def load_tickers():
    with CONFIG.open("r", encoding="utf-8") as f:
        config = json.load(f)
    return config


def fetch_history(symbol):
    """下载日线并与历史文件合并；不覆盖已有历史记录。"""
    path = HISTORY_DIR / f"{symbol}.csv"

    # 首次下载5年；之后从最近已有日期向前回补几天，
    # 以便补齐遗漏的交易日。
    start = "2020-01-01"
    if path.exists():
        old = pd.read_csv(path)
        if not old.empty and "Date" in old.columns:
            dates = pd.to_datetime(old["Date"], errors="coerce").dropna()
            if not dates.empty:
                start = (
                    dates.max() - pd.Timedelta(days=7)
                ).strftime("%Y-%m-%d")

    df = yf.download(
        symbol,
        start=start,
        interval="1d",
        auto_adjust=False,
        actions=True,
        progress=False,
        threads=False,
        timeout=30,
    )

    if df.empty:
        raise RuntimeError(f"{symbol}: 历史行情为空")

    # yfinance 某些版本即使只下载一个代码也会返回多层列名。
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    df = df.reset_index()
    df.columns = [str(c) for c in df.columns]
    df["Date"] = pd.to_datetime(df["Date"]).dt.strftime("%Y-%m-%d")
    df = df.drop_duplicates(subset=["Date"], keep="last")

    if path.exists():
        old = pd.read_csv(path)
        merged = pd.concat([old, df], ignore_index=True)
        merged["Date"] = pd.to_datetime(
            merged["Date"], errors="coerce"
        ).dt.strftime("%Y-%m-%d")
        merged = merged.dropna(subset=["Date"])
        merged = merged.drop_duplicates(subset=["Date"], keep="last")
        merged = merged.sort_values("Date")
    else:
        merged = df.sort_values("Date")

    merged.to_csv(path, index=False)
    return len(df), len(merged)


def fetch_snapshot(symbol, market):
    """尝试读取快照；不把最近收盘价伪装成实时价。"""
    captured_at = utc_now()
    result = {
        "symbol": symbol,
        "market": market,
        "captured_at_utc": captured_at,
        "provider": "Yahoo Finance via yfinance",
        "status": "unavailable",
        "price": None,
        "currency": None,
        "previous_close": None,
        "error": None,
    }

    try:
        ticker = yf.Ticker(symbol)
        info = ticker.fast_info

        price = info.get("last_price")
        previous_close = info.get("previous_close")
        currency = info.get("currency")

        def number_or_none(value):
            try:
                value = float(value)
                return value if pd.notna(value) else None
            except (TypeError, ValueError):
                return None

        result.update({
            "price": number_or_none(price),
            "previous_close": number_or_none(previous_close),
            "currency": str(currency) if currency else None,
        })

        if result["price"] is not None:
            result["status"] = "price_returned_time_unverified"
        else:
            result["error"] = "Provider returned no usable last price"

    except Exception as exc:
        result["error"] = str(exc)[:500]

    return result


def main():
    config = load_tickers()
    snapshots = []
    errors = []

    for market, symbols in config.items():
        for symbol in symbols:
            print(f"[{market}] {symbol}")

            try:
                fetched, total = fetch_history(symbol)
                print(f"  History: fetched={fetched}, stored={total}")
            except Exception as exc:
                errors.append({
                    "symbol": symbol,
                    "stage": "history",
                    "error": str(exc)[:500],
                })
                print(f"  History ERROR: {exc}")

            snapshots.append(fetch_snapshot(symbol, market))
            time.sleep(1)

    output = {
        "generated_at_utc": utc_now(),
        "provider": "Yahoo Finance via yfinance",
        "snapshots": snapshots,
        "errors": errors,
    }

    target = SNAPSHOT_DIR / "latest.json"
    target.write_text(
        json.dumps(output, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"Snapshot written: {target}")
    print(f"Symbols: {len(snapshots)}, errors: {len(errors)}")

    # 仅当历史行情全部失败时，才让任务失败；
    # 快照单独失败会在 JSON 中记录，方便监控。
    if snapshots and all(s["status"] == "unavailable" for s in snapshots):
        if errors:
            raise RuntimeError("All history downloads failed")

    if not snapshots:
        raise RuntimeError("No symbols configured")


if __name__ == "__main__":
    main()
