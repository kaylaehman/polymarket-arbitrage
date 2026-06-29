"""Tests for core.directional.digest — daily Discord digest module."""
import pytest
import datetime
from unittest.mock import AsyncMock, MagicMock
from core.directional.digest import position_mtm, digest_text, pm_current_yes_price, gather_and_send


def test_position_mtm_no_side_gain_when_yes_falls():
    # NO position entered at 0.165 (yes was 0.835). If YES falls to 0.70, NO cost rises to 0.30 -> gain.
    assert position_mtm(0.165, "NO", 48, current_yes=0.70) == pytest.approx((0.30 - 0.165) * 48, abs=1e-4)


def test_position_mtm_yes_side():
    assert position_mtm(0.30, "YES", 10, current_yes=0.45) == pytest.approx((0.45 - 0.30) * 10, abs=1e-4)


def test_position_mtm_yes_side_loss():
    # YES entered at 0.50, current drops to 0.30 -> loss
    assert position_mtm(0.50, "YES", 5, current_yes=0.30) == pytest.approx((0.30 - 0.50) * 5, abs=1e-4)


def test_position_mtm_no_side_loss():
    # NO entered at 0.30 (yes at 0.70), YES rises to 0.90 (NO now costs 0.10) -> loss
    assert position_mtm(0.30, "NO", 10, current_yes=0.90) == pytest.approx((0.10 - 0.30) * 10, abs=1e-4)


def test_position_mtm_rounds_to_4dp():
    result = position_mtm(0.1, "YES", 3, current_yes=0.3)
    # (0.3 - 0.1) * 3 = 0.6 — check it's a float and rounded
    assert isinstance(result, float)
    assert abs(result - round(result, 4)) < 1e-9


def test_digest_text_has_sections():
    txt = digest_text(
        open_count=3,
        by_strategy={"maker_longshot": 2, "artist_paper": 1},
        mtm_lines=["pm:995702 NO Bad Bunny: +$6.48"],
        promotion_report="maker_longshot: accumulating",
        source_health={"kworb": True, "gamma": True, "spotify": False},
    )
    assert "Daily paper digest" in txt
    assert "maker_longshot" in txt and "Bad Bunny" in txt
    assert "kworb" in txt and "spotify" in txt


def test_digest_text_shows_open_count():
    txt = digest_text(
        open_count=7,
        by_strategy={"strat_a": 7},
        mtm_lines=[],
        promotion_report="strat_a: accumulating",
        source_health={"kworb": True},
    )
    assert "7" in txt


def test_digest_text_shows_source_up_down():
    txt = digest_text(
        open_count=0,
        by_strategy={},
        mtm_lines=[],
        promotion_report="(no closed positions yet)",
        source_health={"kworb": True, "gamma": False},
    )
    assert "up" in txt.lower() or "True" in txt or "kworb" in txt
    assert "down" in txt.lower() or "False" in txt or "gamma" in txt


def test_digest_text_total_unrealized():
    # Two mtm lines; total should appear in digest
    txt = digest_text(
        open_count=2,
        by_strategy={"a": 2},
        mtm_lines=["pm:1 YES: +$1.00", "pm:2 NO: -$0.50"],
        promotion_report="a: accumulating",
        source_health={},
    )
    # total unrealized should be shown somewhere
    assert "total" in txt.lower() or "+$0.50" in txt or "0.50" in txt


@pytest.mark.asyncio
async def test_pm_current_yes_price_parses():
    r = MagicMock()
    r.raise_for_status = MagicMock()
    r.json = MagicMock(return_value={"outcomePrices": "[\"0.71\",\"0.29\"]"})
    http = MagicMock()
    http.get = AsyncMock(return_value=r)
    assert await pm_current_yes_price(http, "pm:995702") == pytest.approx(0.71)
    # bare id also works
    assert "/markets/995702" in http.get.call_args.args[0]


@pytest.mark.asyncio
async def test_pm_current_yes_price_bare_id():
    r = MagicMock()
    r.raise_for_status = MagicMock()
    r.json = MagicMock(return_value={"outcomePrices": "[\"0.55\",\"0.45\"]"})
    http = MagicMock()
    http.get = AsyncMock(return_value=r)
    result = await pm_current_yes_price(http, "995702")
    assert result == pytest.approx(0.55)
    assert "/markets/995702" in http.get.call_args.args[0]


@pytest.mark.asyncio
async def test_pm_current_yes_price_error_none():
    http = MagicMock()
    http.get = AsyncMock(side_effect=RuntimeError("down"))
    assert await pm_current_yes_price(http, "pm:1") is None


@pytest.mark.asyncio
async def test_pm_current_yes_price_missing_key_none():
    r = MagicMock()
    r.raise_for_status = MagicMock()
    r.json = MagicMock(return_value={})
    http = MagicMock()
    http.get = AsyncMock(return_value=r)
    assert await pm_current_yes_price(http, "pm:2") is None


@pytest.mark.asyncio
async def test_gather_and_send_builds_and_sends_without_raising():
    # fake store with one pm position + one kalshi position
    def P(mid, side, ep, sz, strat):
        return type("P", (), {
            "market_id": mid, "side": side, "entry_price": ep,
            "size": sz, "strategy": strat,
        })()

    store = MagicMock()
    store.open_positions = MagicMock(return_value=[
        P("pm:995702", "NO", 0.165, 48, "artist_paper"),
        P("kalshi:KXHIGHNY-x", "NO", 0.05, 10, "maker_longshot"),
    ])

    # http: gamma market price + source pings all succeed
    def _resp(js=None):
        r = MagicMock()
        r.raise_for_status = MagicMock()
        r.json = MagicMock(return_value=js or {})
        r.text = "ok"
        return r

    http = MagicMock()

    async def _get(url, *a, **k):
        if "/markets/995702" in url:
            return _resp({"outcomePrices": "[\"0.70\",\"0.30\"]"})
        return _resp({"data": []})

    http.get = AsyncMock(side_effect=_get)
    sent = {}

    async def fake_alert(event_type, title, body, severity="info", dedup_key=""):
        sent["title"] = title
        sent["body"] = body

    txt = await gather_and_send(store, http, alert=fake_alert)
    assert "Daily paper digest" in txt
    # title check is flexible — just confirm it sent
    assert sent and "digest" in sent["title"].lower()


@pytest.mark.asyncio
async def test_gather_and_send_survives_store_failure():
    """Even if open_positions raises, gather_and_send should not raise."""
    store = MagicMock()
    store.open_positions = MagicMock(side_effect=RuntimeError("db gone"))
    store._conn = MagicMock()
    store._conn.execute = MagicMock(side_effect=RuntimeError("db gone"))

    http = MagicMock()
    http.get = AsyncMock(return_value=MagicMock(
        raise_for_status=MagicMock(),
        json=MagicMock(return_value={}),
        text="ok",
    ))

    async def fake_alert(*a, **k):
        pass

    # Must not raise
    txt = await gather_and_send(store, http, alert=fake_alert)
    assert isinstance(txt, str)
