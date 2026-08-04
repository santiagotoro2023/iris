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
import re
from math import asin, cos, radians, sin, sqrt
from urllib.parse import quote_plus
from datetime import datetime, timedelta
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
    "location.stop", type="string", default="", order=3,
    title="Preferred stop",
    description="The stop you actually use. Left empty, IRiS takes the closest one, "
                "which is not always the one you walk to.")
settings.setting(
    "location.work", type="string", default="",
    title="Work", order=2,
    description="The other end of the commute.")
settings.setting(
    "location.auto", type="boolean", default=True,
    title="Follow my location", order=4,
    description="Ask the browser where you are when the page opens and before each "
                "message, so transit and weather use where you actually are.")
settings.setting(
    "location.latitude", type="number", minimum=-90, maximum=90, default=0.0,
    title="Latitude", order=85,
    description="Filled in by 'use my location' above.")
settings.setting(
    "location.longitude", type="number", minimum=-180, maximum=180, default=0.0,
    title="Longitude", order=86)
settings.setting(
    "location.accuracy", type="number", minimum=0, maximum=100000, default=0.0,
    title="Location accuracy (metres)", order=87,
    description="How good the last fix was. A machine without GPS positions itself "
                "by wifi and can be a kilometre out.")
settings.setting(
    "location.fixed_at", type="number", minimum=0, maximum=1e12, default=0.0,
    title="Location taken at", order=88,
    description="When the last fix arrived, as a timestamp.")
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


async def stop_near(lat: float, lon: float) -> tuple[str, int] | None:
    """The closest real stop to any coordinate, with how far it is.

    Used for the far end of a journey: planning to "Mönchaltorf" lands in the town
    centre, when the address is 61 m from Mönchaltorf, Wihalde.
    """
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(f"{TRANSPORT_URL}/locations", params={"x": lon, "y": lat})
    if r.status_code != 200:
        return None
    stops = [s for s in (r.json().get("stations") or [])
             if s.get("id") and s.get("name")]
    stops.sort(key=lambda s: s.get("distance") if s.get("distance") is not None
               else float("inf"))
    if not stops:
        return None
    best = stops[0]
    return best["name"], int(best.get("distance") or 0)


async def nearest_stop() -> str:
    """The stop he actually uses, if he named one; otherwise the closest."""
    preferred = settings.get("location.stop")
    if preferred:
        return preferred
    return await closest_stop()


async def closest_stop() -> str:
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


# The API says "B", "S", "IC". A person says bus, S-Bahn, InterCity, and being told
# "take the direct train" for a bus is simply wrong.
_MODES = {"B": "bus", "BUS": "bus", "NFB": "bus", "TRO": "trolleybus",
          "T": "tram", "NFT": "tram", "M": "metro",
          "S": "S-Bahn", "SN": "night S-Bahn", "R": "regional train",
          "RE": "regional express", "IR": "InterRegio", "IC": "InterCity",
          "ICE": "ICE", "EC": "EuroCity", "TGV": "TGV", "PB": "cable car",
          "FUN": "funicular", "GB": "gondola", "BAT": "boat"}


def _legs(conn: dict) -> str:
    """What you actually board, in order."""
    out = []
    for section in conn.get("sections") or []:
        journey = section.get("journey")
        if not journey:
            continue
        category = (journey.get("category") or "").upper()
        mode = _MODES.get(category, category.lower() or "service")
        number = (journey.get("number") or "").strip()
        out.append(f"{mode} {number}".strip())
    return ", then ".join(out)


_TIME = re.compile(r"\b([01]?\d|2[0-3])[:.h]([0-5]\d)\b")
_ISO_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
             "sunday")


def parse_when(text: str) -> tuple[str, str]:
    """"tomorrow 08:30" -> ("2026-08-06", "08:30").

    One argument rather than three, because a model given three optional arguments
    fills all of them. Returns empty strings for whatever was not said.
    """
    if not text:
        return "", ""
    lowered = text.strip().lower()
    today = datetime.now(ZoneInfo(settings.get("general.timezone"))).date()
    date = ""
    iso = _ISO_DATE.search(lowered)
    if iso:
        date = iso.group(1)
    elif "day after tomorrow" in lowered:
        date = str(today + timedelta(days=2))
    elif "tomorrow" in lowered or "morgen" in lowered:
        date = str(today + timedelta(days=1))
    elif "today" in lowered or "heute" in lowered or "tonight" in lowered:
        date = str(today)
    else:
        for i, name in enumerate(_WEEKDAYS):
            if name in lowered:
                ahead = (i - today.weekday()) % 7 or 7
                date = str(today + timedelta(days=ahead))
                break
    clock = _TIME.search(lowered)
    return date, (f"{int(clock.group(1)):02d}:{clock.group(2)}" if clock else "")


def maps_link(origin: str, destination: str) -> str:
    """A link he can open and check, which is the point of showing the sources."""
    return ("https://www.google.com/maps/dir/?api=1&travelmode=transit"
            f"&origin={quote_plus(origin)}&destination={quote_plus(destination)}")


async def journey(origin: str, destination: str, when: str | None = None,
                  arrive_by: bool = False) -> str:
    origin, destination = await resolve(origin), await resolve(destination)
    if not origin:
        return ("I do not know where you are. Set Home in Settings, or allow location "
                "in the browser.")
    date, clock = parse_when(when or "")
    # Two is what somebody asked "when is the next bus" actually wants. A named time
    # means they are planning, so show more.
    limit = 4 if (date or clock) else 2
    params = {"from": origin, "to": destination, "limit": limit}
    if clock:
        params["time"] = clock
    if date:
        params["date"] = date
    if arrive_by and (clock or date):
        # "I want to be there at 08:30" is an arrival, and asking the timetable for
        # departures at 08:30 answers a different question.
        params["isArrivalTime"] = 1
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(f"{TRANSPORT_URL}/connections", params=params)
    if r.status_code != 200:
        return f"transit lookup failed: HTTP {r.status_code}"
    connections = r.json().get("connections") or []
    if not connections:
        return f"No connections found from {origin} to {destination}."

    # What the timetable actually resolved the names to, not what was typed. It
    # matched "SIDMAR AG" to "Mönchaltorf, Esslingerstr. 32" correctly, but labelled
    # the route with the input, so a right answer looked like a wrong one and a
    # genuinely wrong match would have been invisible.
    first = connections[0]
    origin = (first.get("from", {}).get("station", {}).get("name") or origin)
    destination = (first.get("to", {}).get("station", {}).get("name") or destination)

    heading = f"{origin} to {destination}"
    if date or clock:
        heading += (f", to arrive by {clock or 'then'}" if arrive_by
                    else f", leaving after {clock or 'then'}")
        heading += f" on {date}" if date else ""
    lines = [heading + ":"]
    for conn in connections:
        dep, arr = conn.get("from", {}), conn.get("to", {})
        changes = conn.get("transfers")
        platform = (dep.get("platform") or "").strip()
        # Departure AND arrival, spelled out: "20:09 to 20:28" was being relayed as a
        # departure with no arrival at all.
        legs = _legs(conn)
        lines.append(
            f"- {legs or 'service'}: departs {origin} at "
            f"{_clock(dep.get('departure'))}, arrives {destination} at "
            f"{_clock(arr.get('arrival'))}, {_minutes(conn.get('duration'))}"
            + (f", platform {platform}" if platform else "")
            + (", direct" if changes == 0 else f", {changes} change"
               + ("s" if changes and changes > 1 else "")))
    lines.append(f"- Full route: {maps_link(origin, destination)}")
    return "\n".join(lines)


async def departures(station: str = "", limit: int = 4) -> str:
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
    for item in board[:limit]:
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


def fix_age_seconds() -> float | None:
    taken = settings.get("location.fixed_at")
    return (time.time() - taken) if taken else None


def _fix_note() -> str:
    """Said out loud when the position is old or coarse, so a wrong stop is
    explained rather than mysterious."""
    age = fix_age_seconds()
    accuracy = settings.get("location.accuracy")
    bits = []
    if age is not None and age > 600:
        bits.append(f"the fix is {int(age // 60)} minutes old")
    elif age is None:
        bits.append("the fix has no timestamp, so it may be old")
    if accuracy and accuracy > 500:
        bits.append(f"accurate only to about {int(accuracy)} m")
    return ("Position: " + ", and ".join(bits) + ".") if bits else ""


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


def _haversine(a: tuple[float, float], b: tuple[float, float]) -> int:
    """Metres between two coordinates, for "how far is the walk"."""
    lat1, lon1, lat2, lon2 = map(radians, (a[0], a[1], b[0], b[1]))
    h = (sin((lat2 - lat1) / 2) ** 2
         + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2) ** 2)
    return int(2 * 6371000 * asin(sqrt(h)))


def _walk(metres: int) -> str:
    minutes = max(1, round(metres / 80))       # ~4.8 km/h
    return f"{metres} m, about {minutes} min on foot"


async def route_to(place: str, when: str = "", arrive_by: bool = False) -> str:
    """How to actually get somewhere: find it, then plan public transport to it and
    say how far the walk is. Public transport is the default because that is what
    "how do I get there" means to somebody who asked it here.
    """
    hit = await _lookup(place)
    if not hit:
        # The map does not know every small company, but the timetable knows towns.
        # Planning to the place as named beats refusing outright.
        origin = await nearest_stop() or settings.get("location.home")
        if origin:
            # No hedging: the timetable resolved the name, and a sentence of doubt
            # made the model discard real connections and invent its own route.
            return await journey(origin, place, when, arrive_by)
        return f"I could not find {place!r} on the map."
    label = hit["display_name"].split(",")[0]
    town = (hit.get("address", {}).get("town")
            or hit.get("address", {}).get("village")
            or hit.get("address", {}).get("city") or "")
    target = (float(hit["lat"]), float(hit["lon"]))

    lines = [f"{label}" + (f", {town}" if town else "") + ".",
             f"Address: {hit['display_name']}"]

    here = _fixed_coordinates()
    if here:
        lines.append(f"Straight-line distance from you: {_haversine(here, target)} m.")

    # Plan to the stop by the address, not to the town. The last few hundred metres
    # are the difference between arriving at the door and arriving in the village.
    arrival = await stop_near(*target)
    destination = f"{label}, {town}" if town else label
    if arrival:
        destination = arrival[0]
        lines.append(f"Nearest stop to it: {arrival[0]}, {_walk(arrival[1])} "
                     f"from the door.")

    origin = await nearest_stop() or settings.get("location.home")
    if origin:
        if not settings.get("location.stop"):
            note = _fix_note()
            lines.append(f"Starting from {origin}, the closest stop to your position."
                         + (f" {note}" if note else "")
                         + " Set a preferred stop in Settings if that is not the one "
                           "you use.")
        lines.append("")
        lines.append(await journey(origin, destination, when, arrive_by))
    lines.append(f"- On the map: https://www.google.com/maps/search/"
                 f"?api=1&query={quote_plus(hit['display_name'])}")
    return "\n".join(lines)


# Roughly 65 km around the fix. Wide enough for a day out, narrow enough that a
# company name cannot resolve to another continent.
SEARCH_BOX_DEGREES = 0.6


def _viewbox() -> str | None:
    coords = _fixed_coordinates()
    if not coords:
        return None
    lat, lon = coords
    d = SEARCH_BOX_DEGREES
    return f"{lon - d},{lat - d},{lon + d},{lat + d}"


async def _search_once(query: str, near: bool = True) -> dict | None:
    """`near` restricts the search to a box around the user.

    Without it "SIDMAR AG" resolved to Sidmar, Frederick County, Maryland, and the
    route was planned to the United States.
    """
    params = {"q": query, "format": "json", "limit": 1, "addressdetails": 1}
    box = _viewbox() if near else None
    if box:
        params["viewbox"] = box
        params["bounded"] = 1
    async with _nominatim_lock:
        global _last_nominatim
        wait = NOMINATIM_MIN_INTERVAL - (time.monotonic() - _last_nominatim)
        if wait > 0:
            await asyncio.sleep(wait)
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(NOMINATIM_URL, params=params,
                            headers={"User-Agent": USER_AGENT})
        _last_nominatim = time.monotonic()
    results = r.json() if r.status_code == 200 else []
    return results[0] if results else None


async def _lookup(query: str) -> dict | None:
    """As asked, then simplified, then narrowed to the region.

    The map does not know "SIDMAR AG, Esslingerstrasse 32, Mönchaltorf" but knows
    "Esslingerstrasse 32, Mönchaltorf" perfectly well: it holds addresses, not
    company names. And appending the user's home town to a full address made it
    match nothing at all, so the region is only added last.
    """
    parts = [p.strip() for p in query.split(",") if p.strip()]
    simpler = ", ".join(parts[1:]) if len(parts) > 1 else ""
    where = settings.get("location.home") or settings.get("location.region")

    # Near first, always. Somewhere far away is the last thing meant by "how do I
    # get there", so a global match is only accepted when nothing local exists.
    attempts = [(query, True)]
    if simpler:
        attempts.append((simpler, True))
    if where:
        attempts.append((f"{query} {where}".strip(), False))
        if simpler:
            attempts.append((f"{simpler} {where}".strip(), False))
    # No unbounded fallback on the raw query: that is what resolved "SIDMAR AG" to
    # Sidmar, Maryland. Nothing local means nothing local, and saying so beats
    # routing to another continent.
    for attempt, near in attempts:
        hit = await _search_once(attempt, near)
        if hit:
            return hit
    return None


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
        full = item.get("display_name", "")
        parts = full.split(", ")
        where_bits = f", {', '.join(parts[1:4])}" if len(parts) > 1 else ""
        # The link goes on the result, so the banner is worth opening on its own.
        lines.append(f"- {parts[0]}{where_bits} "
                     f"https://www.google.com/maps/search/?api=1&"
                     f"query={quote_plus(full)}")
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


async def whereabouts() -> str:
    coords = _fixed_coordinates()
    if not coords:
        home = settings.get("location.home")
        return (f"No location fix. Home is set to {home!r}." if home else
                "No location fix, and Home is not set. Press 'use my location' in "
                "Settings, or name a place.")
    lat, lon = coords
    name = await _place_name(lat, lon)
    stop = await nearest_stop()
    accuracy = settings.get("location.accuracy")
    lines = [f"Location: {name or 'unknown'} ({lat}, {lon})"
             + (f", accurate to about {int(accuracy)} m." if accuracy else ".")]
    stale = _fix_note()
    if stale:
        lines.append(stale)
    if stop:
        preferred = settings.get("location.stop")
        lines.append(f"Departure stop: {stop}."
                     + ("" if preferred else " (the closest to that fix)"))
    home, work = settings.get("location.home"), settings.get("location.work")
    if home:
        lines.append(f"Home is {home}." + (f" Work is {work}." if work else ""))
    return "\n".join(lines)


@router.get("/departures")
async def departures_endpoint(station: str, _: dict = Depends(auth.active_user)):
    return {"text": await departures(station)}


@router.get("/journey")
async def journey_endpoint(origin: str, destination: str,
                           _: dict = Depends(auth.active_user)):
    return {"text": await journey(origin, destination)}
