"""
LightGBM 股票预测模型 — 宽度优化版 v3 (与 XGBoost 差异化)
基于轻量级梯度提升的股票走势预测

升级内容:
- 特征维度跟随 data_pipeline.FEATURE_COLUMNS 动态适配
- num_leaves=127, n_estimators=3000, learning_rate=0.003
- extra_trees=True 增强随机性
- feature_fraction=0.7
- TimeSeriesSplit 8折滚动验证
- early_stopping 150轮 + 样本权重时间衰减
"""
from typing import Dict, Any, List, Optional
from pathlib import Path
import numpy as np
from loguru import logger
from dataclasses import dataclass
import json
import threading

from ..prediction_config import get_prediction_config

try:
    import lightgbm as lgb
    from sklearn.preprocessing import StandardScaler
    from sklearn.model_selection import TimeSeriesSplit
    from sklearn.metrics import accuracy_score
    import joblib
    LIGHTGBM_AVAILABLE = True
except ImportError:
    LIGHTGBM_AVAILABLE = False
    logger.warning("lightgbm/sklearn/joblib not installed")


@dataclass
class LGBMPredictionResult:
    """LightGBM 预测结果"""
    direction: str
    probability: float
    confidence: str
    feature_importance: List[Dict[str, Any]]
    method: str = "lightgbm"
    predicted_return: float = 0.0
    class_probabilities: Optional[List[float]] = None


# 模型文件目录
_MODEL_DIR = Path(__file__).parent.parent / "training" / "models"
_MODEL_DIR.mkdir(parents=True, exist_ok=True)

_LGB_MODEL_FILE = _MODEL_DIR / "lightgbm_1w.pkl"
_LGB_SCALER_FILE = _MODEL_DIR / "lightgbm_scaler_1w.pkl"
_LGB_META_FILE = _MODEL_DIR / "lightgbm_meta_1w.json"
_TRAIN_LOCK = threading.Lock()


class LightGBMPredictor:
    """
    LightGBM 股票预测器 — 宽度优化版 (与 XGBoost 差异化)
    num_leaves=63 宽树 + feature_fraction 随机特征选择
    """

    # 51维特征列表 (与 data_pipeline.FEATURE_COLUMNS 一致)
    FEATURE_NAMES = [
        "ma5_ratio", "ma10_ratio", "ma20_ratio",
        "macd", "macd_signal", "macd_hist",
        "kdj_k", "kdj_d", "kdj_j",
        "rsi_14", "rsi_6",
        "amplitude", "turnover_rate",
        "volume_ratio",
        "price_change_5d", "price_change_10d",
        "pe_ttm_pctl", "pb_pctl", "roe_latest", "revenue_yoy",
        "profit_yoy", "gross_margin", "debt_ratio", "market_cap_log",
        "main_net_5d", "main_net_20d", "flow_stability",
        "north_bound_chg", "margin_balance_chg",
        "large_order_ratio", "super_large_pct",
        "atr_14", "volatility_20d", "volatility_ratio",
        "max_drawdown_20d", "skewness_20d",
        "obv_slope", "vwap_deviation", "price_volume_corr",
        "vol_breakout", "turnover_ma_ratio",
        "market_index_return", "market_breadth", "sector_rank",
        "sector_return_5d", "market_volatility", "risk_premium",
        "news_sentiment_score", "analyst_consensus",
        "search_trend_idx", "social_heat_idx",
    ]

    FEATURE_DISPLAY_NAMES = {
        "ma5_ratio": "均线趋势(5)", "ma10_ratio": "均线趋势(10)", "ma20_ratio": "均线趋势(20)",
        "macd": "MACD柱", "macd_signal": "MACD快线", "macd_hist": "MACD慢线",
        "kdj_k": "KDJ-K", "kdj_d": "KDJ-D", "kdj_j": "KDJ-J",
        "rsi_14": "RSI(14)", "rsi_6": "RSI(6)",
        "amplitude": "波动率", "turnover_rate": "换手率", "volume_ratio": "量比",
        "price_change_5d": "5日涨幅", "price_change_10d": "10日涨幅",
        "pe_ttm_pctl": "PE分位", "pb_pctl": "PB分位",
        "roe_latest": "ROE", "revenue_yoy": "营收同比",
        "profit_yoy": "净利同比", "gross_margin": "毛利率",
        "debt_ratio": "资产负债率", "market_cap_log": "市值",
        "main_net_5d": "5日主力净流入", "main_net_20d": "20日主力净流入",
        "flow_stability": "资金稳定性", "north_bound_chg": "北向资金",
        "margin_balance_chg": "融资余额变化", "large_order_ratio": "大单占比",
        "super_large_pct": "超大单净占比",
        "atr_14": "ATR(14)", "volatility_20d": "20日波动率",
        "volatility_ratio": "波动率比", "max_drawdown_20d": "20日最大回撤",
        "skewness_20d": "收益偏度",
        "obv_slope": "OBV斜率", "vwap_deviation": "VWAP偏离",
        "price_volume_corr": "量价相关", "vol_breakout": "量能突破",
        "turnover_ma_ratio": "换手率比",
        "market_index_return": "大盘涨跌", "market_breadth": "涨跌比",
        "sector_rank": "板块排名", "sector_return_5d": "板块涨幅",
        "market_volatility": "市场波动", "risk_premium": "风险溢价",
        "news_sentiment_score": "新闻情绪", "analyst_consensus": "分析师预期",
        "search_trend_idx": "搜索热度", "social_heat_idx": "社交热度",
    }

    def __init__(self, model_path: Optional[str] = None):
        self.model: Optional[lgb.LGBMClassifier] = None
        self.scaler: Optional[StandardScaler] = None
        self._trained = False
        self._train_metrics: Dict[str, Any] = {}

        if not LIGHTGBM_AVAILABLE:
            logger.error("LightGBM unavailable — prediction will fail")
            return

        model_file = Path(model_path) if model_path else _LGB_MODEL_FILE
        scaler_file = _LGB_SCALER_FILE

        if model_file.exists() and scaler_file.exists():
            self._load(model_file, scaler_file)
        else:
            logger.info("No pre-trained LightGBM model found, will auto-train on first predict")

    def _load(self, model_file: Path, scaler_file: Path):
        try:
            self.model = joblib.load(str(model_file))
            self.scaler = joblib.load(str(scaler_file))
            self._trained = True
            if _LGB_META_FILE.exists():
                with open(_LGB_META_FILE) as f:
                    self._train_metrics = json.load(f)
            logger.info(f"Loaded LightGBM model from {model_file}")
        except Exception as e:
            logger.error(f"Failed to load LightGBM model: {e}")
            self.model = None
            self.scaler = None
            self._trained = False

    def _save(self, model: Any, scaler: Any, metrics: Dict):
        joblib.dump(model, str(_LGB_MODEL_FILE))
        joblib.dump(scaler, str(_LGB_SCALER_FILE))
        with open(_LGB_META_FILE, "w") as f:
            json.dump(metrics, f, indent=2, default=str)
        logger.info(f"Saved LightGBM model to {_LGB_MODEL_FILE}")

    # ------------------------------------------------------------------ #
    #  自动训练 — 宽度优化版
    # ------------------------------------------------------------------ #

    def _auto_train(self):
        """LightGBM 宽度模型训练 — 与 XGBoost 差异化"""
        if not LIGHTGBM_AVAILABLE:
            raise RuntimeError("lightgbm/sklearn not installed")

        with _TRAIN_LOCK:
            if self._trained:
                return

            from ..training.data_pipeline import TrainingDataPipeline
            pipeline = TrainingDataPipeline()

            X, y = pipeline.load_dataset(horizon="1w", kind="tabular")
            if X.size == 0:
                logger.info("Building tabular dataset for LightGBM (200+ stocks, 2000 days)...")
                X, y = pipeline.build_dataset(horizon="1w", days=2000, save=True)

            scaler = StandardScaler()
            X_scaled = scaler.fit_transform(X)

            # 从配置加载超参 — 宽度模型与 XGBoost 差异化
            cfg = get_prediction_config()
            lgb_hyperparams = cfg.stacking.get("model_hyperparams", {}).get("lightgbm", {})
            cv_folds = lgb_hyperparams.get("cv_folds", 8)
            early_stopping_rounds = lgb_hyperparams.get("early_stopping_rounds", 150)

            model_params = {
                "n_estimators": lgb_hyperparams.get("n_estimators", 3000),
                "num_leaves": lgb_hyperparams.get("num_leaves", 127),
                "max_depth": lgb_hyperparams.get("max_depth", -1),
                "learning_rate": lgb_hyperparams.get("learning_rate", 0.003),
                "min_data_in_leaf": lgb_hyperparams.get("min_data_in_leaf", 10),
                "feature_fraction": lgb_hyperparams.get("feature_fraction", 0.7),
                "bagging_fraction": lgb_hyperparams.get("bagging_fraction", 0.7),
                "bagging_freq": lgb_hyperparams.get("bagging_freq", 5),
                "lambda_l1": lgb_hyperparams.get("lambda_l1", 0.1),
                "lambda_l2": lgb_hyperparams.get("lambda_l2", 0.5),
                "min_gain_to_split": lgb_hyperparams.get("min_gain_to_split", 0.02),
                "extra_trees": lgb_hyperparams.get("extra_trees", True),  # 增强随机性，与XGB差异化
                "random_state": lgb_hyperparams.get("random_state", 42),
                # Non-configurable model params
                "objective": "multiclass",
                "num_class": 3,
                "metric": "multi_logloss",
                "n_jobs": -1,
                "verbose": -1,
            }

            # 从配置加载样本权重衰减率
            decay_rate = cfg.stacking.get("sample_weight_decay", {}).get("lightgbm", 0.001)

            # TimeSeriesSplit 8 折滚动验证
            tscv = TimeSeriesSplit(n_splits=cv_folds)
            fold_accuracies = []
            best_model = None
            best_acc = 0.0

            for fold, (train_idx, val_idx) in enumerate(tscv.split(X_scaled)):
                X_train, X_val = X_scaled[train_idx], X_scaled[val_idx]
                y_train, y_val = y[train_idx], y[val_idx]

                # 样本权重 — 时间指数衰减
                n_train = len(train_idx)
                sample_weights = np.exp(decay_rate * np.arange(n_train))
                sample_weights /= sample_weights.sum() / n_train

                fold_model = lgb.LGBMClassifier(**model_params)
                fold_model.fit(
                    X_train, y_train,
                    sample_weight=sample_weights,
                    eval_set=[(X_val, y_val)],
                    callbacks=[
                        lgb.early_stopping(stopping_rounds=early_stopping_rounds, verbose=False),
                        lgb.log_evaluation(period=0),
                    ],
                )

                y_pred = fold_model.predict(X_val)
                fold_acc = accuracy_score(y_val, y_pred)
                fold_accuracies.append(fold_acc)
                logger.info(f"LightGBM Fold {fold+1}/{cv_folds} — accuracy: {fold_acc:.4f}")

                if fold_acc > best_acc:
                    best_acc = fold_acc
                    best_model = fold_model

            # 最终全量训练
            split_idx = int(len(X_scaled) * 0.9)
            X_train_final, X_val_final = X_scaled[:split_idx], X_scaled[split_idx:]
            y_train_final, y_val_final = y[:split_idx], y[split_idx:]

            n_final = len(X_train_final)
            final_weights = np.exp(decay_rate * np.arange(n_final))
            final_weights /= final_weights.sum() / n_final

            final_model = lgb.LGBMClassifier(**model_params)
            final_model.fit(
                X_train_final, y_train_final,
                sample_weight=final_weights,
                eval_set=[(X_val_final, y_val_final)],
                callbacks=[
                    lgb.early_stopping(stopping_rounds=early_stopping_rounds, verbose=False),
                    lgb.log_evaluation(period=0),
                ],
            )

            final_acc = accuracy_score(y_val_final, final_model.predict(X_val_final))

            metrics = {
                "model": "LightGBM_v3_wide",
                "n_features": int(X.shape[1]),
                "n_samples": int(X.shape[0]),
                "cv_accuracies": [round(a, 4) for a in fold_accuracies],
                "cv_mean_accuracy": round(float(np.mean(fold_accuracies)), 4),
                "cv_std_accuracy": round(float(np.std(fold_accuracies)), 4),
                "final_accuracy": round(float(final_acc), 4),
                "best_iteration": int(getattr(final_model, 'best_iteration_', 1500)),
                "hyperparams": {
                    k: v for k, v in model_params.items()
                    if k not in ("objective", "num_class", "metric", "n_jobs", "verbose")
                },
            }

            logger.info(
                f"LightGBM training complete — "
                f"CV mean: {metrics['cv_mean_accuracy']:.4f} +/- {metrics['cv_std_accuracy']:.4f}, "
                f"Final: {final_acc:.4f}"
            )

            self._save(final_model, scaler, metrics)
            self.model = final_model
            self.scaler = scaler
            self._trained = True
            self._train_metrics = metrics

    # ------------------------------------------------------------------ #
    #  特征提取
    # ------------------------------------------------------------------ #

    def extract_features(
        self,
        tech: Dict[str, Any],
        money: Dict[str, Any],
        valuation: Dict[str, Any],
        microstructure: Optional[Dict[str, Any]] = None,
    ) -> np.ndarray:
        """
        从在线 Skill 数据提取 55 维特征（与 xgboost_model 保持一致）

        真实数据优先级：
        - 技术面: technical_indicators skill
        - 基本面: financial_report skill（经 ensemble.py _build_valuation_from_financial 处理）
        - 资金面: money_flow skill（经 ensemble.py _build_money_flow_from_skill 处理）
        - 微观结构: bid_ask_depth + intraday_tick
        """
        ms = microstructure or {}
        indicators = tech.get("indicators", {})
        ma = indicators.get("ma", {})
        macd_ind = indicators.get("macd", {})
        kdj = indicators.get("kdj", {})
        rsi = indicators.get("rsi", {})

        summary = tech.get("summary", {})
        money_summary = money.get("summary", {})
        percentiles = valuation.get("percentiles", {})
        current_val = valuation.get("current", {})
        fund_flow = money.get("fund_flow", {})

        current_price = summary.get("current_price", 0) or 1

        roe_real = current_val.get("roe", None)
        revenue_yoy_real = current_val.get("revenue_yoy", None)
        profit_yoy_real = current_val.get("profit_yoy", None)
        gross_margin_real = current_val.get("gross_margin", None)
        debt_ratio_real = current_val.get("debt_ratio", None)
        total_mv_real = current_val.get("total_mv", 0)

        main_net_5d = money_summary.get("main_net_inflow_5d", 0)
        main_net_20d = money_summary.get("main_net_inflow_20d", 0)
        flow_stability = money_summary.get("flow_stability", 0)
        north_bound = money_summary.get("north_bound_change", 0)
        margin_chg = money_summary.get("margin_balance_change", 0)
        large_order = fund_flow.get("large_order_ratio", 0)
        super_large = fund_flow.get("super_large_pct", 0)

        features = [
            (ma.get("ma5", current_price) / current_price - 1) if current_price else 0,
            (ma.get("ma10", current_price) / current_price - 1) if current_price else 0,
            (ma.get("ma20", current_price) / current_price - 1) if current_price else 0,
            macd_ind.get("macd", macd_ind.get("MACD", 0)) / 100,
            macd_ind.get("dif", macd_ind.get("DIF", 0)) / 100,
            macd_ind.get("dea", macd_ind.get("DEA", 0)) / 100,
            kdj.get("k", kdj.get("K", 50)) / 100,
            kdj.get("d", kdj.get("D", 50)) / 100,
            kdj.get("j", kdj.get("J", 50)) / 100,
            rsi.get("rsi14", rsi.get("RSI14", 50)) / 100,
            rsi.get("rsi6", rsi.get("RSI6", 50)) / 100,
            summary.get("amplitude", 0) / 10,
            summary.get("turnover_rate", 0) / 10,
            summary.get("volume_ratio", 1),
            summary.get("change_5d", summary.get("pct_chg_5d", 0)) / 10,
            summary.get("change_10d", summary.get("pct_chg_10d", 0)) / 10,
            # 基本面 8 维（真实财务数据）
            percentiles.get("pe_percentile", 50) / 100,
            percentiles.get("pb_percentile", 50) / 100,
            (roe_real if roe_real is not None else 10) / 100,
            (revenue_yoy_real if revenue_yoy_real is not None else 0) / 100,
            (profit_yoy_real if profit_yoy_real is not None else 0) / 100,
            (gross_margin_real if gross_margin_real is not None else 30) / 100,
            (debt_ratio_real if debt_ratio_real is not None else 50) / 100,
            np.log1p(total_mv_real) / 30,
            # 资金面 7 维（真实 money_flow 数据）
            main_net_5d / 1e8,
            main_net_20d / 1e8,
            flow_stability,
            north_bound / 1e8,
            margin_chg / 1e8,
            large_order,
            super_large,
            summary.get("atr_14", 0) / current_price if current_price else 0,
            summary.get("volatility_20d", 0),
            summary.get("volatility_ratio", 1),
            summary.get("max_drawdown_20d", 0),
            summary.get("skewness_20d", 0),
            summary.get("obv_slope", 0),
            summary.get("vwap_deviation", 0),
            summary.get("price_volume_corr", 0),
            summary.get("vol_breakout", 1),
            summary.get("turnover_ma_ratio", 1),
            summary.get("market_index_return", 0) / 10,
            summary.get("market_breadth", 0.5),
            summary.get("sector_rank", 0.5),
            summary.get("sector_return_5d", 0) / 10,
            summary.get("market_volatility", 0),
            summary.get("risk_premium", 0),
            summary.get("news_sentiment", 0.5),
            summary.get("analyst_consensus", 0.5),
            summary.get("search_trend", 0.5),
            summary.get("social_heat", 0.5),
        ]

        return np.array(features, dtype=np.float32).reshape(1, -1)

    # ------------------------------------------------------------------ #
    #  推理
    # ------------------------------------------------------------------ #

    def predict(
        self,
        tech: Dict[str, Any],
        money: Dict[str, Any],
        valuation: Dict[str, Any],
        horizon: str = "1w",
        microstructure: Optional[Dict[str, Any]] = None,
    ) -> LGBMPredictionResult:
        """预测股票走势 — 纯 ML 推理"""
        if not LIGHTGBM_AVAILABLE:
            raise RuntimeError("lightgbm not installed, cannot predict")

        if not self._trained:
            self._auto_train()

        features = self.extract_features(tech, money, valuation, microstructure=microstructure)
        features = np.nan_to_num(features, nan=0.0, posinf=1.0, neginf=-1.0)
        features_scaled = self.scaler.transform(features)

        proba = self.model.predict_proba(features_scaled)[0]
        class_idx = int(np.argmax(proba))
        max_prob = float(proba[class_idx])

        directions = ["DOWN", "NEUTRAL", "UP"]
        direction = directions[class_idx]

        # 置信度 — 基于概率分布的熵
        entropy = -np.sum(proba * np.log(proba + 1e-9))
        max_entropy = -np.log(1/3)
        confidence_score = 1 - entropy / max_entropy

        cfg = get_prediction_config()
        conf_thresholds = cfg.stacking.get("confidence_thresholds", {})
        high_threshold = conf_thresholds.get("high", 0.6)
        medium_threshold = conf_thresholds.get("medium", 0.3)
        if confidence_score >= high_threshold:
            confidence = "高"
        elif confidence_score >= medium_threshold:
            confidence = "中"
        else:
            confidence = "低"

        feature_importance = self._get_feature_importance(features_scaled[0])

        # 预测收益率
        up_prob = float(proba[2])
        down_prob = float(proba[0])
        magnitude = cfg.stacking.get("return_magnitude_factor", {}).get("lightgbm", 0.15)
        predicted_return = (up_prob - down_prob) * magnitude

        return LGBMPredictionResult(
            direction=direction,
            probability=max_prob,
            confidence=confidence,
            feature_importance=feature_importance,
            method="lightgbm_v2",
            predicted_return=round(predicted_return * 100, 2),
            class_probabilities=[float(p) for p in proba],
        )

    def _get_feature_importance(self, features: np.ndarray) -> List[Dict[str, Any]]:
        """获取特征重要性"""
        if self.model is not None and hasattr(self.model, "feature_importances_"):
            importances = self.model.feature_importances_.astype(float)
        else:
            importances = np.abs(features).astype(float)

        names = self.FEATURE_NAMES[:len(importances)]
        display_map = self.FEATURE_DISPLAY_NAMES

        pairs = sorted(
            zip(names, importances),
            key=lambda x: x[1],
            reverse=True,
        )

        return [
            {
                "feature": name,
                "display_name": display_map.get(name, name),
                "value": round(float(imp), 4),
                "importance": round(float(imp), 4),
            }
            for name, imp in pairs[:8]
        ]


# 全局延迟初始化
_predictor: Optional[LightGBMPredictor] = None
_init_lock = threading.Lock()


def get_lightgbm_predictor() -> LightGBMPredictor:
    global _predictor
    if _predictor is None:
        with _init_lock:
            if _predictor is None:
                _predictor = LightGBMPredictor()
    return _predictor


lightgbm_predictor = None  # type: ignore
