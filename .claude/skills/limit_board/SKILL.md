---
name: limit_board
display_name: 涨跌停板池
readonly: true
description: |
  【功能】获取涨跌停/强势/炸板/次新股池数据
  【数据源】AKShare（stock_zt_pool_em等）
  【输出数据】股票代码、名称、涨跌幅、封板资金、连板数、所属板块、首次封板时间
  【耗时】~3s
  【适用场景】用户问"涨停板"、"跌停板"、"涨停家数"、"连板股"、"炸板"时使用
---

## 调用方式

通过 `bash` 工具执行：

```bash
echo '{...json参数...}' | python3 /app/.claude/skills/limit_board/scripts/limit_board.py
```

或 heredoc（推荐，避免引号转义问题）：

```bash
python3 /app/.claude/skills/limit_board/scripts/limit_board.py <<'EOF'
{...json参数...}
EOF
```

# 涨跌停板池技能

获取涨跌停/强势/炸板/次新股池数据，分析当日热门板块和连板趋势。

## 参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| board_type | string | 否 | 板池类型：涨停/跌停/强势/炸板/次新股，默认"涨停" |
| date | string | 否 | 日期，格式 YYYYMMDD，默认当日 |

## 调用示例

```json
{
  "board_type": "涨停",
  "date": "20260316"
}
```
