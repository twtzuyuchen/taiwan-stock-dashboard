"""
analyst_outlook.py
===================
「分析師關鍵價位與情境策略」卡片（儀表板卡片5）的計算層。

這張卡片原本的設計是使用者手動貼上個人分析師報告的重點整理（技術面解析、籌碼面解析、
情境A/B/C的機率與建議），但那些內容大多是分析師的主觀判斷，不是每檔股票都有報告可貼，
導致沒填的股票這張卡片就是空的。

這個模組改成完全用「已經在抓的資料」自動算出客觀內容，讓每一檔追蹤股票都會顯示這張卡片：

  1) 關鍵支撐／壓力位與ATR預期波動區間 —— 邏輯跟「今日大盤/期貨情境」頁面的
     compute_key_levels() 完全相同，只是套用在個股價格，不是加權指數。
  2) 技術面解析 —— 把 analyze.py 已經算好的技術趨勢（均線多空排列、乖離率）與這裡新算的
     近期高低點、成交量趨勢，組成一段敘述文字，不是新的計算邏輯，只是換句話說。
  3) 籌碼面解析 —— 把 analyze.py 已經算好的主力成本、籌碼乾淨度子項（融資動能、融資使用率、
     大戶持股）組成一段敘述文字，同樣不是新邏輯。
  4) 情境A/B/C —— 邏輯跟「今日大盤/期貨情境」頁面的 compute_breakout_scenarios() /
     _historical_breakout_stats() 完全相同（事件研究法：歷史上每次「收盤價站穩突破近期
     高/低點」之後，接下來幾個交易日平均報酬與正報酬比例），套用在個股價格。用「歷史事件
     統計」取代原本分析師報告裡主觀的「發生機率：高/中/低」，並且不產生「建議買賣」文字
     ——跟儀表板其他卡片一樣，只呈現規則式計算出的客觀資訊，實際進出場判斷留給使用者自己。

持有成本（原本 analyst_report.holding_cost 手動填的欄位）改成在網頁上讓使用者自行輸入
（存在瀏覽器 localStorage，不會回傳到伺服器，也不會被每日重新產生的頁面覆蓋掉），
所以這個模組完全不處理持有成本，那部分邏輯在 dashboard_template.html 的內嵌 JavaScript。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

_OHLC_COLS = {"open", "max", "min", "close"}


def compute_stock_key_levels(price_df: pd.DataFrame, detail_config: dict | None = None) -> dict:
    """個股關鍵支撐/壓力位與ATR預期波動區間。邏輯與 market_overview.py 的
    compute_key_levels() 完全相同，只是這裡的價格欄位是 max/min（FinMind TaiwanStockPrice
    的原始欄位名稱），不是 high/low。"""
    detail_config = detail_config or {}
    range_days = detail_config.get("recent_range_days", 20)
    atr_days = detail_config.get("atr_days", 14)

    required = {"date", "close", "max", "min"}
    min_rows = max(range_days, atr_days) + 1
    if price_df.empty or not required.issubset(price_df.columns) or len(price_df) < min_rows:
        return {"available": False, "reason": f"個股歷史股價資料不足（需要至少 {min_rows} 個交易日）"}

    df = price_df.sort_values("date").copy()
    current_price = float(df["close"].iloc[-1])

    recent = df.tail(range_days)
    resistance = float(recent["max"].astype(float).max())
    support = float(recent["min"].astype(float).min())

    close = df["close"].astype(float)
    ma5 = float(close.tail(5).mean()) if len(df) >= 5 else None
    ma20 = float(close.tail(20).mean()) if len(df) >= 20 else None
    ma60 = float(close.tail(60).mean()) if len(df) >= 60 else None

    high, low = df["max"].astype(float), df["min"].astype(float)
    prev_close = close.shift(1)
    true_range = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    atr = float(true_range.tail(atr_days).mean())

    return {
        "available": True,
        "current_price": round(current_price, 2),
        "resistance": round(resistance, 2),
        "support": round(support, 2),
        "range_days": range_days,
        "ma5": round(ma5, 2) if ma5 else None,
        "ma20": round(ma20, 2) if ma20 else None,
        "ma60": round(ma60, 2) if ma60 else None,
        "atr": round(atr, 2),
        "atr_days": atr_days,
        "expected_range_low": round(current_price - atr, 2),
        "expected_range_high": round(current_price + atr, 2),
    }


def compute_price_change(price_df: pd.DataFrame) -> dict:
    """最新一個交易日的收盤價與漲跌幅（給卡片頂端的「參考現價」用）。"""
    if price_df.empty or len(price_df) < 2 or "close" not in price_df.columns:
        return {"available": False}
    close = price_df.sort_values("date")["close"].astype(float)
    latest, prev = float(close.iloc[-1]), float(close.iloc[-2])
    if prev == 0:
        return {"available": False}
    return {"available": True, "price": round(latest, 2), "change_pct": round((latest - prev) / prev * 100, 2)}


def _historical_breakout_stats(df: pd.DataFrame, range_days: int, follow_through_days: int,
                                buffer_pct: float, direction: str, min_samples: int) -> dict:
    """個股版本的歷史事件統計，邏輯與 market_overview.py 的 _historical_breakout_stats()
    完全相同（見該處docstring），只是這裡吃的欄位是 max/min 不是 high/low。純粹的歷史事件
    統計（event study），不是預測模型；事件之間可能重疊，樣本並非完全獨立，只能當作方向性
    的歷史頻率參考，不是嚴謹的統計推論。"""
    highs = df["max"].astype(float).to_numpy()
    lows = df["min"].astype(float).to_numpy()
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


def compute_stock_breakout_scenarios(price_df: pd.DataFrame, key_levels: dict,
                                      detail_config: dict | None = None) -> dict:
    """個股版本的突破/跌破情境分析，邏輯與 market_overview.py 的 compute_breakout_scenarios()
    完全相同：如果股價「漲過／跌破」關鍵支撐壓力並站穩，下一個關鍵點位在哪、歷史上出現
    類似情況後接下來平均怎麼走（歷史事件統計，不是預測）。"""
    detail_config = detail_config or {}
    if not key_levels.get("available"):
        return {"available": False, "reason": "上游關鍵點位資料不足，無法計算突破情境"}

    confirm_buffer_pct = detail_config.get("confirm_buffer_pct", 0.5)
    extended_range_days = detail_config.get("extended_range_days", 60)
    follow_through_days = detail_config.get("follow_through_days", 5)
    min_event_samples = detail_config.get("min_event_samples", 8)

    df = price_df.sort_values("date").reset_index(drop=True)
    range_days = key_levels["range_days"]
    resistance = key_levels["resistance"]
    support = key_levels["support"]

    if len(df) >= extended_range_days:
        extended_high = float(df["max"].astype(float).tail(extended_range_days).max())
        extended_low = float(df["min"].astype(float).tail(extended_range_days).min())
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
            "trigger_price": round(resistance * (1 + confirm_buffer_pct / 100), 2),
            "next_level": round(next_resistance, 2) if next_resistance else None,
            "next_level_label": (f"近{extended_range_days}日高點" if next_resistance
                                  else "近期高點已是近期區間內相對高點，需留意創新高後的價格發現階段（缺乏歷史高點參考）"),
            "stats": up_stats,
        },
        "down": {
            "trigger_price": round(support * (1 - confirm_buffer_pct / 100), 2),
            "next_level": round(next_support, 2) if next_support else None,
            "next_level_label": (f"近{extended_range_days}日低點" if next_support
                                  else "近期低點已是近期區間內相對低點，需留意創新低後的價格發現階段（缺乏歷史低點參考）"),
            "stats": down_stats,
        },
    }


def generate_technical_narrative(price_df: pd.DataFrame, tech: dict, key_levels: dict,
                                  detail_config: dict | None = None) -> dict:
    """技術面解析敘述文字：完全由 analyze.py 已經算好的技術趨勢（compute_technical_trend）
    跟這個模組算的關鍵點位（compute_stock_key_levels）組成一段話，不是新的計算邏輯。"""
    if not key_levels.get("available"):
        return {"available": False, "reason": key_levels.get("reason", "資料不足")}
    detail_config = detail_config or {}
    recent_vol_days = detail_config.get("recent_vol_days", 5)
    prior_vol_days = detail_config.get("prior_vol_days", 20)

    df = price_df.sort_values("date")
    vol_text = ""
    if "Trading_Volume" in df.columns:
        vol = pd.to_numeric(df["Trading_Volume"], errors="coerce")
        window = vol.tail(recent_vol_days + prior_vol_days)
        if len(window) >= recent_vol_days + prior_vol_days:
            recent_avg = window.tail(recent_vol_days).mean()
            prior_avg = window.head(prior_vol_days).mean()
            if pd.notna(recent_avg) and pd.notna(prior_avg) and prior_avg > 0:
                ratio = recent_avg / prior_avg
                if ratio >= 1.2:
                    vol_text = f"近{recent_vol_days}日均量為前{prior_vol_days}日均量的{ratio * 100:.0f}%，量能明顯放大"
                elif ratio <= 0.8:
                    vol_text = f"近{recent_vol_days}日均量為前{prior_vol_days}日均量的{ratio * 100:.0f}%，量能明顯萎縮"
                else:
                    vol_text = f"近{recent_vol_days}日均量為前{prior_vol_days}日均量的{ratio * 100:.0f}%，量能持平"

    ma_parts = []
    if key_levels.get("ma5") is not None and key_levels.get("ma20") is not None:
        ma_parts.append(f"5日均線{key_levels['ma5']}元、20日均線{key_levels['ma20']}元")
    if key_levels.get("ma60") is not None:
        ma_parts.append(f"60日均線{key_levels['ma60']}元")
    ma_text = "，".join(ma_parts)

    bias_pct = tech.get("bias_pct")
    bias_text = ""
    if bias_pct is not None:
        bias_text = f"，距60日均線乖離{bias_pct:+.1f}%（{'屬安全範圍' if tech.get('bias_safe', True) else '偏離過大，留意追高追空風險'}）"

    text = (
        f"技術趨勢判定為「{tech.get('trend', 'N/A')}」{bias_text}。"
        f"近{key_levels['range_days']}個交易日高低點區間為 {key_levels['support']} - {key_levels['resistance']} 元"
        + (f"，{ma_text}" if ma_text else "")
        + "。"
        + (vol_text + "。" if vol_text else "")
    )
    return {"available": True, "text": text}


def generate_chip_narrative(chip: dict, inst_cost: dict) -> dict:
    """籌碼面解析敘述文字：完全由 analyze.py 已經算好的主力成本（compute_institutional_cost）
    跟籌碼乾淨度子項（compute_chip_cleanliness）組成一段話，不是新的計算邏輯。任一子項缺資料
    就跳過那句，不會硬湊；全部都缺才回傳 available=False。"""
    parts = []
    if inst_cost.get("total_days"):
        parts.append(f"近{inst_cost['total_days']}個交易日三大法人買超天數{inst_cost.get('buy_days', 0)}天")
    if inst_cost.get("cost") is not None:
        parts.append(f"估算主力成本約{inst_cost['cost']:.2f}元")
    if chip.get("margin_change_pct") is not None:
        change = chip["margin_change_pct"]
        parts.append(f"融資餘額近期{'增加' if change > 0 else '減少'}{abs(change):.1f}%")
    if chip.get("margin_utilization_pct") is not None:
        parts.append(f"融資使用率{chip['margin_utilization_pct']:.1f}%")
    if chip.get("big_holder_pct") is not None:
        change = chip.get("big_holder_pct_change")
        if change is not None and abs(change) > 0.01:
            change_text = f"，較上次{'上升' if change > 0 else '下降'}{abs(change):.2f}個百分點"
        else:
            change_text = "，較上次持平"
        parts.append(f"大戶持股比例{chip['big_holder_pct']:.1f}%{change_text}")

    if not parts:
        return {"available": False, "reason": "籌碼相關資料不足（融資、法人買賣、集保股權分散表皆缺）"}
    return {"available": True, "text": "；".join(parts) + "。"}


def _direction_scenario(direction_key: str, name: str, tone: str, key_levels: dict, breakout: dict) -> dict:
    """把 up/down 其中一個方向的突破情境資料，組成卡片要顯示的格式（不含任何「建議」字樣，
    只描述客觀價位與歷史事件統計）。"""
    d = breakout.get(direction_key, {})
    stats = d.get("stats", {})
    verb = "站穩突破" if direction_key == "up" else "站穩跌破"
    level_word = "壓力" if direction_key == "up" else "支撐"

    pattern = f"收盤價{verb} {d.get('trigger_price')} 元（近{key_levels['range_days']}日{level_word} × {breakout['confirm_buffer_pct']}% 緩衝）"

    next_level_note = f"（次一關鍵{level_word}位約 {d.get('next_level')} 元，{d.get('next_level_label')}）" if d.get("next_level") else f"（{d.get('next_level_label')}）"

    if stats.get("available"):
        stats_text = (
            f"近{breakout['extended_range_days']}日內共{stats['sample_size']}次同類事件，"
            f"事件發生後{breakout['follow_through_days']}個交易日平均報酬{stats['avg_return_pct']:+.2f}%、"
            f"中位數{stats['median_return_pct']:+.2f}%、方向延續比例{stats['pct_continued']:.0f}%"
            + next_level_note
        )
    else:
        stats_text = stats.get("reason", "樣本不足，暫無歷史統計") + next_level_note

    return {
        "name": name,
        "tone": tone,
        "pattern": pattern,
        "stats_available": stats.get("available", False),
        "stats_text": stats_text,
    }


def compute_scenario_cards(key_levels: dict, breakout: dict) -> list[dict]:
    """組成卡片5要顯示的三個情境（對應原本手動報告的情境A/B/C，但改用客觀計算）：
    A) 目前位置：現價落在支撐壓力區間內、ATR預期波動區間（不是事件統計，只是現況描述）
    B) 站穩突破近期壓力：次一關鍵壓力位 + 歷史事件統計
    C) 站穩跌破近期支撐：次一關鍵支撐位 + 歷史事件統計
    不产生任何「建議買賣」文字，跟儀表板其他卡片一樣只呈現規則式計算出的客觀資訊。"""
    if not key_levels.get("available"):
        return []

    scenarios = [{
        "name": "情境A：區間整理",
        "tone": "neutral",
        "pattern": (f"現價 {key_levels['current_price']} 元，介於近{key_levels['range_days']}日支撐 "
                    f"{key_levels['support']} 元與壓力 {key_levels['resistance']} 元之間"),
        "stats_available": True,
        "stats_text": (f"ATR（近{key_levels['atr_days']}日真實波動幅度均值）估算今日可能波動區間為 "
                        f"{key_levels['expected_range_low']} - {key_levels['expected_range_high']} 元"
                        f"（此為區間整理假設下的參考範圍，非歷史事件統計）"),
    }]

    if breakout.get("available"):
        scenarios.append(_direction_scenario("up", "情境B：站穩突破壓力", "good", key_levels, breakout))
        scenarios.append(_direction_scenario("down", "情境C：站穩跌破支撐", "bad", key_levels, breakout))
    else:
        reason = breakout.get("reason", "資料不足")
        scenarios.append({"name": "情境B：站穩突破壓力", "tone": "neutral",
                           "pattern": "資料不足，無法計算突破情境", "stats_available": False, "stats_text": reason})
        scenarios.append({"name": "情境C：站穩跌破支撐", "tone": "neutral",
                           "pattern": "資料不足，無法計算跌破情境", "stats_available": False, "stats_text": reason})

    return scenarios


def compute_analyst_outlook(price_df: pd.DataFrame, tech: dict, chip: dict, inst_cost: dict,
                             detail_config: dict | None = None) -> dict:
    """卡片5的總入口：組合上面所有子計算，回傳樣板要用的完整結構。"""
    detail_config = detail_config or {}
    key_level_detail = detail_config.get("key_level_detail", {})
    breakout_detail = detail_config.get("breakout_scenario_detail", {})
    narrative_detail = detail_config.get("narrative_detail", {})

    price_change = compute_price_change(price_df)
    key_levels = compute_stock_key_levels(price_df, key_level_detail)
    breakout = compute_stock_breakout_scenarios(price_df, key_levels, breakout_detail)
    technical_narrative = generate_technical_narrative(price_df, tech, key_levels, narrative_detail)
    chip_narrative = generate_chip_narrative(chip, inst_cost)
    scenarios = compute_scenario_cards(key_levels, breakout)

    return {
        "available": key_levels.get("available", False),
        "reason": key_levels.get("reason"),
        "price_change": price_change,
        "key_levels": key_levels,
        "technical_narrative": technical_narrative,
        "chip_narrative": chip_narrative,
        "scenarios": scenarios,
    }
