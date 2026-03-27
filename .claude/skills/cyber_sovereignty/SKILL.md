---
name: cyber_sovereignty
readonly: true
description: |
  【功能】分析网络安全威胁态势和数字主权政策动态。
  【输出】APT组织分析、攻击技术分类、网络安全新闻、攻击趋势、威胁评估报告。
  【适用场景】用户问"APT组织分析"、"网络攻击态势"、"DDoS趋势"、"网络安全威胁"时使用。
---

## 调用方式

通过 `bash` 工具执行：

```bash
echo '{...json参数...}' | python3 /app/.claude/skills/cyber_sovereignty/scripts/cyber_sovereignty.py
```

或 heredoc（推荐，避免引号转义问题）：

```bash
python3 /app/.claude/skills/cyber_sovereignty/scripts/cyber_sovereignty.py <<'EOF'
{...json参数...}
EOF
```


# 数字主权与网络威胁态势

## 概述
整合 MITRE ATT&CK 威胁框架和 GDELT 网络安全新闻，分析全球网络安全态势。

## 数据源
- MITRE ATT&CK STIX JSON — APT 组织和攻击技术（公开知识库）
- GDELT DOC API — 网络安全主题新闻
- Cloudflare Radar API — DDoS/L3 攻击趋势（需 CLOUDFLARE_API_TOKEN）

## 参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| country | string | 否 | 目标国家名称（中文） |
| focus | string | 否 | 分析焦点，可选 cyber_threats（默认）或 data_policy |
| days | number | 否 | 分析时间跨度（天），默认 7 |

## 调用示例

```json
{
  "country": "中国",
  "focus": "APT组织",
  "days": 30
}
```
