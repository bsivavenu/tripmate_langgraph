import os

from dotenv import load_dotenv

load_dotenv()

from typing import TypedDict, Annotated
import operator
import uuid
from datetime import date
import psycopg
from psycopg.rows import dict_row

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.postgres import PostgresSaver

from pydantic import BaseModel, Field
from langchain_core.messages import (AnyMessage,
                                    HumanMessage,
                                    AIMessage,
                                    SystemMessage)

from langchain_groq import ChatGroq
from tools.tavily_tool import tavily_search
from tools.flight_tool import search_flights


def get_database_url():
    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        raise ValueError("database url is missing, please add your" \
        "render postgresSQL external databse url to .env file")

    if "sslmode=" not in database_url:
        separator = "&" if "?" in database_url else "?"
        database_url = f"{database_url}{separator}sslmode=require"

    return database_url

MAX_TOKENS = 3500
TEMPERATURE = 0.3


def build_llm():
    """Chat model chosen by LLM_PROVIDER in .env: 'groq' (default) or 'openai'."""
    provider = os.getenv("LLM_PROVIDER", "groq").lower()

    if provider == "openai":
        # Imported lazily so Groq-only setups don't need langchain-openai installed.
        from langchain_openai import ChatOpenAI

        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY is missing")
        return ChatOpenAI(
            model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            api_key=api_key,
            temperature=TEMPERATURE,
            max_tokens=MAX_TOKENS,
            max_retries=6,
        )

    if provider != "groq":
        raise ValueError(f"Unknown LLM_PROVIDER '{provider}' (use 'groq' or 'openai')")

    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise ValueError("GROQ API KEY is missing")
    return ChatGroq(
        model=os.getenv("GROQ_MODEL", "openai/gpt-oss-120b"),
        api_key=api_key,
        temperature=TEMPERATURE,
        max_tokens=MAX_TOKENS,
        max_retries=6,
    )


llm = build_llm()


def _looks_degenerate(text: str) -> bool:
    """True if the output is mostly one repeated token (e.g. '> > > > ...')."""
    words = text.split()
    if len(words) < 50:
        return False
    most_common = max(set(words), key=words.count)
    return words.count(most_common) / len(words) > 0.3


def invoke_llm(messages):
    """llm.invoke with a guard: if the reply was cut off (finish_reason 'length')
    or is a repetition loop, retry once asking for a shorter answer."""
    response = llm.invoke(messages)

    def bad(r):
        finish = (r.response_metadata or {}).get("finish_reason")
        return finish == "length" or _looks_degenerate(str(r.content))

    if bad(response):
        retry_note = HumanMessage(
            content="Your previous reply was cut off or repeated itself. "
                    "Answer again, more concisely, in plain Markdown, and finish the answer."
        )
        response = llm.invoke(list(messages) + [retry_note])
        if bad(response):
            raise RuntimeError("The model output was cut off or looped twice; try again.")

    return response

# defining the state

class TravelState(TypedDict):
    messages: Annotated[list[AnyMessage], operator.add]
    user_query : str
    flight_results : str
    hotel_results : str
    itinerary: str
    llm_calls: int
    destination: str
    budget_level: str

# Rules shared by the itinerary and final prompts. The flight and hotel data
# are included further down in each prompt.
PLANNING_RULES = """Rules:
- Use only the flight and hotel data provided below.
- If a fare, airline or hotel price is not in that data, write "price not available". Do not invent exact prices.
- Ballpark estimates are allowed only when clearly labelled "estimate".
- Trip length must match the user's request exactly (N days = N-1 nights unless stated otherwise).
- Assume 1 traveller unless the user states otherwise.
- All totals must add up and stay within the user's stated budget.
- Suggest example travel dates at least 8 weeks after today's date.
- The itinerary must contain exactly N days, with no extra day.
- Do not state weekday names.
- The flight data shows live flights near today, not schedules for future dates. Show them only as examples of airlines and flight numbers. Do not attach future dates or invent return flights.
"""

class TripRequest(BaseModel):
    """Structured fields pulled out of the user's free-text request."""
    destination: str = Field(description="Destination city or country, e.g. 'Seoul'")
    budget_level: str = Field(
        description="One of: budget, mid-range, luxury. Infer from the stated budget, default mid-range."
    )


def parse_request(state: TravelState):
    """Turn free text into structured fields so the search tools get clean queries.
    Falls back to empty fields (and the old raw-query behaviour) if parsing fails."""
    try:
        parser = llm.with_structured_output(TripRequest)
        trip = parser.invoke([
            SystemMessage(content="Extract trip details from the user's request."),
            HumanMessage(content=state["user_query"]),
        ])
        return {
            "destination": trip.destination.strip(),
            "budget_level": trip.budget_level.strip().lower(),
            "llm_calls": state.get("llm_calls", 0) + 1,
        }
    except Exception:
        return {"destination": "", "budget_level": ""}


def flight_agent(state: TravelState):
    query = state["user_query"]
    flight_data = search_flights(query, compact=True)

    return {
        "flight_results": flight_data,
        "messages": [
            AIMessage(content="Flight results fetched.")
        ],
    }

def hotel_agent(state: TravelState):
    destination = state.get("destination")
    if destination:
        level = state.get("budget_level") or "mid-range"
        query = f"best {level} hotels in {destination} with price per night"
    else:  # parsing failed: fall back to the raw request
        query = f"Best hotels for {state['user_query']}"
    hotel_results = tavily_search(query)

    return {
        "hotel_results": hotel_results,
        "messages": [
            AIMessage(content="Hotel information fetched")
        ],
    }


def itinerary_agent(state: TravelState):
    
    prompt = f"""Create ONLY the day-by-day plan for the user's request below.
    Do not include a budget summary, flight tables or general tips; those are written later.
    Keep each day to 4-6 short bullet points, with approximate costs in INR where useful.
    Today's date: {date.today().isoformat()}

{PLANNING_RULES}

    User Query: {state['user_query']}
    Flight Results: {state['flight_results']}
    Hotel Results: {state['hotel_results']}

    Make the plan practical and budget aware. Do not claim a flight's arrival time is
    the departure time: use the flight data exactly as given.
       """

    response = invoke_llm([SystemMessage(content="you are an expert travel planner."),
                           HumanMessage(content=prompt)])

    return {
        "itinerary": response.content,
        "messages": [response],
        "llm_calls": state.get("llm_calls",0)+1
    }


def final_agent(state: TravelState):
    final_prompt = f"""
    Generate the final travel response for the user.
    Today's date: {date.today().isoformat()}

{PLANNING_RULES}

Do not name a specific visa scheme (e.g. K-ETA);
say "check visa requirements for your passport with the destination's embassy".

    user Request: {state['user_query']}

    Flights : {state['flight_results']}

    Hotels:  {state['hotel_results']}

    Day-by-day plan (reuse it as section 4, do not re-plan it): {state['itinerary']}

    Format the final answer beautifully using these sections:

    1. Trip summary
    2. Flight information
    3. Hotel Suggestions
    4. Day-by-Day Itinerary
    5. Estimated Budget
    6. Final Recommendations

    Important:
    - Be clear and practical.
    - Mention that live flight API may not provide ticket prices if pricing is unavailable.
    - Keep the response useful for real travel planning.
    """

    response = invoke_llm([
        SystemMessage(content="you are a professional AI travel booking assistant."),
        HumanMessage(content=final_prompt)
    ])

    return {
        "messages": [response],
        "llm_calls": state.get("llm_calls",0)+1
    }



### Build the graph 

graph = StateGraph(TravelState)


graph.add_node("parse_request",parse_request)
graph.add_node("flight_agent",flight_agent)
graph.add_node("hotel_agent",hotel_agent)
graph.add_node("itinerary_agent",itinerary_agent)
graph.add_node("final_agent",final_agent)

graph.add_edge(START,"parse_request")
graph.add_edge("parse_request","flight_agent")
graph.add_edge("flight_agent","hotel_agent")
graph.add_edge("hotel_agent","itinerary_agent")
graph.add_edge("itinerary_agent","final_agent")
graph.add_edge("final_agent",END)


DATABASE_URL = get_database_url()

_conn = psycopg.connect(
    DATABASE_URL,
    autocommit= True,
    row_factory=dict_row
)

checkpointer = PostgresSaver(_conn)
checkpointer.setup()

travel_graph = graph.compile(checkpointer=checkpointer)


def run_travel_agent(user_input:str, thread_id: str| None= None):
    if not thread_id:
        thread_id = f"user_{uuid.uuid4().hex}"

    config = {
    "configurable": {"thread_id": thread_id},
    "run_name": "tripmate",
    "metadata": {"thread_id": thread_id},
    }


    result = travel_graph.invoke(
        {
        "messages": [
            HumanMessage(content=user_input)
        ],
        "user_query": user_input,
        "flight_results" : "",
        "hotel_results": "",
        "itinerary": "",
        "llm_calls": 0,
        "destination": "",
        "budget_level": ""
    },
    config=config
    )

    final_answer = result["messages"][-1].content

    return {
        "thread_id": thread_id,
        "answer": final_answer,
        "flight_results": result.get("flight_results",""),
        "hotel_results": result.get("hotel_results",""),
        "itinerary": result.get("itinerary",""),
        "llm_calls": result.get("llm_calls",0),
    }