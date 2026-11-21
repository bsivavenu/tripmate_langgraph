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
"""
from backend import run_travel_agent, resume_travel_agent


def check(label, condition):
    print(("PASS" if condition else "FAIL"), "-", label)
    if not condition:
        raise SystemExit(1)


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

print("\nALL TESTS PASSED")
