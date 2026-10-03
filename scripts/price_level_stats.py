"""
price_level_stats.py
=====================
關鍵支撐/壓力位、ATR預期波動區間、歷史突破事件統計——這三段計算邏輯原本在
market_overview.py（套用在加權指數／台指期）跟 analyst_outlook.py（套用在個股）各寫
一份，兩邊的 docstring 都已經寫明「邏輯完全相同」，只是欄位名稱（加權指數資料用
high/low，FinMind個股原始資料用max/min）跟顯示精度（指數動輒上萬點、用1位小數；
個股大多幾十到幾百元、用2位小數）不同。整合清理後抽出共用，市場情境頁跟個股分析師
卡片都改成呼叫這裡的通用版本，數值與行為完全不變（有做過新舊版本逐一比對測試）。

純粹的歷史事件統計（event study），不是預測模型；事件之間可能重疊，樣本並非完全
獨立，只能當作方向性的歷史頻率參考，不是嚴謹的統計推論，也不是對未來的預測或保證。
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def compute_key_levels(df: pd.DataFrame, range_days: int, atr_days: int,
                        high_col: str, low_col: str, round_digits: int,
                        price_field: str, insufficient_data_label: str) -> dict:
    """支撐/壓力與ATR預期波動區間的共用核心算法：近 range_days 個交易日的高低點當作
    近期支撐/壓力，用 ATR（真實波動幅度均值，回溯 atr_days 天）估算現價（或上一個
    交易日收盤價）附近的正常波動大小，推出今日可能波動區間。

    price_field：輸出字典裡「最新一筆收盤價」用哪個鍵名——市場情境頁（预測下一個交易
    session）叫它 prev_close，個股分析師卡片叫它 current_price，語意上是同一個數字，
    只是兩邊頁面的叫法不同，沿用各自原本的命名以免改動下游樣板。
    insufficient_data_label：資料不足時的說明文字（例如「台股加權指數歷史資料」或
    「個股歷史股價資料」），沿用兩邊原本各自的提示文字。"""
    required = {"date", "close", high_col, low_col}
    min_rows = max(range_days, atr_days) + 1
    if df.empty or not required.issubset(df.columns) or len(df) < min_rows:
        return {"available": False,
                "reason": f"{insufficient_data_label}不足（需要至少 {min_rows} 個交易日）"}

    df = df.sort_values("date").copy()
    last_close = float(df["close"].iloc[-1])

    recent = df.tail(range_days)
    resistance = float(recent[high_col].astype(float).max())
    support = float(recent[low_col].astype(float).min())

    close = df["close"].astype(float)
    ma5 = float(close.tail(5).mean()) if len(df) >= 5 else None
    ma20 = float(close.tail(20).mean()) if len(df) >= 20 else None
    ma60 = float(close.tail(60).mean()) if len(df) >= 60 else None

    high = df[high_col].astype(float)
    low = df[low_col].astype(float)
    prev_close_series = close.shift(1)
    true_range = pd.concat([
        high - low, (high - prev_close_series).abs(), (low - prev_close_series).abs(),
    ], axis=1).max(axis=1)
    atr = float(true_range.tail(atr_days).mean())

    return {
        "available": True,
        price_field: round(last_close, round_digits),
        "resistance": round(resistance, round_digits),
        "support": round(support, round_digits),
        "range_days": range_days,
        "ma5": round(ma5, round_digits) if ma5 else None,
        "ma20": round(ma20, round_digits) if ma20 else None,
        "ma60": round(ma60, round_digits) if ma60 else None,
        "atr": round(atr, round_digits),
        "atr_days": atr_days,
        "expected_range_low": round(last_close - atr, round_digits),
        "expected_range_high": round(last_close + atr, round_digits),
    }


def historical_breakout_stats(df: pd.DataFrame, range_days: int, follow_through_days: int,
                               buffer_pct: float, direction: str, min_samples: int,
                               high_col: str, low_col: str) -> dict:
    """在歷史資料裡，找出過去所有「收盤價站穩突破近 range_days 日高／低點」的事件
    （站穩＝超出當時的區間高／低點達 buffer_pct 緩衝以上，不是隨便碰一下就算），
    量測這些事件發生後，接下來 follow_through_days 個交易日的報酬分佈。這是純粹的
    歷史事件統計（event study），不是預測模型；事件之間可能重疊（例如連續上漲時每天
    都符合條件），樣本並非完全獨立，只能當作方向性的歷史頻率參考，不是嚴謹的統計推論。"""
    highs = df[high_col].astype(float).to_numpy()
    lows = df[low_col].astype(float).to_numpy()
    closes = df["close"].astype(float).to_numpy()
    n = len(df)
    fwd_returns = []
    for i in range(range_days, n - follow_through_days):
        window_high = highs[i - range_days:i].max()
        window_low = lows[i - range_days:i].min()
        c = closes[i]
        triggered = (
            c > window_high * (1 + buffer_pct / 100) if direction == "up"
            else c < window_low * (1 - buffer_pct / 100)
        )
        if triggered:
            fwd_returns.append((closes[i + follow_through_days] - c) / c * 100)

    if len(fwd_returns) < min_samples:
        return {"available": False, "sample_size": len(fwd_returns),
                "reason": f"歷史上符合條件的站穩突破事件只有 {len(fwd_returns)} 次，少於門檻 {min_samples} 次，樣本太少不具參考意義"}

    arr = np.array(fwd_returns)
    continued_mask = arr > 0 if direction == "up" else arr < 0
    return {
        "available": True,
        "sample_size": int(len(arr)),
        "avg_return_pct": round(float(arr.mean()), 2),
        "median_return_pct": round(float(np.median(arr)), 2),
        "pct_continued": round(float(continued_mask.mean() * 100), 1),
    }


def compute_breakout_scenarios(df: pd.DataFrame, key_levels: dict, detail_config: dict | None,
                                high_col: str, low_col: str, round_digits: int,
                                default_confirm_buffer_pct: float) -> dict:
    """如果價格「漲過／跌破」關鍵點位算出的近期支撐／壓力，並且站穩（收盤價超出緩衝
    百分比，不是盤中曇花一現），下一個要留意的關鍵點位在哪裡、歷史上出現類似情況後
    接下來大概怎麼走（用 historical_breakout_stats 的歷史事件統計，不是預測模型）。

    次一層關鍵點位：用比近期支撐/壓力更長的回溯天數（extended_range_days）找更高／
    更低的歷史價位；找不到更極端的價位時（例如近期高點剛好也是長期新高），會明講
    「需留意創新高/新低後的價格發現階段」，不會硬湊一個數字出來。"""
    detail_config = detail_config or {}
    if not key_levels.get("available"):
        return {"available": False, "reason": "上游關鍵點位資料不足，無法計算突破情境"}

    confirm_buffer_pct = detail_config.get("confirm_buffer_pct", default_confirm_buffer_pct)
    extended_range_days = detail_config.get("extended_range_days", 60)
    follow_through_days = detail_config.get("follow_through_days", 5)
    min_event_samples = detail_config.get("min_event_samples", 8)

    df = df.sort_values("date").reset_index(drop=True)
    range_days = key_levels["range_days"]
    resistance = key_levels["resistance"]
    support = key_levels["support"]

    if len(df) >= extended_range_days:
        extended_high = float(df[high_col].astype(float).tail(extended_range_days).max())
        extended_low = float(df[low_col].astype(float).tail(extended_range_days).min())
    else:
        extended_high = extended_low = None

    next_resistance = extended_high if extended_high and extended_high > resistance * 1.001 else None
    next_support = extended_low if extended_low and extended_low < support * 0.999 else None

    up_stats = historical_breakout_stats(df, range_days, follow_through_days, confirm_buffer_pct,
                                          "up", min_event_samples, high_col, low_col)
    down_stats = historical_breakout_stats(df, range_days, follow_through_days, confirm_buffer_pct,
                                            "down", min_event_samples, high_col, low_col)

    return {
        "available": True,
        "confirm_buffer_pct": confirm_buffer_pct,
        "extended_range_days": extended_range_days,
        "follow_through_days": follow_through_days,
        "up": {
            "trigger_price": round(resistance * (1 + confirm_buffer_pct / 100), round_digits),
            "next_level": round(next_resistance, round_digits) if next_resistance else None,
            "next_level_label": (f"近{extended_range_days}日高點" if next_resistance
                                  else "近期高點已是近期區間內相對高點，需留意創新高後的價格發現階段（缺乏歷史高點參考）"),
            "stats": up_stats,
        },
        "down": {
            "trigger_price": round(support * (1 - confirm_buffer_pct / 100), round_digits),
            "next_level": round(next_support, round_digits) if next_support else None,
            "next_level_label": (f"近{extended_range_days}日低點" if next_support
                                  else "近期低點已是近期區間內相對低點，需留意創新低後的價格發現階段（缺乏歷史低點參考）"),
            "stats": down_stats,
        },
    }
