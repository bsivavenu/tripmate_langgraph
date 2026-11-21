"""MCP client: one MultiServerMCPClient that talks to three MCP servers.

  - tavily        remote server over HTTP (hotel search)
  - aviationstack third-party server run with uvx over stdio (connected, not used by the graph)
  - weather       our own server, custom_weather_mcp_server.py, run over stdio

Helpers below wrap the tools so the LangGraph steps in backend.py can call them.
"""

import os
import sys
import json
from dotenv import load_dotenv
from langchain_mcp_adapters.client import MultiServerMCPClient

load_dotenv()

TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")
AVIATION_STACK_API_KEY = os.getenv("AVIATIONSTACK_API_KEY")
OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY")

# Absolute path so it works no matter where the app is started from
WEATHER_SERVER_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "custom_weather_mcp_server.py"
)

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

        # Our own weather server, launched as a stdio subprocess.
        # sys.executable = the same Python (venv / Docker) running the app.
        "weather": {
            "transport": "stdio",
            "command": sys.executable,
            "args": [WEATHER_SERVER_PATH],
            "env": {"OPENWEATHER_API_KEY": OPENWEATHER_API_KEY or ""}
        },

    }
)

async def get_all_tools():
    """Print the name of every tool all MCP servers expose (used by test.py to check the connections)."""
    tools = await client.get_tools()
    print("available tools")
    for tool in tools:
        print(tool.name,'\n')

search_tool = None
avaition_tool = {}

async def intialize_mcp():
    """Load the MCP tools once: the Tavily search tool, and every other tool in a dict.

    Note: client.get_tools() starts all three servers. (The name has a typo; kept so imports keep working.)
    """
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
    """Search the web with the Tavily MCP tool (top 5 results) and return compact numbered text for prompts."""
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
        data = json.loads(text)
        results = data.get("results", [])
    except (ValueError, AttributeError):
        return text[:1500]
    # Tavily reports a bad key or a failed search as {"error": ..., "detail": ...}, not as an exception.
    # Raise so callers can fall back instead of silently getting an empty string.
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(f"Tavily search failed: {data.get('error')} - {str(data.get('detail', ''))[:200]}")
    if not results:
        raise RuntimeError("Tavily search returned no results")
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
    """Call one AviationStack MCP tool by name with the given arguments.

    Connected for learning, but not used by the graph (see README).
    """
    
    tools = await client.get_tools()
    tool =  next(tool for tool in tools if tool.name== tool_name)

    result = await tool.ainvoke(
        tool_args or {}
    )
    return result


async def weather_mcp_call(tool_name: str, city: str):
    """Call a tool on our weather server: 'get_current_weather' or 'get_forecast'."""
    tools = await client.get_tools(server_name="weather")
    tool = next(tool for tool in tools if tool.name == tool_name)
    return await tool.ainvoke({"city": city})


def _weather_json(raw) -> dict:
    """MCP returns a list of text blocks; join them and parse the JSON."""
    text = raw if isinstance(raw, str) else "".join(
        b.get("text", "") for b in raw if isinstance(b, dict))
    return json.loads(text)


async def weather_now(city: str) -> str:
    """Current weather as one line, e.g. 'Paris: 15°C (feels 14°C), overcast clouds, ...'"""
    d = _weather_json(await weather_mcp_call("get_current_weather", city))
    return (f"{d['city']}: {d['temperature_c']}°C (feels {d['feels_like_c']}°C), "
            f"{d['condition']}, humidity {d['humidity']}%, wind {d['wind_speed']} m/s")


async def weather_forecast(city: str) -> str:
    """Day-by-day forecast (up to 5 days), one line per day."""
    d = _weather_json(await weather_mcp_call("get_forecast", city))
    lines = [f"{e['date']}: {e['min_c']}-{e['max_c']}°C, {e['condition']}"
             for e in d["forecast"]]
    return f"Forecast for {d['city']}:\n" + "\n".join(lines)

