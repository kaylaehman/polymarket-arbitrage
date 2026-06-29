import pytest
from unittest.mock import AsyncMock, MagicMock
from music_intel.sources.luminate import LuminateSource


@pytest.mark.asyncio
async def test_luminate_disabled_without_key_makes_no_call():
    http = MagicMock(); http.get = AsyncMock()
    src = LuminateSource(http=http, api_key=None)
    assert src.enabled is False
    assert await src.fetch("hot100") == []
    http.get.assert_not_called()


@pytest.mark.asyncio
async def test_luminate_enabled_with_key_stub_returns_empty():
    src = LuminateSource(http=MagicMock(), api_key="dummy")
    assert src.enabled is True
    assert src.trust_tier == 3
    assert await src.fetch("hot100") == []   # stub, no live behavior asserted
