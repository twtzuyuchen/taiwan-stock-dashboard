"""
fetch_market_data.py
======================
資料層：拉取「今日大盤/期貨情境」頁面需要的原始資料，快取為本地 CSV。分兩個獨立來源：
  1. 美股三大指數與台股加權指數：Yahoo Finance（透過 yfinance 套件），不需要 FinMind token。
  2. 台指期（TX）近月合約每日收盤：FinMind「TaiwanFuturesDaily」資料集，用跟個股資料同一組
     finmind token；是收盤後才會更新的公開資料（FinMind 官方文件說明每個交易日 16:30 更新），
     不是即時報價，符合「不需要即時資料，公開可查到的收盤價即可」的需求。

這兩個來源分開抓取、各自獨立包 try/except：任一個抓失敗只會少那一部分的卡片，
不會讓另一個來源也抓不到、更不會讓 daily_update.py 整個流程中斷。

注意：此腳本需要在「可連外」的環境執行（例如 GitHub Actions runner、
你自己的電腦或 VPS）。雲端沙盒若封鎖對外連線（連不到 Yahoo Finance / FinMind），
請改在本機或 CI 執行——跟 fetch_data.py 的既有限制一致。

用法：
    python fetch_market_data.py --config ../config/config.yaml
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import time
from pathlib import Path

import pandas as pd
import yaml

# 各指數對應的 Yahoo Finance 代號
TICKERS = {
    "nasdaq": "^IXIC",   # 納斯達克綜合指數
    "sox": "^SOX",       # 費城半導體指數
    "dow": "^DJI",       # 道瓊工業指數
    "twii": "^TWII",     # 台股加權指數
}

_CONTRACT_MONTH_RE = re.compile(r"^\d{6}$")


def load_config(config_path: str) -> dict:
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def fetch_index_history(ticker: str, start_date: str, retries: int = 3, backoff: float = 1.5) -> pd.DataFrame:
    """抓單一指數的歷史日線資料，欄位標準化為 date/open/high/low/close。
    連續失敗會重試，重試完仍失敗則丟出例外，由呼叫端決定要不要略過。"""
    import yfinance as yf  # 延後 import，避免沒裝 yfinance 時整支程式連命令列說明都跑不動

    last_err = None
    for attempt in range(1, retries + 1):
        try:
            raw = yf.Ticker(ticker).history(start=start_date, interval="1d", auto_adjust=False)
            if raw is None or raw.empty:
                raise RuntimeError(f"[{ticker}] Yahoo Finance 回傳空資料")
            df = raw.reset_index()[["Date", "Open", "High", "Low", "Close"]].copy()
            df["date"] = pd.to_datetime(df["Date"]).dt.strftime("%Y-%m-%d")
            df = df.rename(columns={"Open": "open", "High": "high", "Low": "low", "Close": "close"})
            return df[["date", "open", "high", "low", "close"]].sort_values("date").reset_index(drop=True)
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(backoff ** attempt)
    raise RuntimeError(f"[{ticker}] 抓取失敗，已重試 {retries} 次: {last_err}")


_SESSION_LABELS = {"position": "day", "after_market": "night"}  # FinMind 官方文件範例資料實際出現的字串值


def _select_front_month_close(raw_df: pd.DataFrame) -> pd.DataFrame:
    """從 FinMind TaiwanFuturesDaily 原始資料（同時包含近月／遠月等所有合約月份、以及日盤／夜盤
    兩個交易時段）篩選出「近月合約」的每日代表報價，日盤與夜盤都保留（不互相取代），用一個新的
    session 欄位（day/night）標示，交給下游決定要看哪個時段：
      1) contract_date 是標準 YYYYMM 六碼月合約格式的才留下（排除可能存在的週選擇權等其他代碼）；
      2) 用 trading_session 欄位判斷日盤／夜盤（"position"=日盤、"after_market"=夜盤，這是 FinMind
         官方文件範例資料裡實際出現的字串值）；欄位不存在或值無法辨識時，退回用成交量排序當備援
         （同一天同一近月合約若有兩筆，量大的視為日盤、量小的視為夜盤，因為日盤成交量通常遠大於夜盤）；
      3) 同一天、同一時段裡，contract_date（尚未到期的合約月份）最小的就是近月合約；
      4) 收盤價優先採用 settlement_price（結算價，官方公開用於保證金結算的每日收盤參考價），
         沒有結算價才退回用 close 欄位（夜盤通常沒有官方結算價，會自動退回用 close）。"""
    if raw_df is None or raw_df.empty:
        return pd.DataFrame()

    df = raw_df.copy()
    required = {"date", "contract_date", "open", "max", "min", "close", "volume"}
    if not required.issubset(df.columns):
        return pd.DataFrame()

    df["contract_date"] = df["contract_date"].astype(str).str.strip()
    df = df[df["contract_date"].str.match(_CONTRACT_MONTH_RE)]
    if df.empty:
        return pd.DataFrame()

    df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0)

    if "trading_session" in df.columns and df["trading_session"].isin(_SESSION_LABELS).any():
        df["session"] = df["trading_session"].map(_SESSION_LABELS)
        df = df[df["session"].notna()]
    else:
        # 備援：trading_session 欄位不存在或值無法辨識時，用成交量排序判斷，
        # 同一天同一近月合約若有兩筆，量大的視為日盤、量小的視為夜盤（僅為近似判斷）。
        df["_rank"] = df.groupby("date")["volume"].rank(method="first", ascending=False)
        df["session"] = df["_rank"].map({1: "day", 2: "night"})
        df = df[df["session"].notna()]

    min_contract = df.groupby(["date", "session"])["contract_date"].transform("min")
    front = df[df["contract_date"] == min_contract].copy()
    # 理論上篩到這裡同一天同一時段只會剩一筆，保險起見仍用成交量去重，避免重複列
    front = front.sort_values("volume", ascending=False).drop_duplicates(subset=["date", "session"], keep="first")

    front = front.rename(columns={"max": "high", "min": "low"})
    for col in ("open", "high", "low", "close"):
        front[col] = pd.to_numeric(front[col], errors="coerce")
    if "settlement_price" in front.columns:
        settlement = pd.to_numeric(front["settlement_price"], errors="coerce")
        front["close"] = settlement.where(settlement > 0, front["close"])

    front = front.dropna(subset=["open", "high", "low", "close"])
    keep_cols = ["date", "session", "open", "high", "low", "close", "volume", "contract_date"]
    return front[keep_cols].sort_values(["date", "session"]).reset_index(drop=True)


def fetch_txf_front_month(token: str, start_date: str) -> pd.DataFrame:
    """抓台指期（TX）近月合約每日收盤資料，來源是 FinMind「TaiwanFuturesDaily」資料集，
    跟個股資料共用同一組 finmind token。fetch_dataset() 本身已經有重試與錯誤處理邏輯
    （來自 fetch_data.py），這裡不需要再包一層重試。"""
    from fetch_data import fetch_dataset  # 延後 import，避免沒有 requests 環境時整支程式連命令列說明都跑不動

    raw = fetch_dataset("TaiwanFuturesDaily", "TX", start_date, token)
    front = _select_front_month_close(raw)
    if front.empty:
        raise RuntimeError("TaiwanFuturesDaily 回傳資料經篩選近月合約後為空（可能是還沒到當日更新時間，或近期沒有標準月合約資料）")
    return front


def fetch_all_market_data(config: dict, cache_dir: str = "output/cache") -> dict[str, pd.DataFrame]:
    market_cfg = config.get("market_overview", {})
    # 迴歸模型需要較長的歷史資料才有統計意義，所以抓取天數採用 regression_lookback_days
    # （預設約3年）而不是只抓 key_level 用得到的近期天數，寧可多抓、事後由計算端自行決定要用多少。
    lookback_days = market_cfg.get("regression_lookback_days", 1095)
    start_date = (dt.date.today() - dt.timedelta(days=lookback_days)).isoformat()

    Path(cache_dir).mkdir(parents=True, exist_ok=True)
    result = {}
    for key, ticker in TICKERS.items():
        try:
            df = fetch_index_history(ticker, start_date)
        except Exception as e:
            print(f"  ✗ {key:8s} ({ticker}): 抓取失敗，略過此指數 -> {e}")
            continue
        out_path = Path(cache_dir) / f"market_{key}.csv"
        df.to_csv(out_path, index=False, encoding="utf-8-sig")
        result[key] = df
        print(f"  ✓ {key:8s} ({ticker}): {len(df)} 筆 -> {out_path}")

    # 台指期近月合約：獨立於上面 yfinance 迴圈之外，來源是 FinMind，這裡失敗不該影響
    # 已經抓到的四個指數資料，所以獨立包一層 try/except。
    token = config.get("finmind", {}).get("token", "")
    try:
        txf_df = fetch_txf_front_month(token, start_date)
        out_path = Path(cache_dir) / "market_txf.csv"
        txf_df.to_csv(out_path, index=False, encoding="utf-8-sig")
        result["txf"] = txf_df
        print(f"  ✓ txf      (TaiwanFuturesDaily/TX): {len(txf_df)} 筆 -> {out_path}")
    except Exception as e:
        print(f"  ✗ txf      (TaiwanFuturesDaily/TX): 抓取失敗，略過 -> {e}")

    return result


def main():
    parser = argparse.ArgumentParser(description="從 Yahoo Finance 抓取美股三大指數與台股加權指數歷史資料")
    parser.add_argument("--config", default="config/config.yaml")
    parser.add_argument("--cache-dir", default="output/cache")
    args = parser.parse_args()

    config = load_config(args.config)
    print("\n=== 抓取大盤情境資料（納斯達克／費半／道瓊／加權指數）===")
    fetch_all_market_data(config, args.cache_dir)


if __name__ == "__main__":
    main()
