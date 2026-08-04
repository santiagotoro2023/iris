"""Integration types (SPEC.md 33).

Same machinery as devices: a type declares its fields, the UI renders the form, and
the credentials never come back out. What differs is that an integration usually
gives IRiS a *tool* rather than something to look at.

**Mailbox** is real and needs no dependency: IMAP is in the standard library, and
every provider Santiago is likely to use speaks it. Microsoft Graph and Gmail's own
APIs (SPEC.md 5) buy push notification and richer search, and cost an OAuth app
registration each; IMAP works today with an app password.

**Calendar** is CalDAV, which is one REPORT request and a little XML; every client
library for it drags in more dependency than the feature is worth. **Push** is ntfy,
which puts IRiS on a phone with no account and no app store.

**Webhook** is the outbound half of Phase 6's integration layer: somewhere to POST
when something happens, which Phase 7's proactive engine will want.
"""
import asyncio
import email
import email.header
import email.utils
import imaplib
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
from fastapi import HTTPException

import registry
import settings

MAX_MESSAGES = 15


def _decode(raw: str | None) -> str:
    """Mail headers arrive as =?UTF-8?B?...?= more often than not."""
    if not raw:
        return ""
    out = []
    for text, charset in email.header.decode_header(raw):
        if isinstance(text, bytes):
            try:
                out.append(text.decode(charset or "utf-8", "replace"))
            except LookupError:
                out.append(text.decode("utf-8", "replace"))
        else:
            out.append(text)
    return " ".join(" ".join(out).split())


def _fetch_headers(config: dict, limit: int, unseen_only: bool) -> list[dict]:
    """Blocking IMAP, called in a thread. Headers only: the body of every message in
    a mailbox is a lot of text to put in front of a model that asked "any new mail".
    """
    host = config.get("host", "")
    port = int(config.get("port") or 993)
    cls = imaplib.IMAP4_SSL if config.get("ssl", True) else imaplib.IMAP4
    box = cls(host, port)
    try:
        box.login(config.get("username", ""), config.get("password", ""))
        box.select(config.get("folder") or "INBOX", readonly=True)
        status, data = box.search(None, "UNSEEN" if unseen_only else "ALL")
        if status != "OK":
            return []
        ids = data[0].split()[-limit:]
        messages = []
        for msg_id in reversed(ids):
            status, raw = box.fetch(
                msg_id, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
            if status != "OK" or not raw or not raw[0]:
                continue
            parsed = email.message_from_bytes(raw[0][1])
            messages.append({
                "from": _decode(parsed.get("From")),
                "subject": _decode(parsed.get("Subject")) or "(no subject)",
                "date": _decode(parsed.get("Date")),
            })
        return messages
    finally:
        try:
            box.logout()
        except Exception:
            pass


async def _check_mail(thing: dict, user: dict | None = None) -> dict:
    config = thing["config"]
    limit = min(int(config.get("limit") or 10), MAX_MESSAGES)
    try:
        messages = await asyncio.to_thread(
            _fetch_headers, config, limit, bool(config.get("unseen_only", True)))
    except imaplib.IMAP4.error as e:
        raise HTTPException(502, f"mailbox refused the login or the folder: {e}")
    except Exception as e:
        raise HTTPException(502, f"could not reach the mailbox: {type(e).__name__}")
    return {"mailbox": thing["name"], "count": len(messages), "messages": messages}


async def _test_webhook(thing: dict, user: dict | None = None) -> dict:
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(thing["config"]["url"],
                         json={"source": "IRiS", "event": "test",
                               "text": "Webhook configured correctly."})
    return {"status": r.status_code, "ok": r.status_code < 300}


registry.register(registry.Type(
    kind="integration", name="mailbox", label="Mailbox",
    description="Tells you what has arrived. Works with Outlook, Gmail, iCloud or "
                "your own server.",
    preset_field="provider",
    presets={
        "Outlook / Microsoft 365": {"host": "outlook.office365.com", "port": 993,
                                    "ssl": True},
        "Gmail": {"host": "imap.gmail.com", "port": 993, "ssl": True},
        "iCloud": {"host": "imap.mail.me.com", "port": 993, "ssl": True},
        "Fastmail": {"host": "imap.fastmail.com", "port": 993, "ssl": True},
        "Yahoo": {"host": "imap.mail.yahoo.com", "port": 993, "ssl": True},
        "Other": {},
    },
    fields=[
        registry.Field("provider", "Provider", type="choice", required=True,
                       default="Outlook / Microsoft 365",
                       choices=["Outlook / Microsoft 365", "Gmail", "iCloud",
                                "Fastmail", "Yahoo", "Other"],
                       help="Fills in the server settings for you."),
        registry.Field("username", "Email address", required=True),
        registry.Field("password", "App password", type="password", required=True,
                       secret=True,
                       help="Create one in your account's security settings."),
        registry.Field("host", "IMAP server", required=True),
        registry.Field("port", "Port", type="number", default=993),
        registry.Field("ssl", "Use SSL", type="boolean", default=True),
        registry.Field("folder", "Folder", default="INBOX"),
        registry.Field("unseen_only", "Unread only", type="boolean", default=True),
        registry.Field("limit", "Messages to fetch", type="number", default=10),
    ],
    actions={"check": _check_mail},
    action_labels={"check": "check mail"},
))

registry.register(registry.Type(
    kind="integration", name="webhook", label="Webhook",
    description="Sends what IRiS reports to any URL that accepts an HTTP callback.",
    fields=[
        registry.Field("url", "URL", type="password", required=True, secret=True,
                       help="A webhook URL usually is the credential, so it is "
                            "treated as one."),
    ],
    actions={"test": _test_webhook},
    action_labels={"test": "send a test"},
))


# --------------------------------------------------------------- calendar ----

# CalDAV is WebDAV with a calendar-flavoured REPORT. No library: this is one request
# and a small amount of XML, and every CalDAV client library drags in a dependency
# tree larger than the feature.
_CALDAV_REPORT = """<?xml version="1.0" encoding="utf-8"?>
<C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
  <D:prop><C:calendar-data/></D:prop>
  <C:filter>
    <C:comp-filter name="VCALENDAR">
      <C:comp-filter name="VEVENT">
        <C:time-range start="{start}" end="{end}"/>
      </C:comp-filter>
    </C:comp-filter>
  </C:filter>
</C:calendar-query>"""

_ICS_UNFOLD = re.compile(r"\r?\n[ \t]")


def _parse_events(ics: str) -> list[dict]:
    """Pull SUMMARY and DTSTART out of the VEVENTs in a calendar-data blob.

    iCalendar folds long lines by starting the continuation with a space, so
    unfolding has to happen before anything is parsed or every long summary is
    truncated at 75 characters.
    """
    events = []
    for block in _ICS_UNFOLD.sub("", ics).split("BEGIN:VEVENT")[1:]:
        block = block.split("END:VEVENT")[0]
        event = {}
        for line in block.splitlines():
            name, _, value = line.partition(":")
            key = name.split(";")[0].upper()
            if key == "SUMMARY":
                event["summary"] = value.strip()
            elif key == "DTSTART":
                event["start"] = value.strip()
                event["all_day"] = "VALUE=DATE" in name.upper()
            elif key == "LOCATION":
                event["location"] = value.strip()
        if event.get("summary"):
            events.append(event)
    return events


def _when(event: dict) -> str:
    raw = event.get("start", "")
    if event.get("all_day"):
        return "all day"
    try:
        stamp = datetime.strptime(raw.rstrip("Z")[:15], "%Y%m%dT%H%M%S")
        if raw.endswith("Z"):
            stamp = stamp.replace(tzinfo=timezone.utc).astimezone(
                ZoneInfo(settings.get("general.timezone")))
        return stamp.strftime("%H:%M")
    except ValueError:
        return raw or "?"


async def _fetch_events(config: dict, days: int) -> list[dict]:
    now = datetime.now(timezone.utc)
    body = _CALDAV_REPORT.format(
        start=now.strftime("%Y%m%dT%H%M%SZ"),
        end=(now + timedelta(days=days)).strftime("%Y%m%dT%H%M%SZ"))
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
        r = await c.request(
            "REPORT", config["url"],
            auth=(config.get("username", ""), config.get("password", "")),
            headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
            content=body.encode())
    if r.status_code == 401:
        raise HTTPException(502, "the calendar rejected those credentials")
    if r.status_code >= 300:
        raise HTTPException(502, f"calendar returned HTTP {r.status_code}")
    events = []
    for match in re.finditer(r"BEGIN:VCALENDAR.*?END:VCALENDAR", r.text, re.S):
        events.extend(_parse_events(match.group(0)))
    events.sort(key=lambda e: e.get("start", ""))
    return events


async def _check_calendar(thing: dict, user: dict | None = None) -> dict:
    days = int(thing["config"].get("days") or 1)
    events = await _fetch_events(thing["config"], days)
    return {"calendar": thing["name"], "count": len(events),
            "messages": [{"from": _when(e),
                          "subject": e["summary"]
                          + (f" ({e['location']})" if e.get("location") else "")}
                         for e in events]}


registry.register(registry.Type(
    kind="integration", name="calendar", label="Calendar",
    description="Tells you what is on. Works with Nextcloud, Fastmail, iCloud or your "
                "own server.",
    preset_field="provider",
    presets={
        "Nextcloud": {"url": "https://YOUR-SERVER/remote.php/dav/calendars/"
                             "USERNAME/personal/"},
        "iCloud": {"url": "https://caldav.icloud.com/"},
        "Fastmail": {"url": "https://caldav.fastmail.com/dav/calendars/user/"
                            "USERNAME/Default/"},
        "Other": {},
    },
    fields=[
        registry.Field("provider", "Provider", type="choice", required=True,
                       default="Nextcloud",
                       choices=["Nextcloud", "iCloud", "Fastmail", "Other"],
                       help="Fills in the URL shape; replace the capitals with yours."),
        registry.Field("url", "Calendar URL", required=True,
                       help="Ends in a slash."),
        registry.Field("username", "Username", required=True),
        registry.Field("password", "App password", type="password", required=True,
                       secret=True,
                       help="Create one in your account's security settings."),
        registry.Field("days", "Days ahead", type="number", default=1),
    ],
    actions={"check": _check_calendar},
    action_labels={"check": "what's on"},
))


# ------------------------------------------------------------------ push ----

async def _send_push(thing: dict, user: dict | None = None,
                     text: str = "IRiS is connected.") -> dict:
    config = thing["config"]
    server = (config.get("server") or "https://ntfy.sh").rstrip("/")
    headers = {"Title": "IRiS", "Priority": str(int(config.get("priority") or 3))}
    if config.get("token"):
        headers["Authorization"] = f"Bearer {config['token']}"
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(f"{server}/{config['topic']}", content=text.encode(),
                         headers=headers)
    return {"status": r.status_code, "ok": r.status_code < 300}


registry.register(registry.Type(
    kind="integration", name="push", label="Push to phone",
    description="Notifications on your phone. Install the ntfy app, subscribe to a "
                "topic, put the same topic here.",
    fields=[
        registry.Field("topic", "Topic", type="password", required=True, secret=True,
                       help="Pick something long and unguessable, like a password."),
        registry.Field("server", "Server", default="https://ntfy.sh",
                       help="Change if you self-host ntfy."),
        registry.Field("priority", "Priority", type="number", default=3,
                       help="1 quiet, 3 default, 5 urgent."),
    ],
    actions={"test": _send_push},
    action_labels={"test": "send a test"},
))


# ------------------------------------------------------------------ tool ----

async def check_all_mail() -> str:
    boxes = await registry.enabled_of_type("integration", "mailbox")
    if not boxes:
        return ("No mailbox is set up. Add one under Integrations, with the IMAP "
                "server and an app password.")
    lines = []
    for box in boxes:
        try:
            result = await _check_mail(box)
        except HTTPException as e:
            lines.append(f"{box['name']}: unreachable ({e.detail})")
            continue
        if not result["messages"]:
            lines.append(f"{box['name']}: nothing new.")
            continue
        lines.append(f"{box['name']}, {result['count']} message"
                     f"{'' if result['count'] == 1 else 's'}:")
        lines.extend(f"- {m['from']}: {m['subject']}" for m in result["messages"])
    return "\n".join(lines)


async def check_all_calendars() -> str:
    cals = await registry.enabled_of_type("integration", "calendar")
    if not cals:
        return ("No calendar is set up. Add one under Integrations with its CalDAV "
                "URL and an app password.")
    lines = []
    for cal in cals:
        try:
            result = await _check_calendar(cal)
        except HTTPException as e:
            lines.append(f"{cal['name']}: unreachable ({e.detail})")
            continue
        if not result["messages"]:
            lines.append(f"{cal['name']}: nothing scheduled.")
            continue
        lines.append(f"{cal['name']}:")
        lines.extend(f"- {m['from']} {m['subject']}" for m in result["messages"])
    return "\n".join(lines)


async def notify(text: str, event: str = "notice") -> int:
    """Fan out to every outbound channel. Phase 7 uses this for the briefing."""
    sent = 0
    for hook in await registry.enabled_of_type("integration", "webhook"):
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                await c.post(hook["config"]["url"],
                             json={"source": "IRiS", "event": event, "text": text})
            sent += 1
        except Exception as e:
            print(f"[integrations] webhook {hook['name']} failed: {e}", flush=True)
    for push in await registry.enabled_of_type("integration", "push"):
        try:
            await _send_push(push, text=text)
            sent += 1
        except Exception as e:
            print(f"[integrations] push {push['name']} failed: {e}", flush=True)
    return sent
