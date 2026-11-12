import os
import json
import asyncio
import certifi

from dotenv import load_dotenv
from langchain_mcp_adapters.client import MultiServerMCPClient

load_dotenv()



TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")


client = MultiServerMCPClient(
    {
        "tavily": {
            "transport": "streamable_http",
            "url": "https://mcp.tavily.com/mcp/",
            "headers": {"Authorization": f"Bearer {TAVILY_API_KEY}"},
        }
    }
)


async def get_all_tools():
    tools = await client.get_tools()
    print("\n available mcp tools: \n")

    for tool in tools:
        print(tool.name)


tavily_search_tool = None


async def get_tavily_search_tool():
    global tavily_search_tool
    if tavily_search_tool is not None:
        return

    tools = await client.get_tools()
    tavily_search_tool = next(tool for tool in tools if tool.name == "tavily_search")


async def tavily_mcp_search(query: str):
    await get_tavily_search_tool()
    raw = await tavily_search_tool.ainvoke({"query": query, "max_results": 5})
    return _format_results(raw)


def _format_results(raw) -> str:
    """Turn the MCP content blocks into the compact numbered text the prompts expect."""
    if isinstance(raw, str):
        text = raw
    else:
        text = "".join(b.get("text", "") for b in raw if isinstance(b, dict))
    try:
        results = json.loads(text).get("results", [])
    except (ValueError, AttributeError):
        return text[:1500]  # not JSON: keep a bounded amount

    lines = []
    for i, r in enumerate(results[:5], 1):
        snippet = " ".join((r.get("content") or "").split())
        if len(snippet) > 300:
            snippet = snippet[:300].rsplit(" ", 1)[0] + "..."
        lines.append(f"{i}. **{r.get('title', 'Unknown')}**\n  {r.get('url', '')}\n  {snippet}")
    return "\n\n".join(lines)