#!/usr/bin/env python3
"""Weather executor - retrieves weather data from Open-Meteo.

No authentication required. Accepts location as argument.
Uses Open-Meteo geocoding to resolve location names to coordinates.
Outputs JSON to stdout.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

import httpx

DEFAULT_LOCATION = "Denver"
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# WMO weather codes → human-readable descriptions
WMO_CODES: dict[int, str] = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Foggy",
    48: "Depositing rime fog",
    51: "Light drizzle",
    53: "Moderate drizzle",
    55: "Dense drizzle",
    56: "Light freezing drizzle",
    57: "Dense freezing drizzle",
    61: "Slight rain",
    63: "Moderate rain",
    65: "Heavy rain",
    66: "Light freezing rain",
    67: "Heavy freezing rain",
    71: "Slight snow",
    73: "Moderate snow",
    75: "Heavy snow",
    77: "Snow grains",
    80: "Slight rain showers",
    81: "Moderate rain showers",
    82: "Violent rain showers",
    85: "Slight snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with slight hail",
    99: "Thunderstorm with heavy hail",
}


def _geocode(location: str) -> dict:
    """Resolve a location name to coordinates via Open-Meteo geocoding."""
    # Open-Meteo geocoder works best with city name only — strip state/country
    # suffixes like "Denver, CO" or "Denver, Colorado, US".
    city = location.split(",")[0].strip()

    resp = httpx.get(
        GEOCODE_URL,
        params={"name": city, "count": 1, "language": "en", "format": "json"},
        timeout=10.0,
    )
    resp.raise_for_status()
    data = resp.json()
    results = data.get("results")
    if not results:
        raise ValueError(f"Location not found: {location}")
    return results[0]


def _weather_description(code: int) -> str:
    """Convert WMO weather code to description."""
    return WMO_CODES.get(code, f"Unknown ({code})")


def fetch_weather(location: str) -> dict:
    """Fetch weather data for a location from Open-Meteo."""
    geo = _geocode(location)
    lat = geo["latitude"]
    lon = geo["longitude"]
    name = geo.get("name", location)
    admin = geo.get("admin1", "")
    area_name = f"{name}, {admin}" if admin else name

    resp = httpx.get(
        FORECAST_URL,
        params={
            "latitude": lat,
            "longitude": lon,
            "current": "temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m",
            "daily": "temperature_2m_max,temperature_2m_min,weather_code",
            "temperature_unit": "fahrenheit",
            "wind_speed_unit": "mph",
            "timezone": "auto",
            "forecast_days": 3,
        },
        timeout=10.0,
    )
    resp.raise_for_status()
    data = resp.json()

    current = data.get("current", {})
    daily = data.get("daily", {})

    forecast: list[dict[str, str]] = []
    result: dict[str, Any] = {
        "location": area_name,
        "temp_f": str(current.get("temperature_2m", "")),
        "temp_c": "",  # Open-Meteo returns in requested unit; we request F
        "feels_like_f": str(current.get("apparent_temperature", "")),
        "condition": _weather_description(current.get("weather_code", -1)),
        "humidity": str(current.get("relative_humidity_2m", "")),
        "wind_mph": str(current.get("wind_speed_10m", "")),
        "forecast": forecast,
    }

    dates = daily.get("time", [])
    highs = daily.get("temperature_2m_max", [])
    lows = daily.get("temperature_2m_min", [])
    codes = daily.get("weather_code", [])

    for i in range(min(3, len(dates))):
        forecast.append(
            {
                "date": dates[i],
                "high_f": str(highs[i]) if i < len(highs) else "",
                "low_f": str(lows[i]) if i < len(lows) else "",
                "condition": _weather_description(codes[i]) if i < len(codes) else "",
            }
        )

    return result


def main() -> None:
    location = os.environ.get("LOCATION", DEFAULT_LOCATION)
    # Also accept as first CLI arg
    if len(sys.argv) > 1:
        location = sys.argv[1]

    try:
        result = fetch_weather(location)
        print(json.dumps(result, indent=2))
    except (httpx.HTTPError, ValueError) as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
