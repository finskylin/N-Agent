"""
XGBoost 股票预测模型 — 深度优化版 v3
基于梯度提升树的股票走势预测

升级内容:
- 特征维度跟随 data_pipeline.FEATURE_COLUMNS 动态适配
- n_estimators=2000, max_depth=10, learning_rate=0.008
- colsample_bynode=0.8 三级列采样
- TimeSeriesSplit 8折滚动验证
- Early stopping 80轮 + L1/L2 正则化
- 样本权重时间衰减
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
    import xgboost as xgb
    from sklearn.preprocessing import StandardScaler
    from sklearn.model_selection import TimeSeriesSplit
    from sklearn.metrics import accuracy_score
    import joblib
    XGBOOST_AVAILABLE = True
except ImportError:
    XGBOOST_AVAILABLE = False
    logger.warning("xgboost/sklearn/joblib not installed")


@dataclass
class PredictionResult:
    """预测结果"""
    direction: str  # UP, DOWN, NEUTRAL
    probability: float  # 0.0 - 1.0
    confidence: str  # 高, 中, 低
    magnitude: str  # +5.2%, -3.1%
    key_factors: List[Dict[str, Any]]
    method: str = "xgboost"
    predicted_return: float = 0.0  # 预测收益率
    class_probabilities: Optional[List[float]] = None  # 三分类概率 [DOWN, NEUTRAL, UP]


# 模型文件目录
_MODEL_DIR = Path(__file__).parent.parent / "training" / "models"
_MODEL_DIR.mkdir(parents=True, exist_ok=True)

_XGBOOST_MODEL_FILE = _MODEL_DIR / "xgboost_1w.json"
_XGBOOST_SCALER_FILE = _MODEL_DIR / "xgboost_scaler_1w.pkl"
_XGBOOST_META_FILE = _MODEL_DIR / "xgboost_meta_1w.json"
_TRAIN_LOCK = threading.Lock()


class XGBoostPredictor:
    """
    XGBoost 股票预测器 — 深度优化版
    51维特征 + TimeSeriesSplit + 早停 + 正则化
    """

    # 51维特征列表 (与 data_pipeline.FEATURE_COLUMNS 一致)
    FEATURE_NAMES = [
        # 原始技术面 16 维
        "ma5_ratio", "ma10_ratio", "ma20_ratio",
        "macd", "macd_signal", "macd_hist",
        "kdj_k", "kdj_d", "kdj_j",
        "rsi_14", "rsi_6",
        "amplitude", "turnover_rate",
        "volume_ratio",
        "price_change_5d", "price_change_10d",
        # 基本面 8 维
        "pe_ttm_pctl", "pb_pctl", "roe_latest", "revenue_yoy",
        "profit_yoy", "gross_margin", "debt_ratio", "market_cap_log",
        # 资金面 7 维
        "main_net_5d", "main_net_20d", "flow_stability",
        "north_bound_chg", "margin_balance_chg",
        "large_order_ratio", "super_large_pct",
        # 波动率 5 维
        "atr_14", "volatility_20d", "volatility_ratio",
        "max_drawdown_20d", "skewness_20d",
        # 量价关系 5 维
        "obv_slope", "vwap_deviation", "price_volume_corr",
        "vol_breakout", "turnover_ma_ratio",
        # 市场环境 6 维
        "market_index_return", "market_breadth", "sector_rank",
        "sector_return_5d", "market_volatility", "risk_premium",
        # 情绪量化 4 维
        "news_sentiment_score", "analyst_consensus",
        "search_trend_idx", "social_heat_idx",
    ]

    # 特征中文名称映射 (用于特征重要性展示)
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
        self.model: Optional[xgb.XGBClassifier] = None
        self.scaler: Optional[StandardScaler] = None
        self._trained = False
        self._train_metrics: Dict[str, Any] = {}

        if not XGBOOST_AVAILABLE:
            logger.error("XGBoost unavailable — prediction will fail")
            return

        model_file = Path(model_path) if model_path else _XGBOOST_MODEL_FILE
        scaler_file = _XGBOOST_SCALER_FILE

        if model_file.exists() and scaler_file.exists():
            self._load(model_file, scaler_file)
        else:
            logger.info("No pre-trained XGBoost model found, will auto-train on first predict")

    # ------------------------------------------------------------------ #
    #  加载 / 保存
    # ------------------------------------------------------------------ #

    def _load(self, model_file: Path, scaler_file: Path):
        try:
            self.model = xgb.XGBClassifier()
            self.model.load_model(str(model_file))
            self.scaler = joblib.load(str(scaler_file))
            self._trained = True
            # 加载训练元数据
            if _XGBOOST_META_FILE.exists():
                with open(_XGBOOST_META_FILE) as f:
                    self._train_metrics = json.load(f)
            logger.info(f"Loaded XGBoost model from {model_file}")
        except Exception as e:
            logger.error(f"Failed to load XGBoost model: {e}")
            self.model = None
            self.scaler = None
            self._trained = False

    def _save(self, model: Any, scaler: Any, metrics: Dict):
        model.save_model(str(_XGBOOST_MODEL_FILE))
        joblib.dump(scaler, str(_XGBOOST_SCALER_FILE))
        with open(_XGBOOST_META_FILE, "w") as f:
            json.dump(metrics, f, indent=2, default=str)
        logger.info(f"Saved XGBoost model to {_XGBOOST_MODEL_FILE}")

    # ------------------------------------------------------------------ #
    #  自动训练 — 深度优化版
    # ------------------------------------------------------------------ #

    def _auto_train(self):
        """使用 data_pipeline 下载数据并训练 — TimeSeriesSplit + 早停 + 正则化"""
        if not XGBOOST_AVAILABLE:
            raise RuntimeError("xgboost/sklearn not installed")

        with _TRAIN_LOCK:
            if self._trained:
                return

            from ..training.data_pipeline import TrainingDataPipeline
            pipeline = TrainingDataPipeline()

            # 尝试加载已有数据集
            X, y = pipeline.load_dataset(horizon="1w", kind="tabular")
            if X.size == 0:
                logger.info("Building tabular dataset for XGBoost (200+ stocks, 2000 days)...")
                X, y = pipeline.build_dataset(horizon="1w", days=2000, save=True)

            # 标准化
            scaler = StandardScaler()
            X_scaled = scaler.fit_transform(X)

            # 深度优化超参 — 从配置文件读取，保留硬编码默认值作为 fallback
            cfg = get_prediction_config()
            xgb_hp = cfg.stacking.get("model_hyperparams", {}).get("xgboost", {})
            early_stopping_rounds = xgb_hp.get("early_stopping_rounds", 80)
            cv_folds = xgb_hp.get("cv_folds", 8)

            model = xgb.XGBClassifier(
                n_estimators=xgb_hp.get("n_estimators", 2000),
                max_depth=xgb_hp.get("max_depth", 10),
                learning_rate=xgb_hp.get("learning_rate", 0.008),
                min_child_weight=xgb_hp.get("min_child_weight", 3),
                gamma=xgb_hp.get("gamma", 0.1),
                reg_alpha=xgb_hp.get("reg_alpha", 0.1),        # L1 正则化
                reg_lambda=xgb_hp.get("reg_lambda", 1.0),       # L2 正则化
                subsample=xgb_hp.get("subsample", 0.7),
                colsample_bytree=xgb_hp.get("colsample_bytree", 0.7),
                colsample_bylevel=xgb_hp.get("colsample_bylevel", 0.7),
                colsample_bynode=xgb_hp.get("colsample_bynode", 0.8),  # 节点级列采样
                scale_pos_weight=xgb_hp.get("scale_pos_weight", 1),
                objective="multi:softprob",
                num_class=3,
                eval_metric="mlogloss",
                use_label_encoder=False,
                random_state=xgb_hp.get("random_state", 42),
                n_jobs=-1,
                tree_method=xgb_hp.get("tree_method", "hist"),
                early_stopping_rounds=early_stopping_rounds,
            )

            # TimeSeriesSplit 8 折滚动验证
            tscv = TimeSeriesSplit(n_splits=cv_folds)
            fold_accuracies = []
            best_model = None
            best_acc = 0.0

            for fold, (train_idx, val_idx) in enumerate(tscv.split(X_scaled)):
                X_train, X_val = X_scaled[train_idx], X_scaled[val_idx]
                y_train, y_val = y[train_idx], y[val_idx]

                # 样本权重 — 时间指数衰减 (近期样本权重更高)
                n_train = len(train_idx)
                decay_rate = cfg.stacking.get("sample_weight_decay", {}).get("xgboost", 0.001)
                sample_weights = np.exp(decay_rate * np.arange(n_train))
                sample_weights /= sample_weights.sum() / n_train  # 归一化保持总权重

                fold_model = xgb.XGBClassifier(**model.get_params())
                fold_model.fit(
                    X_train, y_train,
                    sample_weight=sample_weights,
                    eval_set=[(X_val, y_val)],
                    verbose=False,
                )

                # 使用 best_iteration 进行预测
                if hasattr(fold_model, 'best_iteration'):
                    y_pred = fold_model.predict(X_val, iteration_range=(0, fold_model.best_iteration + 1))
                else:
                    y_pred = fold_model.predict(X_val)

                fold_acc = accuracy_score(y_val, y_pred)
                fold_accuracies.append(fold_acc)
                logger.info(f"XGBoost Fold {fold+1}/{cv_folds} — accuracy: {fold_acc:.4f}")

                if fold_acc > best_acc:
                    best_acc = fold_acc
                    best_model = fold_model

            # 最终使用全量数据训练 (保留最后 10% 作为早停验证)
            split_idx = int(len(X_scaled) * 0.9)
            X_train_final, X_val_final = X_scaled[:split_idx], X_scaled[split_idx:]
            y_train_final, y_val_final = y[:split_idx], y[split_idx:]

            n_final = len(X_train_final)
            final_weights = np.exp(decay_rate * np.arange(n_final))
            final_weights /= final_weights.sum() / n_final

            final_model = xgb.XGBClassifier(**model.get_params())
            final_model.fit(
                X_train_final, y_train_final,
                sample_weight=final_weights,
                eval_set=[(X_val_final, y_val_final)],
                verbose=False,
            )

            final_acc = accuracy_score(y_val_final, final_model.predict(X_val_final))

            metrics = {
                "model": "XGBoost_v3_deep",
                "n_features": int(X.shape[1]),
                "n_samples": int(X.shape[0]),
                "cv_accuracies": [round(a, 4) for a in fold_accuracies],
                "cv_mean_accuracy": round(float(np.mean(fold_accuracies)), 4),
                "cv_std_accuracy": round(float(np.std(fold_accuracies)), 4),
                "final_accuracy": round(float(final_acc), 4),
                "best_iteration": int(getattr(final_model, 'best_iteration', 1000)),
                "hyperparams": {
                    "n_estimators": xgb_hp.get("n_estimators", 1000),
                    "max_depth": xgb_hp.get("max_depth", 8),
                    "learning_rate": xgb_hp.get("learning_rate", 0.01),
                    "reg_alpha": xgb_hp.get("reg_alpha", 0.1),
                    "reg_lambda": xgb_hp.get("reg_lambda", 1.0),
                },
            }

            logger.info(
                f"XGBoost training complete — "
                f"CV mean: {metrics['cv_mean_accuracy']:.4f} +/- {metrics['cv_std_accuracy']:.4f}, "
                f"Final: {final_acc:.4f}"
            )

            self._save(final_model, scaler, metrics)
            self.model = final_model
            self.scaler = scaler
            self._trained = True
            self._train_metrics = metrics

    # ------------------------------------------------------------------ #
    #  特征提取 (在线推理时用)
    # ------------------------------------------------------------------ #

    def extract_features(
        self,
        tech: Dict[str, Any],
        money: Dict[str, Any],
        valuation: Dict[str, Any],
        microstructure: Optional[Dict[str, Any]] = None,
    ) -> np.ndarray:
        """
        从在线 Skill 数据提取 51 维特征 (与 data_pipeline 一致)

        真实数据优先级：
        - 技术面: technical_indicators skill → indicators.ma/macd/kdj/rsi + summary
        - 基本面: financial_report skill（经 ensemble.py _build_valuation_from_financial 处理）
        - 资金面: money_flow skill（经 ensemble.py _build_money_flow_from_skill 处理）
        - 市场环境: technical_indicators summary 中的市场宽度字段
        - 微观结构: bid_ask_depth + intraday_tick（委比/价差/大单净买/主动买入占比）
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

        # ---- 基本面：真实数据优先，默认值为行业中位数水平 ----
        roe_real = current_val.get("roe", None)
        revenue_yoy_real = current_val.get("revenue_yoy", None)
        profit_yoy_real = current_val.get("profit_yoy", None)
        gross_margin_real = current_val.get("gross_margin", None)
        debt_ratio_real = current_val.get("debt_ratio", None)
        total_mv_real = current_val.get("total_mv", 0)

        # ---- 资金面：money_flow skill 真实数据 ----
        main_net_5d = money_summary.get("main_net_inflow_5d", 0)
        main_net_20d = money_summary.get("main_net_inflow_20d", 0)
        flow_stability = money_summary.get("flow_stability", 0)
        north_bound = money_summary.get("north_bound_change", 0)
        margin_chg = money_summary.get("margin_balance_change", 0)
        large_order = fund_flow.get("large_order_ratio", 0)
        super_large = fund_flow.get("super_large_pct", 0)

        features = [
            # 技术面 16 维（来自 technical_indicators skill）
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
            # 基本面 8 维（来自 financial_report skill，真实财务数据）
            percentiles.get("pe_percentile", 50) / 100,
            percentiles.get("pb_percentile", 50) / 100,
            (roe_real if roe_real is not None else 10) / 100,            # ROE 真实值，默认行业均值 10%
            (revenue_yoy_real if revenue_yoy_real is not None else 0) / 100,  # 营收增速真实值
            (profit_yoy_real if profit_yoy_real is not None else 0) / 100,    # 净利增速真实值
            (gross_margin_real if gross_margin_real is not None else 30) / 100, # 毛利率真实值，默认30%
            (debt_ratio_real if debt_ratio_real is not None else 50) / 100,    # 资产负债率，默认50%
            np.log1p(total_mv_real) / 30,
            # 资金面 7 维（来自 money_flow skill，真实主力/北向/融资数据）
            main_net_5d / 1e8,    # 5日主力净流入（亿元）
            main_net_20d / 1e8,   # 20日主力净流入（亿元）
            flow_stability,        # 资金流稳定性
            north_bound / 1e8,    # 北向资金变化（亿元）
            margin_chg / 1e8,     # 融资余额变化（亿元）
            large_order,           # 大单占比
            super_large,           # 超大单净占比
            # 波动率 5 维
            summary.get("atr_14", 0) / current_price if current_price else 0,
            summary.get("volatility_20d", 0),
            summary.get("volatility_ratio", 1),
            summary.get("max_drawdown_20d", 0),
            summary.get("skewness_20d", 0),
            # 量价关系 5 维
            summary.get("obv_slope", 0),
            summary.get("vwap_deviation", 0),
            summary.get("price_volume_corr", 0),
            summary.get("vol_breakout", 1),
            summary.get("turnover_ma_ratio", 1),
            # 市场环境 6 维
            summary.get("market_index_return", 0) / 10,
            summary.get("market_breadth", 0.5),
            summary.get("sector_rank", 0.5),
            summary.get("sector_return_5d", 0) / 10,
            summary.get("market_volatility", 0),
            summary.get("risk_premium", 0),
            # 情绪量化 4 维（来自 sentiment_analysis skill 或 technical 估算）
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
    ) -> PredictionResult:
        """预测股票走势 — 纯 ML 推理, 无规则 fallback"""
        if not XGBOOST_AVAILABLE:
            raise RuntimeError("xgboost not installed, cannot predict")

        if not self._trained:
            self._auto_train()

        features = self.extract_features(tech, money, valuation, microstructure=microstructure)

        # 处理 NaN/Inf
        features = np.nan_to_num(features, nan=0.0, posinf=1.0, neginf=-1.0)

        # 标准化
        features_scaled = self.scaler.transform(features)

        # 预测
        proba = self.model.predict_proba(features_scaled)[0]
        # 类别: 0=DOWN, 1=NEUTRAL, 2=UP
        class_idx = int(np.argmax(proba))
        max_prob = float(proba[class_idx])

        directions = ["DOWN", "NEUTRAL", "UP"]
        direction = directions[class_idx]

        # 置信度 — 基于概率分布的熵
        cfg = get_prediction_config()
        conf_thresholds = cfg.stacking.get("confidence_thresholds", {})
        high_threshold = conf_thresholds.get("high", 0.6)
        medium_threshold = conf_thresholds.get("medium", 0.3)

        entropy = -np.sum(proba * np.log(proba + 1e-9))
        max_entropy = -np.log(1/3)  # 均匀分布的熵
        confidence_score = 1 - entropy / max_entropy
        if confidence_score >= high_threshold:
            confidence = "高"
        elif confidence_score >= medium_threshold:
            confidence = "中"
        else:
            confidence = "低"

        # 幅度估算 — 基于概率差异
        magnitude_factor = cfg.stacking.get("return_magnitude_factor", {}).get("xgboost", 0.15)
        up_prob = float(proba[2])
        down_prob = float(proba[0])
        net_prob = up_prob - down_prob
        predicted_return = net_prob * magnitude_factor  # 最大 +/-magnitude_factor*100%
        magnitude = f"{predicted_return * 100:+.1f}%"

        # 特征重要性
        key_factors = self._extract_key_factors(features_scaled[0])

        return PredictionResult(
            direction=direction,
            probability=max_prob,
            confidence=confidence,
            magnitude=magnitude,
            key_factors=key_factors,
            method="xgboost_v2",
            predicted_return=round(predicted_return * 100, 2),
            class_probabilities=[float(p) for p in proba],
        )

    def _extract_key_factors(self, features: np.ndarray) -> List[Dict[str, Any]]:
        """提取关键影响因素 (使用模型特征重要性 + 特征值)"""
        if self.model is not None and hasattr(self.model, "feature_importances_"):
            importances = self.model.feature_importances_
        else:
            importances = np.abs(features)

        # 使用实际特征名称
        names = self.FEATURE_NAMES[:len(importances)]
        display_names = [self.FEATURE_DISPLAY_NAMES.get(n, n) for n in names]

        pairs = sorted(
            zip(display_names, importances, names),
            key=lambda x: x[1],
            reverse=True,
        )

        factors = []
        for display_name, imp, feat_name in pairs[:8]:
            # 获取特征值的方向
            feat_idx = self.FEATURE_NAMES.index(feat_name) if feat_name in self.FEATURE_NAMES else 0
            feat_val = features[feat_idx] if feat_idx < len(features) else 0
            direction = "positive" if feat_val > 0 else ("negative" if feat_val < 0 else "neutral")

            factors.append({
                "name": display_name,
                "feature": feat_name,
                "contribution": round(float(imp), 4),
                "value": round(float(feat_val), 4),
                "direction": direction,
            })

        return factors


# 全局延迟初始化实例
_predictor: Optional[XGBoostPredictor] = None
_init_lock = threading.Lock()


def get_xgboost_predictor() -> XGBoostPredictor:
    """获取 XGBoost 预测器实例 (延迟初始化)"""
    global _predictor
    if _predictor is None:
        with _init_lock:
            if _predictor is None:
                _predictor = XGBoostPredictor()
    return _predictor


# 兼容旧代码
xgboost_predictor = None  # type: ignore
