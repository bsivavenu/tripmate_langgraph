# ✈️ TripMate – AI Travel Planner

TripMate turns a plain-English trip request into a complete travel plan: flights, hotel suggestions, a day-by-day itinerary and a budget breakdown. It is a multi-step **LangGraph** workflow served by **FastAPI**, with a small chat-style web UI.

> Example: *"5 day trip from Hyderabad to Seoul, budget ₹1,50,000"*

## How it works

```mermaid
flowchart LR
    A([User request]) --> B[parse_request]
    B --> C[flight_agent]
    C --> D[hotel_agent]
    D --> E[itinerary_agent]
    E --> F[final_agent]
    F --> G([Markdown travel plan])
```

| Node | What it does |
|---|---|
| `parse_request` | Uses the LLM's structured output to extract the destination and budget level from free text, so search tools receive clean queries. |
| `flight_agent` | Resolves the route to airport codes and fetches live flight data from AviationStack. |
| `hotel_agent` | Searches the web (Tavily) for hotels in the destination. |
| `itinerary_agent` | LLM drafts the day-by-day plan from the flight and hotel data. |
| `final_agent` | LLM assembles the final answer: summary, flights, hotels, itinerary, budget and recommendations. |

State is checkpointed to **PostgreSQL** per `thread_id`, and every run is traced in **LangSmith** (optional).

## Tech stack

- **Orchestration:** LangGraph, LangChain
- **LLM:** Groq (default, `openai/gpt-oss-120b`) or OpenAI, selected with `LLM_PROVIDER`
- **Tools:** Tavily (web search), AviationStack (flights)
- **Persistence:** PostgreSQL via `langgraph-checkpoint-postgres`
- **Backend:** FastAPI + Uvicorn
- **Frontend:** plain HTML, CSS and JavaScript (Markdown rendered with `marked`, sanitised with `DOMPurify`)
- **Tooling:** `uv` for dependencies, Docker for packaging

## Project structure

```
.
├── app.py              # FastAPI app: serves the UI and POST /api/plan
├── backend.py          # LangGraph workflow, LLM setup, Postgres checkpointer
├── tools/
│   ├── flight_tool.py  # Route parsing + AviationStack lookup
│   └── tavily_tool.py  # Web search wrapper
├── templates/index.html
├── static/             # style.css, script.js
├── test.py             # Run the agent from the terminal
├── Dockerfile
└── pyproject.toml / uv.lock
```

## Getting started

### Prerequisites

- Python 3.12+ and [uv](https://docs.astral.sh/uv/)
- A PostgreSQL database (a hosted one such as Render or Supabase works)
- API keys: [Groq](https://console.groq.com/), [Tavily](https://tavily.com/), [AviationStack](https://aviationstack.com/)

### 1. Install

```bash
git clone https://github.com/bsivavenu/tripmate_langgraph.git
cd tripmate_langgraph
uv sync
```

### 2. Configure

Create a `.env` file in the project root:

```env
# Required
GROQ_API_KEY=your_groq_key
TAVILY_API_KEY=your_tavily_key
AVIATIONSTACK_API_KEY=your_aviationstack_key
DATABASE_URL=postgresql://user:password@host:5432/dbname

# Optional
LLM_PROVIDER=groq                 # or: openai
GROQ_MODEL=openai/gpt-oss-120b
OPENAI_API_KEY=                   # only if LLM_PROVIDER=openai
OPENAI_MODEL=gpt-4o-mini
DEFAULT_ORIGIN_IATA=HYD           # origin used when the request names no departure city

# LangSmith tracing (optional)
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=your_langsmith_key
LANGSMITH_PROJECT=tripmate
```

`sslmode=require` is added to `DATABASE_URL` automatically if missing. Never commit `.env`; it is already in `.gitignore`.

### 3. Run

```bash
uv run uvicorn app:app --reload
```

Open <http://127.0.0.1:8000>.

To try the agent without the web UI:

```bash
uv run python test.py
```

## Run with Docker

```bash
docker build -t tripmate .
docker run -d --name tripmate --env-file .env -p 8000:8000 tripmate
```

Secrets are passed at runtime with `--env-file`; `.dockerignore` keeps `.env` out of the image. The container listens on `$PORT` (default `8000`), so it also works on hosts such as Render that set `PORT` themselves.

Useful commands: `docker logs -f tripmate`, `docker stop tripmate`, `docker rm -f tripmate`.

## API

`POST /api/plan`

```json
{ "query": "4 day trip from Mumbai to Dubai, budget ₹80,000", "thread_id": null }
```

Response:

```json
{
  "thread_id": "user_ab12…",
  "answer": "## 1. Trip summary …",
  "flight_results": "…",
  "hotel_results": "…",
  "itinerary": "…",
  "llm_calls": 3
}
```

Send the returned `thread_id` back to continue the same conversation. Errors return `{"detail": "..."}` with status 500.

## Known limitations

- **No fares or schedules.** AviationStack only returns flights that are airborne right now. For routes with no live flights the plan says flight prices are unavailable. A fare API such as Amadeus would fix this.
- **Prices are estimates.** Hotel and cost figures come from web snippets and LLM estimates. Always verify before booking.
- **A plan takes up to a minute**, because it makes live searches and three LLM calls in sequence.
- Free-tier API quotas (Groq, Tavily, AviationStack) can rate-limit heavy use.

## Roadmap

- Real fare and schedule data
- Cap hotel spend relative to the total budget
- Supervisor node that routes between agents
- Human-in-the-loop approval of the draft itinerary
- Streaming progress to the UI

## License

MIT. See [LICENSE](LICENSE).
