from tools.tavily_tool import tavily_search


from backend import run_travel_agent
from tools.flight_tool import search_flights
import uuid

# print(search_flights("Plan a 7 days Japan trip from detroit"))

# res = tavily_search(" who is abdul kalam")
# print(res)

user_input = input("enter travel request")
thread_id = f"user_{uuid.uuid4().hex}"

res  = run_travel_agent(user_input, thread_id=thread_id)


print(res["answer"])