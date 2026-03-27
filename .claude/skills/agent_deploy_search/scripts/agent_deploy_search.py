"""
Agent Deploy Search Skill
搜索 GitHub 上适合 Agent 部署的开源项目，带有相关性评分。
无跨层 import，所有配置通过环境变量读取。
使用 aiohttp 异步 HTTP 调用替代 requests。
"""
import asyncio
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

try:
    import aiohttp
except ImportError:
    aiohttp = None

try:
    from loguru import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)

DISCLAIMER = "数据来源 GitHub 公开 API，结果仅供参考"

AGENT_KEYWORDS = [
    "agent", "llm", "ai", "langchain", "llamaindex",
    "autonomous", "gpt", "openai", "anthropic", "claude",
    "crewai", "auto-gpt", "babyagi", "semantic kernel",
    "chainlit", "streamlit", "gradio", "fastapi",
    "vector", "embedding", "rag", "retrieval",
]


def _parse_project(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "name": item["name"],
        "full_name": item["full_name"],
        "description": item.get("description") or "无描述",
        "url": item["html_url"],
        "clone_url": item["clone_url"],
        "ssh_url": item["ssh_url"],
        "stars": item["stargazers_count"],
        "forks": item["forks_count"],
        "language": item.get("language") or "未知",
        "updated_at": item["updated_at"],
        "license": item.get("license", {}).get("name", "无许可证") if item.get("license") else "无许可证",
        "topics": item.get("topics", []),
        "default_branch": item.get("default_branch", "main"),
    }


def _calculate_relevance_score(project: Dict[str, Any]) -> int:
    score = 0
    text = (
        project.get("name", "") + " " +
        project.get("description", "") + " " +
        " ".join(project.get("topics", []))
    ).lower()
    keyword_matches = sum(1 for kw in AGENT_KEYWORDS if kw in text)
    score += min((keyword_matches / len(AGENT_KEYWORDS)) * 30, 30)

    updated_at = project.get("updated_at")
    if updated_at:
        try:
            update_date = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            days_since = (now - update_date).days
            if days_since < 30:
                score += 25
            elif days_since < 90:
                score += 20
            elif days_since < 180:
                score += 15
            elif days_since < 365:
                score += 8
        except Exception:
            pass

    stars = project.get("stars", 0)
    if stars >= 10000:
        score += 25
    elif stars >= 5000:
        score += 20
    elif stars >= 1000:
        score += 15
    elif stars >= 500:
        score += 10
    elif stars >= 100:
        score += 5

    language = project.get("language", "")
    if language in ["Python", "TypeScript", "JavaScript", "Go", "Rust"]:
        score += 20

    return min(int(score), 100)


async def _check_deployment_files(session: aiohttp.ClientSession, full_name: str,
                                    headers: Dict[str, str]) -> Dict[str, bool]:
    hints = {"has_dockerfile": False, "has_docker_compose": False, "has_kubernetes": False, "has_ci_cd": False}
    url = f"https://api.github.com/repos/{full_name}/contents"
    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status == 200:
                files = await resp.json(content_type=None)
                if isinstance(files, list):
                    file_names = [f.get("name", "").lower() for f in files if isinstance(f, dict)]
                    hints["has_dockerfile"] = any("dockerfile" in name for name in file_names)
                    hints["has_docker_compose"] = any("docker-compose" in name or "compose.y" in name for name in file_names)
                    hints["has_kubernetes"] = any(name in ["k8s", "kubernetes", "helm", "charts"] for name in file_names)
                    hints["has_ci_cd"] = ".github" in file_names or ".gitlab-ci.yml" in file_names
    except Exception:
        pass
    return hints


def _get_deployment_tags(hints: Dict[str, bool]) -> List[str]:
    tags = []
    if hints.get("has_dockerfile"):
        tags.append("Docker")
    if hints.get("has_docker_compose"):
        tags.append("Compose")
    if hints.get("has_kubernetes"):
        tags.append("K8s")
    if hints.get("has_ci_cd"):
        tags.append("CI/CD")
    return tags if tags else ["Clone"]


def _generate_deployment_guide(projects: List[Dict[str, Any]]) -> str:
    if not projects:
        return ""
    top = projects[0]
    guide = f"## 推荐项目部署指南\n\n### {top.get('full_name')}\n\n"
    guide += f"**星标**: {top.get('stars', 0)} | **语言**: {top.get('language', 'N/A')} | **相关性**: {top.get('relevance_score', 0)}/100\n\n"
    guide += f"**描述**: {top.get('description', '无')}\n\n"
    guide += f"```bash\ngit clone {top.get('clone_url', '')}\ncd {top.get('name', 'project')}\n```\n\n"
    hints = top.get("deployment_hints", {})
    if hints.get("has_dockerfile"):
        guide += "#### Docker 部署\n```bash\ndocker build -t agent-app .\ndocker run -p 8000:8000 agent-app\n```\n\n"
    if hints.get("has_docker_compose"):
        guide += "#### Docker Compose 部署\n```bash\ndocker-compose up -d\n```\n\n"
    guide += "### 其他推荐项目\n\n"
    for p in projects[1:4]:
        guide += f"- **{p.get('full_name')}** - {p.get('stars', 0)} star ({p.get('relevance_score', 0)}/100)\n"
    return guide


async def _run_analysis(params: Dict[str, Any]) -> Dict[str, Any]:
    if aiohttp is None:
        return {"error": "aiohttp 未安装", "for_llm": "Error: aiohttp not installed"}

    query = (params.get("query") or "").strip()
    if not query:
        return {"error": "缺少必需参数 query", "for_llm": "Error: missing query parameter"}

    language = (params.get("language") or "Python").strip()
    sort_by = (params.get("sort_by") or "stars").strip()
    max_results = int(params.get("max_results") or 10)
    min_stars = int(params.get("min_stars") or 0)
    deployment_ready = bool(params.get("deployment_ready") or False)
    get_details = bool(params.get("get_details") or False)

    token = (params.get("github_token") or os.environ.get("GITHUB_TOKEN", "")).strip()

    headers: Dict[str, str] = {"Accept": "application/vnd.github.v3+json"}
    if token:
        headers["Authorization"] = f"token {token}"

    search_query = query
    if language:
        search_query += f" language:{language}"
    if min_stars > 0:
        search_query += f" stars:>={min_stars}"

    url = "https://api.github.com/search/repositories"
    search_params = {
        "q": search_query,
        "sort": sort_by,
        "order": "desc",
        "per_page": max_results * 2,
    }

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, headers=headers, params=search_params, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status == 403:
                    return {"error": "GitHub API 限流，请设置 GITHUB_TOKEN 环境变量", "projects": [],
                            "for_llm": "GitHub API rate limit exceeded"}
                if resp.status != 200:
                    text = await resp.text()
                    return {"error": f"GitHub API {resp.status}: {text[:200]}", "projects": [],
                            "for_llm": f"GitHub API error {resp.status}"}
                data = await resp.json(content_type=None)
        except Exception as e:
            return {"error": f"API 请求失败: {str(e)}", "projects": [], "for_llm": str(e)}

        items = data.get("items", [])
        projects = []
        deployment_tasks = []

        for item in items:
            project = _parse_project(item)
            project["relevance_score"] = _calculate_relevance_score(project)
            deployment_tasks.append(_check_deployment_files(session, item["full_name"], headers))
            projects.append(project)

        # Parallel deployment file checks
        deployment_results = await asyncio.gather(*deployment_tasks, return_exceptions=True)
        for i, (project, hints_result) in enumerate(zip(projects, deployment_results)):
            hints = hints_result if not isinstance(hints_result, Exception) else {}
            project["deployment_hints"] = hints
            project["deployment"] = _get_deployment_tags(hints)

        # Filter deployment-ready projects
        if deployment_ready:
            projects = [p for p in projects if any([
                p["deployment_hints"].get("has_dockerfile"),
                p["deployment_hints"].get("has_docker_compose"),
                p["deployment_hints"].get("has_kubernetes"),
            ])]

        # Sort by relevance and trim
        projects.sort(key=lambda x: x.get("relevance_score", 0), reverse=True)
        projects = projects[:max_results]

        agent_relevant = [p for p in projects if p.get("relevance_score", 0) > 50]
        avg_stars = sum(p.get("stars", 0) for p in projects) // max(len(projects), 1)

        deployment_guide = ""
        if get_details and projects:
            deployment_guide = _generate_deployment_guide(projects)

    for_llm = (
        f"GitHub Agent 项目搜索完成：查询='{query}'，找到 {data.get('total_count', 0)} 个结果，"
        f"返回 {len(projects)} 个，其中 {len(agent_relevant)} 个 Agent 高度相关（评分>50）。"
    )

    return {
        "query": query,
        "total_found": data.get("total_count", 0),
        "agent_relevant_count": len(agent_relevant),
        "avg_stars": avg_stars,
        "projects": projects,
        "deployment_guide": deployment_guide,
        "generated_at": datetime.now().isoformat(),
        "for_llm": for_llm,
        "disclaimer": DISCLAIMER,
    }


def main(params: Dict[str, Any]) -> Dict[str, Any]:
    return asyncio.run(_run_analysis(params))


if __name__ == "__main__":
    import sys
    import json as _json

    if len(sys.argv) > 1:
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("--query", default="")
        parser.add_argument("--language", default="Python")
        parser.add_argument("--sort_by", default="stars")
        parser.add_argument("--max_results", type=int, default=10)
        parser.add_argument("--min_stars", type=int, default=0)
        parser.add_argument("--deployment_ready", action="store_true")
        parser.add_argument("--get_details", action="store_true")
        args = parser.parse_args()
        params = {k: v for k, v in vars(args).items() if v}
    else:
        params = _json.loads(sys.stdin.read())

    result = main(params)
    print(_json.dumps(result, ensure_ascii=False))
