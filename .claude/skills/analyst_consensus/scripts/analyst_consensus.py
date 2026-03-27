"""
Analyst Consensus Skill
分析师评级共识技能
获取分析师评级历史和综合评分，分析机构观点共识
"""
import os
import math
import logging
from typing import Dict, Any, List

logger = logging.getLogger(__name__)


def _safe_float(val) -> float:
    if val is None or val == "-" or val == "":
        return 0.0
    try:
        result = float(val)
        if math.isnan(result) or math.isinf(result):
            return 0.0
        return result
    except (ValueError, TypeError):
        return 0.0


def _get_analyst_ratings(code: str) -> List[Dict]:
    """获取分析师综合评级历史"""
    try:
        import akshare as ak
        df = ak.stock_comment_detail_zhpj_lspf_em(symbol=code)
        if df is not None and not df.empty:
            records = []
            for _, row in df.iterrows():
                record = {}
                for col in df.columns:
                    val = row[col]
                    if val is None:
                        record[col] = ""
                    else:
                        try:
                            fval = float(val)
                            record[col] = fval if not (math.isnan(fval) or math.isinf(fval)) else ""
                        except (ValueError, TypeError):
                            record[col] = str(val)
                records.append(record)
            logger.info(f"get_analyst_ratings succeeded: {code}, rows={len(records)}")
            return records
    except Exception as e:
        logger.warning(f"get_analyst_ratings failed: {code}: {e}")
    return []


def _get_analyst_rank(code: str) -> List[Dict]:
    """获取分析师评级明细"""
    try:
        import akshare as ak
        df = ak.stock_analyst_rank_em(symbol=code)
        if df is not None and not df.empty:
            records = []
            for _, row in df.iterrows():
                record = {}
                for col in df.columns:
                    val = row[col]
                    if val is None:
                        record[col] = ""
                    else:
                        try:
                            fval = float(val)
                            record[col] = fval if not (math.isnan(fval) or math.isinf(fval)) else ""
                        except (ValueError, TypeError):
                            record[col] = str(val)
                records.append(record)
            logger.info(f"get_analyst_rank succeeded: {code}, rows={len(records)}")
            return records
    except Exception as e:
        logger.warning(f"get_analyst_rank failed: {code}: {e}")
    return []


def _extract_rating_label(record: Dict) -> str:
    """从一条评级记录中提取评级标签"""
    positive = ["买入", "强烈推荐", "推荐", "增持", "强买"]
    neutral = ["中性", "持有", "观望", "谨慎推荐"]
    negative = ["卖出", "减持", "回避", "卖出评级"]
    for col, val in record.items():
        v = str(val)
        if any(k in v for k in positive):
            return "买入"
        if any(k in v for k in negative):
            return "卖出"
        if any(k in v for k in neutral):
            return "中性"
    return ""


def _extract_target_price(record: Dict) -> float:
    """从一条评级记录中提取目标价"""
    for col, val in record.items():
        if "目标" in str(col) and "价" in str(col):
            p = _safe_float(val)
            if p > 0:
                return p
    return 0.0


def _extract_date(record: Dict) -> str:
    """从一条评级记录中提取日期字符串"""
    for col, val in record.items():
        if "日期" in str(col) or "时间" in str(col):
            return str(val)[:10]
    return ""


def _analyze_rating_trend(ratings: List[Dict]) -> Dict[str, Any]:
    """
    分析评级趋势和目标价变化：
    - 评级连续变化（升/降级）
    - 目标价近30天变化幅度
    - 7天内集群效应（≥3条为集群信号）
    """
    result = {
        "rating_trend": "稳定",
        "rating_change_detail": "",
        "target_price_latest": 0.0,
        "target_price_change_pct": 0.0,
        "cluster_signal": False,
        "cluster_count": 0,
    }
    if not ratings:
        return result

    # 提取最近20条的评级标签和日期
    labeled = []
    for r in ratings[:20]:
        label = _extract_rating_label(r)
        date = _extract_date(r)
        tp = _extract_target_price(r)
        if label:
            labeled.append({"label": label, "date": date, "tp": tp})

    if not labeled:
        return result

    # 目标价变化（最新3条 vs 10-20条）
    recent_tps = [x["tp"] for x in labeled[:3] if x["tp"] > 0]
    older_tps = [x["tp"] for x in labeled[5:15] if x["tp"] > 0]
    if recent_tps and older_tps:
        avg_recent = sum(recent_tps) / len(recent_tps)
        avg_older = sum(older_tps) / len(older_tps)
        result["target_price_latest"] = round(avg_recent, 2)
        result["target_price_change_pct"] = round((avg_recent - avg_older) / max(avg_older, 0.01) * 100, 1)
    elif recent_tps:
        result["target_price_latest"] = round(recent_tps[0], 2)

    # 评级趋势：对比最近3条 vs 前3条
    rank_map = {"买入": 2, "中性": 1, "卖出": 0}
    recent_labels = [x["label"] for x in labeled[:3] if x["label"]]
    older_labels = [x["label"] for x in labeled[3:6] if x["label"]]
    if recent_labels and older_labels:
        recent_score = sum(rank_map.get(l, 1) for l in recent_labels) / len(recent_labels)
        older_score = sum(rank_map.get(l, 1) for l in older_labels) / len(older_labels)
        if recent_score < older_score - 0.3:
            result["rating_trend"] = "下调"
            result["rating_change_detail"] = f"评级从{older_labels[0]}连续下调至{recent_labels[-1]}"
        elif recent_score > older_score + 0.3:
            result["rating_trend"] = "上调"
            result["rating_change_detail"] = f"评级从{older_labels[0]}连续上调至{recent_labels[-1]}"
        else:
            result["rating_trend"] = "稳定"
            result["rating_change_detail"] = f"近期评级维持在{recent_labels[0]}水平"

    # 集群效应：7天内研报数量
    from datetime import datetime, timedelta
    cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    cluster_count = sum(1 for x in labeled if x.get("date", "") >= cutoff)
    result["cluster_signal"] = cluster_count >= 3
    result["cluster_count"] = cluster_count

    return result


def _analyze_consensus(ratings: List[Dict], rank: List[Dict]) -> Dict[str, Any]:
    """分析分析师共识"""
    summary = {
        "rating_records": len(ratings),
        "analyst_count": len(rank),
    }
    signals = []

    # Try to extract latest composite score from rating history
    if ratings:
        latest = ratings[0]
        for col, val in latest.items():
            if "综合" in str(col) and "评分" in str(col):
                score = _safe_float(val)
                if score > 0:
                    summary["latest_composite_score"] = score
                    if score >= 4.0:
                        signals.append(f"分析师综合评分{score:.2f}，机构强烈看好")
                    elif score >= 3.0:
                        signals.append(f"分析师综合评分{score:.2f}，机构总体看好")
                    elif score >= 2.0:
                        signals.append(f"分析师综合评分{score:.2f}，机构观点中性")
                    else:
                        signals.append(f"分析师综合评分{score:.2f}，机构相对谨慎")
                break

        # Count buy/hold/sell ratings
        buy_count = 0
        hold_count = 0
        sell_count = 0
        for r in ratings[:20]:
            for col, val in r.items():
                if "买入" in str(val):
                    buy_count += 1
                elif "增持" in str(val):
                    buy_count += 1
                elif "中性" in str(val) or "持有" in str(val):
                    hold_count += 1
                elif "卖出" in str(val) or "减持" in str(val):
                    sell_count += 1

        if buy_count + hold_count + sell_count > 0:
            summary["buy_ratings"] = buy_count
            summary["hold_ratings"] = hold_count
            summary["sell_ratings"] = sell_count
            total = buy_count + hold_count + sell_count
            buy_ratio = buy_count / total * 100
            if buy_ratio >= 70:
                signals.append(f"买入/增持评级占比{buy_ratio:.0f}%，看多共识强")
            elif buy_ratio >= 50:
                signals.append(f"买入/增持评级占比{buy_ratio:.0f}%，偏多")
            else:
                signals.append("中性或谨慎评级为主")

    if not signals:
        signals.append("暂无足够的分析师评级数据")

    summary["signals"] = signals
    return summary


def main(params: Dict[str, Any]) -> Dict[str, Any]:
    ts_code = params.get("ts_code", "")

    if not ts_code:
        return {"error": "缺少股票代码参数 ts_code", "for_llm": {"error": "缺少股票代码参数 ts_code"}}

    code = ts_code.split('.')[0] if '.' in ts_code else ts_code

    try:
        ratings = _get_analyst_ratings(code)
        rank = _get_analyst_rank(code)

        if not ratings and not rank:
            err = "无法获取分析师评级数据"
            return {"error": err, "for_llm": {"error": err}}

        analysis = _analyze_consensus(ratings, rank)
        trend = _analyze_rating_trend(ratings)
        signals = analysis.get("signals", [])

        # 补充趋势信号
        if trend["rating_trend"] == "下调":
            signals.append(f"评级趋势下调：{trend['rating_change_detail']}")
        elif trend["rating_trend"] == "上调":
            signals.append(f"评级趋势上调：{trend['rating_change_detail']}")
        if trend["target_price_change_pct"] != 0:
            direction = "上调" if trend["target_price_change_pct"] > 0 else "下调"
            signals.append(f"目标价{direction}{abs(trend['target_price_change_pct']):.1f}%至{trend['target_price_latest']:.2f}元")
        if trend["cluster_signal"]:
            signals.append(f"7天内{trend['cluster_count']}家机构集中发布研报，关注度集群效应")

        signal_text = "；".join(signals) if signals else "暂无分析"

        items = ratings[:10] if ratings else rank[:10]
        columns = [{"key": k, "label": k} for k in (items[0].keys() if items else [])]

        result = {
            "ts_code": ts_code,
            "title": f"分析师评级共识 - {ts_code}",
            "items": items,
            "columns": columns,
            "analyst_ratings": ratings[:20],
            "analyst_rank": rank[:10],
            "summary": analysis,
            "rating_trend": trend,
            "analysis": signal_text,
            "data_source": "akshare/stock_comment_em",
        }
        result["for_llm"] = {
            "ts_code": ts_code,
            "rating_records": analysis.get("rating_records", 0),
            "analyst_count": analysis.get("analyst_count", 0),
            "latest_composite_score": analysis.get("latest_composite_score", 0),
            "buy_ratings": analysis.get("buy_ratings", 0),
            "hold_ratings": analysis.get("hold_ratings", 0),
            "sell_ratings": analysis.get("sell_ratings", 0),
            "rating_trend": trend.get("rating_trend", "稳定"),
            "rating_change_detail": trend.get("rating_change_detail", ""),
            "target_price_latest": trend.get("target_price_latest", 0),
            "target_price_change_pct": trend.get("target_price_change_pct", 0),
            "cluster_signal": trend.get("cluster_signal", False),
            "cluster_count": trend.get("cluster_count", 0),
            "signals": signals,
            "analysis": signal_text,
        }
        return result

    except Exception as e:
        logger.error(f"分析师评级共识分析失败: {e}", exc_info=True)
        err = f"分析师评级共识分析失败: {str(e)}"
        return {"error": err, "for_llm": {"error": err}}


if __name__ == "__main__":
    import sys, json as _json
    if len(sys.argv) > 1:
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("--ts_code", default="")
        args = parser.parse_args()
        params = {k: v for k, v in vars(args).items() if v}
    else:
        params = _json.loads(sys.stdin.read())
    result = main(params)
    print(_json.dumps(result, ensure_ascii=False, default=str))
