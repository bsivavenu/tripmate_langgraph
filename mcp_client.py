import os
import json
from dotenv import load_dotenv
from langchain_mcp_adapters.client import MultiServerMCPClient

load_dotenv()

TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")
AVIATION_STACK_API_KEY = os.getenv("AVIATIONSTACK_API_KEY")
OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

client = MultiServerMCPClient(
    {
        "tavily": {
            "transport": "streamable_http",
            "url": (
                "https://mcp.tavily.com/mcp/"
                f"?tavilyApiKey={TAVILY_API_KEY}"
            )
        },

         "aviationstack": {
            "transport": "stdio",
            "command": "uvx",
            "args": [
                "aviationstack-mcp"
            ],
            "env": {"AVIATION_STACK_API_KEY": AVIATION_STACK_API_KEY or ""}
        },

    }
)

async def get_all_tools():
    tools = await client.get_tools()
    print("available tools")
    for tool in tools:
        print(tool.name,'\n')

search_tool = None
avaition_tool = {}

async def intialize_mcp():
    global search_tool
    global avaition_tool

    if search_tool is not None and avaition_tool:
        return

    tools = await client.get_tools()

    search_tool = next(tool for tool in tools if tool.name=="tavily_search")

    avaition_tool = {
        tool.name: tool 
        for tool in tools 
        if tool.name!="tavily_search"
    }


async def tavily_mcp_search(query:str):
    await intialize_mcp()
    result = await search_tool.ainvoke(
        {
            "query": query,
            "max_results": 5
        }
    )
    return _format_results(result)


def _format_results(raw) -> str:
    """Compact numbered text from the MCP content blocks (keeps prompts small)."""
    text = raw if isinstance(raw, str) else "".join(
        b.get("text", "") for b in raw if isinstance(b, dict))
    try:
        results = json.loads(text).get("results", [])
    except (ValueError, AttributeError):
        return text[:1500]
    lines = []
    for i, r in enumerate(results[:5], 1):
        snippet = " ".join((r.get("content") or "").split())
        if len(snippet) > 300:
            snippet = snippet[:300].rsplit(" ", 1)[0] + "..."
        lines.append(f"{i}. **{r.get('title', 'Unknown')}**\n  {r.get('url', '')}\n  {snippet}")
    return "\n\n".join(lines)



async def aviation_stack_mcp_call(
    tool_name : str,
    tool_args: dict = None
    ):
    
    tools = await client.get_tools()
    tool =  next(tool for tool in tools if tool.name== tool_name)

    result = await tool.ainvoke(
        tool_args or {}
    )
    return result


