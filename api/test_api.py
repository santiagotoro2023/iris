"""Check the tool-calling loop without needing Ollama or a GPU.

Run: python -m pytest test_api.py   (or just: python test_api.py)
"""
import json
import re
from unittest.mock import patch

from fastapi import Depends
from fastapi.testclient import TestClient

import auth
import main
import persona
import places
import proactive
import reasoning
import registry
import settings
import backup
import cameras
import memory
import voice

# The API now requires a session. These tests exercise inference and settings logic,
# not the auth gate — that has its own tests at the bottom of this file.
main.app.dependency_overrides[auth.active_user] = lambda: {
    "id": 1, "username": "test", "role": "creator", "must_change": False}


class FakeStream:
    """Stands in for httpx's streaming response: Ollama sends NDJSON chunks."""
    status_code = 200

    def __init__(self, chunks):
        self._chunks = chunks

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aiter_lines(self):
        for chunk in self._chunks:
            yield json.dumps(chunk)

    async def aread(self):
        return b""


def text_round(text):
    """One streamed assistant reply, split across two chunks."""
    half = len(text) // 2
    return [{"message": {"role": "assistant", "content": text[:half]}, "done": False},
            {"message": {"role": "assistant", "content": text[half:]}, "done": True}]


def tool_round(name):
    return [{"message": {"role": "assistant",
                         "tool_calls": [{"function": {"name": name, "arguments": {}}}]},
             "done": True}]


def fake_ollama(*rounds):
    """Patch for AsyncClient.stream: one scripted round per call, recording bodies."""
    it = iter(rounds)
    sent = []

    def stream(self, method, url, json=None, **kw):   # not async: returns a ctx manager
        sent.append(json)
        return FakeStream(next(it))

    stream.sent = sent
    return stream


def test_tool_call_executes_and_loop_terminates():
    rounds = (tool_round("current_time"), text_round("It is currently that time."))
    with patch("httpx.AsyncClient.stream", fake_ollama(*rounds)):
        r = TestClient(main.app).post("/infer", json={"messages": [
            {"role": "user", "content": "what time is it"}]})

    assert r.status_code == 200, r.text
    msgs = r.json()["messages"]
    tool_msgs = [m for m in msgs if m["role"] == "tool"]
    assert len(tool_msgs) == 1, msgs
    assert tool_msgs[0]["tool_name"] == "current_time"
    # A spoken clock reading, not an error string and not ISO: read back from ISO
    # the model announced the wrong time of day (SPEC.md 20).
    assert re.match(r"^\d\d:\d\d on \w+, \d\d \w+ \d{4} ",
                    tool_msgs[0]["content"]), tool_msgs[0]


def test_unknown_tool_reports_back_instead_of_crashing():
    rounds = (tool_round("nope"), text_round("That tool does not exist."))
    with patch("httpx.AsyncClient.stream", fake_ollama(*rounds)):
        r = TestClient(main.app).post("/infer", json={"messages": [
            {"role": "user", "content": "x"}]})

    assert r.status_code == 200, r.text
    tool_msgs = [m for m in r.json()["messages"] if m["role"] == "tool"]
    assert "no such tool" in tool_msgs[0]["content"]


def test_runaway_tool_loop_is_capped():
    with patch("httpx.AsyncClient.stream", fake_ollama(*[tool_round("current_time")] * 20)):
        r = TestClient(main.app).post("/infer", json={"messages": [
            {"role": "user", "content": "x"}]})

    assert r.status_code == 502, r.text
    assert "tool loop exceeded" in r.json()["detail"]


def test_think_follows_the_setting_and_the_per_request_override():
    """Default is 'model-default', which omits the flag and lets qwen3 decide."""
    post = fake_ollama(text_round("hi"))
    with patch("httpx.AsyncClient.stream", post):
        TestClient(main.app).post("/infer", json={"messages": [
            {"role": "user", "content": "x"}]})
    assert "think" not in post.sent[0], post.sent[0]

    post = fake_ollama(text_round("hi"))
    with patch.dict(settings._overrides, {"llm.think": "never"}), \
         patch("httpx.AsyncClient.stream", post):
        TestClient(main.app).post("/infer", json={"messages": [
            {"role": "user", "content": "x"}]})
    assert post.sent[0]["think"] is False, post.sent[0]

    post = fake_ollama(text_round("hi"))
    with patch("httpx.AsyncClient.stream", post):
        TestClient(main.app).post("/infer", json={"messages": [
            {"role": "user", "content": "x"}], "think": True})
    assert post.sent[0]["think"] is True, post.sent[0]


def test_em_dashes_never_reach_the_client():
    """Santiago does not want them anywhere (SPEC.md 18)."""
    with patch("httpx.AsyncClient.stream",
               fake_ollama(text_round("a thought, interrupted, and resumed"))):
        r = TestClient(main.app).post("/infer", json={"messages": [
            {"role": "user", "content": "x"}]})
    assert "\u2014" not in r.json()["message"]["content"]
    assert reasoning.strip_dashes("one\u2014two") == "one, two"
    assert reasoning.strip_dashes("one \u2013 two") == "one, two"


def test_emoji_is_stripped_but_text_is_not():
    """Emojis are banned outright (SPEC.md 22)."""
    assert reasoning.strip_emoji("Operational \U0001F31F and ready \U0001F60A") \
        == "Operational and ready "
    assert reasoning.strip_emoji("\u2705 done") == " done"
    # Everything that merely looks exotic must survive: bullets, arrows, accents.
    keep = "\u2022 caf\u00e9 \u2192 na\u00efve \u2014 51\u2013200 \u00a9"
    assert reasoning.strip_emoji(keep) == keep


class FakeOww:
    """Fires once, on the frame the test asks for."""
    def __init__(self):
        self.score, self.resets = 0.0, 0

    def predict(self, frame):
        return {"test_v0.1": self.score}

    def reset(self):
        self.resets += 1


class FakeVad:
    def __init__(self):
        self.speech = 0.0

    def predict(self, chunk):
        return self.speech


def _turn():
    import wake
    return wake._Turn(FakeOww(), FakeVad()), __import__("numpy").zeros(1280, dtype="int16")


def test_wake_then_silence_ends_the_turn():
    """The whole point of turn-taking: wake, hear speech, stop when it stops."""
    import wake
    t, frame = _turn()
    assert t.feed(frame) is None                      # asleep, nothing said

    t.oww.score = 0.9
    assert t.feed(frame) == "wake"
    t.oww.score = 0.0

    t.vad.speech = 0.9
    assert t.feed(frame) == "listening"
    for _ in range(3):
        assert t.feed(frame) is None                  # still talking

    t.vad.speech = 0.0
    events = [t.feed(frame) for _ in range(20)]
    assert "done" in events, events
    # The captured audio is the utterance, not the silence that followed it.
    assert len(t.pcm) > 0


def test_wake_without_speech_goes_back_to_sleep():
    t, frame = _turn()
    t.oww.score = 0.9
    assert t.feed(frame) == "wake"
    t.oww.score = 0.0
    events = [t.feed(frame) for _ in range(200)]       # 16 s of nothing
    assert "idle" in events, events
    assert t.state == "sleeping"


def test_iris_speaking_cannot_wake_itself():
    """The wake word is not watched for while IRiS talks, so its own voice in the
    microphone cannot trigger it."""
    t, frame = _turn()
    t.speaking = True
    t.oww.score = 0.99
    assert all(t.feed(frame) != "wake" for _ in range(10))
    assert t.state == "sleeping"


def test_barge_in_needs_sustained_speech_and_keeps_the_preroll():
    import wake
    t, frame = _turn()
    t.speaking = True
    t.vad.speech = 0.9
    events = [t.feed(frame) for _ in range(10)]
    assert "barge_in" in events, events
    # One frame must not be enough, or a cough interrupts IRiS.
    assert events.index("barge_in") >= int(wake.BARGE_SECONDS / wake.FRAME_SECONDS) - 1
    # What was said before the interruption was noticed is still captured.
    assert len(t.pcm) > wake.FRAME * 2


def test_barge_in_respects_the_setting():
    t, frame = _turn()
    t.speaking = True
    t.vad.speech = 0.9
    with patch.dict(settings._overrides, {"voice.barge_in": False}):
        assert all(t.feed(frame) != "barge_in" for _ in range(20))


def test_vad_carries_the_remainder_between_frames():
    """80 ms frames are not a whole number of Silero's 30 ms ones; dropping the
    remainder would quietly lose a third of the audio the endpointer sees."""
    import numpy as np
    import wake
    t, frame = _turn()
    seen = []
    t.vad.predict = lambda chunk: seen.append(len(chunk)) or 0.0
    for _ in range(4):
        t._voice_score(frame)
    assert all(n % wake.VAD_FRAME == 0 for n in seen), seen
    assert sum(seen) >= wake.FRAME * 3            # nothing thrown away


def test_wake_models_never_returns_an_empty_dropdown():
    """An empty enum would make the stored setting fail validation on the next save."""
    import wake
    with patch.object(wake, "_bundled", lambda: {}), \
         patch.object(wake, "CUSTOM_DIR", "/nonexistent"):
        assert wake._wake_models()


def test_short_turns_do_not_trigger_recall():
    """bge-m3 scores a two-word fragment against almost anything inside the range a
    real match occupies, so "the weather" would drag in every memory stored."""
    import asyncio
    called = []

    async def boom(*a, **kw):
        called.append(a)
        return []

    with patch.object(memory, "recall", boom):
        assert asyncio.run(memory.context_for("hi", 1)) == ""
        assert asyncio.run(memory.context_for("the weather", 1)) == ""
        assert not called, "short turns must not even reach the store"
        asyncio.run(memory.context_for("what am I running this on", 1))
        assert called, "a real question must"


def test_recalled_memories_are_offered_not_announced():
    import asyncio
    hits = [{"text": "Santiago runs IRiS on an RTX 3060 Ti."}]

    async def fake(*a, **kw):
        return hits

    with patch.object(memory, "recall", fake):
        ctx = asyncio.run(memory.context_for("what hardware is this", 1))
    assert "RTX 3060 Ti" in ctx
    assert "do not announce" in ctx.lower(), ctx


def test_memory_tools_are_hidden_when_they_cannot_work():
    """An 8B model offered a tool that cannot work will call it anyway, so the tool
    is withheld rather than left to fail: no user to remember for, or memory off."""
    import asyncio

    def offered(**kw):
        post = fake_ollama(text_round("hi"))
        with patch("httpx.AsyncClient.stream", post):
            asyncio.run(reasoning.run([{"role": "user", "content": "x"}], **kw))
        return {t["function"]["name"] for t in post.sent[0]["tools"]}

    anonymous = offered()
    assert "remember" not in anonymous and "recall" not in anonymous, anonymous
    assert "web_search" in anonymous, anonymous

    with patch.dict(settings._overrides, {"memory.enabled": False}):
        assert "remember" not in offered(user_id=1)

    with patch.object(memory, "context_for", lambda *a, **k: _none()):
        assert "remember" in offered(user_id=1)


async def _none():
    return ""


SAID = ("user: I've just moved from Zurich to Winterthur, so my commute is different "
        "now.\nassistant: Winterthur to the office is a different line entirely.")


def test_a_fact_without_real_evidence_is_dropped():
    """The extractor pads its list no matter how the prompt is worded: told "at most
    3" it returns 3, and told not to embellish it embellishes anyway, verbatim, on
    the next run. Requiring a copied span turns "did it obey" into "is this string
    present", which is decidable here instead of by the model."""
    kept = memory.evidenced([
        {"fact": "The user has moved from Zurich to Winterthur.",
         "quote": "I've just moved from Zurich to Winterthur"},
        # The invented one. Fluent, plausible, and said by nobody.
        {"fact": "The user is adjusting to a new daily routine.",
         "quote": "I am adjusting to a new daily routine"},
    ], SAID)
    assert kept == ["The user has moved from Zurich to Winterthur."], kept


def test_a_real_quote_cannot_smuggle_in_an_unrelated_fact():
    """Quoting correctly and then asserting something else is the obvious way round
    the check, so the fact is also tested against its own quote."""
    kept = memory.evidenced([
        {"fact": "The user owns a holiday home in Winterthur.",
         "quote": "I've just moved from Zurich to Winterthur"},
    ], SAID)
    assert kept == [], kept


def test_evidence_survives_punctuation_and_case():
    """A quote is compared on words alone; real spans must not be lost to a stray
    comma or a capital letter."""
    assert memory.evidenced([
        {"fact": "The user has moved to Winterthur.",
         "quote": "Moved from Zurich, to Winterthur!"},
    ], SAID)


def test_grounding_ignores_filler_words():
    assert not memory._grounded("They have been there.", SAID)
    assert not memory._grounded("The user enjoys skiing in the Alps.", SAID)


class FakeRedis:
    """Just the five operations compaction uses."""
    def __init__(self):
        self.kv, self.hashes = {}, {}

    async def set(self, k, v):
        self.kv[k] = v

    async def get(self, k):
        return self.kv.get(k)

    async def delete(self, k):
        self.kv.pop(k, None)

    async def hset(self, k, f, v):
        self.hashes.setdefault(k, {})[f] = v

    async def hgetall(self, k):
        return dict(self.hashes.get(k, {}))

    async def hdel(self, k, f):
        self.hashes.get(k, {}).pop(f, None)

    async def scan_iter(self, pattern):
        import fnmatch
        for k in list(self.hashes):
            if fnmatch.fnmatch(k, pattern):
                yield k


def test_compaction_expires_old_transcripts_and_nothing_else():
    """This deletes real user data, so every boundary is pinned: the window itself,
    the disable switch, and the transcript body as well as the index entry."""
    import asyncio
    import time

    import chat

    async def scenario(days):
        r = FakeRedis()
        now = time.time()
        ages = {"fresh": now - 86400, "edge-29d": now - 29 * 86400,
                "edge-31d": now - 31 * 86400, "ancient": now - 400 * 86400,
                "no-timestamp": None}
        for cid, ts in ages.items():
            await r.set(chat._msgs_key(7, cid), json.dumps([{"role": "user"}]))
            meta = {"id": cid}
            if ts:
                meta["updated"] = ts
            await r.hset(chat._index_key(7), cid, json.dumps(meta))
        with patch.object(chat, "_redis", r):
            result = await chat.compact(days)
        return r, result

    r, result = asyncio.run(scenario(30))
    left = set(asyncio.run(r.hgetall(chat._index_key(7))))
    assert left == {"fresh", "edge-29d"}, left
    assert result == {"removed": 3, "kept": 2}, result
    # The transcript must go too, or the index shrinks while Redis keeps growing.
    for cid in ("edge-31d", "ancient", "no-timestamp"):
        assert asyncio.run(r.get(chat._msgs_key(7, cid))) is None, cid
    for cid in ("fresh", "edge-29d"):
        assert asyncio.run(r.get(chat._msgs_key(7, cid))) is not None, cid

    r, result = asyncio.run(scenario(0))
    assert result["removed"] == 0
    assert len(asyncio.run(r.hgetall(chat._index_key(7)))) == 5


def test_home_and_work_resolve_to_configured_places():
    """SPEC.md 8 had "home/work addresses" as an ASK USER. They are settings instead,
    so the two words a person actually uses work without asking him anything."""
    with patch.dict(settings._overrides, {"location.home": "Winterthur",
                                          "location.work": "Zurich HB"}):
        assert places.resolve("home") == "Winterthur"
        assert places.resolve("Home") == "Winterthur"
        assert places.resolve("the office") == "Zurich HB"
        assert places.resolve("Bern") == "Bern"      # a real place is left alone
    # Unconfigured must fall through to the literal word, never to an empty query,
    # which the transit API answers with every station in the country.
    with patch.dict(settings._overrides, {"location.home": ""}):
        assert places.resolve("home") == "home"


def test_journey_duration_is_readable_aloud():
    """The API returns '00d00:19:00', which is unspeakable (SPEC.md 17)."""
    assert places._minutes("00d00:19:00") == "19 min"
    assert places._minutes("00d01:05:00") == "1h 05"
    assert places._minutes("00d02:00:00") == "2h 00"
    # Anything without the expected shape degrades to "?" rather than being read out.
    assert places._minutes(None) == "?"
    assert places._minutes("nonsense") == "?"
    assert places._minutes("00dxx:yy:00") == "00dxx:yy:00"   # shaped but unparseable


def test_quiet_hours_wrap_midnight():
    """Quiet hours normally cross midnight, so a plain start <= now < end comparison
    is wrong for the common case rather than the rare one, and would let IRiS speak
    at three in the morning."""
    import datetime
    tz = datetime.timezone.utc

    def at(hour):
        return datetime.datetime(2026, 8, 4, hour, 0, tzinfo=tz)

    with patch.dict(settings._overrides, {"proactive.quiet_from": "22:00",
                                          "proactive.quiet_to": "07:00"}):
        assert not proactive.in_quiet_hours(at(21))
        assert proactive.in_quiet_hours(at(22))
        assert proactive.in_quiet_hours(at(3))
        assert proactive.in_quiet_hours(at(6))
        assert not proactive.in_quiet_hours(at(7))
        assert not proactive.in_quiet_hours(at(12))

    # A window that does not wrap must still work.
    with patch.dict(settings._overrides, {"proactive.quiet_from": "09:00",
                                          "proactive.quiet_to": "17:00"}):
        assert proactive.in_quiet_hours(at(12))
        assert not proactive.in_quiet_hours(at(20))

    # Equal bounds means no quiet hours at all, not permanently silent.
    with patch.dict(settings._overrides, {"proactive.quiet_from": "00:00",
                                          "proactive.quiet_to": "00:00"}):
        assert not proactive.in_quiet_hours(at(3))


def test_secret_fields_never_reach_a_client():
    """The registry is the single place credentials could leak from, since every
    device and integration goes through it."""
    import datetime
    spec = registry.spec_for("integration", "mailbox")
    row = (1, "integration", "mailbox", "work",
           {"host": "imap.example.com", "username": "u", "password": "s3cret"},
           True, datetime.datetime(2026, 8, 4, tzinfo=datetime.timezone.utc))

    public = registry._row(row, redact=True)
    assert public["config"]["password"] == registry.MASK
    assert "s3cret" not in json.dumps(public)
    # Non-secret fields must survive, or the UI cannot show what is configured.
    assert public["config"]["host"] == "imap.example.com"

    internal = registry._row(row, redact=False)
    assert internal["config"]["password"] == "s3cret"
    assert spec.fields[0].name == "host"


def test_editing_without_retyping_a_secret_keeps_it():
    """The form sends back the dots it was shown. Taking those literally would
    silently replace every password with bullet characters."""
    spec = registry.spec_for("integration", "mailbox")
    stored = {"host": "a", "username": "u", "password": "s3cret", "port": 993}
    incoming = {"host": "b", "password": registry.MASK}
    kept = {k: v for k, v in incoming.items() if v != registry.MASK}
    merged = registry.validate(spec, {**stored, **kept})
    assert merged["password"] == "s3cret"
    assert merged["host"] == "b"


def test_a_port_is_an_integer_not_a_float():
    """993.0 is not a port, and imaplib will not accept it."""
    spec = registry.spec_for("integration", "mailbox")
    out = registry.validate(spec, {"host": "h", "username": "u", "password": "p",
                                   "port": "993"})
    assert out["port"] == 993 and isinstance(out["port"], int)


def test_unknown_type_names_the_ones_that_exist():
    try:
        registry.spec_for("device", "toaster")
    except Exception as e:
        assert "camera" in str(e.detail), e.detail
    else:
        raise AssertionError("an unknown type must be rejected")


def test_quick_commands_hide_what_is_switched_off():
    """A command that steers at a disabled tool would produce an apology, not an
    answer."""
    with patch.dict(settings._overrides, {"location.enabled": False}):
        names = {c["name"] for c in reasoning.quick_commands()}
    assert "transit" not in names and "find" not in names, names
    assert "search" in names
    assert reasoning.apply_command("search", "debian").endswith("debian")
    assert "Search the web" in reasoning.apply_command("search", "debian")
    # An unknown command must pass the text through untouched, not mangle the turn.
    assert reasoning.apply_command("nonsense", "hello") == "hello"


def test_camera_passwords_are_never_exposed():
    """A camera password is typically reused across a household's devices, so it must
    not reach a browser, the activity log, or a model's context. Punctuation in the
    password is the case that breaks a naive pattern."""
    cases = {
        "rtsp://admin:hunter2@192.168.1.50:554/Streaming/Channels/101":
            "rtsp://admin:____@192.168.1.50:554/Streaming/Channels/101",
        # A colon and an at-sign inside the password: a lazy match leaks the tail.
        "rtsps://user:p@ss:word@cam.local/stream": "rtsps://user:____@cam.local/stream",
        # Nothing to hide must be left exactly alone.
        "http://192.168.1.9/snapshot.jpg": "http://192.168.1.9/snapshot.jpg",
        "rtsp://192.168.1.50:554/stream": "rtsp://192.168.1.50:554/stream",
    }
    for raw, expected in cases.items():
        assert cameras.mask(raw) == expected, (raw, cameras.mask(raw))
    for raw in cases:
        masked = cameras.mask(raw)
        for secret in ("hunter2", "p@ss:word", "ss:word", "word"):
            assert secret not in masked or secret in raw.split("@")[-1], (raw, masked)


def test_ffmpeg_errors_do_not_echo_the_stream_password():
    """ffmpeg prints the whole URL back on failure, so its stderr is masked before
    it reaches an error message. Cameras store their URL as a secret field now, so
    this is the remaining path a password could escape by."""
    stderr = ("rtsp://admin:hunter2@10.0.0.5/stream: Connection refused\n"
              "Error opening input file rtsp://admin:hunter2@10.0.0.5/stream.")
    assert "hunter2" not in cameras.mask(stderr)
    assert "10.0.0.5" in cameras.mask(stderr)      # still diagnosable


def test_backup_delete_cannot_escape_the_backup_directory():
    """The filename comes from the client, so a crafted one must not walk out."""
    import asyncio
    for evil in ["../../etc/passwd", "/etc/passwd", "iris-x", "notes.txt",
                 "../.env", "iris-../../../etc/shadow.tar.gz.enc"]:
        try:
            asyncio.run(backup.delete(evil, {"username": "t"}))
        except Exception as e:
            assert getattr(e, "status_code", None) == 400, (evil, e)
            continue
        # Anything accepted must have resolved to a plain name inside BACKUP_DIR.
        resolved = (backup.BACKUP_DIR / __import__("pathlib").Path(evil).name)
        assert resolved.parent == backup.BACKUP_DIR, evil
        assert resolved.name.startswith(backup.PREFIX), evil


def test_backup_prunes_only_the_oldest_beyond_the_limit(tmp_path=None):
    """Pruning is what deletes real data, so the ordering must be right: newest
    first, and only entries past the keep count are ever touched."""
    import pathlib
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        old = backup.BACKUP_DIR
        try:
            backup.BACKUP_DIR = pathlib.Path(d)
            names = [f"{backup.PREFIX}2026080{i}-000000{backup.SUFFIX}"
                     for i in range(1, 6)]
            for n in names:
                (backup.BACKUP_DIR / n).write_bytes(b"x")
            (backup.BACKUP_DIR / "unrelated.txt").write_bytes(b"x")
            found = [p.name for p in backup.archives()]
            assert found == sorted(names, reverse=True), found
            assert "unrelated.txt" not in found
        finally:
            backup.BACKUP_DIR = old


def test_web_search_tool_is_registered():
    """IRiS must search rather than guess (SPEC.md 18)."""
    assert "web_search" in reasoning.TOOLS
    schema = reasoning.TOOLS["web_search"][0]["function"]
    assert "query" in schema["parameters"]["properties"]
    assert "search" in schema["description"].lower()


def test_think_omitted_for_non_thinking_models():
    """Non-thinking models (qwen2.5) reject the flag, so it must be omittable."""
    post = fake_ollama(text_round("hi"))
    with patch.dict(settings._overrides, {"llm.think": "model-default"}), \
         patch("httpx.AsyncClient.stream", post):
        TestClient(main.app).post("/infer", json={"messages": [
            {"role": "user", "content": "x"}]})
    assert "think" not in post.sent[0], post.sent[0]


def test_infer_uses_the_configured_model_not_the_env_default():
    post = fake_ollama(text_round("hi"))
    with patch.dict(settings._overrides, {"llm.model": "some-other-model"}), \
         patch("httpx.AsyncClient.stream", post):
        TestClient(main.app).post("/infer", json={"messages": [
            {"role": "user", "content": "x"}]})
    assert post.sent[0]["model"] == "some-other-model", post.sent[0]


def test_stream_emits_deltas_before_completion():
    """Nothing user-facing waits for the whole reply (SPEC.md 16), so the events must
    arrive incrementally and in order: text deltas, then done."""
    import asyncio

    rounds = (tool_round("current_time"), text_round("One. Two."))
    with patch("httpx.AsyncClient.stream", fake_ollama(*rounds)):
        async def collect():
            return [e async for e in main.reasoning.stream(
                [{"role": "user", "content": "x"}])]
        events = asyncio.run(collect())

    kinds = [e["type"] for e in events]
    # tool_start fires before the tool runs, so the client can show what it is doing.
    assert kinds[0] == "tool_start", kinds
    assert kinds[1] == "tool", kinds
    assert kinds[-1] == "done", kinds
    deltas = [e["text"] for e in events if e["type"] == "delta"]
    assert len(deltas) > 1, "reply arrived in one lump instead of streaming"
    assert "".join(deltas) == "One. Two."
    assert events[-1]["message"]["content"] == "One. Two."


def test_run_and_stream_share_one_path():
    """run() collects stream(), so the two cannot drift apart."""
    with patch("httpx.AsyncClient.stream", fake_ollama(text_round("same answer"))):
        r = TestClient(main.app).post("/infer", json={"messages": [
            {"role": "user", "content": "x"}]})
    assert r.json()["message"]["content"] == "same answer"


# ------------------------------------------------------------- settings ----

class FakeConn:
    """Stands in for a Postgres connection; records the statements issued."""
    executed = []

    async def execute(self, sql, params=None):
        FakeConn.executed.append((sql, params))
        return self

    async def fetchall(self):
        return []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


async def fake_connect():
    return FakeConn()


def test_schema_exposes_registered_settings_with_titles():
    """Clients build their whole UI from this, so every entry needs a title."""
    props = settings.schema()["properties"]
    assert "llm.model" in props and "llm.think" in props
    assert all("title" in spec and "default" in spec for spec in props.values())


def test_callable_enum_is_resolved_in_the_schema():
    """Choices that change at runtime are registered as a function, not a fixed list."""
    settings.setting("test.dynamic", type="string", title="Dynamic",
                     enum=lambda: ["a", "b"], default="a")
    try:
        assert settings.schema()["properties"]["test.dynamic"]["enum"] == ["a", "b"]
        # and the resolved list is what validation enforces
        r = TestClient(main.app).put("/settings/values", json={"test.dynamic": "c"})
        assert r.status_code == 400, r.text
    finally:
        settings.REGISTRY.pop("test.dynamic")


def test_every_choice_setting_offers_a_dropdown():
    """Anything with fixed expected values must enumerate them, not ask for typing."""
    props = settings.schema()["properties"]
    for key in ("llm.model", "llm.think", "general.timezone"):
        assert props[key].get("enum"), f"{key} should offer a dropdown, not free text"


def test_model_dropdown_survives_ollama_being_down():
    """A stopped Ollama must not empty the list and invalidate the stored value."""
    main._tags_cache = (0.0, [])
    with patch("httpx.get", side_effect=OSError("connection refused")):
        choices = main._installed_models()
    assert settings.get("llm.model") in choices


def test_unknown_setting_is_rejected():
    r = TestClient(main.app).put("/settings/values", json={"llm.nonsense": 1})
    assert r.status_code == 400
    assert "unknown settings" in r.json()["detail"]


def test_value_violating_the_schema_is_rejected():
    r = TestClient(main.app).put("/settings/values", json={"llm.think": "sometimes"})
    assert r.status_code == 400, r.text
    assert "llm.think" in r.json()["detail"]


def test_valid_change_is_persisted_and_broadcast():
    FakeConn.executed = []
    with patch.dict(settings._overrides, {}, clear=True), \
         patch.object(settings, "_connect", fake_connect):
        r = TestClient(main.app).put("/settings/values",
                                     json={"general.timezone": "Europe/Berlin"})
        assert r.status_code == 200, r.text
        assert r.json()["general.timezone"] == "Europe/Berlin"
        assert settings.get("general.timezone") == "Europe/Berlin"
    assert any("INSERT INTO settings" in sql for sql, _ in FakeConn.executed)


def test_model_not_installed_is_rejected():
    """The dropdown only offers pulled models; the API must enforce the same."""
    r = TestClient(main.app).put("/settings/values", json={"llm.model": "not-pulled:70b"})
    assert r.status_code == 400, r.text
    assert "llm.model" in r.json()["detail"]


def test_defaults_apply_when_nothing_is_stored():
    with patch.dict(settings._overrides, {}, clear=True):
        assert settings.values()["llm.think"] == settings.REGISTRY["llm.think"]["default"]


# ---------------------------------------------------------------- voice ----

def test_spoken_text_drops_markdown():
    """Reading "**" or a list marker aloud is a defect (SPEC.md 17)."""
    out = voice.speech_text("1. **Discovery**: it `works`.\n2. *Offer*: see [docs](u).")
    for junk in ("*", "#", "`", "[", "]", "1."):
        assert junk not in out, (junk, out)
    assert "Discovery" in out and "docs" in out


def test_initialisms_are_spelled_but_words_are_not():
    spelled = voice.speech_text("AG IT DHCP API CPU HTTPS")
    for pair in ("A G", "I T", "D H C P", "A P I", "C P U", "H T T P S"):
        assert pair in spelled, (pair, spelled)
    # ...while pronounceable all-caps names survive intact
    for word in ("SIDMAR", "NASA", "RAM"):
        assert word in voice.speech_text(f"{word} is fine"), word
    assert "IRiS" in voice.speech_text("IRiS is mixed case")


def test_spoken_text_always_ends_with_a_pause():
    """Every utterance ends on a stop. The extra ellipsis is XTTS-only: XTTS clips
    the tail without trailing room, and it made Piper trail off into a mumble."""
    with patch.dict(settings._overrides, {"voice.engine": "piper"}):
        assert voice.speech_text("No trailing punctuation here") \
            == "No trailing punctuation here."
    with patch.dict(settings._overrides, {"voice.engine": "xtts"}):
        assert voice.speech_text("No trailing punctuation here").endswith("…")
        assert voice.speech_text("Already punctuated!").endswith("…")


def test_persona_is_sent_but_never_stored():
    """Editing the persona must affect existing conversations, so it cannot be
    frozen into the transcript (SPEC.md 17)."""
    post = fake_ollama(text_round("hello"))
    with patch("httpx.AsyncClient.stream", post):
        r = TestClient(main.app).post("/infer", json={"messages": [
            {"role": "user", "content": "hi"}]})
    sent_roles = [m["role"] for m in post.sent[0]["messages"]]
    assert sent_roles[0] == "system", sent_roles
    stored_roles = [m["role"] for m in r.json()["messages"]]
    assert "system" not in stored_roles, stored_roles


def test_persona_refuses_the_disclaimer():
    prompt = persona.system_message()["content"]
    assert "just an AI" in prompt and "don't have feelings" in prompt
    assert "Creator" in prompt


# ----------------------------------------------------------------- auth ----

def test_password_hash_roundtrip():
    h = auth.hash_password("correct horse battery staple")
    assert h.startswith("scrypt$")
    assert "correct horse" not in h, "password must not be recoverable from the hash"
    assert auth.verify_password("correct horse battery staple", h)
    assert not auth.verify_password("wrong password", h)


def test_each_hash_uses_a_fresh_salt():
    """Identical passwords must not produce identical hashes."""
    assert auth.hash_password("same") != auth.hash_password("same")


def test_verify_rejects_malformed_hashes_instead_of_crashing():
    for bad in ("", "nonsense", "scrypt$onlyonefield", "bcrypt$aa$bb", "scrypt$zz$zz"):
        assert not auth.verify_password("anything", bad), bad


def test_api_keys_are_stored_as_digests():
    raw = "iris_" + "a" * 43
    digest = auth._hash_key(raw)
    assert digest != raw and len(digest) == 64


def test_protected_endpoint_401s_without_a_session():
    """Uses a client with no dependency override, so the real gate runs."""
    from fastapi import FastAPI
    probe = FastAPI()
    probe.include_router(settings.router, dependencies=[Depends(auth.active_user)])
    r = TestClient(probe).get("/settings/values")
    assert r.status_code == 401, r.text


if __name__ == "__main__":
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_"):
            _fn()
    print("ok")
