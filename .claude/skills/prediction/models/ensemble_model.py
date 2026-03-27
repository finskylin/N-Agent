"""
多模型融合预测器 — Meta-Learner Stacking + 市场状态感知 + 动态权重 v3

升级内容:
- Level-0: XGBoost, LightGBM, LSTM, Sentiment 各自输出概率
- Level-1: XGBClassifier Meta-Learner (替换 LogisticRegression, 捕捉非线性融合)
           输入: 12维 OOF 概率 + 6维辅助特征 = 18维
           8折 TimeSeriesSplit
- 市场状态感知 (牛市/熊市/震荡) 动态调整权重
- softmax(accuracy / temperature) 动态权重计算
- K-fold out-of-fold predictions 训练 Meta-Learner
"""
from typing import Dict, Any, List, Optional, Tuple
import numpy as np
from loguru import logger
from dataclasses import dataclass, asdict
from pathlib import Path
import json
import threading

from .xgboost_model import get_xgboost_predictor, PredictionResult
from .lightgbm_model import get_lightgbm_predictor, LGBMPredictionResult
from .lstm_model import get_lstm_predictor, LSTMPredictionResult
from .market_regime import (
    detect_regime_from_kline,
    calculate_dynamic_weights,
    REGIME_WEIGHTS,
)
from ..prediction_config import get_prediction_config

try:
    from sklearn.linear_model import LogisticRegression
    from sklearn.model_selection import TimeSeriesSplit
    from sklearn.preprocessing import StandardScaler
    from sklearn.metrics import accuracy_score, log_loss
    import joblib
    SKLEARN_AVAILABLE = True
except ImportError:
    SKLEARN_AVAILABLE = False
    logger.warning("sklearn not installed, Meta-Learner unavailable")

try:
    import xgboost as xgb_lib
    XGB_META_AVAILABLE = True
except ImportError:
    XGB_META_AVAILABLE = False
    logger.warning("xgboost not installed, XGBClassifier Meta-Learner unavailable")


# 模型文件目录
_MODEL_DIR = Path(__file__).parent.parent / "training" / "models"
_MODEL_DIR.mkdir(parents=True, exist_ok=True)

_META_LEARNER_FILE = _MODEL_DIR / "meta_learner.pkl"
_META_SCALER_FILE = _MODEL_DIR / "meta_scaler.pkl"
_META_INFO_FILE = _MODEL_DIR / "meta_learner_info.json"
_ACCURACY_HISTORY_FILE = _MODEL_DIR / "model_accuracy_history.json"


@dataclass
class EnsemblePredictionResult:
    """多模型融合预测结果"""
    direction: str
    probability: float
    confidence: str
    magnitude: str
    key_factors: List[Dict[str, Any]]
    model_predictions: Dict[str, Dict[str, Any]]
    model_weights: Dict[str, float]
    market_regime: str = "unknown"
    meta_learner_used: bool = False
    method: str = "ml_ensemble_v2"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class MetaLearner:
    """
    Level-1 Meta-Learner: XGBClassifier (升级自 LogisticRegression)
    输入: Level-0 模型的概率输出 (4 models x 3 classes = 12 维)
          + 辅助特征 (市场状态等) = 最多 18 维
    输出: 最终三分类概率

    XGBClassifier 比 LogisticRegression 更能捕捉模型间的非线性融合关系。
    """

    def __init__(self):
        self.model = None
        self.scaler = None
        self._trained = False
        self._load()

    def _load(self):
        """加载已训练的 Meta-Learner"""
        if _META_LEARNER_FILE.exists() and _META_SCALER_FILE.exists():
            try:
                self.model = joblib.load(str(_META_LEARNER_FILE))
                self.scaler = joblib.load(str(_META_SCALER_FILE))
                self._trained = True
                logger.info("Loaded Meta-Learner from disk")
            except Exception as e:
                logger.warning(f"Failed to load Meta-Learner: {e}")
                self._trained = False

    def _build_xgb_meta(self) -> Any:
        """构建 XGBClassifier Meta-Learner"""
        cfg = get_prediction_config()
        xgb_meta_hp = cfg.stacking.get("model_hyperparams", {}).get("meta_learner_xgb", {})
        return xgb_lib.XGBClassifier(
            n_estimators=xgb_meta_hp.get("n_estimators", 50),
            max_depth=xgb_meta_hp.get("max_depth", 4),
            learning_rate=xgb_meta_hp.get("learning_rate", 0.05),
            subsample=xgb_meta_hp.get("subsample", 0.8),
            colsample_bytree=xgb_meta_hp.get("colsample_bytree", 0.8),
            objective="multi:softprob",
            num_class=3,
            eval_metric="mlogloss",
            use_label_encoder=False,
            random_state=xgb_meta_hp.get("random_state", 42),
            n_jobs=-1,
            tree_method="hist",
        )

    def train(
        self,
        oof_predictions: np.ndarray,
        labels: np.ndarray,
        n_splits: int = None,
        aux_features: np.ndarray = None,
    ) -> Dict[str, Any]:
        """
        训练 Meta-Learner (优先 XGBClassifier, 回退 LogisticRegression)

        Args:
            oof_predictions: (N, 12) — 4 models x 3 classes 的 out-of-fold 概率
            labels: (N,) — 真实标签 (0/1/2)
            n_splits: TimeSeriesSplit 折数 (None 则从配置读取)
            aux_features: (N, K) — 可选辅助特征 (市场状态等)

        Returns:
            训练指标
        """
        if not SKLEARN_AVAILABLE:
            raise RuntimeError("sklearn required for Meta-Learner training")

        cfg = get_prediction_config()
        meta_cfg = cfg.stacking.get("meta_learner", {})
        if n_splits is None:
            n_splits = cfg.stacking.get("model_hyperparams", {}).get(
                "meta_learner_xgb", {}
            ).get("cv_folds", meta_cfg.get("n_splits", 8))

        # 拼接辅助特征 (如果有)
        if aux_features is not None and aux_features.shape[0] == oof_predictions.shape[0]:
            X = np.hstack([oof_predictions, aux_features])
        else:
            X = oof_predictions

        # 清洗数据
        mask = np.isfinite(X).all(axis=1) & np.isfinite(labels)
        X = X[mask]
        labels = labels[mask]

        if len(labels) < 100:
            raise ValueError(f"Insufficient data for Meta-Learner: {len(labels)} < 100")

        self.scaler = StandardScaler()
        X_scaled = self.scaler.fit_transform(X)

        # TimeSeriesSplit 训练
        tscv = TimeSeriesSplit(n_splits=n_splits)
        val_scores = []
        use_xgb = XGB_META_AVAILABLE

        for fold, (train_idx, val_idx) in enumerate(tscv.split(X_scaled)):
            X_tr, X_va = X_scaled[train_idx], X_scaled[val_idx]
            y_tr, y_va = labels[train_idx], labels[val_idx]

            if use_xgb:
                fold_model = self._build_xgb_meta()
                fold_model.fit(
                    X_tr, y_tr,
                    eval_set=[(X_va, y_va)],
                    verbose=False,
                )
            else:
                fold_model = LogisticRegression(
                    C=meta_cfg.get("C", 1.0),
                    max_iter=meta_cfg.get("max_iter", 1000),
                    multi_class="multinomial",
                    solver=meta_cfg.get("solver", "lbfgs"),
                    class_weight="balanced",
                )
                fold_model.fit(X_tr, y_tr)

            y_pred = fold_model.predict(X_va)
            acc = accuracy_score(y_va, y_pred)
            val_scores.append(acc)
            logger.info(f"Meta-Learner fold {fold+1}/{n_splits}: accuracy={acc:.4f}")

        # 使用全部数据训练最终模型
        if use_xgb:
            self.model = self._build_xgb_meta()
            self.model.fit(X_scaled, labels, verbose=False)
        else:
            self.model = LogisticRegression(
                C=meta_cfg.get("C", 1.0),
                max_iter=meta_cfg.get("max_iter", 1000),
                multi_class="multinomial",
                solver=meta_cfg.get("solver", "lbfgs"),
                class_weight="balanced",
            )
            self.model.fit(X_scaled, labels)

        self._trained = True

        # 保存
        joblib.dump(self.model, str(_META_LEARNER_FILE))
        joblib.dump(self.scaler, str(_META_SCALER_FILE))

        metrics = {
            "n_samples": int(len(labels)),
            "n_features": int(X.shape[1]),
            "meta_learner_type": "XGBClassifier" if use_xgb else "LogisticRegression",
            "mean_cv_accuracy": float(np.mean(val_scores)),
            "std_cv_accuracy": float(np.std(val_scores)),
            "fold_accuracies": [float(s) for s in val_scores],
            "n_splits": n_splits,
        }

        with open(str(_META_INFO_FILE), "w") as f:
            json.dump(metrics, f, indent=2)

        logger.info(
            f"Meta-Learner ({metrics['meta_learner_type']}) trained: "
            f"CV accuracy={np.mean(val_scores):.4f} (+/- {np.std(val_scores):.4f})"
        )
        return metrics

    def predict_proba(self, model_probas: np.ndarray) -> np.ndarray:
        """
        Meta-Learner 预测

        Args:
            model_probas: (1, 12~18) — 4 models x 3 classes 概率拼接 [+ 辅助特征]

        Returns:
            (3,) — 最终三分类概率 [DOWN, NEUTRAL, UP]
        """
        if not self._trained or self.model is None:
            return None

        model_probas = np.nan_to_num(model_probas, nan=1/3)
        X_scaled = self.scaler.transform(model_probas.reshape(1, -1))
        return self.model.predict_proba(X_scaled)[0]

    @property
    def is_trained(self) -> bool:
        return self._trained


class AccuracyTracker:
    """跟踪各模型近 N 日的预测准确率，用于动态权重计算"""

    def __init__(self, history_file: Path = _ACCURACY_HISTORY_FILE, window: int = None):
        self.history_file = history_file
        if window is None:
            cfg = get_prediction_config()
            window = cfg.stacking.get("accuracy_tracker", {}).get("window", 20)
        self.window = window
        self._history = self._load()

    def _load(self) -> Dict[str, List[float]]:
        if self.history_file.exists():
            try:
                with open(str(self.history_file), "r") as f:
                    return json.load(f)
            except Exception:
                pass
        return {"xgboost": [], "lightgbm": [], "lstm": [], "sentiment": []}

    def _save(self):
        with open(str(self.history_file), "w") as f:
            json.dump(self._history, f, indent=2)

    def record(self, model_name: str, correct: bool):
        """记录一次预测的对错"""
        if model_name not in self._history:
            self._history[model_name] = []
        self._history[model_name].append(1.0 if correct else 0.0)
        # 保留最近 window 条
        self._history[model_name] = self._history[model_name][-self.window:]
        self._save()

    def get_accuracies(self) -> Dict[str, float]:
        """获取各模型近 window 日准确率"""
        cfg = get_prediction_config()
        min_records = cfg.stacking.get("accuracy_tracker", {}).get("min_records", 5)
        result = {}
        for model_name, records in self._history.items():
            if len(records) >= min_records:
                result[model_name] = float(np.mean(records))
            else:
                result[model_name] = 0.5  # 数据不足时使用默认值
        return result

    def has_sufficient_data(self) -> bool:
        """是否有足够的历史数据计算动态权重"""
        cfg = get_prediction_config()
        min_records = cfg.stacking.get("accuracy_tracker", {}).get("min_records", 5)
        return all(
            len(records) >= min_records
            for records in self._history.values()
            if records  # 跳过空列表
        )


class ModelEnsemble:
    """
    多模型融合预测器 v2 — Meta-Learner Stacking + 市场状态感知

    融合策略优先级:
    1. Meta-Learner (如果已训练)
    2. 市场状态感知 + 动态权重
    3. 基于历史准确率的 softmax 权重
    4. 降级: 静态默认权重
    """

    _FALLBACK_WEIGHTS = {
        "xgboost": 0.30,
        "lightgbm": 0.30,
        "lstm": 0.20,
        "sentiment": 0.20,
    }

    @property
    def DEFAULT_WEIGHTS(self) -> Dict[str, float]:
        """Read default weights from config with fallback."""
        cfg = get_prediction_config()
        return cfg.stacking.get("default_weights", self._FALLBACK_WEIGHTS)

    def __init__(self):
        self.meta_learner = MetaLearner()
        self.accuracy_tracker = AccuracyTracker()
        self._lock = threading.Lock()

    def predict(
        self,
        tech: Dict[str, Any],
        money: Dict[str, Any],
        valuation: Dict[str, Any],
        kline_data: List[Dict[str, Any]] = None,
        sentiment_score: float = 0.5,
        horizon: str = "1w",
        market_bias: str = "neutral",
        override_weights: Optional[Dict[str, float]] = None,
        microstructure: Optional[Dict[str, Any]] = None,
    ) -> EnsemblePredictionResult:
        """
        多模型融合预测 v2 — Meta-Learner + 市场状态 + 动态权重

        策略:
        1. 收集各模型概率输出
        2. 如果有 override_weights → 直接加权融合（跳过 meta-learner）
        3. 否则尝试 Meta-Learner Stacking
        4. 回退到市场状态感知 + 动态权重

        market_bias: LLM 根据上下文传入的市场偏向 (neutral/bullish/bearish)，
                     bullish/bearish 会覆盖从 kline 检测到的 regime
        override_weights: LLM 传入的模型权重覆盖，如 {xgboost: 0.5, lightgbm: 0.3, ...}
        """
        predictions = {}
        model_probas = {}
        key_factors = []

        ms = microstructure or {}

        # 1. XGBoost
        try:
            xgb_pred = get_xgboost_predictor()
            xgb_result = xgb_pred.predict(tech, money, valuation, horizon, microstructure=ms)
            predictions["xgboost"] = {
                "direction": xgb_result.direction,
                "probability": xgb_result.probability,
                "predicted_return": xgb_result.predicted_return,
                "method": xgb_result.method,
            }
            key_factors = xgb_result.key_factors
            # 提取三分类概率
            if hasattr(xgb_result, 'class_probabilities') and xgb_result.class_probabilities:
                model_probas["xgboost"] = np.array(xgb_result.class_probabilities)
            else:
                model_probas["xgboost"] = self._direction_to_proba(
                    xgb_result.direction, xgb_result.probability
                )
        except Exception as e:
            logger.warning(f"XGBoost prediction failed: {e}")

        # 2. LightGBM
        try:
            lgb_pred = get_lightgbm_predictor()
            lgb_result = lgb_pred.predict(tech, money, valuation, horizon, microstructure=ms)
            predictions["lightgbm"] = {
                "direction": lgb_result.direction,
                "probability": lgb_result.probability,
                "predicted_return": lgb_result.predicted_return,
                "method": lgb_result.method,
            }
            if hasattr(lgb_result, 'class_probabilities') and lgb_result.class_probabilities:
                model_probas["lightgbm"] = np.array(lgb_result.class_probabilities)
            else:
                model_probas["lightgbm"] = self._direction_to_proba(
                    lgb_result.direction, lgb_result.probability
                )
        except Exception as e:
            logger.warning(f"LightGBM prediction failed: {e}")

        # 3. LSTM
        if kline_data and len(kline_data) >= 20:
            try:
                lstm_pred = get_lstm_predictor()
                lstm_result = lstm_pred.predict(kline_data, horizon)
                predictions["lstm"] = {
                    "direction": lstm_result.direction,
                    "probability": lstm_result.probability,
                    "predicted_return": lstm_result.predicted_return,
                    "pattern": lstm_result.sequence_pattern,
                    "method": lstm_result.method,
                }
                model_probas["lstm"] = self._direction_to_proba(
                    lstm_result.direction, lstm_result.probability
                )
            except Exception as e:
                logger.warning(f"LSTM prediction failed: {e}")

        # 4. 情绪信号
        sentiment_proba = self._sentiment_to_proba(sentiment_score)
        sentiment_direction = "UP" if sentiment_score > 0.6 else (
            "DOWN" if sentiment_score < 0.4 else "NEUTRAL"
        )
        predictions["sentiment"] = {
            "direction": sentiment_direction,
            "probability": abs(sentiment_score - 0.5) * 2 + 0.5,
            "method": "sentiment_ml",
        }
        model_probas["sentiment"] = sentiment_proba

        # 至少需要 1 个 ML 模型成功
        ml_models = {"xgboost", "lightgbm", "lstm"}
        ml_success = ml_models & set(predictions.keys())
        if not ml_success:
            raise RuntimeError("All ML models failed -- cannot produce ensemble prediction")

        # 5. 市场状态检测（market_bias 优先于自动检测）
        if market_bias == "bullish":
            market_regime = "bull"
            logger.info("Ensemble: market_bias=bullish → regime=bull (LLM override)")
        elif market_bias == "bearish":
            market_regime = "bear"
            logger.info("Ensemble: market_bias=bearish → regime=bear (LLM override)")
        elif kline_data and len(kline_data) >= 20:
            market_regime = detect_regime_from_kline(kline_data)
            logger.info(f"Ensemble: auto-detected regime={market_regime}")
        else:
            market_regime = "range"
            logger.info("Ensemble: default regime=range (no kline data)")

        # 6. 融合策略选择
        meta_used = False
        if override_weights:
            # 策略 0: LLM 权重覆盖（直接加权，跳过 meta-learner）
            normalized = self._normalize_weights(override_weights, set(model_probas.keys()))
            result = self._weighted_ensemble_with_weights(model_probas, normalized, market_regime)
            logger.info(f"Ensemble: using LLM override_weights={normalized}")
        elif self.meta_learner.is_trained and len(model_probas) >= 3:
            # 策略 A: Meta-Learner Stacking
            result = self._meta_learner_predict(model_probas, market_regime)
            meta_used = True
            logger.info("Ensemble: using Meta-Learner Stacking")
        else:
            # 策略 B: 市场状态 + 动态权重
            result = self._weighted_ensemble(
                predictions, model_probas, market_regime
            )
            logger.info(f"Ensemble: using weighted fusion (regime={market_regime})")

        return EnsemblePredictionResult(
            direction=result["direction"],
            probability=result["probability"],
            confidence=result["confidence"],
            magnitude=result["magnitude"],
            key_factors=key_factors,
            model_predictions=predictions,
            model_weights=result.get("weights", {}),
            market_regime=market_regime,
            meta_learner_used=meta_used,
            method="meta_learner_stacking" if meta_used else "regime_weighted_ensemble",
        )

    @staticmethod
    def _normalize_weights(weights: Dict[str, float], available: set) -> Dict[str, float]:
        """归一化 override_weights，只保留可用模型的权重"""
        active = {k: v for k, v in weights.items() if k in available and v > 0}
        if not active:
            n = len(available)
            return {k: 1.0 / n for k in available}
        total = sum(active.values())
        return {k: v / total for k, v in active.items()}

    def _weighted_ensemble_with_weights(
        self,
        model_probas: Dict[str, np.ndarray],
        weights: Dict[str, float],
        market_regime: str,
    ) -> Dict[str, Any]:
        """使用指定权重直接加权融合"""
        fused_proba = np.zeros(3)
        for model_name, proba in model_probas.items():
            w = weights.get(model_name, 0)
            fused_proba += w * proba
        if fused_proba.sum() > 0:
            fused_proba = fused_proba / fused_proba.sum()
        else:
            fused_proba = np.array([1/3, 1/3, 1/3])
        result = self._proba_to_result(fused_proba, model_probas)
        result["weights"] = weights
        return result

    # ------------------------------------------------------------------ #
    #  策略 A: Meta-Learner Stacking
    # ------------------------------------------------------------------ #

    def _meta_learner_predict(
        self,
        model_probas: Dict[str, np.ndarray],
        market_regime: str,
    ) -> Dict[str, Any]:
        """使用 Meta-Learner 进行 Stacking 融合"""
        # 构建 Meta-Learner 输入: 拼接所有模型的 3-class 概率
        # 顺序: xgboost(3) + lightgbm(3) + lstm(3) + sentiment(3) = 12 维
        model_order = ["xgboost", "lightgbm", "lstm", "sentiment"]
        feature_vec = []
        for model_name in model_order:
            if model_name in model_probas:
                feature_vec.append(model_probas[model_name])
            else:
                # 缺失模型用均匀分布填充
                feature_vec.append(np.array([1/3, 1/3, 1/3]))

        meta_input = np.concatenate(feature_vec).reshape(1, -1)
        meta_proba = self.meta_learner.predict_proba(meta_input)

        if meta_proba is None:
            # Meta-Learner 失败, 降级到加权融合
            return self._weighted_ensemble(
                {}, model_probas, market_regime
            )

        # 市场状态微调: 在 Meta-Learner 输出上叠加 regime bias
        cfg = get_prediction_config()
        regime_bias_factor = cfg.stacking.get("regime_bias_factor", 0.1)
        regime_bias = self._get_regime_bias(market_regime)
        adjusted_proba = meta_proba * (1 + regime_bias * regime_bias_factor)
        adjusted_proba = adjusted_proba / adjusted_proba.sum()

        return self._proba_to_result(adjusted_proba, model_probas)

    def _get_regime_bias(self, regime: str) -> np.ndarray:
        """市场状态偏置: 轻微调整概率方向"""
        if regime == "bull":
            return np.array([-0.05, -0.05, 0.1])   # 偏向 UP
        elif regime == "bear":
            return np.array([0.1, -0.05, -0.05])    # 偏向 DOWN
        else:
            return np.array([0.0, 0.1, 0.0])         # 偏向 NEUTRAL

    # ------------------------------------------------------------------ #
    #  策略 B: 市场状态 + 动态权重
    # ------------------------------------------------------------------ #

    def _weighted_ensemble(
        self,
        predictions: Dict[str, Dict[str, Any]],
        model_probas: Dict[str, np.ndarray],
        market_regime: str,
    ) -> Dict[str, Any]:
        """市场状态感知 + 动态权重融合"""
        # 1. 基础权重: 根据市场状态选择
        if market_regime in REGIME_WEIGHTS:
            base_weights = REGIME_WEIGHTS[market_regime].copy()
        else:
            base_weights = self.DEFAULT_WEIGHTS.copy()

        # 2. 动态权重: 如果有足够的历史准确率数据
        if self.accuracy_tracker.has_sufficient_data():
            cfg = get_prediction_config()
            temperature = cfg.stacking.get("softmax_temperature", 2.0)
            regime_mix = cfg.stacking.get("dynamic_regime_mix", [0.6, 0.4])
            dynamic_ratio = regime_mix[0] if len(regime_mix) > 0 else 0.6
            regime_ratio = regime_mix[1] if len(regime_mix) > 1 else 0.4
            accuracies = self.accuracy_tracker.get_accuracies()
            dynamic_weights = calculate_dynamic_weights(
                accuracies, temperature=temperature
            )
            # 与 regime 权重混合
            final_weights = {}
            for model_name in base_weights:
                dw = dynamic_weights.get(model_name, 0.25)
                rw = base_weights.get(model_name, 0.25)
                final_weights[model_name] = dynamic_ratio * dw + regime_ratio * rw
        else:
            final_weights = base_weights

        # 3. 归一化权重 (仅包含可用模型)
        available_models = set(model_probas.keys())
        active_weights = {
            k: v for k, v in final_weights.items()
            if k in available_models
        }
        total_w = sum(active_weights.values())
        if total_w > 0:
            active_weights = {k: v / total_w for k, v in active_weights.items()}
        else:
            # 均分
            n = len(available_models)
            active_weights = {k: 1.0/n for k in available_models}

        # 4. 加权概率融合
        fused_proba = np.zeros(3)
        for model_name, proba in model_probas.items():
            w = active_weights.get(model_name, 0)
            fused_proba += w * proba

        # 归一化
        if fused_proba.sum() > 0:
            fused_proba = fused_proba / fused_proba.sum()
        else:
            fused_proba = np.array([1/3, 1/3, 1/3])

        result = self._proba_to_result(fused_proba, model_probas)
        result["weights"] = active_weights
        return result

    # ------------------------------------------------------------------ #
    #  概率 -> 结果转换
    # ------------------------------------------------------------------ #

    def _proba_to_result(
        self,
        proba: np.ndarray,
        model_probas: Dict[str, np.ndarray],
    ) -> Dict[str, Any]:
        """将三分类概率转换为预测结果"""
        # NaN 保护：将概率中的 NaN 替换为均匀分布 1/3
        proba = np.nan_to_num(proba, nan=1/3)
        if proba.sum() > 0:
            proba = proba / proba.sum()
        else:
            proba = np.array([1/3, 1/3, 1/3])

        directions = ["DOWN", "NEUTRAL", "UP"]
        class_idx = int(np.argmax(proba))
        direction = directions[class_idx]
        max_prob = float(proba[class_idx])

        # 置信度: 基于概率熵
        entropy = -np.sum(proba * np.log(proba + 1e-9))
        max_entropy = -np.log(1/3)
        confidence_score = 1 - entropy / max_entropy

        cfg = get_prediction_config()
        conf_thresholds = cfg.stacking.get("confidence_thresholds", {"high": 0.6, "medium": 0.3})
        high_threshold = conf_thresholds.get("high", 0.6)
        medium_threshold = conf_thresholds.get("medium", 0.3)

        if confidence_score >= high_threshold:
            confidence = "高"
        elif confidence_score >= medium_threshold:
            confidence = "中"
        else:
            confidence = "低"

        # 幅度估算
        up_prob = float(proba[2])
        down_prob = float(proba[0])
        predicted_return = (up_prob - down_prob) * 0.10  # 最大 +/-10%

        # NaN 保护：概率为 NaN 时视为中性（0%）
        if np.isnan(predicted_return):
            predicted_return = 0.0

        if predicted_return > 0.02:
            magnitude = f"+{predicted_return*100:.1f}%"
        elif predicted_return < -0.02:
            magnitude = f"{predicted_return*100:.1f}%"
        else:
            magnitude = f"{predicted_return*100:+.1f}%"

        # 一致性分析
        model_directions = []
        for name, p in model_probas.items():
            p = np.nan_to_num(p, nan=1/3)
            model_directions.append(directions[int(np.argmax(p))])

        agreement = sum(1 for d in model_directions if d == direction) / max(len(model_directions), 1)

        return {
            "direction": direction,
            "probability": round(max_prob, 4),
            "confidence": confidence,
            "magnitude": magnitude,
            "predicted_return": round(predicted_return * 100, 2),
            "model_agreement": round(agreement, 2),
            "class_probabilities": [round(float(p), 4) for p in proba],
            "weights": {},
        }

    # ------------------------------------------------------------------ #
    #  辅助: 方向 -> 概率
    # ------------------------------------------------------------------ #

    @staticmethod
    def _direction_to_proba(direction: str, probability: float) -> np.ndarray:
        """将方向 + 概率转为三分类概率分布"""
        cfg = get_prediction_config()
        prob_clip = cfg.stacking.get("probability_clip", {"min": 0.34, "max": 0.95})
        clip_min = prob_clip.get("min", 0.34)
        clip_max = prob_clip.get("max", 0.95)
        prob = max(clip_min, min(clip_max, probability))
        remaining = 1.0 - prob

        if direction == "UP":
            return np.array([remaining * 0.3, remaining * 0.7, prob])
        elif direction == "DOWN":
            return np.array([prob, remaining * 0.7, remaining * 0.3])
        else:
            return np.array([remaining * 0.5, prob, remaining * 0.5])

    @staticmethod
    def _sentiment_to_proba(score: float) -> np.ndarray:
        """将情绪分数 [0, 1] 转为三分类概率"""
        score = max(0.0, min(1.0, score))

        if score > 0.6:
            up = 0.3 + (score - 0.6) * 1.5
            down = 0.1
        elif score < 0.4:
            up = 0.1
            down = 0.3 + (0.4 - score) * 1.5
        else:
            up = 0.25
            down = 0.25

        neutral = max(0.1, 1.0 - up - down)
        total = up + down + neutral
        return np.array([down/total, neutral/total, up/total])

    # ------------------------------------------------------------------ #
    #  权重更新接口
    # ------------------------------------------------------------------ #

    def record_prediction_accuracy(
        self,
        model_name: str,
        predicted_direction: str,
        actual_direction: str,
    ):
        """记录模型预测准确性，用于动态权重更新"""
        correct = predicted_direction == actual_direction
        self.accuracy_tracker.record(model_name, correct)

    def train_meta_learner(
        self,
        oof_predictions: np.ndarray,
        labels: np.ndarray,
    ) -> Dict[str, Any]:
        """训练 Meta-Learner (通常在 train.py 中调用)"""
        return self.meta_learner.train(oof_predictions, labels)


# 全局实例
_ensemble: Optional[ModelEnsemble] = None
_init_lock = threading.Lock()


def get_model_ensemble() -> ModelEnsemble:
    global _ensemble
    if _ensemble is None:
        with _init_lock:
            if _ensemble is None:
                _ensemble = ModelEnsemble()
    return _ensemble


# 向后兼容
model_ensemble = None  # type: ignore
