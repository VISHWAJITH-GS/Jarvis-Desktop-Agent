"""
plugins/weather_travel.py — Real-Time Weather & Travel Flight Finder Plugin for JARVIS.

Capabilities:
- weather_get: Fetches real-time weather and 3-day forecast for any city or location
  using lightweight, keyless Open-Meteo / wttr.in APIs with JSON structured output.
- flight_search: Generates structured flight search comparisons and deep-links
  for Google Flights and Skyscanner with flexible date parsing.
- flight_open: Opens the generated flight search in the default web browser.
"""

from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timedelta
from typing import Any, Optional

from core import outcome

logger = logging.getLogger("weather_travel")


def _geocode_city(city_name: str) -> Optional[tuple[float, float, str]]:
    """Geocode city to lat/lon using Open-Meteo public geocoding API."""
    try:
        url = f"https://geocoding-api.open-meteo.com/v1/search?name={urllib.parse.quote_plus(city_name)}&count=1&language=en&format=json"
        req = urllib.request.Request(url, headers={"User-Agent": "Jarvis-Desktop/1.0"})
        with urllib.request.urlopen(req, timeout=6.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            results = data.get("results", [])
            if results:
                r = results[0]
                resolved_name = f"{r.get('name')}, {r.get('country', '')}".strip(", ")
                return float(r["latitude"]), float(r["longitude"]), resolved_name
    except Exception as e:
        logger.debug("Geocoding failed for '%s': %s", city_name, e)
    return None


def _weather_code_to_desc(code: int) -> str:
    codes = {
        0: "Clear sky",
        1: "Mainly clear",
        2: "Partly cloudy",
        3: "Overcast",
        45: "Foggy",
        48: "Depositing rime fog",
        51: "Light drizzle",
        53: "Moderate drizzle",
        55: "Dense drizzle",
        61: "Slight rain",
        63: "Moderate rain",
        65: "Heavy rain",
        71: "Slight snow",
        73: "Moderate snow",
        75: "Heavy snow",
        80: "Slight rain showers",
        81: "Moderate rain showers",
        82: "Violent rain showers",
        95: "Thunderstorm",
        96: "Thunderstorm with slight hail",
        99: "Thunderstorm with heavy hail",
    }
    return codes.get(code, "Cloudy / Variable")


def weather_get(city: str = "London") -> str:
    if not city:
        city = "London"

    coords = _geocode_city(city)
    if not coords:
        # Fallback to wttr.in simple JSON
        try:
            wttr_url = f"https://wttr.in/{urllib.parse.quote_plus(city)}?format=j1"
            req = urllib.request.Request(wttr_url, headers={"User-Agent": "curl/7.68.0"})
            with urllib.request.urlopen(req, timeout=8.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                current = data.get("current_condition", [{}])[0]
                temp_c = current.get("temp_C", "N/A")
                temp_f = current.get("temp_F", "N/A")
                desc = current.get("weatherDesc", [{}])[0].get("value", "Clear")
                humidity = current.get("humidity", "N/A")
                wind_kmh = current.get("windspeedKmph", "N/A")

                forecast_days = []
                for w in data.get("weather", [])[:3]:
                    forecast_days.append({
                        "date": w.get("date"),
                        "max_c": w.get("maxtempC"),
                        "min_c": w.get("mintempC"),
                    })

                return outcome.done(
                    "weather_get",
                    city=city,
                    temperature_c=temp_c,
                    temperature_f=temp_f,
                    condition=desc,
                    humidity_pct=humidity,
                    wind_kmh=wind_kmh,
                    forecast=forecast_days,
                )
        except Exception as e:
            return outcome.failed("weather_get", f"Could not fetch weather for '{city}': {e}")

    lat, lon, resolved_city = coords
    try:
        meteo_url = (
            f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
            f"&current=temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m"
            f"&daily=weather_code,temperature_2m_max,temperature_2m_min&timezone=auto"
        )
        req = urllib.request.Request(meteo_url, headers={"User-Agent": "Jarvis-Desktop/1.0"})
        with urllib.request.urlopen(req, timeout=6.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            curr = data.get("current", {})
            daily = data.get("daily", {})

            temp_c = curr.get("temperature_2m")
            temp_f = round((temp_c * 9 / 5) + 32, 1) if temp_c is not None else None
            condition = _weather_code_to_desc(curr.get("weather_code", 0))
            humidity = curr.get("relative_humidity_2m")
            wind = curr.get("wind_speed_10m")

            dates = daily.get("time", [])[:3]
            maxs = daily.get("temperature_2m_max", [])[:3]
            mins = daily.get("temperature_2m_min", [])[:3]
            codes = daily.get("weather_code", [])[:3]

            forecast = []
            for d, mx, mn, cd in zip(dates, maxs, mins, codes):
                forecast.append({
                    "date": d,
                    "condition": _weather_code_to_desc(cd),
                    "max_c": mx,
                    "min_c": mn,
                })

            return outcome.done(
                "weather_get",
                city=resolved_city,
                temperature_c=temp_c,
                temperature_f=temp_f,
                condition=condition,
                humidity_pct=humidity,
                wind_kmh=wind,
                forecast=forecast,
            )
    except Exception as e:
        logger.error("Open-Meteo error: %s", e)
        return outcome.failed("weather_get", f"Weather service error: {e}")


def flight_search(
    origin: str,
    destination: str,
    date: Optional[str] = None,
    return_date: Optional[str] = None,
    cabin: str = "economy",
) -> str:
    if not origin or not destination:
        return outcome.failed("flight_search", "Missing origin or destination.")

    dep_date = date or (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")

    # Build deep links
    query_parts = f"Flights from {origin} to {destination} on {dep_date}"
    if return_date:
        query_parts += f" returning {return_date}"

    google_flights_url = (
        f"https://www.google.com/travel/flights?q={urllib.parse.quote_plus(query_parts)}&curr=USD"
    )

    clean_origin = origin.strip().upper()[:3]
    clean_dest = destination.strip().upper()[:3]
    skyscanner_url = f"https://www.skyscanner.com/transport/flights/{clean_origin}/{clean_dest}/{dep_date}/"

    return outcome.done(
        "flight_search",
        origin=origin,
        destination=destination,
        date=dep_date,
        return_date=return_date,
        cabin=cabin,
        google_flights_url=google_flights_url,
        skyscanner_url=skyscanner_url,
    )


def flight_open(url: str) -> str:
    if not url or not url.startswith("http"):
        return outcome.failed("flight_open", "Invalid flight URL.")
    try:
        webbrowser.open(url)
        return outcome.done("flight_open", url=url, verified=True)
    except Exception as e:
        return outcome.failed("flight_open", f"Failed to open browser: {e}")


def run(parameters: dict, player: Any = None, session_memory: Any = None) -> str:
    params = parameters or {}
    action = str(params.get("action", "")).lower().strip()

    if action in ("weather_get", "weather", "forecast"):
        city = str(params.get("city") or params.get("location", "London"))
        res = weather_get(city)
        if player and hasattr(player, "write_log"):
            try:
                player.write_log(f"🌤️ Weather requested for {city}")
            except Exception:
                pass
        return res

    elif action in ("flight_search", "flights"):
        origin = str(params.get("origin", ""))
        dest = str(params.get("destination", ""))
        date = params.get("date")
        ret_date = params.get("return_date")
        cabin = str(params.get("cabin", "economy"))
        return flight_search(origin, dest, date=date, return_date=ret_date, cabin=cabin)

    elif action in ("flight_open", "open_flight"):
        url = str(params.get("url", ""))
        return flight_open(url)

    return outcome.failed(
        "weather_travel",
        f"Unknown action '{action}'. Supported: weather_get, flight_search, flight_open",
    )


PLUGIN = {
    "name": "weather_travel",
    "description": (
        "Weather & Travel assistant: retrieves current weather conditions and 3-day forecasts "
        "for any city worldwide, and generates flight comparison deep-links across Google Flights and Skyscanner."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "Exact action: weather_get | flight_search | flight_open",
            },
            "city": {
                "type": "STRING",
                "description": "City or location name for weather_get.",
            },
            "origin": {
                "type": "STRING",
                "description": "Origin city or airport code for flight_search.",
            },
            "destination": {
                "type": "STRING",
                "description": "Destination city or airport code for flight_search.",
            },
            "date": {
                "type": "STRING",
                "description": "Departure date (YYYY-MM-DD) for flight_search.",
            },
            "return_date": {
                "type": "STRING",
                "description": "Optional return date (YYYY-MM-DD) for round trips.",
            },
            "cabin": {
                "type": "STRING",
                "description": "Cabin class: economy | premium | business | first.",
            },
            "url": {
                "type": "STRING",
                "description": "Flight search URL to open in browser for flight_open.",
            },
        },
        "required": ["action"],
    },
    "permissions": ["weather_travel"],
}
