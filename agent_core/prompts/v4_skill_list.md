## 可用技能（Skills）

以下是你可以调用的专用技能列表。每个技能通过 `bash` 工具执行，用 `echo '{{JSON}}' | python3 <script_path>` 格式调用。

**调用规则**：
1. 用 `bash` 工具执行，命令格式：`echo '{{...json...}}' | python3 <script_path>`
2. 首次调用某技能前，系统会自动注入该技能的完整使用说明（含参数示例），请仔细阅读后再构造命令
3. JSON 参数中的引号须转义，或使用 heredoc 格式避免 shell 转义问题

{skill_list}
