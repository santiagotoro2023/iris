"""Check the tool-calling loop without needing Ollama or a GPU.

Run: python -m pytest test_api.py   (or just: python test_api.py)
"""
import json
from unittest.mock import patch

from fastapi.testclient import TestClient

import main
import settings


class FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def fake_ollama(*replies):
    """Return an async post() that yields each scripted reply and records what was sent."""
    it = iter(replies)
    sent = []

    async def post(self, url, json=None, **kw):
        sent.append(json)
        return FakeResponse({"message": next(it)})

    post.sent = sent
    return post


def test_tool_call_executes_and_loop_terminates():
    replies = (
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "current_time", "arguments": {}}}]},
        {"role": "assistant", "content": "It is currently that time."},
    )
    with patch("httpx.AsyncClient.post", fake_ollama(*replies)):
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
    replies = (
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "nope", "arguments": {}}}]},
        {"role": "assistant", "content": "That tool does not exist."},
    )
    with patch("httpx.AsyncClient.post", fake_ollama(*replies)):
        r = TestClient(main.app).post("/infer", json={"messages": [
            {"role": "user", "content": "x"}]})

    assert r.status_code == 200, r.text
    tool_msgs = [m for m in r.json()["messages"] if m["role"] == "tool"]
    assert "no such tool" in tool_msgs[0]["content"]


def test_runaway_tool_loop_is_capped():
    forever = {"role": "assistant", "tool_calls": [
        {"function": {"name": "current_time", "arguments": {}}}]}
    with patch("httpx.AsyncClient.post", fake_ollama(*[forever] * 20)):
        r = TestClient(main.app).post("/infer", json={"messages": [
            {"role": "user", "content": "x"}]})

    assert r.status_code == 508, r.text


def test_think_defaults_off_and_is_overridable_per_request():
    reply = {"role": "assistant", "content": "hi"}

    post = fake_ollama(reply)
    with patch("httpx.AsyncClient.post", post):
        TestClient(main.app).post("/infer", json={"messages": [
            {"role": "user", "content": "x"}]})
    assert post.sent[0]["think"] is False, post.sent[0]

    post = fake_ollama(reply)
    with patch("httpx.AsyncClient.post", post):
        TestClient(main.app).post("/infer", json={"messages": [
            {"role": "user", "content": "x"}], "think": True})
    assert post.sent[0]["think"] is True, post.sent[0]


def test_think_omitted_for_non_thinking_models():
    """Non-thinking models (qwen2.5) reject the flag, so it must be omittable."""
    reply = {"role": "assistant", "content": "hi"}
    post = fake_ollama(reply)
    with patch.dict(settings._overrides, {"llm.think": "model-default"}), \
         patch("httpx.AsyncClient.post", post):
        TestClient(main.app).post("/infer", json={"messages": [
            {"role": "user", "content": "x"}]})
    assert "think" not in post.sent[0], post.sent[0]


def test_infer_uses_the_configured_model_not_the_env_default():
    reply = {"role": "assistant", "content": "hi"}
    post = fake_ollama(reply)
    with patch.dict(settings._overrides, {"llm.model": "some-other-model"}), \
         patch("httpx.AsyncClient.post", post):
        TestClient(main.app).post("/infer", json={"messages": [
            {"role": "user", "content": "x"}]})
    assert post.sent[0]["model"] == "some-other-model", post.sent[0]


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


if __name__ == "__main__":
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_"):
            _fn()
    print("ok")
