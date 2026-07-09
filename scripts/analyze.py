from __future__ import annotations

import argparse
import re
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from signals import compute_all_signals


def _read_cache(cache_dir: str, stock_id: str, key: str) -> pd.DataFrame:
    path = Path(cache_dir) / f"{stock_id}_{key}.csv"
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except pd.errors.EmptyDataError:
        # FinMind 對該資料集回傳完全空白的資料（例如某些個股沒有月營收紀錄），
        # 存成的 CSV 沒有任何欄位，讀取會失敗，視同沒有資料即可，不應讓整體流程中斷
        return pd.DataFrame()


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


def _score_margin_momentum(margin_df: pd.DataFrame, lookback_days: int) -> dict:
    """子項一：融資餘額動能。近 N 個交易日融資餘額下降 -> 籌碼安定 -> 分數高；
    大幅增加(融資追價)-> 分數低。與原本邏輯完全相同，僅抽成獨立函式方便組合。"""
    if margin_df.empty or "MarginPurchaseTodayBalance" not in margin_df.columns:
        return {"score": None, "change_pct": None}

    df = margin_df.sort_values("date").tail(lookback_days)
    balances = df["MarginPurchaseTodayBalance"].astype(float)
    if len(balances) < 2 or balances.iloc[0] == 0:
        return {"score": None, "change_pct": None}

    change_pct = (balances.iloc[-1] - balances.iloc[0]) / balances.iloc[0]
    score = int(np.clip(70 - change_pct * 200, 0, 100))
    return {"score": score, "change_pct": round(change_pct * 100, 1)}


def _score_margin_utilization(margin_df: pd.DataFrame, safe: float = 0.5, danger: float = 0.9) -> dict:
    """子項二：融資使用率（融資餘額 / 融資限額）。使用率越接近上限，
    代表一旦股價下跌，越容易觸發追繳/斷頭賣壓，籌碼風險越高。"""
    if margin_df.empty:
        return {"score": None, "utilization_pct": None}
    cols = {"MarginPurchaseTodayBalance", "MarginPurchaseLimit"}
    if not cols.issubset(margin_df.columns):
        return {"score": None, "utilization_pct": None}

    latest = margin_df.sort_values("date").iloc[-1]
    limit = float(latest["MarginPurchaseLimit"])
    if limit <= 0:
        return {"score": None, "utilization_pct": None}

    utilization = float(latest["MarginPurchaseTodayBalance"]) / limit
    # safe(含)以下滿分；danger(含)以上 0 分；中間線性內插
    if danger <= safe:
        danger = safe + 0.01
    score = (danger - utilization) / (danger - safe) * 100
    score = int(np.clip(score, 0, 100))
    return {"score": score, "utilization_pct": round(utilization * 100, 1)}


_LEVEL_LOWER_BOUND_RE = re.compile(r"([\d,]+)")


def _score_holder_concentration(shareholding_df: pd.DataFrame, big_holder_min_shares: int = 400_000,
                                 lookback_snapshots: int = 4) -> dict:
    """子項三：大戶持股集中度趨勢。資料源 TaiwanStockHoldingSharesPer（集保戶股權分散表，每週更新）。
    加總「持股張數下限 >= big_holder_min_shares（預設 400,001 股，即約 400 張）」各級距的 percent，
    追蹤這個大戶持股比例最近幾次報告是上升/持平還是下降：上升或持平 -> 籌碼安定由大股東/法人主導 -> 分數高；
    明顯下降 -> 大戶出貨、籌碼趨向分散 -> 分數低。"""
    if shareholding_df.empty:
        return {"score": None, "big_holder_pct": None, "big_holder_pct_change": None}
    required = {"date", "HoldingSharesLevel", "percent"}
    if not required.issubset(shareholding_df.columns):
        return {"score": None, "big_holder_pct": None, "big_holder_pct_change": None}

    df = shareholding_df.copy()

    def lower_bound(level: str) -> int:
        match = _LEVEL_LOWER_BOUND_RE.search(str(level))
        if not match:
            return -1
        return int(match.group(1).replace(",", ""))

    df["_lower_bound"] = df["HoldingSharesLevel"].apply(lower_bound)
    big = df[df["_lower_bound"] >= big_holder_min_shares]
    if big.empty:
        return {"score": None, "big_holder_pct": None, "big_holder_pct_change": None}

    by_date = big.groupby("date")["percent"].sum().sort_index().tail(lookback_snapshots)
    if len(by_date) < 2:
        return {"score": None, "big_holder_pct": round(float(by_date.iloc[-1]), 2) if len(by_date) else None,
                "big_holder_pct_change": None}

    change = float(by_date.iloc[-1] - by_date.iloc[0])  # 百分點變化
    # 每變化 1 個百分點 -> 分數 +/- 15 分，中心 50 分
    score = int(np.clip(50 + change * 15, 0, 100))
    return {
        "score": score,
        "big_holder_pct": round(float(by_date.iloc[-1]), 2),
        "big_holder_pct_change": round(change, 2),
    }


def compute_chip_cleanliness(margin_df: pd.DataFrame, shareholding_df: pd.DataFrame | None = None,
                              lookback_days: int = 10, detail_config: dict | None = None) -> dict:
    """籌碼乾淨度（綜合版）：結合三個面向 ——
    1) 融資餘額動能：近期融資餘額是否下降
    2) 融資使用率：融資餘額佔融資限額比例是否健康，避免追繳斷頭風險
    3) 大戶持股集中度趨勢：集保股權分散表中大戶（預設 >400 張）佔比是否穩定或上升
    任一資料來源缺漏時，會自動略過該子項並依剩餘子項重新分配權重；
    三者皆缺漏時，回傳中性分數 50（與舊版行為一致）。"""
    detail_config = detail_config or {}
    if shareholding_df is None:
        shareholding_df = pd.DataFrame()

    weights = detail_config.get("weights", {})
    w_momentum = weights.get("margin_momentum", 0.45)
    w_utilization = weights.get("margin_utilization", 0.25)
    w_holder = weights.get("holder_concentration", 0.30)

    momentum = _score_margin_momentum(margin_df, lookback_days)
    utilization = _score_margin_utilization(
        margin_df,
        safe=detail_config.get("utilization_safe", 0.5),
        danger=detail_config.get("utilization_danger", 0.9),
    )
    holder = _score_holder_concentration(
        shareholding_df,
        big_holder_min_shares=detail_config.get("big_holder_min_shares", 400_000),
        lookback_snapshots=detail_config.get("holder_lookback_snapshots", 4),
    )

    parts = [
        (momentum["score"], w_momentum),
        (utilization["score"], w_utilization),
        (holder["score"], w_holder),
    ]
    available = [(s, w) for s, w in parts if s is not None]

    if not available:
        score = 50
    else:
        total_weight = sum(w for _, w in available) or 1.0
        score = int(round(sum(s * w for s, w in available) / total_weight))
        score = int(np.clip(score, 0, 100))

    return {
        "score": score,
        "margin_momentum_score": momentum["score"],
        "margin_change_pct": momentum["change_pct"],
        "margin_utilization_score": utilization["score"],
        "margin_utilization_pct": utilization["utilization_pct"],
        "holder_concentration_score": holder["score"],
        "big_holder_pct": holder["big_holder_pct"],
        "big_holder_pct_change": holder["big_holder_pct_change"],
    }


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
    bias_safe = bool(abs(bias_pct) < 20)  # 乖離率 < 20% 視為安全，避免追高追空（bool() 避免 numpy bool 無法 JSON 序列化）

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


def _sum_recent_quarterly_eps(financial_df: pd.DataFrame, quarters: int = 4) -> float | None:
    """從 TaiwanStockFinancialStatements（long format：date/stock_id/type/value/origin_name）
    取出 type == "EPS" 的季度資料，加總最近 N 季（預設 4 季，即近四季 TTM）。
    資料不足 N 季時回傳 None，不用不完整的季數硬湊。"""
    if financial_df.empty:
        return None
    if not {"date", "type", "value"}.issubset(financial_df.columns):
        return None

    eps_rows = financial_df[financial_df["type"] == "EPS"].copy()
    if eps_rows.empty:
        return None

    eps_rows = eps_rows.sort_values("date").tail(quarters)
    if len(eps_rows) < quarters:
        return None

    return float(eps_rows["value"].astype(float).sum())


def compute_valuation_band(price_df: pd.DataFrame, per_df: pd.DataFrame, revenue_df: pd.DataFrame,
                            financial_df: pd.DataFrame, detail_config: dict | None = None) -> dict:
    """未來一年樂觀價／穩健價／悲觀價 —— 量化「本益比河流圖」模型估算，非分析師報告或共識目標價。

    做法：
    1) 用 TaiwanStockFinancialStatements 近 4 季 EPS 加總，得出近四季(TTM)每股盈餘。
    2) 用月營收年增率（近12個月 vs 前12個月，與基本面卡片同一算法）當作未來一年獲利成長的
       粗略假設，估算「預估未來一年EPS」= TTM EPS ×(1 + 年增率)；年增率會做極端值裁切
       （預設 ±30%），避免單月營收暴衝暴縮導致估價失真。
    3) 用 TaiwanStockPER 歷史資料算出本益比的低/中/高分位數（預設 20% / 50% / 80%），
       分別乘上預估未來一年EPS，得到悲觀價／穩健價／樂觀價。
    任一步驟資料不足（EPS 不足4季、公司虧損、PER 歷史樣本太少）就回傳 available=False
    並附上具體原因，不會硬湊出看起來合理但其實沒有統計意義的數字。"""
    detail_config = detail_config or {}
    low_pct = detail_config.get("per_percentile_low", 20)
    mid_pct = detail_config.get("per_percentile_mid", 50)
    high_pct = detail_config.get("per_percentile_high", 80)
    growth_cap = detail_config.get("growth_rate_cap_pct", 30)
    min_per_samples = detail_config.get("min_per_samples", 60)

    current_price = None
    if not price_df.empty and "close" in price_df.columns:
        current_price = float(price_df.sort_values("date")["close"].iloc[-1])

    eps_ttm = _sum_recent_quarterly_eps(financial_df)
    if eps_ttm is None:
        return {"available": False,
                "reason": "近四季EPS資料不足（需要 TaiwanStockFinancialStatements 至少4季資料）",
                "current_price": current_price}
    if eps_ttm <= 0:
        return {"available": False,
                "reason": f"近四季EPS加總為 {round(eps_ttm, 2)}（虧損或轉盈虧邊緣），本益比模型不適用",
                "current_price": current_price, "eps_ttm": round(eps_ttm, 2)}

    growth_pct = None
    if not revenue_df.empty and "revenue" in revenue_df.columns:
        rdf = revenue_df.sort_values("date")
        if len(rdf) >= 13:
            latest = rdf["revenue"].iloc[-1]
            year_ago = rdf["revenue"].iloc[-13]
            if year_ago:
                growth_pct = (latest - year_ago) / year_ago * 100
    growth_pct = 0.0 if growth_pct is None else float(np.clip(growth_pct, -growth_cap, growth_cap))
    eps_forward = eps_ttm * (1 + growth_pct / 100)

    if per_df.empty or "PER" not in per_df.columns:
        return {"available": False, "reason": "缺少 TaiwanStockPER 歷史資料",
                "current_price": current_price, "eps_ttm": round(eps_ttm, 2),
                "eps_forward": round(eps_forward, 2)}

    pers = per_df["PER"].astype(float)
    pers = pers[pers > 0].dropna()
    if len(pers) < min_per_samples:
        return {"available": False,
                "reason": f"本益比歷史樣本只有 {len(pers)} 筆，少於門檻 {min_per_samples} 筆，河流圖統計上不夠可靠",
                "current_price": current_price, "eps_ttm": round(eps_ttm, 2),
                "eps_forward": round(eps_forward, 2)}

    per_low = float(np.percentile(pers, low_pct))
    per_mid = float(np.percentile(pers, mid_pct))
    per_high = float(np.percentile(pers, high_pct))

    return {
        "available": True,
        "current_price": current_price,
        "eps_ttm": round(eps_ttm, 2),
        "revenue_yoy_pct_used": round(growth_pct, 1),
        "eps_forward": round(eps_forward, 2),
        "per_low": round(per_low, 1),
        "per_mid": round(per_mid, 1),
        "per_high": round(per_high, 1),
        "per_sample_size": int(len(pers)),
        "pessimistic_price": round(per_low * eps_forward, 1),
        "steady_price": round(per_mid * eps_forward, 1),
        "optimistic_price": round(per_high * eps_forward, 1),
        "note": "量化本益比河流模型估算，非分析師報告或共識目標價",
    }


def score_to_light(score: int, thresholds: dict) -> str:
    if score >= thresholds.get("green", 70):
        return "green"
    if score >= thresholds.get("yellow", 40):
        return "yellow"
    return "red"


def analyze_stock(stock_id: str, config: dict, cache_dir: str = "output/cache",
                   state_dir: str = "state") -> dict:
    scoring = config.get("scoring", {})
    weights = scoring.get("weights", {})
    thresholds = scoring.get("thresholds", {"green": 70, "yellow": 40})
    lookback = config.get("finmind", {}).get("lookback_trading_days", 10)

    price_df = _read_cache(cache_dir, stock_id, "price")
    inst_df = _read_cache(cache_dir, stock_id, "institutional")
    margin_df = _read_cache(cache_dir, stock_id, "margin")
    shareholding_df = _read_cache(cache_dir, stock_id, "shareholding")
    revenue_df = _read_cache(cache_dir, stock_id, "month_revenue")
    per_df = _read_cache(cache_dir, stock_id, "per")
    financial_df = _read_cache(cache_dir, stock_id, "financial_statements")

    inst_cost = compute_institutional_cost(price_df, inst_df, lookback)
    chip = compute_chip_cleanliness(
        margin_df, shareholding_df, lookback,
        detail_config=scoring.get("chip_cleanliness_detail", {}),
    )
    tech = compute_technical_trend(price_df)
    fund = compute_fundamental(revenue_df, per_df)
    valuation = compute_valuation_band(
        price_df, per_df, revenue_df, financial_df,
        detail_config=scoring.get("valuation_band_detail", {}),
    )

    composite = (
        chip["score"] * weights.get("chip_cleanliness", 0.25)
        + inst_cost["score"] * weights.get("institutional_position", 0.30)
        + tech["score"] * weights.get("technical_trend", 0.20)
        + fund["score"] * weights.get("fundamental", 0.25)
    )
    composite = int(round(composite))

    risk_level = "低" if composite >= 70 else ("中" if composite >= 40 else "高")

    signals = compute_all_signals(
        stock_id=stock_id,
        price_df=price_df,
        inst_df=inst_df,
        composite_score=composite,
        current_price=inst_cost.get("current_price"),
        inst_cost=inst_cost.get("cost"),
        thresholds=thresholds,
        state_dir=state_dir,
        lookback_days=lookback,
        accumulation_detail=scoring.get("accumulation_detail", {}),
        pessimistic_price=valuation.get("pessimistic_price"),
        left_side_entry_detail=scoring.get("left_side_entry_detail", {}),
        right_side_entry_detail=scoring.get("right_side_entry_detail", {}),
    )

    return {
        "stock_id": stock_id,
        "composite_score": composite,
        "risk_level": risk_level,
        "chip_cleanliness": {**chip, "light": score_to_light(chip["score"], thresholds)},
        "institutional_position": {**inst_cost, "light": score_to_light(inst_cost["score"], thresholds)},
        "technical": {**tech, "light": score_to_light(tech["score"], thresholds)},
        "fundamental": {**fund, "light": score_to_light(fund["score"], thresholds)},
        "valuation_band": valuation,
        "signals": signals,
    }


def main():
    parser = argparse.ArgumentParser(description="計算主力成本與燈號評分")
    parser.add_argument("--config", default="config/config.yaml")
    parser.add_argument("--stock", required=True)
    parser.add_argument("--cache-dir", default="output/cache")
    parser.add_argument("--state-dir", default="state")
    args = parser.parse_args()

    with open(args.config, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    result = analyze_stock(args.stock, config, args.cache_dir, args.state_dir)
    import json
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
