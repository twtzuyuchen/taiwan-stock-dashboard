"""
market_overview.py
====================
計算層＋呈現層：把前一交易日美股三大指數（納斯達克、費城半導體 SOX、道瓊）收盤表現，
轉換成「今日台股加權指數／台指期可能走勢與關鍵點位」的參考資訊。

跟個股儀表板（analyze.py / generate_dashboard.py）完全獨立，是大盤層級的單一頁面，
不屬於任何一檔股票，資料來源是 fetch_market_data.py 快取的 Yahoo Finance 指數資料。

用兩種方法互相對照，刻意不用黑箱模型：
  A) 規則式綜合評分：三大指數漲跌幅依權重（預設費半權重最高）加總，對照門檻判斷「偏多／中性／偏空」。
  B) 歷史迴歸模型：用過去數年「美股前一日報酬 -> 台股加權指數當日報酬」的歷史資料做線性迴歸，
     算出量化的預測報酬與可信度（R²、樣本數），資料不足時會明確標示，不硬湊。
再加上完全獨立於前兩者、只看加權指數自身價格行為算出來的「關鍵點位」（近期高低點、均線、ATR波動區間）。

台指期部分：來源是 FinMind「TaiwanFuturesDaily」，近月合約收盤後才更新的公開資料（非即時報價），
日盤（08:45–13:45）與夜盤（15:00–次日05:00）各自獨立顯示收盤與漲跌幅；基差、關鍵點位、突破情境
這幾項統一用日盤資料計算，理由是日盤才跟只在日盤交易的加權指數現貨在同一個時間基準上可以比較。

用法：
    python market_overview.py --config config/config.yaml
    python market_overview.py --stock-demo   # 示範資料，不需要網路
"""
from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path

import numpy as np
import pandas as pd
import yaml
from jinja2 import Environment, FileSystemLoader

INDEX_LABELS = {
    "nasdaq": "納斯達克綜合指數",
    "sox": "費城半導體指數",
    "dow": "道瓊工業指數",
    "twii": "台股加權指數",
}


def _read_cache(cache_dir: str, key: str) -> pd.DataFrame:
    path = Path(cache_dir) / f"market_{key}.csv"
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


def _session_slice(txf_df: pd.DataFrame, session: str) -> pd.DataFrame:
    """台指期快取檔案 market_txf.csv 同時包含日盤／夜盤兩個時段（見 fetch_market_data.py 的
    _select_front_month_close），這裡篩出指定時段、轉成 date/open/high/low/close 的標準格式，
    讓 compute_index_summary／compute_key_levels／compute_breakout_scenarios 這些通用函式可以
    直接套用，不用另外寫一份專屬台指期的版本。
    沒有 session 欄位時（例如升級前留下的舊版快取檔案，只有單一時段），一律當成日盤處理，
    避免第一次讀到舊快取格式就整段掛掉；下次重新 fetch 之後就會是新格式了。"""
    if txf_df.empty:
        return pd.DataFrame()
    if "session" not in txf_df.columns:
        return txf_df[["date", "open", "high", "low", "close"]].reset_index(drop=True) if session == "day" else pd.DataFrame()
    sliced = txf_df[txf_df["session"] == session]
    if sliced.empty:
        return pd.DataFrame()
    return sliced[["date", "open", "high", "low", "close"]].reset_index(drop=True)


def compute_index_summary(df: pd.DataFrame) -> dict:
    """單一指數最近一個交易日的收盤與漲跌幅。df 需要 date/close 欄位，且已經照日期排序。"""
    if df.empty or "close" not in df.columns or len(df) < 2:
        return {"available": False, "close": None, "prev_close": None, "pct_change": None,
                "date": None, "reason": "歷史資料不足（需要至少2個交易日）"}

    df = df.sort_values("date")
    close = float(df["close"].iloc[-1])
    prev_close = float(df["close"].iloc[-2])
    pct_change = round((close - prev_close) / prev_close * 100, 2) if prev_close else None

    return {
        "available": True,
        "close": round(close, 2),
        "prev_close": round(prev_close, 2),
        "pct_change": pct_change,
        "date": str(df["date"].iloc[-1]),
    }


def compute_rule_based_bias(us_summaries: dict, detail_config: dict | None = None) -> dict:
    """方法 A：規則式綜合評分。三大美股指數漲跌幅依權重加總，對照門檻判斷偏多／中性／偏空。
    任一指數缺資料就從加權平均中排除、權重依剩餘指數重新分配，三者皆缺才回傳 available=False。"""
    detail_config = detail_config or {}
    weights = detail_config.get("weights", {"sox": 0.5, "nasdaq": 0.3, "dow": 0.2})
    bullish_threshold = detail_config.get("bullish_threshold_pct", 0.5)
    bearish_threshold = detail_config.get("bearish_threshold_pct", -0.5)

    parts = []
    contributions = {}
    for key, weight in weights.items():
        summary = us_summaries.get(key, {})
        if summary.get("available") and summary.get("pct_change") is not None:
            parts.append((summary["pct_change"], weight))
            contributions[key] = summary["pct_change"]

    if not parts:
        return {"available": False, "reason": "納斯達克／費半／道瓊三項前一日資料皆缺，無法計算規則式評分",
                "composite_pct": None, "label": None, "contributions": {}}

    total_weight = sum(w for _, w in parts) or 1.0
    composite_pct = round(sum(p * w for p, w in parts) / total_weight, 2)

    if composite_pct >= bullish_threshold:
        label = "偏多"
    elif composite_pct <= bearish_threshold:
        label = "偏空"
    else:
        label = "中性"

    contrib_text = "、".join(
        f"{INDEX_LABELS[k]}{contributions[k]:+.2f}%" for k in ("sox", "nasdaq", "dow") if k in contributions
    )
    text = f"綜合加權漲跌幅 {composite_pct:+.2f}%（{contrib_text}），規則式判斷台股開盤情境「{label}」"

    return {
        "available": True,
        "composite_pct": composite_pct,
        "label": label,
        "contributions": contributions,
        "weights_used": {k: w for k, w in weights.items() if k in contributions},
        "text": text,
    }


def compute_regression_forecast(twii_df: pd.DataFrame, us_dfs: dict, detail_config: dict | None = None) -> dict:
    """方法 B：歷史迴歸模型。用過去數年「美股前一交易日報酬 -> 台股加權指數當日報酬」的歷史資料
    做複迴歸，估算今日預測報酬、R²（模型解釋力）與樣本數，樣本不足時明確標示原因不硬算。

    對齊邏輯：美股收盤時間在台北時間清晨，也就是台股當日開盤前才知道的最新資訊，所以台股某個
    交易日的報酬，要對齊「該交易日之前、最近一個已經收盤的美股交易日」的報酬，而不是同一個日曆
    日期的美股資料（那天美股可能都還沒收盤）。用 pandas merge_asof 往回找、且不允許同日期匹配。"""
    detail_config = detail_config or {}
    min_samples = detail_config.get("regression_min_samples", 120)

    if twii_df.empty or "close" not in twii_df.columns:
        return {"available": False, "reason": "缺少台股加權指數歷史資料", "sample_size": 0}

    twii = twii_df.sort_values("date").copy()
    twii["date"] = pd.to_datetime(twii["date"])
    twii["tw_return"] = twii["close"].pct_change() * 100
    twii = twii.dropna(subset=["tw_return"])

    us_keys = [k for k in ("sox", "nasdaq", "dow") if k in us_dfs and not us_dfs[k].empty]
    if not us_keys:
        return {"available": False, "reason": "納斯達克／費半／道瓊歷史資料皆缺，無法訓練迴歸模型", "sample_size": 0}

    merged = twii[["date", "tw_return"]].copy()
    for key in us_keys:
        us = us_dfs[key].sort_values("date").copy()
        us["date"] = pd.to_datetime(us["date"])
        us[f"{key}_return"] = us["close"].pct_change() * 100
        us = us.dropna(subset=[f"{key}_return"])[["date", f"{key}_return"]]
        merged = pd.merge_asof(
            merged.sort_values("date"), us.sort_values("date"),
            on="date", direction="backward", allow_exact_matches=False,
        )

    feature_cols = [f"{k}_return" for k in us_keys]
    merged = merged.dropna(subset=feature_cols + ["tw_return"])

    sample_size = len(merged)
    if sample_size < min_samples:
        return {"available": False, "sample_size": sample_size,
                "reason": f"可比對樣本只有 {sample_size} 筆，少於門檻 {min_samples} 筆，迴歸模型統計上不夠可靠"}

    X = np.column_stack([np.ones(sample_size), merged[feature_cols].to_numpy()])
    y = merged["tw_return"].to_numpy()
    coeffs, _, _, _ = np.linalg.lstsq(X, y, rcond=None)

    y_hat = X @ coeffs
    residuals = y - y_hat
    ss_res = float(np.sum(residuals ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    r_squared = round(1 - ss_res / ss_tot, 3) if ss_tot > 0 else 0.0
    residual_std = float(np.std(residuals, ddof=max(1, len(feature_cols) + 1)))

    # 用「最新一筆」美股報酬（即前一交易日相對於再前一日的漲跌幅）代入模型，預測今天的台股報酬
    latest_us_returns = []
    for key in us_keys:
        us = us_dfs[key].sort_values("date")
        us_ret = us["close"].pct_change().iloc[-1] * 100
        latest_us_returns.append(us_ret)
    x_pred = np.array([1.0] + latest_us_returns)
    predicted_return = float(x_pred @ coeffs)

    coef_map = {key: round(float(c), 3) for key, c in zip(us_keys, coeffs[1:])}

    return {
        "available": True,
        "sample_size": sample_size,
        "r_squared": r_squared,
        "predicted_return_pct": round(predicted_return, 2),
        "residual_std_pct": round(residual_std, 2),
        "predicted_low_pct": round(predicted_return - residual_std, 2),
        "predicted_high_pct": round(predicted_return + residual_std, 2),
        "coefficients": coef_map,
        "intercept": round(float(coeffs[0]), 3),
        "text": (
            f"以近 {sample_size} 個可比對交易日樣本訓練的迴歸模型（R²={r_squared}），"
            f"預測今日台股加權指數報酬約 {predicted_return:+.2f}%"
            f"（±1個標準差區間 {predicted_return - residual_std:+.2f}% ～ {predicted_return + residual_std:+.2f}%）"
        ),
    }


def compare_methods(rule_based: dict, regression: dict) -> dict:
    """把兩種方法的方向判斷放在一起對照，讓使用者自己判斷兩者是否一致，不是取平均硬湊出單一答案。"""
    if not rule_based.get("available") or not regression.get("available"):
        missing = []
        if not rule_based.get("available"):
            missing.append("規則式評分")
        if not regression.get("available"):
            missing.append("迴歸模型")
        return {"available": False, "text": f"{'、'.join(missing)}資料不足，暫無法進行方法對照"}

    rule_label = rule_based["label"]
    reg_return = regression["predicted_return_pct"]
    reg_label = "偏多" if reg_return > 0.15 else ("偏空" if reg_return < -0.15 else "中性")

    agree = rule_label == reg_label
    text = (
        f"規則式評分判斷「{rule_label}」，迴歸模型預測報酬 {reg_return:+.2f}%（對應方向「{reg_label}」）——"
        + ("兩種方法方向一致，可信度相對較高" if agree
           else "兩種方法方向不一致，建議降低對今日方向判斷的信心、以關鍵點位實際表態為準")
    )
    return {"available": True, "agree": agree, "rule_label": rule_label, "regression_label": reg_label, "text": text}


def compute_futures_basis(txf_summary: dict, twii_summary: dict) -> dict:
    """基差 = 台指期近月合約收盤 - 加權指數現貨收盤。正價差（期貨價格 > 現貨）常被解讀為市場
    對後市偏多、或反映無風險利率高於除息預期；逆價差則相反，也可能只是反映除權息旺季的除息估算。
    這是市場資金成本／情緒面的參考指標，不是直接的漲跌訊號——期貨到期時理論上會與現貨價格收斂，
    不代表現在的價差方向會延續到到期日。"""
    if not txf_summary.get("available") or not twii_summary.get("available"):
        return {"available": False, "reason": "台指期或加權指數收盤資料不足，無法計算基差"}

    txf_close = txf_summary["close"]
    twii_close = twii_summary["close"]
    basis = round(txf_close - twii_close, 1)
    basis_pct = round(basis / twii_close * 100, 2) if twii_close else None
    label = "正價差" if basis > 0 else ("逆價差" if basis < 0 else "持平")

    return {
        "available": True,
        "txf_close": txf_close,
        "twii_close": twii_close,
        "basis": basis,
        "basis_pct": basis_pct,
        "label": label,
        "text": f"台指期近月合約收盤 {txf_close}，對加權指數收盤 {twii_close}，{label} {basis:+.1f} 點（{basis_pct:+.2f}%）",
    }


def compute_key_levels(twii_df: pd.DataFrame, detail_config: dict | None = None) -> dict:
    """完全獨立於美股連動判斷之外，純粹用台股加權指數自身的價格行為算出的關鍵點位：
    近期高低點（支撐／壓力）、均線、以及用 ATR（真實波動幅度）估算的今日可能波動區間。"""
    detail_config = detail_config or {}
    range_days = detail_config.get("recent_range_days", 20)
    atr_days = detail_config.get("atr_days", 14)

    required = {"date", "close", "high", "low"}
    if twii_df.empty or not required.issubset(twii_df.columns) or len(twii_df) < max(range_days, atr_days) + 1:
        return {"available": False,
                "reason": f"台股加權指數歷史資料不足（需要至少 {max(range_days, atr_days) + 1} 個交易日）"}

    df = twii_df.sort_values("date").copy()
    prev_close = float(df["close"].iloc[-1])

    recent = df.tail(range_days)
    resistance = float(recent["high"].max())
    support = float(recent["low"].min())

    ma5 = float(df["close"].tail(5).mean()) if len(df) >= 5 else None
    ma20 = float(df["close"].tail(20).mean()) if len(df) >= 20 else None
    ma60 = float(df["close"].tail(60).mean()) if len(df) >= 60 else None

    high, low, close = df["high"], df["low"], df["close"]
    prev_close_series = close.shift(1)
    true_range = pd.concat([
        high - low,
        (high - prev_close_series).abs(),
        (low - prev_close_series).abs(),
    ], axis=1).max(axis=1)
    atr = float(true_range.tail(atr_days).mean())

    return {
        "available": True,
        "prev_close": round(prev_close, 1),
        "resistance": round(resistance, 1),
        "support": round(support, 1),
        "range_days": range_days,
        "ma5": round(ma5, 1) if ma5 else None,
        "ma20": round(ma20, 1) if ma20 else None,
        "ma60": round(ma60, 1) if ma60 else None,
        "atr": round(atr, 1),
        "atr_days": atr_days,
        "expected_range_low": round(prev_close - atr, 1),
        "expected_range_high": round(prev_close + atr, 1),
    }


def _historical_breakout_stats(df: pd.DataFrame, range_days: int, follow_through_days: int,
                                buffer_pct: float, direction: str, min_samples: int) -> dict:
    """在台股加權指數的歷史資料裡，找出過去所有「收盤價站穩突破近 range_days 日高／低點」的事件
    （站穩＝超出當時的區間高／低點達 buffer_pct 緩衝以上，不是隨便碰一下就算），
    量測這些事件發生後，接下來 follow_through_days 個交易日的報酬分佈。
    這是純粹的歷史事件統計（event study），不是預測模型；事件之間可能重疊（例如連續上漲時
    每天都符合條件），樣本並非完全獨立，只能當作方向性的歷史頻率參考，不是嚴謹的統計推論。"""
    highs, lows, closes = df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy()
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


def compute_breakout_scenarios(twii_df: pd.DataFrame, key_levels: dict, detail_config: dict | None = None) -> dict:
    """如果加權指數「跌破／漲過」上面關鍵點位卡片算出的近期支撐／壓力，並且站穩（收盤價超出緩衝
    百分比，不是盤中曇花一現），下一個要留意的關鍵點位在哪裡、歷史上出現類似情況後接下來大概怎麼走。

    次一層關鍵點位：用比近期支撐/壓力更長的回溯天數（extended_range_days）找更高／更低的歷史價位；
    找不到更極端的價位時（例如近期高點剛好也是長期新高），會明講「需留意創新高/新低後的價格發現階段」，
    不會硬湊一個數字出來。後續可能走勢：用歷史事件統計（見 _historical_breakout_stats），不是預測模型。"""
    detail_config = detail_config or {}
    if not key_levels.get("available"):
        return {"available": False, "reason": "上游關鍵點位資料不足，無法計算突破情境"}

    confirm_buffer_pct = detail_config.get("confirm_buffer_pct", 0.3)
    extended_range_days = detail_config.get("extended_range_days", 60)
    follow_through_days = detail_config.get("follow_through_days", 5)
    min_event_samples = detail_config.get("min_event_samples", 8)

    df = twii_df.sort_values("date").reset_index(drop=True)
    range_days = key_levels["range_days"]
    resistance = key_levels["resistance"]
    support = key_levels["support"]

    if len(df) >= extended_range_days:
        extended_high = float(df["high"].tail(extended_range_days).max())
        extended_low = float(df["low"].tail(extended_range_days).min())
    else:
        extended_high = extended_low = None

    next_resistance = extended_high if extended_high and extended_high > resistance * 1.001 else None
    next_support = extended_low if extended_low and extended_low < support * 0.999 else None

    up_stats = _historical_breakout_stats(df, range_days, follow_through_days, confirm_buffer_pct, "up", min_event_samples)
    down_stats = _historical_breakout_stats(df, range_days, follow_through_days, confirm_buffer_pct, "down", min_event_samples)

    return {
        "available": True,
        "confirm_buffer_pct": confirm_buffer_pct,
        "extended_range_days": extended_range_days,
        "follow_through_days": follow_through_days,
        "up": {
            "trigger_price": round(resistance * (1 + confirm_buffer_pct / 100), 1),
            "next_level": round(next_resistance, 1) if next_resistance else None,
            "next_level_label": (f"近{extended_range_days}日高點" if next_resistance
                                  else "近期高點已是近期區間內相對高點，需留意創新高後的價格發現階段（缺乏歷史高點參考）"),
            "stats": up_stats,
        },
        "down": {
            "trigger_price": round(support * (1 - confirm_buffer_pct / 100), 1),
            "next_level": round(next_support, 1) if next_support else None,
            "next_level_label": (f"近{extended_range_days}日低點" if next_support
                                  else "近期低點已是近期區間內相對低點，需留意創新低後的價格發現階段（缺乏歷史低點參考）"),
            "stats": down_stats,
        },
    }


def compute_market_overview(config: dict, cache_dir: str = "output/cache") -> dict:
    market_cfg = config.get("market_overview", {})

    dfs = {key: _read_cache(cache_dir, key) for key in ("nasdaq", "sox", "dow", "twii", "txf")}

    us_summaries = {
        key: compute_index_summary(dfs[key]) for key in ("nasdaq", "sox", "dow")
    }
    twii_summary = compute_index_summary(dfs["twii"])

    # 台指期日盤／夜盤各自獨立算收盤與漲跌幅；基差、關鍵點位、突破情境統一用日盤資料，
    # 因為只有日盤才跟只在日盤交易的加權指數現貨站在同一個時間基準上，比較才有意義。
    txf_day_df = _session_slice(dfs["txf"], "day")
    txf_night_df = _session_slice(dfs["txf"], "night")
    txf_summary_day = compute_index_summary(txf_day_df)
    txf_summary_night = compute_index_summary(txf_night_df)

    rule_based = compute_rule_based_bias(us_summaries, market_cfg.get("rule_based_detail", {}))
    regression = compute_regression_forecast(
        dfs["twii"], {k: dfs[k] for k in ("sox", "nasdaq", "dow")},
        {**market_cfg, "regression_min_samples": market_cfg.get("regression_min_samples", 120)},
    )
    comparison = compare_methods(rule_based, regression)
    key_levels = compute_key_levels(dfs["twii"], market_cfg.get("key_level_detail", {}))
    breakout_scenarios = compute_breakout_scenarios(dfs["twii"], key_levels, market_cfg.get("breakout_scenario_detail", {}))

    # 台指期關鍵點位／突破情境：用跟加權指數完全一樣的計算函式，套在台指期日盤自己的價格序列上——
    # 不是「借用」加權指數的方向判斷，是台指期自己的真實收盤資料算出來的。
    futures_basis = compute_futures_basis(txf_summary_day, twii_summary)
    txf_key_levels = compute_key_levels(txf_day_df, market_cfg.get("key_level_detail", {}))
    txf_breakout_scenarios = compute_breakout_scenarios(txf_day_df, txf_key_levels, market_cfg.get("breakout_scenario_detail", {}))

    return {
        "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "us_summaries": us_summaries,
        "twii_summary": twii_summary,
        "txf_summary_day": txf_summary_day,
        "txf_summary_night": txf_summary_night,
        "futures_basis": futures_basis,
        "txf_key_levels": txf_key_levels,
        "txf_breakout_scenarios": txf_breakout_scenarios,
        "rule_based": rule_based,
        "regression": regression,
        "comparison": comparison,
        "key_levels": key_levels,
        "breakout_scenarios": breakout_scenarios,
    }


def demo_market_overview() -> dict:
    """不需要網路的示範資料。"""
    return {
        "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "us_summaries": {
            "nasdaq": {"available": True, "close": 21834.5, "prev_close": 21602.3, "pct_change": 1.07, "date": "2026-08-04"},
            "sox": {"available": True, "close": 6120.8, "prev_close": 5978.4, "pct_change": 2.38, "date": "2026-08-04"},
            "dow": {"available": True, "close": 45210.2, "prev_close": 45102.6, "pct_change": 0.24, "date": "2026-08-04"},
        },
        "twii_summary": {"available": True, "close": 43120.5, "prev_close": 42980.1, "pct_change": 0.33, "date": "2026-08-04"},
        "txf_summary_day": {"available": True, "close": 43095.0, "prev_close": 42960.0, "pct_change": 0.31, "date": "2026-08-04"},
        "txf_summary_night": {"available": True, "close": 43210.0, "prev_close": 43095.0, "pct_change": 0.27, "date": "2026-08-04"},
        "futures_basis": {
            "available": True, "txf_close": 43095.0, "twii_close": 43120.5, "basis": -25.5, "basis_pct": -0.06, "label": "逆價差",
            "text": "台指期近月合約收盤 43095.0，對加權指數收盤 43120.5，逆價差 -25.5 點（-0.06%）（示範資料）",
        },
        "txf_key_levels": {
            "available": True, "prev_close": 43095.0, "resistance": 43650.0, "support": 41790.0,
            "range_days": 20, "ma5": 42910.1, "ma20": 42580.6, "ma60": 41880.2,
            "atr": 405.8, "atr_days": 14,
            "expected_range_low": 42689.2, "expected_range_high": 43500.8,
        },
        "txf_breakout_scenarios": {
            "available": True, "confirm_buffer_pct": 0.3, "extended_range_days": 60, "follow_through_days": 5,
            "up": {
                "trigger_price": 43779.0, "next_level": 44380.0, "next_level_label": "近60日高點",
                "stats": {"available": True, "sample_size": 19, "avg_return_pct": 1.21,
                          "median_return_pct": 0.95, "pct_continued": 63.2},
            },
            "down": {
                "trigger_price": 41664.6, "next_level": 40590.0, "next_level_label": "近60日低點",
                "stats": {"available": True, "sample_size": 11, "avg_return_pct": -1.52,
                          "median_return_pct": -1.30, "pct_continued": 54.5},
            },
        },
        "rule_based": {
            "available": True, "composite_pct": 1.45, "label": "偏多",
            "contributions": {"sox": 2.38, "nasdaq": 1.07, "dow": 0.24},
            "weights_used": {"sox": 0.5, "nasdaq": 0.3, "dow": 0.2},
            "text": "綜合加權漲跌幅 +1.45%（費城半導體指數+2.38%、納斯達克綜合指數+1.07%、道瓊工業指數+0.24%），規則式判斷台股開盤情境「偏多」（示範資料）",
        },
        "regression": {
            "available": True, "sample_size": 612, "r_squared": 0.412,
            "predicted_return_pct": 0.78, "residual_std_pct": 0.65,
            "predicted_low_pct": 0.13, "predicted_high_pct": 1.43,
            "coefficients": {"sox": 0.21, "nasdaq": 0.18, "dow": 0.09}, "intercept": 0.02,
            "text": "以近 612 個可比對交易日樣本訓練的迴歸模型（R²=0.412），預測今日台股加權指數報酬約 +0.78%（±1個標準差區間 +0.13% ～ +1.43%）（示範資料）",
        },
        "comparison": {
            "available": True, "agree": True, "rule_label": "偏多", "regression_label": "偏多",
            "text": "規則式評分判斷「偏多」，迴歸模型預測報酬 +0.78%（對應方向「偏多」）——兩種方法方向一致，可信度相對較高（示範資料）",
        },
        "key_levels": {
            "available": True, "prev_close": 43120.5, "resistance": 43680.0, "support": 41850.0,
            "range_days": 20, "ma5": 42950.3, "ma20": 42610.8, "ma60": 41920.4,
            "atr": 410.5, "atr_days": 14,
            "expected_range_low": 42710.0, "expected_range_high": 43531.0,
        },
        "breakout_scenarios": {
            "available": True, "confirm_buffer_pct": 0.3, "extended_range_days": 60, "follow_through_days": 5,
            "up": {
                "trigger_price": 43811.0, "next_level": 44520.0, "next_level_label": "近60日高點",
                "stats": {"available": True, "sample_size": 23, "avg_return_pct": 1.35,
                          "median_return_pct": 1.02, "pct_continued": 65.2},
            },
            "down": {
                "trigger_price": 41724.4, "next_level": 40680.0, "next_level_label": "近60日低點",
                "stats": {"available": True, "sample_size": 14, "avg_return_pct": -1.68,
                          "median_return_pct": -1.41, "pct_continued": 57.1},
            },
        },
    }


def render_market_overview(overview: dict, template_dir: str, output_dir: str, is_demo: bool = False) -> Path:
    env = Environment(loader=FileSystemLoader(template_dir))
    template = env.get_template("market_overview_template.html")
    html = template.render(is_demo=is_demo, **overview)

    Path(output_dir).mkdir(parents=True, exist_ok=True)
    out_path = Path(output_dir) / "market_overview.html"
    out_path.write_text(html, encoding="utf-8")
    return out_path


def main():
    parser = argparse.ArgumentParser(description="計算並產出今日大盤/期貨情境頁面")
    parser.add_argument("--config", default="config/config.yaml")
    parser.add_argument("--cache-dir", default="output/cache")
    parser.add_argument("--template-dir", default="templates")
    parser.add_argument("--output-dir", default="output")
    parser.add_argument("--demo", action="store_true", help="使用示範資料，不需連網")
    args = parser.parse_args()

    with open(args.config, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    if args.demo:
        overview = demo_market_overview()
    else:
        overview = compute_market_overview(config, args.cache_dir)

    out_path = render_market_overview(overview, args.template_dir, args.output_dir, is_demo=args.demo)
    print(f"大盤情境頁面已產出: {out_path}")


if __name__ == "__main__":
    main()
