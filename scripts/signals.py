"""
signals.py
==========
買賣訊號層：在既有的燈號評分之外，額外提供四種訊號：

  A. 均線黃金/死亡交叉（事件型訊號，只在「交叉發生的當天」出現一次）
     5日均線由下往上穿越20日均線 -> 黃金交叉（偏多）
     5日均線由上往下穿越20日均線 -> 死亡交叉（偏空）
     資料完全來自當次抓到的股價歷史，不需要額外保存狀態。

  B. 綜合評分區間轉換（事件型訊號，需要跨日比較，狀態存在 state/ 資料夾裡）
     從「觀望」轉為「布局」，或反過來，才會觸發；單純維持同一區間不會重複出現。
     因為需要「昨天的結果」，所以每次執行都會把當天的區間寫進
     state/{stock_id}_state.json，下次執行時讀出來比較。

  C. 主力成本防守價（狀態型訊號，只要現價低於主力估算成本就會持續顯示，
     不是只出現一次）

  D. 主力建倉訊號（狀態型訊號，只要近期買超型態符合「悄悄吸籌」就會持續顯示）
     判斷近N個交易日三大法人是否呈現「持續買超、力道加速，但股價尚未大幅表態」的
     型態——這是散戶盯盤常說的「主力建倉」：買超天數多、越買越積極，但股價還沒被
     墊高，代表買超還沒被市場發現。不需要額外保存狀態，每次執行都用當次抓到的
     股價與三大法人買賣超歷史重新判斷。

事件型（A、B）代表「今天發生了什麼變化」；狀態型（C、D）代表「現在是什麼狀態」。
不同類型用途不同，儀表板會分開顯示。
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd


def detect_ma_cross(price_df: pd.DataFrame) -> dict:
    """偵測 5 日均線 vs 20 日均線的黃金/死亡交叉（只看最近兩個交易日）。"""
    if price_df.empty or len(price_df) < 22:
        return {"signal": None, "text": "資料不足，無法判斷均線交叉", "light": None}

    price_df = price_df.sort_values("date")
    close = price_df["close"].astype(float)
    ma5 = close.rolling(5).mean()
    ma20 = close.rolling(20).mean()

    if ma5.iloc[-2:].isna().any() or ma20.iloc[-2:].isna().any():
        return {"signal": None, "text": "資料不足，無法判斷均線交叉", "light": None}

    prev_diff = ma5.iloc[-2] - ma20.iloc[-2]
    curr_diff = ma5.iloc[-1] - ma20.iloc[-1]

    if prev_diff <= 0 and curr_diff > 0:
        return {"signal": "golden_cross", "text": "黃金交叉：5日均線上穿20日均線，偏多訊號", "light": "green"}
    if prev_diff >= 0 and curr_diff < 0:
        return {"signal": "death_cross", "text": "死亡交叉：5日均線下穿20日均線，偏空訊號", "light": "red"}
    return {"signal": None, "text": "近期無均線交叉", "light": None}


def detect_cost_breach(current_price: float | None, inst_cost: float | None) -> dict:
    """主力成本防守價：現價是否跌破主力估算成本（狀態型，持續顯示直到收復）。"""
    if current_price is None or inst_cost is None:
        return {"breached": None, "text": "資料不足，無法比較主力成本", "light": None}
    if current_price < inst_cost:
        pct = (inst_cost - current_price) / inst_cost * 100
        return {
            "breached": True,
            "text": f"現價已跌破主力估算成本 {pct:.1f}%，主力可能同步套牢，留意籌碼鬆動風險",
            "light": "red",
        }
    return {"breached": False, "text": "現價仍在主力估算成本之上，尚未跌破防守價", "light": "green"}


def detect_score_transition(stock_id: str, composite_score: int, thresholds: dict, state_dir: str) -> dict:
    """綜合評分區間轉換（事件型，跨日比較，需要讀寫 state/ 資料夾裡的上一次紀錄）。"""

    def zone_of(score: int) -> str:
        if score >= thresholds.get("green", 70):
            return "布局"
        if score >= thresholds.get("yellow", 40):
            return "區間"
        return "觀望"

    curr_zone = zone_of(composite_score)
    state_path = Path(state_dir) / f"{stock_id}_state.json"
    prev_zone = None
    if state_path.exists():
        try:
            prev = json.loads(state_path.read_text(encoding="utf-8"))
            prev_zone = prev.get("zone")
        except Exception:  # noqa: BLE001
            prev_zone = None

    Path(state_dir).mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps({"zone": curr_zone, "composite_score": composite_score}, ensure_ascii=False),
        encoding="utf-8",
    )

    if prev_zone is None:
        return {"signal": None, "text": "首次執行，尚無歷史資料可比較區間變化", "light": None}
    if prev_zone == curr_zone:
        return {"signal": None, "text": f"維持在「{curr_zone}」區間，無轉折", "light": None}

    order = {"觀望": 0, "區間": 1, "布局": 2}
    if order[curr_zone] > order[prev_zone]:
        return {"signal": "upgrade", "text": f"評分轉強：由「{prev_zone}」轉為「{curr_zone}」", "light": "green"}
    return {"signal": "downgrade", "text": f"評分轉弱：由「{prev_zone}」轉為「{curr_zone}」", "light": "red"}


def detect_accumulation_signal(price_df: pd.DataFrame, inst_df: pd.DataFrame,
                                lookback_days: int = 20, detail_config: dict | None = None) -> dict:
    """主力建倉訊號（狀態型）：近N個交易日三大法人是否呈現「持續買超、力道加速，
    但股價尚未大幅表態」的悄悄吸籌型態，是判斷主力是否正在建立部位的規則式訊號。

    三個條件同時成立才會觸發：
    1) 買超天數比例 >= buy_ratio_threshold（預設70%）：多數交易日都在買，不是零星買超
    2) 買超力道加速：近半段買超股數(取正值加總) > 前半段買超股數，代表越買越積極
    3) 股價尚未大幅表態：同一期間股價漲幅 <= price_change_cap_pct（預設15%），
       代表法人買超還沒被市場發現、股價還沒被墊高，符合「悄悄建倉」的定義
    只要其中任一條件不成立，就不算建倉訊號，文字說明會具體列出是哪個條件沒過，
    方便你自己判斷是「快接近了」還是「差很遠」。"""
    detail_config = detail_config or {}
    buy_ratio_threshold = detail_config.get("buy_ratio_threshold", 0.7)
    price_change_cap_pct = detail_config.get("price_change_cap_pct", 15)
    min_days = detail_config.get("min_days", 5)

    if price_df.empty or inst_df.empty:
        return {"signal": None, "active": False, "text": "資料不足，無法判斷主力建倉訊號", "light": None}

    price_df = price_df.sort_values("date")
    inst = inst_df.copy()
    inst["net"] = inst["buy"] - inst["sell"]
    daily_net = inst.groupby("date")["net"].sum().reset_index()

    merged = pd.merge(daily_net, price_df[["date", "close"]], on="date", how="inner")
    merged = merged.sort_values("date").tail(lookback_days)

    n = len(merged)
    if n < min_days:
        return {"signal": None, "active": False,
                "text": f"近期可比對資料只有 {n} 個交易日，少於門檻 {min_days} 天，暫不判斷主力建倉訊號",
                "light": None}

    buy_days = merged[merged["net"] > 0]
    buy_ratio = len(buy_days) / n

    half = max(1, n // 2)
    recent_strength = merged.tail(half)["net"].clip(lower=0).sum()
    earlier_strength = merged.head(n - half)["net"].clip(lower=0).sum()
    accelerating = bool(recent_strength > earlier_strength)

    price_change_pct = float((merged["close"].iloc[-1] - merged["close"].iloc[0]) / merged["close"].iloc[0] * 100)

    reasons_failed = []
    if buy_ratio < buy_ratio_threshold:
        reasons_failed.append(f"買超天數比例僅{buy_ratio * 100:.0f}%（門檻{buy_ratio_threshold * 100:.0f}%）")
    if not accelerating:
        reasons_failed.append("買超力道未加速")
    if price_change_pct > price_change_cap_pct:
        reasons_failed.append(f"股價漲幅已達{price_change_pct:+.1f}%（門檻{price_change_cap_pct}%），可能已被市場表態")

    common_fields = {
        "buy_ratio_pct": round(buy_ratio * 100, 1),
        "price_change_pct": round(price_change_pct, 1),
        "sample_days": n,
    }

    if not reasons_failed:
        return {
            "signal": "accumulating",
            "active": True,
            "text": (f"近{n}個交易日買超天數比例{buy_ratio * 100:.0f}%、買超力道加速、"
                     f"同期股價僅{price_change_pct:+.1f}%，符合主力悄悄建倉型態"),
            "light": "green",
            **common_fields,
        }

    return {
        "signal": None,
        "active": False,
        "text": "未觸發主力建倉訊號：" + "；".join(reasons_failed),
        "light": None,
        **common_fields,
    }


def compute_all_signals(stock_id: str, price_df: pd.DataFrame, inst_df: pd.DataFrame, composite_score: int,
                         current_price: float | None, inst_cost: float | None,
                         thresholds: dict, state_dir: str, lookback_days: int = 20,
                         accumulation_detail: dict | None = None) -> dict:
    return {
        "ma_cross": detect_ma_cross(price_df),
        "score_transition": detect_score_transition(stock_id, composite_score, thresholds, state_dir),
        "cost_breach": detect_cost_breach(current_price, inst_cost),
        "accumulation": detect_accumulation_signal(price_df, inst_df, lookback_days, accumulation_detail),
    }
