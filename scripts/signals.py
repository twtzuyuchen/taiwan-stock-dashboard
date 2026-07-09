"""
signals.py
==========
買賣訊號層：在既有的燈號評分之外，額外提供六種訊號：

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

  E. 左側交易（逢低佈局）進場提醒（狀態型訊號，只要現價在參考低檔區就會持續顯示）
     現價只要跌到「主力估算成本」或「未來一年悲觀價」（本益比河流圖模型）任一價位
     以下，就視為相對低檔、可考慮分批佈局的參考區間，兩者任一成立即觸發（左側交易
     本身就是逆勢承接，門檻刻意設寬，只要有一個具體的估值或籌碼支撐依據即可）。
     這是「參考價位」，不是買點保證——左側交易在跌勢中提前進場，跌破支撐後可能持續
     下探，風險本來就比右側交易高，訊號文字會附帶這個提醒。

  F. 右側交易（確認轉強）進場提醒（狀態型訊號，只要條件持續成立就會持續顯示）
     三個條件同時成立才觸發：
     1) 近 golden_cross_lookback_days（預設10個交易日）內出現過均線黃金交叉，且沒有
        再度死亡交叉，代表轉強是「最近」發生、目前仍延續的
     2) 股價站上近 breakout_lookback_days（預設20個交易日）的收盤新高，代表不只均線
        翻多，價格也已經突破前波壓力
     3) 當日成交量 >= 20日均量 * volume_multiplier（預設1.5倍），代表這次突破有實際
        買盤進場、不是量縮假突破
     任一條件不成立或資料不足，都不算數，文字會列出目前卡在哪個條件。

事件型（A、B）代表「今天發生了什麼變化」；狀態型（C、D、E、F）代表「現在是什麼狀態」。
不同類型用途不同，儀表板會分開顯示。E、F 是進場時機的參考提醒，不是買賣建議，
實際進出場請自行評估風險並考量部位大小。
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

    第二層「價量型態佐證」（輔助條件，第一層過關後才會檢查，三項中至少要有
    min_pattern_evidence 項成立才算通過；資料不足以判斷的項目會從分母排除，不強行湊數）：
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


def detect_left_side_entry(current_price: float | None, inst_cost: float | None,
                            pessimistic_price: float | None, detail_config: dict | None = None) -> dict:
    """左側交易（逢低佈局）進場提醒（狀態型）：現價只要跌到主力估算成本、或未來一年
    悲觀價（本益比河流圖模型）任一價位以下（含 buffer_pct 緩衝），就視為「相對低檔、
    可考慮左側分批佈局」的參考區間，任一條件成立即觸發，不要求兩者同時成立
    （左側交易本身就是逆勢承接，門檻刻意設寬一點，只要有一個具體的估值/籌碼支撐依據即可）。

    提醒：左側交易是在跌勢中提前佈局，跌破支撐後可能持續下探，屬於風險較高的操作方式，
    這裡只是列出規則式計算出的參考價位，不是買點保證，也不構成投資建議。"""
    detail_config = detail_config or {}
    buffer_pct = detail_config.get("buffer_pct", 0)

    if current_price is None:
        return {"signal": None, "active": False, "text": "資料不足，無法判斷左側進場價位", "light": None}
    if inst_cost is None and pessimistic_price is None:
        return {"signal": None, "active": False,
                "text": "缺主力估算成本與模型悲觀價，無法判斷左側進場價位", "light": None}

    reasons_met = []
    if inst_cost is not None and current_price <= inst_cost * (1 + buffer_pct / 100):
        reasons_met.append(f"現價已低於主力估算成本 {inst_cost:.2f} 元")
    if pessimistic_price is not None and current_price <= pessimistic_price * (1 + buffer_pct / 100):
        reasons_met.append(f"現價已低於模型悲觀價 {pessimistic_price:.2f} 元")

    if reasons_met:
        return {
            "signal": "left_side_entry",
            "active": True,
            "text": ("、".join(reasons_met)
                     + "，來到相對低檔參考區間，可考慮左側分批佈局（非買點保證，逆勢承接風險較高，請自行評估部位）"),
            "light": "green",
        }
    return {"signal": None, "active": False,
            "text": "現價尚未跌破主力估算成本或模型悲觀價，暫無左側佈局參考價位", "light": None}


def detect_right_side_entry(price_df: pd.DataFrame, detail_config: dict | None = None) -> dict:
    """右側交易（確認轉強）進場提醒（狀態型）：三個條件同時成立才觸發——
    1) 近 golden_cross_lookback_days（預設10個交易日）內出現過黃金交叉（5日均線上穿
       20日均線），且沒有再度死亡交叉，代表轉強是「最近」發生、目前仍延續的
    2) 股價站上近 breakout_lookback_days（預設20個交易日）的收盤新高，代表不只均線
       翻多，價格也已經突破前波壓力
    3) 當日成交量 >= 20日均量 * volume_multiplier（預設1.5倍），代表這次突破有實際
       買盤進場、不是量縮假突破
    任一條件不成立或資料不足，都不算數，文字會列出目前卡在哪個條件。"""
    detail_config = detail_config or {}
    golden_cross_lookback_days = detail_config.get("golden_cross_lookback_days", 10)
    breakout_lookback_days = detail_config.get("breakout_lookback_days", 20)
    volume_multiplier = detail_config.get("volume_multiplier", 1.5)
    vol_baseline_days = detail_config.get("vol_baseline_days", 20)

    min_rows_needed = max(22, breakout_lookback_days + 1, vol_baseline_days + golden_cross_lookback_days)
    if price_df.empty or len(price_df) < min_rows_needed:
        return {"signal": None, "active": False, "text": "資料不足，無法判斷右側進場訊號", "light": None}

    df = price_df.sort_values("date").reset_index(drop=True)
    close = df["close"].astype(float)
    ma5 = close.rolling(5).mean()
    ma20 = close.rolling(20).mean()
    diff = ma5 - ma20

    reasons_failed = []

    # 1) 近 N 日內是否出現過黃金交叉，且沒有再度死亡交叉
    recent_diff = diff.tail(golden_cross_lookback_days + 1)
    crossed_recently = False
    prev = None
    for val in recent_diff:
        if prev is not None and not pd.isna(prev) and not pd.isna(val) and prev <= 0 and val > 0:
            crossed_recently = True
        prev = val
    if not crossed_recently:
        reasons_failed.append(f"近{golden_cross_lookback_days}個交易日內無均線黃金交叉")
    elif pd.isna(diff.iloc[-1]) or diff.iloc[-1] <= 0:
        reasons_failed.append("均線已再度翻空，黃金交叉未延續")

    # 2) 股價是否站上近 N 日收盤新高（不含今天）
    breakout_window = close.iloc[-(breakout_lookback_days + 1):-1]
    if breakout_window.empty:
        reasons_failed.append("資料不足，無法判斷是否突破近期高點")
    else:
        recent_high = breakout_window.max()
        if not (close.iloc[-1] >= recent_high):
            reasons_failed.append(f"尚未突破近{breakout_lookback_days}個交易日收盤高點 {recent_high:.2f} 元")

    # 3) 成交量是否放大確認
    if "Trading_Volume" not in df.columns:
        reasons_failed.append("缺成交量欄位，無法確認是否放量")
    else:
        vol = pd.to_numeric(df["Trading_Volume"], errors="coerce")
        avg_vol = vol.rolling(vol_baseline_days, min_periods=5).mean().shift(1).iloc[-1]
        today_vol = vol.iloc[-1]
        if pd.isna(avg_vol) or avg_vol <= 0 or pd.isna(today_vol):
            reasons_failed.append("成交量資料不足，無法確認是否放量")
        elif not (today_vol >= avg_vol * volume_multiplier):
            reasons_failed.append(
                f"成交量僅為均量的{today_vol / avg_vol * 100:.0f}%，未達放量確認門檻{volume_multiplier * 100:.0f}%"
            )

    if not reasons_failed:
        return {
            "signal": "right_side_entry",
            "active": True,
            "text": (f"近{golden_cross_lookback_days}個交易日內出現黃金交叉且延續、股價突破近"
                     f"{breakout_lookback_days}個交易日收盤高點、且帶量確認，符合右側轉強進場條件"),
            "light": "green",
        }
    return {
        "signal": None,
        "active": False,
        "text": "尚未觸發右側進場訊號：" + "；".join(reasons_failed),
        "light": None,
    }


def compute_all_signals(stock_id: str, price_df: pd.DataFrame, inst_df: pd.DataFrame, composite_score: int,
                         current_price: float | None, inst_cost: float | None,
                         thresholds: dict, state_dir: str, lookback_days: int = 20,
                         accumulation_detail: dict | None = None,
                         pessimistic_price: float | None = None,
                         left_side_entry_detail: dict | None = None,
                         right_side_entry_detail: dict | None = None) -> dict:
    return {
        "ma_cross": detect_ma_cross(price_df),
        "score_transition": detect_score_transition(stock_id, composite_score, thresholds, state_dir),
        "cost_breach": detect_cost_breach(current_price, inst_cost),
        "accumulation": detect_accumulation_signal(price_df, inst_df, lookback_days, accumulation_detail),
        "left_side_entry": detect_left_side_entry(current_price, inst_cost, pessimistic_price, left_side_entry_detail),
        "right_side_entry": detect_right_side_entry(price_df, right_side_entry_detail),
    }
