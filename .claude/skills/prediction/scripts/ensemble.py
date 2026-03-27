"""
Prediction Skill
AI 预测技能 - 集成多 ML 模型预测（XGBoost/LightGBM/LSTM 融合）

依赖: xgboost, lightgbm, tensorflow, scikit-learn, akshare
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger


def _load_ensemble():
    """加载模型融合器（延迟加载，避免启动慢）"""
    try:
        # prediction/ 目录视为包，将其父目录加入 sys.path
        # 这样 prediction.models.ensemble_model 内部的相对 import 才能正常工作
        prediction_dir = Path(__file__).parent.parent
        pkg_root = str(prediction_dir.parent)
        if pkg_root not in sys.path:
            sys.path.insert(0, pkg_root)
        from prediction.models.ensemble_model import get_model_ensemble
        return get_model_ensemble()
    except Exception as e:
        logger.error(f"[Prediction] Failed to load ML models: {e}")
        return None


def _generate_risks(direction: str, confidence: str) -> List[str]:
    risks = []
    if confidence == "低":
        risks.append("预测置信度较低，建议观望")
    if direction == "UP":
        risks.append("关注市场整体风险，设置止损位")
    elif direction == "DOWN":
        risks.append("趋势偏弱，谨慎操作")
    else:
        risks.append("横盘整理中，等待方向明确")
    risks.append("以上预测仅供参考，不构成投资建议")
    return risks


def _build_valuation_from_financial(financial_data: Dict[str, Any], existing_valuation: Dict[str, Any]) -> Dict[str, Any]:
    """
    将 financial_report skill 返回的真实财务数据合并到 valuation 结构中。
    真实数据优先，缺失字段回退到 existing_valuation。
    """
    if not financial_data:
        return existing_valuation

    # financial_report skill 可能的字段名（兼容多种返回格式）
    # 尝试从 metrics / data / summary / 顶层等路径提取
    metrics = (
        financial_data.get("metrics")
        or financial_data.get("data")
        or financial_data.get("summary")
        or financial_data
    )

    def _get(*keys, default=None):
        for k in keys:
            v = metrics.get(k) if isinstance(metrics, dict) else None
            if v is not None and v != "" and v != 0 and str(v) != "nan":
                return v
            # 也从顶层 financial_data 找
            v2 = financial_data.get(k)
            if v2 is not None and v2 != "" and v2 != 0 and str(v2) != "nan":
                return v2
        return default

    merged = {
        "current": {
            "roe": _get("roe", "roe_ttm", default=existing_valuation.get("current", {}).get("roe", 10)),
            "revenue_yoy": _get("revenue_yoy", "revenue_growth", default=existing_valuation.get("current", {}).get("revenue_yoy", 0)),
            "profit_yoy": _get("profit_yoy", "net_profit_yoy", "profit_growth", default=existing_valuation.get("current", {}).get("profit_yoy", 0)),
            "gross_margin": _get("gross_margin", "gross_profit_margin", default=existing_valuation.get("current", {}).get("gross_margin", 30)),
            "debt_ratio": _get("debt_ratio", "asset_liability_ratio", default=existing_valuation.get("current", {}).get("debt_ratio", 50)),
            "total_mv": _get("total_mv", "market_cap", "total_market_value", default=existing_valuation.get("current", {}).get("total_mv", 0)),
        },
        "percentiles": existing_valuation.get("percentiles", {}),
    }

    # 如果 financial_data 中有 pe_ttm_pct / pb_pct（百分位），补充到 percentiles
    pe_pct = _get("pe_ttm_pct", "pe_percentile")
    pb_pct = _get("pb_pct", "pb_percentile")
    if pe_pct is not None:
        merged["percentiles"]["pe_percentile"] = pe_pct
    if pb_pct is not None:
        merged["percentiles"]["pb_percentile"] = pb_pct

    return merged


def _build_money_flow_from_skill(money_flow_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    将 money_flow skill 返回的真实资金数据标准化为 extract_features 期望的格式。
    money_flow skill 已返回 summary 字段，直接透传，补充缺失字段。
    """
    if not money_flow_data:
        return {}

    summary = money_flow_data.get("summary", {})
    daily = money_flow_data.get("daily", [])

    # 从 daily 数据计算大单/超大单占比（如果 summary 中没有）
    large_order_ratio = summary.get("large_order_ratio", 0)
    super_large_pct = summary.get("super_large_pct", 0)

    if daily and not large_order_ratio:
        # 取最近5日的大单均值
        recent = daily[-5:] if len(daily) >= 5 else daily
        large_vals = [abs(d.get("large_net", 0)) for d in recent]
        total_vals = [abs(d.get("total_amount", 1)) or 1 for d in recent]
        large_order_ratio = sum(large_vals) / sum(total_vals) if sum(total_vals) > 0 else 0

    return {
        "summary": {
            "main_net_inflow_5d": summary.get("main_net_3d", summary.get("main_net_5d", 0)) * 1e4,  # 万元→元
            "main_net_inflow_20d": summary.get("main_net_20d", 0) * 1e4,
            "flow_stability": summary.get("flow_stability", 0),
            "north_bound_change": summary.get("north_bound_change", 0),
            "margin_balance_change": summary.get("margin_balance_change", 0),
        },
        "fund_flow": {
            "large_order_ratio": large_order_ratio,
            "super_large_pct": super_large_pct,
        },
        **{k: v for k, v in money_flow_data.items() if k not in ("summary", "daily", "for_llm")},
    }


def _build_microstructure(bid_ask_data: Dict[str, Any], tick_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    从 bid_ask_depth 和 intraday_tick skill 输出提取微观结构特征。
    返回字段:
      bid_ratio       委比（-100~100）
      spread_pct      买卖价差百分比
      large_order_net 大单（>50万）净买入额（万元）
      tick_buy_ratio  主动买入成交占比（0~1）
      available       是否有有效数据
    """
    ms: Dict[str, Any] = {
        "bid_ratio": 0.0,
        "spread_pct": 0.0,
        "large_order_net": 0.0,
        "tick_buy_ratio": 0.5,
        "available": False,
    }

    # bid_ask_depth skill 输出
    if bid_ask_data:
        ba_summary = bid_ask_data.get("summary", bid_ask_data)
        bid_ratio = ba_summary.get("bid_ratio", None)
        spread_pct = ba_summary.get("spread_pct", None)
        if bid_ratio is not None:
            ms["bid_ratio"] = float(bid_ratio)
            ms["available"] = True
        if spread_pct is not None:
            ms["spread_pct"] = float(spread_pct)

    # intraday_tick skill 输出
    if tick_data:
        items = tick_data.get("items", tick_data.get("data", []))
        if items:
            buy_amt = 0.0
            sell_amt = 0.0
            large_buy = 0.0
            large_sell = 0.0
            threshold = 500000  # 50万以上算大单
            for item in items:
                amt = float(item.get("成交额", item.get("amount", 0)) or 0)
                direction = str(item.get("方向", item.get("direction", "")) or "")
                if "买" in direction or direction.upper() in ("BUY", "B"):
                    buy_amt += amt
                    if amt >= threshold:
                        large_buy += amt
                elif "卖" in direction or direction.upper() in ("SELL", "S"):
                    sell_amt += amt
                    if amt >= threshold:
                        large_sell += amt
            total = buy_amt + sell_amt
            if total > 0:
                ms["tick_buy_ratio"] = round(buy_amt / total, 4)
                ms["available"] = True
            ms["large_order_net"] = round((large_buy - large_sell) / 10000, 2)  # 元→万元

    return ms


def main(params: Dict[str, Any]) -> Dict[str, Any]:
    ts_code = params.get("ts_code", "")
    horizon = params.get("horizon", "1w")

    if not ts_code:
        return {"error": "缺少 ts_code", "for_llm": {"error": "缺少 ts_code"}}

    ensemble = _load_ensemble()
    if not ensemble:
        return {
            "error": "ML models failed to load",
            "for_llm": {"error": "ML 模型加载失败，无法预测"}
        }

    try:
        # 技术指标（来自 technical_indicators skill）
        tech = params.get("technical_indicators", {})

        # 资金数据（来自 money_flow skill，真实主力/北向/融资数据）
        money_raw = params.get("money_flow", {})
        money = _build_money_flow_from_skill(money_raw)

        # 基本面（来自 financial_report skill）+ valuation_analysis 作为补充
        financial_data = params.get("financial_data", {})
        valuation_base = params.get("valuation_analysis", {})
        valuation = _build_valuation_from_financial(financial_data, valuation_base)

        # K线数据（来自 historical_data skill，供 LSTM 使用）
        kline_data = params.get("kline_data", [])

        # 微观结构数据（来自 bid_ask_depth + intraday_tick skill）
        bid_ask_raw = params.get("bid_ask", {})
        tick_raw = params.get("tick_data", {})
        microstructure = _build_microstructure(bid_ask_raw, tick_raw)

        # LLM 可调参数
        sentiment_score = params.get("sentiment_score")  # None = 未传入，使用默认 0.5
        market_bias = params.get("market_bias", "neutral")
        override_weights = params.get("model_weights")
        label_threshold = params.get("label_threshold", 0.03)

        # 统计数据质量
        data_sources = []
        if tech:
            data_sources.append("technical_indicators")
        if money_raw:
            data_sources.append("money_flow")
        if financial_data:
            data_sources.append("financial_report")
        if sentiment_score is not None:
            data_sources.append("sentiment_analysis")
        if kline_data:
            data_sources.append("kline_data")
        if microstructure.get("available"):
            data_sources.append("microstructure")

        result = ensemble.predict(
            tech=tech,
            money=money,
            valuation=valuation,
            kline_data=kline_data,
            sentiment_score=sentiment_score if sentiment_score is not None else 0.5,
            horizon=horizon,
            market_bias=market_bias,
            override_weights=override_weights,
            microstructure=microstructure,
        )

        # 最终情感分数（优先使用传入的真实值）
        final_sentiment_score = sentiment_score if sentiment_score is not None else (
            0.5 + result.probability * 0.4 if result.direction == "UP" else
            0.5 - result.probability * 0.4 if result.direction == "DOWN" else 0.5
        )

        data_quality = "real_data" if len(data_sources) >= 3 else (
            "partial_real" if len(data_sources) >= 1 else "estimated"
        )

        data = {
            "ts_code": ts_code,
            "horizon": horizon,
            "direction": result.direction,
            "magnitude": result.magnitude,
            "probability": result.probability,
            "confidence": result.confidence,
            "sentiment_score": round(final_sentiment_score, 3),
            "key_factors": result.key_factors,
            "model_predictions": result.model_predictions,
            "model_weights": result.model_weights,
            "risks": _generate_risks(result.direction, result.confidence),
            "method": result.method,
            "data_quality": data_quality,
            "data_sources_used": data_sources,
            "market_bias_applied": market_bias,
        }

        return {
            "for_llm": {
                "ts_code": ts_code,
                "horizon": horizon,
                "direction": result.direction,
                "probability": result.probability,
                "confidence": result.confidence,
                "data_quality": data_quality,
                "data_sources": data_sources,
                "market_bias": market_bias,
                "risks": data["risks"],
                "tip": "数据质量: " + ("真实数据驱动" if data_quality == "real_data" else "部分真实数据" if data_quality == "partial_real" else "估算数据（建议先调用 technical_indicators/money_flow/financial_report）"),
            },
            **data,
        }

    except Exception as e:
        logger.error(f"[Prediction] ensemble predict failed: {e}", exc_info=True)
        return {"error": str(e), "for_llm": {"error": f"预测失败: {e}"}}


if __name__ == "__main__":
    import argparse

    p = {}
    if not sys.stdin.isatty():
        try:
            raw = sys.stdin.read().strip()
            if raw:
                p = json.loads(raw)
        except Exception:
            pass

    parser = argparse.ArgumentParser()
    parser.add_argument("--ts_code", default="")
    parser.add_argument("--horizon", default="1w")
    args = parser.parse_args()
    if args.ts_code:
        p["ts_code"] = args.ts_code
    if args.horizon:
        p["horizon"] = args.horizon

    result = main(p)
    print(json.dumps(result, ensure_ascii=False, default=str))
