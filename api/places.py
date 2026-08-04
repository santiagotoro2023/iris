"""Transit and places (SPEC.md Phase 6).

The two integrations that need no credentials, so they work the moment IRiS is
installed: Swiss public transport via transport.opendata.ch and place lookup via
OpenStreetMap's Nominatim (both named in SPEC.md 5).

"Home" and "work" are settings rather than an ASK USER. Santiago fills them in the
UI once and then asks for "the next train home" like a person would, which is the
whole point (SPEC.md 3.1: everything configurable is configurable in the UI).
"""
import asyncio
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, Depends

import auth
import settings

TRANSPORT_URL = "https://transport.opendata.ch/v1"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
# Nominatim's usage policy requires an identifying User-Agent and at most one
# request a second. Both are conditions of use, not suggestions.
USER_AGENT = os.environ.get(
    "IRIS_USER_AGENT", "IRiS/0.1 (self-hosted personal assistant; one user)")
NOMINATIM_MIN_INTERVAL = 1.1

router = APIRouter(prefix="/places", tags=["places"])

settings.setting(
    "location.home", type="string", default="",
    title="Home",
    description="Your home station or address. Lets you ask for 'the next train home' "
                "instead of naming the stop every time. Left empty, IRiS will ask.")
settings.setting(
    "location.work", type="string", default="",
    title="Work",
    description="Same idea for the other end of the commute.")
settings.setting(
    "location.region", type="string", default="Switzerland",
    title="Where to search",
    description="Biases place searches, so 'a pharmacy' means a nearby one rather "
                "than one on another continent.")
settings.setting(
    "location.enabled", type="boolean", default=True,
    title="Transit and places",
    description="Public transport times and place lookup. Both are free services that "
                "need no account. Transit covers Switzerland only.")


def resolve(name: str) -> str:
    """'home' and 'work' are the two words a person actually uses."""
    key = (name or "").strip().lower()
    if key in ("home", "zuhause", "hause"):
        return settings.get("location.home") or name
    if key in ("work", "the office", "office", "arbeit", "büro"):
        return settings.get("location.work") or name
    return name


def _clock(iso: str | None) -> str:
    if not iso:
        return "?"
    try:
        return datetime.fromisoformat(iso).strftime("%H:%M")
    except ValueError:
        return iso[11:16] or "?"


def _minutes(duration: str | None) -> str:
    """transport.opendata.ch returns '00d00:19:00', which reads badly aloud."""
    if not duration or ":" not in duration:
        return "?"
    try:
        body = duration.split("d")[-1]
        hours, minutes, _ = body.split(":")
        total = int(hours) * 60 + int(minutes)
    except ValueError:
        return duration
    if total < 60:
        return f"{total} min"
    return f"{total // 60}h {total % 60:02d}"


async def journey(origin: str, destination: str, when: str | None = None) -> str:
    origin, destination = resolve(origin), resolve(destination)
    params = {"from": origin, "to": destination, "limit": 4}
    if when:
        params["time"] = when
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(f"{TRANSPORT_URL}/connections", params=params)
    if r.status_code != 200:
        return f"transit lookup failed: HTTP {r.status_code}"
    connections = r.json().get("connections") or []
    if not connections:
        return f"No connections found from {origin} to {destination}."

    lines = [f"{origin} to {destination}:"]
    for conn in connections:
        dep, arr = conn.get("from", {}), conn.get("to", {})
        changes = conn.get("transfers")
        platform = (dep.get("platform") or "").strip()
        lines.append(
            f"- {_clock(dep.get('departure'))} to {_clock(arr.get('arrival'))}"
            f", {_minutes(conn.get('duration'))}"
            + (f", platform {platform}" if platform else "")
            + (", direct" if changes == 0 else f", {changes} change"
               + ("s" if changes and changes > 1 else "")))
    return "\n".join(lines)


async def departures(station: str, limit: int = 6) -> str:
    station = resolve(station)
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(f"{TRANSPORT_URL}/stationboard",
                        params={"station": station, "limit": limit})
    if r.status_code != 200:
        return f"departure board failed: HTTP {r.status_code}"
    board = r.json().get("stationboard") or []
    if not board:
        return f"No departures listed for {station!r}. Check the stop name."
    name = (r.json().get("station") or {}).get("name") or station
    lines = [f"Departures from {name}:"]
    for item in board:
        stop = item.get("stop") or {}
        delay = stop.get("delay")
        service = f"{item.get('category', '')}{item.get('number', '')}".strip()
        lines.append(
            f"- {_clock(stop.get('departure'))} {service} to {item.get('to', '?')}"
            + (f", platform {stop['platform']}" if stop.get("platform") else "")
            + (f", {delay} min late" if delay else ""))
    return "\n".join(lines)


WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
# WMO weather codes. Open-Meteo returns a number; a person wants a word.
_SKY = {0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
        45: "foggy", 48: "freezing fog", 51: "light drizzle", 53: "drizzle",
        55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
        61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain",
        67: "freezing rain", 71: "light snow", 73: "snow", 75: "heavy snow",
        77: "snow grains", 80: "light showers", 81: "showers",
        82: "violent showers", 85: "snow showers", 86: "heavy snow showers",
        95: "thunderstorms", 96: "thunderstorms with hail",
        99: "thunderstorms with hail"}

_geocoded: dict[str, tuple[float, float]] = {}


async def _coordinates(place: str) -> tuple[float, float] | None:
    """Nominatim again, cached: a town does not move between requests."""
    key = place.strip().lower()
    if key in _geocoded:
        return _geocoded[key]
    async with _nominatim_lock:
        global _last_nominatim
        wait = NOMINATIM_MIN_INTERVAL - (time.monotonic() - _last_nominatim)
        if wait > 0:
            await asyncio.sleep(wait)
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(NOMINATIM_URL,
                            params={"q": place, "format": "json", "limit": 1},
                            headers={"User-Agent": USER_AGENT})
        _last_nominatim = time.monotonic()
    if r.status_code != 200 or not r.json():
        return None
    hit = r.json()[0]
    _geocoded[key] = (float(hit["lat"]), float(hit["lon"]))
    return _geocoded[key]


async def weather(place: str = "") -> str:
    where = place or settings.get("location.home") or settings.get("location.region")
    coords = await _coordinates(where)
    if not coords:
        return f"I could not find {where!r} on the map, so I cannot get its weather."
    lat, lon = coords
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(WEATHER_URL, params={
            "latitude": lat, "longitude": lon,
            "current": "temperature_2m,apparent_temperature,weather_code",
            "daily": "temperature_2m_max,temperature_2m_min,"
                     "precipitation_probability_max,weather_code",
            "timezone": settings.get("general.timezone"), "forecast_days": 2})
    if r.status_code != 200:
        return f"weather lookup failed: HTTP {r.status_code}"
    data = r.json()
    now, daily = data.get("current", {}), data.get("daily", {})

    def sky(code):
        return _SKY.get(int(code), "unsettled") if code is not None else "unclear"

    lines = [f"Weather in {where}:"]
    if now:
        feels = now.get("apparent_temperature")
        lines.append(
            f"- now: {round(now.get('temperature_2m', 0))}C, {sky(now.get('weather_code'))}"
            + (f", feels like {round(feels)}C" if feels is not None
               and abs(feels - now.get("temperature_2m", feels)) >= 2 else ""))
    labels = ["today", "tomorrow"]
    for i, label in enumerate(labels):
        if i >= len(daily.get("temperature_2m_max", [])):
            break
        lines.append(
            f"- {label}: {round(daily['temperature_2m_min'][i])} to "
            f"{round(daily['temperature_2m_max'][i])}C, "
            f"{sky(daily['weather_code'][i])}, "
            f"{daily['precipitation_probability_max'][i]}% chance of rain")
    return "\n".join(lines)


_last_nominatim = 0.0
_nominatim_lock = asyncio.Lock()


async def find_place(query: str, near: str = "") -> str:
    where = near or settings.get("location.home") or settings.get("location.region")
    async with _nominatim_lock:
        global _last_nominatim
        wait = NOMINATIM_MIN_INTERVAL - (time.monotonic() - _last_nominatim)
        if wait > 0:
            await asyncio.sleep(wait)          # their policy, not a guess
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(NOMINATIM_URL,
                            params={"q": f"{query} {where}".strip(), "format": "json",
                                    "limit": 5, "addressdetails": 1},
                            headers={"User-Agent": USER_AGENT})
        _last_nominatim = time.monotonic()
    if r.status_code != 200:
        return f"place lookup failed: HTTP {r.status_code}"
    results = r.json()
    if not results:
        return f"Nothing found for {query!r} near {where}."
    lines = [f"{query} near {where}:"]
    for item in results:
        parts = item.get("display_name", "").split(", ")
        lines.append(f"- {parts[0]}" + (f", {', '.join(parts[1:4])}" if len(parts) > 1
                                        else ""))
    return "\n".join(lines)


@router.get("/departures")
async def departures_endpoint(station: str, _: dict = Depends(auth.active_user)):
    return {"text": await departures(station)}


@router.get("/journey")
async def journey_endpoint(origin: str, destination: str,
                           _: dict = Depends(auth.active_user)):
    return {"text": await journey(origin, destination)}
