"""
Rating Skill
综合评级技能 — 7维度
从基本面、技术面、估值面、资金面、情绪面、事件面、创新面计算股票综合评分
数据来源: AKShare (财务/行情/资金流向数据)
评分方法: 规则引擎评分（配置化，权重从环境变量读取）
"""
import os
import math
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger(__name__)


def _safe_float(val, default=0.0) -> float:
    if val is None or val == "-" or val == "":
        return default
    try:
        result = float(val)
        if math.isnan(result) or math.isinf(result):
            return default
        return result
    except (ValueError, TypeError):
        return default


def _load_dimension_weights() -> Dict[str, float]:
    """从环境变量或使用默认权重"""
    return {
        "fundamental": float(os.environ.get("RATING_W_FUNDAMENTAL", "0.25")),
        "technical": float(os.environ.get("RATING_W_TECHNICAL", "0.15")),
        "valuation": float(os.environ.get("RATING_W_VALUATION", "0.15")),
        "capital": float(os.environ.get("RATING_W_CAPITAL", "0.15")),
        "sentiment": float(os.environ.get("RATING_W_SENTIMENT", "0.10")),
        "events": float(os.environ.get("RATING_W_EVENTS", "0.10")),
        "innovation": float(os.environ.get("RATING_W_INNOVATION", "0.10")),
    }


def _get_financial_data(code: str) -> Dict[str, Any]:
    """获取财务指标数据"""
    try:
        import akshare as ak
        df = ak.stock_financial_analysis_indicator(symbol=code, start_year="2020")
        if df is not None and not df.empty:
            row = df.iloc[-1] if len(df) > 0 else None
            if row is not None:
                return {
                    "roe": _safe_float(row.get("净资产收益率", row.get("ROE", 0))),
                    "roa": _safe_float(row.get("总资产净利率", row.get("ROA", 0))),
                    "gross_margin": _safe_float(row.get("销售毛利率", 0)),
                    "net_margin": _safe_float(row.get("销售净利率", 0)),
                }
    except Exception as e:
        logger.warning(f"get_financial_data failed: {code}: {e}")
    return {}


def _get_spot_data(code: str) -> Dict[str, Any]:
    """获取当前行情数据（PE/PB/价格）"""
    try:
        import akshare as ak
        df = ak.stock_zh_a_spot_em()
        if df is not None and not df.empty:
            row = df[df['代码'] == code]
            if not row.empty:
                r = row.iloc[0]
                return {
                    "price": _safe_float(r.get("最新价", 0)),
                    "pe": _safe_float(r.get("市盈率-动态", 0)),
                    "pb": _safe_float(r.get("市净率", 0)),
                    "change_pct": _safe_float(r.get("涨跌幅", 0)),
                    "volume": _safe_float(r.get("成交量", 0)),
                    "turnover": _safe_float(r.get("换手率", 0)),
                }
    except Exception as e:
        logger.warning(f"get_spot_data failed: {code}: {e}")
    return {}


def _get_ma_data(code: str) -> Dict[str, Any]:
    """获取均线数据（技术面）"""
    end_date = datetime.now().strftime('%Y%m%d')
    start_date = (datetime.now() - timedelta(days=120)).strftime('%Y%m%d')
    try:
        import akshare as ak
        df = ak.stock_zh_a_hist(symbol=code, period="daily", start_date=start_date, end_date=end_date, adjust="qfq")
        if df is not None and not df.empty:
            closes = df["收盘"].tolist()
            if len(closes) >= 20:
                ma5 = sum(closes[-5:]) / 5
                ma20 = sum(closes[-20:]) / 20
                ma60 = sum(closes[-60:]) / 60 if len(closes) >= 60 else None
                prev_ma5 = sum(closes[-6:-1]) / 5 if len(closes) >= 6 else ma5
                prev_ma20 = sum(closes[-21:-1]) / 20 if len(closes) >= 21 else ma20
                # Volatility (20-day)
                recent = closes[-20:]
                mean_price = sum(recent) / len(recent)
                variance = sum((p - mean_price) ** 2 for p in recent) / len(recent)
                volatility = (variance ** 0.5) / mean_price * 100
                return {
                    "ma5": ma5, "ma20": ma20, "ma60": ma60,
                    "prev_ma5": prev_ma5, "prev_ma20": prev_ma20,
                    "latest_close": closes[-1],
                    "volatility_20d": round(volatility, 2),
                    "above_ma20": closes[-1] > ma20,
                    "golden_cross": prev_ma5 <= prev_ma20 and ma5 > ma20,
                    "death_cross": prev_ma5 >= prev_ma20 and ma5 < ma20,
                }
    except Exception as e:
        logger.warning(f"get_ma_data failed: {code}: {e}")
    return {}


def _get_money_flow(code: str) -> Dict[str, Any]:
    """获取主力资金流向"""
    try:
        import akshare as ak
        df = ak.stock_individual_fund_flow(stock=code, market="sh" if code.startswith("6") else "sz")
        if df is not None and not df.empty:
            recent = df.tail(5)
            main_net_5d = recent["主力净流入-净额"].sum() / 1e8 if "主力净流入-净额" in recent.columns else 0
            return {
                "main_net_5d": round(_safe_float(main_net_5d), 2),
                "records": len(df),
            }
    except Exception as e:
        logger.warning(f"get_money_flow failed: {code}: {e}")
    return {}


def _get_northbound(code: str) -> Dict[str, Any]:
    """获取北向持股数据"""
    try:
        import akshare as ak
        df = ak.stock_hsgt_individual_em(symbol=code)
        if df is not None and not df.empty:
            latest = df.iloc[-1]
            change_col = None
            for col in df.columns:
                if "变动" in str(col):
                    change_col = col
                    break
            change = _safe_float(latest.get(change_col, 0)) if change_col else 0
            return {"change": change}
    except Exception as e:
        logger.warning(f"get_northbound failed: {code}: {e}")
    return {}


def _score_fundamental(financial: Dict, spot: Dict) -> float:
    """基本面评分 (0-100)"""
    score = 50.0
    roe = financial.get("roe", 0)
    if roe > 20:
        score += 25
    elif roe > 15:
        score += 20
    elif roe > 10:
        score += 10
    elif roe < 0:
        score -= 20

    gross_margin = financial.get("gross_margin", 0)
    if gross_margin > 50:
        score += 10
    elif gross_margin > 30:
        score += 5

    net_margin = financial.get("net_margin", 0)
    if net_margin > 20:
        score += 10
    elif net_margin > 10:
        score += 5
    elif net_margin < 0:
        score -= 15

    return max(0.0, min(100.0, score))


def _score_technical(ma_data: Dict, spot: Dict) -> float:
    """技术面评分 (0-100)"""
    score = 50.0
    if ma_data.get("golden_cross"):
        score += 20
    if ma_data.get("death_cross"):
        score -= 20
    if ma_data.get("above_ma20"):
        score += 15
    else:
        score -= 10
    # Volatility penalty
    vol = ma_data.get("volatility_20d", 0)
    if vol > 5:
        score -= 5
    # Change pct momentum
    change_pct = spot.get("change_pct", 0)
    if 0 < change_pct <= 5:
        score += 5
    elif change_pct > 9.5:
        score -= 5  # Limit up, hard to buy
    elif change_pct < -5:
        score -= 10
    return max(0.0, min(100.0, score))


def _score_valuation(spot: Dict) -> float:
    """估值面评分 (0-100)"""
    score = 50.0
    pe = spot.get("pe", 0)
    if 0 < pe < 15:
        score += 25
    elif 15 <= pe < 25:
        score += 15
    elif 25 <= pe < 40:
        score += 5
    elif pe >= 60:
        score -= 20
    elif pe <= 0:
        score -= 10  # Loss-making

    pb = spot.get("pb", 0)
    if 0 < pb < 1:
        score += 10
    elif 1 <= pb < 3:
        score += 5
    elif pb > 8:
        score -= 10

    return max(0.0, min(100.0, score))


def _score_capital(money_flow: Dict, northbound: Dict) -> float:
    """资金面评分 (0-100)"""
    score = 50.0
    main_net = money_flow.get("main_net_5d", 0)
    if main_net > 5:
        score += 25
    elif main_net > 1:
        score += 15
    elif main_net > 0:
        score += 5
    elif main_net < -5:
        score -= 20
    elif main_net < -1:
        score -= 10

    nb_change = northbound.get("change", 0)
    if nb_change > 0:
        score += 5
    elif nb_change < 0:
        score -= 5

    return max(0.0, min(100.0, score))


def _score_sentiment(spot: Dict) -> float:
    """情绪面评分 (0-100) — 基于换手率和涨跌幅"""
    score = 50.0
    turnover = spot.get("turnover", 0)
    if 2 < turnover <= 8:
        score += 10  # Active but not overheated
    elif turnover > 15:
        score -= 10  # Overheated
    change_pct = spot.get("change_pct", 0)
    if 0 < change_pct <= 3:
        score += 5
    elif change_pct > 8:
        score -= 5
    elif change_pct < -5:
        score -= 10
    return max(0.0, min(100.0, score))


def _get_rating_info(total_score: float) -> Tuple[str, str, str, str]:
    """返回 (level, recommendation, action, position)"""
    if total_score >= 90:
        return "A+", "强烈推荐", "积极买入", "30%"
    elif total_score >= 80:
        return "A", "推荐", "买入", "20%"
    elif total_score >= 70:
        return "B+", "积极", "买入", "15%"
    elif total_score >= 60:
        return "B", "中性", "持有", "10%"
    elif total_score >= 50:
        return "C", "观望", "观望", "5%"
    else:
        return "D", "回避", "回避", "0%"


def main(params: Dict[str, Any]) -> Dict[str, Any]:
    ts_code = params.get("ts_code", "")

    if not ts_code:
        return {"error": "缺少股票代码参数 ts_code", "for_llm": {"error": "缺少股票代码参数 ts_code"}}

    code = ts_code.split('.')[0] if '.' in ts_code else ts_code

    try:
        weights = _load_dimension_weights()

        # Fetch data in parallel-friendly sequential calls (each with independent fallback)
        financial = _get_financial_data(code)
        spot = _get_spot_data(code)
        ma_data = _get_ma_data(code)
        money_flow = _get_money_flow(code)
        northbound = _get_northbound(code)

        # Score each dimension
        f_score = _score_fundamental(financial, spot)
        t_score = _score_technical(ma_data, spot)
        v_score = _score_valuation(spot)
        c_score = _score_capital(money_flow, northbound)
        s_score = _score_sentiment(spot)
        e_score = 60.0  # Default events score (no real-time event feed in standalone)
        i_score = 55.0  # Default innovation score

        dimensions = {
            "fundamental": f_score,
            "technical": t_score,
            "valuation": v_score,
            "capital": c_score,
            "sentiment": s_score,
            "events": e_score,
            "innovation": i_score,
        }

        total_score = sum(dimensions[k] * weights.get(k, 0.1) for k in dimensions)
        total_score = round(total_score, 1)

        level, rec, action, position = _get_rating_info(total_score)

        dimension_names_cn = {
            "fundamental": "基本面", "technical": "技术面",
            "valuation": "估值面", "capital": "资金面",
            "sentiment": "情绪面", "events": "事件面", "innovation": "创新面",
        }

        formatted_dimensions = {
            k: {
                "name": dimension_names_cn.get(k, k),
                "score": round(v, 1),
                "weight": weights.get(k, 0.1),
            }
            for k, v in dimensions.items()
        }

        key_factors = sorted(
            [
                {
                    "dimension": k,
                    "dimension_cn": dimension_names_cn.get(k, k),
                    "score": round(v, 1),
                    "weight": weights.get(k, 0.1),
                    "impact": "正面" if v >= 60 else ("中性" if v >= 45 else "负面"),
                }
                for k, v in dimensions.items()
            ],
            key=lambda x: x["score"],
            reverse=True,
        )[:5]

        strengths = [k for k, v in dimensions.items() if v >= 65]
        weaknesses = [k for k, v in dimensions.items() if v < 45]

        analysis_summary = (
            f"7维度综合评分{total_score}分，评级{level}（{rec}）。"
            f"{'，'.join([dimension_names_cn.get(k, k) for k in strengths])}表现较好。"
            if strengths
            else f"7维度综合评分{total_score}分，评级{level}（{rec}）。"
        )

        result = {
            "ts_code": ts_code,
            "title": f"综合评级 - {ts_code}",
            "total_score": total_score,
            "rating_level": rec,
            "rating_code": level,
            "dimensions": formatted_dimensions,
            "dimension_weights": weights,
            "key_factors": key_factors,
            "analysis": {
                "summary": analysis_summary,
                "strengths": [{"dimension": k, "score": round(dimensions[k], 1)} for k in strengths],
                "weaknesses": [{"dimension": k, "score": round(dimensions[k], 1)} for k in weaknesses],
            },
            "recommendation": {
                "action": action,
                "position": position,
                "rationale": f"综合{len(strengths)}个维度表现良好",
            },
            "industry": params.get("industry", ""),
            "update_time": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "method": "rule_7d",
            "items": [
                {"维度": dimension_names_cn.get(k, k), "评分": round(v, 1), "权重": weights.get(k, 0.1)}
                for k, v in dimensions.items()
            ],
            "columns": [
                {"key": "维度", "label": "维度"},
                {"key": "评分", "label": "评分"},
                {"key": "权重", "label": "权重"},
            ],
        }
        result["for_llm"] = {
            "ts_code": ts_code,
            "total_score": total_score,
            "rating_level": rec,
            "rating_code": level,
            "action": action,
            "position": position,
            "fundamental_score": round(f_score, 1),
            "technical_score": round(t_score, 1),
            "valuation_score": round(v_score, 1),
            "capital_score": round(c_score, 1),
            "pe": spot.get("pe", 0),
            "pb": spot.get("pb", 0),
            "roe": financial.get("roe", 0),
            "analysis": analysis_summary,
        }
        return result

    except Exception as e:
        logger.error(f"综合评级失败: {e}", exc_info=True)
        err = f"综合评级失败: {str(e)}"
        return {"error": err, "for_llm": {"error": err}}


if __name__ == "__main__":
    import sys, json as _json
    if len(sys.argv) > 1:
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("--ts_code", default="")
        parser.add_argument("--industry", default="")
        args = parser.parse_args()
        params = {k: v for k, v in vars(args).items() if v}
    else:
        params = _json.loads(sys.stdin.read())
    result = main(params)
    print(_json.dumps(result, ensure_ascii=False, default=str))
