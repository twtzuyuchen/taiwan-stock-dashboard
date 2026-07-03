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

    return dict(
        is_demo=is_demo,
        stock_id=stock_id,
        stock_name=stock_name,
        recommendation_text=recommendation_text,
        recommendation_class=recommendation_class,
        analysis_period=watch_cfg.get("analysis_period", "1-3個月"),
        risk_preference=watch_cfg.get("risk_preference", "積極"),
        holding=watch_cfg.get("holding", False),
        generated_at=dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        info_rows=info_rows,
        composite_score=composite,
        composite_light=composite_light,
        risk_level=analysis["risk_level"],
        risk_light=composite_light,
        chip_score=chip["score"],
        chip_light=chip["light"],
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
        unrealized_text=unrealized_text,
        unrealized_class=unrealized_class,
        lookback_days=lookback_days,
        buy_days=inst.get("buy_days", 0),
        total_days=inst.get("total_days", lookback_days),
        lights=lights,
        scoring_limit_note="未觸發主力中期或基本面限制" if composite >= 50 else "評分受基本面／籌碼轉弱限制，建議降低部位",
    )


def render(stock_id: str, config: dict, analysis: dict, template_dir: str,
           output_dir: str, is_demo: bool = False) -> Path:
    watch_cfg = next((w for w in config["watchlist"] if str(w["stock_id"]) == str(stock_id)),
                      {"name": stock_id, "analysis_period": "1-3個月", "risk_preference": "積極", "holding": False})
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
        "chip_cleanliness": {"score": 100, "light": "green"},
        "institutional_position": {
            "cost": 68.4, "current_price": 71.2, "unrealized_pct": 4.09,
            "buy_days": 7, "total_days": 10, "score": 92, "light": "green",
        },
        "technical": {"trend": "偏多", "bias_pct": 8.3, "bias_safe": True, "score": 65, "light": "yellow"},
        "fundamental": {"revenue_yoy_pct": 18.4, "per_percentile": 42.0, "score": 78, "light": "green"},
    }


def main():
    parser = argparse.ArgumentParser(description="產出視覺化儀表板 HTML")
    parser.add_argument("--config", default="config/config.yaml")
    parser.add_argument("--stock", required=True)
    parser.add_argument("--cache-dir", default="output/cache")
    parser.add_argument("--template-dir", default="templates")
    parser.add_argument("--output-dir", default="output")
    parser.add_argument("--demo", action="store_true", help="使用示範資料，不需連網")
    args = parser.parse_args()

    with open(args.config, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    if args.demo:
        analysis = demo_analysis(args.stock)
    else:
        analysis = analyze_stock(args.stock, config, args.cache_dir)

    out_path = render(args.stock, config, analysis, args.template_dir, args.output_dir, is_demo=args.demo)
    print(f"儀表板已產出: {out_path}")


if __name__ == "__main__":
    main()
