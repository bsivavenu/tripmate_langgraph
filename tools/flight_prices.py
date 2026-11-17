"""Real round-trip flight fares and schedules via SerpApi's Google Flights engine.

AviationStack only has live flight status (no fares, no future dates), so this is
what the planner uses for prices. Needs SERPAPI_API_KEY (free plan: 250 searches/month).
"""
import os
from datetime import date, timedelta
import time

import requests
from dotenv import load_dotenv

# from tools.flight_tool import resolve_location_to_iatas, DEFAULT_ORIGIN_IATA
from tools.flight_tool import resolve_location_to_iatas, DEFAULT_ORIGIN_IATA, AIRPORTS

load_dotenv()

SERPAPI_URL = "https://serpapi.com/search.json"
# Example travel date offset: Google Flights sells ~11 months ahead; planner rule is >= 8 weeks.
DAYS_AHEAD = 60
MAX_OPTIONS = 4
CACHE_SECONDS = 6 * 60 * 60  # keep a result for 6 hours
_cache = {}  # key -> (time saved, result text)



def _fmt_duration(minutes) -> str:
    if not minutes:
        return "?"
    return f"{minutes // 60}h {minutes % 60:02d}m"


def _describe_option(option: dict) -> str:
    legs = option.get("flights") or []
    if not legs:
        return ""
    airlines = []
    for leg in legs:
        name = leg.get("airline") or "Unknown airline"
        if name not in airlines:
            airlines.append(name)
    first, last = legs[0], legs[-1]
    dep, arr = first.get("departure_airport") or {}, last.get("arrival_airport") or {}
    stops = len(legs) - 1
    via = ", ".join(l.get("id", "?") for l in (option.get("layovers") or []))
    stop_text = "non-stop" if stops == 0 else f"{stops} stop{'s' if stops > 1 else ''} via {via}"
    numbers = " + ".join(l.get("flight_number", "?") for l in legs)
    return (
        f"- {' + '.join(airlines)} ({numbers}): {dep.get('id', '?')} {dep.get('time', '?')} -> "
        f"{arr.get('id', '?')} {arr.get('time', '?')}, {stop_text}, "
        f"{_fmt_duration(option.get('total_duration'))}, "
        f"round trip INR {option.get('price', 0):,}"
    )


def search_flight_prices(origin: str, destination: str, trip_days: int = 0, arrival_iata: str = "") -> str:
    """Return a text block of real round-trip fares, or "" when nothing usable was found."""
    api_key = os.getenv("SERPAPI_API_KEY")
    if not api_key:
        return ""

    dep_codes = resolve_location_to_iatas(origin) if origin else [DEFAULT_ORIGIN_IATA]
    if arrival_iata in AIRPORTS:
        arr_codes = [arrival_iata]
    else:
        arr_codes = resolve_location_to_iatas(destination) if destination else []

    # arr_codes = resolve_location_to_iatas(destination) if destination else []
    if not dep_codes or not arr_codes or dep_codes[0] == arr_codes[0]:
        return ""
    dep_id, arr_id = dep_codes[0], arr_codes[0]

    outbound = date.today() + timedelta(days=DAYS_AHEAD)
    ret = outbound + timedelta(days=max(trip_days, 2) - 1)
    key = (dep_id, arr_id, outbound.isoformat(), ret.isoformat())
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]

    params = {
        "engine": "google_flights",
        "departure_id": dep_id,
        "arrival_id": arr_id,
        "outbound_date": outbound.isoformat(),
        "return_date": ret.isoformat(),
        "type": 1,  # round trip
        "currency": "INR",
        "hl": "en",
        "api_key": api_key,
    }
    try:
        data = requests.get(SERPAPI_URL, params=params, timeout=45).json()
    except (requests.exceptions.RequestException, ValueError):
        return ""
    if data.get("error"):
        return ""

    options = (data.get("best_flights") or []) + (data.get("other_flights") or [])
    options = [o for o in options if o.get("price")]
    options.sort(key=lambda o: o["price"])
    lines = [d for d in (_describe_option(o) for o in options[:MAX_OPTIONS]) if d]
    if not lines:
        return ""

    header = (
        f"Real round-trip fares {dep_id} <-> {arr_id}, outbound {outbound.isoformat()}, "
        f"return {ret.isoformat()} (Google Flights, 1 adult, economy; each fare covers both directions). "
        f"Times are local departure from the first airport / arrival at the last."
    )
    insights = data.get("price_insights") or {}
    low = (insights.get("typical_price_range") or [])
    if len(low) == 2:
        header += f" Typical price range for this route: INR {low[0]:,}-{low[1]:,}."
    
    result = header + "\n" + "\n".join(lines)
    _cache[key] = (time.time(), result)
    
    return result



if __name__ == "__main__":
    print(search_flight_prices("Hyderabad", "Seoul", 5) or "no result (check SERPAPI_API_KEY)")
