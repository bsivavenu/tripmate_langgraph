"""TripMate core: the LangGraph pipeline.

parse_request -> flight_agent -> hotel_agent -> weather_agent -> itinerary_agent -> budget_agent -> final_agent

All steps share one TravelState. State is checkpointed to PostgreSQL per thread_id. Entry point:
run_travel_agent(). See README.md for the architecture and the diagram in docs/.
"""

import os
import json
import uuid
import asyncio
import operator
from datetime import date
from typing import Any, TypedDict, Annotated

from dotenv import load_dotenv

load_dotenv()

import psycopg
from psycopg.rows import dict_row

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.postgres import PostgresSaver

from pydantic import BaseModel, Field
from langchain_core.messages import (AnyMessage,
                                    HumanMessage,
                                    AIMessage,
                                    SystemMessage)

from langgraph.types import interrupt, Command


from langchain_groq import ChatGroq
# from tools.tavily_tool import tavily_search
from mcp_client import tavily_mcp_search, weather_now, weather_forecast
from tools.flight_prices import search_flight_prices


def get_database_url():
    """Read DATABASE_URL from .env and make sure it has sslmode=require (needed by hosted Postgres such as Render)."""
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
    weather_results: str
    itinerary: str

    start_date: str
    
    llm_calls: int
    destination: str
    budget_level: str
    origin: str
    cities: list[str]
    trip_type: str
    budget_table: str
    budget_total: int
    budget_inr: int
    trip_days: int
    arrival_airport: str

    # Supervisor + guardrail state
    guardrail_allowed: bool
    guardrail_reason: str
    selected_agents: list[str]
    trip_constraints: dict[str, Any]
    supervisor_reasoning: str

    # New budget + HITL state
    budget_results: str
    approval_request: str
    approved: bool
    human_feedback: str
    final_response: str


# =========================
# Shared helpers
# =========================
KNOWN_AGENTS = {
    "flight_agent",
    "hotel_agent",
    "weather_agent",
    "budget_agent",
    "itinerary_agent",
}


AGENT_ORDER = [
    "flight_agent",
    "hotel_agent",
    "weather_agent",
    "budget_agent",
    "itinerary_agent",
]


def _llm_text(system_prompt: str, user_prompt: str) -> str:
    """One plain LLM call on the fast (cheap) model; used for the guardrail and the supervisor."""
    response = fast_llm.invoke(
        [
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_prompt),
        ]
    )
    return str(response.content)



def _json_from_llm(text: str) -> dict[str, Any]:
    """Extract the first complete JSON object returned by the model."""
    start = text.find("{")
    end = text.rfind("}")

    if start == -1 or end == -1 or end < start:
        raise ValueError("The model did not return a JSON object.")

    return json.loads(text[start : end + 1])


def _empty_constraints() -> dict[str, Any]:
    """Blank trip constraints, the shape the supervisor fills in."""
    return {
        "destination": "",
        "origin": "",
        "duration": "",
        "budget": "",
        "travel_style": "",
        "special_preferences": [],
    }


# Rules shared by the itinerary and final prompts. The flight and hotel data
# are included further down in each prompt.
PLANNING_RULES = """Rules:
- Use only the flight and hotel data provided below.
- Use listed prices from the data when they exist. Never present an invented price as a listed one.
- When a price is missing (a hotel, a train or bus fare, meals, entry fees), use a realistic typical figure for that place and the traveller's budget level and mark it "(est.)". Flight fares you cannot find may simply be left out in favour of train/bus estimates.
- Trip length must match the user's request exactly (N days = N-1 nights unless stated otherwise).
- Assume 1 traveller unless the user states otherwise.
- All totals must add up and stay within the user's stated budget.
- Use the trip start date given below if there is one; otherwise suggest example travel dates about 8 weeks after today's date.
- The itinerary must contain exactly N days, with no extra day.
- Do not state weekday names.
- Flight data, when present, lists real round-trip fares with dates. Use those fares, airlines, dates and times exactly as given (a fare covers both directions), and use those dates as the trip dates. If the flight data says fares are unavailable, give a typical round-trip fare labelled "(est.)" and do not invent flight numbers or times.
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
    """Prompt rules for the trip shape: one base city with day trips, or a route through state['cities']."""
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
    trip_days: int = Field(
        default=0,
        description="Total number of days of the trip (e.g. '5 day trip' -> 5); 0 if not stated",
    )
    arrival_airport: str = Field(
        default="",
        description="IATA code of the main airport the traveller flies into for this trip, e.g. 'DPS' for Bali, 'ICN' for Seoul; empty if unsure",
    )
    budget_level: str = Field(
        description="One of: budget, mid-range, luxury. Infer from the stated budget, default mid-range."
    )


# =========================
# Supervisor Agent + Input Guardrail
# =========================
def supervisor_agent(state: TravelState):
    """Graph entry. (1) Input guardrail: block requests that are not about travel.
    (2) Supervisor: choose which specialist agents this request needs.

    Writes guardrail_allowed / guardrail_reason, selected_agents, trip_constraints and
    supervisor_reasoning. A blocked request also gets final_response and ends the run.
    Both LLM steps fail open/safe: on any error the request is allowed and the full workflow runs.
    """
    query = state["user_query"]
    llm_calls = state.get("llm_calls", 0)

    guardrail_prompt = f"""
Determine whether the following request asks to PLAN or BOOK a trip, or asks for
travel help tied to a trip: destinations to visit, flights, hotels, weather for
a trip, budgets, visas, transportation, sightseeing, food, packing, or itineraries.

Block general-knowledge questions even if they mention a place (for example
"what is the capital of Telangana", "who is the CM of Kerala", "population of
Delhi"), and block anything unrelated to planning a trip.


Return strict JSON only:
{{
  "allowed": true,
  "reason": ""
}}

User request:
{query}
"""

    # Fail open on parser/model errors so a temporary JSON-format issue does not
    # break the original travel-planning behavior.
    try:
        guardrail_raw = _llm_text(
            "You are the input guardrail for a travel-planning application. "
            "Return strict JSON only.",
            guardrail_prompt,
        )
        guardrail_result = _json_from_llm(guardrail_raw)
        allowed = bool(guardrail_result.get("allowed", True))
        guardrail_reason = str(guardrail_result.get("reason", "")).strip()
        llm_calls += 1
    except Exception as exc:
        print(f"Guardrail fallback used: {exc}")
        allowed = True
        guardrail_reason = "Guardrail validation fallback allowed the request."

    if not allowed:
        reason = guardrail_reason or (
            "TripMate AI can only help with travel-planning requests. "
            "Please ask about a destination, flight, hotel, weather, budget, "
            "or itinerary."
        )
        return {
            "guardrail_allowed": False,
            "guardrail_reason": reason,
            "selected_agents": [],
            "trip_constraints": _empty_constraints(),
            "supervisor_reasoning": reason,
            "final_response": reason,
            "messages": [AIMessage(content=f"Guardrail blocked request: {reason}")],
            "llm_calls": llm_calls,
        }

    supervisor_prompt = f"""
You are the supervisor of a multi-agent travel-planning system.
Choose only the specialist agents needed for the request.

Available agents:
- flight_agent: flights, airports, airlines, routes, airfare, or booking advice
- hotel_agent: hotels, accommodation, neighborhoods, or places to stay
- weather_agent: weather, climate, season, forecast, or packing advice
- budget_agent: cost, affordability, price limits, or budget feasibility
- itinerary_agent: creates the integrated travel plan and must always be included

Return strict JSON only using this schema:
{{
  "selected_agents": ["flight_agent", "hotel_agent", "weather_agent", "budget_agent", "itinerary_agent"],
  "trip_constraints": {{
    "destination": "",
    "origin": "",
    "duration": "",
    "budget": "",
    "travel_style": "",
    "special_preferences": []
  }},
  "reasoning": ""
}}

User request:
{query}
"""

    try:
        supervisor_raw = _llm_text(
            "You route work to travel specialist agents. Return strict JSON only.",
            supervisor_prompt,
        )
        parsed = _json_from_llm(supervisor_raw)
        requested_agents = parsed.get("selected_agents", [])
        selected_agents = [
            name for name in AGENT_ORDER
            if name in requested_agents and name in KNOWN_AGENTS
        ]

        # The itinerary agent integrates whichever specialist results were selected.
        if "itinerary_agent" not in selected_agents:
            selected_agents.append("itinerary_agent")

        constraints = _empty_constraints()
        parsed_constraints = parsed.get("trip_constraints", {})
        if isinstance(parsed_constraints, dict):
            constraints.update(parsed_constraints)

        reasoning = str(parsed.get("reasoning", "")).strip()
        llm_calls += 1
    except Exception as exc:
        print(f"Supervisor fallback used: {exc}")
        # Original workflow behavior is preserved as the fallback.
        selected_agents = AGENT_ORDER.copy()
        constraints = _empty_constraints()
        reasoning = (
            "Supervisor parsing failed, so the original full travel workflow "
            "was selected as a safe fallback."
        )

    return {
        "guardrail_allowed": True,
        "guardrail_reason": guardrail_reason,
        "selected_agents": selected_agents,
        "trip_constraints": constraints,
        "supervisor_reasoning": reasoning,
        "messages": [AIMessage(content="Supervisor created the agent plan.")],
        "llm_calls": llm_calls,
    }


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
            "trip_days": max(trip.trip_days, 0),
            "arrival_airport": trip.arrival_airport.strip().upper(),
            "llm_calls": state.get("llm_calls", 0) + 1,
        }
    except Exception:
        return {"destination": "", "budget_level": "", "origin": "", "trip_type": "single_city", "cities": [], "budget_inr": 0, "trip_days": 0,"arrival_airport": ""}


# =========================
# Guardrail blocked response
# =========================
def guardrail_blocked_agent(state: TravelState):
    """Final step for a request the input guardrail blocked: return the reason as the answer."""
    reason = state.get("final_response") or state.get("guardrail_reason") or (
        "This request was blocked by the travel input guardrail."
    )
    return {
        "final_response": reason,
        "messages": [AIMessage(content=reason)],
    }


# =========================
# Flight Agent - original behavior kept
# =========================
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
    """Real round-trip fares from Google Flights (SerpApi). Falls back to a note that
    makes the planner use clearly-labelled estimates."""
    origin = state.get("origin", "")
    destination = state.get("destination", "")
    if state.get("trip_type") == "multi_city" and state.get("cities"):
        destination = state["cities"][0]

    try:
        flight_data = search_flight_prices(origin, destination, state.get("trip_days", 0),
                                           state.get("arrival_airport", ""), state.get("start_date", ""))

    except Exception:
        flight_data = ""

    if not flight_data:
        flight_data = ("Flight fares unavailable for this route. Do not invent flight numbers or times; "
                       "give a typical round-trip fare or train/bus cost labelled (est.).")

    return {
        "flight_results": flight_data,
        "messages": [AIMessage(content="Flight fares fetched")],
    }
    
def hotel_agent(state: TravelState):
    """Graph step 3: hotel search with Tavily (through MCP), one search per city for multi-city trips. Writes hotel_results."""
    destination = state.get("destination")
    level = state.get("budget_level") or "mid-range"
    cities = state.get("cities") or []

    try:
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
    except Exception as exc:
        # Tavily/MCP down: log it and let the planner continue with clearly-labelled general advice
        print(f"HOTEL AGENT MCP ERROR: {type(exc).__name__}: {exc}", flush=True)
        hotel_results = ("Live hotel search is temporarily unavailable. Give general accommodation "
                         "and neighbourhood guidance for the destination and clearly label it as "
                         "non-live advice (est.). Do not invent hotel names or prices.")

    return {
        "hotel_results": hotel_results,
        "messages": [
            AIMessage(content="Hotel information fetched")
        ],
    }


def weather_agent(state: TravelState):
    """Fetch weather for each city via our weather MCP server.
    Trip starts within ~5 days -> real day-by-day forecast; otherwise today's
    weather as a climate hint."""
    cities = state.get("cities") or []
    if state.get("trip_type") != "multi_city" or not cities:
        cities = [state["destination"]] if state.get("destination") else []

    # the free forecast API only covers 5 days ahead
    try:
        days_away = (date.fromisoformat(state.get("start_date") or "") - date.today()).days
    except ValueError:
        days_away = None
    use_forecast = days_away is not None and 0 <= days_away <= 4
    fetch = weather_forecast if use_forecast else weather_now

    async def fetch_all():
        # one call per city, run together; a failed city becomes an exception
        # object instead of crashing the whole trip plan
        return await asyncio.gather(
            *[fetch(city) for city in cities],
            return_exceptions=True,
        )

    lines = []
    if cities:
        try:
            results = asyncio.run(fetch_all())
        except Exception as exc:
            # weather MCP server failed to start/connect: log it, mark every city unavailable
            print(f"WEATHER AGENT MCP ERROR: {type(exc).__name__}: {exc}", flush=True)
            results = [exc] * len(cities)
        for city, result in zip(cities, results):
            if isinstance(result, Exception):
                lines.append(f"{city}: weather not available")
            else:
                lines.append(result)

    label = ("Forecast for the trip dates" if use_forecast
             else "Weather today (climate hint only, not the forecast for the trip dates)")
    return {
        "weather_results": f"{label}:\n" + "\n".join(lines) if lines else "",
        "messages": [
            AIMessage(content="Weather information fetched")
        ],
    }


def itinerary_agent(state: TravelState):
    """Graph step 5: the strong LLM writes the day-by-day plan.

    Each day ends with a 'Costs:' line that budget_agent reads. Real flight fares are left out of those
    lines on purpose (budget_agent adds them). Writes itinerary.
    """

    prompt = f"""Create ONLY the day-by-day plan for the user's request below.
    Do not include a budget summary, flight tables or general tips; those are written later.
    Keep each day to 4-6 short bullet points.
    End EVERY day with one line in exactly this form (whole INR numbers, 0 if none):
    "Costs: hotel ₹X, meals ₹X, transport ₹X, entry/activities ₹X"
    Count the hotel on each night the traveller sleeps in a hotel, and every transport leg on the day it happens.
    Exception: if the flight data below lists real fares, leave the flight fare OUT of the Costs lines (it is added separately). If it says fares are unavailable, include your estimated fare in transport.
    Today's date: {date.today().isoformat()}

{PLANNING_RULES}
{trip_shape_rules(state)}
    Traveller starts from: {state.get('origin') or 'not stated'} (they already live there; do not plan an "arrival" in the origin city).

    User Query: {state['user_query']}
    Flight Results: {state['flight_results']}
    Hotel Results: {state['hotel_results']}
    Trip start date: {state.get('start_date') or 'not given (suggest dates about 8 weeks from today)'}
    Revision feedback from the user (apply it if present): {state.get('human_feedback') or 'none'}

    Weather: {state.get('weather_results') or 'not available'}

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
    flight_total_inr: int = Field(
        default=0,
        description="Total INR for the flights of the fare the itinerary uses, for ALL travellers "
                    "(listed fares are per adult). 0 if the flight data lists no fare.",
    )


def budget_agent(state: TravelState):
    """Read every cost out of the itinerary, then add them up in code (LLMs add badly)."""
    try:
        extractor = fast_llm.with_structured_output(BudgetItems)
        extracted = extractor.invoke([
            SystemMessage(content=(
                "Read the 'Costs:' line at the end of every day and return one line item per category "
                "per day (hotel, meals, transport, entry/activities) with its INR amount. Use the category "
                "names Hotels, Meals, Transport, Entry fees & activities. Do not invent, merge or skip any. "
                "Do not put flight fares in the items; report them only in flight_total_inr "
                "(the listed round-trip fare of the flight the itinerary uses, times the number of travellers).")),
            HumanMessage(content=(
                f"ITINERARY:\n{state['itinerary']}\n\n"
                f"FLIGHT DATA:\n{state.get('flight_results', '')}\n\n"
                f"USER REQUEST:\n{state['user_query']}")),
        ])
    except Exception:
        return {"budget_table": "", "budget_total": 0}

    totals: dict[str, int] = {}
    for item in extracted.items:
        totals[item.category] = totals.get(item.category, 0) + max(item.amount_inr, 0)
    # Real fares were left out of the day costs on purpose; add them here, once.
    if "round trip INR" in state.get("flight_results", "") and extracted.flight_total_inr > 0:
        totals["Transport"] = totals.get("Transport", 0) + extracted.flight_total_inr
    grand = sum(totals.values())

    rows = "\n".join(f"| {cat} | {amt:,} |" for cat, amt in totals.items())
    table = f"| Category | Cost (INR) |\n|---|---|\n{rows}\n| **Total** | **{grand:,}** |"
    return {"budget_table": table, "budget_total": grand, "llm_calls": state.get("llm_calls", 0) + 1}


def final_agent(state: TravelState):
    """Graph step 7: the strong LLM writes the final 7-section answer from everything collected so far,
    using the budget table computed by budget_agent. The answer is the last message in the state.
    """
    if state.get("budget_table"):
        budget_instruction = (
            "Section 6 must contain EXACTLY this table (already computed, do not change any number), "
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
        budget_instruction = ("Section 6: give a short budget table; label every figure 'estimate' "
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

    Weather: {state['weather_results']}

    Day-by-day plan (reuse it as section 5, do not re-plan it, but fix it if it breaks the rules above, e.g. wrong-city hotel or impossible travel timing): {state['itinerary']}

    Format the final answer beautifully using these sections:

    1. Trip summary
    2. Flight information
    3. Hotel Suggestions
    4. weather information
    5. Day-by-Day Itinerary
    6. Estimated Budget
    7. Final Recommendations


    Important:
    - Be clear and practical.
    - If flight prices are unavailable, say so briefly in the flight section.
    - A specialist the supervisor did not select leaves its data above empty; for that section write one line, "Not requested", instead of inventing content.
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


def human_approval_agent(state: TravelState):
    """Pause the run and show the draft itinerary. Resumes when the user answers."""
    # Don't wrap interrupt() in try/except: LangGraph uses an exception to pause.
    review = interrupt({
        "question": "Do you approve this itinerary?",
        "draft_itinerary": state.get("itinerary", ""),
        "budget_table": state.get("budget_table", ""),
    })
    return {
        "approved": bool(review.get("approved", False)),
        "human_feedback": str(review.get("feedback", "")).strip(),
    }


### Build the graph 

graph = StateGraph(TravelState)


graph.add_node("supervisor_agent",supervisor_agent)
graph.add_node("parse_request",parse_request)
graph.add_node("flight_agent",flight_agent)
graph.add_node("hotel_agent",hotel_agent)
graph.add_node("weather_agent",weather_agent)
graph.add_node("itinerary_agent",itinerary_agent)
graph.add_node("budget_agent",budget_agent)
graph.add_node("final_agent",final_agent)
graph.add_node("guardrail_blocked_agent",guardrail_blocked_agent)
graph.add_node("human_approval_agent", human_approval_agent)


# Fixed order the steps run in. The supervisor decides WHICH of them run; this decides the order.
# (budget_agent stays after itinerary_agent because it reads the itinerary's cost lines.)
PIPELINE = ["parse_request", "flight_agent", "hotel_agent", "weather_agent", "itinerary_agent", "budget_agent"]


def route_after_supervisor(state: TravelState):
    """Guardrail result: a blocked request ends the run, an allowed one continues."""
    return "parse_request" if state.get("guardrail_allowed", True) else "guardrail_blocked_agent"


def next_step(current: str):
    """Router for one node: the next step in PIPELINE that the supervisor selected, else final_agent."""
    def router(state: TravelState):
        selected = state.get("selected_agents") or AGENT_ORDER
        for name in PIPELINE[PIPELINE.index(current) + 1:]:
            if name in selected:
                return name
        return "human_approval_agent"
    return router


graph.add_edge(START,"supervisor_agent")
graph.add_conditional_edges(
    "supervisor_agent", route_after_supervisor,
    {"parse_request": "parse_request", "guardrail_blocked_agent": "guardrail_blocked_agent"},
)
graph.add_edge("guardrail_blocked_agent", END)
for _node in PIPELINE:
    _targets = PIPELINE[PIPELINE.index(_node) + 1:] + ["human_approval_agent"]
    graph.add_conditional_edges(_node, next_step(_node), {t: t for t in _targets})

def route_after_approval(state: TravelState):
    """Approved -> write the final answer. Not approved -> redo the itinerary with the feedback."""
    return "final_agent" if state.get("approved") else "itinerary_agent"

graph.add_conditional_edges(
    "human_approval_agent", route_after_approval,
    {"final_agent": "final_agent", "itinerary_agent": "itinerary_agent"},
)
graph.add_edge("final_agent", END)


DATABASE_URL = get_database_url()

_conn = psycopg.connect(
    DATABASE_URL,
    autocommit= True,
    row_factory=dict_row
)

checkpointer = PostgresSaver(_conn)
checkpointer.setup()

travel_graph = graph.compile(checkpointer=checkpointer)


def run_travel_agent(user_input:str, thread_id: str| None= None, start_date: str | None = None):
    """Entry point used by app.py: run the whole graph for one request.

    Creates a thread_id if none is given (it keys the saved state in Postgres). start_date is optional,
    'YYYY-MM-DD'. Returns the final answer plus the intermediate results.
    """
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
        "weather_results": "",
        "start_date": start_date or "",
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
        "guardrail_allowed": True,
        "guardrail_reason": "",
        "selected_agents": [],
        "trip_constraints": {},
        "supervisor_reasoning": "",
        "final_response": "",
        "approved": False,
        "human_feedback": "",

    },
    config=config
    )
    if result.get("__interrupt__"):
        draft = result["__interrupt__"][0].value
        return {"thread_id": thread_id, "needs_approval": True,
            "draft_itinerary": draft["draft_itinerary"],
            "budget_table": draft["budget_table"]}

    # A request blocked by the input guardrail has no itinerary: its message is final_response.
    if result.get("guardrail_allowed") is False:
        final_answer = result.get("final_response", "")
    else:
        final_answer = result["messages"][-1].content

    return {
        "thread_id": thread_id,
        "answer": final_answer,
        "flight_results": result.get("flight_results",""),
        "hotel_results": result.get("hotel_results",""),
        "weather_results": result.get("weather_results",""),
        "itinerary": result.get("itinerary",""),
        "llm_calls": result.get("llm_calls",0),
        "guardrail_allowed": result.get("guardrail_allowed", True),
        "selected_agents": result.get("selected_agents", []),
        "supervisor_reasoning": result.get("supervisor_reasoning", ""),
    }


def resume_travel_agent(thread_id: str, approved: bool, feedback: str = ""):
    """Continue a paused run with the user's answer."""
    config = {"configurable": {"thread_id": thread_id}}
    result = travel_graph.invoke(
        Command(resume={"approved": approved, "feedback": feedback}), config=config)
    if result.get("__interrupt__"):  # revision made a new draft: ask again
        draft = result["__interrupt__"][0].value
        return {"thread_id": thread_id, "needs_approval": True,
                "draft_itinerary": draft["draft_itinerary"],
                "budget_table": draft["budget_table"]}
    return {"thread_id": thread_id, "needs_approval": False,
            "answer": result["messages"][-1].content}
