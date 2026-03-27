"""
市场状态检测模块
识别当前市场为牛市(bull)、熊市(bear)或震荡(range)，
用于动态调整融合模型的权重分配。

所有阈值和权重参数从 config/prediction/stacking.json 读取，
若配置缺失则回退到硬编码默认值以保证向后兼容。
"""
from typing import Dict, List, Optional
import numpy as np
from loguru import logger

from ..prediction_config import get_prediction_config


# ---------------------------------------------------------------------------
# 默认值常量 (仅在配置文件缺失时作为 fallback)
# ---------------------------------------------------------------------------
_DEFAULT_REGIME_WEIGHTS: Dict[str, Dict[str, float]] = {
    "bull": {"xgboost": 0.30, "lightgbm": 0.25, "lstm": 0.25, "sentiment": 0.20},
    "bear": {"xgboost": 0.25, "lightgbm": 0.25, "lstm": 0.15, "sentiment": 0.35},
    "range": {"xgboost": 0.35, "lightgbm": 0.35, "lstm": 0.15, "sentiment": 0.15},
}

_DEFAULT_REGIME_DETECTION = {
    "return_bull_threshold": 0.03,
    "return_bear_threshold": -0.03,
    "breadth_bull_threshold": 0.6,
    "breadth_bear_threshold": 0.4,
    "volatility_high_threshold": 0.25,
    "volatility_low_threshold": 0.15,
    "kline_bull_return": 0.05,
    "kline_bear_return": -0.05,
}

_DEFAULT_SOFTMAX_TEMPERATURE = 2.0


def _load_regime_weights() -> Dict[str, Dict[str, float]]:
    """从配置加载 regime_weights，缺失时返回默认值。"""
    try:
        cfg = get_prediction_config()
        weights = cfg.stacking.get("regime_weights")
        if weights and isinstance(weights, dict):
            return weights
    except Exception as e:
        logger.warning(f"Failed to load regime_weights from config: {e}")
    return _DEFAULT_REGIME_WEIGHTS.copy()


def _load_regime_detection() -> Dict[str, float]:
    """从配置加载 regime_detection 阈值，缺失时返回默认值。"""
    try:
        cfg = get_prediction_config()
        detection = cfg.stacking.get("regime_detection")
        if detection and isinstance(detection, dict):
            return detection
    except Exception as e:
        logger.warning(f"Failed to load regime_detection from config: {e}")
    return _DEFAULT_REGIME_DETECTION.copy()


def _load_softmax_temperature() -> float:
    """从配置加载 softmax_temperature，缺失时返回默认值。"""
    try:
        cfg = get_prediction_config()
        temp = cfg.stacking.get("softmax_temperature")
        if temp is not None and isinstance(temp, (int, float)):
            return float(temp)
    except Exception as e:
        logger.warning(f"Failed to load softmax_temperature from config: {e}")
    return _DEFAULT_SOFTMAX_TEMPERATURE


class MarketRegimeDetector:
    """
    市场状态检测器
    通过多维信号(指数收益、市场宽度、波动率、均线位置)判断市场状态，
    并据此返回各子模型的推荐权重。

    所有阈值和权重从 config/prediction/stacking.json 读取。
    """

    def __init__(self):
        # 从配置加载权重和阈值
        self.REGIME_WEIGHTS: Dict[str, Dict[str, float]] = _load_regime_weights()

        detection = _load_regime_detection()
        self.RETURN_BULL_THRESHOLD = detection.get(
            "return_bull_threshold", _DEFAULT_REGIME_DETECTION["return_bull_threshold"]
        )
        self.RETURN_BEAR_THRESHOLD = detection.get(
            "return_bear_threshold", _DEFAULT_REGIME_DETECTION["return_bear_threshold"]
        )
        self.BREADTH_BULL_THRESHOLD = detection.get(
            "breadth_bull_threshold", _DEFAULT_REGIME_DETECTION["breadth_bull_threshold"]
        )
        self.BREADTH_BEAR_THRESHOLD = detection.get(
            "breadth_bear_threshold", _DEFAULT_REGIME_DETECTION["breadth_bear_threshold"]
        )
        self.VOLATILITY_HIGH_THRESHOLD = detection.get(
            "volatility_high_threshold", _DEFAULT_REGIME_DETECTION["volatility_high_threshold"]
        )
        self.VOLATILITY_LOW_THRESHOLD = detection.get(
            "volatility_low_threshold", _DEFAULT_REGIME_DETECTION["volatility_low_threshold"]
        )

        logger.debug(
            f"MarketRegimeDetector initialized with thresholds: "
            f"return_bull={self.RETURN_BULL_THRESHOLD}, "
            f"return_bear={self.RETURN_BEAR_THRESHOLD}, "
            f"breadth_bull={self.BREADTH_BULL_THRESHOLD}, "
            f"breadth_bear={self.BREADTH_BEAR_THRESHOLD}, "
            f"vol_high={self.VOLATILITY_HIGH_THRESHOLD}, "
            f"vol_low={self.VOLATILITY_LOW_THRESHOLD}"
        )

    def detect_regime(self, market_data: Dict) -> str:
        """
        根据多维度市场信号判断市场状态。

        Args:
            market_data: 包含以下可选字段的字典:
                - index_return (float): 市场指数近期收益率
                - breadth (float): 市场宽度(上涨股票占比, 0~1)
                - volatility (float): 市场波动率(年化)
                - price (float): 当前价格
                - ma20 (float): 20日均线

        Returns:
            "bull", "bear", 或 "range"
        """
        scores = {"bull": 0.0, "bear": 0.0, "range": 0.0}

        # 信号 1: 指数收益率
        index_return = market_data.get("index_return")
        if index_return is not None:
            if index_return > self.RETURN_BULL_THRESHOLD:
                scores["bull"] += 1.0
            elif index_return < self.RETURN_BEAR_THRESHOLD:
                scores["bear"] += 1.0
            else:
                scores["range"] += 1.0

        # 信号 2: 市场宽度
        breadth = market_data.get("breadth")
        if breadth is not None:
            if breadth > self.BREADTH_BULL_THRESHOLD:
                scores["bull"] += 1.0
            elif breadth < self.BREADTH_BEAR_THRESHOLD:
                scores["bear"] += 1.0
            else:
                scores["range"] += 1.0

        # 信号 3: 波动率
        volatility = market_data.get("volatility")
        if volatility is not None:
            if volatility > self.VOLATILITY_HIGH_THRESHOLD:
                # 高波动倾向熊市/震荡
                scores["bear"] += 0.5
                scores["range"] += 0.5
            elif volatility < self.VOLATILITY_LOW_THRESHOLD:
                # 低波动倾向牛市/震荡
                scores["bull"] += 0.5
                scores["range"] += 0.5
            else:
                scores["range"] += 1.0

        # 信号 4: 价格相对均线位置
        price = market_data.get("price")
        ma20 = market_data.get("ma20")
        if price is not None and ma20 is not None and ma20 > 0:
            deviation = (price - ma20) / ma20
            if deviation > 0.02:
                scores["bull"] += 1.0
            elif deviation < -0.02:
                scores["bear"] += 1.0
            else:
                scores["range"] += 1.0

        # 无有效信号时默认震荡
        if sum(scores.values()) == 0:
            logger.warning("No valid market signals provided, defaulting to 'range'")
            return "range"

        regime = max(scores, key=scores.get)
        logger.info(
            f"Market regime detected: {regime} "
            f"(scores: bull={scores['bull']:.1f}, bear={scores['bear']:.1f}, range={scores['range']:.1f})"
        )
        return regime

    def get_regime_weights(self, regime: str) -> Dict[str, float]:
        """
        返回给定市场状态下各模型的推荐权重。

        Args:
            regime: "bull", "bear", 或 "range"

        Returns:
            模型名称到权重的映射字典
        """
        if regime not in self.REGIME_WEIGHTS:
            logger.warning(f"Unknown regime '{regime}', falling back to 'range'")
            regime = "range"
        return self.REGIME_WEIGHTS[regime].copy()


def detect_regime_from_kline(kline_data: List[Dict]) -> str:
    """
    从近期K线数据中检测市场状态。

    规则:
        - 近20日收益率 > kline_bull_return 且 当前价格 > MA20 -> bull
        - 近20日收益率 < kline_bear_return 且 当前价格 < MA20 -> bear
        - 其他 -> range

    Args:
        kline_data: K线数据列表，每条记录需包含 "close" 字段，
                    按时间升序排列。至少需要 20 条数据。

    Returns:
        "bull", "bear", 或 "range"
    """
    if not kline_data or len(kline_data) < 20:
        logger.warning(
            f"Insufficient kline data ({len(kline_data) if kline_data else 0} records), "
            "need at least 20. Defaulting to 'range'"
        )
        return "range"

    recent_data = kline_data[-20:]

    try:
        closes = [float(d["close"]) for d in recent_data]
    except (KeyError, TypeError, ValueError) as e:
        logger.error(f"Failed to parse kline close prices: {e}")
        return "range"

    if closes[0] == 0:
        logger.warning("First close price is 0, cannot compute return. Defaulting to 'range'")
        return "range"

    # 20日收益率
    return_20d = (closes[-1] - closes[0]) / closes[0]

    # MA20
    ma20 = np.mean(closes)
    current_price = closes[-1]

    logger.info(
        f"Kline regime signals: 20d_return={return_20d:.4f}, "
        f"price={current_price:.2f}, ma20={ma20:.2f}"
    )

    # 从配置读取 kline 阈值
    detection = _load_regime_detection()
    kline_bull_return = detection.get(
        "kline_bull_return", _DEFAULT_REGIME_DETECTION["kline_bull_return"]
    )
    kline_bear_return = detection.get(
        "kline_bear_return", _DEFAULT_REGIME_DETECTION["kline_bear_return"]
    )

    if return_20d > kline_bull_return and current_price > ma20:
        return "bull"
    elif return_20d < kline_bear_return and current_price < ma20:
        return "bear"
    else:
        return "range"


def calculate_dynamic_weights(
    model_accuracies: Dict[str, float],
    temperature: Optional[float] = None,
    regime: Optional[str] = None,
) -> Dict[str, float]:
    """
    基于模型历史准确率动态计算权重，使用 softmax(accuracy / temperature)。

    若无有效准确率数据，则回退到基于市场状态的固定权重。

    Args:
        model_accuracies: 模型名称到准确率的映射 (0~1)
        temperature: softmax 温度参数，值越大权重越均匀。
                     若为 None 则从配置读取。
        regime: 可选的市场状态，用于 fallback

    Returns:
        模型名称到归一化权重的映射
    """
    # 若未显式传入 temperature，从配置读取
    if temperature is None:
        temperature = _load_softmax_temperature()

    if not model_accuracies:
        logger.info("No model accuracy data, falling back to regime-based weights")
        detector = get_market_regime_detector()
        return detector.get_regime_weights(regime or "range")

    # 过滤有效准确率
    valid = {k: v for k, v in model_accuracies.items() if isinstance(v, (int, float)) and v > 0}

    if not valid:
        logger.info("No valid accuracy values, falling back to regime-based weights")
        detector = get_market_regime_detector()
        return detector.get_regime_weights(regime or "range")

    # softmax(accuracy / temperature)
    names = list(valid.keys())
    accuracies = np.array([valid[n] for n in names], dtype=np.float64)

    if temperature <= 0:
        default_temp = _load_softmax_temperature()
        logger.warning(f"Invalid temperature {temperature}, using config default {default_temp}")
        temperature = default_temp

    logits = accuracies / temperature
    # 数值稳定性: 减去最大值
    logits -= np.max(logits)
    exp_logits = np.exp(logits)
    weights = exp_logits / np.sum(exp_logits)

    result = {name: round(float(w), 4) for name, w in zip(names, weights)}
    logger.info(f"Dynamic weights (temperature={temperature}): {result}")
    return result


# ---------------------------------------------------------------------------
# 模块级别导出 (便于 from .market_regime import REGIME_WEIGHTS)
# ---------------------------------------------------------------------------
REGIME_WEIGHTS = _load_regime_weights()


# ---------------------------------------------------------------------------
# 全局单例
# ---------------------------------------------------------------------------

_detector_instance: Optional[MarketRegimeDetector] = None


def get_market_regime_detector() -> MarketRegimeDetector:
    """返回 MarketRegimeDetector 的全局单例。"""
    global _detector_instance
    if _detector_instance is None:
        _detector_instance = MarketRegimeDetector()
        logger.info("MarketRegimeDetector singleton initialized")
    return _detector_instance
