"""
LLM Agent系统使用示例
演示如何使用新的LLM Agent系统进行意图识别和任务执行
"""
import asyncio
import sys
from pathlib import Path

# 添加项目根目录到路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from app.agent.agents.agent_orchestrator import get_orchestrator
from loguru import logger


async def example_basic_usage():
    """基本使用示例"""
    print("=" * 50)
    print("示例1: 基本使用")
    print("=" * 50)

    # 获取编排器
    orchestrator = get_orchestrator()

    # 初始化(首次使用必须)
    await orchestrator.initialize()

    # 处理查询
    result = await orchestrator.process("获取贵州茅台的基本信息")

    # 输出结果
    if result["success"]:
        print(f"✓ 成功!")
        print(f"答案: {result['answer']}")
        print(f"执行时间: {result['execution_time_seconds']:.2f}秒")

        # 查看详细信息
        intent = result["details"]["intent"]
        print(f"\n意图分析:")
        print(f"  - 主要意图: {intent['primary_intent']}")
        print(f"  - 实体: {intent['entities']}")
        print(f"  - 参数: {intent['parameters']}")
    else:
        print(f"✗ 失败: {result.get('error')}")

    # 清理
    await orchestrator.cleanup()


async def example_multiple_queries():
    """多查询示例"""
    print("\n" + "=" * 50)
    print("示例2: 批量处理多个查询")
    print("=" * 50)

    orchestrator = get_orchestrator()
    await orchestrator.initialize()

    queries = [
        "获取赖清德的基本信息",
        "深度分析贵州茅台",
        "预测茅台未来3个月股价"
    ]

    for query in queries:
        print(f"\n查询: {query}")
        result = await orchestrator.process(query)

        if result["success"]:
            print(f"✓ {result['answer'][:100]}...")
            print(f"  耗时: {result['execution_time_seconds']:.2f}秒")
        else:
            print(f"✗ 失败")

    await orchestrator.cleanup()


async def example_with_context():
    """带上下文的示例"""
    print("\n" + "=" * 50)
    print("示例3: 带上下文的查询")
    print("=" * 50)

    orchestrator = get_orchestrator()
    await orchestrator.initialize()

    # 传递额外上下文
    result = await orchestrator.process(
        query="分析它的财务状况",
        context={
            "session_id": 123,
            "user_id": 1,
            "ts_code": "600519.SH"  # 事先知道是茅台
        }
    )

    if result["success"]:
        print(f"✓ {result['answer'][:150]}...")

    await orchestrator.cleanup()


async def example_direct_agent_usage():
    """直接使用Agent示例"""
    print("\n" + "=" * 50)
    print("示例4: 直接使用单个Agent")
    print("=" * 50)

    from app.agent.agents.intent_analysis_agent import IntentAnalysisAgent

    # 创建意图分析Agent
    agent = IntentAnalysisAgent()
    await agent.initialize()

    # 分析查询
    query = "深度分析贵州茅台的财务状况"
    result = await agent.process({"query": query})

    if result["success"]:
        data = result["data"]
        print(f"查询: {query}")
        print(f"✓ 意图: {data['primary_intent']}")
        print(f"✓ 实体: {[e['name'] for e in data['entities']]}")
        print(f"✓ 置信度: {data['confidence']:.2%}")

    await agent.cleanup()


async def example_api_usage():
    """API调用示例"""
    print("\n" + "=" * 50)
    print("示例5: API调用方式")
    print("=" * 50)

    import httpx

    # API端点
    import os
    _host = os.environ.get("AGENT_EXTERNAL_HOST", "localhost")
    url = f"http://{_host}:8000/chat/llm-agent"

    # 请求数据
    payload = {
        "message": "获取国盾量子的5日k线",
        "session_id": 1
    }

    print(f"发送请求到: {url}")
    print(f"查询: {payload['message']}")

    # 注意: 需要先启动服务
    print("\n提示: 请先启动服务 uvicorn app.main:app")
    print("然后取消下面的注释运行:")

    """
    async with httpx.AsyncClient() as client:
        response = await client.post(url, json=payload)
        result = response.json()

        print(f"✓ 答案: {result['text'][:100]}...")
        print(f"✓ 组件数: {len(result['components'])}")
    """


async def main():
    """运行所有示例"""
    print("\n🚀 LLM Agent系统使用示例\n")

    try:
        await example_basic_usage()
        await example_multiple_queries()
        await example_with_context()
        await example_direct_agent_usage()
        await example_api_usage()

        print("\n" + "=" * 50)
        print("✓ 所有示例运行完成!")
        print("=" * 50)

    except Exception as e:
        print(f"\n✗ 错误: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())
