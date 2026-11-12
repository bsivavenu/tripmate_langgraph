
import asyncio
from mcp_client_test import get_all_tools, tavily_mcp_search


query = "what is the latest news about nvidia"
if __name__ == "__main__":
    print(asyncio.run(tavily_mcp_search(query)))