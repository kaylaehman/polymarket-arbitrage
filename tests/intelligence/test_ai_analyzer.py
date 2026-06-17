"""Tests for intelligence.ai_analyzer — JSON parsing, clamping, error fallback.

The Anthropic HTTP call is mocked so no network/API key is required.
"""

import httpx
import pytest

from intelligence import ai_analyzer
from intelligence.ai_analyzer import AIAnalyzer


class _FakeResponse:
    def __init__(self, json_data, status_code=200):
        self._json = json_data
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=None)

    def json(self):
        return self._json


class _FakeClient:
    def __init__(self, response=None, raise_exc=None):
        self._response = response
        self._raise_exc = raise_exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, *args, **kwargs):
        if self._raise_exc:
            raise self._raise_exc
        return self._response


def _content(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}]}


def _patch_client(monkeypatch, response=None, raise_exc=None):
    monkeypatch.setattr(
        ai_analyzer.httpx,
        "AsyncClient",
        lambda *a, **k: _FakeClient(response=response, raise_exc=raise_exc),
    )


@pytest.mark.asyncio
async def test_valid_json_parsed(monkeypatch):
    body = '{"probability": 0.72, "confidence": 0.8, "reasoning": "Strong news"}'
    _patch_client(monkeypatch, response=_FakeResponse(_content(body)))
    analyzer = AIAnalyzer(api_key="key")

    prob, conf, reason = await analyzer.analyze("Will X?", 0.6, [])
    assert prob == 0.72
    assert conf == 0.8
    assert reason == "Strong news"


@pytest.mark.asyncio
async def test_strips_markdown_fences(monkeypatch):
    body = '```json\n{"probability": 0.4, "confidence": 0.6, "reasoning": "ok"}\n```'
    _patch_client(monkeypatch, response=_FakeResponse(_content(body)))
    analyzer = AIAnalyzer(api_key="key")

    prob, conf, reason = await analyzer.analyze("Will X?", 0.5, [])
    assert prob == 0.4
    assert conf == 0.6


@pytest.mark.asyncio
async def test_malformed_json_falls_back_to_price(monkeypatch):
    _patch_client(monkeypatch, response=_FakeResponse(_content("not json at all")))
    analyzer = AIAnalyzer(api_key="key")

    prob, conf, reason = await analyzer.analyze("Will X?", 0.55, [])
    assert prob == 0.55  # falls back to current price
    assert conf == 0.0
    assert reason == "Parse error"


@pytest.mark.asyncio
async def test_probability_and_confidence_clamped(monkeypatch):
    body = '{"probability": 1.5, "confidence": -0.3, "reasoning": "out of range"}'
    _patch_client(monkeypatch, response=_FakeResponse(_content(body)))
    analyzer = AIAnalyzer(api_key="key")

    prob, conf, _ = await analyzer.analyze("Will X?", 0.5, [])
    assert prob == 1.0   # clamped to 1.0
    assert conf == 0.0   # clamped to 0.0


@pytest.mark.asyncio
async def test_request_failure_falls_back(monkeypatch):
    _patch_client(monkeypatch, raise_exc=httpx.TimeoutException("slow"))
    analyzer = AIAnalyzer(api_key="key")

    prob, conf, reason = await analyzer.analyze("Will X?", 0.42, [])
    assert prob == 0.42
    assert conf == 0.0
    assert reason == "Claude request failed"


@pytest.mark.asyncio
async def test_disabled_without_key():
    analyzer = AIAnalyzer(api_key=None)
    prob, conf, reason = await analyzer.analyze("Will X?", 0.3, [])
    assert prob == 0.3
    assert conf == 0.0
    assert "disabled" in reason.lower()
