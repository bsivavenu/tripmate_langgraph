import os
import asyncio
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
# from tools.tavily_tool import tavily_search
from mcp_client import tavily_mcp_search, aviation_stack_mcp_call
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


def build_llm(role: str = "strong"):
    """Chat model chosen by LLM_PROVIDER in .env: 'groq' (default) or 'openai'.

    role = "fast"   -> cheap model for basic tasks (request parsing, budget extraction)
    role = "strong" -> best model for the itinerary and the final answer
    With OpenAI the model ids come from OPENAI_FAST_MODEL / OPENAI_STRONG_MODEL.
    """
    provider = os.getenv("LLM_PROVIDER", "groq").lower()

    if provider == "openai":
        # Imported lazily so Groq-only setups don't need langchain-openai installed.
        from langchain_openai import ChatOpenAI

        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY is missing")
        if role == "fast":
            model = os.getenv("OPENAI_FAST_MODEL", "gpt-5-nano")
        else:
            model = os.getenv("OPENAI_STRONG_MODEL","gpt-6-luna")
            if not model:
                raise ValueError("OPENAI_STRONG_MODEL is missing (exact model id from your OpenAI account)")
        # Reasoning models reject a custom temperature, and their hidden reasoning
        # tokens count toward the output cap, so the cap is larger and effort is low.
        return ChatOpenAI(
            model=model,
            api_key=api_key,
            max_tokens=MAX_TOKENS * 3,
            reasoning_effort="low",
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


fast_llm = build_llm("fast")
llm = build_llm("strong")


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
    origin: str
    cities: list[str]
    trip_type: str
    budget_table: str
    budget_total: int
    budget_inr: int

# Rules shared by the itinerary and final prompts. The flight and hotel data
# are included further down in each prompt.
PLANNING_RULES = """Rules:
- Use only the flight and hotel data provided below.
- Use listed prices from the data when they exist. Never present an invented price as a listed one.
- When a price is missing (a hotel, a train or bus fare, meals, entry fees), use a realistic typical figure for that place and the traveller's budget level and mark it "(est.)". Flight fares you cannot find may simply be left out in favour of train/bus estimates.
- Trip length must match the user's request exactly (N days = N-1 nights unless stated otherwise).
- Assume 1 traveller unless the user states otherwise.
- All totals must add up and stay within the user's stated budget.
- Suggest example travel dates at least 8 weeks after today's date.
- The itinerary must contain exactly N days, with no extra day.
- Do not state weekday names.
- The flight data shows live flights near today, not schedules for future dates. Show them only as examples of airlines and flight numbers. Do not attach future dates or invent return flights.
- Never mention "provided data", "supplied plan", "the feed you provided" or similar; the user supplied only their request. Say "price not available" or "estimate" instead.
- Only use hotels located in the city where the traveller sleeps that night. Never label a hotel as being in a different city than it really is.
- Every place you suggest must really lie in the destination the user asked for (e.g. if they ask for Andhra Pradesh, do not include places in Telangana).
- Be realistic about travel time. For long journeys (over ~600 km, i.e. an overnight train or bus), the traveller arrives the next morning or later and departs the day before or that evening. Do not schedule sightseeing before the arrival time or claim same-day arrival from an overnight journey.
- If the trip is domestic (origin and destination in the same country) and the flight data has no matching route, say so in one line and suggest train/bus (or a typical domestic fare labelled "estimate"). Do not tell the user to research flights elsewhere.
- For domestic trips never use the word "visa" at all (not even "no visa needed").
- Do not write a budget table or totals; the budget is computed separately.
- Use the budget sensibly: a long or multi-city trip should plan for the stated budget, not a fraction of it.
"""

SINGLE_CITY_RULES = """Trip shape: single city. Pick ONE base city, keep one hotel there, and use day trips only for places within about 3 hours each way.
"""

MULTI_CITY_RULES = """Trip shape: multi-city route. Do NOT use one base city with day trips.
- Plan a route that moves through the cities listed below, in a sensible geographic order, with a hotel in each city and an overnight stay (or overnight train) for each long leg.
- Spread the days across the cities; do not repeat the starting city for most of the trip.
- Day trips are fine only from the city where the traveller is currently staying, and only for nearby places.
"""


def trip_shape_rules(state: "TravelState") -> str:
    if state.get("trip_type") == "multi_city":
        return MULTI_CITY_RULES + "Cities to cover: " + ", ".join(state.get("cities") or []) + "\n"
    return SINGLE_CITY_RULES

class TripRequest(BaseModel):
    """Structured fields pulled out of the user's free-text request."""
    destination: str = Field(description="Destination city, state or country, e.g. 'Seoul' or 'Andhra Pradesh'")
    origin: str = Field(default="", description="City the traveller starts from, empty if not stated")
    trip_type: str = Field(
        description="'single_city' if the trip is based in one city; 'multi_city' if the user wants "
                    "several cities, a whole state/country, or a tour/route."
    )
    cities: list[str] = Field(
        default_factory=list,
        description="For multi_city trips, 3-5 real cities/towns INSIDE the destination worth visiting, in "
                    "a sensible travel order. For single_city trips, just the one city.",
    )
    budget_inr: int = Field(
        default=0,
        description="The user's total budget as a whole number in INR (convert if another currency is stated); 0 if no budget is given",
    )
    budget_level: str = Field(
        description="One of: budget, mid-range, luxury. Infer from the stated budget, default mid-range."
    )


def parse_request(state: TravelState):
    """Turn free text into structured fields so the search tools get clean queries.
    Falls back to empty fields (and the old raw-query behaviour) if parsing fails."""
    try:
        parser = fast_llm.with_structured_output(TripRequest)
        trip = parser.invoke([
            SystemMessage(content="Extract trip details from the user's request."),
            HumanMessage(content=state["user_query"]),
        ])
        trip_type = "multi_city" if trip.trip_type.strip().lower().startswith("multi") else "single_city"
        return {
            "destination": trip.destination.strip(),
            "origin": trip.origin.strip(),
            "trip_type": trip_type,
            "cities": [c.strip() for c in trip.cities if c.strip()][:5],
            "budget_level": trip.budget_level.strip().lower(),
            "budget_inr": max(trip.budget_inr, 0),
            "llm_calls": state.get("llm_calls", 0) + 1,
        }
    except Exception:
        return {"destination": "", "budget_level": "", "origin": "", "trip_type": "single_city", "cities": [], "budget_inr": 0}


# def flight_agent(state: TravelState):
#     query = state["user_query"]
#     flight_data = aviation_stack_mcp_call(query, compact=True)

#     return {
#         "flight_results": flight_data,
#         "messages": [
#             AIMessage(content="Flight results fetched.")
#         ],
#     }


FLIGHT_AGENT_PROMPT = """
You are a travel flight expert.

User Query:
{query}

Airport Information:
{airport_data}

Airline Information:
{airline_data}

Generate:

1. Likely departure airport
2. Likely arrival airport
3. Airlines serving this route
4. Typical flight duration
5. Estimated airfare range
6. Peak season pricing warning
7. Booking advice

Return concise travel guidance.
"""

def flight_agent(state: TravelState):
    query = state["user_query"]

    try:

        airports = asyncio.run(
            aviation_stack_mcp_call(
                "list_airports"
            )
        )

        airlines = asyncio.run(
            aviation_stack_mcp_call(
                "list_airlines"
            )
        )


        prompt = FLIGHT_AGENT_PROMPT.format(
            query=query,
            airport_data=str(airports)[:3000],
            airline_data=str(airlines)[:3000]
        )

        response = llm.invoke([
            SystemMessage(
                content="You are an expert travel flight planner."
            ),
            HumanMessage(content=prompt)
        ])

        flight_data = response.content

    except Exception as e:

        flight_data = "Flight information unavailable; plan with train/bus or typical fares marked (est.)."

    return {
        "flight_results": flight_data,
        "messages": [
            AIMessage(
                content="Flight recommendations generated"
            )
        ],
        "llm_calls": state.get("llm_calls", 0) + 1
    }
    
def hotel_agent(state: TravelState):
    destination = state.get("destination")
    level = state.get("budget_level") or "mid-range"
    cities = state.get("cities") or []

    if state.get("trip_type") == "multi_city" and cities:
        # one search per stop, run together, so every city has its own hotel data
        async def search_all():
            return await asyncio.gather(*[
                tavily_mcp_search(f"best {level} hotels in {city} with price per night")
                for city in cities
            ])
        per_city = asyncio.run(search_all())
        hotel_results = "\n\n".join(f"### Hotels in {c}\n{r}" for c, r in zip(cities, per_city))
    else:
        if destination:
            query = f"best {level} hotels in {destination} with price per night"
        else:  # parsing failed: fall back to the raw request
            query = f"Best hotels for {state['user_query']}"
        hotel_results = asyncio.run(tavily_mcp_search(query))

    return {
        "hotel_results": hotel_results,
        "messages": [
            AIMessage(content="Hotel information fetched")
        ],
    }


def itinerary_agent(state: TravelState):
    
    prompt = f"""Create ONLY the day-by-day plan for the user's request below.
    Do not include a budget summary, flight tables or general tips; those are written later.
    Keep each day to 4-6 short bullet points.
    End EVERY day with one line in exactly this form (whole INR numbers, 0 if none):
    "Costs: hotel ₹X, meals ₹X, transport ₹X, entry/activities ₹X"
    Count the hotel on each night the traveller sleeps in a hotel, and every transport leg on the day it happens.
    Today's date: {date.today().isoformat()}

{PLANNING_RULES}
{trip_shape_rules(state)}
    Traveller starts from: {state.get('origin') or 'not stated'} (they already live there; do not plan an "arrival" in the origin city).

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


class BudgetItem(BaseModel):
    category: str = Field(description="One of: Hotels, Transport, Meals, Entry fees & activities, Other")
    description: str = Field(description="Short label, e.g. '4 nights x 1500' or 'Train HYD-BBS return'")
    amount_inr: int = Field(description="Total cost of this line in INR, whole number")


class BudgetItems(BaseModel):
    items: list[BudgetItem]


def budget_agent(state: TravelState):
    """Read every cost out of the itinerary, then add them up in code (LLMs add badly)."""
    try:
        extractor = fast_llm.with_structured_output(BudgetItems)
        extracted = extractor.invoke([
            SystemMessage(content=(
                "Read the 'Costs:' line at the end of every day and return one line item per category "
                "per day (hotel, meals, transport, entry/activities) with its INR amount. Use the category "
                "names Hotels, Meals, Transport, Entry fees & activities. Do not invent, merge or skip any.")),
            HumanMessage(content=state["itinerary"]),
        ])
    except Exception:
        return {"budget_table": "", "budget_total": 0}

    totals: dict[str, int] = {}
    for item in extracted.items:
        totals[item.category] = totals.get(item.category, 0) + max(item.amount_inr, 0)
    grand = sum(totals.values())

    rows = "\n".join(f"| {cat} | {amt:,} |" for cat, amt in totals.items())
    table = f"| Category | Cost (INR) |\n|---|---|\n{rows}\n| **Total** | **{grand:,}** |"
    return {"budget_table": table, "budget_total": grand, "llm_calls": state.get("llm_calls", 0) + 1}


def final_agent(state: TravelState):
    if state.get("budget_table"):
        budget_instruction = (
            "Section 5 must contain EXACTLY this table (already computed, do not change any number), "
            "then one sentence comparing the total of "
            f"{state['budget_total']:,} INR with the user's budget:\n{state['budget_table']}\n"
            "Remove the per-day 'Costs:' lines from the itinerary section; the table replaces them. "
            "State that figures marked (est.) are estimates."
        )
        limit = state.get("budget_inr") or 0
        if limit and state["budget_total"] < 0.4 * limit:
            budget_instruction += (
                f" The plan uses under 40% of the {limit:,} INR budget, so add 2-3 concrete optional "
                "upgrades (e.g. better hotels, AC trains, extra experiences) with rough extra costs, "
                "keeping the total under the budget."
            )
        elif limit and state["budget_total"] > limit:
            budget_instruction += (
                f" The total EXCEEDS the {limit:,} INR budget: say so clearly and suggest specific cuts."
            )
    else:
        budget_instruction = ("Section 5: give a short budget table; label every figure 'estimate' "
                              "and make sure the rows add up to the stated total.")

    final_prompt = f"""
    Generate the final travel response for the user.
    Today's date: {date.today().isoformat()}

{PLANNING_RULES}
{trip_shape_rules(state)}
    {budget_instruction}

For international trips only: do not name a specific visa scheme (e.g. K-ETA);
say "check visa requirements for your passport with the destination's embassy".
For domestic trips, omit visa advice entirely.

    user Request: {state['user_query']}

    Flights : {state['flight_results']}

    Hotels:  {state['hotel_results']}

    Day-by-day plan (reuse it as section 4, do not re-plan it, but fix it if it breaks the rules above, e.g. wrong-city hotel or impossible travel timing): {state['itinerary']}

    Format the final answer beautifully using these sections:

    1. Trip summary
    2. Flight information
    3. Hotel Suggestions
    4. Day-by-Day Itinerary
    5. Estimated Budget
    6. Final Recommendations

    Important:
    - Be clear and practical.
    - If flight prices are unavailable, say so briefly in the flight section.
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
graph.add_node("budget_agent",budget_agent)
graph.add_node("final_agent",final_agent)

graph.add_edge(START,"parse_request")
graph.add_edge("parse_request","flight_agent")
graph.add_edge("flight_agent","hotel_agent")
graph.add_edge("hotel_agent","itinerary_agent")
graph.add_edge("itinerary_agent","budget_agent")
graph.add_edge("budget_agent","final_agent")
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
        "budget_level": "",
        "origin": "",
        "cities": [],
        "trip_type": "single_city",
        "budget_table": "",
        "budget_total": 0,
        "budget_inr": 0,
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