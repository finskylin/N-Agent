"""
Sentiment Analysis Skill
舆情情感分析技能 - 分析个股新闻情感倾向
"""
import os
import sys
from pathlib import Path
from typing import Dict, Any, List

# 将 scripts 目录加入 path，以便导入 sentiment_trainer
_scripts_dir = Path(__file__).resolve().parent
if str(_scripts_dir) not in sys.path:
    sys.path.insert(0, str(_scripts_dir))

from sentiment_trainer import SentimentMLPredictor, get_sentiment_predictor


def _get_stock_news(code: str, limit: int = 20) -> List[Dict[str, str]]:
    """通过 AkShare 获取个股新闻"""
    try:
        import akshare as ak

        # 尝试个股新闻
        df = ak.stock_news_em(symbol=code)
        if df is not None and not df.empty:
            news = []
            for _, row in df.head(limit).iterrows():
                title = str(row.get('新闻标题', row.get('title', '')))
                content = str(row.get('新闻内容', row.get('content', '')))
                pub_time = str(row.get('发布时间', row.get('时间', '')))
                if title:
                    news.append({
                        "title": title,
                        "content": content[:200] if content else "",
                        "pub_time": pub_time
                    })
            return news
    except Exception as e:
        print(f"[sentiment_analysis] get_stock_news AkShare error: {e}")

    return []


def _score_to_label(score: float) -> str:
    """将情感分数转换为标签"""
    if score >= 0.6:
        return "正面"
    elif score <= 0.4:
        return "负面"
    else:
        return "中性"


def main(params: Dict[str, Any]) -> Dict[str, Any]:
    """
    分析个股舆情情感

    params:
        ts_code (str): 股票代码，如 600519.SH
        limit (int): 分析最近 N 条新闻，默认 20
    """
    ts_code = params.get("ts_code", "").strip()
    if not ts_code:
        return {
            "error": "缺少 ts_code 参数",
            "for_llm": {"error": "缺少 ts_code 参数"}
        }

    limit = int(params.get("limit", 20))
    code = ts_code.split('.')[0]

    # 获取新闻
    news_list = _get_stock_news(code, limit)

    if not news_list:
        return {
            "ts_code": ts_code,
            "news_count": 0,
            "overall_score": 0.5,
            "overall_label": "中性",
            "confidence": 0.0,
            "news_items": [],
            "message": f"未找到 {ts_code} 的相关新闻",
            "for_llm": {
                "ts_code": ts_code,
                "overall_label": "中性",
                "overall_score": 0.5,
                "news_count": 0,
                "message": f"未找到 {ts_code} 的相关新闻"
            }
        }

    # 获取情感预测器
    predictor = get_sentiment_predictor()

    # 对每条新闻进行情感分析
    news_items = []
    scores = []
    confidences = []
    positive_count = 0
    negative_count = 0
    neutral_count = 0

    for item in news_list:
        text = item["title"] + " " + item.get("content", "")
        result = predictor.predict(text)
        score = result["score"]
        confidence = result["confidence"]
        label = _score_to_label(score)

        if label == "正面":
            positive_count += 1
        elif label == "负面":
            negative_count += 1
        else:
            neutral_count += 1

        scores.append(score)
        confidences.append(confidence)

        news_items.append({
            "title": item["title"],
            "pub_time": item.get("pub_time", ""),
            "score": round(score, 4),
            "label": label,
            "confidence": round(confidence, 4),
            "method": result.get("method", "dictionary")
        })

    # 计算整体分数
    overall_score = sum(scores) / len(scores) if scores else 0.5
    avg_confidence = sum(confidences) / len(confidences) if confidences else 0.0
    overall_label = _score_to_label(overall_score)

    return {
        "ts_code": ts_code,
        "news_count": len(news_items),
        "overall_score": round(overall_score, 4),
        "overall_label": overall_label,
        "confidence": round(avg_confidence, 4),
        "news_items": news_items,
        "for_llm": {
            "ts_code": ts_code,
            "overall_label": overall_label,
            "overall_score": round(overall_score, 4),
            "news_count": len(news_items),
            "positive_count": positive_count,
            "negative_count": negative_count,
            "neutral_count": neutral_count,
            "top_titles": [n["title"] for n in news_items[:3]]
        }
    }


if __name__ == "__main__":
    import json
    import argparse

    if len(sys.argv) > 1:
        parser = argparse.ArgumentParser(description="舆情情感分析")
        parser.add_argument("--ts_code", type=str, required=True, help="股票代码，如 600519.SH")
        parser.add_argument("--limit", type=int, default=20, help="分析最近 N 条新闻")
        args = parser.parse_args()
        result = main({"ts_code": args.ts_code, "limit": args.limit})
    else:
        data = json.loads(sys.stdin.read())
        result = main(data)

    print(json.dumps(result, ensure_ascii=False))
