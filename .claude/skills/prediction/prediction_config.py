"""
统一预测配置加载器 — 从 .claude/skills/prediction/config/*.json 读取所有预测参数

单例模式，支持热更新。
所有预测/评级/回测/风控/事件模块的硬编码值都从此处读取。
"""
import json
import threading
from pathlib import Path
from typing import Dict, Any, Optional
from loguru import logger


# 配置文件目录: skill 目录下的 config/
_CONFIG_DIR = Path(__file__).resolve().parent / "config"

# 配置文件名 → 属性名映射
_CONFIG_FILES = {
    "stacking": "stacking.json",
    "divergence": "divergence.json",
    "sentiment": "sentiment_weights.json",
    "backtest": "backtest.json",
    "risk_control": "risk_control.json",
    "factor_model": "factor_model.json",
    "macro": "macro_indicator.json",
    "industry_rotation": "industry_rotation.json",
    "event_driven": "event_driven.json",
    "enhanced_features": "enhanced_features.json",
}


class PredictionConfig:
    """
    单例配置加载器 — 从 .claude/skills/prediction/config/*.json 读取所有预测参数。

    用法::

        cfg = PredictionConfig.get_instance()
        weights = cfg.stacking["default_weights"]
        threshold = cfg.divergence["divergence_threshold"]
    """

    _instance: Optional["PredictionConfig"] = None
    _lock = threading.Lock()

    def __init__(self):
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._load_all()

    @classmethod
    def get_instance(cls) -> "PredictionConfig":
        """获取单例实例（线程安全）"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls):
        """重置单例（用于测试或配置热更新）"""
        with cls._lock:
            cls._instance = None

    def reload(self):
        """重新加载所有配置文件"""
        self._cache.clear()
        self._load_all()
        logger.info("PredictionConfig reloaded all config files")

    # ------------------------------------------------------------------ #
    #  配置属性
    # ------------------------------------------------------------------ #

    @property
    def stacking(self) -> Dict[str, Any]:
        return self._get("stacking")

    @property
    def divergence(self) -> Dict[str, Any]:
        return self._get("divergence")

    @property
    def sentiment(self) -> Dict[str, Any]:
        return self._get("sentiment")

    @property
    def backtest(self) -> Dict[str, Any]:
        return self._get("backtest")

    @property
    def risk_control(self) -> Dict[str, Any]:
        return self._get("risk_control")

    @property
    def factor_model(self) -> Dict[str, Any]:
        return self._get("factor_model")

    @property
    def macro(self) -> Dict[str, Any]:
        return self._get("macro")

    @property
    def industry_rotation(self) -> Dict[str, Any]:
        return self._get("industry_rotation")

    @property
    def event_driven(self) -> Dict[str, Any]:
        return self._get("event_driven")

    @property
    def enhanced_features(self) -> Dict[str, Any]:
        return self._get("enhanced_features")

    # ------------------------------------------------------------------ #
    #  内部方法
    # ------------------------------------------------------------------ #

    def _get(self, name: str) -> Dict[str, Any]:
        """从缓存获取配置，若缓存为空则重新加载"""
        if name not in self._cache:
            self._load_one(name)
        return self._cache.get(name, {})

    def _load_all(self):
        """加载所有配置文件"""
        for name in _CONFIG_FILES:
            self._load_one(name)

    def _load_one(self, name: str):
        """加载单个配置文件"""
        filename = _CONFIG_FILES.get(name)
        if not filename:
            logger.warning(f"Unknown config name: {name}")
            return

        filepath = _CONFIG_DIR / filename
        if not filepath.exists():
            logger.warning(f"Config file not found: {filepath}")
            self._cache[name] = {}
            return

        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._cache[name] = data
            logger.debug(f"Loaded prediction config: {filename}")
        except (json.JSONDecodeError, IOError) as e:
            logger.error(f"Failed to load {filepath}: {e}")
            self._cache[name] = {}

    @property
    def config_dir(self) -> Path:
        """返回配置文件目录路径"""
        return _CONFIG_DIR


def get_prediction_config() -> PredictionConfig:
    """便捷函数: 获取 PredictionConfig 单例"""
    return PredictionConfig.get_instance()
