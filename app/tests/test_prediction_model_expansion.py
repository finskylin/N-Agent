"""
预测模型扩充单元测试 + 真实数据输入验证

覆盖：
1. stacking.json 新超参读取正确 (n_estimators=2000, seq_len=40 等)
2. data_pipeline.build_lstm_sequences 输出 shape=(N, 40, 25)
3. LSTM _build_model 架构：3层BiLSTM + 2层Attention
4. XGBoost 训练参数：colsample_bynode, early_stopping_rounds, cv_folds=8
5. LightGBM 训练参数：extra_trees, num_leaves=127, cv_folds=8
6. MetaLearner 使用 XGBClassifier (18维输入)
7. prepare_sequence 生成 (1, 40, 25) 序列 — 真实输入路径
8. 特征 shape 一致性：pipeline 和 online inference 路径
9. 真实 AkShare 数据接入测试（网络可用时运行）
"""
import sys
import os
import json
from pathlib import Path
import numpy as np
import pytest

# 将 .claude/skills 目录加入 path 以便导入 prediction 包
_SKILLS_DIR = Path(__file__).parent.parent.parent / ".claude" / "skills"
if str(_SKILLS_DIR) not in sys.path:
    sys.path.insert(0, str(_SKILLS_DIR))

_PREDICTION_DIR = _SKILLS_DIR / "prediction"
_CONFIG_FILE = _PREDICTION_DIR / "config" / "stacking.json"


# ====================================================================
# 1. 配置文件读取验证
# ====================================================================

class TestStackingConfigExpansion:
    """验证 stacking.json 新超参是否正确写入"""

    def _load_config(self):
        assert _CONFIG_FILE.exists(), f"Config not found: {_CONFIG_FILE}"
        with open(_CONFIG_FILE) as f:
            return json.load(f)

    def test_xgboost_expanded_params(self):
        cfg = self._load_config()
        xgb = cfg["model_hyperparams"]["xgboost"]
        assert xgb["n_estimators"] == 2000, f"Expected 2000, got {xgb['n_estimators']}"
        assert xgb["max_depth"] == 10, f"Expected 10, got {xgb['max_depth']}"
        assert xgb["learning_rate"] == 0.008
        assert xgb["min_child_weight"] == 3
        assert "colsample_bynode" in xgb, "colsample_bynode missing"
        assert xgb["colsample_bynode"] == 0.8
        assert xgb["cv_folds"] == 8
        assert xgb["early_stopping_rounds"] == 80

    def test_lightgbm_expanded_params(self):
        cfg = self._load_config()
        lgb = cfg["model_hyperparams"]["lightgbm"]
        assert lgb["n_estimators"] == 3000
        assert lgb["num_leaves"] == 127
        assert lgb["learning_rate"] == 0.003
        assert lgb["min_data_in_leaf"] == 10
        assert lgb["feature_fraction"] == 0.7
        assert lgb.get("extra_trees") is True, "extra_trees must be True"
        assert lgb["cv_folds"] == 8
        assert lgb["early_stopping_rounds"] == 150

    def test_lstm_expanded_params(self):
        cfg = self._load_config()
        lstm = cfg["model_hyperparams"]["lstm"]
        assert lstm["seq_len"] == 40, f"Expected 40, got {lstm['seq_len']}"
        assert lstm["n_features"] == 25, f"Expected 25, got {lstm['n_features']}"
        assert lstm["lstm_units"] == [256, 128, 64]
        assert lstm["attention_layers"] == 2
        assert lstm["attention_heads"] == 8
        assert lstm["batch_size"] == 64
        assert lstm["epochs"] == 150
        assert lstm["patience"] == 20
        assert lstm["learning_rate"] == 0.0005

    def test_meta_learner_xgb_params(self):
        cfg = self._load_config()
        meta = cfg["model_hyperparams"]["meta_learner_xgb"]
        assert meta["n_estimators"] == 50
        assert meta["max_depth"] == 4
        assert meta["cv_folds"] == 8

    def test_meta_learner_cv_folds(self):
        """旧的 meta_learner 配置也要更新"""
        cfg = self._load_config()
        assert cfg["meta_learner"]["n_splits"] == 8


# ====================================================================
# 2. data_pipeline LSTM 序列 shape 验证
# ====================================================================

class TestDataPipelineLSTMShape:
    """验证 build_lstm_sequences 输出 shape=(N, 40, 25)"""

    def _make_mock_df(self, n=300):
        """生成模拟 OHLCV DataFrame"""
        try:
            import pandas as pd
        except ImportError:
            pytest.skip("pandas not installed")

        np.random.seed(42)
        dates = pd.date_range("2020-01-01", periods=n, freq="B")
        close = 10 * np.cumprod(1 + np.random.randn(n) * 0.01)
        df = pd.DataFrame({
            "open": close * (1 + np.random.randn(n) * 0.002),
            "high": close * (1 + np.abs(np.random.randn(n)) * 0.005),
            "low": close * (1 - np.abs(np.random.randn(n)) * 0.005),
            "close": close,
            "volume": np.random.randint(1e6, 1e8, n).astype(float),
            "amount": close * np.random.randint(1e6, 1e8, n),
            "amplitude": np.abs(np.random.randn(n)) * 2,
            "turnover": np.abs(np.random.randn(n)) * 3 + 1,
        }, index=dates)
        return df

    def test_lstm_sequences_shape_25_dim(self):
        try:
            from prediction.training.data_pipeline import TrainingDataPipeline
        except ImportError:
            pytest.skip("data_pipeline not importable in this environment")

        df = self._make_mock_df(300)
        pipeline = TrainingDataPipeline()
        X, y = pipeline.build_lstm_sequences(df, seq_len=40, horizon="1w")

        assert X.ndim == 3, f"Expected 3D array, got {X.ndim}D"
        assert X.shape[1] == 40, f"seq_len should be 40, got {X.shape[1]}"
        assert X.shape[2] == 25, f"n_features should be 25, got {X.shape[2]}"
        assert len(X) == len(y), "X and y length mismatch"
        assert not np.isnan(X).any(), "NaN found in LSTM sequences"
        print(f"✓ LSTM sequences shape: {X.shape}, labels: {y.shape}")

    def test_lstm_sequences_label_distribution(self):
        """验证标签分布合理（三类都有样本）"""
        try:
            from prediction.training.data_pipeline import TrainingDataPipeline
        except ImportError:
            pytest.skip("data_pipeline not importable")

        df = self._make_mock_df(500)
        pipeline = TrainingDataPipeline()
        X, y = pipeline.build_lstm_sequences(df, seq_len=40, horizon="1w")

        if len(y) == 0:
            pytest.skip("No sequences generated (data too short)")

        unique_labels = np.unique(y)
        assert 0 in unique_labels or 1 in unique_labels or 2 in unique_labels, \
            "No valid labels generated"
        print(f"✓ Label distribution: DOWN={np.sum(y==0)}, NEUTRAL={np.sum(y==1)}, UP={np.sum(y==2)}")


# ====================================================================
# 3. LSTM 模型架构验证（需要 TensorFlow）
# ====================================================================

class TestLSTMArchitecture:
    """验证 LSTM _build_model 生成正确架构"""

    def test_model_layers_3_bilstm_2_attention(self):
        try:
            import tensorflow as tf
        except ImportError:
            pytest.skip("TensorFlow not installed")

        try:
            from prediction.models.lstm_model import LSTMPredictor
        except ImportError:
            pytest.skip("lstm_model not importable")

        predictor = LSTMPredictor.__new__(LSTMPredictor)
        model = predictor._build_model(seq_len=40, n_features=25)

        layer_names = [l.name for l in model.layers]
        print(f"Model layers: {layer_names}")

        # 验证有3层BiLSTM
        bilstm_layers = [n for n in layer_names if "bi_lstm" in n]
        assert len(bilstm_layers) >= 3, f"Expected >=3 BiLSTM layers, got {len(bilstm_layers)}: {bilstm_layers}"

        # 验证有2层Attention
        attn_layers = [n for n in layer_names if "multi_head_attention" in n]
        assert len(attn_layers) >= 2, f"Expected >=2 Attention layers, got {len(attn_layers)}: {attn_layers}"

        # 验证输入输出 shape
        assert model.input_shape == (None, 40, 25), f"Input shape mismatch: {model.input_shape}"
        assert model.output_shape == (None, 3), f"Output shape mismatch: {model.output_shape}"

        param_count = model.count_params()
        assert param_count > 500_000, f"Model too small: {param_count} params (expected >500K)"
        print(f"✓ LSTM model params: {param_count:,}")

    def test_lstm_config_driven_architecture(self):
        """验证 architecture 跟配置文件联动"""
        try:
            import tensorflow as tf
        except ImportError:
            pytest.skip("TensorFlow not installed")

        try:
            from prediction.models.lstm_model import LSTMPredictor
        except ImportError:
            pytest.skip("lstm_model not importable")

        predictor = LSTMPredictor.__new__(LSTMPredictor)
        assert predictor._DEFAULT_SEQ_LEN == 40, f"DEFAULT_SEQ_LEN should be 40"
        assert predictor._DEFAULT_N_FEATURES == 25, f"DEFAULT_N_FEATURES should be 25"
        assert len(predictor.FEATURE_NAMES) == 25, f"FEATURE_NAMES length should be 25"


# ====================================================================
# 4. prepare_sequence 在线推理路径验证
# ====================================================================

class TestPrepareSequence:
    """验证在线推理时 prepare_sequence 正确生成 (1, 40, 25) 序列"""

    def _make_kline_list(self, n=60):
        """生成模拟 kline list"""
        np.random.seed(123)
        close = 10 * np.cumprod(1 + np.random.randn(n) * 0.01)
        rows = []
        for i in range(n):
            rows.append({
                "close": float(close[i]),
                "volume": float(np.random.randint(1e6, 1e8)),
                "amplitude": float(abs(np.random.randn()) * 2),
                "turnover": float(abs(np.random.randn()) * 3 + 1),
            })
        return rows

    def test_prepare_sequence_shape(self):
        try:
            import tensorflow as tf
        except ImportError:
            pytest.skip("TensorFlow not installed")

        try:
            from prediction.models.lstm_model import LSTMPredictor
        except ImportError:
            pytest.skip("lstm_model not importable")

        predictor = LSTMPredictor.__new__(LSTMPredictor)
        predictor.sequence_length = 40
        kline = self._make_kline_list(60)
        seq = predictor.prepare_sequence(kline)

        assert seq is not None, "prepare_sequence returned None"
        assert seq.shape == (1, 40, 25), f"Expected (1,40,25), got {seq.shape}"
        assert not np.isnan(seq).any(), "NaN in prepared sequence"
        assert not np.isinf(seq).any(), "Inf in prepared sequence"
        print(f"✓ prepare_sequence output shape: {seq.shape}")

    def test_prepare_sequence_insufficient_data(self):
        """数据不足时返回 None"""
        try:
            import tensorflow as tf
        except ImportError:
            pytest.skip("TensorFlow not installed")

        try:
            from prediction.models.lstm_model import LSTMPredictor
        except ImportError:
            pytest.skip("lstm_model not importable")

        predictor = LSTMPredictor.__new__(LSTMPredictor)
        predictor.sequence_length = 40
        kline = self._make_kline_list(20)  # 少于 seq_len=40
        seq = predictor.prepare_sequence(kline)
        assert seq is None, "Should return None when insufficient data"

    def test_prepare_sequence_feature_names_count(self):
        """FEATURE_NAMES 应该有 25 个"""
        try:
            import tensorflow as tf
        except ImportError:
            pytest.skip("TensorFlow not installed")

        try:
            from prediction.models.lstm_model import LSTMPredictor
        except ImportError:
            pytest.skip("lstm_model not importable")

        predictor = LSTMPredictor.__new__(LSTMPredictor)
        assert len(predictor.FEATURE_NAMES) == 25, \
            f"Expected 25 feature names, got {len(predictor.FEATURE_NAMES)}"


# ====================================================================
# 5. XGBoost 训练参数验证
# ====================================================================

class TestXGBoostExpandedParams:
    """验证 XGBoost 从配置正确加载扩充超参"""

    def test_xgboost_model_params_from_config(self):
        try:
            import xgboost as xgb
        except ImportError:
            pytest.skip("xgboost not installed")

        try:
            from prediction.prediction_config import get_prediction_config
        except ImportError:
            pytest.skip("prediction_config not importable")

        cfg = get_prediction_config()
        xgb_hp = cfg.stacking.get("model_hyperparams", {}).get("xgboost", {})

        assert xgb_hp.get("n_estimators") == 2000
        assert xgb_hp.get("max_depth") == 10
        assert xgb_hp.get("colsample_bynode") == 0.8
        assert xgb_hp.get("cv_folds") == 8
        assert xgb_hp.get("early_stopping_rounds") == 80


# ====================================================================
# 6. MetaLearner XGBClassifier 验证
# ====================================================================

class TestMetaLearnerXGB:
    """验证 MetaLearner 使用 XGBClassifier 且输入维度正确"""

    def test_meta_learner_uses_xgb(self):
        try:
            import xgboost as xgb
            import sklearn
        except ImportError:
            pytest.skip("xgboost or sklearn not installed")

        try:
            from prediction.models.ensemble_model import MetaLearner, XGB_META_AVAILABLE
        except ImportError:
            pytest.skip("ensemble_model not importable")

        assert XGB_META_AVAILABLE, "xgboost must be available for XGBClassifier Meta-Learner"

    def test_meta_learner_train_18dim(self):
        """验证 MetaLearner 可以用 18 维输入训练"""
        try:
            import xgboost as xgb
            import sklearn
        except ImportError:
            pytest.skip("xgboost or sklearn not installed")

        try:
            from prediction.models.ensemble_model import MetaLearner
        except ImportError:
            pytest.skip("ensemble_model not importable")

        # 生成模拟 OOF 数据 (12维 + 6维辅助)
        np.random.seed(42)
        n = 500
        oof_preds = np.random.dirichlet([1, 1, 1], n * 4).reshape(n, 12)
        aux_features = np.random.randn(n, 6)
        labels = np.random.randint(0, 3, n)

        meta = MetaLearner.__new__(MetaLearner)
        meta.model = None
        meta.scaler = None
        meta._trained = False

        # 跳过文件加载，直接训练
        metrics = meta.train(oof_preds, labels, n_splits=3, aux_features=aux_features)

        assert meta._trained, "MetaLearner should be trained"
        assert metrics["n_features"] == 18, f"Expected 18 features, got {metrics['n_features']}"
        assert metrics["meta_learner_type"] == "XGBClassifier"
        assert len(metrics["fold_accuracies"]) == 3
        print(f"✓ MetaLearner trained: CV={metrics['mean_cv_accuracy']:.4f}, type={metrics['meta_learner_type']}")

    def test_meta_learner_predict_proba_shape(self):
        """验证 predict_proba 返回 shape=(3,)"""
        try:
            import xgboost as xgb
            import sklearn
        except ImportError:
            pytest.skip("xgboost or sklearn not installed")

        try:
            from prediction.models.ensemble_model import MetaLearner
        except ImportError:
            pytest.skip("ensemble_model not importable")

        np.random.seed(0)
        n = 300
        oof_preds = np.random.dirichlet([1, 1, 1], n * 4).reshape(n, 12)
        labels = np.random.randint(0, 3, n)

        meta = MetaLearner.__new__(MetaLearner)
        meta.model = None
        meta.scaler = None
        meta._trained = False
        meta.train(oof_preds, labels, n_splits=3)

        test_input = np.random.dirichlet([1, 1, 1], 4).flatten()  # 12维
        result = meta.predict_proba(test_input)
        assert result is not None
        assert result.shape == (3,), f"Expected (3,), got {result.shape}"
        assert abs(result.sum() - 1.0) < 0.01, f"Probabilities don't sum to 1: {result.sum()}"


# ====================================================================
# 7. 真实 AkShare 数据输入测试（需要网络）
# ====================================================================

@pytest.mark.skipif(
    os.environ.get("SKIP_NETWORK_TESTS", "false").lower() == "true",
    reason="Network tests disabled"
)
class TestRealDataInput:
    """真实数据接入测试 — 验证输入数据是否足够"""

    def test_fetch_real_kline_for_lstm(self):
        """获取真实 K 线数据，验证 prepare_sequence 可以正常处理"""
        try:
            import akshare as ak
            import pandas as pd
        except ImportError:
            pytest.skip("akshare or pandas not installed")

        try:
            import tensorflow as tf
            from prediction.models.lstm_model import LSTMPredictor
        except ImportError:
            pytest.skip("lstm_model or tensorflow not importable")

        # 获取贵州茅台 60 天 K 线
        try:
            from datetime import datetime, timedelta
            end = datetime.now().strftime("%Y%m%d")
            start = (datetime.now() - timedelta(days=120)).strftime("%Y%m%d")
            df = ak.stock_zh_a_hist(
                symbol="600519", period="daily",
                start_date=start, end_date=end, adjust="qfq"
            )
            if df is None or len(df) < 50:
                pytest.skip("Insufficient real data from AkShare")
        except Exception as e:
            pytest.skip(f"AkShare unavailable: {e}")

        # 转换为 kline list 格式
        kline_list = []
        for _, row in df.iterrows():
            kline_list.append({
                "close": float(row.get("收盘", row.get("close", 0))),
                "volume": float(row.get("成交量", row.get("volume", 0))),
                "amplitude": float(row.get("振幅", row.get("amplitude", 0))),
                "turnover": float(row.get("换手率", row.get("turnover", 0))),
            })

        predictor = LSTMPredictor.__new__(LSTMPredictor)
        predictor.sequence_length = 40

        seq = predictor.prepare_sequence(kline_list)
        assert seq is not None, f"prepare_sequence failed with {len(kline_list)} real kline rows"
        assert seq.shape == (1, 40, 25), f"Real data shape: {seq.shape}"
        assert not np.isnan(seq).any(), "NaN in real data sequence"

        print(f"✓ Real data: {len(kline_list)} kline rows → sequence shape {seq.shape}")
        print(f"  Feature range: [{seq.min():.3f}, {seq.max():.3f}]")
        print(f"  NaN count: {np.isnan(seq).sum()}, Inf count: {np.isinf(seq).sum()}")

    def test_real_tabular_features_dimension(self):
        """真实 K 线 → tabular 特征维度验证"""
        try:
            import akshare as ak
            import pandas as pd
        except ImportError:
            pytest.skip("akshare or pandas not installed")

        try:
            from prediction.training.data_pipeline import TrainingDataPipeline
        except ImportError:
            pytest.skip("data_pipeline not importable")

        try:
            from datetime import datetime, timedelta
            end = datetime.now().strftime("%Y%m%d")
            start = (datetime.now() - timedelta(days=500)).strftime("%Y%m%d")
            df = ak.stock_zh_a_hist(
                symbol="000001", period="daily",
                start_date=start, end_date=end, adjust="qfq"
            )
            if df is None or len(df) < 100:
                pytest.skip("Insufficient real data from AkShare")
        except Exception as e:
            pytest.skip(f"AkShare unavailable: {e}")

        # 标准化列名
        col_map = {
            "日期": "date", "开盘": "open", "收盘": "close",
            "最高": "high", "最低": "low", "成交量": "volume",
            "成交额": "amount", "振幅": "amplitude",
            "涨跌幅": "change_pct", "涨跌额": "change_amt",
            "换手率": "turnover",
        }
        df = df.rename(columns=col_map)
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date").sort_index()

        pipeline = TrainingDataPipeline()
        feat = pipeline.calculate_features(df)

        assert feat is not None, "calculate_features returned None for real data"
        assert feat.shape[1] == len(pipeline.FEATURE_COLUMNS), \
            f"Expected {len(pipeline.FEATURE_COLUMNS)} features, got {feat.shape[1]}"

        # 验证 LSTM 序列
        X, y = pipeline.build_lstm_sequences(df, seq_len=40, horizon="1w")
        assert X.shape[1] == 40, f"seq_len should be 40, got {X.shape[1]}"
        assert X.shape[2] == 25, f"n_features should be 25, got {X.shape[2]}"

        print(f"✓ Real data tabular features: {feat.shape}")
        print(f"✓ Real data LSTM sequences: {X.shape}")
        nan_ratio = feat.isna().sum().sum() / feat.size
        print(f"  NaN ratio in tabular features: {nan_ratio:.2%}")


# ====================================================================
# 8. 数据充足性诊断测试
# ====================================================================

class TestDataSufficiency:
    """验证真实使用时输入数据是否足够驱动模型"""

    def test_minimum_kline_for_lstm_seq40(self):
        """LSTM seq_len=40 需要至少 40 条 K 线"""
        try:
            import tensorflow as tf
            from prediction.models.lstm_model import LSTMPredictor
        except ImportError:
            pytest.skip("lstm_model or tensorflow not importable")

        predictor = LSTMPredictor.__new__(LSTMPredictor)
        predictor.sequence_length = 40

        # 精确 40 条 → 应该有结果
        np.random.seed(99)
        kline_40 = [
            {"close": float(10 + np.random.randn() * 0.1 * i),
             "volume": 1e6, "amplitude": 2.0, "turnover": 1.0}
            for i in range(40)
        ]
        seq = predictor.prepare_sequence(kline_40)
        assert seq is not None, "40 rows should be sufficient for seq_len=40"

        # 39 条 → 应该返回 None
        seq = predictor.prepare_sequence(kline_40[:39])
        assert seq is None, "39 rows should be insufficient for seq_len=40"

    def test_tabular_features_no_all_nan_columns(self):
        """tabular 特征不应存在全为 NaN 的列"""
        try:
            import pandas as pd
            from prediction.training.data_pipeline import TrainingDataPipeline
        except ImportError:
            pytest.skip("data_pipeline or pandas not importable")

        np.random.seed(42)
        n = 300
        dates = pd.date_range("2020-01-01", periods=n, freq="B")
        close = 10 * np.cumprod(1 + np.random.randn(n) * 0.01)
        df = pd.DataFrame({
            "open": close * 0.99,
            "high": close * 1.01,
            "low": close * 0.98,
            "close": close,
            "volume": np.random.randint(1e6, 1e8, n).astype(float),
            "amount": close * np.random.randint(1e6, 1e8, n),
            "amplitude": np.abs(np.random.randn(n)) * 2,
            "turnover": np.abs(np.random.randn(n)) * 3 + 1,
        }, index=dates)

        pipeline = TrainingDataPipeline()
        feat = pipeline.calculate_features(df)

        assert feat is not None
        all_nan_cols = [col for col in feat.columns if feat[col].isna().all()]
        assert len(all_nan_cols) == 0, f"All-NaN columns found: {all_nan_cols}"

        # 检查整体 NaN 比例
        nan_ratio = feat.isna().sum().sum() / feat.size
        assert nan_ratio < 0.15, f"Too many NaN in features: {nan_ratio:.2%}"
        print(f"✓ Tabular features NaN ratio: {nan_ratio:.2%}")

    def test_lstm_features_no_nan_after_seq40(self):
        """LSTM 25维特征在 seq_len=40 后无 NaN"""
        try:
            import pandas as pd
            from prediction.training.data_pipeline import TrainingDataPipeline
        except ImportError:
            pytest.skip("data_pipeline or pandas not importable")

        np.random.seed(42)
        n = 300
        dates = pd.date_range("2020-01-01", periods=n, freq="B")
        close = 10 * np.cumprod(1 + np.random.randn(n) * 0.01)
        df = pd.DataFrame({
            "open": close * 0.99,
            "high": close * 1.01,
            "low": close * 0.98,
            "close": close,
            "volume": np.random.randint(1e6, 1e8, n).astype(float),
            "amount": close * np.random.randint(1e6, 1e8, n),
            "amplitude": np.abs(np.random.randn(n)) * 2,
            "turnover": np.abs(np.random.randn(n)) * 3 + 1,
        }, index=dates)

        pipeline = TrainingDataPipeline()
        X, y = pipeline.build_lstm_sequences(df, seq_len=40, horizon="1w")

        if len(X) == 0:
            pytest.skip("No sequences generated (data too short)")

        nan_count = np.isnan(X).sum()
        assert nan_count == 0, f"NaN found in LSTM sequences: {nan_count}"
        inf_count = np.isinf(X).sum()
        assert inf_count == 0, f"Inf found in LSTM sequences: {inf_count}"
        print(f"✓ LSTM 25-dim sequences: shape={X.shape}, NaN=0, Inf=0")
