"""
analyze.py
==========
分析層：讀取 fetch_data.py 產出的原始 CSV，計算：
  1. 近兩週（預設 10 個交易日）主力／三大法人持倉成本
  2. 籌碼、技術、基本面燈號與綜合評分
  3. 產出給 generate_dashboard.py 使用的結構化字典

主力成本估算方法
-----------------
以三大法人（外資 + 投信 + 自營商合計）逐日買賣超「股數」為權重，
只取「淨買超」的交易日，計算成交量加權平均價（VWAP）：

    主力成本 = Σ(當日淨買超股數 × 當日收盤價)  / Σ(當日淨買超股數)
               （僅加總淨買超 > 0 的交易日，回溯 lookback_trading_days 個交易日）

這是業界常見的「籌碼成本估算」簡化模型：假設買超當天的成交是以收盤價
附近成交，藉此推估主力／法人目前部位的平均成本，可與現價比較「浮盈/浮虧」。
若要更精細，可改用日內 VWAP 或分點籌碼資料加權，但需要更細的資料來源。
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd
import yaml


def _read_cache(cache_dir: str, stock_id: str, key: str) -> pd.DataFrame:
    path = Path(cache_dir) / f"{stock_id}_{key}.csv"
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path)


def compute_institutional_cost(price_df: pd.DataFrame, inst_df: pd.DataFrame,
                                lookback_days: int = 10) -> dict:
    """計算近 N 個交易日的主力（三大法人合計）持倉成本與布局分數。"""
    if price_df.empty or inst_df.empty:
        return {"cost": None, "current_price": None, "unrealized_pct": None,
                "buy_days": 0, "score": 0}

    price_df = price_df.sort_values("date")
    inst_df = inst_df.copy()

    # FinMind InstitutionalInvestorsBuySell 欄位: date, stock_id, name(投信/外資/自營商...), buy, sell
    inst_df["net"] = inst_df["buy"] - inst_df["sell"]
    daily_net = inst_df.groupby("date")["net"].sum().reset_index()

    merged = pd.merge(daily_net, price_df[["date", "close"]], on="date", how="inner")
    merged = merged.sort_values("date").tail(lookback_days)

    buy_days = merged[merged["net"] > 0]
    total_shares = buy_days["net"].sum()

    if total_shares <= 0:
        cost = None
    else:
        cost = float((buy_days["net"] * buy_days["close"]).sum() / total_shares)

    current_price = float(price_df["close"].iloc[-1]) if not price_df.empty else None
    unrealized_pct = None
    if cost and current_price:
        unrealized_pct = round((current_price - cost) / cost * 100, 2)

    # 主力布局分數：買超天數比例(50%) + 買超力道趨勢(50%)
    n = len(merged) or 1
    buy_ratio = len(buy_days) / n
    # 力道趨勢：近半段 vs 前半段 買超股數合計，若加速買超則加分
    half = max(1, n // 2)
    recent_strength = merged.tail(half)["net"].clip(lower=0).sum()
    earlier_strength = merged.head(n - half)["net"].clip(lower=0).sum()
    momentum = 0.5
    if recent_strength + earlier_strength > 0:
        momentum = recent_strength / (recent_strength + earlier_strength)

    score = round((buy_ratio * 0.5 + momentum * 0.5) * 100)

    return {
        "cost": round(cost, 2) if cost else None,
        "current_price": current_price,
        "unrealized_pct": unrealized_pct,
        "buy_days": int(len(buy_days)),
        "total_days": int(n),
        "score": int(score),
    }


def compute_chip_cleanliness(margin_df: pd.DataFrame, lookback_days: int = 10) -> int:
    """籌碼乾淨度：融資餘額近期是否下降（籌碼安定）、券資比是否健康。"""
    if margin_df.empty:
        return 50
    margin_df = margin_df.sort_values("date").tail(lookback_days)
    if "MarginPurchaseTodayBalance" not in margin_df.columns:
        return 50
    balances = margin_df["MarginPurchaseTodayBalance"].astype(float)
    if len(balances) < 2 or balances.iloc[0] == 0:
        return 50
    change_pct = (balances.iloc[-1] - balances.iloc[0]) / balances.iloc[0]
    # 融資餘額下降 -> 籌碼安定 -> 分數高；大幅增加(融資追價) -> 分數低
    score = 70 - change_pct * 200
    return int(np.clip(score, 0, 100))


def compute_technical_trend(price_df: pd.DataFrame) -> dict:
    """簡化技術面：均線多空排列 + 長期均線乖離率。"""
    if price_df.empty or len(price_df) < 60:
        return {"trend": "資料不足", "bias_safe": True, "score": 50}

    price_df = price_df.sort_values("date")
    close = price_df["close"].astype(float)
    ma5 = close.rolling(5).mean().iloc[-1]
    ma20 = close.rolling(20).mean().iloc[-1]
    ma60 = close.rolling(60).mean().iloc[-1]
    last = close.iloc[-1]

    if ma5 > ma20 > ma60:
        trend, score = "偏多", 80
    elif ma5 < ma20 < ma60:
        trend, score = "偏空", 20
    else:
        trend, score = "盤整", 50

    bias_pct = (last - ma60) / ma60 * 100
    bias_safe = abs(bias_pct) < 20  # 乖離率 < 20% 視為安全，避免追高追空

    return {"trend": trend, "bias_pct": round(bias_pct, 1), "bias_safe": bias_safe, "score": score}


def compute_fundamental(revenue_df: pd.DataFrame, per_df: pd.DataFrame) -> dict:
    """基本面催化：月營收年增率 + PER 相對位階。"""
    score = 50
    yoy = None
    if not revenue_df.empty and "revenue" in revenue_df.columns:
        revenue_df = revenue_df.sort_values("date")
        if len(revenue_df) >= 13:
            latest = revenue_df["revenue"].iloc[-1]
            year_ago = revenue_df["revenue"].iloc[-13]
            if year_ago:
                yoy = round((latest - year_ago) / year_ago * 100, 1)
                score = 50 + np.clip(yoy, -50, 50) * 0.6

    per_percentile = None
    if not per_df.empty and "PER" in per_df.columns:
        pers = per_df["PER"].astype(float).dropna()
        if len(pers) > 5:
            per_percentile = round((pers.iloc[-1] <= pers).mean() * 100, 1)

    return {
        "revenue_yoy_pct": yoy,
        "per_percentile": per_percentile,
        "score": int(np.clip(score, 0, 100)),
    }


def score_to_light(score: int, thresholds: dict) -> str:
    if score >= thresholds.get("green", 70):
        return "green"
    if score >= thresholds.get("yellow", 40):
        return "yellow"
    return "red"


def analyze_stock(stock_id: str, config: dict, cache_dir: str = "output/cache") -> dict:
    scoring = config.get("scoring", {})
    weights = scoring.get("weights", {})
    thresholds = scoring.get("thresholds", {"green": 70, "yellow": 40})
    lookback = config.get("finmind", {}).get("lookback_trading_days", 10)

    price_df = _read_cache(cache_dir, stock_id, "price")
    inst_df = _read_cache(cache_dir, stock_id, "institutional")
    margin_df = _read_cache(cache_dir, stock_id, "margin")
    revenue_df = _read_cache(cache_dir, stock_id, "month_revenue")
    per_df = _read_cache(cache_dir, stock_id, "per")

    inst_cost = compute_institutional_cost(price_df, inst_df, lookback)
    chip_score = compute_chip_cleanliness(margin_df, lookback)
    tech = compute_technical_trend(price_df)
    fund = compute_fundamental(revenue_df, per_df)

    composite = (
        chip_score * weights.get("chip_cleanliness", 0.25)
        + inst_cost["score"] * weights.get("institutional_position", 0.30)
        + tech["score"] * weights.get("technical_trend", 0.20)
        + fund["score"] * weights.get("fundamental", 0.25)
    )
    composite = int(round(composite))

    risk_level = "低" if composite >= 70 else ("中" if composite >= 40 else "高")

    return {
        "stock_id": stock_id,
        "composite_score": composite,
        "risk_level": risk_level,
        "chip_cleanliness": {"score": chip_score, "light": score_to_light(chip_score, thresholds)},
        "institutional_position": {**inst_cost, "light": score_to_light(inst_cost["score"], thresholds)},
        "technical": {**tech, "light": score_to_light(tech["score"], thresholds)},
        "fundamental": {**fund, "light": score_to_light(fund["score"], thresholds)},
    }


def main():
    parser = argparse.ArgumentParser(description="計算主力成本與燈號評分")
    parser.add_argument("--config", default="config/config.yaml")
    parser.add_argument("--stock", required=True)
    parser.add_argument("--cache-dir", default="output/cache")
    args = parser.parse_args()

    with open(args.config, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    result = analyze_stock(args.stock, config, args.cache_dir)
    import json
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
