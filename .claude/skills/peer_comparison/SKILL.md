---
name: peer_comparison
display_name: 同行比较
readonly: true
description: |
  【功能】同行业成长性/估值/杜邦/规模对比分析
  【数据源】AKShare同行比较数据
  【输出数据】同行排名、成长性指标对比、估值水平对比
  【耗时】~5-15秒
  【适用场景】用户问"同行比较"、"行业对比"、"同行排名"时使用
---

## 调用方式

通过 `bash` 工具执行：

```bash
echo '{...json参数...}' | python3 /app/.claude/skills/peer_comparison/scripts/peer_comparison.py
```

或 heredoc（推荐，避免引号转义问题）：

```bash
python3 /app/.claude/skills/peer_comparison/scripts/peer_comparison.py <<'EOF'
{...json参数...}
EOF
```

# 同行比较技能

同行业成长性/估值/杜邦/规模比较分析。

## 参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| ts_code | string | 是 | 股票代码，如 "600519.SH" |

## 调用示例

```json
{
  "ts_code": "600519.SH"
}
```
