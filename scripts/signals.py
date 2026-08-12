"""
signals.py
==========
買賣訊號層：在既有的燈號評分之外，額外提供八種訊號：

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
     判斷邏輯分兩層，缺資料時會自動略過對應層，不會整個訊號失效：

     1) 法人籌碼面（必要條件，三者同時成立才算過關）：
        - 買超天數比例 >= buy_ratio_threshold（預設70%）：多數交易日都在買，不是零星買超
          （這也是「籌碼集中度提升」的量化替代指標——用三大法人買超集中度近似，
          非個別券商分點進出，分點資料目前不在抓取範圍內）
        - 買超力道加速：近半段買超股數(取正值加總) > 前半段買超股數，代表越買越積極
        - 股價尚未大幅表態：同一期間股價漲幅 <= price_change_cap_pct（預設15%）

     2) 價量型態佐證（輔助條件，三項中至少要有 min_pattern_evidence 項成立，
        資料不足以判斷的項目會直接從分母排除，不強行湊數）：
        - 關鍵價位量增不漲：股價接近近期低點時，出現單日成交量明顯放大
          （>= 20日均量 * volume_spike_multiplier）但股價漲跌幅很小
          （<= price_flat_threshold_pct），代表低檔有大量承接
        - 股價強勢破底翻：期間內創新低後，股價收復回來（漲幅 >= reversal_recovery_pct），
          代表主力不願讓價格脫離成本區間，具有下檔保護力
        - 盤整期量縮至極致：近期成交量明顯萎縮（<= 前段均量 * consolidation_shrink_ratio）
          且股價波動幅度收斂（<= consolidation_range_cap_pct），代表籌碼已收乾、賣壓輕

     兩層都通過才會顯示「符合主力悄悄建倉型態」；法人籌碼面沒過關就不會再往下看
     價量型態；法人籌碼面過關但價量型態佐證不足，會明確顯示還缺哪些型態證據。
     不需要額外保存狀態，每次執行都用當次抓到的股價與三大法人買賣超歷史重新判斷。

  E. 短線（約1-2週）進場提醒（狀態型訊號，只要條件持續成立就會持續顯示）
     以「價量關係」與「K線型態」為判斷基礎，三個條件同時成立才觸發：
     1) 現價貼近近 low_lookback_days（預設10個交易日≈2週）交易日低點（含 near_low_pct
        緩衝），代表目前處於短線相對低檔，不是在高檔追價
     2) 近 pattern_lookback_days（預設5個交易日）內，於這個低檔區出現看漲反轉K線型態
        （多頭吞噬，或下影線夠長、上影線夠短的槌子線）
     3) 當日價量關係為「價漲量增」，且成交量達均量 volume_confirm_multiplier（預設1.3）倍
        以上，代表買盤積極介入、不是量縮的雜訊反彈
     三者缺一都不算數，文字會列出卡在哪個條件；這是短線（約1-2週）進出場時機的參考，
     不是買點保證，實際進出場請自行評估風險與部位大小。

  F. 短線（約1-2週）出場提醒（狀態型訊號，同時提供兩種出場依據，任何時候都會顯示）
     1) 停利停損參考價位：用短線 ATR（真實波動幅度，預設回溯10個交易日）估算現價附近的
        正常波動大小，停損參考價 = 現價 - ATR * stop_atr_multiplier（預設1.2倍），
        停利參考價 = 現價 + ATR * take_profit_atr_multiplier（預設2.0倍），只要有近期
        股價資料就會算出來，跟是否曾經觸發過短線進場訊號無關，方便隨時對照手上部位
     2) 技術反轉警訊：現價貼近近期高點時，若出現看跌反轉K線型態（空頭吞噬或流星線／長
        上影線），或當日價量關係為「價跌量增」（跌價又爆量），視為該留意獲利了結或執行
        停損的警訊
     兩種出場依據獨立顯示，警訊觸發時燈號轉紅、文字會具體說明是哪個條件；沒有警訊時
     維持顯示目前算出的停利停損參考價位（燈號綠色，僅供對照，非觸發訊號）。

  G. 波段（約1-2個月）進場提醒（狀態型訊號，只要條件持續成立就會持續顯示）
     邏輯與短線進場提醒相同（貼近低點 + 看漲反轉K線型態 + 價漲量增），但多一層中期趨勢
     過濾、且回溯天數拉長為約1-2個月，四個條件同時成立才觸發：
     1) 中期均線呈多頭排列（trend_ma_fast 預設20日均線 > trend_ma_slow 預設60日均線），
        代表波段格局本身偏多，這一層只是背景過濾，不是直接觸發訊號
     2) 現價拉回至近 pullback_lookback_days（預設20個交易日≈1個月）低點附近（含
        near_low_pct 緩衝），代表拉回找買點，而不是追高
     3) 拉回過程中，近 pattern_lookback_days（預設10個交易日）內出現看漲反轉K線型態
     4) 當日價量關係為「價漲量增」，且量能達均量 volume_confirm_multiplier 倍以上
     四者缺一都不算數；這是波段（約1-2個月）逢低找買點的參考，不是買點保證。

  H. 波段（約1-2個月）出場提醒（狀態型訊號，同時提供兩種出場依據，任何時候都會顯示）
     1) 停利停損參考價位：邏輯與短線出場提醒相同，但改用較長的 ATR 回溯天數（預設20個
        交易日）與較寬的倍數（停損預設2.0倍ATR、停利預設3.5倍ATR），反映波段操作能承受
        較大波動、但停損距離也拉得比短線更遠
     2) 技術反轉警訊：除了看跌反轉K線型態、價跌量增之外，額外加入「中期均線死亡交叉」
        （trend_ma_fast 由上往下穿越 trend_ma_slow）——短線出場只看價格轉折，波段出場
        則多看一層「波段格局本身是否轉弱」
     三項任一觸發即視為警訊，燈號轉紅；沒有警訊時維持顯示目前算出的停利停損參考價位。

事件型（A、B）代表「今天發生了什麼變化」；狀態型（C 到 H）代表「現在是什麼狀態」。
不同類型用途不同，儀表板會分開顯示。E 到 H 是短線／波段進出場時機的參考提醒，不是
買賣建議，實際進出場請自行評估風險並考量部位大小。
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


def _detect_volume_spike_no_rise(price_df: pd.DataFrame, lookback_days: int,
                                  volume_spike_multiplier: float = 1.8,
                                  price_flat_threshold_pct: float = 3.0,
                                  near_low_pct: float = 10.0,
                                  vol_baseline_days: int = 20) -> dict:
    """關鍵價位量增不漲：股價接近近期低點時，若出現單日成交量明顯放大、但股價漲跌幅很小，
    代表有大量資金在低檔承接賣壓，是主力吸籌的經典特徵。"""
    if "Trading_Volume" not in price_df.columns:
        return {"confirmed": None, "available": False, "text": "缺成交量欄位，無法判斷"}

    df = price_df.sort_values("date").copy()
    df["Trading_Volume"] = pd.to_numeric(df["Trading_Volume"], errors="coerce")
    df["avg_vol_prior"] = df["Trading_Volume"].rolling(vol_baseline_days, min_periods=5).mean().shift(1)
    df["price_change_pct"] = df["close"].astype(float).pct_change() * 100

    window = df.tail(lookback_days)
    window = window.dropna(subset=["avg_vol_prior", "price_change_pct"])
    if window.empty:
        return {"confirmed": None, "available": False, "text": "資料不足，無法判斷"}

    low_close = window["close"].astype(float).min()
    near_low_bound = low_close * (1 + near_low_pct / 100)

    matched = window[
        (window["Trading_Volume"] >= window["avg_vol_prior"] * volume_spike_multiplier)
        & (window["price_change_pct"].abs() <= price_flat_threshold_pct)
        & (window["close"].astype(float) <= near_low_bound)
    ]
    if not matched.empty:
        hit = matched.iloc[-1]
        return {
            "confirmed": True, "available": True,
            "text": f"{hit['date']} 於低檔量增不漲（量能達均量{volume_spike_multiplier}倍以上、當日漲跌僅{hit['price_change_pct']:+.1f}%）",
        }
    return {"confirmed": False, "available": True, "text": "近期無低檔量增不漲的跡象"}


def _detect_breakdown_reversal(price_df: pd.DataFrame, lookback_days: int,
                                reversal_recovery_pct: float = 5.0) -> dict:
    """股價強勢破底翻：期間內創新低後又收復回來，代表主力不願讓價格脫離成本區間，
    具備下檔保護力（對應「抗跌」或「破底翻」的價格韌性特徵）。"""
    df = price_df.sort_values("date").tail(lookback_days).reset_index(drop=True)
    if len(df) < 5:
        return {"confirmed": None, "available": False, "text": "資料不足，無法判斷"}

    close = df["close"].astype(float)
    low_idx = close.idxmin()
    low_close = close.iloc[low_idx]
    latest_close = close.iloc[-1]

    if low_idx == len(close) - 1:
        return {"confirmed": False, "available": True, "text": "近期股價仍處於期間低點，尚未出現破底翻"}
    if low_close >= close.iloc[0]:
        return {"confirmed": False, "available": True, "text": "近期未出現明顯破底走勢，無法判斷破底翻"}

    recovery_pct = (latest_close - low_close) / low_close * 100
    if recovery_pct >= reversal_recovery_pct:
        return {
            "confirmed": True, "available": True,
            "text": f"{df['date'].iloc[low_idx]} 創低後強勢收復，至今反彈{recovery_pct:.1f}%",
        }
    return {"confirmed": False, "available": True, "text": f"創低後僅反彈{recovery_pct:.1f}%，尚不足以判斷破底翻"}


def _detect_consolidation_volume_shrink(price_df: pd.DataFrame,
                                         recent_days: int = 5, prior_days: int = 15,
                                         shrink_ratio: float = 0.6,
                                         range_cap_pct: float = 5.0) -> dict:
    """盤整期量縮至極致：近期成交量明顯萎縮、股價波動幅度也收斂，代表籌碼已被主力收乾、
    上方賣壓輕，是規則式判斷的「洗盤尾聲」跡象。"""
    if "Trading_Volume" not in price_df.columns:
        return {"confirmed": None, "available": False, "text": "缺成交量欄位，無法判斷"}

    df = price_df.sort_values("date").copy()
    df["Trading_Volume"] = pd.to_numeric(df["Trading_Volume"], errors="coerce")
    window = df.tail(recent_days + prior_days)
    if len(window) < recent_days + prior_days:
        return {"confirmed": None, "available": False, "text": "資料不足，無法判斷"}

    prior = window.head(prior_days)
    recent = window.tail(recent_days)

    prior_vol_avg = prior["Trading_Volume"].mean()
    recent_vol_avg = recent["Trading_Volume"].mean()
    if pd.isna(prior_vol_avg) or pd.isna(recent_vol_avg) or prior_vol_avg <= 0:
        return {"confirmed": None, "available": False, "text": "成交量資料不足，無法判斷"}

    recent_close = recent["close"].astype(float)
    recent_range_pct = (recent_close.max() - recent_close.min()) / recent_close.mean() * 100

    volume_shrunk = recent_vol_avg <= prior_vol_avg * shrink_ratio
    range_narrow = recent_range_pct <= range_cap_pct

    if volume_shrunk and range_narrow:
        return {
            "confirmed": True, "available": True,
            "text": f"近{recent_days}日均量僅前段的{recent_vol_avg / prior_vol_avg * 100:.0f}%、波動幅度收斂至{recent_range_pct:.1f}%",
        }
    return {"confirmed": False, "available": True,
            "text": f"近{recent_days}日均量為前段的{recent_vol_avg / prior_vol_avg * 100:.0f}%、波動幅度{recent_range_pct:.1f}%，尚未達量縮極致"}


def detect_accumulation_signal(price_df: pd.DataFrame, inst_df: pd.DataFrame,
                                lookback_days: int = 20, detail_config: dict | None = None) -> dict:
    """主力建倉訊號（狀態型）：判斷邏輯分兩層。

    第一層「法人籌碼面」（必要條件，三者同時成立才算過關）：
    1) 買超天數比例 >= buy_ratio_threshold（預設70%）：多數交易日都在買，不是零星買超
    2) 買超力道加速：近半段買超股數(取正值加總) > 前半段買超股數，代表越買越積極
    3) 股價尚未大幅表態：同一期間股價漲幅 <= price_change_cap_pct（預設15%）

    第二層「價量型態佐證」（輔助條件，第一層過關後才會檢查；三項中至少要有
    min_pattern_evidence 項成立才算通過；資料不足以判斷的項目會從分母排除，不硬湊數）：
    a) 關鍵價位量增不漲  b) 股價強勢破底翻  c) 盤整期量縮至極致

    只要第一層任一條件不成立，就不算建倉訊號；第一層過關但第二層佐證不足，也不算，
    文字說明都會具體列出卡在哪一層、哪個條件，方便你判斷是「快接近了」還是「差很遠」。"""
    detail_config = detail_config or {}
    buy_ratio_threshold = detail_config.get("buy_ratio_threshold", 0.7)
    price_change_cap_pct = detail_config.get("price_change_cap_pct", 15)
    min_days = detail_config.get("min_days", 5)
    min_pattern_evidence = detail_config.get("min_pattern_evidence", 2)

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

    core_reasons_failed = []
    if buy_ratio < buy_ratio_threshold:
        core_reasons_failed.append(f"買超天數比例僅{buy_ratio * 100:.0f}%（門檻{buy_ratio_threshold * 100:.0f}%）")
    if not accelerating:
        core_reasons_failed.append("買超力道未加速")
    if price_change_pct > price_change_cap_pct:
        core_reasons_failed.append(f"股價漲幅已達{price_change_pct:+.1f}%（門檻{price_change_cap_pct}%），可能已被市場表態")

    common_fields = {
        "buy_ratio_pct": round(buy_ratio * 100, 1),
        "price_change_pct": round(price_change_pct, 1),
        "sample_days": n,
    }

    if core_reasons_failed:
        return {
            "signal": None,
            "active": False,
            "text": "未觸發主力建倉訊號：" + "；".join(core_reasons_failed),
            "light": None,
            **common_fields,
        }

    # 第一層過關，接著檢查第二層價量型態佐證
    pattern_checks = {
        "關鍵價位量增不漲": _detect_volume_spike_no_rise(
            price_df, lookback_days,
            detail_config.get("volume_spike_multiplier", 1.8),
            detail_config.get("price_flat_threshold_pct", 3.0),
            detail_config.get("near_low_pct", 10.0),
        ),
        "股價強勢破底翻": _detect_breakdown_reversal(
            price_df, lookback_days,
            detail_config.get("reversal_recovery_pct", 5.0),
        ),
        "盤整期量縮至極致": _detect_consolidation_volume_shrink(
            price_df,
            detail_config.get("consolidation_recent_days", 5),
            detail_config.get("consolidation_prior_days", 15),
            detail_config.get("consolidation_shrink_ratio", 0.6),
            detail_config.get("consolidation_range_cap_pct", 5.0),
        ),
    }
    available_checks = {k: v for k, v in pattern_checks.items() if v.get("available")}
    confirmed_names = [k for k, v in available_checks.items() if v.get("confirmed")]
    pattern_fields = {
        "pattern_evidence_confirmed": confirmed_names,
        "pattern_evidence_available_count": len(available_checks),
    }

    core_text = (f"近{n}個交易日買超天數比例{buy_ratio * 100:.0f}%、買超力道加速、"
                 f"同期股價僅{price_change_pct:+.1f}%")

    if not available_checks:
        # 完全沒有可用的成交量資料時，退回只看法人籌碼面（維持原本行為，不因缺資料而失效）
        return {
            "signal": "accumulating",
            "active": True,
            "text": core_text + "，符合主力悄悄建倉型態（缺成交量資料，本次未含價量型態佐證）",
            "light": "green",
            **common_fields,
            **pattern_fields,
        }

    required = min(min_pattern_evidence, len(available_checks))
    if len(confirmed_names) >= required:
        return {
            "signal": "accumulating",
            "active": True,
            "text": (core_text + f"，且出現{len(confirmed_names)}項價量型態佐證（"
                     + "、".join(confirmed_names) + "），符合主力悄悄建倉型態"),
            "light": "green",
            **common_fields,
            **pattern_fields,
        }

    missing = [k for k in available_checks if k not in confirmed_names]
    return {
        "signal": None,
        "active": False,
        "text": (core_text + f"，符合法人買超條件，但價量型態佐證僅{len(confirmed_names)}/"
                 f"{len(available_checks)}項（尚缺：" + "、".join(missing) + "），暫不判定為主力建倉"),
        "light": "yellow",
        **common_fields,
        **pattern_fields,
    }


# ------------------------------------------------------------------------
# 短線／波段進出場訊號共用的 K線型態與價量關係判斷工具
# ------------------------------------------------------------------------

_OHLC_COLS = {"open", "max", "min", "close"}


def _candle_shape(row) -> dict:
    """把一根K線拆解成型態判斷需要的基本量：實體、上影線、下影線、當日漲跌方向。"""
    o, h, l, c = float(row["open"]), float(row["max"]), float(row["min"]), float(row["close"])
    body = abs(c - o)
    upper_shadow = h - max(o, c)
    lower_shadow = min(o, c) - l
    return {
        "date": row["date"], "open": o, "high": h, "low": l, "close": c,
        "body": body, "upper_shadow": upper_shadow, "lower_shadow": lower_shadow,
        "bullish": c > o,
    }


def _detect_bullish_reversal_candle(df_window: pd.DataFrame, low_bound: float,
                                     hammer_shadow_ratio: float = 2.0,
                                     hammer_upper_cap_ratio: float = 0.5) -> dict:
    """在 df_window（依日期排序）中，尋找收盤價落在 low_bound（含）以下才有效的看漲反轉
    K線型態，只看兩種公認型態，避免主觀判讀：
      1) 多頭吞噬：當日收紅，且實體完全吞噬前一日的黑K實體（今日開盤<=昨收、今日收盤>=昨開）
      2) 槌子線：下影線長度 >= 實體 * hammer_shadow_ratio（下影線夠長），且上影線長度
         <= 實體 * hammer_upper_cap_ratio（上影線夠短），代表當日一度重挫但收復大半、
         下方有買盤承接
    型態出現在低檔以外的地方（例如高檔整理時）不列入判斷，因為看漲反轉型態只有出現在
    相對低檔才具備「落底」意義。回傳型態窗口內「最近一次」的出現紀錄。"""
    if not _OHLC_COLS.issubset(df_window.columns):
        return {"confirmed": None, "available": False, "text": "缺開高低收欄位，無法判斷K線型態"}
    if len(df_window) < 2:
        return {"confirmed": None, "available": False, "text": "資料不足，無法判斷K線型態"}

    rows = df_window.sort_values("date").reset_index(drop=True)
    hits = []
    for i in range(1, len(rows)):
        cur = _candle_shape(rows.iloc[i])
        prev = _candle_shape(rows.iloc[i - 1])
        if cur["close"] > low_bound:
            continue  # 不在相對低檔區間內，型態不列入判斷

        is_engulfing = (
            cur["bullish"] and not prev["bullish"]
            and cur["open"] <= prev["close"] and cur["close"] >= prev["open"]
            and cur["body"] >= prev["body"]
        )
        is_hammer = (
            cur["body"] > 0
            and cur["lower_shadow"] >= cur["body"] * hammer_shadow_ratio
            and cur["upper_shadow"] <= cur["body"] * hammer_upper_cap_ratio
        )
        if is_engulfing or is_hammer:
            hits.append({"date": cur["date"], "pattern": "多頭吞噬" if is_engulfing else "槌子線（長下影線）"})

    if not hits:
        return {"confirmed": False, "available": True, "text": "低檔區近期無看漲反轉K線型態"}
    last = hits[-1]
    return {
        "confirmed": True, "available": True,
        "text": f"{last['date']} 於低檔出現{last['pattern']}，具看漲反轉意味",
        "pattern_date": last["date"], "pattern_name": last["pattern"],
    }


def _detect_bearish_reversal_candle(df_window: pd.DataFrame, high_bound: float,
                                     shooting_star_shadow_ratio: float = 2.0,
                                     shooting_star_lower_cap_ratio: float = 0.5) -> dict:
    """跟 _detect_bullish_reversal_candle 邏輯對稱，改成在高檔（收盤價 >= high_bound）
    尋找看跌反轉K線型態：
      1) 空頭吞噬：當日收黑，且實體完全吞噬前一日的紅K實體
      2) 流星線（射擊之星）：上影線夠長（>= 實體 * shooting_star_shadow_ratio）、下影線夠短
         （<= 實體 * shooting_star_lower_cap_ratio），代表當日一度大漲但收盤回落，上方有賣壓"""
    if not _OHLC_COLS.issubset(df_window.columns):
        return {"confirmed": None, "available": False, "text": "缺開高低收欄位，無法判斷K線型態"}
    if len(df_window) < 2:
        return {"confirmed": None, "available": False, "text": "資料不足，無法判斷K線型態"}

    rows = df_window.sort_values("date").reset_index(drop=True)
    hits = []
    for i in range(1, len(rows)):
        cur = _candle_shape(rows.iloc[i])
        prev = _candle_shape(rows.iloc[i - 1])
        if cur["close"] < high_bound:
            continue  # 不在相對高檔區間內，型態不列入判斷

        is_engulfing = (
            not cur["bullish"] and prev["bullish"]
            and cur["open"] >= prev["close"] and cur["close"] <= prev["open"]
            and cur["body"] >= prev["body"]
        )
        is_shooting_star = (
            cur["body"] > 0
            and cur["upper_shadow"] >= cur["body"] * shooting_star_shadow_ratio
            and cur["lower_shadow"] <= cur["body"] * shooting_star_lower_cap_ratio
        )
        if is_engulfing or is_shooting_star:
            hits.append({"date": cur["date"], "pattern": "空頭吞噬" if is_engulfing else "流星線（長上影線）"})

    if not hits:
        return {"confirmed": False, "available": True, "text": "高檔區近期無看跌反轉K線型態"}
    last = hits[-1]
    return {
        "confirmed": True, "available": True,
        "text": f"{last['date']} 於高檔出現{last['pattern']}，具看跌反轉意味",
        "pattern_date": last["date"], "pattern_name": last["pattern"],
    }


def _volume_price_relationship(price_df: pd.DataFrame, vol_baseline_days: int = 20) -> dict:
    """判斷「最新一個交易日」的價量關係：價漲量增／價漲量縮／價跌量增／價跌量縮。
    價漲量增代表買盤積極追價，訊號較可信；價漲量縮代表追價力道不足，訊號需要保留；
    價跌量增代表出貨／恐慌賣壓重；價跌量縮代表賣壓輕，可能進入表態末端。"""
    if "Trading_Volume" not in price_df.columns:
        return {"available": False, "text": "缺成交量欄位，無法判斷價量關係"}

    df = price_df.sort_values("date").copy()
    df["Trading_Volume"] = pd.to_numeric(df["Trading_Volume"], errors="coerce")
    df["avg_vol_prior"] = df["Trading_Volume"].rolling(vol_baseline_days, min_periods=5).mean().shift(1)
    df["price_change_pct"] = df["close"].astype(float).pct_change() * 100

    latest = df.iloc[-1]
    if pd.isna(latest["avg_vol_prior"]) or pd.isna(latest["price_change_pct"]) or latest["avg_vol_prior"] <= 0:
        return {"available": False, "text": "資料不足，無法判斷價量關係"}

    vol_ratio = float(latest["Trading_Volume"] / latest["avg_vol_prior"])
    price_change_pct = float(latest["price_change_pct"])
    price_up = price_change_pct > 0
    vol_up = vol_ratio >= 1.0

    if price_up and vol_up:
        label = "價漲量增"
    elif price_up and not vol_up:
        label = "價漲量縮"
    elif not price_up and vol_up:
        label = "價跌量增"
    else:
        label = "價跌量縮"

    return {
        "available": True, "label": label,
        "price_change_pct": round(price_change_pct, 2),
        "volume_ratio": round(vol_ratio, 2),
        "text": f"今日{label}（漲跌{price_change_pct:+.1f}%、量能為均量{vol_ratio * 100:.0f}%）",
    }


def _atr(price_df: pd.DataFrame, atr_days: int) -> float | None:
    """計算 ATR（Average True Range，真實波動幅度均值），衡量近期正常波動大小，
    作為停利停損參考價位的距離依據（跟市場情境頁面「加權指數關鍵點位」用的方法一致）。"""
    if not {"max", "min", "close"}.issubset(price_df.columns) or len(price_df) < atr_days + 1:
        return None
    df = price_df.sort_values("date").copy()
    high = df["max"].astype(float)
    low = df["min"].astype(float)
    close = df["close"].astype(float)
    prev_close = close.shift(1)
    true_range = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    atr = true_range.rolling(atr_days).mean().iloc[-1]
    return float(atr) if pd.notna(atr) else None


def detect_short_term_entry(price_df: pd.DataFrame, detail_config: dict | None = None) -> dict:
    """短線（約1-2週）進場提醒（狀態型）：三個條件同時成立才觸發——
    1) 現價貼近近 low_lookback_days（預設10個交易日≈2週）交易日低點（含 near_low_pct 緩衝）
    2) 近 pattern_lookback_days（預設5個交易日）內，於這個低檔區出現看漲反轉K線型態
       （多頭吞噬或槌子線）
    3) 當日價量關係為「價漲量增」，且量能達均量 volume_confirm_multiplier（預設1.3）倍以上
    三者缺一都不算數，文字會列出目前卡在哪個條件；這是短線（約1-2週）進場時機的參考，
    不是買點保證，實際進出場請自行評估風險與部位大小。"""
    detail_config = detail_config or {}
    low_lookback_days = detail_config.get("low_lookback_days", 10)
    near_low_pct = detail_config.get("near_low_pct", 6.0)
    pattern_lookback_days = detail_config.get("pattern_lookback_days", 5)
    vol_baseline_days = detail_config.get("vol_baseline_days", 20)
    volume_confirm_multiplier = detail_config.get("volume_confirm_multiplier", 1.3)

    min_rows = max(low_lookback_days, vol_baseline_days, pattern_lookback_days) + 5
    if price_df.empty or len(price_df) < min_rows or not _OHLC_COLS.issubset(price_df.columns):
        return {"signal": None, "active": False, "text": "資料不足，無法判斷短線進場訊號", "light": None,
                "horizon": "短線（約1-2週）"}

    df = price_df.sort_values("date").reset_index(drop=True)
    recent_close = float(df["close"].iloc[-1])
    low_window = df.tail(low_lookback_days)
    period_low = float(low_window["min"].astype(float).min())
    low_bound = period_low * (1 + near_low_pct / 100)

    reasons_failed = []
    if recent_close > low_bound:
        reasons_failed.append(f"現價{recent_close:.2f}元未貼近近{low_lookback_days}個交易日低點{period_low:.2f}元（需落在{low_bound:.2f}元以下）")

    candle = _detect_bullish_reversal_candle(df.tail(pattern_lookback_days + 1), low_bound)
    if not candle.get("available"):
        reasons_failed.append(candle.get("text", "K線型態資料不足"))
    elif not candle.get("confirmed"):
        reasons_failed.append(f"近{pattern_lookback_days}個交易日低檔區無看漲反轉K線型態")

    vol_rel = _volume_price_relationship(df, vol_baseline_days)
    if not vol_rel.get("available"):
        reasons_failed.append("成交量資料不足，無法確認價量關係")
    else:
        volume_ok = vol_rel["label"] == "價漲量增" and vol_rel["volume_ratio"] >= volume_confirm_multiplier
        if not volume_ok:
            reasons_failed.append(
                f"今日價量關係為「{vol_rel['label']}」（量能為均量{vol_rel['volume_ratio'] * 100:.0f}%），"
                f"未達價漲量增且量能≥均量{volume_confirm_multiplier * 100:.0f}%的確認門檻"
            )

    if reasons_failed:
        return {"signal": None, "active": False, "horizon": "短線（約1-2週）",
                "text": "尚未觸發短線進場訊號：" + "；".join(reasons_failed), "light": None}

    return {
        "signal": "short_term_entry", "active": True, "horizon": "短線（約1-2週）",
        "text": (f"現價貼近近{low_lookback_days}個交易日低點（{period_low:.2f}元），"
                 f"且{candle['pattern_date']}於低檔出現{candle['pattern_name']}，今日價量關係為價漲量增"
                 f"（量能達均量{vol_rel['volume_ratio'] * 100:.0f}%），符合短線（約1-2週）進場參考條件"),
        "light": "green",
        "reference_low": round(period_low, 2),
        "pattern_date": candle.get("pattern_date"),
        "pattern_name": candle.get("pattern_name"),
    }


def detect_short_term_exit(price_df: pd.DataFrame, detail_config: dict | None = None) -> dict:
    """短線（約1-2週）出場提醒（狀態型）：同時提供兩種出場依據——
    1) 停利停損參考價位：以現價為基準，用短線 ATR（預設回溯10個交易日）估算正常波動大小，
       停損參考價 = 現價 - ATR * stop_atr_multiplier（預設1.2倍），
       停利參考價 = 現價 + ATR * take_profit_atr_multiplier（預設2.0倍）；
       只要有近期股價資料就能算，跟是否觸發過進場訊號無關，任何時候都可以拿來對照手上部位
    2) 技術反轉警訊：現價貼近近 high_lookback_days（預設10個交易日）高點時，若出現看跌反轉
       K線型態（空頭吞噬或流星線），或當日價量關係為「價跌量增」，視為該留意獲利了結或
       執行停損的警訊
    兩種依據獨立顯示：有警訊時燈號轉紅、文字具體說明是哪個條件；沒有警訊時仍會照樣顯示
    目前算出的停利停損參考價位（燈號綠色），不是只在有警訊時才顯示。"""
    detail_config = detail_config or {}
    atr_days = detail_config.get("atr_days", 10)
    stop_atr_multiplier = detail_config.get("stop_atr_multiplier", 1.2)
    take_profit_atr_multiplier = detail_config.get("take_profit_atr_multiplier", 2.0)
    high_lookback_days = detail_config.get("high_lookback_days", 10)
    near_high_pct = detail_config.get("near_high_pct", 6.0)
    pattern_lookback_days = detail_config.get("pattern_lookback_days", 5)
    vol_baseline_days = detail_config.get("vol_baseline_days", 20)
    volume_warn_multiplier = detail_config.get("volume_warn_multiplier", 1.3)

    min_rows = max(atr_days, high_lookback_days, vol_baseline_days, pattern_lookback_days) + 5
    if price_df.empty or len(price_df) < min_rows or not _OHLC_COLS.issubset(price_df.columns):
        return {"signal": None, "active": False, "text": "資料不足，無法判斷短線出場參考價位", "light": None,
                "horizon": "短線（約1-2週）"}

    df = price_df.sort_values("date").reset_index(drop=True)
    recent_close = float(df["close"].iloc[-1])
    atr = _atr(df, atr_days)
    if atr is None:
        return {"signal": None, "active": False, "text": "ATR資料不足，無法估算短線停利停損價位", "light": None,
                "horizon": "短線（約1-2週）"}

    stop_loss_price = recent_close - atr * stop_atr_multiplier
    take_profit_price = recent_close + atr * take_profit_atr_multiplier

    high_window = df.tail(high_lookback_days)
    period_high = float(high_window["max"].astype(float).max())
    high_bound = period_high * (1 - near_high_pct / 100)

    reversal_hit = None
    if recent_close >= high_bound:
        candle = _detect_bearish_reversal_candle(df.tail(pattern_lookback_days + 1), high_bound)
        if candle.get("confirmed"):
            reversal_hit = candle

    vol_rel = _volume_price_relationship(df, vol_baseline_days)
    volume_warn = bool(
        vol_rel.get("available") and vol_rel["label"] == "價跌量增"
        and vol_rel["volume_ratio"] >= volume_warn_multiplier
    )

    base_text = f"停損參考價 {stop_loss_price:.2f} 元（現價-{stop_atr_multiplier}倍ATR）、停利參考價 {take_profit_price:.2f} 元（現價+{take_profit_atr_multiplier}倍ATR）"
    common_fields = {
        "horizon": "短線（約1-2週）",
        "stop_loss_price": round(stop_loss_price, 2),
        "take_profit_price": round(take_profit_price, 2),
    }

    warnings = []
    if reversal_hit:
        warnings.append(f"{reversal_hit['pattern_date']} 於近高點出現{reversal_hit['pattern_name']}")
    if volume_warn:
        warnings.append(f"今日價跌量增（量能達均量{vol_rel['volume_ratio'] * 100:.0f}%），賣壓浮現")

    if warnings:
        return {
            "signal": "short_term_exit_warning", "active": True, "light": "red",
            "text": base_text + "；技術反轉警訊：" + "；".join(warnings) + "，建議留意獲利了結或執行停損",
            **common_fields,
        }
    return {
        "signal": None, "active": False, "light": "green",
        "text": base_text + "；目前無技術反轉警訊",
        **common_fields,
    }


def detect_swing_entry(price_df: pd.DataFrame, detail_config: dict | None = None) -> dict:
    """波段（約1-2個月）進場提醒（狀態型）：邏輯與短線進場提醒相同（貼近低點 + 看漲反轉
    K線型態 + 價漲量增），但多一層中期趨勢過濾、回溯天數拉長為約1-2個月，四個條件同時
    成立才觸發：
    1) 中期均線呈多頭排列（trend_ma_fast 預設20日均線 > trend_ma_slow 預設60日均線），
       代表波段格局本身偏多——這一層只是背景過濾條件，不是直接觸發訊號
    2) 現價拉回至近 pullback_lookback_days（預設20個交易日≈1個月）低點附近（含
       near_low_pct 緩衝），代表拉回找買點，不是追高
    3) 拉回過程中，近 pattern_lookback_days（預設10個交易日）內出現看漲反轉K線型態
    4) 當日價量關係為「價漲量增」，且量能達均量 volume_confirm_multiplier 倍以上
    四者缺一都不算數；這是波段（約1-2個月）逢低找買點的參考，不是買點保證。"""
    detail_config = detail_config or {}
    trend_ma_fast = detail_config.get("trend_ma_fast", 20)
    trend_ma_slow = detail_config.get("trend_ma_slow", 60)
    pullback_lookback_days = detail_config.get("pullback_lookback_days", 20)
    near_low_pct = detail_config.get("near_low_pct", 8.0)
    pattern_lookback_days = detail_config.get("pattern_lookback_days", 10)
    vol_baseline_days = detail_config.get("vol_baseline_days", 20)
    volume_confirm_multiplier = detail_config.get("volume_confirm_multiplier", 1.3)

    min_rows = max(trend_ma_slow, pullback_lookback_days, vol_baseline_days, pattern_lookback_days) + 5
    if price_df.empty or len(price_df) < min_rows or not _OHLC_COLS.issubset(price_df.columns):
        return {"signal": None, "active": False, "text": "資料不足，無法判斷波段進場訊號", "light": None,
                "horizon": "波段（約1-2個月）"}

    df = price_df.sort_values("date").reset_index(drop=True)
    close = df["close"].astype(float)
    ma_fast = close.rolling(trend_ma_fast).mean()
    ma_slow = close.rolling(trend_ma_slow).mean()
    if pd.isna(ma_fast.iloc[-1]) or pd.isna(ma_slow.iloc[-1]):
        return {"signal": None, "active": False, "text": "均線資料不足，無法判斷波段進場訊號", "light": None,
                "horizon": "波段（約1-2個月）"}

    reasons_failed = []
    if not (ma_fast.iloc[-1] > ma_slow.iloc[-1]):
        reasons_failed.append(
            f"中期均線未呈多頭排列（{trend_ma_fast}日均線{ma_fast.iloc[-1]:.2f}元 <= "
            f"{trend_ma_slow}日均線{ma_slow.iloc[-1]:.2f}元），波段格局尚未轉多"
        )

    recent_close = float(close.iloc[-1])
    low_window = df.tail(pullback_lookback_days)
    period_low = float(low_window["min"].astype(float).min())
    low_bound = period_low * (1 + near_low_pct / 100)
    if recent_close > low_bound:
        reasons_failed.append(f"現價{recent_close:.2f}元未拉回至近{pullback_lookback_days}個交易日低點{period_low:.2f}元附近（需落在{low_bound:.2f}元以下）")

    candle = _detect_bullish_reversal_candle(df.tail(pattern_lookback_days + 1), low_bound)
    if not candle.get("available"):
        reasons_failed.append(candle.get("text", "K線型態資料不足"))
    elif not candle.get("confirmed"):
        reasons_failed.append(f"近{pattern_lookback_days}個交易日拉回區間無看漲反轉K線型態")

    vol_rel = _volume_price_relationship(df, vol_baseline_days)
    if not vol_rel.get("available"):
        reasons_failed.append("成交量資料不足，無法確認價量關係")
    else:
        volume_ok = vol_rel["label"] == "價漲量增" and vol_rel["volume_ratio"] >= volume_confirm_multiplier
        if not volume_ok:
            reasons_failed.append(
                f"今日價量關係為「{vol_rel['label']}」（量能為均量{vol_rel['volume_ratio'] * 100:.0f}%），"
                f"未達價漲量增且量能≥均量{volume_confirm_multiplier * 100:.0f}%的確認門檻"
            )

    if reasons_failed:
        return {"signal": None, "active": False, "horizon": "波段（約1-2個月）",
                "text": "尚未觸發波段進場訊號：" + "；".join(reasons_failed), "light": None}

    return {
        "signal": "swing_entry", "active": True, "horizon": "波段（約1-2個月）",
        "text": (f"{trend_ma_fast}日均線在{trend_ma_slow}日均線之上、波段格局偏多，現價拉回至近"
                 f"{pullback_lookback_days}個交易日低點（{period_low:.2f}元）附近，且{candle['pattern_date']}"
                 f"出現{candle['pattern_name']}，今日價量關係為價漲量增（量能達均量{vol_rel['volume_ratio'] * 100:.0f}%），"
                 f"符合波段（約1-2個月）逢低進場參考條件"),
        "light": "green",
        "reference_low": round(period_low, 2),
        "pattern_date": candle.get("pattern_date"),
        "pattern_name": candle.get("pattern_name"),
    }


def detect_swing_exit(price_df: pd.DataFrame, detail_config: dict | None = None) -> dict:
    """波段（約1-2個月）出場提醒（狀態型）：同時提供兩種出場依據——
    1) 停利停損參考價位：邏輯與短線出場提醒相同，但改用較長的 ATR 回溯天數（預設20個
       交易日）與較寬的倍數（停損預設2.0倍ATR、停利預設3.5倍ATR），反映波段操作能承受
       較大波動、停損距離也拉得比短線更遠
    2) 技術反轉警訊：除了「看跌反轉K線型態」「價跌量增」之外，額外加入「中期均線死亡
       交叉」（trend_ma_fast 由上往下穿越 trend_ma_slow）——短線出場只看價格轉折，波段
       出場則多看一層「波段格局本身是否轉弱」
    三項任一觸發即視為警訊，燈號轉紅；沒有警訊時仍會照樣顯示目前算出的停利停損參考價位
    （燈號綠色）。"""
    detail_config = detail_config or {}
    atr_days = detail_config.get("atr_days", 20)
    stop_atr_multiplier = detail_config.get("stop_atr_multiplier", 2.0)
    take_profit_atr_multiplier = detail_config.get("take_profit_atr_multiplier", 3.5)
    high_lookback_days = detail_config.get("high_lookback_days", 20)
    near_high_pct = detail_config.get("near_high_pct", 8.0)
    pattern_lookback_days = detail_config.get("pattern_lookback_days", 10)
    vol_baseline_days = detail_config.get("vol_baseline_days", 20)
    volume_warn_multiplier = detail_config.get("volume_warn_multiplier", 1.3)
    trend_ma_fast = detail_config.get("trend_ma_fast", 20)
    trend_ma_slow = detail_config.get("trend_ma_slow", 60)

    min_rows = max(atr_days, high_lookback_days, vol_baseline_days, pattern_lookback_days, trend_ma_slow + 1) + 5
    if price_df.empty or len(price_df) < min_rows or not _OHLC_COLS.issubset(price_df.columns):
        return {"signal": None, "active": False, "text": "資料不足，無法判斷波段出場參考價位", "light": None,
                "horizon": "波段（約1-2個月）"}

    df = price_df.sort_values("date").reset_index(drop=True)
    recent_close = float(df["close"].iloc[-1])
    atr = _atr(df, atr_days)
    if atr is None:
        return {"signal": None, "active": False, "text": "ATR資料不足，無法估算波段停利停損價位", "light": None,
                "horizon": "波段（約1-2個月）"}

    stop_loss_price = recent_close - atr * stop_atr_multiplier
    take_profit_price = recent_close + atr * take_profit_atr_multiplier

    high_window = df.tail(high_lookback_days)
    period_high = float(high_window["max"].astype(float).max())
    high_bound = period_high * (1 - near_high_pct / 100)

    reversal_hit = None
    if recent_close >= high_bound:
        candle = _detect_bearish_reversal_candle(df.tail(pattern_lookback_days + 1), high_bound)
        if candle.get("confirmed"):
            reversal_hit = candle

    vol_rel = _volume_price_relationship(df, vol_baseline_days)
    volume_warn = bool(
        vol_rel.get("available") and vol_rel["label"] == "價跌量增"
        and vol_rel["volume_ratio"] >= volume_warn_multiplier
    )

    close = df["close"].astype(float)
    ma_fast = close.rolling(trend_ma_fast).mean()
    ma_slow = close.rolling(trend_ma_slow).mean()
    trend_break = False
    if not (ma_fast.iloc[-2:].isna().any() or ma_slow.iloc[-2:].isna().any()):
        prev_diff = ma_fast.iloc[-2] - ma_slow.iloc[-2]
        curr_diff = ma_fast.iloc[-1] - ma_slow.iloc[-1]
        trend_break = bool(prev_diff >= 0 and curr_diff < 0)

    base_text = f"停損參考價 {stop_loss_price:.2f} 元（現價-{stop_atr_multiplier}倍ATR）、停利參考價 {take_profit_price:.2f} 元（現價+{take_profit_atr_multiplier}倍ATR）"
    common_fields = {
        "horizon": "波段（約1-2個月）",
        "stop_loss_price": round(stop_loss_price, 2),
        "take_profit_price": round(take_profit_price, 2),
    }

    warnings = []
    if reversal_hit:
        warnings.append(f"{reversal_hit['pattern_date']} 於近高點出現{reversal_hit['pattern_name']}")
    if volume_warn:
        warnings.append(f"今日價跌量增（量能達均量{vol_rel['volume_ratio'] * 100:.0f}%），賣壓浮現")
    if trend_break:
        warnings.append(f"{trend_ma_fast}日均線下穿{trend_ma_slow}日均線，波段中期趨勢轉弱")

    if warnings:
        return {
            "signal": "swing_exit_warning", "active": True, "light": "red",
            "text": base_text + "；技術反轉警訊：" + "；".join(warnings) + "，建議留意獲利了結或執行停損",
            **common_fields,
        }
    return {
        "signal": None, "active": False, "light": "green",
        "text": base_text + "；目前無技術反轉警訊",
        **common_fields,
    }


def compute_all_signals(stock_id: str, price_df: pd.DataFrame, inst_df: pd.DataFrame, composite_score: int,
                         current_price: float | None, inst_cost: float | None,
                         thresholds: dict, state_dir: str, lookback_days: int = 20,
                         accumulation_detail: dict | None = None,
                         short_term_entry_detail: dict | None = None,
                         short_term_exit_detail: dict | None = None,
                         swing_entry_detail: dict | None = None,
                         swing_exit_detail: dict | None = None) -> dict:
    return {
        "ma_cross": detect_ma_cross(price_df),
        "score_transition": detect_score_transition(stock_id, composite_score, thresholds, state_dir),
        "cost_breach": detect_cost_breach(current_price, inst_cost),
        "accumulation": detect_accumulation_signal(price_df, inst_df, lookback_days, accumulation_detail),
        "short_term_entry": detect_short_term_entry(price_df, short_term_entry_detail),
        "short_term_exit": detect_short_term_exit(price_df, short_term_exit_detail),
        "swing_entry": detect_swing_entry(price_df, swing_entry_detail),
        "swing_exit": detect_swing_exit(price_df, swing_exit_detail),
    }
