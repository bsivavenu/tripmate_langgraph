# TripMate: performance, token cost and monitoring

How much a trip costs in tokens and time, what to measure, how to see it, how to compare models, and which ready-made Python packages do the monitoring so you do not have to build it yourself.

Measured: 21 Nov 2026 · Branch `multi-agent-supervisor-guardrails-hitl`

**In one paragraph:** one normal trip made 6 AI calls and used about 9.6k tokens in about 35 seconds, and the most surprising step was `budget_agent`. **That step is now fixed (section 8): a trip makes 5 AI calls, uses about 7.1k tokens and takes about 29 seconds.** You do not need to build monitoring from scratch: **LangSmith tracing is already switched on in your `.env`**, and open-source alternatives (Langfuse, Arize Phoenix) exist if you want your data on your own machine. Only the TripMate-specific quality checks need custom code.

---

## 1. What one trip costs (measured, before the budget speed-up)

One run of "3 day trip from Hyderabad to Goa under 25000", including the Approve step. A temporary recorder (a LangChain callback attached to the graph) counted tokens and time per step. No project file was changed. The model names are what the provider reported for your current setup: `gpt-5-nano` for the "fast" steps and `gpt-6-luna` for the "strong" steps.

| Step | Time | AI calls | Tokens in | Tokens out | Model |
|---|---|---|---|---|---|
| `final_agent` | 7.8 s | 1 | 2,284 | 837 | strong |
| `budget_agent` | **7.3 s** | 1 | 927 | **1,321** | fast |
| `itinerary_agent` | 6.7 s | 1 | 1,847 | 651 | strong |
| `hotel_agent` (Tavily, no AI) | 3.8 s | 0 | 0 | 0 | none |
| `input_guardrail_agent` | 2.3 s | 1 | 171 | 171 | fast |
| `supervisor_agent` | 2.0 s | 1 | 328 | 346 | fast |
| `parse_request` | 1.6 s | 1 | 441 | 249 | fast |
| `weather_agent` | 0.6 s | 0 | 0 | 0 | none |
| `flight_agent` | 0.1 s | 0 | 0 | 0 | none |
| **Total** | **34.6 s** | **6** | **5,998** | **3,575** | about 9.6k tokens |

- Time to the draft: 25.6 s. Time from Approve to the final answer: 9.0 s.
- **Cost formula:** `cost = input_tokens × price_in + output_tokens × price_out` (prices are per million tokens, from your provider's pricing page; they change, so no prices are written here).
- **Scale example:** 1,000 trips would use about 6.0 million input tokens and 3.6 million output tokens.
- **A revision** adds roughly 3 more AI calls (`revise_trip_agent`, `itinerary_agent`, `budget_agent`) and, if the trip length changed, one more flight search.

**Caveats:** this is one run, so treat it as a guide, not an average. The flight search was served from the 6-hour fare cache (0.1 s); a cold search is slower. Times include network delays and vary from run to run.

## 2. What the numbers show

1. **`budget_agent` is the surprise.** It uses the *cheap* model, yet it took 7.3 s and produced more output tokens (1,321) than the itinerary writer (651). These look like *reasoning* models, where "output tokens" include hidden thinking. Even the guardrail's tiny yes/no answer used 171 output tokens.
2. **The budget step may not need an AI at all.** Every plan ends each day with a fixed line: `Costs: hotel ₹X, meals ₹X, transport ₹X, entry/activities ₹X`. A few lines of Python can read it, with the AI only as a fallback when the format is off. That would save about 7 s and about 2.2k tokens per trip and per revision.
3. **Flights, hotels and weather run one after another.** They do not depend on each other and could run in parallel, which would save roughly 4 to 8 seconds.
4. **The guardrail and the supervisor are two separate calls.** Merging them, or giving the guardrail a smaller model, would save about 2 s.
5. **`final_agent` receives about 2.3k tokens of input.** It re-sends the flights, hotels, weather and the whole itinerary. Trimming the hotel snippets would lower both cost and time.

## 3. What to measure

| Kind | Examples | Where to see it today |
|---|---|---|
| **Cost** | Tokens per trip and per step, cost per trip, cost per day | LangSmith traces; your AI provider's usage page |
| **Speed** | Time per step and end to end; the slowest 5% of trips | LangSmith trace waterfall and Monitoring tab |
| **Quality** | Does the total fit the budget? Does the plan have the right number of days? Do hotel names in the answer appear in the search results? How often do users click "Request changes"? | Not measured yet |
| **Reliability** | How often a fallback is used (no fares, no hotels), errors, retries, 409s | Only the server log for now |

Also watch your **free quotas**: SerpApi (250 searches a month on the free plan) and Tavily credits usually run out before money does. Their own dashboards show usage.

**Tokens, cost and speed are not quality.** A cheaper model that makes worse plans is not a saving. Always look at a quality check next to the cost numbers.

## 4. How to see it right now (no code)

Your `.env` already has `LANGSMITH_TRACING=true`, so each request is traced.

1. Open **smith.langchain.com** and go to your project (the name in `LANGSMITH_PROJECT`).
2. Open any trace to see every step with its inputs, outputs, tokens, time and estimated cost. You can filter by the trip's `thread_id` (the app already sends it as metadata).
3. Use the **Monitoring** tab for averages, the slowest trips and the error rate.
4. If a model is missing from the price table, cost may show as empty; add its price in the LangSmith settings.
5. **Privacy:** tracing sends users' text and the AI's answers to LangSmith's servers. That is fine for a personal project, but decide on purpose before real users arrive.

Your AI provider's dashboard (OpenAI or Groq) also shows tokens and spend per model per day. Set a monthly spending limit there as a safety net.

**Built-in token counter (no install):** `langchain_core.callbacks.get_usage_metadata_callback()` returns total tokens per model for any code run inside it. It is available in your installed version. It gives totals, not per-step numbers.

## 5. Comparing models

Do not judge a model on one trip.

1. Make a fixed set of about 20 prompts (the ones in `test_routing.py` and `docs/TESTING.md` are a start, plus a few multi-city and revision cases).
2. Run each model on the same prompts. Switching models only needs `.env`: `LLM_PROVIDER`, `OPENAI_FAST_MODEL`, `OPENAI_STRONG_MODEL` (or `GROQ_MODEL`).
3. Compare a table: cost per trip, median and slowest time, the automatic quality checks (budget fits, days match, no invented hotels), and a judge score on the plan.
4. Change **one role at a time**. For example keep the strong model for the itinerary but try a cheaper one for `final_agent`, and see whether quality drops. The guardrail, supervisor, parse and budget steps are the best candidates for the smallest model.

Notes: with `LLM_PROVIDER=groq`, `build_llm` ignores the fast/strong role and uses one model (`GROQ_MODEL`) for both. Reasoning models use hidden thinking tokens; check whether a provider setting such as reasoning effort exists and whether a non-reasoning model is enough for the small steps.

## 6. Quick wins, in order of value

| Change | Saves | Effort |
|---|---|---|
| ~~Read the `Costs:` lines in Python first; call the AI only if that fails (`budget_agent`)~~ **DONE** (section 8) | saved about 2.4k tokens and 5 to 7 s per trip | Small |
| Run flights, hotels and weather in parallel (a LangGraph fan-out) | about 4 to 8 s | Medium |
| Lower the reasoning effort or use a non-reasoning model for the small steps | tokens and seconds on 4 steps | Small |
| Merge guardrail and supervisor, or use a smaller guardrail model | about 2 s | Small to medium |
| Trim what `final_agent` receives (shorter hotel snippets) | tokens, a little time | Small |
| Cache hotel and weather results per destination for a few hours | one search per repeat destination | Small |
| Put a monthly spending limit on the provider account | protects against surprises | None (a setting) |

---

## 7. Do you need to build this yourself? No: use a package

Several packages already do tracing, token and cost tracking, latency and dashboards, and most plug into LangGraph with a few lines. Facts below are from each project's documentation as found on 21 Nov 2026; **check the linked docs for current features, limits, prices and licences before relying on them.**

| Package | What it is | Good for | Trade-offs |
|---|---|---|---|
| **LangSmith** (`langsmith`, already installed and on) | Hosted tracing and evaluation from the makers of LangChain and LangGraph | Zero extra code for you now; traces, tokens, cost, latency, dashboards, datasets, evaluators and experiments to compare models | Data goes to LangSmith's cloud; free tier has limits (check them) |
| **Langfuse** (`langfuse`) | Open-source observability; cloud or self-hosted | Same kind of traces, cost and sessions, plus prompt management and evals; a callback handler for LangGraph (`from langfuse.langchain import CallbackHandler`, passed in the run config) | Self-hosting means running extra services; older tutorials use an old import path |
| **Arize Phoenix** (`arize-phoenix`, `openinference-instrumentation-langchain`) | Open-source tracing and evals that can run locally (`phoenix serve`) | Free, local, no account; instruments LangChain and LangGraph automatically through OpenTelemetry | Fewer team and production features than the hosted tools; the hosted product (Arize AX) is separate |
| **LiteLLM** (`litellm`) | One interface to many AI providers, plus an optional proxy server | Built-in price table, per-key and per-model spend, budgets and logs (stored in Postgres); makes switching models easy | Adds a proxy service to run; it tracks cost at the gateway and does not know your steps |
| **Pydantic Logfire**, **OpenLLMetry** (`traceloop-sdk`), **Weights & Biases Weave**, **MLflow tracing**, **Opik** | More OpenTelemetry-based or experiment-tracking tools | If your team already uses one of them | Not specific to LangGraph |
| **Evaluation helpers**: `openevals`, `deepeval`, `ragas`, `promptfoo` | Ready-made quality checks and model comparison runs | Scoring plans with a judge model, comparing models on a prompt set | Still need your own test prompts and your own definition of "good" |

### What I recommend

1. **Start with LangSmith today.** It is already on, it understands LangGraph, and it answers the cost and speed questions without any code. Spend a week looking at real traces first.
2. **Move to Langfuse (self-hosted) or Phoenix (local) if privacy or cost matters.** Both are open source and also record tokens, time and cost. Moving later is a small change: a callback handler or an instrumentor.
3. **Only custom code you actually need:** the TripMate-specific quality checks that no package knows, such as "total fits the budget", "number of days matches", "hotel names come from the search results", "how often a fallback was used" and "how often users ask for changes". Compute them per trip and send them to the tool as scores or tags, so they show up next to the cost and time numbers.
4. **For model comparisons**, use LangSmith experiments (or `promptfoo` / `deepeval`) on your fixed prompt set instead of a hand-made script.
5. **Use LiteLLM only if** you want one place to set budgets across many models or keys. For a single app it is probably more than you need.

### What I have not done
I measured one trip and wrote this guide. I did **not** install any monitoring package, change any app code, or turn LangSmith settings on or off. The per-step recorder I used for the measurement is a temporary script and is not part of the repository.

---

## 8. The budget speed-up (done)

`budget_agent` no longer asks an AI in the normal case. It now reads the plan's `Costs:` lines in Python, adds the flight fare, and builds the table. The AI is kept only as a fallback.

**What the Python reader does** (`backend.py`: `_parse_cost_lines`, `_pick_fare`, `_travellers`, `_budget_in_python`)
- Adds up the four categories (hotel, meals, transport, entry/activities) from every day's `Costs:` line, whether or not it has bold text, bullets, commas or "(est.)".
- Picks the fare of the flight the plan actually uses (it matches the flight number in the plan, for example `6E 362`) and multiplies it by the number of travellers.
- **Falls back to the AI when unsure:** a day without a `Costs:` line, a line missing a category, several possible fares with no clue which one is used, an unclear number of travellers ("me and my girlfriend", "trip for 2"), or a day whose transport is already as big as the flight fare (the plan probably counted the flight itself, and adding it again would double count).

**Result on a real trip (same prompt, one run each)**

| | Before | After |
|---|---|---|
| `budget_agent` time | 7.3 s | **0.0 s** |
| AI calls per trip | 6 | **5** |
| Tokens in + out | 5,998 + 3,575 = 9,573 | 4,932 + 2,216 = **7,148** (−25%) |
| Total time (plan + approve) | 34.6 s | **29.0 s** (−5.6 s, about −16%) |

The other steps vary by a second or two from run to run, so the total is not an exact comparison. The 7.3 s saving on the budget step itself is the reliable number.

**Checked against the AI on your 77 saved trips that had a plan and a stored budget (read-only)**
- 71 trips (92%) could be handled by Python; 6 correctly fell back to the AI.
- **68 of the 71 gave exactly the same total as the old AI budget.**
- The 3 that differed were cases where **Python was right and the old AI budget was wrong**: two used a different flight's fare than the one in the plan (₹210 off), and one dropped a ₹75,000 airfare estimate.
- Two plans where Python at first double counted the flight were caught by the "transport already includes the flight" rule and now go to the AI.

13 new checks in `test_hitl.py` cover the reader (no API calls needed). A revision also gets the speed-up, because it re-runs the budget step.

## Sources
- Langfuse with LangChain and LangGraph (Python): [langfuse.com/integrations/frameworks/langgraph](https://langfuse.com/integrations/frameworks/langgraph), [LangChain docs page](https://docs.langchain.com/oss/python/integrations/providers/langfuse)
- Arize Phoenix with LangGraph: [Phoenix LangGraph tracing](https://arize.com/docs/phoenix/integrations/python/langgraph/langgraph-tracing), [LangChain docs: OpenInference](https://docs.langchain.com/oss/python/integrations/callbacks/openinference)
- LiteLLM spend tracking: [docs.litellm.ai/docs/proxy/cost_tracking](https://docs.litellm.ai/docs/proxy/cost_tracking), [custom pricing](https://docs.litellm.ai/docs/proxy/custom_pricing)
