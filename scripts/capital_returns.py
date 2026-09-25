"""
capital_returns.py
====================
資本回報與獲利品質（儀表板卡片6）：計算「已動用資本回報率」
（Return on Capital Employed, ROCE）最近 5 個完整會計年度的趨勢，並附加三項判讀輔助：

    1) 長期趨勢：近5年ROCE是持續上升、大致上升、持平、還是下降？年度間波動大不大？
       （持續上升 → 資金配置效率隨時間優化；劇烈波動 → 獲利穩定度不足）
    2) 高於資金成本：ROCE 是否高於公司自身的「隱含借款利率」（利息費用 ÷ 有息負債）？
       只有 ROCE > 借款利率，才代表這些資本運用「淨創造」了價值，而不只是覆蓋掉借款成本。
       （注意：這只是用「借款成本」當資金成本的簡化代理，不是完整的加權平均資金成本
       WACC，WACC 還要納入股東權益的機會成本，本工具沒有計算）
    3) 與同業對比：這項刻意「沒有」自動計算。台灣證交所／公開資訊觀測站都沒有現成的
       「產業別平均ROCE」開放資料庫；唯一可行的替代方案（用 FinMind 抓同產業別所有上市
       公司財報自己算平均）需要大量額外 API 呼叫（每個產業別可能50+家公司），有超過
       FinMind 免費額度的風險，經評估後決定不實作，改用文字提醒使用者自行對照同業數字。

ROCE 定義（本專案採用的簡化版本）：
    ROCE = 年度 EBIT（以「營業利益 OperatingIncome」作為代理，未扣除業外損益）
           ÷ 年底已動用資本（總資產 － 流動負債）

隱含借款利率定義：
    借款利率 = 年度利息費用 ÷ 年底有息負債（短期借款 + 長期借款 + 應付公司債，
    三者中有比對到哪些就加總哪些，找不到任何一項則視為無法計算）

資料來源（皆為 scripts/fetch_data.py 抓取、long-format 的 FinMind 資料集，
欄位皆為 date, stock_id, type, value, origin_name）：
    - TaiwanStockFinancialStatements（損益表，逐季單季值）：加總同一年度的4季數值
      得到年度合計；只有湊滿4季的年度才視為「完整會計年度」。EBIT 與利息費用都用這個方法。
    - TaiwanStockBalanceSheet（資產負債表，逐季快照）：取每年度最後一筆（通常是Q4/年報）
      的數值，作為年底水位。總資產、流動負債、有息負債的各項組成都用這個方法。

穩健性設計：FinMind 資料集的 `type` 欄位命名可能隨版本微調（大小寫、底線等），
本模組用「候選名稱列表」比對（不分大小寫），真的比對不到時，回傳的 reason 會列出
該資料集實際出現過的 type 名稱，方便直接對照調整下方的候選清單。ROCE 本體（EBIT/
總資產/流動負債）比對不到會讓整張卡片不可用；隱含借款利率（利息費用/有息負債）比對
不到只會讓「高於資金成本」這部分顯示資料不足，不影響 ROCE 本體的計算與顯示。
"""
from __future__ import annotations

import math

import pandas as pd

# 各指標可能對應到的 FinMind `type` 欄位候選名稱（依常見程度排序，比對時不分大小寫）
_EBIT_TYPE_CANDIDATES = [
    "OperatingIncome", "Operating_Income", "營業利益", "EBIT",
]
_TOTAL_ASSETS_TYPE_CANDIDATES = [
    "TotalAssets", "Total_Assets", "資產總額", "資產總計",
]
_CURRENT_LIABILITIES_TYPE_CANDIDATES = [
    "CurrentLiabilities", "Current_Liabilities", "流動負債",
]
_INTEREST_EXPENSE_TYPE_CANDIDATES = [
    "InterestExpense", "Interest_Expense", "利息費用", "利息支出", "FinanceCosts", "Finance_Costs",
]
# 有息負債的組成項目：每一組是同一個概念的候選名稱，三組中比對到哪些就加總哪些
# （缺的項目視為0，不是整體失敗；三組都比對不到才視為無法計算有息負債）
_DEBT_COMPONENT_TYPE_CANDIDATES = [
    ["ShorttermBorrowings", "Shortterm_Borrowings", "短期借款"],
    ["LongtermBorrowings", "Longterm_Borrowings", "長期借款"],
    ["BondsPayable", "Bonds_Payable", "應付公司債"],
]


def _pick_type(df: pd.DataFrame, candidates: list[str]) -> str | None:
    """在 df 的 type 欄位裡，依候選清單找出實際存在的名稱（不分大小寫），找不到回傳 None。"""
    if df is None or df.empty or "type" not in df.columns:
        return None
    available = df["type"].astype(str).unique().tolist()
    lower_map = {t.lower(): t for t in available}
    for cand in candidates:
        if cand.lower() in lower_map:
            return lower_map[cand.lower()]
    return None


def _found_types(df: pd.DataFrame, limit: int = 20) -> list[str]:
    if df is None or df.empty or "type" not in df.columns:
        return []
    return sorted(df["type"].astype(str).unique().tolist())[:limit]


def _annual_sum_by_type(financial_df: pd.DataFrame, value_type: str) -> dict[int, float]:
    """把逐季數值（單季值）依年份加總，回傳 {year: annual_sum}。
    只保留「該年度已經有4季資料」的年份，避免用不完整年度低估合計數。"""
    df = financial_df[financial_df["type"] == value_type].copy()
    if df.empty:
        return {}
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"])
    df["year"] = df["date"].dt.year
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df = df.dropna(subset=["value"])
    if df.empty:
        return {}

    counts = df.groupby("year")["value"].count()
    sums = df.groupby("year")["value"].sum()
    complete_years = counts[counts >= 4].index
    return {int(y): float(sums[y]) for y in complete_years}


def _year_end_balance(balance_df: pd.DataFrame, picked_type: str) -> dict[int, float]:
    """取資產負債表每年度最後一筆（年底/Q4）快照值，回傳 {year: value}。"""
    df = balance_df[balance_df["type"] == picked_type].copy()
    if df.empty:
        return {}
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"])
    df["year"] = df["date"].dt.year
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df = df.dropna(subset=["value"])
    if df.empty:
        return {}

    df = df.sort_values("date")
    last_per_year = df.groupby("year").last()["value"]
    return {int(y): float(v) for y, v in last_per_year.items()}


def _interest_bearing_debt_year_end(balance_df: pd.DataFrame) -> tuple[dict[int, float], list[str]]:
    """取每年度年底的有息負債合計（短期借款＋長期借款＋應付公司債，缺的項目視為0）。
    回傳 (年度合計字典, 實際比對到的 type 名稱清單)；三個組成項目全部比對不到時，
    回傳 ({}, [])，代表這個資料集無法算有息負債。"""
    matched_names: list[str] = []
    per_component_year_values: list[dict[int, float]] = []
    for candidates in _DEBT_COMPONENT_TYPE_CANDIDATES:
        picked = _pick_type(balance_df, candidates)
        if picked is not None:
            matched_names.append(picked)
            per_component_year_values.append(_year_end_balance(balance_df, picked))

    if not matched_names:
        return {}, []

    all_years: set[int] = set()
    for d in per_component_year_values:
        all_years |= set(d.keys())

    result = {y: sum(d.get(y, 0.0) for d in per_component_year_values) for y in all_years}
    return result, matched_names


def _classify_trend(roce_values: list[float], volatility_cv_threshold: float) -> dict:
    """依「由舊到新」排列的 ROCE 數值序列，判斷長期趨勢方向與波動度。

    方向：逐年比較（後一年 - 前一年），全部上升 -> 持續上升；全部下降 -> 持續下降；
    上升次數 > 下降次數 -> 大致上升；反之 -> 大致下降；打平 -> 持平震盪。
    波動度：用變動係數（標準差 ÷ 平均值的絕對值）衡量，>= volatility_cv_threshold
    （預設0.3，即30%）視為「劇烈波動」。"""
    if len(roce_values) < 2:
        return {"available": False}

    diffs = [roce_values[i] - roce_values[i - 1] for i in range(1, len(roce_values))]
    up = sum(1 for d in diffs if d > 0)
    down = sum(1 for d in diffs if d < 0)
    n_diffs = len(diffs)

    mean = sum(roce_values) / len(roce_values)
    variance = sum((v - mean) ** 2 for v in roce_values) / len(roce_values)
    std = math.sqrt(variance)
    cv = (std / abs(mean)) if mean else None

    if up == n_diffs:
        direction = "持續上升"
    elif down == n_diffs:
        direction = "持續下降"
    elif up > down:
        direction = "大致上升"
    elif down > up:
        direction = "大致下降"
    else:
        direction = "持平震盪"

    volatility_high = bool(cv is not None and cv >= volatility_cv_threshold)

    return {
        "available": True,
        "direction": direction,
        "std_pct": round(std, 2),
        "cv_pct": round(cv * 100, 1) if cv is not None else None,
        "volatility_high": volatility_high,
    }


def _trend_narrative(trend: dict, n_years: int) -> str | None:
    """把 _classify_trend() 的結構化結果組成一句敘述文字。"""
    if not trend.get("available"):
        return None
    direction = trend["direction"]
    cv_pct = trend.get("cv_pct")
    volatility_high = trend.get("volatility_high")

    if direction in ("持續上升", "大致上升"):
        base = f"近{n_years}年ROCE呈「{direction}」趨勢"
        if volatility_high:
            tail = f"，但年度間波動較大（變動係數約{cv_pct}%），獲利穩定度可能不足，建議留意個別年度的異常原因"
        else:
            tail = f"，且年度間波動度穩定（變動係數約{cv_pct}%），代表資金配置效率隨時間優化"
    elif direction in ("持續下降", "大致下降"):
        base = f"近{n_years}年ROCE呈「{direction}」趨勢"
        if volatility_high:
            tail = f"，且年度間波動較大（變動係數約{cv_pct}%），資金配置效率與獲利穩定度都可能轉弱，建議進一步檢視原因"
        else:
            tail = f"（變動係數約{cv_pct}%），資金配置效率有轉弱跡象"
    else:
        base = f"近{n_years}年ROCE呈「{direction}」，沒有明顯持續上升或下降的方向"
        if volatility_high:
            tail = f"，且年度間波動較大（變動係數約{cv_pct}%），獲利穩定度不足"
        else:
            tail = f"，年度間波動度尚屬穩定（變動係數約{cv_pct}%）"
    return base + tail


def compute_roce_history(financial_df: pd.DataFrame, balance_df: pd.DataFrame,
                          detail_config: dict | None = None) -> dict:
    """計算最近 N 個完整會計年度的 ROCE 趨勢（預設 5 年），並附加長期趨勢判讀與
    「是否高於隱含借款利率」判讀。「與同業對比」刻意不自動計算，見模組開頭說明。

    ROCE = 年度 EBIT（以營業利益 OperatingIncome 為代理）÷ 年底已動用資本（總資產 － 流動負債）

    回傳格式：
    {
      "available": bool,
      "reason": str | None,     # 不可用時的原因
      "note": str | None,       # 可用但有需要留意的地方（例如年度不足5年）
      "years": [
        {"year": 2021, "ebit": ..., "capital_employed": ..., "roce_pct": ...,
         "implied_borrowing_rate_pct": float | None, "value_creating": bool | None},
        ...
      ],  # 由舊到新
      "avg_roce_pct": float | None,
      "latest_roce_pct": float | None,
      "trend": {"available": bool, "direction": str, "std_pct": float, "cv_pct": float,
                "volatility_high": bool, "narrative": str} | {"available": False},
      "cost_of_capital_available": bool,
      "cost_of_capital_reason": str | None,   # 不可用時的原因（例如找不到利息費用或有息負債欄位）
      "latest_cost_of_capital_narrative": str | None,
    }
    """
    detail_config = detail_config or {}
    years = int(detail_config.get("years", 5))
    volatility_cv_threshold = float(detail_config.get("volatility_cv_threshold", 0.3))

    empty_extra = {
        "trend": {"available": False},
        "cost_of_capital_available": False,
        "cost_of_capital_reason": None,
        "latest_cost_of_capital_narrative": None,
    }

    if financial_df is None or financial_df.empty:
        return {"available": False,
                "reason": "缺少損益表（TaiwanStockFinancialStatements）資料，"
                          "請確認 fetch_data.py 是否已抓取 financial_statements 資料集",
                "note": None, "years": [], "avg_roce_pct": None, "latest_roce_pct": None,
                **empty_extra}
    if balance_df is None or balance_df.empty:
        return {"available": False,
                "reason": "缺少資產負債表（TaiwanStockBalanceSheet）資料，"
                          "請確認 fetch_data.py 是否已抓取 balance_sheet 資料集",
                "note": None, "years": [], "avg_roce_pct": None, "latest_roce_pct": None,
                **empty_extra}

    ebit_type = _pick_type(financial_df, _EBIT_TYPE_CANDIDATES)
    if ebit_type is None:
        return {"available": False,
                "reason": f"損益表資料中找不到營業利益(EBIT代理)欄位，實際出現的 type 有：{_found_types(financial_df)}",
                "note": None, "years": [], "avg_roce_pct": None, "latest_roce_pct": None,
                **empty_extra}

    assets_type = _pick_type(balance_df, _TOTAL_ASSETS_TYPE_CANDIDATES)
    liab_type = _pick_type(balance_df, _CURRENT_LIABILITIES_TYPE_CANDIDATES)
    if assets_type is None or liab_type is None:
        missing = []
        if assets_type is None:
            missing.append("總資產")
        if liab_type is None:
            missing.append("流動負債")
        return {"available": False,
                "reason": f"資產負債表資料中找不到「{'、'.join(missing)}」欄位，實際出現的 type 有：{_found_types(balance_df)}",
                "note": None, "years": [], "avg_roce_pct": None, "latest_roce_pct": None,
                **empty_extra}

    ebit_by_year = _annual_sum_by_type(financial_df, ebit_type)
    assets_by_year = _year_end_balance(balance_df, assets_type)
    liab_by_year = _year_end_balance(balance_df, liab_type)

    common_years = sorted(set(ebit_by_year) & set(assets_by_year) & set(liab_by_year))
    if not common_years:
        return {"available": False,
                "reason": "損益表與資產負債表沒有重疊的完整年度資料，可能是新上市股票或資料回溯天數不足",
                "note": None, "years": [], "avg_roce_pct": None, "latest_roce_pct": None,
                **empty_extra}

    common_years = common_years[-years:]

    rows = []
    for y in common_years:
        ebit = ebit_by_year[y]
        capital_employed = assets_by_year[y] - liab_by_year[y]
        roce_pct = round(ebit / capital_employed * 100, 2) if capital_employed else None
        rows.append({
            "year": y,
            "ebit": round(ebit, 0),
            "capital_employed": round(capital_employed, 0),
            "roce_pct": roce_pct,
        })

    valid_roce = [r["roce_pct"] for r in rows if r["roce_pct"] is not None]
    avg_roce = round(sum(valid_roce) / len(valid_roce), 2) if valid_roce else None
    latest_roce = rows[-1]["roce_pct"] if rows else None

    note = None
    if len(rows) < years:
        note = f"僅取得 {len(rows)} 個完整會計年度資料（預期 {years} 年），可能是新上市股票或資料回溯天數不足"

    # 長期趨勢判讀（用實際算出的 ROCE 數列，由舊到新）
    trend = _classify_trend(valid_roce, volatility_cv_threshold)
    if trend.get("available"):
        trend["narrative"] = _trend_narrative(trend, len(valid_roce))

    # 是否高於隱含借款利率（資金成本的簡化代理）
    interest_type = _pick_type(financial_df, _INTEREST_EXPENSE_TYPE_CANDIDATES)
    debt_by_year, matched_debt_types = _interest_bearing_debt_year_end(balance_df)

    cost_of_capital_available = interest_type is not None and bool(matched_debt_types)
    cost_of_capital_reason = None
    if not cost_of_capital_available:
        missing = []
        if interest_type is None:
            missing.append("利息費用")
        if not matched_debt_types:
            missing.append("有息負債（短期借款／長期借款／應付公司債）")
        cost_of_capital_reason = (
            f"損益表或資產負債表中找不到「{'、'.join(missing)}」欄位，無法計算隱含借款利率；"
            f"實際出現的損益表 type 有：{_found_types(financial_df)}"
        )
        for row in rows:
            row["implied_borrowing_rate_pct"] = None
            row["value_creating"] = None
    else:
        interest_by_year = _annual_sum_by_type(financial_df, interest_type)
        for row in rows:
            y = row["year"]
            interest = interest_by_year.get(y)
            debt = debt_by_year.get(y)
            if interest is not None and debt:
                rate_pct = round(interest / debt * 100, 2)
                row["implied_borrowing_rate_pct"] = rate_pct
                row["value_creating"] = (row["roce_pct"] is not None and row["roce_pct"] > rate_pct)
            else:
                row["implied_borrowing_rate_pct"] = None
                row["value_creating"] = None

    latest_cost_of_capital_narrative = None
    if rows:
        latest = rows[-1]
        if latest.get("implied_borrowing_rate_pct") is not None and latest.get("roce_pct") is not None:
            rate = latest["implied_borrowing_rate_pct"]
            roce_v = latest["roce_pct"]
            if latest["value_creating"]:
                latest_cost_of_capital_narrative = (
                    f"最新年度（{latest['year']}）ROCE {roce_v}% 高於估算的隱含借款利率 {rate}%，"
                    f"顯示這一年資本運用的報酬有覆蓋借款成本並創造超額價值（此處僅以借款成本近似資金成本，"
                    f"未納入股東權益的機會成本，不是完整的加權平均資金成本 WACC）"
                )
            else:
                latest_cost_of_capital_narrative = (
                    f"最新年度（{latest['year']}）ROCE {roce_v}% 未高於估算的隱含借款利率 {rate}%，"
                    f"顯示這一年資本運用的報酬可能不足以覆蓋借款成本，未必有淨創造股東價值"
                    f"（此處僅以借款成本近似資金成本，未納入股東權益的機會成本，不是完整的WACC）"
                )
        elif not cost_of_capital_available:
            latest_cost_of_capital_narrative = None

    return {
        "available": True,
        "reason": None,
        "note": note,
        "years": rows,
        "avg_roce_pct": avg_roce,
        "latest_roce_pct": latest_roce,
        "trend": trend,
        "cost_of_capital_available": cost_of_capital_available,
        "cost_of_capital_reason": cost_of_capital_reason,
        "latest_cost_of_capital_narrative": latest_cost_of_capital_narrative,
    }
