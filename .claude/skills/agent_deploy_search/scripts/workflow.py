"""
Agent 部署项目搜索 - 工作流执行脚本
基于 github_project_search 技能，添加 Agent 相关性评分和过滤
"""

import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional


class AgentDeployWorkflow:
    """Agent 部署项目搜索工作流"""

    # Agent 相关关键词
    AGENT_KEYWORDS = [
        'agent', 'llm', 'ai', 'langchain', 'llamaindex',
        'autonomous', 'gpt', 'openai', 'anthropic', 'claude',
        'crewai', 'auto-gpt', 'babyagi', 'semantic kernel',
        'chainlit', 'streamlit', 'gradio', 'fastapi',
        'vector', 'embedding', 'rag', 'retrieval'
    ]

    # 主流技术栈
    MAINSTREAM_LANGUAGES = ['Python', 'TypeScript', 'JavaScript', 'Go', 'Rust']

    async def execute(
        self,
        query: str,
        language: str = "Python",
        sort_by: str = "stars",
        max_results: int = 10,
        get_details: bool = False,
        min_stars: int = 0,
        deployment_ready: bool = False,
        github_search_skill=None,
    ) -> Dict[str, Any]:
        """
        执行工作流

        Args:
            query: 搜索关键词
            language: 编程语言
            sort_by: 排序方式
            max_results: 最大结果数
            get_details: 是否获取详情
            min_stars: 最小星标数
            deployment_ready: 只返回部署就绪的项目
            github_search_skill: github_project_search 技能实例

        Returns:
            工作流执行结果
        """
        # Step 1: 调用 github_project_search 搜索项目
        search_results = await self._search_projects(
            query, language, sort_by, max_results, get_details, github_search_skill
        )

        if not search_results or not search_results.get('projects'):
            return self._empty_result(query)

        # Step 2: 过滤和评分
        filtered_projects = self._filter_and_score(
            search_results['projects'],
            min_stars,
            deployment_ready
        )

        # Step 3: 按相关性排序
        sorted_projects = sorted(
            filtered_projects,
            key=lambda p: p.get('relevance_score', 0),
            reverse=True
        )

        # Step 4: 生成部署指南
        deployment_guide = None
        if get_details:
            deployment_guide = self._generate_deployment_guide(sorted_projects)

        # Step 5: 统计信息
        stats = self._calculate_stats(sorted_projects, query)

        return {
            'projects': sorted_projects[:max_results],
            'stats': stats,
            'deployment_guide': deployment_guide,
            'total_found': len(search_results['projects']),
            'agent_relevant_count': len(sorted_projects)
        }

    async def _search_projects(
        self,
        query: str,
        language: str,
        sort_by: str,
        max_results: int,
        get_details: bool,
        github_search_skill
    ) -> Optional[Dict[str, Any]]:
        """调用 github_project_search 搜索项目"""
        if not github_search_skill:
            # 如果没有提供技能实例，返回模拟数据用于测试
            return self._mock_search_results(query, max_results)

        # 调用技能（实际使用时）
        return await github_search_skill.execute(
            query=query,
            language=language,
            sort_by=sort_by,
            max_results=max_results * 2,  # 搜索更多以便过滤
            get_details=get_details
        )

    def _filter_and_score(
        self,
        projects: List[Dict[str, Any]],
        min_stars: int,
        deployment_ready: bool
    ) -> List[Dict[str, Any]]:
        """过滤和评分项目"""
        filtered = []

        for project in projects:
            # 最小星标过滤
            if project.get('stars', 0) < min_stars:
                continue

            # 部署就绪过滤
            if deployment_ready:
                deployment_hints = project.get('deployment_hints', {})
                if not any([
                    deployment_hints.get('has_dockerfile'),
                    deployment_hints.get('has_docker_compose'),
                    deployment_hints.get('has_kubernetes'),
                ]):
                    continue

            # 计算相关性评分
            project['relevance_score'] = self._calculate_relevance_score(project)

            # 添加部署标签
            project['deployment'] = self._get_deployment_tags(project)

            filtered.append(project)

        return filtered

    def _calculate_relevance_score(self, project: Dict[str, Any]) -> int:
        """计算 Agent 相关性评分 (0-100)"""
        score = 0
        text = (
            project.get('name', '') + ' ' +
            project.get('description', '')
        ).lower()

        # 1. 关键词匹配 (30%)
        keyword_matches = sum(1 for kw in self.AGENT_KEYWORDS if kw in text)
        score += min((keyword_matches / len(self.AGENT_KEYWORDS)) * 30, 30)

        # 2. 部署就绪度 (25%)
        deployment_hints = project.get('deployment_hints', {})
        deployment_score = 0
        if deployment_hints.get('has_dockerfile'):
            deployment_score += 10
        if deployment_hints.get('has_docker_compose'):
            deployment_score += 8
        if deployment_hints.get('has_kubernetes'):
            deployment_score += 5
        if deployment_hints.get('has_ci_cd'):
            deployment_score += 2
        score += deployment_score

        # 3. 活跃度 (20%)
        updated_at = project.get('updated_at')
        if updated_at:
            try:
                update_date = datetime.fromisoformat(updated_at.replace('Z', '+00:00'))
                days_since = (datetime.now(update_date.tzinfo) - update_date).days
                if days_since < 30:
                    score += 20
                elif days_since < 90:
                    score += 15
                elif days_since < 180:
                    score += 10
                elif days_since < 365:
                    score += 5
            except:
                pass

        # 4. 星标分数 (15%)
        stars = project.get('stars', 0)
        if stars >= 10000:
            score += 15
        elif stars >= 5000:
            score += 12
        elif stars >= 1000:
            score += 8
        elif stars >= 100:
            score += 4

        # 5. 技术栈匹配 (10%)
        language = project.get('language', '')
        if language in self.MAINSTREAM_LANGUAGES:
            score += 10

        return min(int(score), 100)

    def _get_deployment_tags(self, project: Dict[str, Any]) -> List[str]:
        """获取部署标签"""
        tags = []
        deployment_hints = project.get('deployment_hints', {})

        if deployment_hints.get('has_dockerfile'):
            tags.append('Docker')
        if deployment_hints.get('has_docker_compose'):
            tags.append('Compose')
        if deployment_hints.get('has_kubernetes'):
            tags.append('K8s')
        if deployment_hints.get('has_ci_cd'):
            tags.append('CI/CD')

        return tags if tags else ['Clone']

    def _generate_deployment_guide(self, projects: List[Dict[str, Any]]) -> str:
        """生成部署指南"""
        if not projects:
            return ""

        top_project = projects[0]
        guide = f"""## 🚀 快速部署指南

### 推荐: {top_project.get('full_name', top_project.get('name'))}

**星标**: {top_project.get('stars', 0)} | **语言**: {top_project.get('language', 'N/A')} | **相关性**: {top_project.get('relevance_score', 0)}/100

**描述**: {top_project.get('description', '无描述')}

### 克隆项目

```bash
git clone {top_project.get('clone_url', top_project.get('url', ''))}
cd {top_project.get('name', 'project')}
```

### 部署方式

"""

        deployment_hints = top_project.get('deployment_hints', {})

        if deployment_hints.get('has_dockerfile'):
            guide += "#### Docker 部署\n\n```bash\n# 构建镜像\ndocker build -t agent-app .\n\n# 运行容器\ndocker run -p 8000:8000 agent-app\n```\n\n"

        if deployment_hints.get('has_docker_compose'):
            guide += "#### Docker Compose 部署\n\n```bash\n# 一键启动\ndocker-compose up -d\n```\n\n"

        if deployment_hints.get('has_kubernetes'):
            guide += "#### Kubernetes 部署\n\n```bash\n# 应用配置\nkubectl apply -f k8s/\n\n# 查看状态\nkubectl get pods\n```\n\n"

        guide += """### 环境变量配置

通常需要配置以下环境变量：

```bash
# OpenAI API (如果使用)
OPENAI_API_KEY=your_key_here

# Anthropic API (如果使用)
ANTHROPIC_API_KEY=your_key_here

# 其他配置
DATABASE_URL=your_database_url
LOG_LEVEL=info
```

### 其他推荐项目

"""

        for project in projects[1:4]:
            name = project.get('full_name', project.get('name'))
            stars = project.get('stars', 0)
            guide += f"- **{name}** - {stars} ⭐\n"

        return guide

    def _calculate_stats(self, projects: List[Dict[str, Any]], query: str) -> Dict[str, Any]:
        """计算统计信息"""
        if not projects:
            return {
                'total_found': 0,
                'agent_relevant_count': 0,
                'query': query,
                'avg_stars': 0
            }

        total_stars = sum(p.get('stars', 0) for p in projects)
        avg_stars = total_stars // len(projects)

        return {
            'total_found': len(projects),
            'agent_relevant_count': len([p for p in projects if p.get('relevance_score', 0) > 50]),
            'query': query,
            'avg_stars': avg_stars
        }

    def _empty_result(self, query: str) -> Dict[str, Any]:
        """返回空结果"""
        return {
            'projects': [],
            'stats': {
                'total_found': 0,
                'agent_relevant_count': 0,
                'query': query,
                'avg_stars': 0
            },
            'deployment_guide': None,
            'total_found': 0,
            'agent_relevant_count': 0
        }

    def _mock_search_results(self, query: str, max_results: int) -> Dict[str, Any]:
        """模拟搜索结果（用于测试）"""
        mock_projects = [
            {
                'name': 'langchain',
                'full_name': 'langchain-ai/langchain',
                'description': 'Building applications with LLMs through composability',
                'stars': 85000,
                'language': 'Python',
                'clone_url': 'https://github.com/langchain-ai/langchain.git',
                'url': 'https://github.com/langchain-ai/langchain',
                'updated_at': '2024-02-01T10:00:00Z',
                'deployment_hints': {
                    'has_dockerfile': True,
                    'has_docker_compose': True,
                    'has_kubernetes': False,
                    'has_ci_cd': True
                }
            },
            {
                'name': 'llama-index',
                'full_name': 'run-llama/llama_index',
                'description': 'Data framework for LLM applications',
                'stars': 35000,
                'language': 'Python',
                'clone_url': 'https://github.com/run-llama/llama_index.git',
                'url': 'https://github.com/run-llama/llama_index',
                'updated_at': '2024-01-28T15:30:00Z',
                'deployment_hints': {
                    'has_dockerfile': True,
                    'has_docker_compose': False,
                    'has_kubernetes': False,
                    'has_ci_cd': True
                }
            },
            {
                'name': 'fastapi',
                'full_name': 'tiangolo/fastapi',
                'description': 'FastAPI framework, high performance, easy to learn',
                'stars': 72000,
                'language': 'Python',
                'clone_url': 'https://github.com/tiangolo/fastapi.git',
                'url': 'https://github.com/tiangolo/fastapi',
                'updated_at': '2024-02-05T08:00:00Z',
                'deployment_hints': {
                    'has_dockerfile': True,
                    'has_docker_compose': True,
                    'has_kubernetes': True,
                    'has_ci_cd': True
                }
            }
        ]

        return {'projects': mock_projects[:max_results]}


# 导出工作流类
__all__ = ['AgentDeployWorkflow']
