"""Custom Weather MCP server for TripMate.

Exposes two tools backed by the OpenWeatherMap API:
    - get_current_weather: live conditions for a city
    - get_forecast: the next few 3-hour forecast entries for a city
The TripMate agents call them over MCP (stdio) to include real weather in
trip plans.

Setup:
    - Add OPENWEATHER_API_KEY to your .env file
      (free key: https://openweathermap.org/api)

Run:
    python custom_weather_mcp_server.py
"""

import os

import requests
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

# Load OPENWEATHER_API_KEY from the .env file
load_dotenv()

# Name of the MCP server, as clients will see it
mcp = FastMCP("weather")


def _request_json(url: str, city: str) -> dict:
    """Call an OpenWeather endpoint for a city and return the parsed JSON.

    Shared by all tools. Raises ValueError if the city is empty or the API
    doesn't return 200.
    """
    city = city.strip()
    if not city:
        raise ValueError("city cannot be empty")

    response = requests.get(
        url,
        params={
            "q": city,
            "appid": os.getenv("OPENWEATHER_API_KEY"),
            "units": "metric",  # Celsius and m/s
        },
        timeout=10,  # don't hang forever if the API is slow
    )
    data = response.json()

    # OpenWeather returns 200 on success; otherwise "message" says why
    # (e.g. 404 "city not found", 401 "Invalid API key")
    if response.status_code != 200:
        raise ValueError(
            f"Weather lookup failed ({response.status_code}): {data.get('message')}"
        )
    return data


@mcp.tool()
def get_current_weather(city: str) -> dict:
    """Return the current weather for a city.

    The docstring is shown to the LLM as the tool description, so it
    explains when and how to use the tool.

    Args:
        city: City name, e.g. "Paris" or "Paris,FR".

    Returns:
        A dict with city, temperature_c, feels_like_c, humidity (%),
        condition (e.g. "light rain") and wind_speed (m/s).
    """
    data = _request_json("https://api.openweathermap.org/data/2.5/weather", city)

    # Return only the fields the agent needs, not the whole API response
    return {
        "city": data["name"],
        "temperature_c": data["main"]["temp"],
        "feels_like_c": data["main"]["feels_like"],
        "humidity": data["main"]["humidity"],
        "condition": data["weather"][0]["description"],
        "wind_speed": data["wind"]["speed"],
    }


@mcp.tool()
def get_forecast(city: str) -> dict:
    """Return the next five 3-hour forecast entries (about 15 hours) for a city.

    Args:
        city: City name, e.g. "Paris" or "Paris,FR".

    Returns:
        A dict with city and a forecast list; each entry has datetime,
        temperature_c and condition.
    """
    data = _request_json("https://api.openweathermap.org/data/2.5/forecast", city)

    # The API returns ~40 entries (5 days); keep just the first 5
    forecast = [
        {
            "datetime": item["dt_txt"],
            "temperature_c": item["main"]["temp"],
            "condition": item["weather"][0]["description"],
        }
        for item in data.get("list", [])[:5]
    ]

    return {
        "city": data["city"]["name"],
        "forecast": forecast,
    }


if __name__ == "__main__":
    # mcp_client.py launches this as a stdio subprocess.
    mcp.run(
        transport="stdio",
    )

