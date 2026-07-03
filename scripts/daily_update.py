"""
daily_update.py
================
主排程入口：收盤後（建議每日 19:00）執行一次，完成：
  1. 抓取 watchlist 內每檔股票的最新資料 (fetch_data)
  2. 計算主力成本與燈號評分 (analyze)
  3. 產出每檔股票的 HTML 儀表板 + 一個總覽 index.html

用法：
    python daily_update.py --config config/config.yaml
"""
from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader

from analyze import analyze_stock
from fetch_data import fetch_all
from generate_dashboard import render

INDEX_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-Hant"><head><meta charset="UTF-8">
<title>台股波段 AI Agent 儀表板總覽</title>
<style>
body{font-family:system-ui,-apple-system,"Noto Sans TC",sans-serif;background:#f9f9f7;color:#0b0b0b;padding:32px;}
.wrap{max-width:760px;margin:0 auto;}
h1{font-size:22px;}
.item{display:flex;justify-content:space-between;align-items:center;background:#fcfcfb;border:1px solid rgba(11,11,11,.1);
border-radius:10px;padding:14px 18px;margin-bottom:10px;text-decoration:none;color:inherit;}
.item .score{font-weight:700;}
.meta{color:#898781;font-size:13px;margin-bottom:20px;}
</style></head><body><div class="wrap">
<h1>台股波段 AI Agent 儀表板總覽</h1>
<div class="meta">更新時間：{{ updated_at }}</div>
{% for row in rows %}
<a class="item" href="{{ row.stock_id }}_dashboard.html">
  <span>{{ row.stock_id }} {{ row.name }}</span>
  <span class="score">{{ row.score }}/100</span>
</a>
{% endfor %}
</div></body></html>
"""


def run(config_path: str, demo: bool = False) -> None:
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    output_dir = config.get("output", {}).get("dashboard_dir", "output")
    template_dir = "templates"
    cache_dir = f"{output_dir}/cache"

    summary_rows = []
    for w in config["watchlist"]:
        stock_id = w["stock_id"]
        print(f"\n=== {stock_id} {w['name']} ===")
        if demo:
            from generate_dashboard import demo_analysis
            analysis = demo_analysis(stock_id)
        else:
            fetch_all(stock_id, config, cache_dir)
            analysis = analyze_stock(stock_id, config, cache_dir)

        out_path = render(stock_id, config, analysis, template_dir, output_dir, is_demo=demo)
        print(f"  -> {out_path}")

        summary_rows.append({"stock_id": stock_id, "name": w["name"], "score": analysis["composite_score"]})

    # 產出總覽頁
    env = Environment()
    index_html = env.from_string(INDEX_TEMPLATE).render(
        updated_at=dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        rows=sorted(summary_rows, key=lambda r: -r["score"]),
    )
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    (Path(output_dir) / "index.html").write_text(index_html, encoding="utf-8")

    # 若設定 GitHub Pages 發佈，複製一份到 docs/
    out_cfg = config.get("output", {})
    if out_cfg.get("publish_github_pages"):
        import shutil
        pages_dir = Path(out_cfg.get("github_pages_dir", "docs"))
        pages_dir.mkdir(parents=True, exist_ok=True)
        for html_file in Path(output_dir).glob("*.html"):
            shutil.copy(html_file, pages_dir / html_file.name)
        print(f"已同步輸出到 {pages_dir}/（可搭配 GitHub Pages 發佈）")


def main():
    parser = argparse.ArgumentParser(description="每日收盤後總排程：抓資料→分析→產出儀表板")
    parser.add_argument("--config", default="config/config.yaml")
    parser.add_argument("--demo", action="store_true")
    args = parser.parse_args()
    run(args.config, demo=args.demo)


if __name__ == "__main__":
    main()
