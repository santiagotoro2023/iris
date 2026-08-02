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
    """Return an async post() that yields each scripted reply in turn."""
    it = iter(replies)

    async def post(self, url, json=None, **kw):
        return FakeResponse({"message": next(it)})

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


if __name__ == "__main__":
    test_tool_call_executes_and_loop_terminates()
    test_unknown_tool_reports_back_instead_of_crashing()
    test_runaway_tool_loop_is_capped()
    print("ok")
