"""Tests for core/alerts.py — TDD London School."""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

import core.alerts as alerts_module
from core.alerts import Alerter


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_global_alerter():
    """Each test gets a clean global singleton."""
    original = alerts_module._ALERTER
    yield
    alerts_module._ALERTER = original


def _make_alerter(*, discord=None, tg_token=None, tg_chat=None,
                  cooldown=60.0, now_fn=None):
    if now_fn is None:
        now_fn = time.monotonic
    return Alerter(
        discord_webhook=discord,
        telegram_bot_token=tg_token,
        telegram_chat_id=tg_chat,
        cooldown_seconds=cooldown,
        now_fn=now_fn,
    )


# ---------------------------------------------------------------------------
# PART A-1: Alerter.send — Discord payload
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_discord_post_correct_payload():
    """Alerter sends the correct JSON to the Discord webhook."""
    alerter = _make_alerter(discord="https://discord.test/webhook")

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_cls.return_value = mock_client

        result = await alerter.send("test_event", "My Title", "My Body", severity="warn")

    mock_client.post.assert_called_once()
    call_args = mock_client.post.call_args
    url = call_args.args[0] if call_args.args else call_args.kwargs.get("url", "")
    assert url == "https://discord.test/webhook"
    json_payload = call_args.kwargs.get("json", {})
    assert "content" in json_payload
    assert "warn" in json_payload["content"]
    assert "My Title" in json_payload["content"]
    assert "My Body" in json_payload["content"]
    assert "discord" in result


@pytest.mark.asyncio
async def test_telegram_post_correct_params():
    """Alerter sends the correct POST to the Telegram sendMessage endpoint."""
    alerter = _make_alerter(tg_token="BOTTOKEN", tg_chat="CHAT123")

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_cls.return_value = mock_client

        result = await alerter.send("test_event", "TG Title", "TG Body", severity="info")

    mock_client.post.assert_called_once()
    call_args = mock_client.post.call_args
    url = call_args.args[0] if call_args.args else call_args.kwargs.get("url", "")
    assert "api.telegram.org" in url
    assert "BOTTOKEN" in url
    assert "sendMessage" in url
    json_payload = call_args.kwargs.get("json", {})
    assert json_payload.get("chat_id") == "CHAT123"
    text = json_payload.get("text", "")
    assert "TG Title" in text
    assert "TG Body" in text
    assert "telegram" in result


@pytest.mark.asyncio
async def test_both_channels_sends_twice():
    """When both Discord and Telegram are configured, both are sent."""
    alerter = _make_alerter(
        discord="https://discord.test/hook",
        tg_token="TOK",
        tg_chat="CHAT",
    )

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_cls.return_value = mock_client

        result = await alerter.send("evt", "T", "B")

    assert mock_client.post.call_count == 2
    assert "discord" in result
    assert "telegram" in result


# ---------------------------------------------------------------------------
# PART A-2: No-op when unconfigured
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_noop_when_no_channels():
    """send() returns empty list and makes no HTTP calls when no channels configured."""
    alerter = _make_alerter()  # no discord, no telegram

    with patch("httpx.AsyncClient") as mock_cls:
        result = await alerter.send("evt", "T", "B")

    mock_cls.assert_not_called()
    assert result == []


# ---------------------------------------------------------------------------
# PART A-3: Dedup / cooldown
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dedup_suppresses_repeat_within_cooldown():
    """A second send with the same (event_type, dedup_key) within cooldown is suppressed."""
    clock = [0.0]
    alerter = _make_alerter(
        discord="https://discord.test/hook",
        cooldown=60.0,
        now_fn=lambda: clock[0],
    )

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_cls.return_value = mock_client

        # First send at t=0
        r1 = await alerter.send("kill_switch", "Title", "Body", dedup_key="k1")
        assert "discord" in r1

        # Second send at t=30 (within 60s cooldown)
        clock[0] = 30.0
        r2 = await alerter.send("kill_switch", "Title", "Body", dedup_key="k1")
        assert r2 == []

        assert mock_client.post.call_count == 1


@pytest.mark.asyncio
async def test_dedup_allows_after_cooldown_expires():
    """A send after the cooldown window passes is NOT suppressed."""
    clock = [0.0]
    alerter = _make_alerter(
        discord="https://discord.test/hook",
        cooldown=60.0,
        now_fn=lambda: clock[0],
    )

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_cls.return_value = mock_client

        await alerter.send("kill_switch", "T", "B", dedup_key="k1")
        clock[0] = 61.0
        r2 = await alerter.send("kill_switch", "T", "B", dedup_key="k1")

    assert "discord" in r2
    assert mock_client.post.call_count == 2


@pytest.mark.asyncio
async def test_different_dedup_keys_not_suppressed():
    """Different dedup_keys are tracked independently."""
    alerter = _make_alerter(
        discord="https://discord.test/hook",
        cooldown=60.0,
        now_fn=lambda: 0.0,
    )

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_cls.return_value = mock_client

        await alerter.send("evt", "T", "B", dedup_key="mkt-A")
        await alerter.send("evt", "T", "B", dedup_key="mkt-B")

    assert mock_client.post.call_count == 2


# ---------------------------------------------------------------------------
# PART A-4: send() never raises even on httpx error
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_swallows_httpx_exception():
    """If httpx raises, send() must not propagate — it logs and returns."""
    import httpx
    alerter = _make_alerter(discord="https://discord.test/hook")

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
        mock_cls.return_value = mock_client

        # Must not raise
        result = await alerter.send("evt", "T", "B")

    # discord was attempted but failed; result may be empty or contain attempted channels
    # The important thing is no exception was raised. Result is implementation-defined.
    assert isinstance(result, list)


@pytest.mark.asyncio
async def test_send_swallows_generic_exception():
    """If an arbitrary exception is raised, send() must not propagate."""
    alerter = _make_alerter(discord="https://discord.test/hook")

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(side_effect=RuntimeError("oops"))
        mock_cls.return_value = mock_client

        result = await alerter.send("evt", "T", "B")

    assert isinstance(result, list)


# ---------------------------------------------------------------------------
# PART A-5: Module-level notify no-ops when not configured
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_notify_noop_when_no_alerter():
    """alerts.notify is a no-op (no exception) when configure() was never called."""
    alerts_module._ALERTER = None
    # Must not raise
    await alerts_module.notify("evt", "T", "B")


@pytest.mark.asyncio
async def test_configure_and_notify():
    """configure() sets the global alerter; notify() calls through to send()."""
    alerter = _make_alerter(discord="https://discord.test/hook")
    alerter.send = AsyncMock(return_value=["discord"])
    alerts_module.configure(alerter)

    await alerts_module.notify("evt", "Title", "Body", severity="info")

    alerter.send.assert_called_once_with("evt", "Title", "Body", severity="info", dedup_key="")


# ---------------------------------------------------------------------------
# PART A-6: Wiring — kill switch fires notify when alerts enabled
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_kill_switch_fires_notify_when_enabled(monkeypatch):
    """RiskManager._trigger_kill_switch calls alerts.notify when an alerter is set."""
    from core.risk_manager import RiskManager, RiskConfig

    calls = []

    async def fake_notify(event_type, title, body, severity="info", dedup_key=""):
        calls.append((event_type, severity))

    monkeypatch.setattr(alerts_module, "_ALERTER", MagicMock())
    monkeypatch.setattr(alerts_module, "notify", fake_notify)

    rm = RiskManager(RiskConfig())
    rm._trigger_kill_switch("Daily loss limit exceeded")

    # Give any tasks a chance to run
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert any(e == "kill_switch" for e, _ in calls), f"notify not called: {calls}"
    assert any(s == "critical" for _, s in calls)


@pytest.mark.asyncio
async def test_kill_switch_no_notify_when_disabled(monkeypatch):
    """RiskManager._trigger_kill_switch does NOT call alerts.notify when _ALERTER is None."""
    from core.risk_manager import RiskManager, RiskConfig

    calls = []

    async def fake_notify(*a, **kw):
        calls.append(a)

    monkeypatch.setattr(alerts_module, "_ALERTER", None)
    monkeypatch.setattr(alerts_module, "notify", fake_notify)

    rm = RiskManager(RiskConfig())
    rm._trigger_kill_switch("reason")

    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert calls == []


# ---------------------------------------------------------------------------
# PART A-7: Wiring — directional position open fires notify
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_position_open_fires_notify_when_enabled(monkeypatch):
    """Executor._record calls alerts.notify when alerter is configured."""
    from core.directional.executor import Executor
    from core.directional.models import DirectionalOrder

    calls = []

    async def fake_notify(event_type, title, body, severity="info", dedup_key=""):
        calls.append((event_type, dedup_key))

    class Store:
        def record_position(self, p): pass

    monkeypatch.setattr(alerts_module, "_ALERTER", MagicMock())
    monkeypatch.setattr(alerts_module, "notify", fake_notify)

    store = Store()
    ex = Executor(None, store)
    order = DirectionalOrder("mkt-001", "YES", 0.4, 5, 2.0, "safe_compounder")
    ex._record(order, "paper", None, None)

    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert any(e == "directional_open" for e, _ in calls), f"notify not called: {calls}"
    assert any(k == "mkt-001" for _, k in calls)


@pytest.mark.asyncio
async def test_position_open_no_notify_when_disabled(monkeypatch):
    """Executor._record does NOT call alerts.notify when _ALERTER is None."""
    from core.directional.executor import Executor
    from core.directional.models import DirectionalOrder

    calls = []

    async def fake_notify(*a, **kw):
        calls.append(a)

    class Store:
        def record_position(self, p): pass

    monkeypatch.setattr(alerts_module, "_ALERTER", None)
    monkeypatch.setattr(alerts_module, "notify", fake_notify)

    store = Store()
    ex = Executor(None, store)
    order = DirectionalOrder("mkt-001", "YES", 0.4, 5, 2.0, "safe_compounder")
    ex._record(order, "paper", None, None)

    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert calls == []


# ---------------------------------------------------------------------------
# PART B-1 (C1 regression): _trigger_kill_switch fires with NO running event loop
# ---------------------------------------------------------------------------

def test_kill_switch_fires_notify_without_running_loop(monkeypatch):
    """_trigger_kill_switch dispatches alert even when called from a plain sync context
    (no running event loop).  asyncio.run() fallback must invoke notify."""
    from core.risk_manager import RiskManager, RiskConfig

    calls = []

    async def fake_notify(event_type, title, body, severity="info", dedup_key=""):
        calls.append((event_type, severity))

    monkeypatch.setattr(alerts_module, "_ALERTER", MagicMock())
    monkeypatch.setattr(alerts_module, "notify", fake_notify)

    rm = RiskManager(RiskConfig())
    # Called from a plain sync function — no running loop.
    rm._trigger_kill_switch("sync-context trigger")

    assert any(e == "kill_switch" for e, _ in calls), (
        f"notify not called from sync context: {calls}"
    )
    assert any(s == "critical" for _, s in calls)


def test_record_fires_notify_without_running_loop(monkeypatch):
    """Executor._record dispatches alert from a plain sync context via asyncio.run()."""
    from core.directional.executor import Executor
    from core.directional.models import DirectionalOrder

    calls = []

    async def fake_notify(event_type, title, body, severity="info", dedup_key=""):
        calls.append((event_type, dedup_key))

    class Store:
        def record_position(self, p): pass

    monkeypatch.setattr(alerts_module, "_ALERTER", MagicMock())
    monkeypatch.setattr(alerts_module, "notify", fake_notify)

    store = Store()
    ex = Executor(None, store)
    order = DirectionalOrder("mkt-sync", "YES", 0.5, 3, 1.5, "safe_compounder")
    ex._record(order, "paper", None, None)

    assert any(e == "directional_open" for e, _ in calls), (
        f"notify not called from sync context: {calls}"
    )


# ---------------------------------------------------------------------------
# PART B-2: Critical alerts bypass cooldown
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_critical_bypasses_cooldown():
    """Two critical sends within the cooldown window both go through."""
    clock = [0.0]
    alerter = _make_alerter(
        discord="https://discord.test/hook",
        cooldown=3600.0,
        now_fn=lambda: clock[0],
    )

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_cls.return_value = mock_client

        r1 = await alerter.send("kill_switch", "KS", "first", severity="critical", dedup_key="ks")
        clock[0] = 1.0  # well within cooldown
        r2 = await alerter.send("kill_switch", "KS", "second", severity="critical", dedup_key="ks")

    assert "discord" in r1, "first critical send should fire"
    assert "discord" in r2, "second critical within cooldown must also fire"
    assert mock_client.post.call_count == 2


# ---------------------------------------------------------------------------
# PART B-3: Non-critical repeat within cooldown is suppressed
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_non_critical_repeat_suppressed_within_cooldown():
    """A second info send for the same key within the cooldown window is dropped."""
    clock = [0.0]
    alerter = _make_alerter(
        discord="https://discord.test/hook",
        cooldown=60.0,
        now_fn=lambda: clock[0],
    )

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_cls.return_value = mock_client

        r1 = await alerter.send("some_event", "T", "B", severity="info", dedup_key="x")
        clock[0] = 30.0
        r2 = await alerter.send("some_event", "T", "B", severity="info", dedup_key="x")

    assert "discord" in r1
    assert r2 == [], "non-critical within cooldown must be suppressed"
    assert mock_client.post.call_count == 1


# ---------------------------------------------------------------------------
# PART B-4: min_severity gates non-critical; critical always passes
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_min_severity_gates_below_threshold_non_critical():
    """A debug alert is dropped when min_severity is 'warning'."""
    alerter = Alerter(
        discord_webhook="https://discord.test/hook",
        telegram_bot_token=None,
        telegram_chat_id=None,
        cooldown_seconds=0.0,
        min_severity="warning",
    )

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=MagicMock(raise_for_status=MagicMock()))
        mock_cls.return_value = mock_client

        result = await alerter.send("evt", "T", "B", severity="debug")

    assert result == [], "debug below warning threshold should be dropped"
    mock_client.post.assert_not_called()


@pytest.mark.asyncio
async def test_min_severity_does_not_gate_critical():
    """A critical alert sends even when min_severity is set to 'critical' or higher."""
    alerter = Alerter(
        discord_webhook="https://discord.test/hook",
        telegram_bot_token=None,
        telegram_chat_id=None,
        cooldown_seconds=3600.0,
        min_severity="warning",
    )

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_cls.return_value = mock_client

        result = await alerter.send("kill_switch", "KS", "body", severity="critical")

    assert "discord" in result, "critical must not be blocked by min_severity"
    mock_client.post.assert_called_once()


# ---------------------------------------------------------------------------
# PART B-5: Hook safety — caller unaffected when notify raises synchronously
# ---------------------------------------------------------------------------

def test_kill_switch_caller_unaffected_when_notify_raises(monkeypatch):
    """If notify raises synchronously (e.g. asyncio.run re-raises), _trigger_kill_switch
    must still return normally and the kill-switch state must be set."""
    from core.risk_manager import RiskManager, RiskConfig

    async def bad_notify(*a, **kw):
        raise RuntimeError("simulated notify failure")

    monkeypatch.setattr(alerts_module, "_ALERTER", MagicMock())
    monkeypatch.setattr(alerts_module, "notify", bad_notify)

    rm = RiskManager(RiskConfig())
    # Must not raise, even though notify blows up.
    rm._trigger_kill_switch("test failure isolation")

    assert rm.state.kill_switch_triggered is True
    assert rm.state.kill_switch_reason == "test failure isolation"


def test_record_caller_unaffected_when_notify_raises(monkeypatch):
    """If notify raises inside _record, the position is still recorded and _record returns."""
    from core.directional.executor import Executor
    from core.directional.models import DirectionalOrder

    async def bad_notify(*a, **kw):
        raise RuntimeError("simulated notify failure")

    recorded = []

    class Store:
        def record_position(self, p):
            recorded.append(p)

    monkeypatch.setattr(alerts_module, "_ALERTER", MagicMock())
    monkeypatch.setattr(alerts_module, "notify", bad_notify)

    store = Store()
    ex = Executor(None, store)
    order = DirectionalOrder("mkt-safe", "YES", 0.4, 5, 2.0, "safe_compounder")
    pos = ex._record(order, "paper", None, None)

    assert pos is not None, "_record must return position even when notify raises"
    assert len(recorded) == 1, "position must still be recorded"
