from __future__ import annotations

import argparse
import re
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from signals import compute_all_signals, compute_institutional_daily_net, compute_buy_ratio_and_momentum
from analyst_outlook import compute_analyst_outlook
from capital_returns import compute_roce_history


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
    """計算近 N 個交易日的主力（三大法人合計）持倉成本與布局分數。
    買超天數比例／買超力道趨勢的共用計算見 signals.py 的 compute_institutional_daily_net()
    與 compute_buy_ratio_and_momentum()（整合清理：原本這兩段在這裡跟 signals.py 的
    detect_accumulation_signal() 各寫一份，現已抽出共用，數值與行為完全不變）。"""
    if price_df.empty or inst_df.empty:
        return {"cost": None, "current_price": None, "unrealized_pct": None,
                "buy_days": 0, "score": 0}

    price_df = price_df.sort_values("date")
    # FinMind InstitutionalInvestorsBuySell 欄位: date, stock_id, name(投信/外資/自營商...), buy, sell
    merged = compute_institutional_daily_net(price_df, inst_df, lookback_days)

    # 主力布局分數：買超天數比例(50%) + 買超力道趨勢(50%)
    n = len(merged) or 1
    ratio_info = compute_buy_ratio_and_momentum(merged, n)
    buy_days = ratio_info["buy_days"]
    buy_ratio = ratio_info["buy_ratio"]
    momentum = ratio_info["momentum_ratio"]

    total_shares = buy_days["net"].sum()
    if total_shares <= 0:
        cost = None
    else:
        cost = float((buy_days["net"] * buy_days["close"]).sum() / total_shares)

    current_price = float(price_df["close"].iloc[-1]) if not price_df.empty else None
    unrealized_pct = None
    if cost and current_price:
        unrealized_pct = round((current_price - cost) / cost * 100, 2)

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


def _true_range_series(price_df: pd.DataFrame) -> pd.Series | None:
    """計算每日真實波動幅度（True Range）序列，跟 signals.py 的 _atr() 用同一套公式
    （三者取最大值：當日高低差、當日高點與前一日收盤差的絕對值、當日低點與前一日收盤
    差的絕對值），差別是這裡回傳整段序列而非只取最後一筆平均值，供後續算歷史分位用。"""
    if not {"max", "min", "close"}.issubset(price_df.columns):
        return None
    df = price_df.sort_values("date").reset_index(drop=True)
    high = df["max"].astype(float)
    low = df["min"].astype(float)
    close = df["close"].astype(float)
    prev_close = close.shift(1)
    return pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)


def _compute_trend_efficiency(close: pd.Series, detail_config: dict) -> dict:
    """技術面效率指標（概念上類似 Kaufman's Efficiency Ratio）：
    效率 = |近N個交易日淨漲跌幅| ÷ 近N個交易日「每日漲跌幅絕對值總和」。
    分子是起點到終點的直線距離，分母是走過的總路徑長度，比值介於0～1；
    越接近1代表股價走勢方向清楚、來回雜訊少（乾淨的趨勢走勢），越接近0代表
    股價雖然天天在動但淨結果原地踏步，較可能處於橫盤整理或雜訊交易階段。
    這是純粹描述「走勢乾不乾淨」的輔助指標，不代表多空方向，也不計入 score。"""
    window = detail_config.get("efficiency_window_days", 20)
    high_threshold = detail_config.get("efficiency_high_threshold", 0.5)
    low_threshold = detail_config.get("efficiency_low_threshold", 0.25)

    if len(close) < window + 1:
        return {"available": False, "reason": f"股價歷史資料不足（需至少{window + 1}筆交易日資料）"}

    recent = close.iloc[-(window + 1):]
    net_change = abs(float(recent.iloc[-1] - recent.iloc[0]))
    path_sum = float(recent.diff().abs().sum())
    if path_sum == 0:
        return {"available": False, "reason": "近期股價無波動，無法計算效率指標"}

    efficiency = round(net_change / path_sum, 2)
    if efficiency >= high_threshold:
        label = "高效率趨勢"
        text = (f"近{window}個交易日效率指標為{efficiency}（淨漲跌幅佔總波動路徑的比例），"
                f"數值偏高代表走勢方向清楚、來回雜訊相對少，屬於較乾淨的趨勢走勢。")
    elif efficiency <= low_threshold:
        label = "低效率（雜訊大／橫盤整理）"
        text = (f"近{window}個交易日效率指標僅{efficiency}，股價來回震盪的總幅度遠大於淨漲跌幅，"
                f"方向性不明顯，較可能處於橫盤整理或雜訊交易階段，追價風險相對較高。")
    else:
        label = "中等效率"
        text = f"近{window}個交易日效率指標為{efficiency}，走勢方向性中等，不算特別乾淨也不算特別雜亂。"

    return {"available": True, "efficiency_ratio": efficiency, "window_days": window,
            "label": label, "text": text}


def _compute_volatility_percentile(price_df: pd.DataFrame, detail_config: dict) -> dict:
    """ATR 歷史自身百分位：把「目前波動度」放回這檔股票自己過去一段時間（預設約1年）
    的 ATR 分布裡，看目前落在第幾百分位，藉此判斷現在是波動度放大還是收斂的階段
    （而不是只看 ATR 的絕對數字，不同股票、不同價位的 ATR 絕對值本來就無從比較）。
    這是輔助判讀用的「波動度分位」，不代表多空方向，也不計入 score；僅跟自己的
    歷史比較，不是跟其他股票比較（無同業波動度排名）。"""
    atr_days = detail_config.get("atr_days", 14)
    lookback_days = detail_config.get("volatility_percentile_lookback_days", 240)
    min_samples = detail_config.get("volatility_percentile_min_samples", 60)
    high_percentile = detail_config.get("volatility_high_percentile", 80)
    low_percentile = detail_config.get("volatility_low_percentile", 20)

    true_range = _true_range_series(price_df)
    if true_range is None:
        return {"available": False, "reason": "股價資料缺少高低價欄位，無法計算ATR"}

    atr_series = true_range.rolling(atr_days).mean().dropna()
    if len(atr_series) < min_samples:
        return {"available": False,
                "reason": f"ATR歷史樣本數不足（需至少{min_samples}筆，目前僅{len(atr_series)}筆）"}

    window = atr_series.iloc[-lookback_days:] if len(atr_series) > lookback_days else atr_series
    current_atr = float(atr_series.iloc[-1])
    sample_size = len(window)
    percentile = round(float((window <= current_atr).sum()) / sample_size * 100, 1)

    if percentile >= high_percentile:
        label = "波動度偏高"
        text = (f"目前ATR處於近{sample_size}個交易日自身歷史分布的第{percentile:.0f}百分位，"
                f"波動度相對自身歷史明顯放大，宜留意停損停利參考價的距離是否仍合理，部位大小也應跟著調整。")
    elif percentile <= low_percentile:
        label = "波動度偏低"
        text = (f"目前ATR處於近{sample_size}個交易日自身歷史分布的第{percentile:.0f}百分位，"
                f"波動度相對自身歷史明顯收斂，盤整階段常見；惟須留意未來若出現方向性突破，波動度可能快速放大。")
    else:
        label = "波動度中性"
        text = (f"目前ATR處於近{sample_size}個交易日自身歷史分布的第{percentile:.0f}百分位，"
                f"波動度水準大致落在自身歷史的中段，無明顯異常放大或收斂。")

    return {"available": True, "atr": round(current_atr, 2), "percentile": percentile,
            "sample_size": sample_size, "label": label, "text": text}


def compute_technical_trend(price_df: pd.DataFrame, detail_config: dict | None = None) -> dict:
    """簡化技術面：均線多空排列 + 長期均線乖離率，外加效率指標、ATR歷史波動度分位兩項
    輔助判讀（皆為獨立的描述性指標，不影響此處的 score 計算，score 仍只由均線排列決定，
    維持向下相容）。"""
    detail_config = detail_config or {}
    if price_df.empty or len(price_df) < 60:
        return {"trend": "資料不足", "bias_safe": True, "score": 50,
                "efficiency": {"available": False, "reason": "股價歷史資料不足"},
                "volatility": {"available": False, "reason": "股價歷史資料不足"}}

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

    efficiency = _compute_trend_efficiency(close.reset_index(drop=True), detail_config)
    volatility = _compute_volatility_percentile(price_df, detail_config)

    return {"trend": trend, "bias_pct": round(bias_pct, 1), "bias_safe": bias_safe, "score": score,
            "efficiency": efficiency, "volatility": volatility}


def _monthly_yoy_series(revenue_df: pd.DataFrame, max_months: int = 18) -> list[float | None]:
    """由舊到新，回傳最近 max_months 個月、每個月各自的年增率（當月營收 vs 去年同月營收）。
    資料不足 13 個月（算不出任何一個月的年增率）時回傳空列表；資料不足以覆蓋到 max_months
    時，能算幾個月就回傳幾個月，不強求補滿。"""
    if revenue_df.empty or "revenue" not in revenue_df.columns or len(revenue_df) < 13:
        return []
    df = revenue_df.sort_values("date").reset_index(drop=True)
    revenues = df["revenue"].astype(float)
    n = len(revenues)
    start = max(12, n - max_months)
    out: list[float | None] = []
    for i in range(start, n):
        prev_year = revenues.iloc[i - 12]
        out.append(round(float((revenues.iloc[i] - prev_year) / prev_year * 100), 2) if prev_year else None)
    return out


def _revenue_momentum_label(yoy: float | None, acceleration_pct: float | None,
                             consecutive_positive_months: int) -> str | None:
    """用營收年增率、加速度（近3個月平均年增率 vs 前3個月平均年增率）、連續正年增月數，
    組合成一句簡短判讀標籤，純文字分類，不影響基本面催化分數。"""
    if yoy is None:
        return None
    if yoy <= 0:
        return "營收轉弱"
    if consecutive_positive_months >= 3 and (acceleration_pct is None or acceleration_pct >= 0):
        return "營收成長加速"
    if acceleration_pct is not None and acceleration_pct < -5:
        return "營收成長減速"
    return "營收持續成長"


def compute_fundamental(revenue_df: pd.DataFrame, per_df: pd.DataFrame) -> dict:
    """基本面催化：月營收年增率 + PER 相對位階。

    營收動能（revenue_momentum）是額外附加的輔助判讀，包含：近3個月平均年增率、
    「加速度」（近3個月平均年增率 - 前3個月平均年增率，衡量成長是在加速還是減速，
    不是單月年增率的變化）、近6個月正年增比例、連續正年增月數（由最新月往回數，
    中斷就停止，不受6個月窗口限制）。這些欄位純粹是描述性的輔助資訊，
    不會改變 score／composite_score 的計算方式，避免既有燈號判斷被意外牽動。"""
    score = 50
    yoy = None
    if not revenue_df.empty and "revenue" in revenue_df.columns:
        sorted_revenue_df = revenue_df.sort_values("date")
        if len(sorted_revenue_df) >= 13:
            latest = sorted_revenue_df["revenue"].iloc[-1]
            year_ago = sorted_revenue_df["revenue"].iloc[-13]
            if year_ago:
                yoy = round((latest - year_ago) / year_ago * 100, 1)
                score = 50 + np.clip(yoy, -50, 50) * 0.6

    per_percentile = None
    if not per_df.empty and "PER" in per_df.columns:
        pers = per_df["PER"].astype(float).dropna()
        if len(pers) > 5:
            per_percentile = round((pers.iloc[-1] <= pers).mean() * 100, 1)

    yoy_series = _monthly_yoy_series(revenue_df, max_months=18)  # 由舊到新
    revenue_momentum: dict = {"available": False}
    if yoy_series:
        recent6 = yoy_series[-6:]
        recent6_valid = [v for v in recent6 if v is not None]
        positive_ratio_6m = (
            round(sum(1 for v in recent6_valid if v > 0) / len(recent6_valid) * 100, 1)
            if recent6_valid else None
        )

        consecutive = 0
        for v in reversed(yoy_series):
            if v is not None and v > 0:
                consecutive += 1
            else:
                break

        recent_3m = [v for v in yoy_series[-3:] if v is not None]
        recent_3m_avg = round(sum(recent_3m) / len(recent_3m), 1) if recent_3m else None
        prior_3m = [v for v in yoy_series[-6:-3] if v is not None] if len(yoy_series) >= 6 else []
        prior_3m_avg = round(sum(prior_3m) / len(prior_3m), 1) if prior_3m else None
        acceleration_pct = (
            round(recent_3m_avg - prior_3m_avg, 1)
            if recent_3m_avg is not None and prior_3m_avg is not None else None
        )

        revenue_momentum = {
            "available": True,
            "latest_yoy_pct": yoy,
            "recent_3m_avg_yoy_pct": recent_3m_avg,
            "prior_3m_avg_yoy_pct": prior_3m_avg,
            "acceleration_pct": acceleration_pct,
            "positive_ratio_6m_pct": positive_ratio_6m,
            "consecutive_positive_months": consecutive,
            "months_covered": len([v for v in yoy_series if v is not None]),
            "label": _revenue_momentum_label(yoy, acceleration_pct, consecutive),
        }

    return {
        "revenue_yoy_pct": yoy,
        "per_percentile": per_percentile,
        "revenue_momentum": revenue_momentum,
        "score": int(np.clip(score, 0, 100)),
    }


def _window_return_pct(close: pd.Series, window_days: int) -> float | None:
    """N 個交易日報酬率：最新收盤價 vs N 個交易日前收盤價。資料不足時回傳 None。"""
    if len(close) < window_days + 1:
        return None
    base = float(close.iloc[-(window_days + 1)])
    if base == 0:
        return None
    return round((float(close.iloc[-1]) - base) / base * 100, 2)


def _classify_timeframe_direction(return_pct: float | None, threshold_pct: float) -> str | None:
    if return_pct is None:
        return None
    if return_pct >= threshold_pct:
        return "偏多"
    if return_pct <= -threshold_pct:
        return "偏空"
    return "盤整"


def compute_timeframe_alignment(price_df: pd.DataFrame, detail_config: dict | None = None) -> dict:
    """多時間尺度對齊：把短線（約1週）、波段（約1-2個月）、中期（約3-6個月）三個時間尺度
    各自的價格報酬方向算出來，檢查彼此是否同向。這是輔助判讀用的交叉檢查，刻意不納入
    composite_score、也不產生自己的燈號——如果三個時間尺度互相打架（例如中期偏空但短線
    偏多），代表目前走勢還沒有「全時間尺度一致」的確認，可能只是短線反彈或短線拉回；
    建議的研判優先順序是先看中期背景、再看波段方向，短線時機放最後參考——這個優先順序
    是交易上常見的「由大時間框架到小時間框架」判讀習慣，不是從資料統計出來的規則。

    短線：近 short_window_days（預設5個交易日）報酬。
    波段：近 swing_window_short_days／swing_window_long_days（預設20、40個交易日）報酬平均。
    中期：近 mid_window_short_days／mid_window_long_days（預設60、120個交易日）報酬平均
    （120日資料不足時，退回只用60日，並在結果中註明，不會讓整個中期判讀直接不可用）。
    各時間尺度報酬達門檻（預設短線2%、波段4%、中期6%，門檻隨窗口拉長而加大，避免長窗口
    被相對小的報酬波動誤判方向）以上才算偏多/偏空，否則視為盤整。"""
    detail_config = detail_config or {}
    short_window = detail_config.get("short_window_days", 5)
    swing_short_window = detail_config.get("swing_window_short_days", 20)
    swing_long_window = detail_config.get("swing_window_long_days", 40)
    mid_short_window = detail_config.get("mid_window_short_days", 60)
    mid_long_window = detail_config.get("mid_window_long_days", 120)
    short_threshold = detail_config.get("short_threshold_pct", 2.0)
    swing_threshold = detail_config.get("swing_threshold_pct", 4.0)
    mid_threshold = detail_config.get("mid_threshold_pct", 6.0)

    if price_df.empty or "close" not in price_df.columns or len(price_df) < short_window + 1:
        return {"available": False, "reason": "股價歷史資料不足，無法計算多時間尺度對齊"}

    close = price_df.sort_values("date")["close"].astype(float).reset_index(drop=True)

    short_return = _window_return_pct(close, short_window)
    short_direction = _classify_timeframe_direction(short_return, short_threshold)

    swing_returns = [r for r in (
        _window_return_pct(close, swing_short_window),
        _window_return_pct(close, swing_long_window),
    ) if r is not None]
    swing_return = round(sum(swing_returns) / len(swing_returns), 2) if swing_returns else None
    swing_direction = _classify_timeframe_direction(swing_return, swing_threshold)
    swing_note = (
        f"資料僅夠計算{swing_short_window}日報酬，{swing_long_window}日報酬因資料不足略過"
        if len(swing_returns) == 1 else None
    )

    mid_returns = [r for r in (
        _window_return_pct(close, mid_short_window),
        _window_return_pct(close, mid_long_window),
    ) if r is not None]
    mid_return = round(sum(mid_returns) / len(mid_returns), 2) if mid_returns else None
    mid_direction = _classify_timeframe_direction(mid_return, mid_threshold)
    mid_note = (
        f"資料僅夠計算{mid_short_window}日報酬，{mid_long_window}日報酬因資料不足略過"
        if len(mid_returns) == 1 else None
    )

    short_detail = {"return_pct": short_return, "direction": short_direction, "window_days": short_window}
    swing_detail = {"return_pct": swing_return, "direction": swing_direction,
                     "window_days": [swing_short_window, swing_long_window], "note": swing_note}
    mid_detail = {"return_pct": mid_return, "direction": mid_direction,
                  "window_days": [mid_short_window, mid_long_window], "note": mid_note}

    if short_direction is None or swing_direction is None or mid_direction is None:
        return {
            "available": False,
            "reason": "資料不足以同時計算短線／波段／中期三個時間尺度的報酬方向",
            "short": short_detail, "swing": swing_detail, "mid": mid_detail,
        }

    directions = {short_direction, swing_direction, mid_direction}
    aligned = len(directions) == 1 and "盤整" not in directions
    conflicting = "偏多" in directions and "偏空" in directions

    if aligned:
        narrative = f"短線、波段、中期三個時間尺度方向一致，皆為「{short_direction}」，走勢在不同時間框架下互相確認。"
    elif conflicting:
        narrative = (
            f"短線（{short_direction}）、波段（{swing_direction}）、中期（{mid_direction}）三個時間尺度方向互相矛盾，"
            f"研判優先順序建議先看中期背景、再看波段方向，短線時機放最後參考——"
            f"目前中期背景為「{mid_direction}」，若短線訊號與中期方向相反，較可能只是短線反彈或拉回，須留意追高殺低風險。"
        )
    else:
        narrative = (
            f"短線「{short_direction}」、波段「{swing_direction}」、中期「{mid_direction}」尚未完全一致"
            f"（其中至少一個時間尺度為盤整），走勢方向尚待更明確的確認訊號。"
        )

    return {
        "available": True,
        "short": short_detail, "swing": swing_detail, "mid": mid_detail,
        "aligned": aligned, "conflicting": conflicting,
        "narrative": narrative,
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
    balance_df = _read_cache(cache_dir, stock_id, "balance_sheet")

    inst_cost = compute_institutional_cost(price_df, inst_df, lookback)
    chip = compute_chip_cleanliness(
        margin_df, shareholding_df, lookback,
        detail_config=scoring.get("chip_cleanliness_detail", {}),
    )
    tech = compute_technical_trend(price_df, detail_config=scoring.get("technical_efficiency_detail", {}))
    fund = compute_fundamental(revenue_df, per_df)

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
        short_term_entry_detail=scoring.get("short_term_entry_detail", {}),
        short_term_exit_detail=scoring.get("short_term_exit_detail", {}),
        swing_entry_detail=scoring.get("swing_entry_detail", {}),
        swing_exit_detail=scoring.get("swing_exit_detail", {}),
        shareholding_df=shareholding_df,
        triple_institution_buy_detail=scoring.get("triple_institution_buy_detail", {}),
        single_institution_streak_detail=scoring.get("single_institution_streak_detail", {}),
    )

    analyst_outlook = compute_analyst_outlook(
        price_df, tech, chip, inst_cost, fund,
        detail_config=scoring.get("analyst_outlook_detail", {}),
    )

    capital_returns = compute_roce_history(
        financial_df, balance_df,
        detail_config=scoring.get("roce_detail", {}),
    )

    timeframe_alignment = compute_timeframe_alignment(
        price_df, detail_config=scoring.get("timeframe_alignment_detail", {}),
    )

    return {
        "stock_id": stock_id,
        "composite_score": composite,
        "risk_level": risk_level,
        "chip_cleanliness": {**chip, "light": score_to_light(chip["score"], thresholds)},
        "institutional_position": {**inst_cost, "light": score_to_light(inst_cost["score"], thresholds)},
        "technical": {**tech, "light": score_to_light(tech["score"], thresholds)},
        "fundamental": {**fund, "light": score_to_light(fund["score"], thresholds)},
        "signals": signals,
        "analyst_outlook": analyst_outlook,
        "capital_returns": capital_returns,
        "timeframe_alignment": timeframe_alignment,
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
