
import asyncio
# from mcp_client_test import get_all_tools, tavily_mcp_search
from mcp_client import get_all_tools

query = "what is the latest news about nvidia"
if __name__ == "__main__":
    # print(asyncio.run(tavily_mcp_search(query)))
    print(asyncio.run(get_all_tools()))