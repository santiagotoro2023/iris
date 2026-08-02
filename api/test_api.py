"""Check the tool-calling loop without needing Ollama or a GPU.

Run: python -m pytest test_api.py   (or just: python test_api.py)
"""
import json
from unittest.mock import patch

from fastapi.testclient import TestClient

import main


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


def test_think_omitted_when_env_disables_it():
    """Non-thinking models (qwen2.5) reject the flag, so it must be omittable."""
    reply = {"role": "assistant", "content": "hi"}
    post = fake_ollama(reply)
    with patch.object(main, "DEFAULT_THINK", None), patch("httpx.AsyncClient.post", post):
        TestClient(main.app).post("/infer", json={"messages": [
            {"role": "user", "content": "x"}]})
    assert "think" not in post.sent[0], post.sent[0]


if __name__ == "__main__":
    test_tool_call_executes_and_loop_terminates()
    test_unknown_tool_reports_back_instead_of_crashing()
    test_runaway_tool_loop_is_capped()
    test_think_defaults_off_and_is_overridable_per_request()
    test_think_omitted_when_env_disables_it()
    print("ok")
