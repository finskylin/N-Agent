"""
LLM Rating Enhancer
LLM 评级增强器 - 7.4 LLM融合实现

功能:
1. 使用 LLM 增强 ML 评级结果
2. 执行一致性分析、风险识别、优势分析
3. 融合 ML 和 LLM 结果
4. 生成个性化分析报告
"""
import json
import re
from typing import Dict, Any, List, Optional
from loguru import logger
from datetime import datetime


# ========== LLM Prompt 模板 ==========

LLM_RATING_PROMPT = """
你是一位资深证券分析师，请基于以下多维度数据对 {stock_name}({ts_code}) 进行综合分析。

## 一、多维度评分数据

### 基本面 (权重25%, 当前评分: {fundamental_score})
- ROE: {roe}%
- 毛利率: {gross_margin}%
- 营收同比: {revenue_yoy}%
- 净利润同比: {profit_yoy}%
- 资产负债率: {debt_ratio}%
- 成长质量评分: {growth_quality}

### 技术面 (权重15%, 当前评分: {technical_score})
- 均线排列: {ma_status}
- MACD状态: {macd_status}
- RSI(14): {rsi}
- 技术信号: {signals}

### 估值面 (权重15%, 当前评分: {valuation_score})
- 当前PE: {pe_ttm}
- PE历史分位: {pe_percentile}%
- 当前PB: {pb}
- PB历史分位: {pb_percentile}%
- 估值结论: {valuation_conclusion}

### 资金面 (权重15%, 当前评分: {capital_score})
- 5日主力净流入: {main_net_5d}亿
- 20日主力净流入: {main_net_20d}亿
- 资金稳定性: {flow_stability}
- 北向资金: {north_net}亿
- 资金行为: {capital_behavior}

### 情绪面 (权重10%, 当前评分: {sentiment_score})
- AI预测方向: {prediction_direction}
- 预测概率: {prediction_probability}
- 新闻情绪: {news_sentiment}

### 事件面 (权重10%, 当前评分: {event_score})
- 近期重大事件:
{recent_events}
- 净影响评分: {event_impact}

### 创新面 (权重10%, 当前评分: {innovation_score})
- 研发费用率: {rd_ratio}%
- 专利/论文: {innovation_items}
- 创新评分: {innovation_overall}

## 二、ML模型初步评级

- 综合评分: {ml_total_score}
- 评级等级: {ml_rating_level}
- 模型置信度: {model_confidence}

## 三、用户对话上下文

{conversation_context}

用户特别关注: {user_focus}

## 四、请完成以下分析任务

### 任务1: 数据一致性分析
检查各维度数据是否存在逻辑矛盾或异常。

### 任务2: 关键风险识别
列出当前最需要关注的3个风险点。

### 任务3: 核心优势分析
列出该股票最突出的3个投资优势。

### 任务4: 评级调整建议
基于以上分析，你认为ML模型的评级是否合理？如需调整，建议调整幅度 (-10 到 +10 分)。

### 任务5: 投资建议
给出具体的操作建议(操作方向、建议仓位、止损位、目标价位)。

### 任务6: 针对用户关注点的回应
根据用户在对话中表达的关注点，给出针对性的分析。

## 五、输出格式要求

请严格按照以下JSON格式输出:

```json
{{
    "consistency_analysis": {{
        "has_contradictions": true/false,
        "contradictions": [
            {{
                "dimension1": "维度1",
                "dimension2": "维度2",
                "description": "矛盾描述",
                "possible_reason": "可能原因"
            }}
        ]
    }},

    "key_risks": [
        {{
            "risk": "风险名称",
            "severity": "high/medium/low",
            "description": "风险描述"
        }}
    ],

    "key_advantages": [
        {{
            "advantage": "优势名称",
            "strength": "high/medium",
            "description": "优势描述"
        }}
    ],

    "rating_adjustment": {{
        "adjustment": 0,
        "reason": "调整理由",
        "confidence": 0.8
    }},

    "recommendation": {{
        "action": "买入/持有/观望/减持",
        "position": "建议仓位",
        "stop_loss": "止损价",
        "target_price": "目标价",
        "holding_period": "持有周期",
        "rationale": "建议理由"
    }},

    "user_focused_analysis": "针对用户关注点的分析...",

    "analysis_summary": "综合分析摘要..."
}}
```
"""


class LLMRatingEnhancer:
    """
    LLM 评级增强器

    使用 LLM 增强 ML 评级结果:
    1. 一致性检查 - 检测各维度数据是否存在逻辑矛盾
    2. 风险识别 - 识别ML模型可能遗漏的风险点
    3. 评级校准 - 根据定性分析调整ML评分 (±10分)
    4. 报告生成 - 生成自然语言分析报告
    5. 个性化建议 - 根据用户关注点定制建议
    """

    def __init__(self, llm_client=None):
        """
        初始化 LLM 增强器

        Args:
            llm_client: LLM 客户端 (支持 Anthropic Claude 或 OpenAI)
        """
        self.llm = llm_client
        self.max_retries = 3
        self._initialized = False

    def _ensure_llm_client(self):
        """确保 LLM 客户端已初始化"""
        if self.llm is not None:
            return True

        # 尝试初始化 LLM 客户端
        try:
            from anthropic import Anthropic
            import os

            api_key = os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY")
            if api_key:
                self.llm = Anthropic(api_key=api_key)
                self._llm_type = "anthropic"
                logger.info("LLM client initialized with Anthropic")
                return True
        except ImportError:
            pass

        try:
            from openai import OpenAI
            import os

            api_key = os.environ.get("OPENAI_API_KEY")
            if api_key:
                self.llm = OpenAI(api_key=api_key)
                self._llm_type = "openai"
                logger.info("LLM client initialized with OpenAI")
                return True
        except ImportError:
            pass

        logger.warning("No LLM client available, LLM enhancement will be skipped")
        return False

    async def enhance_rating(
        self,
        ml_rating: Dict[str, Any],
        data_pool: Dict[str, Any],
        conversation_history: List[Dict] = None,
        user_focus: Dict[str, Any] = None
    ) -> Dict[str, Any]:
        """
        使用 LLM 增强评级结果

        Args:
            ml_rating: ML模型的评级结果
            data_pool: 所有依赖Skill的数据
            conversation_history: 对话历史
            user_focus: 用户关注点

        Returns:
            增强后的评级结果
        """
        if not self._ensure_llm_client():
            return self._fallback_result(ml_rating)

        # Step 1: 构建 Prompt
        prompt = self._build_prompt(ml_rating, data_pool, conversation_history, user_focus)

        # Step 2: 调用 LLM
        llm_result = None
        for attempt in range(self.max_retries):
            try:
                response = await self._call_llm(prompt)

                # Step 3: 解析 JSON 输出
                llm_result = self._parse_response(response)
                if llm_result:
                    break

            except Exception as e:
                logger.warning(f"LLM call attempt {attempt+1} failed: {e}")
                if attempt == self.max_retries - 1:
                    # 降级: 使用 ML 结果
                    return self._fallback_result(ml_rating)

        if not llm_result:
            return self._fallback_result(ml_rating)

        # Step 4: 融合 ML 和 LLM 结果
        final_rating = self._merge_results(ml_rating, llm_result)

        return final_rating

    async def _call_llm(self, prompt: str) -> str:
        """调用 LLM API"""
        if hasattr(self, '_llm_type') and self._llm_type == "anthropic":
            response = self.llm.messages.create(
                model="claude-3-sonnet-20240229",
                max_tokens=2500,
                temperature=0.3,
                messages=[
                    {"role": "user", "content": prompt}
                ]
            )
            return response.content[0].text

        elif hasattr(self, '_llm_type') and self._llm_type == "openai":
            response = self.llm.chat.completions.create(
                model="gpt-4-turbo-preview",
                max_tokens=2500,
                temperature=0.3,
                messages=[
                    {"role": "user", "content": prompt}
                ]
            )
            return response.choices[0].message.content

        raise ValueError("No valid LLM client configured")

    def _build_prompt(
        self,
        ml_rating: Dict[str, Any],
        data_pool: Dict[str, Any],
        conversation_history: List[Dict] = None,
        user_focus: Dict[str, Any] = None
    ) -> str:
        """构建 LLM Prompt"""

        # 提取各维度数据
        dimensions = ml_rating.get("dimensions", {})

        # 从 data_pool 提取详细数据
        financial = data_pool.get("financial_report", {})
        tech = data_pool.get("technical_indicators", {})
        valuation = data_pool.get("valuation_analysis", {})
        money = data_pool.get("money_flow", {})
        sentiment = data_pool.get("sentiment", {})
        events = data_pool.get("events", {})
        innovation = data_pool.get("innovation", {})

        # 提取财务数据
        reports = financial.get("reports", [{}])
        latest_report = reports[0] if reports else {}

        # 提取技术指标
        indicators = tech.get("indicators", {})
        ma = indicators.get("ma", {})
        macd = indicators.get("macd", {})

        # 提取估值数据
        percentiles = valuation.get("percentiles", {})

        # 提取资金数据
        money_summary = money.get("summary", {})

        # 构建对话上下文
        conversation_context = "无对话历史"
        if conversation_history:
            recent = conversation_history[-5:]  # 最近5轮
            context_parts = []
            for msg in recent:
                role = msg.get("role", "")
                content = msg.get("content", "")[:200]  # 截断
                context_parts.append(f"[{role}]: {content}")
            conversation_context = "\n".join(context_parts)

        # 构建用户关注点
        user_focus_text = "无特别关注"
        if user_focus:
            focus_parts = []
            if user_focus.get("mentioned_indicators"):
                focus_parts.append(f"关注指标: {', '.join(user_focus['mentioned_indicators'])}")
            if user_focus.get("concerns"):
                focus_parts.append(f"担忧: {', '.join(user_focus['concerns'])}")
            if user_focus.get("time_horizon"):
                horizon_map = {"short": "短线", "medium": "中线", "long": "长线"}
                focus_parts.append(f"投资周期: {horizon_map.get(user_focus['time_horizon'], '未知')}")
            if focus_parts:
                user_focus_text = "; ".join(focus_parts)

        # 构建近期事件
        recent_events_text = "无重大事件"
        event_items = events.get("recent_events", [])
        if event_items:
            event_lines = [f"- {e.get('title', '')} ({e.get('date', '')})" for e in event_items[:5]]
            recent_events_text = "\n".join(event_lines)

        # 填充模板
        return LLM_RATING_PROMPT.format(
            stock_name=ml_rating.get("stock_name", "未知"),
            ts_code=ml_rating.get("ts_code", ""),

            # 基本面
            fundamental_score=dimensions.get("fundamental", 50),
            roe=latest_report.get("roe", 0),
            gross_margin=latest_report.get("gross_margin", 0),
            revenue_yoy=latest_report.get("revenue_yoy", 0),
            profit_yoy=latest_report.get("profit_yoy", 0),
            debt_ratio=latest_report.get("debt_ratio", 0),
            growth_quality=financial.get("trend_analysis", {}).get("growth_quality", 50),

            # 技术面
            technical_score=dimensions.get("technical", 50),
            ma_status="多头排列" if ma.get("ma5", 0) > ma.get("ma20", 0) else "空头排列",
            macd_status="金叉" if macd.get("macd", 0) > macd.get("signal", 0) else "死叉",
            rsi=indicators.get("rsi", {}).get("rsi", 50),
            signals=self._format_signals(tech.get("signals", [])),

            # 估值面
            valuation_score=dimensions.get("valuation", 50),
            pe_ttm=valuation.get("pe_ttm", 0),
            pe_percentile=percentiles.get("pe_percentile", 50),
            pb=valuation.get("pb", 0),
            pb_percentile=percentiles.get("pb_percentile", 50),
            valuation_conclusion=valuation.get("conclusion", "无法判断"),

            # 资金面
            capital_score=dimensions.get("capital", 50),
            main_net_5d=money_summary.get("main_net_5d", 0),
            main_net_20d=money_summary.get("main_net_20d", 0),
            flow_stability=money_summary.get("flow_stability", 0.5),
            north_net=money_summary.get("north_net_10d", 0),
            capital_behavior=money_summary.get("behavior", "中性"),

            # 情绪面
            sentiment_score=dimensions.get("sentiment", 50),
            prediction_direction=sentiment.get("prediction", {}).get("direction", "neutral"),
            prediction_probability=sentiment.get("prediction", {}).get("probability", 0.5),
            news_sentiment=sentiment.get("news_sentiment", {}).get("score", 0.5),

            # 事件面
            event_score=dimensions.get("events", 50),
            recent_events=recent_events_text,
            event_impact=events.get("event_summary", {}).get("net_impact_score", 0),

            # 创新面
            innovation_score=dimensions.get("innovation", 50),
            rd_ratio=latest_report.get("rd_ratio", 0),
            innovation_items=self._format_innovation(innovation),
            innovation_overall=innovation.get("innovation_score", {}).get("overall", 50),

            # ML评级
            ml_total_score=ml_rating.get("total_score", 50),
            ml_rating_level=ml_rating.get("rating_level", "中性"),
            model_confidence=ml_rating.get("model_confidence", 0.6),

            # 对话上下文
            conversation_context=conversation_context,
            user_focus=user_focus_text
        )

    def _format_signals(self, signals: List[Dict]) -> str:
        """格式化技术信号"""
        if not signals:
            return "无明显信号"
        parts = []
        for s in signals[:3]:
            signal_type = s.get("type", "")
            signal_value = s.get("signal", "")
            parts.append(f"{signal_type}:{signal_value}")
        return ", ".join(parts)

    def _format_innovation(self, innovation: Dict) -> str:
        """格式化创新数据"""
        dims = innovation.get("innovation_score", {}).get("dimensions", {})
        parts = []
        if dims.get("research_output", 50) > 60:
            parts.append("研发产出良好")
        if dims.get("patent_quality", 50) > 60:
            parts.append("专利质量较高")
        if dims.get("tech_leadership", 50) > 60:
            parts.append("技术领先")
        return ", ".join(parts) if parts else "创新数据有限"

    def _parse_response(self, response: str) -> Optional[Dict[str, Any]]:
        """解析 LLM 响应中的 JSON"""
        try:
            # 尝试提取 JSON 块
            json_match = re.search(r'```json\s*(.*?)\s*```', response, re.DOTALL)
            if json_match:
                json_str = json_match.group(1)
            else:
                # 尝试直接解析整个响应
                json_str = response

            # 清理可能的问题字符
            json_str = json_str.strip()

            return json.loads(json_str)

        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse LLM response as JSON: {e}")
            # 尝试修复常见的 JSON 问题
            try:
                # 移除可能的注释
                cleaned = re.sub(r'//.*?$', '', json_str, flags=re.MULTILINE)
                return json.loads(cleaned)
            except:
                return None

    def _merge_results(self, ml_rating: Dict, llm_result: Dict) -> Dict[str, Any]:
        """融合 ML 和 LLM 评级结果"""

        # 计算最终评分
        ml_score = ml_rating.get("total_score", 50)
        llm_adjustment = llm_result.get("rating_adjustment", {}).get("adjustment", 0)

        # 限制调整幅度
        llm_adjustment = max(-10, min(10, llm_adjustment))

        # 加权融合: ML 80% + LLM调整 20%
        final_score = ml_score * 0.8 + (ml_score + llm_adjustment) * 0.2
        final_score = max(0, min(100, final_score))

        # 确定评级等级
        rating_level, rating_text = self._get_rating_level(final_score)

        # 确保 dimensions 格式正确 (前端期望 {name, score, weight} 结构)
        raw_dimensions = ml_rating.get("dimensions", {})
        dimension_names_cn = {
            "fundamental": "基本面",
            "technical": "技术面",
            "valuation": "估值面",
            "capital": "资金面",
            "sentiment": "情绪面",
            "events": "事件面",
            "innovation": "创新面"
        }
        dimension_weights = ml_rating.get("dimension_weights", {})

        formatted_dimensions = {}
        for k, v in raw_dimensions.items():
            if isinstance(v, dict) and "score" in v:
                # 已经是正确格式
                formatted_dimensions[k] = v
            else:
                # 转换为正确格式
                formatted_dimensions[k] = {
                    "name": dimension_names_cn.get(k, k),
                    "score": round(v, 1) if isinstance(v, (int, float)) else 50,
                    "weight": dimension_weights.get(k, 0.1)
                }

        # 组装最终结果
        return {
            "ts_code": ml_rating.get("ts_code"),
            "stock_name": ml_rating.get("stock_name", ""),
            "total_score": round(final_score, 1),
            "rating_level": rating_text,
            "rating_code": rating_level,

            "dimensions": formatted_dimensions,
            "dimension_weights": dimension_weights,

            "ml_result": {
                "score": ml_score,
                "weight": 0.8,
                "method": ml_rating.get("method", "unknown")
            },

            "llm_result": {
                "adjustment": llm_adjustment,
                "weight": 0.2,
                "confidence": llm_result.get("rating_adjustment", {}).get("confidence", 0.7),
                "reason": llm_result.get("rating_adjustment", {}).get("reason", "")
            },

            "analysis": {
                "consistency": llm_result.get("consistency_analysis", {}),
                "risks": llm_result.get("key_risks", []),
                "advantages": llm_result.get("key_advantages", []),
                "summary": llm_result.get("analysis_summary", "")
            },

            "recommendation": llm_result.get("recommendation", ml_rating.get("recommendation", {})),

            "user_focused_analysis": llm_result.get("user_focused_analysis", ""),

            "key_factors": ml_rating.get("key_factors", []),

            "model_info": {
                "method": "ml_ensemble + llm_reasoning",
                "ml_method": ml_rating.get("method", "unknown"),
                "llm_model": "claude-3-sonnet" if hasattr(self, '_llm_type') and self._llm_type == "anthropic" else "gpt-4",
                "data_sources": list(ml_rating.get("data_sources", {}).keys()) if "data_sources" in ml_rating else []
            },

            "update_time": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }

    def _get_rating_level(self, score: float) -> tuple:
        """根据分数获取评级等级"""
        if score >= 85:
            return "A+", "强烈推荐"
        elif score >= 75:
            return "A", "推荐"
        elif score >= 65:
            return "B+", "积极"
        elif score >= 55:
            return "B", "中性"
        elif score >= 45:
            return "C", "观望"
        else:
            return "D", "回避"

    def _fallback_result(self, ml_rating: Dict) -> Dict[str, Any]:
        """降级结果 - 当 LLM 不可用时使用"""
        return {
            **ml_rating,
            "llm_result": {
                "adjustment": 0,
                "weight": 0,
                "confidence": 0,
                "reason": "LLM服务不可用，使用ML结果"
            },
            "model_info": {
                "method": "ml_only_fallback",
                "ml_method": ml_rating.get("method", "unknown"),
                "llm_model": None
            },
            "update_time": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }


# ========== 工厂函数 ==========

_enhancer_instance: Optional[LLMRatingEnhancer] = None


def get_llm_enhancer() -> LLMRatingEnhancer:
    """获取 LLM 增强器单例"""
    global _enhancer_instance
    if _enhancer_instance is None:
        _enhancer_instance = LLMRatingEnhancer()
    return _enhancer_instance
