"""
Skill Discovery -- 从 SKILL.md 文件发现和解析技能元数据

从 app/agent/v4/skill_discovery.py 迁移，精简后的版本:
- 删除 7 个 CLAW/unused 字段: body_content, source_layer, requires, prompt_budget,
  execution_constraints, composable, compose_from
- 新增自治化字段: authority, key_params, cache_ttl, confidence_score
- has_script 自动检测，不再区分 skill_type（native/mcp/script/prompt 全部废弃）
"""
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional
from loguru import logger


@dataclass
class SkillMetadata:
    """技能元数据 — 从 SKILL.md YAML frontmatter 解析"""

    # === 必填（或自动检测） ===
    name: str
    display_name: str = ""
    description: str = ""
    has_script: bool = False
    script_paths: List[str] = field(default_factory=list)
    skill_dir: str = ""
    last_modified: float = 0.0

    # === 功能性（可选，有默认值） ===
    priority: int = 50
    intents: List[str] = field(default_factory=list)
    keywords: List[str] = field(default_factory=list)
    ui_components: List[Dict] = field(default_factory=list)
    llm_strip_fields: List[str] = field(default_factory=list)

    # === 元数据自治化（新增，全部可选） ===
    authority: str = "unknown"
    key_params: List[str] = field(default_factory=list)
    cache_ttl: int = 300
    confidence_score: Optional[float] = None

    # === 只读标记（Phase 2 并行执行依赖）===
    readonly: bool = False  # True = 只读工具（搜索/查询/读取），可并行执行

    # === 工具参数 Schema ===
    input_schema: Dict = field(default_factory=dict)  # JSON Schema，控制 LLM 调用时的参数结构

    # === 动态技能（内部使用） ===
    is_dynamic: bool = False
    dynamic_skill_id: Optional[str] = None
    user_id: Optional[str] = None
    is_shared: bool = False

    # === 私有 Skill 所有者 ===
    # "public" 或空 = 系统公共 Skill（所有用户可用）
    # user_id 字符串 = 私有 Skill（仅 owner 可见）
    owner: str = "public"


class SkillDiscovery:
    """
    技能发现器 -- 合并静态 SKILL.md + 动态数据库技能

    用法:
        discovery = SkillDiscovery(skills_dir="/path/to/.claude/skills")
        discovery.scan()
        all_skills = discovery.get_all()
    """

    def __init__(
        self,
        skills_dir: str,
        extra_dirs: Optional[List[str]] = None,
        bundled_dir: Optional[str] = None,
        qualifier=None,
    ):
        self._skills_dir = skills_dir
        self._extra_dirs = extra_dirs or []
        self._bundled_dir = bundled_dir
        self._qualifier = qualifier
        self._static_skills: Dict[str, SkillMetadata] = {}
        self._dynamic_skills: Dict[str, SkillMetadata] = {}
        self._skills: Dict[str, SkillMetadata] = {}
        self._file_timestamps: Dict[str, float] = {}
        self._dynamic_loader = None

    def set_dynamic_loader(self, loader) -> None:
        """设置动态技能加载器"""
        self._dynamic_loader = loader

    def refresh_dynamic_skills(self) -> int:
        """刷新动态技能（从加载器获取）"""
        if not self._dynamic_loader:
            return 0

        dynamic_skills = self._dynamic_loader.get_all_loaded()
        self._dynamic_skills.clear()

        count = 0
        for skill_id, skill_data in dynamic_skills.items():
            metadata = self._parse_dynamic_skill(skill_id, skill_data)
            if metadata:
                self._dynamic_skills[metadata.name] = metadata
                count += 1

        logger.debug(f"[SkillDiscovery] Refreshed {count} dynamic skills")
        return count

    def _parse_dynamic_skill(self, skill_id: str, skill_data: dict) -> Optional[SkillMetadata]:
        """解析动态技能为 SkillMetadata"""
        skill_md = skill_data.get("skill_md", "")
        frontmatter, body = self._split_frontmatter(skill_md)

        if frontmatter:
            yaml_data = self._parse_yaml_simple(frontmatter)
        else:
            yaml_data = {}

        if not yaml_data:
            yaml_data = {}

        name = yaml_data.get("name", skill_data.get("skill_name", skill_id.split(":")[-1]))

        from agent_core.config import _to_bool
        return SkillMetadata(
            name=name,
            display_name=yaml_data.get("display_name", skill_data.get("display_name", name)),
            description=yaml_data.get("description", skill_data.get("description", "")),
            priority=int(yaml_data.get("priority", 50)),
            intents=yaml_data.get("intents", []),
            keywords=yaml_data.get("keywords", []),
            has_script=True,  # 动态技能（workflow/script）均视为可执行
            is_dynamic=True,
            dynamic_skill_id=skill_id,
            user_id=skill_data.get("user_id"),
            is_shared=skill_data.get("is_shared", False),
            authority=yaml_data.get("authority", "unknown"),
            readonly=_to_bool(yaml_data.get("readonly", False)),
            input_schema=yaml_data.get("input_schema", {}),
        )

    def scan(self) -> int:
        """
        扫描所有层级的 SKILL.md，解析元数据，同时刷新动态技能

        扫描顺序（低优先级先扫描，高优先级后覆盖）:
        1. extra_dirs (lowest priority)
        2. bundled_dir
        3. workspace (skills_dir)
        4. user/dynamic (highest priority for dynamic)

        Returns:
            发现的技能数量
        """
        all_static: Dict[str, SkillMetadata] = {}

        # Layer 1: extra_dirs
        for extra_dir in self._extra_dirs:
            scanned = self._scan_single_directory(extra_dir)
            all_static.update(scanned)

        # Layer 2: bundled_dir
        if self._bundled_dir:
            scanned = self._scan_single_directory(self._bundled_dir)
            all_static.update(scanned)

        # Layer 3: workspace (skills_dir)
        workspace_scanned = self._scan_single_directory(self._skills_dir)
        all_static.update(workspace_scanned)

        # Layer 3b: 私有 Skill（skills_dir/users/{user_id}/{skill_name}/）
        private_scanned = self._scan_private_skills(self._skills_dir)
        all_static.update(private_scanned)

        self._static_skills = all_static

        # Layer 4: dynamic skills
        dynamic_count = 0
        if self._dynamic_loader:
            dynamic_count = self.refresh_dynamic_skills()

        # 合并
        self._skills = {**self._static_skills, **self._dynamic_skills}

        # 资格检查
        if self._qualifier:
            qualified, disqualified = self._qualifier.qualify_all(
                list(self._skills.values())
            )
            self._skills = {s.name: s for s in qualified}
            disqualified_names = {s.name for s in disqualified}
            self._static_skills = {
                k: v for k, v in self._static_skills.items()
                if k not in disqualified_names
            }
            self._dynamic_skills = {
                k: v for k, v in self._dynamic_skills.items()
                if k not in disqualified_names
            }

        total = len(self._skills)
        logger.info(
            f"[SkillDiscovery] Total {total} skills "
            f"(static: {len(self._static_skills)}, dynamic: {len(self._dynamic_skills)})"
        )
        return total

    def _scan_single_directory(self, directory: str) -> Dict[str, SkillMetadata]:
        """扫描单个目录下的 */SKILL.md 文件（跳过 users/ 子目录）"""
        skills_path = Path(directory)
        if not skills_path.is_dir():
            return {}

        result: Dict[str, SkillMetadata] = {}
        new_timestamps: Dict[str, float] = {}

        for skill_dir in sorted(skills_path.iterdir()):
            if not skill_dir.is_dir():
                continue

            # 跳过 users/ 目录，私有 Skill 由 _scan_private_skills 单独处理
            if skill_dir.name == "users":
                continue

            skill_md = skill_dir / "SKILL.md"
            if not skill_md.exists():
                continue

            try:
                mtime = skill_md.stat().st_mtime
                new_timestamps[str(skill_md)] = mtime

                metadata = self._parse_skill_md(skill_md, skill_dir)
                if metadata:
                    result[metadata.name] = metadata
            except Exception as e:
                logger.warning(f"[SkillDiscovery] Failed to parse {skill_md}: {e}")

        self._file_timestamps.update(new_timestamps)
        logger.debug(
            f"[SkillDiscovery] Scanned {len(result)} skills from {directory}"
        )
        return result

    def _scan_private_skills(self, skills_dir: str) -> Dict[str, SkillMetadata]:
        """
        扫描 skills_dir/users/{user_id}/{skill_name}/SKILL.md 格式的私有 Skill。
        返回 {skill_name: SkillMetadata}，owner 字段设置为对应 user_id。
        """
        users_path = Path(skills_dir) / "users"
        if not users_path.is_dir():
            return {}

        result: Dict[str, SkillMetadata] = {}
        new_timestamps: Dict[str, float] = {}

        for user_dir in sorted(users_path.iterdir()):
            if not user_dir.is_dir():
                continue
            owner_id = user_dir.name

            for skill_dir in sorted(user_dir.iterdir()):
                if not skill_dir.is_dir():
                    continue
                skill_md = skill_dir / "SKILL.md"
                if not skill_md.exists():
                    continue

                try:
                    mtime = skill_md.stat().st_mtime
                    new_timestamps[str(skill_md)] = mtime

                    metadata = self._parse_skill_md(skill_md, skill_dir)
                    if metadata:
                        metadata.owner = owner_id
                        # 私有 Skill 用 owner:name 作 key，避免与系统 Skill 同名冲突
                        result[f"{owner_id}:{metadata.name}"] = metadata
                except Exception as e:
                    logger.warning(f"[SkillDiscovery] Failed to parse private {skill_md}: {e}")

        self._file_timestamps.update(new_timestamps)
        logger.debug(
            f"[SkillDiscovery] Scanned {len(result)} private skills from {users_path}"
        )
        return result

    def get_all(self, user_id: Optional[str] = None) -> List[SkillMetadata]:
        """
        获取所有技能（静态系统 Skill + 动态 Skill + 私有 Skill），按 priority 降序。

        Args:
            user_id: 当前用户 ID。提供时额外返回该用户的私有 Skill。
        """
        # 系统公共 Skill（owner == "public" 或未设置）
        all_skills = [
            s for s in self._static_skills.values()
            if s.owner in ("public", "", None)
        ]

        # 当前用户的私有 Skill（owner == user_id）
        if user_id is not None:
            for skill in self._static_skills.values():
                if skill.owner == str(user_id):
                    all_skills.append(skill)

        # 动态 Skill（数据库来源，保持原有逻辑）
        for skill in self._dynamic_skills.values():
            if user_id is None:
                if skill.is_shared:
                    all_skills.append(skill)
            else:
                if str(skill.user_id) == str(user_id) or skill.is_shared:
                    all_skills.append(skill)

        return sorted(all_skills, key=lambda s: s.priority, reverse=True)

    def get_private(self, user_id: str) -> List[SkillMetadata]:
        """获取指定用户的所有私有 Skill"""
        return [
            s for s in self._static_skills.values()
            if s.owner == str(user_id)
        ]

    def get_by_name(self, name: str, user_id: Optional[str] = None) -> Optional[SkillMetadata]:
        """
        按名称获取技能元数据。

        查找顺序:
        1. 精确匹配 static_skills[name]（公共 Skill）
        2. 精确匹配 static_skills[user_id:name]（当前用户的私有 Skill）
        3. 遍历 static_skills 找 meta.name == name 且 owner 匹配的私有 Skill
        4. 匹配 dynamic_skills[name]
        """
        # 公共 Skill（直接 key 匹配）
        if name in self._static_skills:
            return self._static_skills[name]

        # 私有 Skill（owner:name key 匹配）
        if user_id:
            prefixed = f"{user_id}:{name}"
            if prefixed in self._static_skills:
                return self._static_skills[prefixed]

        # 回退：遍历找 name 匹配（兼容任何 key 方案）
        for skill in self._static_skills.values():
            if skill.name == name:
                if skill.owner in ("public", "", None):
                    return skill
                if user_id and skill.owner == str(user_id):
                    return skill

        return self._dynamic_skills.get(name)

    def get_executable_skills(self, user_id: Optional[str] = None) -> List[SkillMetadata]:
        """获取所有有脚本的可执行技能（含当前用户的私有 Skill）"""
        return [s for s in self.get_all(user_id=user_id) if s.has_script]

    def build_skills_summary(self, user_id: Optional[str] = None) -> str:
        """
        构建 Skills XML 摘要，注入 system prompt。
        每个 skill 包含 name、description、script_path（供 LLM 用 bash 调用）。
        """
        all_skills = self.get_all(user_id=user_id)
        if not all_skills:
            return ""

        def _esc(s: str) -> str:
            return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        lines = ["<skills>"]
        for meta in all_skills:
            lines.append("  <skill>")
            lines.append(f"    <name>{_esc(meta.name)}</name>")
            desc_first_line = (meta.description or "").split("\n")[0].strip()
            lines.append(f"    <description>{_esc(desc_first_line)}</description>")
            if meta.has_script and meta.script_paths:
                lines.append(f"    <script_path>{_esc(meta.script_paths[0])}</script_path>")
            lines.append("  </skill>")
        lines.append("</skills>")
        return "\n".join(lines)

    def get_prompt_only_skills(self, user_id: Optional[str] = None) -> List[SkillMetadata]:
        """获取仅作为 prompt 知识参考的技能（无脚本）"""
        return [s for s in self.get_all(user_id=user_id) if not s.has_script]

    def get_llm_strip_fields(self, skill_name: str, user_id: Optional[str] = None) -> List[str]:
        """获取指定技能的 LLM 数据预算剥离字段"""
        meta = self.get_by_name(skill_name, user_id=user_id)
        return meta.llm_strip_fields if meta else []

    def needs_reload(self) -> bool:
        """检查是否有 SKILL.md 文件发生变更（含私有 Skill）"""
        skills_path = Path(self._skills_dir)
        if not skills_path.is_dir():
            return False

        for skill_dir in skills_path.iterdir():
            if not skill_dir.is_dir():
                continue
            if skill_dir.name == "users":
                continue
            skill_md = skill_dir / "SKILL.md"
            if not skill_md.exists():
                continue

            current_mtime = skill_md.stat().st_mtime
            cached_mtime = self._file_timestamps.get(str(skill_md), 0.0)

            if current_mtime != cached_mtime:
                logger.info(f"[SkillDiscovery] Change detected: {skill_md}")
                return True

        # 检查私有 Skill 目录
        users_path = skills_path / "users"
        if users_path.is_dir():
            for user_dir in users_path.iterdir():
                if not user_dir.is_dir():
                    continue
                for skill_dir in user_dir.iterdir():
                    if not skill_dir.is_dir():
                        continue
                    skill_md = skill_dir / "SKILL.md"
                    if not skill_md.exists():
                        continue
                    current_mtime = skill_md.stat().st_mtime
                    cached_mtime = self._file_timestamps.get(str(skill_md), 0.0)
                    if current_mtime != cached_mtime:
                        logger.info(f"[SkillDiscovery] Change detected (private): {skill_md}")
                        return True

        return False

    def _parse_skill_md(self, skill_md: Path, skill_dir: Path) -> Optional[SkillMetadata]:
        """解析 SKILL.md 文件的 YAML frontmatter"""
        content = skill_md.read_text(encoding="utf-8")

        frontmatter, body = self._split_frontmatter(content)
        if not frontmatter:
            return SkillMetadata(
                name=skill_dir.name,
                display_name=skill_dir.name,
                skill_dir=str(skill_dir),
                last_modified=skill_md.stat().st_mtime,
            )

        yaml_data = self._parse_yaml_simple(frontmatter)
        if not yaml_data:
            return None

        name = yaml_data.get("name", skill_dir.name)

        # 检测脚本
        scripts_dir = skill_dir / "scripts"
        script_paths = []
        has_script = False
        if scripts_dir.is_dir():
            for py_file in sorted(scripts_dir.glob("*.py")):
                if py_file.name != "__init__.py":
                    script_paths.append(str(py_file))
                    has_script = True

        # 自治化字段解析
        authority = yaml_data.get("authority", "unknown")
        key_params = yaml_data.get("key_params", [])
        cache_ttl = int(yaml_data.get("cache_ttl", 300))
        confidence_score_raw = yaml_data.get("confidence_score")
        confidence_score = float(confidence_score_raw) if confidence_score_raw is not None else None

        # owner 字段: 空/缺省/"public" 均视为公共 Skill
        raw_owner = yaml_data.get("owner", "public") or "public"
        owner = "public" if str(raw_owner).strip().lower() in ("public", "") else str(raw_owner).strip()

        from agent_core.config import _to_bool
        return SkillMetadata(
            name=name,
            display_name=yaml_data.get("display_name", name),
            description=yaml_data.get("description", ""),
            priority=int(yaml_data.get("priority", 50)),
            ui_components=yaml_data.get("ui_components", []),
            intents=yaml_data.get("intents", []),
            keywords=yaml_data.get("keywords", []),
            has_script=has_script,
            script_paths=script_paths,
            skill_dir=str(skill_dir),
            last_modified=skill_md.stat().st_mtime,
            llm_strip_fields=yaml_data.get("llm_strip_fields", []),
            authority=authority,
            key_params=key_params,
            cache_ttl=cache_ttl,
            confidence_score=confidence_score,
            readonly=_to_bool(yaml_data.get("readonly", False)),
            input_schema=yaml_data.get("input_schema", {}),
            owner=owner,
        )

    @staticmethod
    def _split_frontmatter(content: str) -> tuple:
        """分离 YAML frontmatter 和 Markdown body"""
        match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)", content, re.DOTALL)
        if match:
            return match.group(1), match.group(2)
        return None, content

    @staticmethod
    def _parse_yaml_simple(yaml_text: str) -> Optional[dict]:
        """简单的 YAML 解析器"""
        try:
            import yaml
            return yaml.safe_load(yaml_text)
        except ImportError:
            pass

        # Fallback: 手工解析
        result = {}
        lines = yaml_text.split("\n")
        current_key = None
        current_list = None
        current_obj = None
        in_nested_list = False

        i = 0
        while i < len(lines):
            line = lines[i]
            stripped = line.strip()

            if not stripped or stripped.startswith("#"):
                i += 1
                continue

            # 嵌套列表项
            if in_nested_list and re.match(r"^\s+-\s+\w+:", line):
                if current_obj:
                    current_list.append(current_obj)
                current_obj = {}
                m = re.match(r"^\s+-\s+(\w+):\s*(.*)", line)
                if m:
                    k, v = m.group(1), m.group(2).strip()
                    current_obj[k] = _parse_scalar(v) if v else ""
                i += 1
                continue

            # 嵌套对象属性
            if in_nested_list and current_obj is not None and re.match(r"^\s{4,}\w+:", line):
                m = re.match(r"^\s+(\w+):\s*(.*)", line)
                if m:
                    k, v = m.group(1), m.group(2).strip()
                    if not v:
                        sub_list = []
                        i += 1
                        while i < len(lines):
                            sub_line = lines[i].strip()
                            if sub_line.startswith("- "):
                                sub_list.append(sub_line[2:].strip())
                                i += 1
                            else:
                                break
                        current_obj[k] = sub_list
                        continue
                    else:
                        current_obj[k] = _parse_scalar(v)
                i += 1
                continue

            # 顶层 key: value
            m = re.match(r"^(\w[\w_]*):\s*(.*)", line)
            if m:
                if in_nested_list and current_list is not None:
                    if current_obj:
                        current_list.append(current_obj)
                        current_obj = None
                    result[current_key] = current_list
                    in_nested_list = False
                    current_list = None

                key = m.group(1)
                value = m.group(2).strip()
                current_key = key

                if not value:
                    i += 1
                    if i < len(lines):
                        next_stripped = lines[i].strip()
                        if next_stripped.startswith("- "):
                            if re.match(r"^-\s+\w+:", next_stripped):
                                in_nested_list = True
                                current_list = []
                                current_obj = {}
                                m2 = re.match(r"^-\s+(\w+):\s*(.*)", next_stripped)
                                if m2:
                                    current_obj[m2.group(1)] = _parse_scalar(m2.group(2).strip())
                                i += 1
                                continue
                            else:
                                simple_list = []
                                while i < len(lines):
                                    sl = lines[i].strip()
                                    if sl.startswith("- "):
                                        simple_list.append(sl[2:].strip())
                                        i += 1
                                    else:
                                        break
                                result[key] = simple_list
                                continue
                    continue
                else:
                    result[key] = _parse_scalar(value)

            i += 1

        if in_nested_list and current_list is not None:
            if current_obj:
                current_list.append(current_obj)
            result[current_key] = current_list

        return result if result else None


def _parse_scalar(value: str):
    """解析 YAML 标量值"""
    if not value:
        return ""
    if (value.startswith('"') and value.endswith('"')) or \
       (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    if value.lower() in ("true", "yes"):
        return True
    if value.lower() in ("false", "no"):
        return False
    return value
