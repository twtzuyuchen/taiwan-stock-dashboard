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
