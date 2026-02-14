#!/usr/bin/env python3
"""Weather executor - retrieves weather data from wttr.in.

No authentication required. Accepts location as argument.
Outputs JSON to stdout.
"""

from __future__ import annotations

import json
import os
import sys

import httpx

DEFAULT_LOCATION = "Denver"
WTTR_URL = "https://wttr.in"


def fetch_weather(location: str) -> dict:
    """Fetch weather data for a location from wttr.in."""
    url = f"{WTTR_URL}/{location}"
    params = {"format": "j1"}

    resp = httpx.get(url, params=params, timeout=15.0)
    resp.raise_for_status()
    data = resp.json()

    current = data.get("current_condition", [{}])[0]
    weather_area = data.get("nearest_area", [{}])[0]
    forecast_days = data.get("weather", [])

    area_name = ""
    if weather_area.get("areaName"):
        area_name = weather_area["areaName"][0].get("value", "")

    result = {
        "location": area_name or location,
        "temp_f": current.get("temp_F", ""),
        "temp_c": current.get("temp_C", ""),
        "feels_like_f": current.get("FeelsLikeF", ""),
        "condition": current.get("weatherDesc", [{}])[0].get("value", ""),
        "humidity": current.get("humidity", ""),
        "wind_mph": current.get("windspeedMiles", ""),
        "forecast": [],
    }

    for day in forecast_days[:3]:
        result["forecast"].append(
            {
                "date": day.get("date", ""),
                "high_f": day.get("maxtempF", ""),
                "low_f": day.get("mintempF", ""),
                "condition": day.get("hourly", [{}])[4].get("weatherDesc", [{}])[0].get("value", "")
                if len(day.get("hourly", [])) > 4
                else "",
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
    except httpx.HTTPError as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
