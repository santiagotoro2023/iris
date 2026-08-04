"""Integration types (SPEC.md 33).

Same machinery as devices: a type declares its fields, the UI renders the form, and
the credentials never come back out. What differs is that an integration usually
gives IRiS a *tool* rather than something to look at.

**Mailbox** is real and needs no dependency: IMAP is in the standard library, and
every provider Santiago is likely to use speaks it. Microsoft Graph and Gmail's own
APIs (SPEC.md 5) buy push notification and richer search, and cost an OAuth app
registration each; IMAP works today with an app password.

**Webhook** is the outbound half of Phase 6's integration layer: somewhere to POST
when something happens, which Phase 7's proactive engine will want.
"""
import asyncio
import email
import email.header
import email.utils
import imaplib
import json

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
    kind="integration", name="mailbox", label="Mailbox (IMAP)",
    description="Any IMAP mailbox: Outlook, Gmail with an app password, or your own "
                "server. IRiS can tell you what has arrived.",
    fields=[
        registry.Field("host", "IMAP server", required=True,
                       help="outlook.office365.com, imap.gmail.com, mail.example.com"),
        registry.Field("port", "Port", type="number", default=993),
        registry.Field("ssl", "Use SSL", type="boolean", default=True),
        registry.Field("username", "Username", required=True),
        registry.Field("password", "Password", type="password", required=True,
                       secret=True,
                       help="Use an app password. Gmail and Outlook both refuse a "
                            "normal one over IMAP when 2FA is on."),
        registry.Field("folder", "Folder", default="INBOX"),
        registry.Field("unseen_only", "Unread only", type="boolean", default=True),
        registry.Field("limit", "Messages to fetch", type="number", default=10),
    ],
    actions={"check": _check_mail},
    action_labels={"check": "check mail"},
))

registry.register(registry.Type(
    kind="integration", name="webhook", label="Webhook",
    description="Somewhere for IRiS to POST when something happens. Useful for "
                "wiring it into anything that accepts an HTTP callback.",
    fields=[
        registry.Field("url", "URL", type="password", required=True, secret=True,
                       help="A webhook URL usually is the credential, so it is "
                            "treated as one."),
    ],
    actions={"test": _test_webhook},
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


async def notify(text: str, event: str = "notice") -> int:
    """Fan out to every configured webhook. Phase 7 will want this."""
    hooks = await registry.enabled_of_type("integration", "webhook")
    sent = 0
    for hook in hooks:
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                await c.post(hook["config"]["url"],
                             json={"source": "IRiS", "event": event, "text": text})
            sent += 1
        except Exception as e:
            print(f"[integrations] webhook {hook['name']} failed: {e}", flush=True)
    return sent
