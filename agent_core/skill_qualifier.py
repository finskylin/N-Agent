"""
Skill Qualifier -- 资源资格检查

职责:
1. 检查 Skill 元数据中 requires 字段声明的依赖是否满足
2. 支持 4 项检查: os / bins / env / python_packages
3. 结果缓存，避免重复检查
4. 支持 skip_qualification 全局开关

不修改任何 Skill 数据，仅做「是否可用」的判定。
"""
import os
import sys
import shutil
import importlib.util
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from loguru import logger


@dataclass
class QualifyResult:
    """资格检查结果"""
    qualified: bool
    skill_name: str
    reason: str = ""
    missing_requirements: List[str] = field(default_factory=list)


class SkillQualifier:
    """
    Skill 资源资格检查器

    根据 SkillMetadata.requires 字段检查当前环境是否满足运行条件。

    requires 字段格式:
        {
            "os": ["linux", "darwin"],
            "bins": ["ffmpeg", "git"],
            "env": ["OPENAI_API_KEY"],
            "python_packages": ["pandas", "numpy"]
        }

    用法:
        qualifier = SkillQualifier(config)
        result = qualifier.qualify(skill_meta)
        if not result.qualified:
            print(f"Skill {result.skill_name} disqualified: {result.reason}")
    """

    def __init__(self, config: Optional[Dict] = None):
        self._config = config or {}
        self._skip = self._config.get("skip_qualification", False)
        self._cache_ttl = self._config.get("cache_ttl_seconds", 300)
        self._cache: Dict[str, Tuple[QualifyResult, float]] = {}

    def qualify(self, skill_meta) -> QualifyResult:
        """
        检查单个 Skill 是否满足资源资格

        Args:
            skill_meta: SkillMetadata 实例（需有 name 和 requires 属性）

        Returns:
            QualifyResult
        """
        name = getattr(skill_meta, "name", "unknown")

        if self._skip:
            return QualifyResult(qualified=True, skill_name=name)

        # 检查缓存
        cached = self._cache.get(name)
        if cached:
            result, ts = cached
            if (time.time() - ts) < self._cache_ttl:
                return result

        requires = getattr(skill_meta, "requires", None)
        if not requires:
            result = QualifyResult(qualified=True, skill_name=name)
            self._cache[name] = (result, time.time())
            return result

        missing = []

        # 1. OS 检查
        os_req = requires.get("os")
        if os_req and isinstance(os_req, list):
            if sys.platform not in os_req:
                missing.append(f"os:{sys.platform} not in {os_req}")

        # 2. 二进制检查
        bins_req = requires.get("bins")
        if bins_req and isinstance(bins_req, list):
            for b in bins_req:
                if not shutil.which(b):
                    missing.append(f"bin:{b}")

        # 3. 环境变量检查
        env_req = requires.get("env")
        if env_req and isinstance(env_req, list):
            for e in env_req:
                if not os.environ.get(e):
                    missing.append(f"env:{e}")

        # 4. Python 包检查
        pkg_req = requires.get("python_packages")
        if pkg_req and isinstance(pkg_req, list):
            for pkg in pkg_req:
                if importlib.util.find_spec(pkg) is None:
                    missing.append(f"python_package:{pkg}")

        if missing:
            result = QualifyResult(
                qualified=False,
                skill_name=name,
                reason=f"Missing: {', '.join(missing)}",
                missing_requirements=missing,
            )
        else:
            result = QualifyResult(qualified=True, skill_name=name)

        self._cache[name] = (result, time.time())
        return result

    def qualify_all(self, skills: list) -> Tuple[List, List]:
        """
        批量检查所有 Skill

        Args:
            skills: SkillMetadata 列表

        Returns:
            (qualified_list, disqualified_list)
        """
        qualified = []
        disqualified = []

        for skill in skills:
            result = self.qualify(skill)
            if result.qualified:
                qualified.append(skill)
            else:
                disqualified.append(skill)
                logger.info(
                    f"[SkillQualifier] Disqualified: {result.skill_name} — {result.reason}"
                )

        return qualified, disqualified

    def clear_cache(self):
        """清除缓存"""
        self._cache.clear()
