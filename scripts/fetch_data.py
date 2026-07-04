"""
fetch_data.py
==============
資料層：向 FinMind API 拉取指定個股所需的原始資料，並快取為本地 CSV。

注意：此腳本需要在「可連外」的環境執行（例如 GitHub Actions runner、
你自己的電腦或 VPS）。雲端沙盒若封鎖對外連線，請改在本機或 CI 執行。

用法：
    python fetch_data.py --config ../config/config.yaml --stock 2618
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import time
from pathlib import Path

import pandas as pd
import requests
import yaml

FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"

# 各儀表板區塊對應的 FinMind dataset
DATASETS = {
    "price": "TaiwanStockPrice",
    "margin": "TaiwanStockMarginPurchaseShortSale",
    "institutional": "TaiwanStockInstitutionalInvestorsBuySell",
    "shareholding": "TaiwanStockHoldingSharesPer",
    "month_revenue": "TaiwanStockMonthRevenue",
    "financial_statements": "TaiwanStockFinancialStatements",
    "balance_sheet": "TaiwanStockBalanceSheet",
    "per": "TaiwanStockPER",
}


def load_config(config_path: str) -> dict:
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def fetch_dataset(dataset: str, stock_id: str, start_date: str, token: str,
                   retries: int = 3, backoff: float = 1.5) -> pd.DataFrame:
    params = {
        "dataset": dataset,
        "data_id": stock_id,
        "start_date": start_date,
    }
    if token:
        params["token"] = token

    last_err = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(FINMIND_URL, params=params, timeout=20)
            if resp.status_code == 402:
                raise RuntimeError(
                    f"[{dataset}] FinMind 額度已用盡 (HTTP 402)。"
                    "請稍後再試、註冊/升級帳號，或降低抓取頻率。"
                )
            resp.raise_for_status()
            payload = resp.json()
            if payload.get("status") != 200 and "data" not in payload:
                raise RuntimeError(f"[{dataset}] API 回應異常: {payload.get('msg')}")
            return pd.DataFrame(payload.get("data", []))
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(backoff ** attempt)
    raise RuntimeError(f"[{dataset}] 抓取失敗，已重試 {retries} 次: {last_err}")


def fetch_all(stock_id: str, config: dict, cache_dir: str = "output/cache") -> dict[str, pd.DataFrame]:
    token = config.get("finmind", {}).get("token", "")

    today = dt.date.today()
    # 技術面均線(MA60)需要至少60個「交易日」，60個「日曆天」扣掉週末/假日常常不夠，
    # 抓寬一點（100天）確保有足夠交易日；margin/institutional/shareholding 這些子項
    # 用到的回溯天數(lookback_trading_days、holder_lookback_snapshots)遠小於100天，同樣受惠。
    chip_start = (today - dt.timedelta(days=100)).isoformat()
    fin_start = (today - dt.timedelta(days=400)).isoformat()
    # 「本益比河流圖」估價模型需要數年本益比歷史才有統計意義，不能沿用短天期的 chip_start，
    # 另外用獨立、可設定的回溯天數（預設約3年）。
    valuation_lookback_days = config.get("finmind", {}).get("valuation_lookback_days", 1095)
    valuation_start = (today - dt.timedelta(days=valuation_lookback_days)).isoformat()

    start_by_key = {
        "price": chip_start,
        "margin": chip_start,
        "institutional": chip_start,
        "shareholding": chip_start,
        "month_revenue": fin_start,
        "financial_statements": fin_start,
        "balance_sheet": fin_start,
        "per": valuation_start,
    }

    Path(cache_dir).mkdir(parents=True, exist_ok=True)
    result = {}
    for key, dataset in DATASETS.items():
        try:
            df = fetch_dataset(dataset, stock_id, start_by_key[key], token)
        except Exception as e:
            print(f"  ✗ {key:22s} ({dataset}): 抓取失敗，略過此資料集 -> {e}")
            continue
        out_path = Path(cache_dir) / f"{stock_id}_{key}.csv"
        df.to_csv(out_path, index=False, encoding="utf-8-sig")
        result[key] = df
        print(f"  ✓ {key:22s} ({dataset}): {len(df)} 筆 -> {out_path}")
    return result


def main():
    parser = argparse.ArgumentParser(description="從 FinMind 抓取個股籌碼/財務資料")
    parser.add_argument("--config", default="config/config.yaml")
    parser.add_argument("--stock", required=False, help="單一股票代號，未指定則抓 watchlist 全部")
    args = parser.parse_args()

    config = load_config(args.config)
    stocks = [args.stock] if args.stock else [w["stock_id"] for w in config["watchlist"]]

    for stock_id in stocks:
        print(f"\n=== 抓取 {stock_id} ===")
        fetch_all(stock_id, config)


if __name__ == "__main__":
    main()
