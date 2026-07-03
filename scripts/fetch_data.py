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
    "price": "TaiwanStockPrice",                          # 股價 OHLCV
    "margin": "TaiwanStockMarginPurchaseShortSale",        # 融資融券
    "institutional": "TaiwanStockInstitutionalInvestorsBuySell",  # 三大法人買賣超
    "shareholding": "TaiwanStockHoldingSharesPer",          # 股權分散表（週資料）
    "month_revenue": "TaiwanStockMonthRevenue",             # 月營收
    "financial_statements": "TaiwanStockFinancialStatements",  # 財報（損益）
    "balance_sheet": "TaiwanStockBalanceSheet",              # 資產負債表
    "per": "TaiwanStockPER",                                 # 本益比/殖利率/淨值比
}


def load_config(config_path: str) -> dict:
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def fetch_dataset(dataset: str, stock_id: str, start_date: str, token: str,
                   retries: int = 3, backoff: float = 1.5) -> pd.DataFrame:
    """呼叫 FinMind v4 API 並回傳 DataFrame，內建重試與 402(額度用盡)偵測。"""
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
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt < retries:
                time.sleep(backoff ** attempt)
    raise RuntimeError(f"[{dataset}] 抓取失敗，已重試 {retries} 次: {last_err}")


def fetch_all(stock_id: str, config: dict, cache_dir: str = "output/cache") -> dict[str, pd.DataFrame]:
    token = config.get("finmind", {}).get("token", "")
    lookback_days = config.get("finmind", {}).get("lookback_trading_days", 10)

    # 財務類資料抓長一點的區間（近一年），籌碼類抓近兩個月即可覆蓋兩週分析+比較基期
    today = dt.date.today()
    chip_start = (today - dt.timedelta(days=60)).isoformat()
    fin_start = (today - dt.timedelta(days=400)).isoformat()

    start_by_key = {
        "price": chip_start,
        "margin": chip_start,
        "institutional": chip_start,
        "shareholding": chip_start,
        "month_revenue": fin_start,
        "financial_statements": fin_start,
        "balance_sheet": fin_start,
        "per": chip_start,
    }

    Path(cache_dir).mkdir(parents=True, exist_ok=True)
    result = {}
    for key, dataset in DATASETS.items():
        df = fetch_dataset(dataset, stock_id, start_by_key[key], token)
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
