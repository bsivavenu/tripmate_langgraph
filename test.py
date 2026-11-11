import uuid

from backend import run_travel_agent

DEBUG = False   # set True to see raw tool data and the draft itinerary

user_input = input("enter travel request: ")
thread_id = f"user_{uuid.uuid4().hex}"

res = run_travel_agent(user_input, thread_id=thread_id)


def section(title, body):
    print(f"\n{'=' * 20} {title} {'=' * 20}")
    print(body)


if DEBUG:
    section("FLIGHT RESULTS", res["flight_results"])
    section("HOTEL RESULTS", res["hotel_results"])
    section("ITINERARY DRAFT", res["itinerary"])

section("FINAL ANSWER", res["answer"])
print(f"\nthread_id={res['thread_id']}  llm_calls={res['llm_calls']}")
