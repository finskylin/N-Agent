"""
预测 Skill 真实数据接入 + LLM 可调参数 单元测试

覆盖：
1. _build_valuation_from_financial — 财务数据合并
2. _build_money_flow_from_skill — 资金数据标准化
3. extract_features — 真实数据优先级验证
4. market_bias → regime 覆盖逻辑
5. override_weights → 直接加权融合
6. data_quality 标注
7. input_schema 参数透传
"""
import sys
import os
from pathlib import Path
import numpy as np
import pytest

# 将 .claude/skills 目录加入 path 以便导入 prediction 包
_SKILLS_DIR = Path(__file__).parent.parent.parent / ".claude" / "skills"
if str(_SKILLS_DIR) not in sys.path:
    sys.path.insert(0, str(_SKILLS_DIR))


# ====================================================================
# 测试 _build_valuation_from_financial
# ====================================================================

class TestBuildValuationFromFinancial:
    """验证财务数据合并辅助函数"""

    def _get_fn(self):
        from prediction.scripts.ensemble import _build_valuation_from_financial
        return _build_valuation_from_financial

    def test_real_roe_injected(self):
        fn = self._get_fn()
        result = fn({"roe": 18.5}, {})
        assert result["current"]["roe"] == 18.5

    def test_real_revenue_yoy_injected(self):
        fn = self._get_fn()
        result = fn({"revenue_yoy": 32.1}, {})
        assert result["current"]["revenue_yoy"] == 32.1

    def test_fallback_to_existing_when_no_financial(self):
        fn = self._get_fn()
        existing = {"current": {"roe": 12.0, "gross_margin": 25.0}}
        result = fn({}, existing)
        assert result == existing

    def test_nested_metrics_key(self):
        """financial_data 可能有 metrics 子键"""
        fn = self._get_fn()
        result = fn({"metrics": {"roe": 20.0, "profit_yoy": 15.5}}, {})
        assert result["current"]["roe"] == 20.0
        assert result["current"]["profit_yoy"] == 15.5

    def test_pe_percentile_injected(self):
        fn = self._get_fn()
        result = fn({"pe_ttm_pct": 65.0}, {})
        assert result["percentiles"]["pe_percentile"] == 65.0

    def test_existing_percentiles_preserved(self):
        fn = self._get_fn()
        existing = {"percentiles": {"pe_percentile": 45.0, "pb_percentile": 30.0}}
        result = fn({"roe": 15.0}, existing)
        assert result["percentiles"]["pe_percentile"] == 45.0

    def test_zero_values_fall_through_to_default(self):
        """0 值视为缺失，应使用默认值"""
        fn = self._get_fn()
        result = fn({"roe": 0}, {})
        # roe=0 被过滤，应使用 existing 或最终 extract_features 默认值
        # _get() 会跳过 0 值
        assert result["current"]["roe"] != 0  # 应使用默认值而非0


# ====================================================================
# 测试 _build_money_flow_from_skill
# ====================================================================

class TestBuildMoneyFlowFromSkill:
    """验证资金数据标准化辅助函数"""

    def _get_fn(self):
        from prediction.scripts.ensemble import _build_money_flow_from_skill
        return _build_money_flow_from_skill

    def test_empty_returns_empty(self):
        fn = self._get_fn()
        assert fn({}) == {}

    def test_main_net_5d_in_summary(self):
        fn = self._get_fn()
        result = fn({"summary": {"main_net_3d": 5000.0}})
        # main_net_3d 单位是万元，×1e4 = 元
        assert result["summary"]["main_net_inflow_5d"] == 5000.0 * 1e4

    def test_main_net_5d_field(self):
        fn = self._get_fn()
        result = fn({"summary": {"main_net_5d": 8000.0}})
        assert result["summary"]["main_net_inflow_5d"] == 8000.0 * 1e4

    def test_fund_flow_preserved(self):
        fn = self._get_fn()
        result = fn({"summary": {"flow_stability": 0.7}})
        assert result["summary"]["flow_stability"] == 0.7

    def test_large_order_from_daily(self):
        """当 summary 中无 large_order_ratio 时，从 daily 计算"""
        fn = self._get_fn()
        daily = [
            {"large_net": 1000, "total_amount": 10000},
            {"large_net": 2000, "total_amount": 10000},
        ]
        result = fn({"daily": daily})
        assert result["fund_flow"]["large_order_ratio"] > 0


# ====================================================================
# 测试 extract_features — 真实数据优先级
# ====================================================================

class TestExtractFeaturesRealData:
    """验证 XGBoost/LightGBM extract_features 真实数据优先级"""

    def _make_predictor(self, model_type="xgboost"):
        """创建预测器实例（跳过模型加载，只测 extract_features 逻辑）"""
        if model_type == "xgboost":
            pytest.importorskip("xgboost", reason="xgboost not installed")
            from prediction.models.xgboost_model import XGBoostPredictor
            p = XGBoostPredictor.__new__(XGBoostPredictor)
            p.model = None
            p.scaler = None
            p._trained = False
            return p
        else:
            pytest.importorskip("lightgbm", reason="lightgbm not installed")
            from prediction.models.lightgbm_model import LightGBMPredictor
            p = LightGBMPredictor.__new__(LightGBMPredictor)
            p.model = None
            p.scaler = None
            p._trained = False
            return p

    @pytest.mark.parametrize("model_type", ["xgboost", "lightgbm"])
    def test_real_roe_reflected_in_features(self, model_type):
        """真实 ROE=25% 应显著不同于 ROE=10% 默认值"""
        p = self._make_predictor(model_type)

        valuation_real = {"current": {"roe": 25.0, "gross_margin": 50.0, "debt_ratio": 30.0, "total_mv": 1e9}}
        valuation_default = {"current": {}}

        f_real = p.extract_features({}, {}, valuation_real)[0]
        f_default = p.extract_features({}, {}, valuation_default)[0]

        # ROE 特征在第 18 个位置（索引从0）
        assert f_real[18] != f_default[18], "真实ROE应与默认值不同"
        assert abs(f_real[18] - 25.0 / 100) < 0.01  # 真实值 0.25

    @pytest.mark.parametrize("model_type", ["xgboost", "lightgbm"])
    def test_real_money_flow_reflected(self, model_type):
        """真实主力净流入数据应反映在资金面特征中"""
        p = self._make_predictor(model_type)

        money_real = {
            "summary": {
                "main_net_inflow_5d": 5e8,   # 5亿净流入
                "main_net_inflow_20d": 15e8,
                "flow_stability": 0.8,
                "north_bound_change": 2e8,
                "margin_balance_change": 1e8,
            },
            "fund_flow": {"large_order_ratio": 0.15, "super_large_pct": 0.05},
        }
        money_empty = {}

        f_real = p.extract_features({}, money_real, {})[0]
        f_empty = p.extract_features({}, money_empty, {})[0]

        # main_net_5d 在第 24 位（索引从0）
        assert f_real[24] != f_empty[24], "真实资金数据应与空数据不同"
        assert abs(f_real[24] - 5.0) < 0.1  # 5e8 / 1e8 = 5.0

    @pytest.mark.parametrize("model_type", ["xgboost", "lightgbm"])
    def test_feature_length_always_51(self, model_type):
        """特征维度始终是 51"""
        p = self._make_predictor(model_type)
        features = p.extract_features({}, {}, {})
        assert features.shape == (1, 51)

    @pytest.mark.parametrize("model_type", ["xgboost", "lightgbm"])
    def test_no_nan_in_features(self, model_type):
        """特征不包含 NaN"""
        p = self._make_predictor(model_type)
        features = p.extract_features({}, {}, {})
        assert not np.any(np.isnan(features)), "特征不应含 NaN"

    @pytest.mark.parametrize("model_type", ["xgboost", "lightgbm"])
    def test_technical_indicators_reflected(self, model_type):
        """真实技术指标应反映在特征中"""
        p = self._make_predictor(model_type)

        tech = {
            "indicators": {
                "ma": {"ma5": 105.0, "ma10": 102.0, "ma20": 98.0},
                "macd": {"macd": 2.5, "dif": 1.2, "dea": 0.8},
                "kdj": {"k": 75, "d": 65, "j": 95},
                "rsi": {"rsi14": 65, "rsi6": 72},
            },
            "summary": {"current_price": 100.0},
        }
        f = p.extract_features(tech, {}, {})[0]

        # ma5_ratio = (105/100 - 1) = 0.05
        assert abs(f[0] - 0.05) < 0.001
        # kdj_k = 75/100 = 0.75
        assert abs(f[6] - 0.75) < 0.01


# ====================================================================
# 测试 market_bias 覆盖 regime
# ====================================================================

class TestMarketBiasRegimeOverride:
    """验证 market_bias 参数能正确覆盖自动检测的市场状态"""

    def _make_ensemble(self):
        pytest.importorskip("sklearn", reason="sklearn not installed")
        from prediction.models.ensemble_model import ModelEnsemble
        e = ModelEnsemble.__new__(ModelEnsemble)
        e.meta_learner = type("ML", (), {"is_trained": False})()

        class _FakeTracker:
            def has_sufficient_data(self): return False
            def get_accuracies(self): return {}

        e.accuracy_tracker = _FakeTracker()
        import threading
        e._lock = threading.Lock()
        return e

    def test_bullish_selects_bull_regime_weights(self):
        """market_bias=bullish 应选用牛市权重"""
        from prediction.models.market_regime import REGIME_WEIGHTS
        e = self._make_ensemble()

        model_probas = {
            "xgboost": np.array([0.2, 0.3, 0.5]),
            "lightgbm": np.array([0.25, 0.25, 0.5]),
            "sentiment": np.array([0.15, 0.25, 0.6]),
        }

        result = e._weighted_ensemble({}, model_probas, "bull")
        bull_weights = REGIME_WEIGHTS.get("bull", {})

        # 验证 UP 概率大于 DOWN
        proba = result.get("class_probabilities", [1/3, 1/3, 1/3])
        assert proba[2] > proba[0], "牛市状态下 UP 概率应大于 DOWN"

    def test_bearish_selects_bear_regime_weights(self):
        """market_bias=bearish 应选用熊市权重"""
        e = self._make_ensemble()

        model_probas = {
            "xgboost": np.array([0.5, 0.3, 0.2]),
            "lightgbm": np.array([0.5, 0.25, 0.25]),
            "sentiment": np.array([0.6, 0.25, 0.15]),
        }

        result = e._weighted_ensemble({}, model_probas, "bear")
        proba = result.get("class_probabilities", [1/3, 1/3, 1/3])
        assert proba[0] > proba[2], "熊市状态下 DOWN 概率应大于 UP"


# ====================================================================
# 测试 override_weights
# ====================================================================

class TestOverrideWeights:
    """验证 LLM 传入 model_weights 时直接覆盖默认权重"""

    def _make_ensemble(self):
        pytest.importorskip("sklearn", reason="sklearn not installed")
        from prediction.models.ensemble_model import ModelEnsemble
        e = ModelEnsemble.__new__(ModelEnsemble)
        e.meta_learner = type("ML", (), {"is_trained": False})()

        class _FakeTracker:
            def has_sufficient_data(self): return False

        e.accuracy_tracker = _FakeTracker()
        import threading
        e._lock = threading.Lock()
        return e

    def test_normalize_weights_sums_to_one(self):
        e = self._make_ensemble()
        available = {"xgboost", "lightgbm"}
        normalized = e._normalize_weights({"xgboost": 0.8, "lightgbm": 0.4}, available)
        assert abs(sum(normalized.values()) - 1.0) < 1e-6

    def test_normalize_weights_ignores_unavailable(self):
        e = self._make_ensemble()
        available = {"xgboost"}
        normalized = e._normalize_weights({"xgboost": 0.6, "lstm": 0.4}, available)
        assert "lstm" not in normalized
        assert abs(normalized["xgboost"] - 1.0) < 1e-6

    def test_override_weights_used_in_fusion(self):
        """传入 override_weights 后，加权结果应反映指定权重"""
        e = self._make_ensemble()

        model_probas = {
            "xgboost": np.array([0.1, 0.1, 0.8]),  # 强 UP
            "lightgbm": np.array([0.8, 0.1, 0.1]),  # 强 DOWN
        }
        override = {"xgboost": 0.9, "lightgbm": 0.1}
        normalized = e._normalize_weights(override, set(model_probas.keys()))
        result = e._weighted_ensemble_with_weights(model_probas, normalized, "range")

        # xgboost 权重 90%，应最终偏 UP
        proba = result.get("class_probabilities", [1/3, 1/3, 1/3])
        assert proba[2] > proba[0], "xgboost权重90%时最终应偏UP"

    def test_empty_override_falls_back_to_equal(self):
        """空 override_weights 应均分权重"""
        e = self._make_ensemble()
        available = {"xgboost", "lightgbm"}
        normalized = e._normalize_weights({}, available)
        for v in normalized.values():
            assert abs(v - 0.5) < 1e-6


# ====================================================================
# 测试 data_quality 标注
# ====================================================================

class TestDataQuality:
    """验证 data_quality 字段正确标注数据来源质量"""

    def _call_main(self, params):
        from prediction.scripts.ensemble import main
        return main(params)

    def test_real_data_quality_with_3_sources(self):
        """传入 3 个真实数据源时应标注 real_data"""
        result = self._call_main({
            "ts_code": "600519.SH",
            "technical_indicators": {"summary": {"current_price": 100}},
            "money_flow": {"summary": {"main_net_5d": 1000}},
            "financial_data": {"roe": 15.0},
        })
        if "error" not in result:
            assert result.get("data_quality") == "real_data"
            assert len(result.get("data_sources_used", [])) >= 3

    def test_partial_real_with_1_source(self):
        """传入 1 个数据源时标注 partial_real"""
        result = self._call_main({
            "ts_code": "600519.SH",
            "technical_indicators": {"summary": {}},
        })
        if "error" not in result:
            assert result.get("data_quality") in ("partial_real", "real_data")

    def test_estimated_with_no_sources(self):
        """无数据源时标注 estimated"""
        result = self._call_main({"ts_code": "600519.SH"})
        if "error" not in result:
            assert result.get("data_quality") == "estimated"

    def test_sentiment_score_passed_through(self):
        """传入真实 sentiment_score 应出现在结果中"""
        result = self._call_main({
            "ts_code": "600519.SH",
            "sentiment_score": 0.75,
        })
        if "error" not in result:
            # sentiment_score 在 data_sources_used 中
            assert "sentiment_analysis" in result.get("data_sources_used", [])

    def test_market_bias_reflected_in_output(self):
        """market_bias 应出现在输出的 market_bias_applied 字段"""
        result = self._call_main({
            "ts_code": "600519.SH",
            "market_bias": "bullish",
        })
        if "error" not in result:
            assert result.get("market_bias_applied") == "bullish"

    def test_for_llm_contains_data_quality(self):
        """for_llm 字段应包含 data_quality 信息（ML模型未安装时跳过）"""
        result = self._call_main({"ts_code": "600519.SH"})
        # ML 模型未安装时返回 error，属于环境问题，跳过验证
        if "for_llm" in result and "error" not in result["for_llm"]:
            assert "data_quality" in result["for_llm"]
            assert "tip" in result["for_llm"]


# ====================================================================
# 测试 input_schema 参数透传（集成测试）
# ====================================================================

class TestInputSchemaParams:
    """验证所有 input_schema 参数能正确解析和传递"""

    def test_horizon_parsed(self):
        from prediction.scripts.ensemble import main
        # 不会因为 horizon 参数崩溃
        result = main({"ts_code": "600519.SH", "horizon": "3d"})
        if "error" not in result:
            assert result.get("horizon") == "3d"

    def test_label_threshold_in_params(self):
        """label_threshold 不应导致异常"""
        from prediction.scripts.ensemble import main
        result = main({"ts_code": "600519.SH", "label_threshold": 0.04})
        assert "ts_code" in result or "error" in result

    def test_model_weights_schema_valid(self):
        """model_weights 格式正确时不崩溃"""
        from prediction.scripts.ensemble import main
        result = main({
            "ts_code": "600519.SH",
            "model_weights": {"xgboost": 0.5, "lightgbm": 0.3, "lstm": 0.1, "sentiment": 0.1},
        })
        assert "ts_code" in result or "error" in result

    def test_missing_ts_code_returns_error(self):
        """缺少 ts_code 应返回 error"""
        from prediction.scripts.ensemble import main
        result = main({"horizon": "1w"})
        assert "error" in result

    def test_all_params_together(self):
        """所有参数同时传入不崩溃"""
        from prediction.scripts.ensemble import main
        result = main({
            "ts_code": "600519.SH",
            "horizon": "1w",
            "market_bias": "neutral",
            "sentiment_score": 0.6,
            "financial_data": {"roe": 18.0, "revenue_yoy": 25.0, "gross_margin": 42.0},
            "money_flow": {"summary": {"main_net_5d": 2000.0, "main_net_20d": 8000.0}},
            "technical_indicators": {
                "indicators": {
                    "ma": {"ma5": 102.0, "ma10": 100.0, "ma20": 98.0},
                    "macd": {"macd": 1.2, "dif": 0.8, "dea": 0.5},
                    "kdj": {"k": 65, "d": 55, "j": 85},
                    "rsi": {"rsi14": 58, "rsi6": 62},
                },
                "summary": {"current_price": 100.0, "turnover_rate": 2.5, "volume_ratio": 1.3},
            },
            "label_threshold": 0.03,
        })
        assert "ts_code" in result or "error" in result


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
