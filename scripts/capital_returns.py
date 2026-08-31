"""
capital_returns.py
====================
資本回報與獲利品質（儀表板卡片6）：計算「已動用資本回報率」
（Return on Capital Employed, ROCE）最近 5 個完整會計年度的趨勢。

ROCE 定義（本專案採用的簡化版本，跨產業比較時仍建議搭配產業特性判讀）：
    ROCE = 年度 EBIT（以「營業利益 OperatingIncome」作為代理，未扣除業外損益）
           ÷ 年底已動用資本（總資產 － 流動負債）

資料來源（皆為 scripts/fetch_data.py 抓取、long-format 的 FinMind 資料集，
欄位皆為 date, stock_id, type, value, origin_name）：
    - TaiwanStockFinancialStatements（損益表，逐季單季值）：加總同一年度的4季 OperatingIncome
      得到年度 EBIT 代理值；只有湊滿4季的年度才視為「完整會計年度」。
    - TaiwanStockBalanceSheet（資產負債表，逐季快照）：取每年度最後一筆（通常是Q4/年報）的
      TotalAssets 與 CurrentLiabilities 作為年底已動用資本的組成。

穩健性設計：FinMind 兩個資料集的 `type` 欄位命名可能隨版本微調（大小寫、底線等），
本模組用「候選名稱列表」比對（不分大小寫），真的比對不到時，回傳的 reason 會列出
該資料集實際出現過的 type 名稱，方便直接對照調整下方的候選清單。
"""
from __future__ import annotations

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


def _annual_ebit(financial_df: pd.DataFrame, ebit_type: str) -> dict[int, float]:
    """把逐季 OperatingIncome（單季值）依年份加總，回傳 {year: ebit_annual}。
    只保留「該年度已經有4季資料」的年份，避免用不完整年度低估EBIT。"""
    df = financial_df[financial_df["type"] == ebit_type].copy()
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


def compute_roce_history(financial_df: pd.DataFrame, balance_df: pd.DataFrame,
                          detail_config: dict | None = None) -> dict:
    """計算最近 N 個完整會計年度的 ROCE 趨勢（預設 5 年）。

    ROCE = 年度 EBIT（以營業利益 OperatingIncome 為代理）÷ 年底已動用資本（總資產 － 流動負債）

    回傳格式：
    {
      "available": bool,
      "reason": str | None,     # 不可用時的原因
      "note": str | None,       # 可用但有需要留意的地方（例如年度不足5年）
      "years": [{"year": 2021, "ebit": ..., "capital_employed": ..., "roce_pct": ...}, ...],  # 由舊到新
      "avg_roce_pct": float | None,
      "latest_roce_pct": float | None,
    }
    """
    detail_config = detail_config or {}
    years = int(detail_config.get("years", 5))

    if financial_df is None or financial_df.empty:
        return {"available": False,
                "reason": "缺少損益表（TaiwanStockFinancialStatements）資料，"
                          "請確認 fetch_data.py 是否已抓取 financial_statements 資料集",
                "note": None, "years": [], "avg_roce_pct": None, "latest_roce_pct": None}
    if balance_df is None or balance_df.empty:
        return {"available": False,
                "reason": "缺少資產負債表（TaiwanStockBalanceSheet）資料，"
                          "請確認 fetch_data.py 是否已抓取 balance_sheet 資料集",
                "note": None, "years": [], "avg_roce_pct": None, "latest_roce_pct": None}

    ebit_type = _pick_type(financial_df, _EBIT_TYPE_CANDIDATES)
    if ebit_type is None:
        return {"available": False,
                "reason": f"損益表資料中找不到營業利益(EBIT代理)欄位，實際出現的 type 有：{_found_types(financial_df)}",
                "note": None, "years": [], "avg_roce_pct": None, "latest_roce_pct": None}

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
                "note": None, "years": [], "avg_roce_pct": None, "latest_roce_pct": None}

    ebit_by_year = _annual_ebit(financial_df, ebit_type)
    assets_by_year = _year_end_balance(balance_df, assets_type)
    liab_by_year = _year_end_balance(balance_df, liab_type)

    common_years = sorted(set(ebit_by_year) & set(assets_by_year) & set(liab_by_year))
    if not common_years:
        return {"available": False,
                "reason": "損益表與資產負債表沒有重疊的完整年度資料，可能是新上市股票或資料回溯天數不足",
                "note": None, "years": [], "avg_roce_pct": None, "latest_roce_pct": None}

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

    return {
        "available": True,
        "reason": None,
        "note": note,
        "years": rows,
        "avg_roce_pct": avg_roce,
        "latest_roce_pct": latest_roce,
    }
