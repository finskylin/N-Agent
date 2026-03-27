#!/bin/bash

# LLM Agent API快速测试脚本

BASE_URL="http://${AGENT_EXTERNAL_HOST:-localhost}:8000"

echo "=================================="
echo "LLM Agent系统 - API测试"
echo "=================================="
echo ""

# 测试用例
test_cases=(
    "获取赖清德的基本信息"
    "@miroflow 获取赖清德的基本信息"
    "获取国盾量子的5日k线"
    "@pptx 生成赖清德的基本信息ppt"
    "深度分析赖清德的信息，生成ppt"
)

echo "选择测试方式:"
echo "1) 测试单个用例"
echo "2) 测试所有用例"
echo "3) 查看系统状态"
echo ""
read -p "请选择 (1-3): " choice

case $choice in
    1)
        echo ""
        echo "可用测试用例:"
        for i in "${!test_cases[@]}"; do
            echo "$((i+1)). ${test_cases[$i]}"
        done
        echo ""
        read -p "请选择测试用例 (1-5): " case_num

        if [[ $case_num -ge 1 && $case_num -le 5 ]]; then
            query="${test_cases[$((case_num-1))]}"
            echo ""
            echo "测试: $query"
            echo "----------------------------------------"
            curl -X POST "$BASE_URL/api/v1/chat/llm-agent" \
                -H "Content-Type: application/json" \
                -d "{
                    \"message\": \"$query\",
                    \"session_id\": $case_num
                }" | jq '.'
        else
            echo "无效选择"
        fi
        ;;

    2)
        echo ""
        echo "开始测试所有用例..."
        echo ""

        for i in "${!test_cases[@]}"; do
            query="${test_cases[$i]}"
            num=$((i+1))

            echo "========================================"
            echo "测试 $num: $query"
            echo "========================================"

            curl -s -X POST "$BASE_URL/chat/llm-agent" \
                -H "Content-Type: application/json" \
                -d "{
                    \"message\": \"$query\",
                    \"session_id\": $num
                }" | jq '.'

            echo ""
            sleep 1
        done

        echo "========================================"
        echo "所有测试完成!"
        echo "========================================"
        ;;

    3)
        echo ""
        echo "系统状态:"
        echo "----------------------------------------"
        curl -s "$BASE_URL/api/v1/chat/llm-agent/status" | jq '.'
        ;;

    *)
        echo "无效选择"
        exit 1
        ;;
esac

echo ""
