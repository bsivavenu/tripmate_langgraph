import os
from dotenv import load_dotenv
import re
import requests
import pycountry
import airportsdata
from langsmith import traceable

load_dotenv()
api_key = os.getenv("AVIATIONSTACK_API_KEY")
# DEAFAULT_ORIGIN_IATA kept as a fallback for existing .env files with the old typo
DEFAULT_ORIGIN_IATA = os.getenv("DEFAULT_ORIGIN_IATA") or os.getenv("DEAFAULT_ORIGIN_IATA", "HYD")
BASE_URL = "https://api.aviationstack.com/v1/flights"

AIRPORTS = airportsdata.load("IATA")

# Common nicknames / alternate spellings that pycountry doesn't cover.
# Values are ISO 3166-1 alpha-2 codes (same format as AIRPORTS[...]["country"]).
_EXTRA_COUNTRY_ALIASES = {
    "usa": "US", "u.s.": "US", "u.s.a.": "US", "us": "US", "america": "US",
    "united states of america": "US", "the states": "US",
    "uk": "GB", "u.k.": "GB", "britain": "GB", "great britain": "GB",
    "england": "GB", "scotland": "GB", "wales": "GB", "northern ireland": "GB",
    "uae": "AE", "emirates": "AE",
    "south korea": "KR", "korea": "KR", "republic of korea": "KR",
    "north korea": "KP",
    "russia": "RU", "vietnam": "VN", "laos": "LA", "syria": "SY",
    "iran": "IR", "bolivia": "BO", "venezuela": "VE", "tanzania": "TZ",
    "moldova": "MD", "brunei": "BN", "taiwan": "TW", "palestine": "PS",
    "czech republic": "CZ", "czechia": "CZ",
    "turkey": "TR", "turkiye": "TR", "türkiye": "TR",
    "holland": "NL", "the netherlands": "NL",
    "ivory coast": "CI", "cote d'ivoire": "CI", "côte d'ivoire": "CI",
    "burma": "MM", "myanmar": "MM",
    "swaziland": "SZ", "eswatini": "SZ",
    "cape verde": "CV", "cabo verde": "CV",
    "east timor": "TL", "timor-leste": "TL",
    "macedonia": "MK", "north macedonia": "MK",
    "drc": "CD", "dr congo": "CD", "congo-kinshasa": "CD",
    "congo-brazzaville": "CG", "republic of the congo": "CG",
    "hong kong": "HK", "macau": "MO", "macao": "MO",
    "vatican": "VA", "vatican city": "VA", "holy see": "VA",
    "bharat": "IN", "hindustan": "IN",
}


def _build_country_aliases():
    aliases = {}
    for c in pycountry.countries:
        code = c.alpha_2
        names = {c.name, c.alpha_2, c.alpha_3}
        for attr in ("official_name", "common_name"):
            val = getattr(c, attr, None)
            if val:
                names.add(val)
        for n in names:
            aliases[n.lower()] = code
    aliases.update(_EXTRA_COUNTRY_ALIASES)
    return aliases


# alias (lowercase) -> ISO alpha-2 code, covering every country in pycountry
COUNTRY_ALIASES = _build_country_aliases()

# Curated major airports (IATA) per country, for the most-travelled countries.
# airportsdata has no traffic data, so "major" can't be derived from it.
_MAJOR_AIRPORTS = {
    "IN": ["DEL", "BOM", "BLR", "HYD", "MAA", "CCU", "COK", "AMD", "GOI", "PNQ", "JAI", "TRV", "LKO", "GAU"],
    "US": ["ATL", "LAX", "ORD", "DFW", "DEN", "JFK", "SFO", "SEA", "LAS", "MCO", "MIA", "EWR", "BOS", "IAD", "IAH", "PHX", "CLT", "MSP", "DTW", "PHL", "SJU",
           "FLL", "TPA", "JAX", "RSW", "RDU"],
    "GB": ["LHR", "LGW", "MAN", "STN", "LTN", "EDI", "BHX", "GLA", "BRS", "NCL", "LPL", "BFS"],
    "CA": ["YYZ", "YVR", "YUL", "YYC", "YEG", "YOW", "YWG", "YHZ"],
    "AU": ["SYD", "MEL", "BNE", "PER", "ADL", "CBR", "OOL", "CNS"],
    "NZ": ["AKL", "WLG", "CHC", "ZQN"],
    "AE": ["DXB", "AUH", "SHJ", "DWC"],
    "SA": ["RUH", "JED", "DMM", "MED"],
    "QA": ["DOH"], "KW": ["KWI"], "BH": ["BAH"], "OM": ["MCT"],
    "SG": ["SIN"], "MY": ["KUL", "PEN", "BKI", "LGK", "KCH"],
    "TH": ["BKK", "DMK", "HKT", "CNX", "USM", "KBV"],
    "ID": ["CGK", "DPS", "SUB", "JOG", "UPG", "KNO"],
    "PH": ["MNL", "CEB", "CRK", "DVO"],
    "VN": ["SGN", "HAN", "DAD", "CXR", "PQC"],
    "KH": ["PNH", "REP"], "LA": ["VTE", "LPQ"], "MM": ["RGN", "MDL"],
    "CN": ["PEK", "PKX", "PVG", "SHA", "CAN", "SZX", "CTU", "CKG", "XIY", "KMG", "HGH", "NKG", "WUH"],
    "HK": ["HKG"], "MO": ["MFM"], "TW": ["TPE", "KHH", "TSA"],
    "JP": ["HND", "NRT", "KIX", "ITM", "NGO", "FUK", "CTS", "OKA"],
    "KR": ["ICN", "GMP", "PUS", "CJU"],
    "LK": ["CMB"], "NP": ["KTM"], "BD": ["DAC", "CGP"], "PK": ["KHI", "LHE", "ISB"], "MV": ["MLE"],
    "DE": ["FRA", "MUC", "BER", "DUS", "HAM", "CGN", "STR"],
    "FR": ["CDG", "ORY", "NCE", "LYS", "MRS", "TLS", "BOD", "NTE"],
    "ES": ["MAD", "BCN", "PMI", "AGP", "ALC", "VLC", "SVQ", "TFS", "LPA", "IBZ"],
    "IT": ["FCO", "MXP", "LIN", "VCE", "NAP", "BGY", "BLQ", "FLR", "CTA", "PMO"],
    "PT": ["LIS", "OPO", "FAO", "FNC"],
    "NL": ["AMS", "EIN", "RTM"], "BE": ["BRU", "CRL"], "LU": ["LUX"],
    "CH": ["ZRH", "GVA", "BSL"], "AT": ["VIE", "SZG", "INN"],
    "IE": ["DUB", "ORK", "SNN"], "IS": ["KEF"],
    "SE": ["ARN", "GOT", "MMX"], "NO": ["OSL", "BGO", "TRD", "SVG"],
    "DK": ["CPH", "BLL", "AAR"], "FI": ["HEL", "OUL", "RVN"],
    "PL": ["WAW", "KRK", "GDN", "WRO"], "CZ": ["PRG"], "HU": ["BUD"],
    "GR": ["ATH", "SKG", "HER", "RHO", "CFU", "JMK"],
    "TR": ["IST", "SAW", "AYT", "ESB", "ADB", "DLM", "BJV"],
    "RU": ["SVO", "DME", "VKO", "LED", "KZN", "SVX", "AER"],
    "UA": ["KBP", "LWO", "ODS"], "RO": ["OTP", "CLJ"], "BG": ["SOF", "VAR"],
    "HR": ["ZAG", "SPU", "DBV"], "RS": ["BEG"], "CY": ["LCA", "PFO"], "MT": ["MLA"],
    "EG": ["CAI", "HRG", "SSH", "LXR"], "MA": ["CMN", "RAK", "AGA", "TNG"],
    "ZA": ["JNB", "CPT", "DUR"], "KE": ["NBO", "MBA"], "ET": ["ADD"],
    "NG": ["LOS", "ABV"], "GH": ["ACC"], "TZ": ["DAR", "JRO", "ZNZ"], "TN": ["TUN"],
    "DZ": ["ALG"], "MU": ["MRU"], "SC": ["SEZ"],
    "IL": ["TLV"], "JO": ["AMM"], "LB": ["BEY"], "IR": ["IKA", "THR", "MHD"], "IQ": ["BGW", "EBL"],
    "KZ": ["ALA", "NQZ"], "UZ": ["TAS"], "GE": ["TBS"], "AM": ["EVN"], "AZ": ["GYD"],
    "MX": ["MEX", "CUN", "GDL", "MTY", "TIJ", "SJD", "PVR"],
    "BR": ["GRU", "GIG", "BSB", "CGH", "SDU", "CNF", "SSA", "REC", "POA"],
    "AR": ["EZE", "AEP", "COR"], "CL": ["SCL"], "CO": ["BOG", "MDE", "CTG"],
    "PE": ["LIM", "CUZ"], "EC": ["UIO", "GYE"], "UY": ["MVD"], "VE": ["CCS"],
    "PA": ["PTY"], "CR": ["SJO", "LIR"], "CU": ["HAV", "VRA"], "DO": ["PUJ", "SDQ"],
    "JM": ["KIN", "MBJ"], "BS": ["NAS"],
}


def _fallback_major(country_code):
    """For countries without a curated list: airports named 'International',
    else every airport if the country has few of them."""
    country_airports = [k for k, v in AIRPORTS.items() if v["country"] == country_code]
    intl = [k for k in country_airports if "international" in AIRPORTS[k]["name"].lower()]
    if intl:
        return intl
    return country_airports if len(country_airports) <= 5 else []


def _build_country_airports():
    result = {}
    for code in {v["country"] for v in AIRPORTS.values()}:
        codes = _MAJOR_AIRPORTS.get(code) or _fallback_major(code)
        # keep only codes that actually exist in the airport database
        result[code] = [c for c in codes if c in AIRPORTS]
    return result


# ISO alpha-2 -> list of major airport IATA codes
COUNTRY_AIRPORTS = _build_country_airports()


def get_country_airports(country_text):
    """'india' / 'IN' / 'Republic of India' -> [{'iata','name','city'}, ...]"""
    code = COUNTRY_ALIASES.get(country_text.strip().lower())
    if not code:
        return []
    return [
        {"iata": c, "name": AIRPORTS[c]["name"], "city": AIRPORTS[c]["city"]}
        for c in COUNTRY_AIRPORTS.get(code, [])
    ]


# ---------------------------------------------------------------------------
# Metro areas, cities, states
# ---------------------------------------------------------------------------

# IATA metropolitan-area codes: multi-airport cities.
_METRO_AIRPORTS = {
    "LON": {"name": "London", "airports": ["LHR", "LGW", "STN", "LTN", "LCY", "SEN"], "aliases": ["london"]},
    "NYC": {"name": "New York", "airports": ["JFK", "LGA", "EWR"], "aliases": ["new york", "new york city", "nyc", "ny"]},
    "PAR": {"name": "Paris", "airports": ["CDG", "ORY", "BVA"], "aliases": ["paris"]},
    "TYO": {"name": "Tokyo", "airports": ["HND", "NRT"], "aliases": ["tokyo"]},
    "OSA": {"name": "Osaka", "airports": ["KIX", "ITM"], "aliases": ["osaka"]},
    "SEL": {"name": "Seoul", "airports": ["ICN", "GMP"], "aliases": ["seoul"]},
    "BJS": {"name": "Beijing", "airports": ["PEK", "PKX"], "aliases": ["beijing", "peking"]},
    "SHA": {"name": "Shanghai", "airports": ["PVG", "SHA"], "aliases": ["shanghai"]},
    "WAS": {"name": "Washington DC", "airports": ["IAD", "DCA", "BWI"], "aliases": ["washington", "washington dc", "washington d.c.", "dc"]},
    "CHI": {"name": "Chicago", "airports": ["ORD", "MDW"], "aliases": ["chicago"]},
    "SFO": {"name": "San Francisco Bay Area", "airports": ["SFO", "OAK", "SJC"], "aliases": ["san francisco", "bay area"]},
    "QLA": {"name": "Los Angeles Area", "airports": ["LAX", "BUR", "LGB", "SNA", "ONT"], "aliases": ["los angeles", "la"]},
    "MIL": {"name": "Milan", "airports": ["MXP", "LIN", "BGY"], "aliases": ["milan", "milano"]},
    "ROM": {"name": "Rome", "airports": ["FCO", "CIA"], "aliases": ["rome", "roma"]},
    "MOW": {"name": "Moscow", "airports": ["SVO", "DME", "VKO"], "aliases": ["moscow"]},
    "STO": {"name": "Stockholm", "airports": ["ARN", "BMA", "NYO"], "aliases": ["stockholm"]},
    "BUH": {"name": "Bucharest", "airports": ["OTP", "BBU"], "aliases": ["bucharest"]},
    "SAO": {"name": "Sao Paulo", "airports": ["GRU", "CGH", "VCP"], "aliases": ["sao paulo", "são paulo"]},
    "RIO": {"name": "Rio de Janeiro", "airports": ["GIG", "SDU"], "aliases": ["rio", "rio de janeiro"]},
    "BUE": {"name": "Buenos Aires", "airports": ["EZE", "AEP"], "aliases": ["buenos aires"]},
    "YTO": {"name": "Toronto", "airports": ["YYZ", "YTZ"], "aliases": ["toronto"]},
    "YMQ": {"name": "Montreal", "airports": ["YUL", "YMX"], "aliases": ["montreal"]},
    "IST": {"name": "Istanbul", "airports": ["IST", "SAW"], "aliases": ["istanbul"]},
    "BKK": {"name": "Bangkok", "airports": ["BKK", "DMK"], "aliases": ["bangkok"]},
    "JKT": {"name": "Jakarta", "airports": ["CGK", "HLP"], "aliases": ["jakarta"]},
    "DFW": {"name": "Dallas-Fort Worth", "airports": ["DFW", "DAL"], "aliases": ["dallas", "fort worth", "dallas fort worth"]},
    "MIA": {"name": "Miami Area", "airports": ["MIA", "FLL"], "aliases": ["miami", "south florida"]},
    "TPE": {"name": "Taipei", "airports": ["TPE", "TSA"], "aliases": ["taipei"]},
    "BER": {"name": "Berlin", "airports": ["BER"], "aliases": ["berlin"]},
    "DEL": {"name": "Delhi", "airports": ["DEL", "HDO"], "aliases": ["delhi", "new delhi", "ncr", "delhi ncr"]},
}

# Alternate spellings/names -> the name used in airportsdata (lowercase).
_CITY_ALIASES = {
    "bombay": "mumbai", "bengaluru": "bangalore", "madras": "chennai",
    "calcutta": "kolkata", "poona": "pune", "cochin": "kochi",
    "trivandrum": "thiruvananthapuram", "vizag": "visakhapatnam",
    "newark": "newark", "saigon": "ho chi minh city", "peking": "beijing",
    "bkk": "bangkok", "kuala lumpur": "kuala lumpur",
}


def _airport_info(codes):
    """IATA codes -> [{'iata','name','city'}], skipping codes not in the database."""
    return [
        {"iata": c, "name": AIRPORTS[c]["name"], "city": AIRPORTS[c]["city"]}
        for c in codes if c in AIRPORTS
    ]


def _build_metro_index():
    """metro code and every metro alias (lowercase) -> metro code."""
    index = {}
    for code, m in _METRO_AIRPORTS.items():
        index[code.lower()] = code
        for a in m["aliases"]:
            index[a.lower()] = code
    return index


def _build_city_index():
    """lowercase city name -> list of IATA codes."""
    index = {}
    for iata, a in AIRPORTS.items():
        if a["city"]:  # some entries have no city; don't index them under ""
            index.setdefault(a["city"].lower(), []).append(iata)
    for alias, target in _CITY_ALIASES.items():
        if target in index:
            index.setdefault(alias, index[target])
    return index


def _build_state_index():
    """(country, subdivision lowercase) -> major airports in that state/region.

    Uses the curated major list for the country; if none of its airports are in
    that state, falls back to airports named 'International'."""
    by_state = {}
    for iata, a in AIRPORTS.items():
        if a["subd"]:
            by_state.setdefault((a["country"], a["subd"].lower()), []).append(iata)
    index = {}
    for key, codes in by_state.items():
        major = set(_MAJOR_AIRPORTS.get(key[0], []))
        picked = [c for c in codes if c in major]
        if not picked:
            picked = [c for c in codes if "international" in AIRPORTS[c]["name"].lower()]
        if picked:
            # keep the curated order (busiest first) where we have one
            order = _MAJOR_AIRPORTS.get(key[0], [])
            picked.sort(key=lambda c: order.index(c) if c in order else len(order))
            index[key] = picked
    return index


_METRO_INDEX = _build_metro_index()
_CITY_INDEX = _build_city_index()
_STATE_INDEX = _build_state_index()
# state name (lowercase) -> list of (country, state) keys, since names can repeat
_STATE_NAMES = {}
for _country, _subd in _STATE_INDEX:
    _STATE_NAMES.setdefault(_subd, []).append((_country, _subd))


def search_airports(query):
    """Resolve free text to airports. Returns a list of candidates:
    [{'type': 'airport'|'metro'|'city'|'state'|'country', 'label': str,
      'airports': [{'iata','name','city'}]}]
    Ambiguous text (e.g. 'georgia') returns every candidate."""
    text = (query or "").strip()
    q = text.lower()
    if not q:
        return []

    # 1. exact IATA code (user typed e.g. "HYD" / "hyd")
    if len(q) == 3 and q.upper() in AIRPORTS and q not in _METRO_INDEX:
        a = AIRPORTS[q.upper()]
        return [{"type": "airport", "label": a["name"], "airports": _airport_info([q.upper()])}]

    results = []

    # 2. metro area (code or name)
    metro = _METRO_INDEX.get(q)
    if metro:
        m = _METRO_AIRPORTS[metro]
        results.append({"type": "metro", "label": m["name"], "airports": _airport_info(m["airports"])})

    # 3. city (only if no metro already covers it)
    if not metro:
        codes = _CITY_INDEX.get(q)
        if codes:
            results.append({"type": "city", "label": AIRPORTS[codes[0]]["city"], "airports": _airport_info(codes)})

    # 4. state / region
    for country, subd in _STATE_NAMES.get(q, []):
        results.append({
            "type": "state",
            "label": f"{subd.title()}, {country}",
            "airports": _airport_info(_STATE_INDEX[(country, subd)]),
        })

    # 5. country
    country_airports = get_country_airports(q)
    if country_airports:
        code = COUNTRY_ALIASES[q]
        results.append({"type": "country", "label": code, "airports": country_airports})

    return results


# ---------------------------------------------------------------------------
# Natural-language query -> route -> AviationStack
# ---------------------------------------------------------------------------

# Uppercase English words that happen to be valid IATA codes (ALL, THE, ...).
_CODE_STOPWORDS = {"ALL", "THE", "AND", "FOR", "ARE", "NOT", "CAN", "NOW", "ANY", "OUR", "YOU"}
# Words that must never be treated as a place name when scanning free text.
_MENTION_STOPWORDS = {
    "plan", "trip", "tour", "days", "day", "week", "weeks", "from", "flight", "flights",
    "for", "the", "and", "with", "under", "including", "show", "find", "book", "next",
    "live", "info", "all", "any", "best", "cheap", "this", "that", "budget", "hotel",
}
_ALL_MAJOR = {c for codes in _MAJOR_AIRPORTS.values() for c in codes}
_PATTERN_END =r"(?:\s+(?:on|for|under|including|with|in|at)\b|[.!?]|$)"


def resolve_location_to_iata(location: str):
    """Country / city / metro / state / airport / IATA text -> one IATA code.

    Examples: Bangladesh -> DAC, Japan -> HND, Tokyo -> HND, DAC -> DAC.
    Delegates to search_airports(); the first candidate's first (busiest) airport wins.
    """
    codes = resolve_location_to_iatas(location, limit=1)
    return codes[0] if codes else None


def resolve_location_to_iatas(location: str, limit: int = 3):
    """Like resolve_location_to_iata, but returns up to `limit` airports (busiest first).

    A country or metro area has several airports (Japan: HND, NRT, KIX), and a route may
    only exist between some of them, so callers should try each.
    """
    for candidate in search_airports(location):
        codes = [a["iata"] for a in candidate["airports"]]
        if not codes:
            continue
        # Same city name exists in several countries (Hyderabad IN/PK): prefer curated major airports.
        majors = [c for c in codes if c in _ALL_MAJOR]
        if candidate["type"] == "city":
            # A plain city name: only its main airport (avoids HDD/BPM for "Hyderabad").
            return (majors or codes)[:1]
        return (majors + [c for c in codes if c not in _ALL_MAJOR])[:limit]
    return []


def _is_known_place(phrase: str) -> bool:
    if phrase in _METRO_INDEX or phrase in _CITY_INDEX:
        return len(phrase) >= 3
    if phrase in _EXTRA_COUNTRY_ALIASES:
        return phrase != "us"
    return phrase in COUNTRY_ALIASES and len(phrase) >= 4


def find_location_mentions(query: str):
    """Place names (countries, cities, metro areas) found in free text, in order of appearance.

    Scans word n-grams (longest first) against the existing alias/city/metro indexes.
    """
    tokens = re.findall(r"[^\W_]+(?:['.\-][^\W_]+)*", query.lower())
    used = [False] * len(tokens)
    found = []  # (position, phrase)

    for n in range(4, 0, -1):
        for i in range(len(tokens) - n + 1):
            if any(used[i:i + n]):
                continue
            phrase = " ".join(tokens[i:i + n])
            if n == 1 and phrase in _MENTION_STOPWORDS:
                continue
            if _is_known_place(phrase):
                found.append((i, phrase))
                for j in range(i, i + n):
                    used[j] = True

    found.sort()
    mentions = []
    for _, phrase in found:
        if phrase not in mentions:
            mentions.append(phrase)
    return mentions


def _resolve_phrase(text: str):
    """Resolve a captured phrase; if it is noisy ('japan trip'), fall back to place mentions in it."""
    codes = resolve_location_to_iatas(text)
    if codes:
        return codes
    for mention in find_location_mentions(text):
        codes = resolve_location_to_iatas(mention)
        if codes:
            return codes
    return []


def parse_route(query: str):
    """Returns (dep_iata, arr_iata): the best single airport per side, or None.

    (None, None) -> global live flights;  (DAC, NRT) -> route;
    (DAC, None)  -> all from DAC;         (None, NRT) -> all to NRT.
    """
    dep, arr = parse_route_options(query)
    return (dep[0] if dep else None), (arr[0] if arr else None)


def parse_route_options(query: str):
    """Returns (dep_codes, arr_codes): candidate airport lists (empty list = unconstrained)."""
    q = query.strip()
    q_lower = q.lower()

    global_keywords = [
        "all country", "all countries", "global flight", "global flights",
        "all flight", "all flights", "worldwide flight", "worldwide flights",
    ]
    if any(k in q_lower for k in global_keywords):
        return [], []

    # Explicit IATA codes typed in uppercase: "DAC to NRT"
    codes = [c for c in re.findall(r"\b[A-Z]{3}\b", q) if c in AIRPORTS and c not in _CODE_STOPWORDS]
    if len(codes) >= 2:
        return [codes[0]], [codes[1]]

    dep_iata, arr_iata = [], []
    matched_pattern = False

    m = re.search(rf"\bfrom\s+(.+?)\s+\bto\s+(.+?){_PATTERN_END}", q_lower)
    if m:
        dep_iata, arr_iata = _resolve_phrase(m.group(1)), _resolve_phrase(m.group(2))
        matched_pattern = True
    else:
        m = re.search(rf"\bto\s+(.+?)\s+\bfrom\s+(.+?){_PATTERN_END}", q_lower)
        if m:
            arr_iata, dep_iata = _resolve_phrase(m.group(1)), _resolve_phrase(m.group(2))
            matched_pattern = True
        else:
            m = re.search(rf"\bfrom\s+(.+?){_PATTERN_END}", q_lower)
            if m:
                dep_iata = _resolve_phrase(m.group(1))
                matched_pattern = True
            else:
                m = re.search(rf"\bto\s+(.+?){_PATTERN_END}", q_lower)
                if m:
                    arr_iata = _resolve_phrase(m.group(1))
                    matched_pattern = True

    if dep_iata and arr_iata:
        return dep_iata, arr_iata

    # One side missing: look for another place in the text,
    # e.g. "7 days Japan trip from Bangladesh" -> from=Bangladesh, other mention=Japan.
    mention_iatas = []  # list of airport-code lists, one per distinct place
    for mention in find_location_mentions(q):
        options = resolve_location_to_iatas(mention)
        if options and all(options[0] != o[0] for o in mention_iatas):
            mention_iatas.append(options)

    known = dep_iata or arr_iata
    others = [o for o in mention_iatas if not known or o[0] != known[0]]

    if matched_pattern:
        if dep_iata and not arr_iata and others:
            arr_iata = others[0]
        elif arr_iata and not dep_iata and others:
            dep_iata = others[0]
        return dep_iata, arr_iata

    # No from/to wording at all
    if len(mention_iatas) >= 2:
        return mention_iatas[0], mention_iatas[1]
    if len(mention_iatas) == 1:
        return [DEFAULT_ORIGIN_IATA], mention_iatas[0]
    return [], []


def format_flight(flight: dict):
    airline = (flight.get("airline") or {}).get("name") or "Unknown airline"
    flight_number = (flight.get("flight") or {}).get("iata") or "Unknown flight number"
    status = flight.get("flight_status") or "Unknown"

    dep = flight.get("departure") or {}
    arr = flight.get("arrival") or {}

    def delay_text(d):
        return f"{d} minutes" if d is not None else "N/A"

    return f"""
Airline: {airline}
Flight: {flight_number}
Status: {status}

Departure:
- Airport: {dep.get("airport") or "Unknown departure airport"}
- IATA: {dep.get("iata") or "Unknown"}
- Terminal: {dep.get("terminal") or "N/A"}
- Gate: {dep.get("gate") or "N/A"}
- Scheduled: {dep.get("scheduled") or "Unknown"}
- Delay: {delay_text(dep.get("delay"))}

Arrival:
- Airport: {arr.get("airport") or "Unknown arrival airport"}
- IATA: {arr.get("iata") or "Unknown"}
- Terminal: {arr.get("terminal") or "N/A"}
- Gate: {arr.get("gate") or "N/A"}
- Scheduled: {arr.get("scheduled") or "Unknown"}
- Delay: {delay_text(arr.get("delay"))}
""".strip()

def format_flights_compact(flights: list, max_flights: int = 5):
    """One short line per distinct flight number (the API repeats the same flight on
    several days), to keep LLM prompts small."""
    seen, lines = set(), []
    for f in flights:
        number = (f.get("flight") or {}).get("iata")
        if not number or number in seen:
            continue
        seen.add(number)
        airline = (f.get("airline") or {}).get("name") or "Unknown airline"
        dep = f.get("departure") or {}
        arr = f.get("arrival") or {}
        dep_time = (dep.get("scheduled") or "")[11:16] or "?"
        arr_time = (arr.get("scheduled") or "")[11:16] or "?"
        lines.append(
            f"- {airline} {number}: {dep.get('iata') or '?'} {dep_time} -> "
            f"{arr.get('iata') or '?'} {arr_time}"
        )
        if len(lines) >= max_flights:
            break
    return "\n".join(lines)


@traceable
def search_flights(query: str, limit: int = 10, compact: bool = False):
    if not api_key:
        return (
            "Flight API error: AVIATIONSTACK_API_KEY is missing.\n"
            "Please add this in your .env file:\n"
            "AVIATIONSTACK_API_KEY=your_api_key_here"
        )

    dep_codes, arr_codes = parse_route_options(query)

    # A country/metro has several airports and a route may exist for only some of them
    # (Bangladesh -> Japan: DAC-HND has no flights, DAC-NRT does), so try each pair.
    # Capped at 1 departure x 2 arrival airports (max 2 API calls) to protect the API quota.
    pairs = [(d, a) for d in (dep_codes[:1] or [None]) for a in (arr_codes[:2] or [None])]
    flight_data, tried = [], []

    for dep_iata, arr_iata in pairs:
        params = {"access_key": api_key, "limit": min(limit - len(flight_data), 100)}
        if dep_iata:
            params["dep_iata"] = dep_iata
        if arr_iata:
            params["arr_iata"] = arr_iata
        tried.append(_route_text(dep_iata, arr_iata))

        try:
            response = requests.get(BASE_URL, params=params, timeout=30)
            data = response.json()
        except requests.exceptions.RequestException as e:
            return f"Flight API request failed: {e}"
        except ValueError:
            return "Flight API returned invalid JSON."

        if "error" in data:
            error = data["error"]
            return (
                "Flight API error:\n"
                f"Code: {error.get('code', 'Unknown')}\n"
                f"Message: {error.get('message', 'Unknown error')}"
            )

        flight_data.extend(data.get("data", []))
        if len(flight_data) >= limit:
            break

    if not flight_data:
        return (
            "No live flight data found for: " + "; ".join(tried) + ".\n\n"
            "Note: AviationStack provides live/status flight data (flights currently active), "
            "not schedules or ticket prices. For actual fare prices, use a flight-pricing API such as Amadeus."
        )

    shown = sorted({(f.get("departure") or {}).get("iata") + " to " + (f.get("arrival") or {}).get("iata")
                    for f in flight_data
                    if (f.get("departure") or {}).get("iata") and (f.get("arrival") or {}).get("iata")})
    route_info = "Live flights " + (", ".join(shown) if shown else "") if tried[0] != "" else "Global live flights"
    if compact:
        return f"{route_info}\n\n" + format_flights_compact(flight_data)

    formatted = [format_flight(f) for f in flight_data[:limit]]
    return f"{route_info}\n\n" + "\n\n---\n\n".join(formatted)


def _route_text(dep_iata, arr_iata):
    if dep_iata and arr_iata:
        return f"{dep_iata} to {arr_iata}"
    if dep_iata:
        return f"from {dep_iata}"
    if arr_iata:
        return f"to {arr_iata}"
    return ""


if __name__ == "__main__":
    print(search_flights("Plan a 7 days Japan trip from Bangladesh"))
    print("\n" + "=" * 80 + "\n")
    print(search_flights("all country flight info"))

