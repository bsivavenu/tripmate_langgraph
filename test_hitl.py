"""End-to-end test of the guardrail, clarification and human-in-the-loop approval flow
(uses the real LLM/search APIs, so it spends a few credits).

Run:  .venv/bin/python test_hitl.py

Flow tested:
  1. A travel request pauses at human_approval_agent  -> needs_approval=True, draft returned
  2. Request changes (twice)                           -> new draft each time; the FIRST change is kept
  3. Approve                                           -> final answer, needs_approval=False
  4. Approving again / an unknown thread               -> ValueError (nothing waiting), no second answer
  5. A non-travel request is blocked by the guardrail  -> answer returned, no pause
  6. A travel request with no destination              -> the supervisor asks a question, no pause
  7. An information-only question (visa rules)         -> redirected with no pipeline run, no pause
  8. A region as destination ("Europe")                -> treated as a destination, pauses with a draft
  0. (first, no API calls) airport names with a country resolve; Tavily errors raise instead of ""
  9. Request changes that alters the trip length       -> days, fares and the final answer all follow it
 10. Follow-ups ("make it cheaper") on the same trip   -> merged with the earlier request; a new trip is not merged
 11. A draft waiting for approval can be looked up     -> get_pending_approval (used to restore it after a reload)
 12. review=False                                      -> final answer directly, no approval pause
"""
import re
from backend import run_travel_agent, resume_travel_agent, get_pending_approval, travel_graph
from mcp_client import _format_results
from tools.flight_tool import resolve_location_to_iatas


def check(label, condition):
    print(("PASS" if condition else "FAIL"), "-", label)
    if not condition:
        raise SystemExit(1)


# 0. Quick checks that need no API calls
# 0a. Airport names the LLM writes with a country still resolve (Bali, Kyoto, Goa, Leh were missing)
for name, expected in [("Bali, Indonesia", "DPS"), ("Kyoto", "KIX"), ("Kyoto, Japan", "KIX"),
                       ("Goa, India", "GOI"), ("Leh", "IXL"), ("Ladakh, India", "IXL"), ("Goa", "GOI")]:
    check(f"airport for {name!r} is {expected}", expected in resolve_location_to_iatas(name))

# 0b. A Tavily error response must raise (so hotel_agent can fall back), not become an empty string
try:
    _format_results('{"error":"Search failed","detail":"Invalid Tavily API key"}')
    check("Tavily error response raises", False)
except RuntimeError:
    check("Tavily error response raises", True)
try:
    _format_results('{"results": []}')
    check("Tavily empty results raise", False)
except RuntimeError:
    check("Tavily empty results raise", True)
check("Tavily normal results still format",
      "Hotel A" in _format_results('{"results":[{"title":"Hotel A","url":"u","content":"nice"}]}'))


# 1. Start a trip: the run should stop and ask for approval
r1 = run_travel_agent("2 day trip from Bangalore to Goa, mid-range, budget 20000 INR",
                      start_date="2026-12-20")
thread_id = r1["thread_id"]
check("run pauses for approval", r1.get("needs_approval") is True)
check("draft itinerary is returned", len(r1.get("draft_itinerary", "")) > 100)
print("\n--- first draft (first 300 chars) ---\n", r1["draft_itinerary"][:300], "\n")

# 2. Ask for a change, then another: both must survive in the latest draft
r2 = resume_travel_agent(thread_id, approved=False, feedback="Add a sunset dolphin-watching boat trip on day 1")
check("revision pauses for approval again", r2.get("needs_approval") is True)
check("draft changed after feedback", r2["draft_itinerary"] != r1["draft_itinerary"])
check("first feedback appears in new draft", "dolphin" in r2["draft_itinerary"].lower())

r2b = resume_travel_agent(thread_id, approved=False, feedback="Also add a spice plantation visit on day 2")
check("second revision pauses for approval", r2b.get("needs_approval") is True)
check("second feedback is applied", "spice" in r2b["draft_itinerary"].lower())
check("first feedback is NOT forgotten by the second revision", "dolphin" in r2b["draft_itinerary"].lower())

# 3. Approve: should finish with the final answer
r3 = resume_travel_agent(thread_id, approved=True)
check("approval finishes the run", r3.get("needs_approval") is False)
check("final answer has the 7 sections", "Final Recommendations" in r3["answer"])
print("\n--- final answer (first 300 chars) ---\n", r3["answer"][:300], "\n")

# 4. Approving again, or approving an unknown thread, must not re-run anything
for label, tid in [("approving a finished trip again", thread_id),
                   ("approving an unknown thread", "user_does_not_exist")]:
    try:
        resume_travel_agent(tid, approved=True)
        check(f"{label} is rejected", False)
    except ValueError:
        check(f"{label} is rejected with a clear error", True)

# 5. Non-travel request: guardrail still blocks it, no pause
r5 = run_travel_agent("Write me a python script to sort a list")
check("guardrail still blocks non-travel", r5.get("guardrail_allowed") is False)
check("blocked request does not pause", not r5.get("needs_approval"))

# 6. Travel-related but no destination: the supervisor asks for details instead of planning
r6 = run_travel_agent("ok, or else book the tickets for 50% less rates in this budget")
check("vague request is not blocked by the guardrail", r6.get("guardrail_allowed") is True)
check("vague request gets a clarifying question", r6.get("needs_clarification") is True)
check("clarifying request does not pause for approval", not r6.get("needs_approval"))
check("question mentions destination", "where" in r6["answer"].lower())

# 7. Information-only question: no pipeline, no approval screen
r7 = run_travel_agent("visa requirements for Japan?")
check("info question gets a direct reply", r7.get("needs_clarification") is True)
check("info question does not pause for approval", not r7.get("needs_approval"))
check("info reply invites a trip request", "budget" in r7["answer"].lower())
check("info question ran no search agents", r7.get("selected_agents") == [])

# 8. A region counts as a destination
r8 = run_travel_agent("15 day trip from Mumbai to Europe under 300000", start_date="2027-03-01")
check("'Europe' is accepted as a destination", r8.get("needs_approval") is True)

# 9. A revision that changes the trip length must change days, flight dates and the final answer
def state_of(tid):
    return travel_graph.get_state({"configurable": {"thread_id": tid}}).values

r9 = run_travel_agent("3 day trip from Hyderabad to Goa under 25000", start_date="2027-02-10")
r9b = resume_travel_agent(r9["thread_id"], approved=False, feedback="Make it 2 days instead")
v9 = state_of(r9["thread_id"])
check("revision updates the trip length to 2", v9.get("trip_days") == 2)
check("flight fares are searched again for the new dates", "return 2027-02-11" in (v9.get("flight_results") or ""))
r9c = resume_travel_agent(r9["thread_id"], approved=True)
days_in_final = set(re.findall(r"Day\s+(\d+)", r9c["answer"]))
check("final answer has 2 days, not the original 3", days_in_final == {"1", "2"})

# 10. Follow-up memory
r10 = run_travel_agent("3 day trip from Hyderabad to Goa under 25000", start_date="2027-02-10")
t10 = r10["thread_id"]
r10b = run_travel_agent("make it cheaper", thread_id=t10, start_date="2027-02-10")
v10 = state_of(t10)
check("follow-up 'make it cheaper' is planned, not asked again", r10b.get("needs_approval") is True)
check("follow-up keeps the earlier destination", "goa" in (v10.get("user_query") or "").lower())
check("follow-up lowers the budget", 0 < (v10.get("budget_inr") or 0) < 25000)
r10c = run_travel_agent("5 day trip from Delhi to Manali under 30000", thread_id=t10, start_date="2027-02-10")
v10c = state_of(t10)
check("a new trip on the same thread is NOT merged with the old one",
      "manali" in (v10c.get("user_query") or "").lower() and "goa" not in (v10c.get("user_query") or "").lower())
r10d = run_travel_agent("make it cheaper")
check("a follow-up with no earlier trip still asks for details", r10d.get("needs_clarification") is True)

# 11. Pending draft lookup (used to restore the draft after a page reload)
pend = get_pending_approval(t10)
check("a paused trip has a pending draft", bool(pend) and len(pend["draft_itinerary"]) > 100 and "manali" in pend["query"].lower())
check("a finished trip has no pending draft", get_pending_approval(r9["thread_id"]) is None)
check("an unknown trip has no pending draft", get_pending_approval("user_does_not_exist") is None)

# 12. Skip review
r12 = run_travel_agent("2 day trip from Hyderabad to Goa under 20000", start_date="2027-02-10", review=False)
check("review=False returns the final answer directly", "Final Recommendations" in r12.get("answer", ""))
check("review=False does not pause", not r12.get("needs_approval"))

print("\nALL TESTS PASSED")
