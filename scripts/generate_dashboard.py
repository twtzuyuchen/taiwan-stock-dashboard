"""
generate_dashboard.py
======================
呈現層：把 analyze.py 算出的結構化結果，套入 HTML 樣板，產出視覺化儀表板。

用法：
    python generate_dashboard.py --config config/config.yaml --stock 2618
    python generate_dashboard.py --stock 2618 --demo   # 用示範資料產出（不需要網路）
"""
from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader

from analyze import analyze_stock

LIGHT_LABELS = {
    "green": "良好",
    "yellow": "留意",
    "red": "警示",
}


def light_pill_class(light: str) -> str:
    return light  # green / yellow / red 對應到 CSS class


def build_context(stock_id: str, stock_name: str, watch_cfg: dict, analysis: dict,
                   lookback_days: int, is_demo: bool) -> dict:
    inst = analysis["institutional_position"]
    chip = analysis["chip_cleanliness"]
    tech = analysis["technical"]
    fund = analysis["fundamental"]

    composite = analysis["composite_score"]
    composite_light = "green" if composite >= 70 else ("yellow" if composite >= 40 else "red")

    recommendation_text, recommendation_class = "建議布局", ""
    if composite < 40:
        recommendation_text, recommendation_class = "建議觀望", "avoid"
    elif composite < 70:
        recommendation_text, recommendation_class = "區間操作", "caution"

    unrealized = inst.get("unrealized_pct")
    if unrealized is None:
        unrealized_text, unrealized_class = "N/A", ""
    else:
        unrealized_text = f"{unrealized:+.2f}%"
        unrealized_class = "pos" if unrealized >= 0 else "neg"

    today = dt.date.today().isoformat()
    info_rows = [
        ("股價日期", today),
        ("融資日期", today),
        ("法人日期", today),
        ("籌碼分佈 CSV 日期", today),
        ("經營績效 CSV 日期", f"{today[:4]}/1Q" if False else today),
        ("SIGNAL_SUMMARY", dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")),
    ]

    lights = [
        {"name": "融資浮額清洗", "light": chip["light"], "status": LIGHT_LABELS[chip["light"]]},
        {"name": "籌碼集中度", "light": inst["light"], "status": LIGHT_LABELS[inst["light"]]},
        {"name": "主力承接", "light": inst["light"], "status": LIGHT_LABELS[inst["light"]]},
        {"name": "技術趨勢", "light": tech["light"], "status": LIGHT_LABELS[tech["light"]]},
        {"name": "長期均線乖離", "light": "green" if tech.get("bias_safe", True) else "red",
         "status": "安全" if tech.get("bias_safe", True) else "偏離過大"},
        {"name": "基本面催化", "light": fund["light"], "status": LIGHT_LABELS[fund["light"]]},
        {"name": "綜合風險", "light": composite_light, "status": LIGHT_LABELS[composite_light]},
    ]

    signals = analysis.get("signals", {})
    ma_cross = signals.get("ma_cross", {})
    score_transition = signals.get("score_transition", {})
    cost_breach = signals.get("cost_breach", {})
    accumulation = signals.get("accumulation", {})
    short_term_entry = signals.get("short_term_entry", {})
    short_term_exit = signals.get("short_term_exit", {})
    swing_entry = signals.get("swing_entry", {})
    swing_exit = signals.get("swing_exit", {})

    def _signal_class(light):
        return light or "neutral"

    def _price_levels(sig):
        levels = []
        if sig.get("stop_loss_price") is not None:
            levels.append({"label": "停損參考價", "value": f"{sig['stop_loss_price']:.2f} 元"})
        if sig.get("take_profit_price") is not None:
            levels.append({"label": "停利參考價", "value": f"{sig['take_profit_price']:.2f} 元"})
        return levels

    signal_cards = [
        {
            "name": "均線黃金／死亡交叉",
            "kind": "事件型",
            "text": ma_cross.get("text", "資料不足"),
            "light": _signal_class(ma_cross.get("light")),
            "active": ma_cross.get("signal") is not None,
        },
        {
            "name": "評分區間轉換",
            "kind": "事件型",
            "text": score_transition.get("text", "資料不足"),
            "light": _signal_class(score_transition.get("light")),
            "active": score_transition.get("signal") is not None,
        },
        {
            "name": "主力成本防守價",
            "kind": "狀態型",
            "text": cost_breach.get("text", "資料不足"),
            "light": _signal_class(cost_breach.get("light")),
            "active": cost_breach.get("breached") is True,
        },
        {
            "name": "主力建倉訊號",
            "kind": "狀態型",
            "text": accumulation.get("text", "資料不足"),
            "light": _signal_class(accumulation.get("light")),
            "active": accumulation.get("active") is True,
        },
        {
            "name": "短線進場提醒（1-2週）",
            "kind": "狀態型",
            "text": short_term_entry.get("text", "資料不足"),
            "light": _signal_class(short_term_entry.get("light")),
            "active": short_term_entry.get("active") is True,
        },
        {
            "name": "短線出場提醒（1-2週）",
            "kind": "狀態型",
            "text": short_term_exit.get("text", "資料不足"),
            "light": _signal_class(short_term_exit.get("light")),
            "active": short_term_exit.get("active") is True,
            "price_levels": _price_levels(short_term_exit),
        },
        {
            "name": "波段進場提醒（1-2個月）",
            "kind": "狀態型",
            "text": swing_entry.get("text", "資料不足"),
            "light": _signal_class(swing_entry.get("light")),
            "active": swing_entry.get("active") is True,
        },
        {
            "name": "波段出場提醒（1-2個月）",
            "kind": "狀態型",
            "text": swing_exit.get("text", "資料不足"),
            "light": _signal_class(swing_exit.get("light")),
            "active": swing_exit.get("active") is True,
            "price_levels": _price_levels(swing_exit),
        },
    ]

    outlook = analysis.get("analyst_outlook", {"available": False})
    outlook_price_change = outlook.get("price_change", {})
    outlook_key_levels = outlook.get("key_levels", {})
    outlook_technical = outlook.get("technical_narrative", {})
    outlook_chip = outlook.get("chip_narrative", {})

    return dict(
        is_demo=is_demo,
        stock_id=stock_id,
        stock_name=stock_name,
        recommendation_text=recommendation_text,
        recommendation_class=recommendation_class,
        analysis_period=watch_cfg.get("analysis_period", "1-3個月"),
        risk_preference=watch_cfg.get("risk_preference", "積極"),
        holding=watch_cfg.get("holding", False),
        # 產業資訊：手動維護於 config.yaml watchlist 各檔股票的設定，非自動抓取（見 config.example.yaml 註解）
        sector=watch_cfg.get("sector", "尚未設定"),
        business_summary=watch_cfg.get("business_summary", "尚未設定，請於 config.yaml 該檔股票補上 business_summary 欄位"),
        us_relation=watch_cfg.get("us_relation", "尚未設定，請於 config.yaml 該檔股票補上 us_relation 欄位"),
        generated_at=dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        info_rows=info_rows,
        composite_score=composite,
        composite_light=composite_light,
        risk_level=analysis["risk_level"],
        risk_light=composite_light,
        chip_score=chip["score"],
        chip_light=chip["light"],
        # 籌碼乾淨度細項（任一子項缺資料時為 None，樣板會顯示「—」）
        chip_momentum_score=chip.get("margin_momentum_score"),
        chip_utilization_score=chip.get("margin_utilization_score"),
        chip_holder_score=chip.get("holder_concentration_score"),
        inst_score=inst["score"],
        inst_light=inst["light"],
        trend_text=tech.get("trend", "N/A"),
        trend_light=tech["light"],
        pattern_text="良好" if composite >= 70 else "普通",
        pattern_light=composite_light,
        bias_text="安全" if tech.get("bias_safe", True) else "偏離過大",
        bias_light="green" if tech.get("bias_safe", True) else "red",
        fund_text=LIGHT_LABELS[fund["light"]],
        fund_light=fund["light"],
        inst_cost_text=(f"{inst['cost']:.2f} 元" if inst.get("cost") else "資料不足"),
        current_price_text=(f"{inst['current_price']:.2f} 元" if inst.get("current_price") else "N/A"),
        # 持有成本卡片（卡片5頂端）在瀏覽器端用 localStorage 記住使用者輸入，需要一個原始數字
        # （不是格式化字串）給 JavaScript 算浮盈虧用；沒有現價資料時傳 None，樣板會處理成 null
        current_price_raw=inst.get("current_price"),
        unrealized_text=unrealized_text,
        unrealized_class=unrealized_class,
        lookback_days=lookback_days,
        buy_days=inst.get("buy_days", 0),
        total_days=inst.get("total_days", lookback_days),
        lights=lights,
        scoring_limit_note="未觸發主力中期或基本面限制" if composite >= 50 else "評分受基本面／籌碼轉弱限制，建議降低部位",
        signal_cards=signal_cards,
        # 分析師關鍵價位與情境策略：完全由 analyze.py 的 compute_analyst_outlook() 自動計算
        # （見 scripts/analyst_outlook.py），不需要任何人工設定，所有追蹤股票都會顯示這張卡片；
        # 唯一的例外是「持有成本」——那是使用者自己的私人交易紀錄，程式無從得知，改成在網頁上
        # 讓使用者自行輸入，存在瀏覽器 localStorage（見樣板內嵌的 JavaScript），不會回傳到伺服器。
        ao_available=outlook.get("available", False),
        ao_reason=outlook.get("reason"),
        ao_price=outlook_price_change.get("price"),
        ao_change_pct=outlook_price_change.get("change_pct"),
        ao_support=outlook_key_levels.get("support"),
        ao_resistance=outlook_key_levels.get("resistance"),
        ao_range_days=outlook_key_levels.get("range_days"),
        ao_expected_range_low=outlook_key_levels.get("expected_range_low"),
        ao_expected_range_high=outlook_key_levels.get("expected_range_high"),
        ao_technical_available=outlook_technical.get("available", False),
        ao_technical_text=outlook_technical.get("text") or outlook_technical.get("reason"),
        ao_chip_available=outlook_chip.get("available", False),
        ao_chip_text=outlook_chip.get("text") or outlook_chip.get("reason"),
        ao_scenarios=outlook.get("scenarios", []),
    )


def render(stock_id: str, config: dict, analysis: dict, template_dir: str,
           output_dir: str, is_demo: bool = False) -> Path:
    watch_cfg = next((w for w in config["watchlist"] if str(w["stock_id"]) == str(stock_id)),
                      {"name": stock_id, "analysis_period": "1-3個月", "risk_preference": "積極", "holding": False,
                       "sector": "尚未設定", "business_summary": "尚未設定", "us_relation": "尚未設定"})
    lookback_days = config.get("finmind", {}).get("lookback_trading_days", 10)

    ctx = build_context(stock_id, watch_cfg.get("name", stock_id), watch_cfg, analysis, lookback_days, is_demo)

    env = Environment(loader=FileSystemLoader(template_dir))
    template = env.get_template("dashboard_template.html")
    html = template.render(**ctx)

    Path(output_dir).mkdir(parents=True, exist_ok=True)
    out_path = Path(output_dir) / f"{stock_id}_dashboard.html"
    out_path.write_text(html, encoding="utf-8")
    return out_path


def demo_analysis(stock_id: str) -> dict:
    """不需要網路的示範資料，數值參考截圖範例但標明為 demo。"""
    return {
        "stock_id": stock_id,
        "composite_score": 89,
        "risk_level": "低",
        "chip_cleanliness": {
            "score": 100, "light": "green",
            "margin_momentum_score": 100, "margin_change_pct": -12.4,
            "margin_utilization_score": 100, "margin_utilization_pct": 14.2,
            "holder_concentration_score": 100, "big_holder_pct": 46.8, "big_holder_pct_change": 1.6,
        },
        "institutional_position": {
            "cost": 68.4, "current_price": 71.2, "unrealized_pct": 4.09,
            "buy_days": 7, "total_days": 10, "score": 92, "light": "green",
        },
        "technical": {"trend": "偏多", "bias_pct": 8.3, "bias_safe": True, "score": 65, "light": "yellow"},
        "fundamental": {"revenue_yoy_pct": 18.4, "per_percentile": 42.0, "score": 78, "light": "green"},
        "signals": {
            "ma_cross": {
                "signal": "golden_cross",
                "text": "黃金交叉：5日均線上穿20日均線，偏多訊號",
                "light": "green",
            },
            "score_transition": {
                "signal": "upgrade",
                "text": "評分轉強：由「區間」轉為「布局」",
                "light": "green",
            },
            "cost_breach": {
                "breached": False,
                "text": "現價仍在主力估算成本之上，尚未跌破防守價",
                "light": "green",
            },
            "accumulation": {
                "signal": "accumulating",
                "active": True,
                "text": "近20個交易日買超天數比例80%、買超力道加速、同期股價僅+6.3%，且出現2項價量型態佐證"
                         "（關鍵價位量增不漲、盤整期量縮至極致），符合主力悄悄建倉型態",
                "light": "green",
                "buy_ratio_pct": 80.0,
                "price_change_pct": 6.3,
                "sample_days": 20,
                "pattern_evidence_confirmed": ["關鍵價位量增不漲", "盤整期量縮至極致"],
                "pattern_evidence_available_count": 3,
            },
            "short_term_entry": {
                "signal": "short_term_entry",
                "active": True,
                "horizon": "短線（約1-2週）",
                "text": "現價貼近近10個交易日低點（68.20元），且2026-08-10於低檔出現槌子線（長下影線），"
                         "今日價量關係為價漲量增（量能達均量165%），符合短線（約1-2週）進場參考條件",
                "light": "green",
                "reference_low": 68.2,
                "pattern_date": "2026-08-10",
                "pattern_name": "槌子線（長下影線）",
            },
            "short_term_exit": {
                "signal": None,
                "active": False,
                "horizon": "短線（約1-2週）",
                "text": "停損參考價 69.30 元（現價-1.2倍ATR）、停利參考價 73.60 元（現價+2.0倍ATR）；目前無技術反轉警訊",
                "light": "green",
                "stop_loss_price": 69.3,
                "take_profit_price": 73.6,
            },
            "swing_entry": {
                "signal": None,
                "active": False,
                "horizon": "波段（約1-2個月）",
                "text": "尚未觸發波段進場訊號：現價71.20元未拉回至近20個交易日低點68.20元附近（需落在73.66元以下）",
                "light": None,
            },
            "swing_exit": {
                "signal": "swing_exit_warning",
                "active": True,
                "horizon": "波段（約1-2個月）",
                "text": "停損參考價 65.80 元（現價-2.0倍ATR）、停利參考價 82.10 元（現價+3.5倍ATR）；"
                         "技術反轉警訊：2026-08-11 於近高點出現流星線（長上影線），建議留意獲利了結或執行停損",
                "light": "red",
                "stop_loss_price": 65.8,
                "take_profit_price": 82.1,
            },
        },
        "analyst_outlook": {
            "available": True,
            "reason": None,
            "price_change": {"available": True, "price": 71.2, "change_pct": 0.85},
            "key_levels": {
                "available": True, "current_price": 71.2, "resistance": 74.5, "support": 68.2,
                "range_days": 20, "ma5": 70.8, "ma20": 69.5, "ma60": 66.4,
                "atr": 1.6, "atr_days": 14, "expected_range_low": 69.6, "expected_range_high": 72.8,
            },
            "technical_narrative": {
                "available": True,
                "text": "技術趨勢判定為「偏多」，距60日均線乖離+7.2%（屬安全範圍）。近20個交易日高低點區間為 "
                        "68.2 - 74.5 元，5日均線70.8元、20日均線69.5元，60日均線66.4元。近5日均量為前20日均量的118%，量能持平。",
            },
            "chip_narrative": {
                "available": True,
                "text": "近20個交易日三大法人買超天數14天；估算主力成本約68.40元；融資餘額近期減少8.2%；"
                        "融資使用率22.5%；大戶持股比例46.8%，較上次上升1.60個百分點。",
            },
            "scenarios": [
                {
                    "name": "情境A：區間整理", "tone": "neutral",
                    "pattern": "現價 71.2 元，介於近20日支撐 68.2 元與壓力 74.5 元之間",
                    "stats_available": True,
                    "stats_text": "ATR（近14日真實波動幅度均值）估算今日可能波動區間為 69.6 - 72.8 元（此為區間整理假設下的參考範圍，非歷史事件統計）",
                },
                {
                    "name": "情境B：站穩突破壓力", "tone": "good",
                    "pattern": "收盤價站穩突破 74.87 元（近20日壓力 × 0.5% 緩衝）",
                    "stats_available": True,
                    "stats_text": "近60日內共11次同類事件，事件發生後5個交易日平均報酬+2.34%、中位數+1.85%、"
                                   "方向延續比例64%（次一關鍵壓力位約 79.30 元，近60日高點）",
                },
                {
                    "name": "情境C：站穩跌破支撐", "tone": "bad",
                    "pattern": "收盤價站穩跌破 67.86 元（近20日支撐 × 0.5% 緩衝）",
                    "stats_available": False,
                    "stats_text": "歷史上符合條件的站穩突破事件只有 5 次，少於門檻 8 次，樣本太少不具參考意義"
                                   "（次一關鍵支撐位約 61.20 元，近60日低點）",
                },
            ],
        },
    }


def main():
    parser = argparse.ArgumentParser(description="產出視覺化儀表板 HTML")
    parser.add_argument("--config", default="config/config.yaml")
    parser.add_argument("--stock", required=True)
    parser.add_argument("--cache-dir", default="output/cache")
    parser.add_argument("--state-dir", default="state")
    parser.add_argument("--template-dir", default="templates")
    parser.add_argument("--output-dir", default="output")
    parser.add_argument("--demo", action="store_true", help="使用示範資料，不需連網")
    args = parser.parse_args()

    with open(args.config, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    if args.demo:
        analysis = demo_analysis(args.stock)
    else:
        analysis = analyze_stock(args.stock, config, args.cache_dir, args.state_dir)

    out_path = render(args.stock, config, analysis, args.template_dir, args.output_dir, is_demo=args.demo)
    print(f"儀表板已產出: {out_path}")


if __name__ == "__main__":
    main()
