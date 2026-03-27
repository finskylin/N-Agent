"""
Insider Trading Skill
股东/高管增减持分析技能
追踪内部人交易动态，分析增减持趋势
"""
import os
import math
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)


def _safe_float(val) -> float:
    if val is None or val == '-' or val == '':
        return 0.0
    try:
        result = float(val)
        if math.isnan(result) or math.isinf(result):
            return 0.0
        return result
    except (ValueError, TypeError):
        return 0.0


def _get_insider_trades(code: str) -> List[Dict]:
    try:
        import akshare as ak
        df = ak.stock_inner_trade_xq(symbol=code)
        if df is None or df.empty:
            return []
        trades = []
        for _, row in df.iterrows():
            change_str = str(row.get('变动方向', '') or row.get('交易类型', ''))
            direction = "增持" if ('增' in change_str or '买' in change_str) else (
                "减持" if ('减' in change_str or '卖' in change_str) else change_str or "未知")
            shares = _safe_float(row.get('变动股数', 0) or row.get('成交量', 0))
            price = _safe_float(row.get('变动均价', 0) or row.get('成交均价', 0))
            amount = _safe_float(row.get('变动金额', 0))
            if amount == 0 and shares != 0 and price != 0:
                amount = abs(shares * price)
            trades.append({
                "date": str(row.get('变动日期', '') or row.get('交易日期', '')),
                "name": str(row.get('变动人', '') or row.get('高管姓名', '') or row.get('股东名称', '')),
                "position": str(row.get('职务', '') or row.get('关系', '')),
                "direction": direction,
                "shares": abs(shares),
                "price": price,
                "amount": round(abs(amount) / 10000, 2),
                "reason": str(row.get('变动原因', '') or ''),
                "source": "insider_trade"
            })
        return trades
    except Exception as e:
        logger.warning(f"get_insider_trades failed: {code}: {e}")
        return []


def _get_shareholder_changes(code: str) -> List[Dict]:
    try:
        import akshare as ak
        df = ak.stock_gpjj_em(symbol=code)
        if df is None or df.empty:
            return []
        trades = []
        for _, row in df.iterrows():
            change_str = str(row.get('增减', '') or row.get('变动方向', ''))
            direction = "增持" if '增' in change_str else ("减持" if '减' in change_str else change_str or "未知")
            shares = _safe_float(row.get('变动股数', 0))
            price = _safe_float(row.get('变动均价', 0))
            amount = _safe_float(row.get('变动市值', 0))
            if amount == 0 and shares != 0 and price != 0:
                amount = abs(shares * price)
            trades.append({
                "date": str(row.get('公告日期', '') or row.get('变动截止日期', '')),
                "name": str(row.get('股东名称', '') or row.get('变动人', '')),
                "position": "股东",
                "direction": direction,
                "shares": abs(shares),
                "price": price,
                "amount": round(abs(amount) / 10000, 2),
                "reason": str(row.get('变动原因', '') or ''),
                "source": "shareholder_change"
            })
        return trades
    except Exception as e:
        logger.warning(f"get_shareholder_changes failed: {code}: {e}")
        return []


def _merge_trades(all_trades: List[Dict], months: int) -> List[Dict]:
    cutoff_date = (datetime.now() - timedelta(days=months * 30)).strftime('%Y-%m-%d')
    filtered = [t for t in all_trades if t.get('date', '') >= cutoff_date]
    if not filtered and all_trades:
        filtered = all_trades
    filtered.sort(key=lambda x: x.get('date', ''), reverse=True)
    return filtered


def _calculate_summary(trades: List[Dict]) -> Dict[str, Any]:
    if not trades:
        return {"total_trades": 0, "buy_count": 0, "sell_count": 0,
                "net_buy_amount": 0.0, "trend": "无数据", "unique_insiders": 0}
    buy_trades = [t for t in trades if t.get('direction') == '增持']
    sell_trades = [t for t in trades if t.get('direction') == '减持']
    total_buy_amount = sum(t.get('amount', 0) for t in buy_trades)
    total_sell_amount = sum(t.get('amount', 0) for t in sell_trades)
    net_amount = total_buy_amount - total_sell_amount
    if net_amount > 100:
        trend = "净增持"
    elif net_amount < -100:
        trend = "净减持"
    elif len(buy_trades) > len(sell_trades) * 2:
        trend = "偏增持"
    elif len(sell_trades) > len(buy_trades) * 2:
        trend = "偏减持"
    else:
        trend = "增减持平衡"
    unique_names = set(t.get('name', '') for t in trades if t.get('name'))
    return {
        "total_trades": len(trades),
        "buy_count": len(buy_trades),
        "sell_count": len(sell_trades),
        "net_buy_amount": round(net_amount, 2),
        "total_buy_amount": round(total_buy_amount, 2),
        "total_sell_amount": round(total_sell_amount, 2),
        "trend": trend,
        "latest_date": trades[0].get('date', '') if trades else '',
        "unique_insiders": len(unique_names),
    }


def _analyze_insider_behavior(trades: List[Dict], summary: Dict) -> Dict[str, Any]:
    if not trades:
        return {"sentiment": "中性", "sentiment_score": 50, "signals": [],
                "recommendation": "无内部人交易数据"}
    signals = []
    score = 50
    net_amount = summary.get('net_buy_amount', 0)
    buy_count = summary.get('buy_count', 0)
    sell_count = summary.get('sell_count', 0)
    if net_amount > 1000:
        score += 20
        signals.append({"type": "positive", "message": f"净增持金额达{net_amount:.0f}万元，内部人看好"})
    elif net_amount > 100:
        score += 10
        signals.append({"type": "positive", "message": f"净增持{net_amount:.0f}万元"})
    elif net_amount < -1000:
        score -= 20
        signals.append({"type": "negative", "message": f"净减持金额达{abs(net_amount):.0f}万元，需要关注"})
    elif net_amount < -100:
        score -= 10
        signals.append({"type": "negative", "message": f"净减持{abs(net_amount):.0f}万元"})
    if buy_count >= 5 and sell_count == 0:
        score += 15
        signals.append({"type": "positive", "message": f"连续{buy_count}笔增持，无减持，看多信号强烈"})
    elif sell_count >= 5 and buy_count == 0:
        score -= 15
        signals.append({"type": "negative", "message": f"连续{sell_count}笔减持，无增持，看空信号明显"})
    if score >= 70:
        sentiment, recommendation = "积极", "内部人持续增持，看多信号明确"
    elif score >= 55:
        sentiment, recommendation = "偏多", "内部人增持为主，信号偏正面"
    elif score >= 45:
        sentiment, recommendation = "中性", "内部人增减持平衡，观望为主"
    elif score >= 30:
        sentiment, recommendation = "偏空", "内部人减持为主，谨慎操作"
    else:
        sentiment, recommendation = "谨慎", "内部人大幅减持，注意风险"
    return {"sentiment": sentiment, "sentiment_score": score,
            "signals": signals, "recommendation": recommendation}


def main(params: Dict[str, Any]) -> Dict[str, Any]:
    ts_code = params.get("ts_code", "")
    months = int(params.get("months", 6))

    if not ts_code:
        return {"error": "缺少股票代码参数 ts_code", "for_llm": {"error": "缺少股票代码参数 ts_code"}}

    code = ts_code.split('.')[0] if '.' in ts_code else ts_code

    try:
        insider_trades = _get_insider_trades(code)
        shareholder_changes = _get_shareholder_changes(code)
        all_trades = _merge_trades(insider_trades + shareholder_changes, months)
        summary = _calculate_summary(all_trades)
        analysis = _analyze_insider_behavior(all_trades, summary)

        result = {
            "ts_code": ts_code,
            "title": f"股东/高管增减持分析 (近{months}月)",
            "summary": summary,
            "items": all_trades[:10],
            "columns": [
                {"key": "date", "label": "日期"},
                {"key": "name", "label": "变动人"},
                {"key": "direction", "label": "方向"},
                {"key": "shares", "label": "变动股数"},
                {"key": "amount", "label": "变动金额(万)"},
                {"key": "price", "label": "变动均价"},
                {"key": "reason", "label": "变动原因"},
            ],
            "trades": all_trades,
            "analysis": analysis,
        }
        result["for_llm"] = {
            "ts_code": ts_code,
            "total_trades": summary.get("total_trades", 0),
            "buy_count": summary.get("buy_count", 0),
            "sell_count": summary.get("sell_count", 0),
            "net_buy_amount": summary.get("net_buy_amount", 0),
            "trend": summary.get("trend", ""),
            "sentiment": analysis.get("sentiment", ""),
            "recommendation": analysis.get("recommendation", ""),
        }
        return result

    except Exception as e:
        logger.error(f"内部人交易分析失败: {e}", exc_info=True)
        err = f"内部人交易分析失败: {str(e)}"
        return {"error": err, "for_llm": {"error": err}}


if __name__ == "__main__":
    import sys, json as _json
    if len(sys.argv) > 1:
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("--ts_code", default="")
        parser.add_argument("--months", default="6")
        args = parser.parse_args()
        params = {k: v for k, v in vars(args).items() if v}
    else:
        params = _json.loads(sys.stdin.read())
    result = main(params)
    print(_json.dumps(result, ensure_ascii=False, default=str))
