"""
API Schema Generator
API Schema 生成器 - 通过调用真实 API 并使用 LLM 自动生成语义描述
"""
import json
import asyncio
from typing import Dict, Any, List, Optional
from dataclasses import dataclass
from pathlib import Path
from loguru import logger

from app.agent.api_tools import APIToolWrapper
from app.agent.api_registry import api_registry, APIEndpoint


@dataclass
class GeneratedSchema:
    """LLM 生成的 Schema"""
    api_name: str
    description: str
    ui_hint: str
    field_descriptions: Dict[str, str]
    field_aliases: Dict[str, List[str]]
    response_example: Dict[str, Any]


class APISchemaGenerator:
    """
    API Schema 生成器
    通过调用真实 API 获取数据，然后使用 LLM 生成语义描述
    """
    
    # 可用的 UI 组件类型
    AVAILABLE_UI_HINTS = [
        "rating_card", "prediction_card", "stock_info_card",
        "kline_chart", "price_chart", "money_flow_chart",
        "valuation_gauge", "technical_panel", "news_list",
        "data_table", "report_document", "plan_steps", "text_card"
    ]
    
    # LLM Prompt 模板
    PROMPT_TEMPLATE = """分析以下 API 响应数据，生成配置信息。

API 名称: {api_name}
API 路径: {api_path}
当前描述: {current_description}

响应数据示例:
```json
{response_data}
```

请生成以下信息（严格按 JSON 格式输出，不要添加其他文字）：

{{
    "description": "中文描述（一句话说明 API 用途和返回内容）",
    "ui_hint": "推荐的 UI 组件类型（从以下选择一个最匹配的）: {ui_hints}",
    "field_descriptions": {{
        "字段名": "字段的中文含义"
    }},
    "field_aliases": {{
        "字段名": ["可能的别名1", "可能的别名2"]
    }}
}}
"""
    
    def __init__(
        self,
        api_wrapper: Optional[APIToolWrapper] = None,
        llm_base_url: str = "https://api.openai.com/v1",
        llm_api_key: str = "",
        llm_model: str = "gpt-3.5-turbo"
    ):
        self.api_wrapper = api_wrapper or APIToolWrapper()
        self.llm_base_url = llm_base_url
        self.llm_api_key = llm_api_key
        self.llm_model = llm_model
    
    async def generate_schema(
        self,
        api_name: str,
        sample_params: Optional[Dict[str, Any]] = None
    ) -> Optional[GeneratedSchema]:
        """
        生成单个 API 的 Schema
        
        Args:
            api_name: API 名称
            sample_params: 调用 API 的示例参数
            
        Returns:
            GeneratedSchema: 生成的 Schema，失败返回 None
        """
        # 1. 获取 API 定义
        api = api_registry.get(api_name)
        if not api:
            logger.error(f"API not found: {api_name}")
            return None
        
        # 2. 调用真实 API 获取数据
        try:
            response = await self.api_wrapper.call_api(
                api_name,
                params=sample_params or {}
            )
            
            if "error" in response:
                logger.warning(f"API call failed: {response['error']}")
                # 使用空数据继续
                response = {}
        except Exception as e:
            logger.error(f"Failed to call API {api_name}: {e}")
            response = {}
        
        # 3. 构建 LLM Prompt
        prompt = self.PROMPT_TEMPLATE.format(
            api_name=api_name,
            api_path=api.path,
            current_description=api.description,
            response_data=json.dumps(response, ensure_ascii=False, indent=2)[:2000],  # 限制长度
            ui_hints=", ".join(self.AVAILABLE_UI_HINTS)
        )
        
        # 4. 调用 LLM
        llm_result = await self._call_llm(prompt)
        if not llm_result:
            return None
        
        # 5. 解析结果
        try:
            parsed = json.loads(llm_result)
            return GeneratedSchema(
                api_name=api_name,
                description=parsed.get("description", api.description),
                ui_hint=parsed.get("ui_hint", "data_table"),
                field_descriptions=parsed.get("field_descriptions", {}),
                field_aliases=parsed.get("field_aliases", {}),
                response_example=response
            )
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse LLM result: {e}")
            return None
    
    async def batch_generate(
        self,
        api_params_map: Dict[str, Dict[str, Any]]
    ) -> Dict[str, GeneratedSchema]:
        """
        批量生成多个 API 的 Schema
        
        Args:
            api_params_map: {api_name: sample_params} 映射
            
        Returns:
            Dict[str, GeneratedSchema]: 生成结果
        """
        results = {}
        
        for api_name, params in api_params_map.items():
            logger.info(f"Generating schema for: {api_name}")
            schema = await self.generate_schema(api_name, params)
            if schema:
                results[api_name] = schema
            
            # 避免 API 限流
            await asyncio.sleep(1)
        
        return results
    
    async def _call_llm(self, prompt: str) -> Optional[str]:
        """调用 LLM API (支持 Deepseek/Qwen 等 OpenAI 兼容接口)"""
        import httpx
        
        if not self.llm_api_key:
            logger.error("LLM API key not configured")
            return None
        
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(
                    f"{self.llm_base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.llm_api_key}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "model": self.llm_model,
                        "messages": [
                            {
                                "role": "system",
                                "content": "你是一个 API 文档专家，负责分析 API 响应数据并生成准确的语义描述。请严格按 JSON 格式输出。"
                            },
                            {
                                "role": "user",
                                "content": prompt
                            }
                        ],
                        "temperature": 0.3,
                        "max_tokens": 1500
                    }
                )
                
                if response.status_code != 200:
                    logger.error(f"LLM API error: {response.status_code} - {response.text}")
                    return None
                
                result = response.json()
                content = result["choices"][0]["message"]["content"]
                
                # 清理可能的 markdown 代码块
                if content.startswith("```"):
                    lines = content.split("\n")
                    content = "\n".join(lines[1:-1])
                
                return content.strip()
                
        except Exception as e:
            logger.error(f"LLM call failed: {e}")
            return None
    
    def apply_schema(self, schema: GeneratedSchema) -> bool:
        """
        将生成的 Schema 应用到 API Registry
        
        Args:
            schema: 生成的 Schema
            
        Returns:
            bool: 是否成功
        """
        api = api_registry.get(schema.api_name)
        if not api:
            return False
        
        # 更新 API 定义
        api.description = schema.description
        api.ui_hint = schema.ui_hint
        api.response_example = schema.response_example
        
        logger.info(f"Applied schema to {schema.api_name}: ui_hint={schema.ui_hint}")
        return True
    
    def export_schemas(
        self,
        schemas: Dict[str, GeneratedSchema],
        output_file: str = "generated_schemas.json"
    ) -> bool:
        """导出生成的 Schema 到 JSON 文件"""
        try:
            output = {}
            for api_name, schema in schemas.items():
                output[api_name] = {
                    "description": schema.description,
                    "ui_hint": schema.ui_hint,
                    "field_descriptions": schema.field_descriptions,
                    "field_aliases": schema.field_aliases,
                    "response_example": schema.response_example
                }
            
            with open(output_file, "w", encoding="utf-8") as f:
                json.dump(output, f, ensure_ascii=False, indent=2)
            
            logger.info(f"Exported {len(schemas)} schemas to {output_file}")
            return True
        except Exception as e:
            logger.error(f"Failed to export schemas: {e}")
            return False


# 便捷函数
async def generate_all_schemas(
    llm_api_key: str,
    llm_base_url: str = "https://api.deepseek.com/v1",
    llm_model: str = "deepseek-chat",
    sample_stock: str = "600519.SH"
) -> Dict[str, GeneratedSchema]:
    """
    为所有已注册的 API 生成 Schema
    
    Args:
        llm_api_key: LLM API Key
        llm_base_url: LLM API URL
        llm_model: LLM 模型名称
        sample_stock: 示例股票代码
    """
    generator = APISchemaGenerator(
        llm_api_key=llm_api_key,
        llm_base_url=llm_base_url,
        llm_model=llm_model
    )
    
    # 构建参数映射
    api_params = {}
    for api in api_registry.get_all():
        params = {}
        # 尝试填充常见参数
        if "ts_code" in str(api.params):
            params["ts_code"] = sample_stock
        api_params[api.name] = params
    
    return await generator.batch_generate(api_params)
