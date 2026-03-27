# Skills 开发规范

每个 Skill 是 `.claude/skills/` 下一个独立子目录。Agent 启动时扫描所有子目录的 `SKILL.md`，构建工具列表注入 LLM；LLM 决定调用哪个 Skill 后，框架负责执行。

---

## 目录结构

```
.claude/skills/
├── README.md
│
├── quick_search/         # 有脚本的 Skill
│   ├── SKILL.md
│   └── scripts/
│       └── quick_search.py
│
├── doc-coauthoring/      # 无脚本的 Skill（prompt-only）
│   └── SKILL.md
│
└── ...
```

---

## 两种模式

框架通过检测 `scripts/` 目录下是否有 `.py` 文件自动判断，**无需在 SKILL.md 中声明**。

| 模式 | 判断条件 | 执行方式 |
|------|----------|----------|
| **有脚本** | `scripts/*.py` 存在 | 框架通过 `python3 scripts/xxx.py` 执行，stdin 传入 JSON 参数，stdout 读取 JSON 结果 |
| **无脚本（prompt-only）** | 无 `scripts/` 目录 | SKILL.md body 注入 LLM system prompt，LLM 直接回答 |

---

## SKILL.md 字段说明

### Frontmatter（`---` 之间的部分）

这部分由框架解析，影响 Skill 的发现、排序、调度行为。

```yaml
---
# ── 基础（必填）──────────────────────────────────────────────
name: quick_search           # 必填。唯一标识，小写+下划线。框架按此名调用 Skill。
description: |               # 必填。LLM 依据此字段决定是否调用本 Skill，需写清楚：
  轻量搜索引擎查询。           #   - 能做什么
  适用场景：用户说"搜索"、     #   - 适用场景（用户说什么话时触发）
  "查询资料"时使用。           #   - 与相似 Skill 的区别（如有）

# ── 基础（建议填写）──────────────────────────────────────────
display_name: 快速搜索        # 用户可见名称，用于 UI 展示。不填则显示 name。
priority: 85                 # 排序优先级，0-100，越高越靠前。默认 50。
                             #   90+：核心高频（stock_query、quick_search）
                             #   70-85：常用分析（financial_report、document_reader）
                             #   50-65：辅助工具（pdf_preview、xlsx_preview）
                             #   30-49：低频/专项
                             #   10-29：prompt-only 背景知识

# ── 调度提示（选填）──────────────────────────────────────────
readonly: true               # 选填，默认 false。
                             #   true = 只读操作（搜索/查询/读文件），
                             #   同一次 LLM 响应中多个 readonly 工具可并行执行（asyncio.gather）。
                             #   false = 写操作（发消息/生成文件/执行代码），严格顺序执行。

keywords:                    # 选填。触发关键词，辅助 LLM 识别调用时机。
  - 搜索
  - search

intents:                     # 选填。触发意图标签，与 keywords 互补。
  - search
  - 搜索

# ── 数据质量（选填）──────────────────────────────────────────
authority: official_primary  # 选填，默认 "unknown"。数据权威性标注，供 LLM 参考。

cache_ttl: 300               # 选填，默认 300（秒）。session 内相同参数缓存有效期。

key_params:                  # 选填。缓存命中的关键参数名列表。
  - query

llm_strip_fields:            # 选填。返回结果中不传给 LLM 的字段名。
  - raw_html

# ── 前端渲染（选填）──────────────────────────────────────────
ui_components:               # 选填。前端渲染组件配置，无 UI 需求可省略。
  - component: dynamic_card
    priority: 3
    data_hints:
      - has_results

# ── 耗时提示（选填）──────────────────────────────────────────
time_estimates:
  default:
    min: 3
    max: 15
    desc: "搜索中"
---
```

### Body（`---` 之后的部分）

Body 是 **渐进式披露（Progressive Disclosure）** 的核心——LLM 首次调用某 Skill 前，框架自动把该 Skill 的 Body 作为 user message 注入对话，LLM 读完再重新构造调用命令。

- **有脚本的 Skill**：Body **必须** 以 `## 调用方式` 开头，给出 bash stdin JSON 调用示例（见下方规范）。LLM 照着示例构造 bash 命令调用脚本。
- **无脚本的 Skill（prompt-only）**：Body 内容直接作为 LLM 的行为指导，无需调用示例。

#### 有脚本 Skill 的 Body 标准结构

Body 是 LLM 首次调用工具时读取的完整使用手册，**必须按以下顺序组织**：

```markdown
## 调用方式

通过 `bash` 工具执行：

｜```bash
echo '{...json参数...}' | python3 /app/.claude/skills/{skill_name}/scripts/{skill_name}.py
｜```

或 heredoc（推荐，避免引号转义问题）：

｜```bash
python3 /app/.claude/skills/{skill_name}/scripts/{skill_name}.py <<'EOF'
{...json参数...}
EOF
｜```

## 参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| param1 | string | 是 | 参数说明 |
| param2 | number | 否 | 参数说明，默认值 |

## 调用示例

### 场景1：基础用法
｜```json
{"param1": "value1"}
｜```

### 场景2：完整参数
｜```json
{"param1": "value1", "param2": 10}
｜```

## 返回格式

｜```json
{"for_llm": {"result": "..."}}
｜```
```

**必填部分**（缺少任何一项 LLM 都无法正确调用）：
1. `## 调用方式` — bash 调用命令模板，包含容器绝对路径
2. `## 参数` — 完整参数表格（名称、类型、是否必填、说明）
3. `## 调用示例` — 覆盖主要使用场景的完整 JSON 示例（至少 2 个）

**选填部分**：
4. `## 返回格式` — 返回 JSON 结构说明
5. 能力边界、使用要求等补充说明

**路径规则**：容器内绝对路径 `/app/.claude/skills/{skill目录名}/scripts/{脚本名}.py`，不得使用相对路径。

#### description 与 body 的分工（渐进式披露核心原则）

| | description（frontmatter） | body（--- 之后） |
|---|---|---|
| **读取时机** | 每次请求都读，选工具阶段 | 首次调用时注入，执行阶段 |
| **应该写** | 能做什么、适用场景、不适用场景 | 参数表格、调用示例、返回格式 |
| **禁止写** | 参数名、参数格式、JSON 示例、技术库名、API 路径 | — |
| **长度要求** | 2-4 行，简洁 | 不限，详细为佳 |

---

## 废弃字段（写了无效，会被忽略）

| 字段 | 废弃原因 |
|------|----------|
| `input_schema` | **禁止写在 frontmatter**。问答链路不依赖此字段（框架传给 LLM 的 parameters 固定为空 `{}`）。参数说明写在 SKILL.md body 的 `## 参数` 表格中。 |
| `type` | 已改为自动检测 `scripts/*.py` 是否存在 |
| `category` | 无运行时业务逻辑，已删除 |
| `skill_type` | 同 `type` |
| `source_layer` | CLAW 遗留，已移除 |
| `composable` / `compose_from` | CLAW 遗留，已移除 |
| `requires` | CLAW 遗留，已移除 |
| `prompt_budget` / `execution_constraints` | CLAW 遗留，已移除 |
| `confidence_score` | 置信度系统已停用 |

---

## 有脚本的 Skill：脚本规范

### 执行机制（B 方案）

**LLM 通过 `bash` 工具调用 Skill 脚本**，不再使用 function calling 参数传递。

流程：
1. LLM 在 system prompt 中看到 Skill 名称和 description（阶段 1：选工具）
2. LLM 首次决定调用某 Skill 时，框架自动把该 Skill 的 SKILL.md body pre-inject 给 LLM
3. LLM 读完 body 中的 `## 调用方式` 示例，构造 bash 命令：`echo '{...json...}' | python3 /app/.claude/skills/.../scripts/xxx.py`
4. bash 工具执行该命令，stdout JSON 作为工具结果返回给 LLM

脚本接收 stdin JSON，输出 stdout JSON，完全独立，不需要继承任何基类。

### 最小脚本结构

```python
import json
import sys


def main():
    """从 stdin 读取 JSON 参数，执行业务逻辑，输出 JSON 结果到 stdout"""
    # 1. 读取参数
    raw = sys.stdin.read().strip()
    params = json.loads(raw) if raw else {}

    query = params.get("query", "")
    limit = params.get("max_results", 10)

    # 2. 业务逻辑
    results = do_search(query, limit)

    # 3. 输出结果（JSON 格式写到 stdout）
    output = {
        "for_llm": {            # 给 LLM 看的精简数据
            "results": results,
        },
        "for_ui": {             # 给前端渲染的组件（无 UI 时可省略）
            "components": [
                {"component": "dynamic_card", "data": {...}}
            ]
        },
    }
    print(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    main()
```

### 返回格式规范

```json
{
  "for_llm": {
    "summary": "...",
    "items": []
  },
  "for_ui": {
    "components": [
      {"component": "dynamic_card", "data": {}}
    ]
  }
}
```

| 字段 | 说明 |
|------|------|
| `for_llm` | 给 LLM 看的精简结构化数据，控制 token 消耗 |
| `for_ui.components` | 前端渲染组件，无 UI 时整个 `for_ui` 可省略 |

错误时返回：

```json
{
  "status": "error",
  "error": "错误信息"
}
```

### 调试命令

```bash
# 容器内调试（与 LLM 实际调用方式完全一致）
docker exec agent-service bash -c "echo '{\"query\": \"茅台\", \"max_results\": 5}' | python3 /app/.claude/skills/my_skill/scripts/my_skill.py"

# heredoc 方式（推荐，无需转义引号）
docker exec agent-service bash -c "python3 /app/.claude/skills/my_skill/scripts/my_skill.py <<'EOF'
{\"query\": \"茅台\", \"max_results\": 5}
EOF"
```

---

## 独立性要求（强制）

每个 Skill 的脚本必须自给自足，**禁止跨 Skill 导入**：

```python
# ❌ 禁止
from skills.stock_query.scripts.stock_query import something
from skills._shared.utils import fetch_data

# ✅ 正确：所需逻辑直接内联到本脚本
def _fetch_data(url):
    ...
```

多个 Skill 需要相同逻辑时，各自内联一份。**代码重复优于耦合**。

---

## 新建 Skill 检查清单

```
SKILL.md frontmatter
  ✅ name 已填，小写+下划线，全局唯一
  ✅ description 说清了适用场景（用户说什么话时触发）
  ✅ priority 已根据使用频率设置合理值
  ✅ 只读操作（搜索/查询）已标记 readonly: true
  ✅ 有前端展示需求时已配置 ui_components
  ✅ 无废弃字段（type / category / input_schema / skill_type / source_layer 等）

SKILL.md body（有脚本的 Skill 强制要求）
  ✅ 以 `## 调用方式` 开头，包含容器绝对路径的 bash 调用示例
  ✅ 示例路径格式：/app/.claude/skills/{skill_name}/scripts/{script}.py
  ✅ 示例 JSON 参数覆盖主要使用场景
  ✅ 包含参数说明表格

脚本（有脚本的 Skill）
  ✅ scripts/{skill_name}.py 存在
  ✅ 从 stdin 读取 JSON 参数
  ✅ 结果以 JSON 输出到 stdout，包含 for_llm 字段
  ✅ docker exec agent-service bash -c "echo '{...}' | python3 /app/.claude/skills/.../scripts/xxx.py" 可正常运行并输出 JSON
  ✅ 所有 return 语句返回 dict，不使用 SkillResult / SkillStatus 等未定义类
  ✅ 无跨 Skill import
```
