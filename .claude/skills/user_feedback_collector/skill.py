# -*- coding: utf-8 -*-
"""
用户反馈收集器 Skill
提供持久的反馈收集表单和访问链接
"""

from typing import Optional, Dict, Any, List
from datetime import datetime
import json
import os
import uuid
from pathlib import Path

from .base import Skill, SkillContext, skill


@skill(
    name="user_feedback_collector",
    description="创建持久的用户反馈收集表单，提供可分享的访问链接，支持评分、文本意见、问题报告等多种反馈类型，数据持久化存储",
    category="interaction",
    version="1.0.0",
    author="System",
    timeout=60,
    enable_stream=False
)
class UserFeedbackCollectorSkill(Skill):
    """用户反馈收集器技能"""

    # 存储目录
    _storage_dir = Path("/app/app/data/feedback")
    _forms_dir = _storage_dir / "forms"
    _responses_dir = _storage_dir / "responses"

    def __init__(self):
        super().__init__()
        # 确保目录存在
        self._storage_dir.mkdir(parents=True, exist_ok=True)
        self._forms_dir.mkdir(exist_ok=True)
        self._responses_dir.mkdir(exist_ok=True)

    async def execute(self, context: SkillContext) -> Any:
        """
        执行用户反馈收集

        支持的操作：
        - create_form: 创建新的反馈表单
        - get_form: 获取表单信息
        - list_forms: 列出所有表单
        - get_responses: 获取表单的反馈数据
        - get_stats: 获取统计报告
        """
        action = context.params.get("action", "create_form")

        if action == "create_form":
            return await self._create_form(context)
        elif action == "get_form":
            return await self._get_form(context)
        elif action == "list_forms":
            return await self._list_forms(context)
        elif action == "get_responses":
            return await self._get_responses(context)
        elif action == "get_stats":
            return await self._get_stats(context)
        else:
            raise ValueError(f"Unknown action: {action}")

    async def _create_form(self, context: SkillContext) -> Dict[str, Any]:
        """创建新的反馈表单"""
        params = context.params

        # 生成唯一表单 ID
        form_id = str(uuid.uuid4())[:8]
        form_title = params.get("title", "用户反馈表单")
        form_description = params.get("description", "请留下您的宝贵意见")

        # 反馈类型配置
        feedback_types = params.get("feedback_types", [
            {"type": "rating", "label": "整体评分", "required": True},
            {"type": "text", "label": "您的建议", "required": False},
            {"type": "select", "label": "反馈类型", "options": ["功能建议", "问题报告", "使用体验", "其他"], "required": True}
        ])

        # 创建表单元数据
        form_metadata = {
            "form_id": form_id,
            "title": form_title,
            "description": form_description,
            "feedback_types": feedback_types,
            "created_at": datetime.now().isoformat(),
            "status": "active"
        }

        # 保存表单元数据
        form_file = self._forms_dir / f"{form_id}.json"
        with open(form_file, "w", encoding="utf-8") as f:
            json.dump(form_metadata, f, ensure_ascii=False, indent=2)

        # 生成 HTML 表单页面
        html_content = self._generate_form_html(form_metadata)

        # 保存 HTML 文件
        html_file = self._forms_dir / f"{form_id}.html"
        with open(html_file, "w", encoding="utf-8") as f:
            f.write(html_content)

        # 生成访问链接
        _host = os.environ.get("AGENT_EXTERNAL_HOST", "localhost")
        base_url = params.get("base_url", f"http://{_host}:8000")
        access_url = f"{base_url}/feedback/forms/{form_id}"

        return {
            "success": True,
            "form_id": form_id,
            "title": form_title,
            "access_url": access_url,
            "html_file": str(html_file),
            "created_at": form_metadata["created_at"],
            "qr_code_url": f"{base_url}/feedback/qr/{form_id}",  # 可选：二维码
            "embed_code": f'<iframe src="{access_url}" width="100%" height="600"></iframe>'
        }

    async def _get_form(self, context: SkillContext) -> Dict[str, Any]:
        """获取表单信息"""
        form_id = context.params.get("form_id")
        if not form_id:
            raise ValueError("form_id is required")

        form_file = self._forms_dir / f"{form_id}.json"
        if not form_file.exists():
            raise ValueError(f"Form not found: {form_id}")

        with open(form_file, "r", encoding="utf-8") as f:
            form_metadata = json.load(f)

        return {
            "success": True,
            "form": form_metadata
        }

    async def _list_forms(self, context: SkillContext) -> Dict[str, Any]:
        """列出所有表单"""
        forms = []

        for form_file in self._forms_dir.glob("*.json"):
            with open(form_file, "r", encoding="utf-8") as f:
                form_metadata = json.load(f)

            # 统计反馈数量
            form_id = form_metadata["form_id"]
            response_count = len(list(self._responses_dir.glob(f"{form_id}_*.json")))

            forms.append({
                "form_id": form_id,
                "title": form_metadata["title"],
                "status": form_metadata.get("status", "unknown"),
                "created_at": form_metadata["created_at"],
                "response_count": response_count
            })

        # 按创建时间倒序排序
        forms.sort(key=lambda x: x["created_at"], reverse=True)

        return {
            "success": True,
            "forms": forms,
            "total": len(forms)
        }

    async def _get_responses(self, context: SkillContext) -> Dict[str, Any]:
        """获取表单的反馈数据"""
        form_id = context.params.get("form_id")
        if not form_id:
            raise ValueError("form_id is required")

        limit = context.params.get("limit", 100)

        responses = []
        for response_file in list(self._responses_dir.glob(f"{form_id}_*.json"))[:limit]:
            with open(response_file, "r", encoding="utf-8") as f:
                response_data = json.load(f)
                responses.append(response_data)

        # 按提交时间倒序排序
        responses.sort(key=lambda x: x.get("submitted_at", ""), reverse=True)

        return {
            "success": True,
            "form_id": form_id,
            "responses": responses,
            "total": len(responses)
        }

    async def _get_stats(self, context: SkillContext) -> Dict[str, Any]:
        """获取统计报告"""
        form_id = context.params.get("form_id")
        if not form_id:
            raise ValueError("form_id is required")

        # 获取所有反馈
        result = await self._get_responses(
            SkillContext(params={"form_id": form_id, "limit": 10000})
        )
        responses = result["responses"]

        if not responses:
            return {
                "success": True,
                "form_id": form_id,
                "total_responses": 0,
                "stats": {}
            }

        # 统计分析
        stats = {
            "total_responses": len(responses),
            "by_feedback_type": {},
            "average_rating": None,
            "rating_distribution": {},
            "latest_response": responses[0] if responses else None
        }

        # 按反馈类型统计
        for response in responses:
            feedback_type = response.get("data", {}).get("feedback_type", "unknown")
            stats["by_feedback_type"][feedback_type] = \
                stats["by_feedback_type"].get(feedback_type, 0) + 1

            # 评分统计
            rating = response.get("data", {}).get("rating")
            if rating is not None:
                if stats["average_rating"] is None:
                    stats["average_rating"] = {"sum": 0, "count": 0}
                stats["average_rating"]["sum"] += rating
                stats["average_rating"]["count"] += 1

                rating_key = str(int(rating))
                stats["rating_distribution"][rating_key] = \
                    stats["rating_distribution"].get(rating_key, 0) + 1

        # 计算平均评分
        if stats["average_rating"] and stats["average_rating"]["count"] > 0:
            stats["average_rating_value"] = round(
                stats["average_rating"]["sum"] / stats["average_rating"]["count"], 2
            )
            del stats["average_rating"]

        return {
            "success": True,
            "form_id": form_id,
            "stats": stats
        }

    def _generate_form_html(self, form_metadata: Dict[str, Any]) -> str:
        """生成表单 HTML 页面"""
        form_id = form_metadata["form_id"]
        title = form_metadata["title"]
        description = form_metadata["description"]
        feedback_types = form_metadata["feedback_types"]

        # 生成表单字段 HTML
        fields_html = ""
        for field in feedback_types:
            field_type = field["type"]
            field_label = field["label"]
            field_required = field.get("required", False)
            required_attr = "required" if field_required else ""

            if field_type == "rating":
                fields_html += f'''
                <div class="form-group">
                    <label>{field_label} {"*" if field_required else ""}</label>
                    <div class="rating-stars" id="rating-field">
                        <span class="star" data-value="1">★</span>
                        <span class="star" data-value="2">★</span>
                        <span class="star" data-value="3">★</span>
                        <span class="star" data-value="4">★</span>
                        <span class="star" data-value="5">★</span>
                    </div>
                    <input type="hidden" name="rating" id="rating-input" {required_attr}>
                </div>
                '''
            elif field_type == "text":
                fields_html += f'''
                <div class="form-group">
                    <label>{field_label} {"*" if field_required else ""}</label>
                    <textarea name="{field_label}" rows="4" class="form-control" {required_attr}></textarea>
                </div>
                '''
            elif field_type == "select":
                options_html = "".join([
                    f'<option value="{opt}">{opt}</option>'
                    for opt in field.get("options", [])
                ])
                fields_html += f'''
                <div class="form-group">
                    <label>{field_label} {"*" if field_required else ""}</label>
                    <select name="feedback_type" class="form-control" {required_attr}>
                        <option value="">请选择...</option>
                        {options_html}
                    </select>
                </div>
                '''

        html = f"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>
    <style>
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 20px;
        }}
        .container {{
            background: white;
            border-radius: 16px;
            box-shadow: 0 20px 60px rgba(0,0,0,0.3);
            max-width: 600px;
            width: 100%;
            padding: 40px;
        }}
        h1 {{
            color: #333;
            margin-bottom: 10px;
            font-size: 28px;
        }}
        .description {{
            color: #666;
            margin-bottom: 30px;
            font-size: 14px;
        }}
        .form-group {{
            margin-bottom: 24px;
        }}
        label {{
            display: block;
            color: #333;
            font-weight: 500;
            margin-bottom: 8px;
            font-size: 14px;
        }}
        .form-control {{
            width: 100%;
            padding: 12px 16px;
            border: 2px solid #e1e8ed;
            border-radius: 8px;
            font-size: 14px;
            transition: all 0.3s;
            font-family: inherit;
        }}
        .form-control:focus {{
            outline: none;
            border-color: #667eea;
            box-shadow: 0 0 0 3px rgba(102, 126, 234, 0.1);
        }}
        textarea.form-control {{
            resize: vertical;
            min-height: 100px;
        }}
        .rating-stars {{
            display: flex;
            gap: 8px;
            flex-direction: row-reverse;
            justify-content: flex-end;
        }}
        .star {{
            font-size: 32px;
            color: #ddd;
            cursor: pointer;
            transition: color 0.2s;
            user-select: none;
        }}
        .star:hover,
        .star:hover ~ .star,
        .star.active,
        .star.active ~ .star {{
            color: #ffc107;
        }}
        .btn-submit {{
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            border: none;
            padding: 14px 32px;
            border-radius: 8px;
            font-size: 16px;
            font-weight: 600;
            cursor: pointer;
            width: 100%;
            transition: transform 0.2s, box-shadow 0.2s;
        }}
        .btn-submit:hover {{
            transform: translateY(-2px);
            box-shadow: 0 8px 25px rgba(102, 126, 234, 0.4);
        }}
        .btn-submit:active {{
            transform: translateY(0);
        }}
        .success-message {{
            display: none;
            text-align: center;
            padding: 40px 20px;
        }}
        .success-icon {{
            font-size: 64px;
            margin-bottom: 20px;
        }}
        .success-title {{
            font-size: 24px;
            color: #333;
            margin-bottom: 10px;
        }}
        .success-text {{
            color: #666;
        }}
        .error-message {{
            display: none;
            background: #fee;
            color: #c33;
            padding: 12px 16px;
            border-radius: 8px;
            margin-bottom: 20px;
            font-size: 14px;
        }}
    </style>
</head>
<body>
    <div class="container">
        <div id="form-container">
            <h1>{title}</h1>
            <p class="description">{description}</p>

            <div class="error-message" id="error-message"></div>

            <form id="feedback-form">
                {fields_html}

                <button type="submit" class="btn-submit">提交反馈</button>
            </form>
        </div>

        <div class="success-message" id="success-message">
            <div class="success-icon">✓</div>
            <div class="success-title">感谢您的反馈！</div>
            <div class="success-text">您的意见对我们非常重要</div>
        </div>
    </div>

    <script>
        // 评分星级交互
        const stars = document.querySelectorAll('.star');
        const ratingInput = document.getElementById('rating-input');

        stars.forEach(star => {{
            star.addEventListener('click', function() {{
                const value = this.getAttribute('data-value');
                ratingInput.value = value;

                stars.forEach(s => s.classList.remove('active'));
                this.classList.add('active');
            }});
        }});

        // 表单提交
        document.getElementById('feedback-form').addEventListener('submit', async function(e) {{
            e.preventDefault();

            const formData = new FormData(this);
            const data = {{}};
            formData.forEach((value, key) => {{
                data[key] = value;
            }});

            try {{
                const response = await fetch('/api/v1/feedback/submit', {{
                    method: 'POST',
                    headers: {{
                        'Content-Type': 'application/json'
                    }},
                    body: JSON.stringify({{
                        session_id: '{form_id}_' + Date.now(),
                        component_name: '{form_id}',
                        data_pattern: 'user_feedback',
                        feedback_type: 'explicit',
                        feedback_value: data.rating ? data.rating / 5 : 0.5,
                        context: {{ form_data: data }},
                        metadata: {{ form_id: '{form_id}' }}
                    }})
                }});

                if (response.ok) {{
                    document.getElementById('form-container').style.display = 'none';
                    document.getElementById('success-message').style.display = 'block';
                }} else {{
                    const error = await response.json();
                    document.getElementById('error-message').textContent = error.detail || '提交失败，请稍后重试';
                    document.getElementById('error-message').style.display = 'block';
                }}
            }} catch (error) {{
                document.getElementById('error-message').textContent = '网络错误，请稍后重试';
                document.getElementById('error-message').style.display = 'block';
            }}
        }});
    </script>
</body>
</html>
        """
        return html


# 导出
__all__ = ['UserFeedbackCollectorSkill']
