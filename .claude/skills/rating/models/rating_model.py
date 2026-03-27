"""
ML 评级模型 - 7维度升级版 — 真实训练版
基于 XGBRegressor 的股票综合评级

自动训练:
1. 检查磁盘是否存在已训练的评级模型
2. 若不存在, 通过 prediction/training/data_pipeline 构建 rating 数据集 → 训练 → 保存
3. 使用 ML 预测 + 7 维度规则评分混合

7维度评分框架:
- 基本面 (25%)  - 技术面 (15%)  - 估值面 (15%)
- 资金面 (15%)  - 情绪面 (10%)  - 事件面 (10%)  - 创新面 (10%)
"""
from typing import Dict, Any, List, Optional
from pathlib import Path
import numpy as np
from loguru import logger
from dataclasses import dataclass, asdict, field
import threading

try:
    import xgboost as xgb
    from sklearn.preprocessing import StandardScaler
    from sklearn.model_selection import train_test_split
    import joblib
    XGBOOST_AVAILABLE = True
except ImportError:
    XGBOOST_AVAILABLE = False

try:
    from ...prediction.prediction_config import get_prediction_config
except ImportError:
    get_prediction_config = None


@dataclass
class RatingResult:
    """评级结果 - 7维度版"""
    total_score: float
    rating_level: str
    recommendation: str
    confidence: float
    dimensions: Dict[str, float]
    key_factors: List[Dict[str, Any]]
    method: str = "ml_rating"
    dimension_weights: Dict[str, float] = field(default_factory=dict)
    analysis: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# 模型路径
_MODEL_DIR = Path(__file__).parent.parent.parent / "prediction" / "training" / "models"
_MODEL_DIR.mkdir(parents=True, exist_ok=True)
_RATING_MODEL_FILE = _MODEL_DIR / "rating_xgb.json"
_RATING_SCALER_FILE = _MODEL_DIR / "rating_scaler.pkl"
_TRAIN_LOCK = threading.Lock()


class MLRatingModel:
    """
    ML 评级模型 - 7维度升级版 — 真实训练版

    混合模式:
    - ML 模型: XGBRegressor 预测总分 (用代理标签训练)
    - 规则评分: 7 维度各自有规则逻辑计算分数
    - 最终分数 = ML 预测总分 * ml_weight + 7 维度加权平均 * rule_weight

    所有维度权重、行业调整、评级阈值和 ML/规则混合比例
    均从 config/prediction/factor_model.json 读取，类常量仅作为回退默认值。
    """

    DIMENSION_WEIGHTS = {
        "fundamental": 0.25,
        "technical": 0.15,
        "valuation": 0.15,
        "capital": 0.15,
        "sentiment": 0.10,
        "events": 0.10,
        "innovation": 0.10,
    }

    INDUSTRY_WEIGHT_ADJUSTMENTS = {
        "科技": {"innovation": 0.15, "fundamental": 0.20},
        "医药": {"innovation": 0.15, "fundamental": 0.20},
        "新能源": {"innovation": 0.12, "fundamental": 0.23},
        "白酒": {"fundamental": 0.30, "innovation": 0.05},
        "银行": {"fundamental": 0.30, "innovation": 0.05},
        "地产": {"capital": 0.20, "innovation": 0.05},
    }

    RATING_LEVELS = {
        (90, 100): ("A+", "强烈推荐"),
        (80, 90): ("A", "推荐"),
        (70, 80): ("B+", "积极"),
        (60, 70): ("B", "中性"),
        (50, 60): ("C", "观望"),
        (0, 50): ("D", "回避"),
    }

    def __init__(self, model_path: Optional[str] = None):
        self.model = None
        self.scaler = None
        self._trained = False

        if not XGBOOST_AVAILABLE:
            logger.warning("XGBoost unavailable, rating will use dimension-weighted scoring only")
            return

        model_file = Path(model_path) if model_path else _RATING_MODEL_FILE
        scaler_file = _RATING_SCALER_FILE

        if model_file.exists() and scaler_file.exists():
            self._load(model_file, scaler_file)
        else:
            logger.info("No pre-trained rating model found, will auto-train on first rate()")

    # ------------------------------------------------------------------ #
    #  加载 / 保存
    # ------------------------------------------------------------------ #

    def _load(self, model_file: Path, scaler_file: Path):
        try:
            self.model = xgb.XGBRegressor()
            self.model.load_model(str(model_file))
            self.scaler = joblib.load(str(scaler_file))
            self._trained = True
            logger.info(f"Loaded rating model from {model_file}")
        except Exception as e:
            logger.error(f"Failed to load rating model: {e}")
            self.model = None
            self.scaler = None
            self._trained = False

    def _save(self, model, scaler):
        model.save_model(str(_RATING_MODEL_FILE))
        joblib.dump(scaler, str(_RATING_SCALER_FILE))
        logger.info(f"Saved rating model to {_RATING_MODEL_FILE}")

    # ------------------------------------------------------------------ #
    #  自动训练
    # ------------------------------------------------------------------ #

    def _auto_train(self):
        if not XGBOOST_AVAILABLE:
            raise RuntimeError("xgboost not installed")

        with _TRAIN_LOCK:
            if self._trained:
                return

            # 使用 prediction 的 data_pipeline
            from ...prediction.training.data_pipeline import TrainingDataPipeline
            pipeline = TrainingDataPipeline()

            X, y = pipeline.load_dataset(horizon="rating", kind="rating")
            if X.size == 0:
                logger.info("Building rating dataset …")
                X, y = pipeline.build_rating_dataset(save=True)

            scaler = StandardScaler()
            X_scaled = scaler.fit_transform(X)

            model = xgb.XGBRegressor(
                n_estimators=200,
                max_depth=5,
                learning_rate=0.1,
                subsample=0.8,
                colsample_bytree=0.8,
                objective="reg:squarederror",
                random_state=42,
                n_jobs=-1,
            )

            X_train, X_val, y_train, y_val = train_test_split(
                X_scaled, y, test_size=0.2, random_state=42
            )
            model.fit(X_train, y_train, eval_set=[(X_val, y_val)], verbose=False)

            from sklearn.metrics import mean_absolute_error, r2_score
            y_pred = model.predict(X_val)
            mae = mean_absolute_error(y_val, y_pred)
            r2 = r2_score(y_val, y_pred)
            logger.info(f"Rating model auto-train done — MAE: {mae:.2f}, R²: {r2:.4f}")

            self._save(model, scaler)
            self.model = model
            self.scaler = scaler
            self._trained = True

    # ------------------------------------------------------------------ #
    #  特征提取 (16维 — 与 data_pipeline FEATURE_COLUMNS 一致)
    # ------------------------------------------------------------------ #

    def extract_ml_features(
        self,
        technical: Dict[str, Any],
        money: Dict[str, Any],
        valuation: Dict[str, Any],
    ) -> np.ndarray:
        """提取 16 维特征用于 ML 模型预测 (与 data_pipeline 一致)"""
        indicators = technical.get("indicators", {})
        ma = indicators.get("ma", {})
        macd = indicators.get("macd", {})
        kdj = indicators.get("kdj", {})
        rsi = indicators.get("rsi", {})
        summary = technical.get("summary", {})

        current_price = summary.get("current_price", 1) or 1

        features = [
            (ma.get("ma5", current_price) / current_price - 1) if current_price else 0,
            (ma.get("ma10", current_price) / current_price - 1) if current_price else 0,
            (ma.get("ma20", current_price) / current_price - 1) if current_price else 0,
            macd.get("macd", 0) / 100,
            macd.get("dif", 0) / 100,
            macd.get("dea", 0) / 100,
            kdj.get("k", 50) / 100,
            kdj.get("d", 50) / 100,
            kdj.get("j", 50) / 100,
            rsi.get("rsi14", 50) / 100,
            rsi.get("rsi6", 50) / 100,
            summary.get("amplitude", 0) / 10,
            summary.get("turnover_rate", 0) / 10,
            summary.get("volume_ratio", 1),
            summary.get("change_5d", 0) / 10,
            summary.get("change_10d", 0) / 10,
        ]

        return np.array(features, dtype=np.float32).reshape(1, -1)

    # ------------------------------------------------------------------ #
    #  评级入口
    # ------------------------------------------------------------------ #

    def rate(
        self,
        fundamental: Dict[str, Any],
        technical: Dict[str, Any],
        money: Dict[str, Any],
        valuation: Dict[str, Any],
        sentiment: Dict[str, Any] = None,
        events: Dict[str, Any] = None,
        innovation: Dict[str, Any] = None,
        industry: str = None,
    ) -> RatingResult:
        """7维度综合评级 — ML + 规则混合"""

        weights = self._get_adjusted_weights(industry)

        # 7 维度规则评分
        dimensions = self._calculate_dimensions(
            fundamental, technical, money, valuation,
            sentiment or {}, events or {}, innovation or {},
        )

        # 规则加权平均
        rule_score = sum(dimensions[k] * weights[k] for k in dimensions)

        # ML 预测总分
        ml_score = None
        if XGBOOST_AVAILABLE:
            if not self._trained:
                try:
                    self._auto_train()
                except Exception as e:
                    logger.warning(f"Rating auto-train failed: {e}")

            if self._trained:
                try:
                    features = self.extract_ml_features(technical, money, valuation)
                    features_scaled = self.scaler.transform(features)
                    ml_score = float(self.model.predict(features_scaled)[0])
                    ml_score = max(0, min(100, ml_score))
                except Exception as e:
                    logger.warning(f"ML rating prediction failed: {e}")

        # 混合: ML + 规则 (权重从配置读取)
        mix = self._get_ml_rule_mix()
        if ml_score is not None:
            total_score = ml_score * mix["ml_weight"] + rule_score * mix["rule_weight"]
            confidence = 0.85
            method = "ml_rating"
        else:
            total_score = rule_score
            confidence = 0.7
            method = "weighted_average"

        rating_level, recommendation = self._get_rating_level(total_score)
        key_factors = self._extract_key_factors(dimensions, weights)
        analysis = self._generate_analysis(dimensions, fundamental, money, events or {})

        return RatingResult(
            total_score=round(total_score, 1),
            rating_level=rating_level,
            recommendation=recommendation,
            confidence=round(confidence, 2),
            dimensions=dimensions,
            key_factors=key_factors,
            method=method,
            dimension_weights=weights,
            analysis=analysis,
        )

    # ------------------------------------------------------------------ #
    #  配置读取 (优先从 config/prediction/factor_model.json, 回退到类常量)
    # ------------------------------------------------------------------ #

    def _get_dimension_weights(self) -> Dict[str, float]:
        """从配置文件读取维度权重，失败时回退到类常量"""
        try:
            if get_prediction_config is not None:
                cfg = get_prediction_config()
                return cfg.factor_model.get("dimension_weights", self.DIMENSION_WEIGHTS)
        except Exception:
            pass
        return self.DIMENSION_WEIGHTS

    def _get_industry_weight_adjustments(self) -> Dict[str, Dict[str, float]]:
        """从配置文件读取行业权重调整，失败时回退到类常量"""
        try:
            if get_prediction_config is not None:
                cfg = get_prediction_config()
                return cfg.factor_model.get("industry_weight_adjustments", self.INDUSTRY_WEIGHT_ADJUSTMENTS)
        except Exception:
            pass
        return self.INDUSTRY_WEIGHT_ADJUSTMENTS

    def _get_rating_levels(self) -> Dict[tuple, tuple]:
        """从配置文件读取评级阈值和标签，失败时回退到类常量

        配置文件格式:
            rating_thresholds: {"A+": 90, "A": 80, ...}
            rating_labels: {"A+": "强烈推荐", "A": "推荐", ...}
        转换为:
            {(90, 100): ("A+", "强烈推荐"), (80, 90): ("A", "推荐"), ...}
        """
        try:
            if get_prediction_config is not None:
                cfg = get_prediction_config()
                thresholds = cfg.factor_model.get("rating_thresholds")
                labels = cfg.factor_model.get("rating_labels")
                if thresholds and labels:
                    # 按阈值降序排列，构建 (low, high) 区间
                    sorted_levels = sorted(thresholds.items(), key=lambda x: x[1], reverse=True)
                    result = {}
                    for i, (level, low) in enumerate(sorted_levels):
                        high = sorted_levels[i - 1][1] if i > 0 else 100
                        label = labels.get(level, level)
                        result[(low, high)] = (level, label)
                    return result
        except Exception:
            pass
        return self.RATING_LEVELS

    def _get_ml_rule_mix(self) -> Dict[str, float]:
        """从配置文件读取 ML/规则混合权重，失败时回退到默认值"""
        default = {"ml_weight": 0.6, "rule_weight": 0.4}
        try:
            if get_prediction_config is not None:
                cfg = get_prediction_config()
                return cfg.factor_model.get("ml_rule_mix", default)
        except Exception:
            pass
        return default

    # ------------------------------------------------------------------ #
    #  权重调整
    # ------------------------------------------------------------------ #

    def _get_adjusted_weights(self, industry: str = None) -> Dict[str, float]:
        weights = self._get_dimension_weights().copy()
        industry_adjustments = self._get_industry_weight_adjustments()
        if industry and industry in industry_adjustments:
            adjustments = industry_adjustments[industry]
            for dim, new_weight in adjustments.items():
                if dim in weights:
                    diff = new_weight - weights[dim]
                    weights[dim] = new_weight
                    other_dims = [d for d in weights if d != dim]
                    for d in other_dims:
                        weights[d] -= diff / len(other_dims)
            total = sum(weights.values())
            weights = {k: v / total for k, v in weights.items()}
        return weights

    # ------------------------------------------------------------------ #
    #  7 维度规则评分
    # ------------------------------------------------------------------ #

    def _calculate_dimensions(
        self,
        fundamental: Dict[str, Any],
        technical: Dict[str, Any],
        money: Dict[str, Any],
        valuation: Dict[str, Any],
        sentiment: Dict[str, Any],
        events: Dict[str, Any],
        innovation: Dict[str, Any],
    ) -> Dict[str, float]:
        dimensions = {}

        # 1. 基本面
        reports = fundamental.get("reports", [{}])
        latest_report = reports[0] if reports else {}
        f_score = 50
        roe = latest_report.get("roe", 0)
        revenue_yoy = latest_report.get("revenue_yoy", 0)
        profit_yoy = latest_report.get("profit_yoy", 0)
        gross_margin = latest_report.get("gross_margin", 0)
        debt_ratio = latest_report.get("debt_ratio", 50)

        if roe > 20: f_score += 20
        elif roe > 15: f_score += 15
        elif roe > 10: f_score += 10
        elif roe > 5: f_score += 5
        elif roe < 0: f_score -= 15

        if revenue_yoy > 30: f_score += 15
        elif revenue_yoy > 15: f_score += 10
        elif revenue_yoy > 5: f_score += 5
        elif revenue_yoy < -10: f_score -= 10

        if profit_yoy > 30: f_score += 15
        elif profit_yoy > 15: f_score += 10
        elif profit_yoy > 5: f_score += 5
        elif profit_yoy < -10: f_score -= 10

        if gross_margin > 50: f_score += 10
        elif gross_margin > 30: f_score += 5

        if debt_ratio > 70: f_score -= 15
        elif debt_ratio > 60: f_score -= 5

        dimensions["fundamental"] = round(min(100, max(0, f_score)), 1)

        # 2. 技术面
        indicators = technical.get("indicators", {})
        ma = indicators.get("ma", {})
        macd = indicators.get("macd", {})
        rsi_data = indicators.get("rsi", {})
        t_score = 50
        if ma.get("ma5", 0) > ma.get("ma10", 0) > ma.get("ma20", 0):
            t_score += 25
        elif ma.get("ma5", 0) > ma.get("ma20", 0):
            t_score += 15
        elif ma.get("ma5", 0) < ma.get("ma10", 0) < ma.get("ma20", 0):
            t_score -= 20
        if macd.get("macd", 0) > macd.get("signal", 0):
            t_score += 10
        elif macd.get("macd", 0) < macd.get("signal", 0):
            t_score -= 5
        rsi = rsi_data.get("rsi14", 50)
        if 30 < rsi < 70: t_score += 10
        elif rsi > 80 or rsi < 20: t_score -= 10
        dimensions["technical"] = round(min(100, max(0, t_score)), 1)

        # 3. 估值面
        percentiles = valuation.get("percentiles", {})
        pe_pct = percentiles.get("pe_percentile", 50)
        pb_pct = percentiles.get("pb_percentile", 50)
        v_score = 50
        if pe_pct < 20: v_score += 30
        elif pe_pct < 40: v_score += 20
        elif pe_pct < 60: v_score += 5
        elif pe_pct > 80: v_score -= 20
        if pb_pct < 20: v_score += 15
        elif pb_pct < 40: v_score += 10
        elif pb_pct > 80: v_score -= 10
        dimensions["valuation"] = round(min(100, max(0, v_score)), 1)

        # 4. 资金面
        money_summary = money.get("summary", {})
        main_net_5d = money_summary.get("main_net_5d", 0)
        main_net_20d = money_summary.get("main_net_20d", 0)
        stability = money_summary.get("flow_stability", 0.5)
        consecutive = money_summary.get("consecutive_inflow_days", 0)
        north = money_summary.get("north_bound", {})
        c_score = 50
        if main_net_5d > 5: c_score += 20
        elif main_net_5d > 1: c_score += 10
        elif main_net_5d < -5: c_score -= 20
        elif main_net_5d < -1: c_score -= 10
        if main_net_20d > 10: c_score += 15
        elif main_net_20d > 3: c_score += 8
        elif main_net_20d < -10: c_score -= 15
        if stability > 0.7: c_score += 10
        elif stability < 0.3: c_score -= 10
        if consecutive >= 5: c_score += 10
        elif consecutive >= 3: c_score += 5
        if north.get("available") and north.get("net_5d", 0) > 1: c_score += 10
        elif north.get("available") and north.get("net_5d", 0) < -1: c_score -= 10
        dimensions["capital"] = round(min(100, max(0, c_score)), 1)

        # 5. 情绪面
        sentiment_index = sentiment.get("sentiment_index", {})
        news_sent = sentiment.get("news_sentiment", {})
        prediction_sent = sentiment.get("prediction", {})
        s_score = 50
        overall_sentiment = sentiment_index.get("overall", 0.5)
        news_score = news_sent.get("score", 0.5)
        s_score += (overall_sentiment - 0.5) * 60
        if news_score > 0.7: s_score += 15
        elif news_score > 0.6: s_score += 8
        elif news_score < 0.3: s_score -= 15
        pred_direction = prediction_sent.get("direction", "neutral")
        pred_prob = prediction_sent.get("probability", 0.5)
        if pred_direction == "up" and pred_prob > 0.6: s_score += 15
        elif pred_direction == "down" and pred_prob > 0.6: s_score -= 15
        dimensions["sentiment"] = round(min(100, max(0, s_score)), 1)

        # 6. 事件面
        event_summary = events.get("event_summary", {})
        net_impact = event_summary.get("net_impact_score", 0)
        risk_level = event_summary.get("risk_level", "low")
        opportunity = event_summary.get("opportunity_level", "low")
        e_score = 60
        e_score += net_impact * 0.6
        if risk_level == "high": e_score -= 25
        elif risk_level == "medium": e_score -= 10
        if opportunity == "high": e_score += 20
        elif opportunity == "medium": e_score += 10
        dimensions["events"] = round(min(100, max(0, e_score)), 1)

        # 7. 创新面
        innovation_score = innovation.get("innovation_score", {})
        overall_innovation = innovation_score.get("overall", 50)
        dims = innovation_score.get("dimensions", {})
        if dims:
            i_score = (
                dims.get("research_output", 50) * 0.3 +
                dims.get("patent_quality", 50) * 0.3 +
                dims.get("tech_leadership", 50) * 0.25 +
                dims.get("rd_investment", 50) * 0.15
            )
        else:
            i_score = overall_innovation
        dimensions["innovation"] = round(min(100, max(0, i_score)), 1)

        return dimensions

    # ------------------------------------------------------------------ #
    #  辅助
    # ------------------------------------------------------------------ #

    def _get_rating_level(self, score: float) -> tuple:
        rating_levels = self._get_rating_levels()
        for (low, high), (level, rec) in rating_levels.items():
            if low <= score < high:
                return level, rec
        return "D", "回避"

    def _extract_key_factors(
        self, dimensions: Dict[str, float], weights: Dict[str, float]
    ) -> List[Dict[str, Any]]:
        dim_names = {
            "fundamental": "基本面", "technical": "技术面", "valuation": "估值面",
            "capital": "资金面", "sentiment": "情绪面", "events": "事件面",
            "innovation": "创新面",
        }

        weighted = [(dim, score, weights.get(dim, 0.1), score * weights.get(dim, 0.1))
                     for dim, score in dimensions.items()]
        weighted.sort(key=lambda x: x[3], reverse=True)

        factors = []
        for dim, score, weight, contribution in weighted[:5]:
            impact = "正面" if score >= 65 else ("中性" if score >= 45 else "负面")
            factors.append({
                "dimension": dim,
                "dimension_cn": dim_names.get(dim, dim),
                "score": round(score, 1),
                "weight": round(weight, 2),
                "weighted_contribution": round(contribution, 2),
                "impact": impact,
                "description": self._get_factor_description(dim, score),
            })
        return factors

    def _get_factor_description(self, dimension: str, score: float) -> str:
        descriptions = {
            "fundamental": {"high": "财务状况优秀，盈利能力强", "medium": "财务状况良好，盈利稳定", "low": "财务状况一般，需关注"},
            "technical": {"high": "技术形态强势，趋势向上", "medium": "技术形态中性，震荡整理", "low": "技术形态偏弱，注意风险"},
            "valuation": {"high": "估值具有吸引力，存在低估", "medium": "估值合理，处于正常区间", "low": "估值偏高，需谨慎"},
            "capital": {"high": "资金持续流入，主力积极", "medium": "资金面中性，观望为主", "low": "资金流出明显，需警惕"},
            "sentiment": {"high": "市场情绪积极，关注度高", "medium": "市场情绪中性，波动正常", "low": "市场情绪偏谨慎"},
            "events": {"high": "近期利好事件较多", "medium": "无重大事件影响", "low": "存在潜在风险事件"},
            "innovation": {"high": "研发实力强，创新能力突出", "medium": "研发投入正常，技术稳定", "low": "创新能力一般"},
        }
        level = "high" if score >= 65 else ("medium" if score >= 45 else "low")
        return descriptions.get(dimension, {}).get(level, "评分正常")

    def _generate_analysis(
        self, dimensions: Dict[str, float],
        fundamental: Dict[str, Any], money: Dict[str, Any], events: Dict[str, Any],
    ) -> Dict[str, Any]:
        analysis = {"strengths": [], "weaknesses": [], "opportunities": [], "risks": [], "summary": ""}

        for dim, score in dimensions.items():
            if score >= 65:
                analysis["strengths"].append({"dimension": dim, "score": score, "detail": self._get_factor_description(dim, score)})
        for dim, score in dimensions.items():
            if score < 45:
                analysis["weaknesses"].append({"dimension": dim, "score": score, "detail": self._get_factor_description(dim, score)})

        event_summary = events.get("event_summary", {})
        if event_summary.get("opportunity_level") in ["high", "medium"]:
            analysis["opportunities"].append({"type": "event", "detail": "近期存在积极事件或催化剂"})

        money_summary = money.get("summary", {})
        if money_summary.get("consecutive_inflow_days", 0) >= 3:
            analysis["opportunities"].append({"type": "capital", "detail": f"主力资金连续{money_summary.get('consecutive_inflow_days')}日流入"})

        if event_summary.get("risk_level") in ["high", "medium"]:
            analysis["risks"].append({"type": "event", "detail": "存在潜在风险事件需关注"})
        if dimensions.get("valuation", 50) < 40:
            analysis["risks"].append({"type": "valuation", "detail": "当前估值偏高，注意回调风险"})

        avg_score = sum(dimensions.values()) / len(dimensions) if dimensions else 50
        if avg_score >= 70: summary = "综合评估优秀，各维度表现均衡，具有较好的投资价值"
        elif avg_score >= 55: summary = "综合评估良好，存在一定投资机会，建议关注"
        elif avg_score >= 45: summary = "综合评估中性，建议观望等待更好时机"
        else: summary = "综合评估偏弱，建议谨慎操作，注意风险控制"

        analysis["summary"] = summary
        analysis["dimension_stats"] = {
            "avg_score": round(avg_score, 1),
            "max_dim": max(dimensions.items(), key=lambda x: x[1]) if dimensions else ("", 0),
            "min_dim": min(dimensions.items(), key=lambda x: x[1]) if dimensions else ("", 0),
        }
        return analysis


# 全局延迟初始化
_model: Optional[MLRatingModel] = None
_init_lock = threading.Lock()


def get_ml_rating_model() -> MLRatingModel:
    global _model
    if _model is None:
        with _init_lock:
            if _model is None:
                _model = MLRatingModel()
    return _model


ml_rating_model = None  # type: ignore
