---
name: agent_deploy_search
readonly: true
description: "【功能】搜索 GitHub 上适合 Agent 部署的开源项目 【数据源】GitHub REST API 【输出数据】项目名称、描述、星标数、克隆地址、部署提示（Docker/K8s/CI/CD）、Agent 相关性评分 【耗时】~5-15秒 【适用场景】用户问'搜索Agent项目'、'找部署框架'、'GitHub Agent搜索'时使用"
---

## 调用方式

通过 `bash` 工具执行：

```bash
echo '{...json参数...}' | python3 /app/.claude/skills/agent_deploy_search/scripts/agent_deploy_search.py
```

或 heredoc（推荐，避免引号转义问题）：

```bash
python3 /app/.claude/skills/agent_deploy_search/scripts/agent_deploy_search.py <<'EOF'
{...json参数...}
EOF
```


# Agent 部署项目搜索

## 概述
搜索 GitHub 上适合 Agent 部署的开源项目，带有相关性评分和部署方式识别。

## 数据源
- GitHub REST API (https://api.github.com/search/repositories)
- GitHub Contents API (获取部署文件列表)

## 环境变量
- GITHUB_TOKEN: GitHub Personal Access Token（推荐，提高 API 限流）

## 参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| query | string | 是 | 搜索关键词 |
| language | string | 否 | 编程语言过滤，默认 Python |
| sort_by | string | 否 | 排序方式，默认 stars |
| max_results | number | 否 | 最大返回数量，默认 10 |
| min_stars | number | 否 | 最低星标数过滤，默认 0 |
| deployment_ready | boolean | 否 | 是否只返回有部署文件的项目，默认 false |
| get_details | boolean | 否 | 是否获取部署文件详情，默认 false |
| github_token | string | 否 | GitHub Token，也可通过环境变量 GITHUB_TOKEN 提供 |

## 调用示例

```json
{
  "query": "LLM agent deployment framework",
  "language": "python",
  "sort_by": "stars",
  "max_results": 10,
  "min_stars": 100,
  "deployment_ready": true,
  "get_details": true
}
```
