"""
send_message Skill

轻量级实现：实际发送由 PreToolUse Hook 处理。
本 Skill 只负责验证参数并返回确认。
"""
import json
import sys


def main():
    """MCP Tool 入口：从 stdin 读取参数，输出 JSON 结果"""
    raw = sys.stdin.read().strip()
    if not raw:
        print(json.dumps({"for_llm": {"status": "skipped", "reason": "empty input"}}))
        return

    try:
        params = json.loads(raw)
    except json.JSONDecodeError:
        print(json.dumps({"for_llm": {"status": "error", "reason": "invalid JSON input"}}))
        return

    content = params.get("content", "").strip()
    msg_type = params.get("msg_type", "text")
    title = params.get("title", "分析进展")

    if not content:
        print(json.dumps({"for_llm": {"status": "skipped", "reason": "empty content"}}))
        return

    # 实际消息发送由 PreToolUse Hook 拦截处理
    # 本脚本只做参数验证和返回确认
    result = {
        "for_llm": {
            "status": "sent",
            "msg_type": msg_type,
            "title": title,
            "content_length": len(content),
        }
    }
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
