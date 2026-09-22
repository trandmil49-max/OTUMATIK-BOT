"""
Unit tests for infrastructure/macro/client.py.

Same approach as tests/unit/test_binance_client.py: all HTTP mocked via
`aioresponses`, no real network call ever reaches CoinGecko or Yahoo
Finance (this sandbox's egress rules would block both anyway -- see
infrastructure/macro/client.py's module docstring).

Run with:
    pytest tests/unit/test_macro_client.py -v
"""

import re

import pytest
from aioresponses import aioresponses

from config.schema import PlatformConfig
from infrastructure.macro.client import MacroDataClient

COINGECKO_URL = "https://api.coingecko.com/api/v3/global"
YAHOO_URL_PATTERN = re.compile(r"^https://query1\.finance\.yahoo\.com/v8/finance/chart/DX-Y\.NYB.*$")

SAMPLE_COINGECKO_PAYLOAD = {
    "data": {
        "market_cap_percentage": {
            "btc": 52.34, "eth": 12.1, "usdt": 4.87, "bnb": 3.2,
        },
    },
}


def _yahoo_payload(closes: list) -> dict:
    return {"chart": {"result": [{"indicators": {"quote": [{"close": closes}]}}], "error": None}}


@pytest.fixture
def config() -> PlatformConfig:
    return PlatformConfig()


# ── get_dominance() ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_dominance_parses_btc_and_usdt_pct(config):
    with aioresponses() as mocked:
        mocked.get(COINGECKO_URL, payload=SAMPLE_COINGECKO_PAYLOAD)
        async with MacroDataClient(config=config) as client:
            snapshot = await client.get_dominance()

    assert snapshot is not None
    assert snapshot.btc_pct == 52.34
    assert snapshot.usdt_pct == 4.87
    assert snapshot.fetched_at.tzinfo is not None


@pytest.mark.asyncio
async def test_get_dominance_returns_none_when_keys_missing(config):
    with aioresponses() as mocked:
        mocked.get(COINGECKO_URL, payload={"data": {"market_cap_percentage": {"eth": 12.1}}})  # no btc/usdt
        async with MacroDataClient(config=config) as client:
            snapshot = await client.get_dominance()

    assert snapshot is None


@pytest.mark.asyncio
async def test_get_dominance_returns_none_on_http_error(config):
    with aioresponses() as mocked:
        mocked.get(COINGECKO_URL, status=500)
        async with MacroDataClient(config=config) as client:
            snapshot = await client.get_dominance()

    assert snapshot is None


@pytest.mark.asyncio
async def test_get_dominance_returns_none_on_malformed_json(config):
    with aioresponses() as mocked:
        mocked.get(COINGECKO_URL, payload={"data": {}})  # missing market_cap_percentage entirely
        async with MacroDataClient(config=config) as client:
            snapshot = await client.get_dominance()

    assert snapshot is None


# ── get_dxy_snapshot() ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_dxy_snapshot_computes_sma20_and_sma50(config):
    # 50 ascending closes: 51.0, 52.0, ..., 100.0 -- SMA20 = mean of the last
    # 20 (81..100 -> 90.5), SMA50 = mean of all 50 (51..100 -> 75.5).
    closes = [float(51 + i) for i in range(50)]
    with aioresponses() as mocked:
        mocked.get(YAHOO_URL_PATTERN, payload=_yahoo_payload(closes))
        async with MacroDataClient(config=config) as client:
            snapshot = await client.get_dxy_snapshot()

    assert snapshot is not None
    assert snapshot.price == 100.0
    assert snapshot.sma20 == pytest.approx(90.5)
    assert snapshot.sma50 == pytest.approx(75.5)


@pytest.mark.asyncio
async def test_get_dxy_snapshot_ignores_null_closes_from_non_trading_days(config):
    # Yahoo pads weekends/holidays with null closes -- must be filtered before counting/averaging.
    closes = [None, None] + [float(51 + i) for i in range(50)]
    with aioresponses() as mocked:
        mocked.get(YAHOO_URL_PATTERN, payload=_yahoo_payload(closes))
        async with MacroDataClient(config=config) as client:
            snapshot = await client.get_dxy_snapshot()

    assert snapshot is not None
    assert snapshot.price == 100.0  # the nulls must not shift which value is "latest"


@pytest.mark.asyncio
async def test_get_dxy_snapshot_returns_none_with_fewer_than_50_closes(config):
    closes = [float(51 + i) for i in range(49)]  # one short of the SMA50 floor
    with aioresponses() as mocked:
        mocked.get(YAHOO_URL_PATTERN, payload=_yahoo_payload(closes))
        async with MacroDataClient(config=config) as client:
            snapshot = await client.get_dxy_snapshot()

    assert snapshot is None


@pytest.mark.asyncio
async def test_get_dxy_snapshot_returns_none_on_http_error(config):
    with aioresponses() as mocked:
        mocked.get(YAHOO_URL_PATTERN, status=503)
        async with MacroDataClient(config=config) as client:
            snapshot = await client.get_dxy_snapshot()

    assert snapshot is None


@pytest.mark.asyncio
async def test_get_dxy_snapshot_sends_a_browser_user_agent(config):
    """Yahoo's public chart endpoint 403s a default client User-Agent on some networks (ported finding)."""
    closes = [float(51 + i) for i in range(50)]
    with aioresponses() as mocked:
        mocked.get(YAHOO_URL_PATTERN, payload=_yahoo_payload(closes))
        async with MacroDataClient(config=config) as client:
            await client.get_dxy_snapshot()

    request_call = next(iter(mocked.requests.values()))[0]
    sent_headers = request_call.kwargs["headers"]
    assert "Mozilla" in sent_headers["User-Agent"]
