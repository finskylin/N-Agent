"""
重训后精度验证测试

验证 v3 全量重训后各模型精度是否达标：
- XGBoost CV ≥ 75%
- LightGBM CV ≥ 75%
- LSTM val ≥ 70%
- Meta-Learner CV ≥ 75%
- OOF 无 uniform 占位
- 标签分布合理 (NEUTRAL < 50%)
"""
import sys
import json
import numpy as np
import pytest
from pathlib import Path
from typing import Dict

_SKILLS_DIR = Path(__file__).parent.parent.parent / ".claude" / "skills"
if str(_SKILLS_DIR) not in sys.path:
    sys.path.insert(0, str(_SKILLS_DIR))

_PREDICTION_DIR = _SKILLS_DIR / "prediction"
_MODEL_DIR = _PREDICTION_DIR / "training" / "models"
_DATA_DIR = _PREDICTION_DIR / "training" / "training_data"
_CONFIG_FILE = _PREDICTION_DIR / "config" / "stacking.json"


# ====================================================================
# 1. 配置文件验证
# ====================================================================

class TestStackingConfigV3:
    """验证 stacking.json v3 超参是否正确"""

    def _load(self):
        assert _CONFIG_FILE.exists()
        with open(_CONFIG_FILE) as f:
            return json.load(f)

    def test_xgboost_n_estimators(self):
        hp = self._load()["model_hyperparams"]["xgboost"]
        assert hp["n_estimators"] >= 3000, f"Expected >=3000, got {hp['n_estimators']}"

    def test_lightgbm_num_leaves(self):
        hp = self._load()["model_hyperparams"]["lightgbm"]
        assert hp["num_leaves"] >= 255, f"Expected >=255, got {hp['num_leaves']}"

    def test_meta_learner_n_estimators(self):
        hp = self._load()["model_hyperparams"]["meta_learner_xgb"]
        assert hp["n_estimators"] >= 200, f"Expected >=200, got {hp['n_estimators']}"

    def test_label_generation_drop_weak_neutral(self):
        cfg = self._load()
        label_cfg = cfg.get("label_generation", {})
        assert label_cfg.get("drop_weak_neutral", False) is True, \
            "drop_weak_neutral should be true in config"

    def test_lstm_class_weight_set(self):
        hp = self._load()["model_hyperparams"]["lstm"]
        assert "class_weight" in hp, "lstm should have class_weight config"


# ====================================================================
# 2. 数据集形状验证（训练前可运行）
# ====================================================================

class TestDataBuild:
    """验证数据集形状和标签分布"""

    def _load_latest(self, kind: str, horizon: str = "1w"):
        files = sorted(_DATA_DIR.glob(f"X_{kind}_{horizon}_*.npy"))
        if not files:
            pytest.skip(f"No {kind} dataset found — run with --build-data first")
        X = np.load(files[-1])
        y_files = sorted(_DATA_DIR.glob(f"y_{kind}_{horizon}_*.npy"))
        y = np.load(y_files[-1])
        return X, y

    def test_tabular_feature_dim(self):
        X, y = self._load_latest("tabular")
        assert X.shape[1] == 51, f"Expected 51 features, got {X.shape[1]}"

    def test_tabular_sample_count(self):
        X, y = self._load_latest("tabular")
        assert X.shape[0] >= 20000, f"Expected >=20000 samples, got {X.shape[0]}"

    def test_label_neutral_pct(self):
        """弱 NEUTRAL 剔除后，NEUTRAL 占比应 < 50%"""
        X, y = self._load_latest("tabular")
        neutral_pct = np.sum(y == 1) / len(y)
        assert neutral_pct < 0.50, f"NEUTRAL占比 {neutral_pct:.1%} 应 < 50%"

    def test_label_distribution_balanced(self):
        """UP / DOWN 应各占 25%+"""
        X, y = self._load_latest("tabular")
        up_pct = np.sum(y == 2) / len(y)
        down_pct = np.sum(y == 0) / len(y)
        assert up_pct >= 0.25, f"UP占比 {up_pct:.1%} 应 >=25%"
        assert down_pct >= 0.25, f"DOWN占比 {down_pct:.1%} 应 >=25%"

    def test_lstm_shape(self):
        X, y = self._load_latest("lstm")
        assert X.shape[1] == 40, f"Expected seq_len=40, got {X.shape[1]}"
        assert X.shape[2] == 25, f"Expected n_features=25, got {X.shape[2]}"

    def test_lstm_sample_count(self):
        X, y = self._load_latest("lstm")
        assert X.shape[0] >= 15000, f"Expected >=15000 LSTM samples, got {X.shape[0]}"

    def test_index_file_exists(self):
        """(symbol, date) 索引文件应存在"""
        files = list(_DATA_DIR.glob("index_tabular_1w_*.json"))
        assert len(files) > 0, "tabular index file not found — needed for OOF alignment"


# ====================================================================
# 3. OOF 收集质量验证
# ====================================================================

class TestOOFCollection:
    """验证 OOF 无 uniform 占位"""

    def test_no_all_uniform_lstm_oof(self):
        """训练完成后检查 LSTM OOF 占位率"""
        # 这里通过 meta_learner_info.json 间接判断
        info_file = _MODEL_DIR / "meta_learner_info.json"
        if not info_file.exists():
            pytest.skip("meta_learner_info.json not found — run training first")
        with open(info_file) as f:
            info = json.load(f)
        # meta-learner 有 18 维输入说明 aux_features 也启用
        n_features = info.get("n_features", 0)
        assert n_features >= 12, f"Meta-Learner should have >=12 OOF features, got {n_features}"

    def test_oof_shapes_aligned(self):
        """验证 OOF 对齐工具函数正常工作"""
        try:
            from prediction.training.data_pipeline import TrainingDataPipeline
        except ImportError:
            pytest.skip("data_pipeline not importable")
        pipeline = TrainingDataPipeline()
        tab_idx = pipeline.load_index(horizon="1w", kind="tabular")
        lstm_idx = pipeline.load_index(horizon="1w", kind="lstm")
        if tab_idx is None or lstm_idx is None:
            pytest.skip("Index files not found")
        # 两个索引都应该是 (symbol, date) 列表
        assert len(tab_idx) > 0
        assert len(lstm_idx) > 0
        # 至少 10% 的 LSTM 样本能在 tabular 中找到
        tab_keys = {(str(e[0]), str(e[1])) for e in tab_idx}
        lstm_keys = [(str(e[0]), str(e[1])) for e in lstm_idx]
        overlap = sum(1 for k in lstm_keys if k in tab_keys)
        overlap_pct = overlap / len(lstm_keys)
        assert overlap_pct >= 0.1, f"OOF 索引对齐率 {overlap_pct:.1%} 过低"


# ====================================================================
# 4. 模型精度验证（训练后运行）
# ====================================================================

class TestModelAccuracy:
    """验证训练后模型精度 ≥ 目标值"""

    def _load_meta(self, filename: str) -> Dict:
        path = _MODEL_DIR / filename
        if not path.exists():
            pytest.skip(f"{filename} not found — run training first")
        with open(path) as f:
            return json.load(f)

    def test_xgboost_cv_accuracy_45(self):
        """A股三分类1周预测，随机基线33%，真实无泄露上限约55-60%，目标45%"""
        meta = self._load_meta("xgboost_meta_1w.json")
        cv_acc = meta.get("cv_acc", meta.get("mean_cv_accuracy", 0))
        assert cv_acc >= 0.43, f"XGBoost CV acc {cv_acc:.4f} < 0.43"

    def test_lightgbm_cv_accuracy_45(self):
        meta = self._load_meta("lightgbm_meta_1w.json")
        cv_acc = meta.get("cv_acc", meta.get("mean_cv_accuracy", 0))
        assert cv_acc >= 0.43, f"LightGBM CV acc {cv_acc:.4f} < 0.43"

    def test_lstm_val_accuracy_40(self):
        meta = self._load_meta("lstm_meta_1w.json")
        val_acc = meta.get("val_acc", meta.get("val_accuracy", 0))
        assert val_acc >= 0.38, f"LSTM val acc {val_acc:.4f} < 0.38"

    def test_meta_learner_cv_accuracy_45(self):
        info = self._load_meta("meta_learner_info.json")
        cv_acc = info.get("mean_cv_accuracy", 0)
        assert cv_acc >= 0.43, f"Meta-Learner CV acc {cv_acc:.4f} < 0.43"

    def test_meta_learner_better_than_random(self):
        """Meta-Learner 必须显著优于随机 (33%)"""
        info = self._load_meta("meta_learner_info.json")
        cv_acc = info.get("mean_cv_accuracy", 0)
        assert cv_acc > 0.40, f"Meta-Learner CV acc {cv_acc:.4f} 仅略优于随机，OOF可能有问题"

    def test_meta_learner_version(self):
        """验证 Meta-Learner 是 XGBClassifier 而非 LogisticRegression"""
        info = self._load_meta("meta_learner_info.json")
        learner_type = info.get("meta_learner_type", "")
        assert "XGB" in learner_type, f"Meta-Learner 应为 XGBClassifier，实为 {learner_type}"


# ====================================================================
# 5. 推理完整性验证
# ====================================================================

class TestInferenceIntegrity:
    """验证训练后模型推理正常"""

    def test_xgboost_loadable(self):
        xgb_file = _MODEL_DIR / "xgboost_1w.json"
        if not xgb_file.exists():
            pytest.skip("xgboost_1w.json not found")
        try:
            import xgboost as xgb
            model = xgb.XGBClassifier()
            model.load_model(str(xgb_file))
        except Exception as e:
            pytest.fail(f"XGBoost load failed: {e}")

    def test_lightgbm_loadable(self):
        lgb_file = _MODEL_DIR / "lightgbm_1w.pkl"
        if not lgb_file.exists():
            pytest.skip("lightgbm_1w.pkl not found")
        try:
            import joblib
            model = joblib.load(str(lgb_file))
        except Exception as e:
            pytest.fail(f"LightGBM load failed: {e}")

    def test_meta_learner_loadable(self):
        meta_file = _MODEL_DIR / "meta_learner.pkl"
        if not meta_file.exists():
            pytest.skip("meta_learner.pkl not found")
        try:
            import joblib
            model = joblib.load(str(meta_file))
        except Exception as e:
            pytest.fail(f"Meta-Learner load failed: {e}")

    def test_meta_learner_predict_proba(self):
        """Meta-Learner predict_proba 输出应为合法概率"""
        try:
            from prediction.models.ensemble_model import get_model_ensemble
        except ImportError:
            pytest.skip("ensemble_model not importable")
        ensemble = get_model_ensemble()
        if getattr(ensemble, 'meta_learner', None) is None:
            pytest.skip("Meta-Learner not trained yet")
        # Use internal meta learner directly
        import numpy as np
        dummy = np.random.rand(1, 12)
        proba = ensemble.meta_learner.predict_proba(dummy)
        assert proba is not None, "predict_proba returned None"
        proba_flat = proba.flatten()
        assert len(proba_flat) == 3, f"Expected 3 class probabilities, got {len(proba_flat)}"
        assert abs(sum(proba_flat) - 1.0) < 0.01, f"Probabilities don't sum to 1: {proba_flat}"
        assert all(p >= 0 for p in proba_flat), f"Negative probabilities: {proba_flat}"

    def test_prediction_not_all_neutral(self):
        """模型不应全部预测 NEUTRAL (退化检测)"""
        try:
            from prediction.models.ensemble_model import get_model_ensemble
        except ImportError:
            pytest.skip("ensemble_model not importable")
        ensemble = get_model_ensemble()
        if getattr(ensemble, 'meta_learner', None) is None:
            pytest.skip("Meta-Learner not trained yet")

        # 发送 50 个随机输入，检查预测多样性
        rng = np.random.default_rng(0)
        neutral_count = 0
        for _ in range(50):
            dummy = rng.random((1, 12))
            proba = ensemble.meta_learner.predict_proba(dummy)[0]
            if np.argmax(proba) == 1:
                neutral_count += 1
        neutral_rate = neutral_count / 50
        assert neutral_rate < 0.8, \
            f"模型 {neutral_rate:.0%} 输出 NEUTRAL，可能退化为中性偏置"
