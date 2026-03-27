"""
LSTM 股票预测模型 — Bidirectional + Attention 优化版 v3
基于长短期记忆网络的时序预测

升级内容:
- 3层 Bidirectional LSTM (256, 128, 64) + Residual Connection
- 2层 Multi-Head Attention (8头, key_dim=[64,32]) + Residual Connection
- 25维特征 (原10维 + 15维新增技术/资金/情绪特征)
- seq_len=40 (原20), 150 epochs + ReduceLROnPlateau
- Layer Normalization + GELU 激活
"""
from typing import Dict, Any, List, Optional, Tuple
from pathlib import Path
import numpy as np
from loguru import logger
from dataclasses import dataclass
import threading

from ..prediction_config import get_prediction_config

try:
    import tensorflow as tf
    from tensorflow.keras.models import Model, load_model as keras_load_model
    from tensorflow.keras.layers import (
        Input, LSTM, Bidirectional, Dense, Dropout,
        BatchNormalization, LayerNormalization,
        Attention, MultiHeadAttention, GlobalAveragePooling1D,
        Concatenate, Add,
    )
    from tensorflow.keras.callbacks import EarlyStopping, ReduceLROnPlateau
    from tensorflow.keras.optimizers import Adam
    from sklearn.preprocessing import MinMaxScaler
    import joblib
    TF_AVAILABLE = True
except ImportError:
    TF_AVAILABLE = False
    logger.warning("tensorflow not installed, LSTM predictions unavailable")


@dataclass
class LSTMPredictionResult:
    """LSTM 预测结果"""
    direction: str
    probability: float
    confidence: str
    predicted_return: float
    sequence_pattern: str
    attention_weights: Optional[List[float]] = None
    method: str = "lstm"


# 模型文件目录
_MODEL_DIR = Path(__file__).parent.parent / "training" / "models"
_MODEL_DIR.mkdir(parents=True, exist_ok=True)

_LSTM_MODEL_FILE = _MODEL_DIR / "lstm_1w.keras"
_LSTM_MODEL_FILE_H5 = _MODEL_DIR / "lstm_1w.h5"
_LSTM_META_FILE = _MODEL_DIR / "lstm_meta_1w.json"
_TRAIN_LOCK = threading.Lock()


class LSTMPredictor:
    """
    LSTM 股票预测器 — Bidirectional + Attention 优化版
    双向LSTM编码 + Multi-Head Attention + 10维特征
    """

    # Defaults; overridden at runtime from config/prediction/stacking.json
    _DEFAULT_SEQ_LEN = 40
    _DEFAULT_N_FEATURES = 25

    @classmethod
    def _lstm_cfg(cls) -> Dict[str, Any]:
        """Return the lstm hyperparams dict from stacking config (with fallbacks)."""
        try:
            cfg = get_prediction_config()
            return cfg.stacking.get("model_hyperparams", {}).get("lstm", {})
        except Exception:
            return {}

    @property
    def SEQ_LEN(self) -> int:
        return self._lstm_cfg().get("seq_len", self._DEFAULT_SEQ_LEN)

    @property
    def N_FEATURES(self) -> int:
        return self._lstm_cfg().get("n_features", self._DEFAULT_N_FEATURES)

    # 25 维特征说明 (原10维 + 15维新增)
    FEATURE_NAMES = [
        # 原 10 维
        "close_norm",       # 归一化收盘价
        "volume_norm",      # 归一化成交量
        "change_pct",       # 涨跌幅
        "amplitude",        # 振幅
        "turnover",         # 换手率
        "ma5_ratio",        # MA5 偏离率
        "rsi_14",           # RSI(14)
        "macd_norm",        # MACD 归一化
        "volatility",       # 波动率
        "obv_slope",        # OBV 斜率
        # 新增 15 维
        "ma20_ratio",       # MA20 偏离率
        "boll_pct",         # 布林带位置百分比
        "rsi_5",            # RSI(5) 短周期
        "cci_14",           # CCI(14)
        "volume_spike",     # 成交量脉冲 (当日量/20日均量)
        "main_net_norm",    # 主力净流入归一化
        "north_5d_norm",    # 北向5日归一化
        "momentum_5d",      # 5日动量
        "return_10d",       # 10日收益率
        "return_20d",       # 20日收益率
        "beta_10d",         # 10日Beta
        "sentiment_norm",   # 情绪归一化
        "tick_buy_5d",      # 5日主动买入比
        "spread_norm",      # 买卖价差归一化
        "vwap_5d_ratio",    # 5日VWAP偏离
    ]

    def __init__(self, model_path: Optional[str] = None, sequence_length: int = 20):
        self.model = None
        self.scaler = None
        self.sequence_length = sequence_length
        self._trained = False

        if not TF_AVAILABLE:
            logger.error("TensorFlow unavailable — LSTM prediction will fail")
            return

        self.scaler = MinMaxScaler(feature_range=(0, 1))

        model_file = Path(model_path) if model_path else _LSTM_MODEL_FILE
        if model_file.exists():
            self._load(model_file)
        elif _LSTM_MODEL_FILE_H5.exists():
            self._load(_LSTM_MODEL_FILE_H5)
        else:
            logger.info("No pre-trained LSTM model found, will auto-train on first predict")

    def _load(self, model_file: Path):
        try:
            self.model = keras_load_model(str(model_file))
            self._trained = True
            logger.info(f"Loaded LSTM model from {model_file}")
        except Exception as e:
            logger.error(f"Failed to load LSTM model: {e}")
            self.model = None
            self._trained = False

    def _save(self, model):
        model.save(str(_LSTM_MODEL_FILE))
        logger.info(f"Saved LSTM model to {_LSTM_MODEL_FILE}")

    # ------------------------------------------------------------------ #
    #  模型架构 — Bidirectional LSTM + Multi-Head Attention
    # ------------------------------------------------------------------ #

    def _build_model(self, seq_len: int, n_features: int) -> Any:
        """构建 3层Bidirectional LSTM + 2层Multi-Head Attention 模型"""
        cfg = get_prediction_config()
        lstm_hp = cfg.stacking.get("model_hyperparams", {}).get("lstm", {})

        # Architecture hyperparams from config (with hardcoded fallbacks)
        lstm_units = lstm_hp.get("lstm_units", [256, 128, 64])
        attention_layers = lstm_hp.get("attention_layers", 2)
        attention_heads = lstm_hp.get("attention_heads", 8)
        # attention_key_dim 支持列表(每层不同)或单值
        raw_key_dim = lstm_hp.get("attention_key_dim", [64, 32])
        attention_key_dims = raw_key_dim if isinstance(raw_key_dim, list) else [raw_key_dim] * attention_layers
        dropout_rate = lstm_hp.get("dropout", 0.2)
        recurrent_dropout_rate = lstm_hp.get("recurrent_dropout", 0.1)
        fc_units = lstm_hp.get("fc_units", [128, 64, 32])
        fc_dropout = lstm_hp.get("fc_dropout", [0.3, 0.2, 0.1])
        learning_rate = lstm_hp.get("learning_rate", 0.0005)

        inputs = Input(shape=(seq_len, n_features), name="sequence_input")
        x = inputs

        # 多层 Bidirectional LSTM (支持可变层数)
        for i, units in enumerate(lstm_units):
            is_last = (i == len(lstm_units) - 1)
            lstm_out = Bidirectional(
                LSTM(units, return_sequences=True,
                     dropout=dropout_rate, recurrent_dropout=recurrent_dropout_rate),
                name=f"bi_lstm_{i+1}"
            )(x)
            lstm_out = LayerNormalization(name=f"ln_lstm_{i+1}")(lstm_out)
            # Residual Connection（维度匹配时添加）
            if x.shape[-1] == lstm_out.shape[-1]:
                lstm_out = Add(name=f"residual_lstm_{i+1}")([x, lstm_out])
                lstm_out = LayerNormalization(name=f"ln_res_lstm_{i+1}")(lstm_out)
            x = lstm_out

        # 多层 Multi-Head Attention
        for i in range(attention_layers):
            key_dim = attention_key_dims[i] if i < len(attention_key_dims) else attention_key_dims[-1]
            attn_out = MultiHeadAttention(
                num_heads=attention_heads, key_dim=key_dim,
                name=f"multi_head_attention_{i+1}"
            )(x, x)
            attn_out = Dropout(dropout_rate, name=f"attention_dropout_{i+1}")(attn_out)
            # Residual Connection
            x = Add(name=f"residual_attn_{i+1}")([x, attn_out])
            x = LayerNormalization(name=f"ln_attn_{i+1}")(x)

        # Global Average Pooling
        pooled = GlobalAveragePooling1D(name="global_pool")(x)

        # 全连接层 (支持可变层数)
        x = pooled
        for i, units in enumerate(fc_units):
            activation = "gelu" if i < len(fc_units) - 1 else "relu"
            x = Dense(units, activation=activation, name=f"fc_{i+1}")(x)
            drop = fc_dropout[i] if i < len(fc_dropout) else 0.1
            x = Dropout(drop, name=f"fc_dropout_{i+1}")(x)

        # 输出层
        outputs = Dense(3, activation="softmax", name="output")(x)

        model = Model(inputs=inputs, outputs=outputs, name="BiLSTM_v3_MultiAttn")

        optimizer = Adam(learning_rate=learning_rate)
        model.compile(
            optimizer=optimizer,
            loss="sparse_categorical_crossentropy",
            metrics=["accuracy"],
        )

        return model

    # ------------------------------------------------------------------ #
    #  自动训练
    # ------------------------------------------------------------------ #

    def _auto_train(self):
        if not TF_AVAILABLE:
            raise RuntimeError("tensorflow not installed")

        with _TRAIN_LOCK:
            if self._trained:
                return

            cfg = get_prediction_config()
            lstm_hp = cfg.stacking.get("model_hyperparams", {}).get("lstm", {})
            decay_cfg = cfg.stacking.get("sample_weight_decay", {})

            # Training hyperparams from config (with hardcoded fallbacks)
            epochs = lstm_hp.get("epochs", 100)
            batch_size = lstm_hp.get("batch_size", 128)
            patience = lstm_hp.get("patience", 15)
            reduce_lr_factor = lstm_hp.get("reduce_lr_factor", 0.5)
            reduce_lr_patience = lstm_hp.get("reduce_lr_patience", 5)
            min_lr = lstm_hp.get("min_lr", 1e-6)
            decay_rate = decay_cfg.get("lstm", 0.0005)

            from ..training.data_pipeline import TrainingDataPipeline
            pipeline = TrainingDataPipeline()

            X, y = pipeline.load_dataset(horizon="1w", kind="lstm")
            if X.size == 0:
                logger.info("Building LSTM dataset (10 features, 200+ stocks)...")
                X, y = pipeline.build_lstm_dataset(
                    horizon="1w", seq_len=self.sequence_length,
                    days=2000, save=True
                )

            if X.size == 0:
                raise RuntimeError("Cannot build LSTM dataset — no data available")

            # 时序拆分 (保持时间顺序)
            split = int(len(X) * 0.85)
            X_train, X_val = X[:split], X[split:]
            y_train, y_val = y[:split], y[split:]

            model = self._build_model(X.shape[1], X.shape[2])

            logger.info(f"LSTM model architecture: {model.count_params()} parameters")

            # 回调
            early_stop = EarlyStopping(
                monitor="val_loss", patience=patience, restore_best_weights=True,
                verbose=1
            )
            reduce_lr = ReduceLROnPlateau(
                monitor="val_loss", factor=reduce_lr_factor,
                patience=reduce_lr_patience,
                min_lr=min_lr, verbose=1
            )

            # 样本权重 — 时间指数衰减
            n_train = len(X_train)
            sample_weights = np.exp(decay_rate * np.arange(n_train))
            sample_weights /= sample_weights.mean()

            # 训练
            history = model.fit(
                X_train, y_train,
                sample_weight=sample_weights,
                validation_data=(X_val, y_val),
                epochs=epochs,
                batch_size=batch_size,
                callbacks=[early_stop, reduce_lr],
                verbose=1,
            )

            # 评估
            _, val_acc = model.evaluate(X_val, y_val, verbose=0)
            best_epoch = len(history.history["val_loss"]) - early_stop.patience if early_stop.stopped_epoch > 0 else len(history.history["val_loss"])

            logger.info(
                f"LSTM training complete — val accuracy: {val_acc:.4f}, "
                f"best epoch: {best_epoch}, "
                f"params: {model.count_params()}"
            )

            self._save(model)
            self.model = model
            self._trained = True

    # ------------------------------------------------------------------ #
    #  推理用序列准备
    # ------------------------------------------------------------------ #

    def prepare_sequence(self, kline_data: List[Dict[str, Any]]) -> Optional[np.ndarray]:
        """
        将在线 K 线数据转为 (1, seq_len, N_FEATURES) 序列

        25维特征 (与 data_pipeline.build_lstm_sequences 一致):
        原10维: close_norm, volume_norm, change, amp/10, turn/10,
                ma5_ratio, rsi_14, macd_norm, volatility, obv_slope
        新15维: ma20_ratio, boll_pct, rsi_5, cci_14, volume_spike,
                main_net_norm, north_5d_norm, momentum_5d, return_10d, return_20d,
                beta_10d, sentiment_norm, tick_buy_5d, spread_norm, vwap_5d_ratio
        """
        seq_len = self.SEQ_LEN
        if len(kline_data) < seq_len:
            logger.warning(f"Insufficient data: {len(kline_data)} < {seq_len}")
            return None

        recent = kline_data[-seq_len:]

        closes, volumes, changes, amps, turns = [], [], [], [], []
        ma5_ratios, rsi_vals, macd_vals, vols, obvs = [], [], [], [], []

        for i, row in enumerate(recent):
            close = float(row.get("close", 0))
            prev_close = float(recent[i - 1].get("close", close)) if i > 0 else close
            vol = float(row.get("volume", 0))
            amp = float(row.get("amplitude", 0))
            turn = float(row.get("turnover", 0))
            change = ((close - prev_close) / (prev_close + 1e-9)) * 100

            closes.append(close)
            volumes.append(vol)
            changes.append(change)
            amps.append(amp)
            turns.append(turn)

            # MA5 偏离率
            if i >= 4:
                ma5 = np.mean(closes[-5:])
                ma5_ratios.append((ma5 / close - 1) if close > 0 else 0)
            else:
                ma5_ratios.append(0)

            # RSI(14) — 简化计算
            if i >= 1:
                gain = max(0, change)
                loss_val = max(0, -change)
                rsi_vals.append(gain / (gain + loss_val + 1e-9))
            else:
                rsi_vals.append(0.5)

            # MACD 归一化
            macd_vals.append(change / 10)

            # 波动率 (基于振幅)
            vols.append(amp / 100)

            # OBV 斜率
            if i >= 1:
                obv_dir = 1 if change > 0 else (-1 if change < 0 else 0)
                obvs.append(obv_dir * vol / (max(volumes) + 1e-9))
            else:
                obvs.append(0)

        closes = np.array(closes, dtype=np.float32)
        volumes = np.array(volumes, dtype=np.float32)

        # 归一化
        c_min, c_max = closes.min(), closes.max()
        close_norm = (closes - c_min) / (c_max - c_min + 1e-9)
        v_max = volumes.max() + 1
        vol_norm = volumes / v_max

        # ---- 新增 15 维特征 ----
        # MA20 偏离率
        ma20_ratios = []
        for i in range(len(closes)):
            if i >= 19:
                ma20 = np.mean(closes[i-19:i+1])
                ma20_ratios.append((ma20 / (closes[i] + 1e-9) - 1))
            else:
                ma20_ratios.append(0)

        # 布林带位置 (20日)
        boll_pcts = []
        for i in range(len(closes)):
            if i >= 19:
                window = closes[i-19:i+1]
                m = window.mean()
                s = window.std() + 1e-9
                boll_pcts.append(np.clip((closes[i] - (m - 2*s)) / (4*s), 0, 1))
            else:
                boll_pcts.append(0.5)

        # RSI(5) 短周期
        rsi_5_vals = []
        for i in range(len(changes)):
            if i >= 4:
                recent_changes = changes[i-4:i+1]
                gains = sum(c for c in recent_changes if c > 0)
                losses = sum(-c for c in recent_changes if c < 0)
                rsi_5_vals.append(gains / (gains + losses + 1e-9))
            else:
                rsi_5_vals.append(0.5)

        # CCI(14) 归一化
        cci_vals = []
        for i in range(len(closes)):
            if i >= 13:
                tp = closes[i-13:i+1]  # 简化：用收盘价代替典型价
                m = tp.mean()
                d = np.mean(np.abs(tp - m)) + 1e-9
                cci = (closes[i] - m) / (0.015 * d)
                cci_vals.append(np.clip(cci / 200, -1, 1))
            else:
                cci_vals.append(0)

        # 成交量脉冲
        volume_spikes = []
        for i in range(len(volumes)):
            if i >= 19:
                avg = np.mean(volumes[i-19:i+1]) + 1e-9
                volume_spikes.append(np.clip(volumes[i] / avg - 1, -1, 3))
            else:
                volume_spikes.append(0)

        # 主力净流入代理 (变化量 * 价格方向归一化)
        changes_arr = np.array(changes, dtype=np.float32)
        signed_vol = volumes * np.sign(changes_arr)
        main_net = np.zeros(len(volumes))
        for i in range(len(volumes)):
            if i >= 4:
                sv_sum = signed_vol[i-4:i+1].sum()
                vol_sum = volumes[i-4:i+1].sum() + 1e-9
                main_net[i] = sv_sum / vol_sum
        main_net_norm = np.clip(main_net, -1, 1)

        # 北向代理 (大成交额比例变化, 简化)
        north_norm = np.zeros(len(volumes))
        vol_ma20_arr = np.array([
            np.mean(volumes[max(0, i-19):i+1]) for i in range(len(volumes))
        ])
        for i in range(len(volumes)):
            if i >= 4:
                large_ratio = np.mean(
                    (volumes[max(0, i-4):i+1] > vol_ma20_arr[i] * 1.5).astype(float)
                )
                north_norm[i] = np.clip(large_ratio * 2 - 1, -1, 1)

        # 动量 5日
        momentum_5d = np.zeros(len(closes))
        for i in range(5, len(closes)):
            momentum_5d[i] = (closes[i] / (closes[i-5] + 1e-9) - 1)
        momentum_5d = np.clip(momentum_5d, -0.5, 0.5)

        # 10日收益率
        return_10d = np.zeros(len(closes))
        for i in range(10, len(closes)):
            return_10d[i] = (closes[i] / (closes[i-10] + 1e-9) - 1)
        return_10d = np.clip(return_10d, -0.5, 0.5)

        # 20日收益率
        return_20d = np.zeros(len(closes))
        for i in range(20, len(closes)):
            return_20d[i] = (closes[i] / (closes[i-20] + 1e-9) - 1)
        return_20d = np.clip(return_20d, -0.5, 0.5)

        # Beta 10日代理 (个股波动/大盘波动 比, 用波动率代理)
        vol_10d = np.zeros(len(changes))
        for i in range(10, len(changes)):
            vol_10d[i] = np.std(changes[i-10:i+1]) + 1e-9
        # 用单只股票历史波动率比代理 Beta (归一化)
        vol_20d_arr = np.array([
            np.std(changes[max(0, i-19):i+1]) + 1e-9 for i in range(len(changes))
        ])
        beta_10d = np.clip(vol_10d / (vol_20d_arr + 1e-9), 0, 3) / 3  # 归一化到 [0,1]

        # 情绪代理 (成交量激增 * 价格方向)
        vol_surge = np.clip((volumes / (vol_ma20_arr + 1e-9) - 1), -1, 3)
        price_dir = np.sign(changes_arr)
        sentiment = (vol_surge * price_dir)
        sentiment_norm = 1 / (1 + np.exp(-sentiment))  # sigmoid 归一化

        # tick_buy_5d 代理 (5日主动买入比, 用价格方向代理)
        tick_buy_5d = np.zeros(len(changes))
        for i in range(5, len(changes)):
            up_count = sum(1 for c in changes[i-5:i+1] if c > 0)
            tick_buy_5d[i] = up_count / 6

        # 买卖价差代理 (振幅归一化)
        amps_arr = np.array(amps, dtype=np.float32)
        spread_norm = np.clip(amps_arr / 10, 0, 1)

        # 5日VWAP偏离
        vwap_5d_ratio = np.zeros(len(closes))
        amounts = volumes * closes
        for i in range(5, len(closes)):
            vwap = amounts[i-5:i+1].sum() / (volumes[i-5:i+1].sum() + 1e-9)
            vwap_5d_ratio[i] = np.clip(closes[i] / (vwap + 1e-9) - 1, -0.5, 0.5)

        raw = np.column_stack([
            # 原 10 维
            close_norm,
            vol_norm,
            changes_arr / 10,
            amps_arr / 10,
            np.array(turns, dtype=np.float32) / 10,
            np.array(ma5_ratios, dtype=np.float32),
            np.array(rsi_vals, dtype=np.float32),
            np.array(macd_vals, dtype=np.float32),
            np.array(vols, dtype=np.float32),
            np.array(obvs, dtype=np.float32),
            # 新增 15 维
            np.array(ma20_ratios, dtype=np.float32),
            np.array(boll_pcts, dtype=np.float32),
            np.array(rsi_5_vals, dtype=np.float32),
            np.array(cci_vals, dtype=np.float32),
            np.array(volume_spikes, dtype=np.float32),
            main_net_norm.astype(np.float32),
            north_norm.astype(np.float32),
            momentum_5d.astype(np.float32),
            return_10d.astype(np.float32),
            return_20d.astype(np.float32),
            beta_10d.astype(np.float32),
            sentiment_norm.astype(np.float32),
            tick_buy_5d.astype(np.float32),
            spread_norm,
            vwap_5d_ratio.astype(np.float32),
        ])

        return raw.reshape(1, seq_len, -1).astype(np.float32)

    # ------------------------------------------------------------------ #
    #  推理
    # ------------------------------------------------------------------ #

    def predict(
        self,
        kline_data: List[Dict[str, Any]],
        horizon: str = "1w",
    ) -> LSTMPredictionResult:
        """预测股票走势 — Bidirectional LSTM + Attention"""
        if not TF_AVAILABLE:
            raise RuntimeError("tensorflow not installed, cannot predict")

        if not self._trained:
            self._auto_train()

        sequence = self.prepare_sequence(kline_data)
        if sequence is None:
            raise ValueError(f"Insufficient kline data (need >= {self.sequence_length} rows)")

        # 处理 NaN
        sequence = np.nan_to_num(sequence, nan=0.0, posinf=1.0, neginf=-1.0)

        proba = self.model.predict(sequence, verbose=0)[0]
        class_idx = int(np.argmax(proba))
        max_prob = float(proba[class_idx])

        directions = ["DOWN", "NEUTRAL", "UP"]
        direction = directions[class_idx]

        # 置信度 — 基于概率熵
        cfg = get_prediction_config()
        conf_thresholds = cfg.stacking.get("confidence_thresholds", {})
        high_threshold = conf_thresholds.get("high", 0.6)
        medium_threshold = conf_thresholds.get("medium", 0.3)

        entropy = -np.sum(proba * np.log(proba + 1e-9))
        max_entropy = -np.log(1/3)
        confidence_score = 1 - entropy / max_entropy
        if confidence_score >= high_threshold:
            confidence = "高"
        elif confidence_score >= medium_threshold:
            confidence = "中"
        else:
            confidence = "低"

        # 估算收益率 — 基于概率分布
        magnitude_cfg = cfg.stacking.get("return_magnitude_factor", {})
        return_magnitude = magnitude_cfg.get("lstm", 0.08)
        up_prob = float(proba[2])
        down_prob = float(proba[0])
        predicted_return = (up_prob - down_prob) * return_magnitude

        pattern = self._identify_pattern(kline_data)

        return LSTMPredictionResult(
            direction=direction,
            probability=max_prob,
            confidence=confidence,
            predicted_return=round(predicted_return * 100, 2),
            sequence_pattern=pattern,
            attention_weights=None,  # 可扩展为提取 attention weights
            method="lstm_v2_attention",
        )

    def _identify_pattern(self, kline_data: List[Dict[str, Any]]) -> str:
        """基于统计分析识别序列模式"""
        if len(kline_data) < 10:
            return "数据不足"

        cfg = get_prediction_config()
        pat = cfg.stacking.get("pattern_detection", {})

        # Pattern detection thresholds from config (with hardcoded fallbacks)
        consecutive_strong_up = pat.get("consecutive_strong_up", 4)
        consecutive_moderate_up = pat.get("consecutive_moderate_up", 3)
        consecutive_strong_down = pat.get("consecutive_strong_down", 4)
        consecutive_moderate_down = pat.get("consecutive_moderate_down", 3)
        strong_change_threshold = pat.get("strong_change_threshold", 0.01)
        trend_strength_threshold = pat.get("trend_strength_threshold", 1.5)
        narrow_range_std = pat.get("narrow_range_std", 0.01)
        volatile_std = pat.get("volatile_std", 0.03)

        recent = kline_data[-10:]
        changes = []
        for i in range(1, len(recent)):
            prev = float(recent[i - 1].get("close", 0))
            curr = float(recent[i].get("close", 0))
            if prev:
                changes.append((curr - prev) / prev)

        if not changes:
            return "数据异常"

        changes = np.array(changes)
        mean_change = np.mean(changes)
        std_change = np.std(changes)
        trend_strength = abs(mean_change) / (std_change + 1e-9)

        # 连续性分析
        up_count = np.sum(changes > 0.005)
        down_count = np.sum(changes < -0.005)
        consecutive_up = self._max_consecutive(changes > 0)
        consecutive_down = self._max_consecutive(changes < 0)

        if consecutive_up >= consecutive_strong_up and mean_change > strong_change_threshold:
            return "强势上涨"
        elif consecutive_up >= consecutive_moderate_up:
            return "连续上涨"
        elif consecutive_down >= consecutive_strong_down and mean_change < -strong_change_threshold:
            return "强势下跌"
        elif consecutive_down >= consecutive_moderate_down:
            return "连续下跌"
        elif trend_strength > trend_strength_threshold and mean_change > 0:
            return "趋势上行"
        elif trend_strength > trend_strength_threshold and mean_change < 0:
            return "趋势下行"
        elif std_change < narrow_range_std:
            return "窄幅震荡"
        elif std_change > volatile_std:
            return "剧烈波动"
        else:
            return "横盘整理"

    @staticmethod
    def _max_consecutive(mask: np.ndarray) -> int:
        """计算最大连续 True 长度"""
        max_count = 0
        count = 0
        for val in mask:
            if val:
                count += 1
                max_count = max(max_count, count)
            else:
                count = 0
        return max_count


# 全局延迟初始化
_predictor: Optional[LSTMPredictor] = None
_init_lock = threading.Lock()


def get_lstm_predictor() -> LSTMPredictor:
    global _predictor
    if _predictor is None:
        with _init_lock:
            if _predictor is None:
                _predictor = LSTMPredictor()
    return _predictor


lstm_predictor = None  # type: ignore
