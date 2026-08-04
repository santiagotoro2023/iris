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
from pydantic import BaseModel, Field as PField

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
    title="Home", order=1,
    description="Your home stop or address, so 'the next train home' works.")
settings.setting(
    "location.work", type="string", default="",
    title="Work", order=2,
    description="The other end of the commute.")
settings.setting(
    "location.latitude", type="number", minimum=-90, maximum=90, default=0.0,
    title="Latitude", order=85,
    description="Filled in by 'use my location' above.")
settings.setting(
    "location.longitude", type="number", minimum=-180, maximum=180, default=0.0,
    title="Longitude", order=86)
settings.setting(
    "location.region", type="string", default="Switzerland",
    title="Where to search", order=87,
    description="Used only when nothing more precise is known.")
settings.setting(
    "location.enabled", type="boolean", default=True,
    title="Transit and places", order=3,
    description="Timetables, maps and weather. Timetables cover Switzerland.")


_HERE = {"here", "current location", "my location", "where i am", "nearby",
         "hier", "my position"}


async def nearest_stop() -> str:
    """The closest stop to wherever the browser last said we were.

    "when is the next bus to Uster" has an origin, it just is not spoken, and
    defaulting it to Home is wrong precisely when it matters: away from home.
    """
    coords = _fixed_coordinates()
    if not coords:
        return ""
    lat, lon = coords
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(f"{TRANSPORT_URL}/locations", params={"x": lon, "y": lat})
    if r.status_code != 200:
        return ""
    # Sorted by distance, but the list mixes addresses in with real stops and an
    # address cannot be departed from. Take the closest thing with an id.
    stops = [s for s in (r.json().get("stations") or [])
             if s.get("id") and s.get("name")]
    stops.sort(key=lambda s: s.get("distance") if s.get("distance") is not None
               else float("inf"))
    return stops[0]["name"] if stops else ""


async def resolve(name: str) -> str:
    """The words a person actually uses, mapped to something the timetable knows."""
    key = (name or "").strip().lower()
    if not key or key in _HERE:
        return await nearest_stop() or settings.get("location.home") or name
    if key in ("home", "zuhause", "hause"):
        return settings.get("location.home") or await nearest_stop() or name
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
    origin, destination = await resolve(origin), await resolve(destination)
    if not origin:
        return ("I do not know where you are. Set Home in Settings, or allow location "
                "in the browser.")
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


async def departures(station: str = "", limit: int = 6) -> str:
    station = await resolve(station)
    if not station:
        return ("I do not know where you are. Name a stop, set Home in Settings, or "
                "allow location in the browser.")
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


NOMINATIM_REVERSE = "https://nominatim.openstreetmap.org/reverse"


async def reverse(lat: float, lon: float) -> str:
    """Coordinates to a name a person recognises, for the briefing to say."""
    async with _nominatim_lock:
        global _last_nominatim
        wait = NOMINATIM_MIN_INTERVAL - (time.monotonic() - _last_nominatim)
        if wait > 0:
            await asyncio.sleep(wait)
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(NOMINATIM_REVERSE,
                            params={"lat": lat, "lon": lon, "format": "json",
                                    "zoom": 14},
                            headers={"User-Agent": USER_AGENT})
        _last_nominatim = time.monotonic()
    if r.status_code != 200:
        return ""
    address = r.json().get("address", {})
    for key in ("city", "town", "village", "suburb", "municipality", "county"):
        if address.get(key):
            return address[key]
    return (r.json().get("display_name") or "").split(",")[0]


_reverse_cache: dict[tuple[float, float], str] = {}


async def _place_name(lat: float, lon: float) -> str:
    key = (round(lat, 3), round(lon, 3))
    if key not in _reverse_cache:
        _reverse_cache[key] = await reverse(lat, lon)
    return _reverse_cache[key]


def _fixed_coordinates() -> tuple[float, float] | None:
    """Only trust a stored fix if it is actually set; 0,0 is in the Atlantic."""
    lat = settings.get("location.latitude")
    lon = settings.get("location.longitude")
    return (lat, lon) if (lat or lon) else None


async def weather(place: str = "") -> str:
    if place:
        where = place
        coords = await _coordinates(place)
    else:
        # A precise fix from the browser beats geocoding a town name, and beats
        # falling back to the country, which puts the forecast in a random field.
        coords = _fixed_coordinates()
        where = settings.get("location.home") or settings.get("location.region")
        if coords and not settings.get("location.home"):
            # The coordinates were right but the label said "Switzerland", which
            # reads as though the forecast is for the whole country.
            where = await _place_name(*coords) or where
        if not coords:
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


class Position(BaseModel):
    latitude: float = PField(ge=-90, le=90)
    longitude: float = PField(ge=-180, le=180)


@router.post("/location")
async def set_location(body: Position, user: dict = Depends(auth.active_user)):
    """The browser knows where it is; the server does not. Stored as settings so it
    is visible and editable like everything else (SPEC.md 3.1)."""
    name = await reverse(body.latitude, body.longitude)
    # Six decimals is ~0.1 m. Four was ~11 m, which is enough to land on the wrong
    # side of a village and pick the wrong bus stop.
    patch = {"location.latitude": round(body.latitude, 6),
             "location.longitude": round(body.longitude, 6)}
    # Only fill Home if it is empty: a name he typed himself outranks a guess.
    if name and not settings.get("location.home"):
        patch["location.home"] = name
    await settings.apply(patch, actor=user["username"])
    return {"place": name or "unknown", **patch}


@router.get("/departures")
async def departures_endpoint(station: str, _: dict = Depends(auth.active_user)):
    return {"text": await departures(station)}


@router.get("/journey")
async def journey_endpoint(origin: str, destination: str,
                           _: dict = Depends(auth.active_user)):
    return {"text": await journey(origin, destination)}
