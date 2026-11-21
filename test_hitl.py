"""End-to-end test of the human-in-the-loop approval step (uses the real LLM/search APIs).

Run:  .venv/bin/python test_hitl.py

Flow tested:
  1. A travel request pauses at human_approval_agent  -> needs_approval=True, draft returned
  2. Resume with feedback ("not approved")             -> itinerary is rewritten, pauses again
  3. Resume with approval                              -> final answer, needs_approval=False
  4. A non-travel request is blocked by the guardrail  -> answer returned, no pause
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

# 2. Ask for a change: should rewrite the itinerary and pause again
FEEDBACK = "Add a sunset dolphin-watching boat trip on day 1"
r2 = resume_travel_agent(thread_id, approved=False, feedback=FEEDBACK)
check("revision pauses for approval again", r2.get("needs_approval") is True)
check("draft changed after feedback", r2["draft_itinerary"] != r1["draft_itinerary"])
check("feedback appears in new draft", "dolphin" in r2["draft_itinerary"].lower())

# 3. Approve: should finish with the final answer
r3 = resume_travel_agent(thread_id, approved=True)
check("approval finishes the run", r3.get("needs_approval") is False)
check("final answer has the 7 sections", "Final Recommendations" in r3["answer"])
print("\n--- final answer (first 300 chars) ---\n", r3["answer"][:300], "\n")

# 4. Non-travel request: guardrail still blocks it, no pause
r4 = run_travel_agent("Write me a python script to sort a list")
check("guardrail still blocks non-travel", r4.get("guardrail_allowed") is False)
check("blocked request does not pause", not r4.get("needs_approval"))

print("\nALL HITL TESTS PASSED")
