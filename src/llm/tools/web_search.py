"""web_search 工具：Tavily 直调（AI 摘要前置 + 详细结果，可直接朗读中文）。"""

import httpx

from src.utils import config

_TAVILY_URL = "https://api.tavily.com/search"


async def _web_search(query: str) -> str:
    """Tavily 搜索：AI 摘要前置 + 详细结果（对标 live-2d(2) web-search 插件）。"""
    key = config.cfg.TAVILY_API_KEY
    if not key:
        return "错误：未配置 TAVILY_API_KEY，无法联网搜索。请在 .env 中填入。"

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                _TAVILY_URL,
                json={
                    "query": query,
                    "max_results": 3,
                    "include_answer": True,
                    "search_depth": "basic",
                    "api_key": key,
                },
            )
            response.raise_for_status()
            data = response.json()
    except Exception as e:
        return f"错误：搜索失败（{e}）。请稍后重试。"

    if not data:
        return "错误：搜索没有返回任何结果。"

    parts = []
    ai_answer = data.get("answer")
    if ai_answer:
        parts.append(f"AI答案摘要：{ai_answer}")
        parts.append("")

    results = data.get("results") or []
    if results:
        parts.append("详细搜索结果：")
        for i, result in enumerate(results, 1):
            title = result.get("title") or "无标题"
            content = result.get("content") or "无内容"
            url = result.get("url") or "无来源"
            published = result.get("published_date") or ""
            date_str = f"（发布于 {published}）" if published else ""
            parts.append(f"{i}. 标题：{title}{date_str}")
            parts.append(f"   内容：{content[:1500]}{'...' if len(content) > 1500 else ''}")
            parts.append(f"   来源：{url}")
            parts.append("")
    else:
        parts.append("未找到相关搜索结果。")

    return "\n".join(parts).strip()
