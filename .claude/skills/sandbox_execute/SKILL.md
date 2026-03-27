---
name: sandbox_execute
display_name: 安全沙箱代码执行
description: |
  【功能】在完全隔离的云端microVM沙箱中执行代码（基于E2B Firecracker）
  【数据源】用户提供的代码片段、Python/JavaScript/Bash脚本
  【输出数据】代码执行stdout/stderr、返回值、执行耗时
  【耗时】~3-30秒(启动<200ms)
  【适用场景】用户说"执行这段代码"、"验证代码"、"安全运行"时使用；与docker_operator区别：sandbox临时隔离(用后销毁)、docker_operator持久化服务器
priority: 80
keywords:
  - 沙箱
  - sandbox
  - 安全执行
  - 隔离执行
  - 代码执行
  - 安全运行
  - 临时环境
  - 代码测试
  - 代码验证
intents:
  - sandbox
  - safe_execute
  - isolated_execution
  - code_test
  - code_verify
time_estimates:
  default:
    min: 3
    max: 30
    desc: "沙箱代码执行"
authority: dynamic_collection
---

## 调用方式

通过 `bash` 工具执行：

```bash
echo '{...json参数...}' | python3 /app/.claude/skills/sandbox_execute/scripts/sandbox_execute.py
```

或 heredoc（推荐，避免引号转义问题）：

```bash
python3 /app/.claude/skills/sandbox_execute/scripts/sandbox_execute.py <<'EOF'
{...json参数...}
EOF
```

# Sandbox Execute Skill

## 执行方式

- 使用 `python3` 直接执行 `scripts/*.py`
- 支持命令行参数或 stdin JSON 输入
- 不使用 `mcp__...` 工具名

安全沙箱代码执行器 - 基于 E2B Firecracker microVM

## 功能概述

在完全隔离的云端微型虚拟机中执行代码：
- 支持 Python、JavaScript、Bash
- 每次执行创建独立沙箱，用后自动销毁
- 启动时间 < 200ms
- 内置常用 Python 数据科学库

## 使用场景

1. **代码验证** - 验证用户提供的代码片段是否正确
2. **数据分析实验** - 在隔离环境中进行数据分析
3. **教学演示** - 安全执行教学代码示例
4. **代码竞赛** - 运行和评测算法代码

## 与 docker_operator 的区别

| 特性 | sandbox_execute | docker_operator |
|------|----------------|----------------|
| 环境类型 | 临时（用后销毁） | 持久化服务器 |
| 隔离级别 | 完全隔离（microVM） | Docker 容器级别 |
| 状态持久化 | 不支持 | 支持 |
| 定时任务 | 不支持 | 支持 |
| 通知推送 | 不支持 | 支持 |
| 适用场景 | 不可信代码/实验 | 系统级任务/自动化 |

## 参数说明

| 参数 | 类型 | 必填 | 描述 |
|------|------|------|------|
| code | string | 是 | 要执行的代码 |
| language | string | 否 | 语言: python (默认), javascript, bash |
| timeout | integer | 否 | 超时时间(秒), 默认 30 |

## 调用示例

```json
{
  "code": "import pandas as pd\ndf = pd.DataFrame({'stock': ['600519.SH', '000858.SZ'], 'pe': [28.5, 22.3]})\nprint(df.describe())",
  "language": "python",
  "timeout": 30
}
```
