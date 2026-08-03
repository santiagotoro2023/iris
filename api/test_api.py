"""Check the tool-calling loop without needing Ollama or a GPU.

Run: python -m pytest test_api.py   (or just: python test_api.py)
"""
import json
from unittest.mock import patch

from fastapi import Depends
from fastapi.testclient import TestClient

import auth
import main
import persona
import reasoning
import settings
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
    # current_time returns an ISO timestamp, not an error string
    assert tool_msgs[0]["content"].startswith("20"), tool_msgs[0]


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
    """XTTS clips the tail of an utterance without trailing room."""
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
