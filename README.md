# ✈️ TripMate – AI Travel Planner

TripMate turns a plain-English trip request ("5 day trip from Hyderabad to Seoul, budget ₹1,50,000") into a full travel plan: real flight fares, hotels, weather, a day-by-day itinerary and a computed budget. **You review the draft itinerary first**: approve it, or ask for changes and the plan is rewritten.

It is a **multi-agent LangGraph workflow** served by **FastAPI**, with a small chat-style web UI:

- a **supervisor** that checks every request with an **input guardrail** (non-travel requests are blocked) and picks which specialist agents are needed,
- specialist agents for flights, hotels, weather, itinerary and budget,
- a **human-in-the-loop approval step** that pauses the run until you click *Approve* or *Request changes*,
- **MCP (Model Context Protocol) servers** for hotels and weather, one of which is written in this repo.

**🌐 Live demo: <https://tripmate-langgraph.onrender.com>** (free tier: the first load after idle can take ~1 minute; it may run an older branch than the one described here, see [Branches](#branches))

<p align="center">
  <a href="docs/architecture.png">
    <img src="docs/architecture.png" alt="TripMate architecture diagram" width="100%">
  </a>
  <br>
  <sub>Click the diagram to open it full size. Source: <code>docs/architecture.svg</code> (editable text), rendered to <code>docs/architecture.png</code>.</sub>
</p>

---

## Table of contents

1. [How it works in 60 seconds](#how-it-works-in-60-seconds)
2. [Request lifecycle](#request-lifecycle)
3. [Run the whole project](#run-the-whole-project) (start here to try it)
4. [Troubleshooting](#troubleshooting)
5. [Configuration reference](#configuration-reference)
6. [What every file does](#what-every-file-does)
7. [The MCP part, explained](#the-mcp-part-explained)
8. [API](#api)
9. [Design notes](#design-notes) (start date and weather, airports, LLM call counting)
10. [Tech stack · Branches · Known limitations · Next steps](#tech-stack)

---

## How it works in 60 seconds

1. The browser sends the request text (+ an optional start date) to `POST /api/plan` in `app.py`.
2. `app.py` calls `run_travel_agent()` in `backend.py`, which runs a **LangGraph graph** that shares one `TravelState` dict between all steps.
3. **`supervisor_agent`** runs first. Its **input guardrail** blocks anything that is not a trip-planning request (for example "what is the capital of Telangana"). A blocked request goes to `guardrail_blocked_agent`, which returns the reason, and the run ends.
4. An allowed request continues: the supervisor chooses which specialists are needed, then `parse_request`, `flight_agent`, `hotel_agent`, `weather_agent`, `itinerary_agent` and `budget_agent` run in a fixed order (unselected ones are skipped).
5. **`human_approval_agent`** pauses the graph with LangGraph's `interrupt()`. The API returns the **draft itinerary and budget table** (`needs_approval: true`) instead of a final answer, and the UI shows **Approve** and **Request changes** buttons.
6. The browser calls `POST /api/approve`:
   - **Approve** → `final_agent` writes the final answer in 7 sections.
   - **Request changes** (with feedback text) → `itinerary_agent` and `budget_agent` run again using your feedback, and you get a new draft to review. This can repeat.
7. State is checkpointed in **PostgreSQL** per `thread_id`. That is what lets a paused run be resumed later.

**Where to start reading the code:** `backend.py`, from the bottom up. `run_travel_agent()` / `resume_travel_agent()` → the graph definition (`graph.add_node(...)`) → each node function in the same order.

**Three things that surprise people (including future me)**

- **The LLM never chooses a tool.** The supervisor decides *which agents run*, but each agent calls its tools in fixed code. (Making one agent a real tool-choosing ReAct agent is in [Next steps](#next-steps).)
- **Only two of the three MCP servers are used.** AviationStack MCP is connected in `mcp_client.py` but no graph step calls it. Flight fares come from a *direct* SerpApi call, not from MCP.
- **The weather API only sees 5 days ahead.** So weather is a real forecast only when the trip starts within ~4 days; otherwise it is a "today's weather" climate hint.

---

## Request lifecycle

```mermaid
flowchart TD
    A([Browser: text + start date]) --> B["POST /api/plan (app.py)"]
    B --> S["supervisor_agent<br/>input guardrail + picks specialists"]
    S -- "not a travel request" --> X["guardrail_blocked_agent"]
    X --> R1([Reason shown in the UI])
    S -- "travel request" --> P[parse_request]
    P --> F[flight_agent]
    F --> H[hotel_agent]
    H --> W[weather_agent]
    W --> I[itinerary_agent]
    I --> BU[budget_agent]
    BU --> HA{{"human_approval_agent<br/>PAUSE: draft shown in the UI"}}
    HA -- "Approve (POST /api/approve)" --> FN[final_agent]
    HA -- "Request changes + feedback" --> I
    FN --> R2([Final plan in Markdown])
```

| Step (node in `backend.py`) | What it does | Uses |
|---|---|---|
| `supervisor_agent` | **Guardrail:** is this a travel-planning request? If not, it ends the run with a reason. **Routing:** picks which specialists this request needs. If the guardrail call itself fails, the request is allowed (fail-open). | fast LLM (2 calls) |
| `guardrail_blocked_agent` | Returns the block reason as the answer. | none |
| `parse_request` | Turns free text into structured fields: destination, origin, `cities` (for multi-city trips), `trip_days`, `budget_inr`, `budget_level`, `arrival_airport`. | fast LLM (structured output) |
| `flight_agent` | Looks up real **round-trip fares** for the chosen date and formats the best few options. If nothing is found, it writes a note telling the planner to use clearly labelled estimates. | `tools/flight_prices.py` → SerpApi Google Flights |
| `hotel_agent` | One web search per city for hotels with prices. If the search fails, it logs the error and falls back to clearly labelled general advice. | Tavily **MCP** (remote) |
| `weather_agent` | Trip starts within 4 days → day-by-day forecast. Otherwise → today's weather as a climate hint. One call per city, in parallel; a failed city does not break the plan. | Weather **MCP** (ours) → OpenWeatherMap |
| `itinerary_agent` | Writes the day-by-day plan and ends every day with a `Costs:` line. Real flight fares are deliberately **left out** of those lines. Applies your feedback when you asked for changes. | strong LLM |
| `budget_agent` | Reads the `Costs:` lines, **adds the flight fare** (fare × travellers), and sums everything **in Python** (LLMs add badly). | fast LLM (extraction) + code |
| `human_approval_agent` | Calls `interrupt()` with the draft itinerary and budget table. The run stops here and resumes when `POST /api/approve` is called. Not wrapped in `try/except` on purpose: LangGraph uses an exception to pause. | none |
| `final_agent` | Writes the final answer in 7 sections (summary, flights, hotels, weather, itinerary, budget, recommendations) using the computed budget table. | strong LLM |

**Fast vs strong LLM:** `build_llm("fast")` is a cheap model for the guardrail, supervisor, parsing and extraction; `build_llm("strong")` is the best model for the itinerary and final answer. Both come from `LLM_PROVIDER` (`groq` default, or `openai`).

---

## Run the whole project

### 1. Prerequisites

| You need | Notes |
|---|---|
| **Python 3.12+** | `.python-version` pins 3.12 |
| **[uv](https://docs.astral.sh/uv/)** | Installs the dependencies and also provides `uvx`, which the app uses to start the AviationStack MCP server. Install: `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| **PostgreSQL** | Stores LangGraph checkpoints (needed for the approval pause). Any Postgres works: a hosted one (Render, Supabase, Neon) or a local Docker one (below). The tables are created automatically on first start. |
| **API keys** | Groq (LLM), Tavily (hotels), SerpApi (fares), OpenWeather (weather). All have free plans. See the [table](#configuration-reference). |
| Internet access | The first start downloads the AviationStack MCP package with `uvx`, and all the tools call live APIs. |

### 2. Get the code and install dependencies

```bash
git clone https://github.com/bsivavenu/tripmate_langgraph.git
cd tripmate_langgraph
git checkout multi-agent-supervisor-guardrails-hitl     # the branch this README describes
uv sync                                                 # creates .venv and installs everything from uv.lock
```

### 3. Create a PostgreSQL database

**Option A: a hosted database.** Create a free Postgres on Render, Supabase or Neon and copy its **external connection URL**.

**Option B: a local one with Docker.**

```bash
docker run -d --name tripmate-pg \
  -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=tripmate \
  -p 5433:5432 postgres:16
```

Your URL is then:

```
postgresql://postgres:postgres@localhost:5433/tripmate?sslmode=disable
```

> **Important:** the app adds `sslmode=require` to `DATABASE_URL` when the URL has no `sslmode=` in it. A local Postgres has no SSL, so keep `?sslmode=disable` in the URL or the connection will fail.

### 4. Configure your keys

```bash
cp .env.example .env
```

Open `.env` and fill in at least: `GROQ_API_KEY`, `TAVILY_API_KEY`, `SERPAPI_API_KEY`, `OPENWEATHER_API_KEY` and `DATABASE_URL`. Never commit `.env` (it is in `.gitignore`).

### 5. Start the app

```bash
uv run uvicorn app:app --reload
```

Open **<http://127.0.0.1:8000>**. The first start takes longer: it connects to PostgreSQL, creates the checkpoint tables and downloads the AviationStack MCP package.

### 6. Try the full flow in the browser

1. **Plan a trip.** Click an example chip or type: `3 day trip from Hyderabad to Goa under 25000`, then press Enter. The plan takes up to a minute (live searches plus several LLM calls).
2. **Review the draft.** You see the draft itinerary and the budget table with **Approve** and **Request changes** buttons.
3. **Request changes.** Type feedback such as `Add a sunset boat trip on day 1` and click *Request changes*. A new draft appears with your change.
4. **Approve.** Click *Approve*. The final answer appears in 7 sections.
5. **Try the guardrail.** Ask `what is the capital of Telangana`. It is blocked with a reason and never reaches the planning agents.

### 7. Run the automated check (optional)

```bash
uv run python test_hitl.py
```

This runs the real pipeline end to end (it **uses live API calls and credits**) and checks four things: the run pauses for approval, a revision rewrites the draft using the feedback, approval produces the final answer, and a non-travel request is blocked without pausing. It prints `PASS` / `FAIL` for each and `ALL HITL TESTS PASSED` at the end.

Other quick checks:

```bash
uv run python test.py                  # lists every MCP tool (Tavily, AviationStack, weather)
uv run python -m tools.flight_prices   # prints a sample fare lookup (uses 1 SerpApi search)
```

### Run with Docker

```bash
docker build -t tripmate .
docker run -d --name tripmate --env-file .env -p 8000:8000 tripmate
```

The container listens on `$PORT` (default `8000`). The Dockerfile pre-downloads `aviationstack-mcp` with `uvx` (it needs Python ≥ 3.13, so uv fetches a managed 3.13 for that tool only). If your database runs on your own machine, use `host.docker.internal` instead of `localhost` in `DATABASE_URL`.

### Deploy (Render)

Deployed as a Docker web service from this repo's `Dockerfile`. Set the same variables as in `.env` in the service's *Environment* tab (use Render's **internal** database URL when in the same region). The live demo's Render service tracks its own branch, so pushing this branch does not change the live site until you point the service at it.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| The UI says **"No answer was returned."** | Your browser is running an old cached `script.js` that does not know about the approval step. Hard-refresh (Cmd/Ctrl + Shift + R). When you change `static/script.js` or `style.css`, bump the `?v=` number on their tags in `templates/index.html`. |
| `ValueError: database url is missing` | `DATABASE_URL` is not set in `.env`. |
| `server does not support SSL` / connection refused on start | A local Postgres needs `?sslmode=disable` on the URL (see [step 3](#3-create-a-postgresql-database)), and the container must be running (`docker ps`). |
| `Address already in use` on port 8000 | Another copy of the app is running. Stop it, or use `--port 8001`. |
| Hotels say "live search is temporarily unavailable" | The Tavily call failed (check `TAVILY_API_KEY` and quota). The plan still completes with labelled general advice; the error is printed in the server log as `HOTEL AGENT MCP ERROR`. |
| Weather says "not available" for a city | The weather MCP call failed (check `OPENWEATHER_API_KEY`). Printed in the log as `WEATHER AGENT MCP ERROR`. |
| Flights say "fares unavailable" | SerpApi returned nothing (quota, key, or an airport-code mix-up). See [Airports](#airports-the-one-place-that-bit-us). |
| Slow first start, or an error mentioning `uvx` | The first start downloads the AviationStack MCP package; it needs internet and `uv` on your PATH. |
| Rate-limit errors from the LLM | Free-tier limits on Groq. Wait a minute, or switch `LLM_PROVIDER=openai`. |

---

## Configuration reference

Copy `.env.example` to `.env` (never commit `.env`):

| Variable | Needed? | Used for |
|---|---|---|
| `GROQ_API_KEY` | If `LLM_PROVIDER=groq` (default) | LLM · <https://console.groq.com/keys> |
| `GROQ_MODEL` | Optional | Default `openai/gpt-oss-120b` |
| `LLM_PROVIDER` | Optional | `groq` (default) or `openai` |
| `OPENAI_API_KEY`, `OPENAI_FAST_MODEL`, `OPENAI_STRONG_MODEL` | If `LLM_PROVIDER=openai` | LLM (two model tiers) |
| `TAVILY_API_KEY` | Yes | Hotel search (Tavily MCP) · <https://tavily.com> |
| `SERPAPI_API_KEY` | Yes for real fares | Google Flights fares (free plan: 250 searches/month) · <https://serpapi.com> |
| `OPENWEATHER_API_KEY` | Yes for weather | Weather MCP server · <https://openweathermap.org/api> |
| `AVIATIONSTACK_API_KEY` | Optional | Only the (unused) AviationStack MCP and old `search_flights` |
| `DATABASE_URL` | Yes | PostgreSQL (`sslmode=require` is added if the URL has no `sslmode`) |
| `DEFAULT_ORIGIN_IATA` | Optional | Origin airport when the request names none (default `HYD`) |
| `LANGSMITH_TRACING`, `LANGSMITH_API_KEY`, `LANGSMITH_PROJECT` | Optional | Tracing · <https://smith.langchain.com> |

Note: `flight_tool.py` also accepts the misspelt `DEAFAULT_ORIGIN_IATA` as a fallback (kept for an old `.env`).

---

## What every file does

```
.
├── app.py                         FastAPI app: serves the UI, POST /api/plan, POST /api/approve
├── backend.py                     THE CORE: TravelState, LLM setup, supervisor + guardrail, all node
│                                  functions, human-approval node, graph wiring, Postgres checkpointer,
│                                  run_travel_agent() and resume_travel_agent()
├── mcp_client.py                  One MultiServerMCPClient for 3 MCP servers + helper
│                                  functions (tavily_mcp_search, weather_now, weather_forecast)
├── custom_weather_mcp_server.py   OUR OWN MCP server (FastMCP, stdio): get_current_weather,
│                                  get_forecast. Launched by mcp_client.py as a subprocess.
├── tools/
│   ├── flight_prices.py           Real fares via SerpApi (Google Flights): date handling,
│   │                              6-hour in-memory cache, formatting
│   ├── flight_tool.py             Airport/IATA resolution (cities, countries, states) built on
│   │                              `airportsdata`, plus LOCATION_OVERRIDES for places it gets
│   │                              wrong. Also an older AviationStack lookup (search_flights)
│   │                              that the graph no longer uses.
│   └── tavily_tool.py             Older direct Tavily client. Replaced by the Tavily MCP server;
│                                  kept for reference.
├── templates/index.html           The single page (chat box + start-date picker)
├── static/script.js, style.css    Frontend: sends the request, shows the draft with Approve /
│                                  Request changes buttons, calls /api/approve
├── docs/                          architecture.svg (editable source) / architecture.png (rendered)
├── test_hitl.py                   End-to-end test of the pause / revise / approve / guardrail flow
├── test.py                        Prints every tool the MCP client can see (a quick MCP check)
├── mcp_client_test.py             Old scratch file from when MCP was first tried; safe to ignore
├── main.py                        Empty `uv init` stub; safe to ignore
├── .env.example                   Template for your .env (copy it, fill in your keys)
├── Dockerfile                     Container image (also pre-downloads the AviationStack MCP)
└── pyproject.toml / uv.lock       Dependencies (source of truth)
```

---

## The MCP part, explained

`mcp_client.py` builds **one** `MultiServerMCPClient` that talks to **three** MCP servers. Each server exposes tools (plain functions the client can call by name).

| Server | Transport | Where it runs | Who wrote it | Used by the graph? |
|---|---|---|---|---|
| **Tavily** | `streamable_http` | Remote, on Tavily's servers | Tavily | Yes: `hotel_agent` (`tavily_search`) |
| **Weather** | `stdio` | Local subprocess (`sys.executable custom_weather_mcp_server.py`) | **Us** | Yes: `weather_agent` |
| **AviationStack** | `stdio` via `uvx` | Local subprocess (package downloaded from PyPI and cached) | Third party | **No** (connected, never called) |

**Why write our own weather server?** So we control the output: it returns 6 fields instead of the whole API response, and `get_forecast` returns a short per-day summary (min/max °C and the most common condition) instead of 40 three-hour entries.

**How a tool call travels:** `weather_agent` → `weather_now(city)` in `mcp_client.py` → `weather_mcp_call` → MCP client starts/uses the weather subprocess → `get_current_weather` in `custom_weather_mcp_server.py` → OpenWeatherMap → result flows back as JSON text.

**To see what the client can discover:** `uv run python test.py` prints all tool names (Tavily 5, AviationStack 12, weather 2).

**Handy facts**
- Passing `env={...}` to a stdio server is how its API key reaches the subprocess. A subprocess does **not** inherit your whole environment, and in Docker there is no `.env` file.
- `async def` / `await` is needed for MCP calls. The sync LangGraph nodes use `asyncio.run(...)` to call them.

---

## API

### `POST /api/plan`: start a trip

```json
{ "query": "4 day trip from Mumbai to Dubai, budget ₹80,000",
  "thread_id": null,
  "start_date": "2026-12-05" }
```

`start_date` is optional (`YYYY-MM-DD`). `thread_id` keys the saved state in PostgreSQL; the browser keeps it in `localStorage`, and one is created if you send `null`.

There are **two possible responses**:

**a) The run paused for your review** (every normal travel request):

```json
{ "thread_id": "user_ab12…",
  "needs_approval": true,
  "draft_itinerary": "### Day 1 — …",
  "budget_table": "| Category | Cost (INR) |\n|---|---|\n…" }
```

**b) The request was blocked by the guardrail** (no pause):

```json
{ "thread_id": "user_ab12…",
  "answer": "General knowledge question unrelated to travel planning …",
  "guardrail_allowed": false,
  "selected_agents": [], "llm_calls": 2, "…": "…" }
```

### `POST /api/approve`: answer the approval question

```json
{ "thread_id": "user_ab12…", "approved": true, "feedback": "" }
```

- `approved: true` → the run finishes: `{ "thread_id": "…", "needs_approval": false, "answer": "## 1. Trip summary …" }`
- `approved: false` + `feedback` → the itinerary and budget are redone with your feedback and the response is a new `needs_approval: true` draft.

Use the same `thread_id` that `/api/plan` returned. Errors return `{"detail": "..."}` with status 500.

Try it with curl:

```bash
curl -s localhost:8000/api/plan -H 'content-type: application/json' \
  -d '{"query":"3 day trip from Hyderabad to Goa under 25000","start_date":"2027-01-20"}'
# copy the thread_id from the response, then:
curl -s localhost:8000/api/approve -H 'content-type: application/json' \
  -d '{"thread_id":"PASTE_THREAD_ID_HERE","approved":true}'
```

---

## Design notes

### Start date and weather logic

- The UI has a **Trip start date** picker, pre-filled with today + 60 days and blocking past dates. Its value travels as `start_date` through `app.py` → `run_travel_agent()` → `TravelState`.
- `flight_prices.py` searches fares for exactly that date; with no date it falls back to today + 60 days (`DAYS_AHEAD`).
- `weather_agent` uses the forecast only if the start is **0–4 days away** (the free OpenWeather forecast covers 5 days); otherwise it labels the result "Weather today (climate hint only…)" so the planner does not present it as the trip-date forecast.

### Airports: the one place that bit us

The planner resolves city names to airport codes with `airportsdata`, and some names collide: **"Goa" matched `GOA` (Genoa, Italy)** instead of `GOI`, and "Bali" matched an airport in Cameroon. The fix is the `LOCATION_OVERRIDES` table in `tools/flight_tool.py`, checked first. It also beats the LLM's guessed `arrival_airport`. **If a route returns odd fares, check the airport code first** and add an override.

### How `llm_calls` is counted

`llm_calls` counts agents that really call an LLM (supervisor, parse, itinerary, budget extraction, final). The flight, hotel, weather and blocked-response nodes make no LLM call and do not add to it. A normal trip is about 5–6; each revision adds about 2 (itinerary + budget). It is returned in the final `/api/plan` response for blocked requests.

### Why the approval step is a pause, not a loop in the browser

`interrupt()` saves the whole graph state to PostgreSQL and stops the run, so the server holds nothing in memory while you read the draft. `POST /api/approve` resumes the same `thread_id` with `Command(resume={...})`. This is also why a database is required even for a single user.

---

## Tech stack

LangGraph (supervisor, conditional edges, `interrupt()`) + LangChain · Groq or OpenAI · MCP (`langchain-mcp-adapters`, `mcp` / FastMCP) · Tavily · SerpApi · OpenWeatherMap · `airportsdata` · PostgreSQL (`langgraph-checkpoint-postgres`) · FastAPI + Uvicorn · plain HTML/CSS/JS (`marked`, `DOMPurify`) · `uv` · Docker · LangSmith (optional).

---

## Branches

| Branch | What it is |
|---|---|
| `main` | Baseline |
| `multi-agent-without-mcp` | The pipeline using direct tool clients |
| `multi-agent-with-mcp` | Tavily + weather through MCP, SerpApi fares, start-date picker, weather agent, budget fix |
| `multi-agent-supervisor-guardrails-hitl` | **Current work (this README):** supervisor with input guardrail and conditional routing, blocked-request node, hotel/weather error handling, human-in-the-loop approval, `/api/approve` and the approval UI |

---

## Known limitations

- **Every trip needs a click.** The run always pauses for approval before the final answer. There is no "skip review" option yet.
- **A pending draft does not survive a page reload in the UI.** The run stays paused in PostgreSQL, but the buttons are gone; start a new trip. (The `thread_id` is kept, so the state is not lost, only the UI to continue it.)
- **Approval with a narrow plan is untested.** If the supervisor skips `itinerary_agent` (for example a flights-only request), the draft shown for approval may be empty, and *Request changes* always loops back to `itinerary_agent`.
- **The guardrail is an LLM judgement.** It blocks general-knowledge questions and unrelated requests, but borderline phrasing can slip through or be blocked wrongly. If the guardrail call itself fails, the request is allowed.
- **Prices are estimates.** Hotel and cost figures come from web snippets and LLM estimates; fares come from Google Flights via SerpApi. Always verify before booking.
- **Weather is only a forecast within ~5 days.** Further out it is a climate hint.
- **Return flight times are not listed** in the fare data, so the plan says so.
- **A plan takes up to a minute**: live searches plus several LLM calls in sequence. The progress card in the UI is an estimate on a timer, not live status.
- **Free-tier quotas** (Groq, Tavily, SerpApi 250/month, OpenWeather) can rate-limit heavy use. Fares are cached in memory for 6 hours to save SerpApi quota; the cache is lost on restart.

## Next steps

1. **Make one agent really choose its tools.** E.g. turn `weather_agent` into a LangGraph ReAct agent that gets the MCP tools from `client.get_tools()` and decides which to call.
2. **Run flights, hotels and weather in parallel** (a LangGraph fan-out) instead of one after another.
3. **Optional approval.** A "review the draft before finalizing" checkbox, and restoring a pending draft after a page reload.
4. **Stream real progress** to the UI instead of the timer-based progress card.
5. Use or remove the AviationStack MCP.
6. From/To airport autocomplete in the UI (would avoid airport-code mistakes at the source).
7. Automated tests for the guardrail with a set of borderline prompts.

## License

MIT. See [LICENSE](LICENSE).
