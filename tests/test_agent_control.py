"""Tests for AgentController + the /api/agent control surface."""

import importlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.agent_control import AgentController


class _FakeRisk:
    def __init__(self):
        self.killed = False
        self.reason = ""

    def get_summary(self):
        return {"kill_switch_triggered": self.killed, "kill_switch_reason": self.reason}

    def trigger_kill_switch(self, reason="Manual trigger"):
        self.killed = True
        self.reason = reason

    def reset_kill_switch(self):
        self.killed = False
        self.reason = ""


class _FakePortfolio:
    def get_summary(self):
        return {"pnl": {"total_pnl": 12.5}}


def _controller():
    return AgentController(portfolio=_FakePortfolio(), risk_manager=_FakeRisk(),
                          mode="dry_run")


# ---- controller unit tests ------------------------------------------------
def test_pause_resume_toggles_flag():
    c = _controller()
    assert c.paused is False
    c.pause()
    assert c.paused is True
    c.resume()
    assert c.paused is False


def test_kill_switch_round_trip():
    c = _controller()
    c.trigger_kill_switch("test halt")
    assert c.status()["kill_switch_triggered"] is True
    assert c.status()["kill_switch_reason"] == "test halt"
    c.reset_kill_switch()
    assert c.status()["kill_switch_triggered"] is False


# ---- API auth / gating tests ---------------------------------------------
@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("AGENT_API_TOKEN", "secret123")
    agent_api = importlib.import_module("dashboard.agent_api")
    agent_api.set_controller(_controller(), allow_control=True)
    app = FastAPI()
    app.include_router(agent_api.router)
    return TestClient(app)


def test_missing_token_rejected(client):
    assert client.get("/api/agent/status").status_code == 401


def test_wrong_token_rejected(client):
    r = client.get("/api/agent/status", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_valid_token_reads_status(client):
    r = client.get("/api/agent/status", headers={"Authorization": "Bearer secret123"})
    assert r.status_code == 200
    assert r.json()["mode"] == "dry_run"


def test_kill_switch_requires_confirm(client):
    h = {"Authorization": "Bearer secret123"}
    assert client.post("/api/agent/kill-switch", headers=h, json={}).status_code == 400
    ok = client.post("/api/agent/kill-switch", headers=h, json={"confirm": True})
    assert ok.status_code == 200
    assert ok.json()["kill_switch_triggered"] is True


def test_disabled_without_token(monkeypatch):
    monkeypatch.delenv("AGENT_API_TOKEN", raising=False)
    agent_api = importlib.import_module("dashboard.agent_api")
    agent_api.set_controller(_controller(), allow_control=True)
    app = FastAPI(); app.include_router(agent_api.router)
    c = TestClient(app)
    assert c.get("/api/agent/status").status_code == 503


def test_control_blocked_when_allow_control_false(monkeypatch):
    monkeypatch.setenv("AGENT_API_TOKEN", "secret123")
    agent_api = importlib.import_module("dashboard.agent_api")
    agent_api.set_controller(_controller(), allow_control=False)
    app = FastAPI(); app.include_router(agent_api.router)
    c = TestClient(app)
    h = {"Authorization": "Bearer secret123"}
    # read still works...
    assert c.get("/api/agent/status", headers=h).status_code == 200
    # ...but control is forbidden
    assert c.post("/api/agent/pause", headers=h).status_code == 403
