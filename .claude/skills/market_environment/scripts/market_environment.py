"""
Market Environment Skill
市场环境分析技能 - 从量能、两融、北向、情绪多维度评估市场环境
"""
import os
import logging
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


def _safe_float(val, default=0.0) -> float:
    if val is None or val == '' or val == '-':
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def _get_market_volume(days: int = 20) -> Dict[str, Any]:
    """获取市场总量能数据"""
    try:
        import akshare as ak
        import pandas as pd

        end_date = datetime.now().strftime("%Y%m%d")
        start_date = (datetime.now() - timedelta(days=days * 2)).strftime("%Y%m%d")

        df = ak.stock_zh_index_daily(symbol="sh000001")
        if df is None or df.empty:
            return {}

        df['date'] = pd.to_datetime(df['date']).dt.strftime('%Y%m%d')
        df = df.sort_values('date').tail(days + 5)

        # 成交额列
        amount_col = None
        for col in ['amount', '成交额', 'volume']:
            if col in df.columns:
                amount_col = col
                break

        if amount_col is None:
            return {}

        df[amount_col] = pd.to_numeric(df[amount_col], errors='coerce')
        today_vol = _safe_float(df[amount_col].iloc[-1])
        avg_5d = _safe_float(df[amount_col].tail(5).mean())
        avg_20d = _safe_float(df[amount_col].mean())

        # 转换为亿（原始单位可能是元或万）
        unit = 1e8  # 默认假设单位是元
        if today_vol > 1e12:
            unit = 1e8
        elif today_vol > 1e8:
            unit = 1e4
        else:
            unit = 1

        return {
            "today_vol_yi": round(today_vol / unit, 2),
            "avg_5d_vol_yi": round(avg_5d / unit, 2),
            "avg_20d_vol_yi": round(avg_20d / unit, 2),
            "vol_ratio": round(today_vol / avg_5d, 2) if avg_5d > 0 else 1.0,
            "vol_ratio_20d": round(today_vol / avg_20d, 2) if avg_20d > 0 else 1.0,
        }
    except Exception as e:
        logger.warning(f"获取市场量能失败: {e}")
        return {}


def _get_margin_balance() -> Dict[str, Any]:
    """获取两融余额"""
    try:
        import akshare as ak
        df = ak.stock_margin_ratio_pa()
        if df is None or df.empty:
            return {}
        df = df.sort_values(df.columns[0]).tail(10)
        latest = df.iloc[-1]
        prev_5 = df.iloc[-6] if len(df) >= 6 else df.iloc[0]

        balance = _safe_float(latest.get('融资融券余额', latest.iloc[-1] if len(latest) > 1 else 0))
        balance_5d_ago = _safe_float(prev_5.get('融资融券余额', prev_5.iloc[-1] if len(prev_5) > 1 else 0))

        unit = 1e8  # 转亿
        return {
            "balance_yi": round(balance / unit, 2),
            "balance_5d_ago_yi": round(balance_5d_ago / unit, 2),
            "change_5d_yi": round((balance - balance_5d_ago) / unit, 2),
            "change_5d_pct": round((balance - balance_5d_ago) / balance_5d_ago * 100, 2) if balance_5d_ago > 0 else 0
        }
    except Exception as e:
        logger.warning(f"获取两融余额失败: {e}")
        return {}


def _get_north_flow(days: int = 10) -> Dict[str, Any]:
    """获取北向资金近期流向"""
    try:
        import akshare as ak
        import pandas as pd

        df = ak.stock_hsgt_north_net_flow_in_em(symbol="北向资金")
        if df is None or df.empty:
            return {}

        df = df.tail(days)
        net_flows = []
        date_col = df.columns[0]
        flow_col = df.columns[1] if len(df.columns) > 1 else df.columns[0]

        for _, row in df.iterrows():
            net_flows.append(_safe_float(row[flow_col]))

        total_5d = sum(net_flows[-5:]) if len(net_flows) >= 5 else sum(net_flows)
        total_10d = sum(net_flows)

        unit = 1e8
        return {
            "net_5d_yi": round(total_5d / unit, 2),
            "net_10d_yi": round(total_10d / unit, 2),
            "direction": "流入" if total_5d > 0 else "流出",
            "daily": [round(f / unit, 2) for f in net_flows[-5:]]
        }
    except Exception as e:
        logger.warning(f"获取北向资金失败: {e}")
        return {}


def _calc_environment_score(volume: Dict, margin: Dict, north_flow: Dict) -> Dict[str, Any]:
    """计算市场环境综合评分"""
    score = 50
    signals = []

    # 量能分析
    vol_ratio = volume.get("vol_ratio", 1.0)
    if vol_ratio > 1.3:
        score += 10
        signals.append("量能明显放大")
    elif vol_ratio > 1.1:
        score += 5
        signals.append("量能温和放大")
    elif vol_ratio < 0.7:
        score -= 10
        signals.append("量能明显萎缩")
    elif vol_ratio < 0.9:
        score -= 5
        signals.append("量能温和萎缩")

    # 两融分析
    margin_change = margin.get("change_5d_yi", 0)
    if margin_change > 100:
        score += 10
        signals.append(f"两融余额5日增加{margin_change:.0f}亿")
    elif margin_change > 0:
        score += 5
        signals.append("两融余额小幅增加")
    elif margin_change < -100:
        score -= 10
        signals.append(f"两融余额5日减少{abs(margin_change):.0f}亿")
    elif margin_change < 0:
        score -= 5
        signals.append("两融余额小幅减少")

    # 北向资金分析
    north_5d = north_flow.get("net_5d_yi", 0)
    if north_5d > 50:
        score += 15
        signals.append(f"北向5日净流入{north_5d:.1f}亿")
    elif north_5d > 10:
        score += 8
        signals.append(f"北向5日净流入{north_5d:.1f}亿")
    elif north_5d < -50:
        score -= 15
        signals.append(f"北向5日净流出{abs(north_5d):.1f}亿")
    elif north_5d < -10:
        score -= 8
        signals.append(f"北向5日净流出{abs(north_5d):.1f}亿")

    # 评级
    score = max(0, min(100, score))
    if score >= 75:
        level = "强势"
    elif score >= 60:
        level = "中性偏多"
    elif score >= 40:
        level = "中性"
    elif score >= 25:
        level = "中性偏空"
    else:
        level = "弱势"

    return {
        "score": score,
        "level": level,
        "signals": signals
    }


def main(params: Dict[str, Any]) -> Dict[str, Any]:
    """
    分析市场整体环境

    params:
        days (int): 分析近 N 日数据，默认 20
    """
    days = int(params.get("days", 20))
    date = datetime.now().strftime("%Y%m%d")

    volume = _get_market_volume(days)
    margin = _get_margin_balance()
    north_flow = _get_north_flow(min(days, 10))

    env = _calc_environment_score(volume, margin, north_flow)

    return {
        "date": date,
        "volume": volume,
        "margin": margin,
        "north_flow": north_flow,
        "environment_score": env["score"],
        "environment_level": env["level"],
        "key_signals": env["signals"],
        "for_llm": {
            "date": date,
            "environment_level": env["level"],
            "score": env["score"],
            "vol_ratio": volume.get("vol_ratio"),
            "today_vol_yi": volume.get("today_vol_yi"),
            "margin_change_5d_yi": margin.get("change_5d_yi"),
            "north_net_5d_yi": north_flow.get("net_5d_yi"),
            "north_direction": north_flow.get("direction"),
            "key_signals": env["signals"]
        }
    }


if __name__ == "__main__":
    import sys
    import json
    import argparse

    if len(sys.argv) > 1:
        parser = argparse.ArgumentParser(description="市场环境分析")
        parser.add_argument("--days", type=int, default=20, help="分析近 N 日")
        args = parser.parse_args()
        result = main({"days": args.days})
    else:
        data = json.loads(sys.stdin.read())
        result = main(data)

    print(json.dumps(result, ensure_ascii=False))
