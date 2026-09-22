"""
Unit tests for infrastructure/binance/client.py (Module 4).

All HTTP is mocked via `aioresponses` -- no real network call ever
reaches Binance (and this sandbox's egress rules would block it anyway).
URLs are matched with a regex (path prefix + `.*`) rather than an exact
query string: aioresponses matches the query string literally when one
is present in the registered URL, which would make these tests depend on
Python's dict-ordering of `params` -- matching on path only is more
robust and is exactly what these tests should care about.

Run with:
    pytest tests/unit/test_binance_client.py -v
"""

import hashlib
import hmac
import re
import urllib.parse
from datetime import datetime, timezone

import pytest
from aioresponses import aioresponses

from config.schema import APIConfig, PlatformConfig
from infrastructure.binance.client import BinanceFuturesClient
from system.exceptions import BinanceAPIError, BinanceDataError, BinanceRateLimitError

BASE = "https://fapi.binance.com"


def _url_pattern(path: str) -> re.Pattern:
    return re.compile(rf"^{re.escape(BASE + path)}.*$")


SAMPLE_CANDLE_ROW = [
    1717200000000, "50000.00", "50500.00", "49800.00", "50250.00",
    "1234.567", 1717200899999, "62012345.67", 4321, "600.123", "30123456.78", "0",
]

SAMPLE_EXCHANGE_INFO = {
    "symbols": [
        {
            "symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT",
            "status": "TRADING", "contractType": "PERPETUAL",
            "pricePrecision": 2, "quantityPrecision": 3,
            "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                {"filterType": "LOT_SIZE", "stepSize": "0.001"},
                {"filterType": "MIN_NOTIONAL", "notional": "5.0"},
            ],
        },
        {
            "symbol": "BTCUSD_PERP", "baseAsset": "BTC", "quoteAsset": "USD",  # COIN-M -- must be filtered out
            "status": "TRADING", "contractType": "PERPETUAL",
            "pricePrecision": 1, "quantityPrecision": 0, "filters": [],
        },
        {
            "symbol": "SOMEUSDT_240329", "baseAsset": "SOME", "quoteAsset": "USDT",  # dated future -- filtered out
            "status": "TRADING", "contractType": "CURRENT_QUARTER", "filters": [],
        },
    ]
}


@pytest.fixture
def config() -> PlatformConfig:
    return PlatformConfig()


@pytest.mark.asyncio
async def test_get_klines_parses_candles_correctly(config):
    with aioresponses() as mocked:
        mocked.get(_url_pattern("/fapi/v1/klines"), payload=[SAMPLE_CANDLE_ROW])
        async with BinanceFuturesClient(config=config) as client:
            candles = await client.get_klines("BTCUSDT", "15m", limit=200)

    assert len(candles) == 1
    candle = candles[0]
    assert candle.open == 50000.00
    assert candle.close == 50250.00
    assert candle.num_trades == 4321
    assert candle.open_time.tzinfo is not None


# ── get_klines start_time/end_time (Module 21) ──────────────────────────


@pytest.mark.asyncio
async def test_get_klines_omits_time_params_when_not_provided(config):
    """Backward compatibility: calling exactly as before this parameter existed sends no time params at all."""
    with aioresponses() as mocked:
        mocked.get(_url_pattern("/fapi/v1/klines"), payload=[SAMPLE_CANDLE_ROW])
        async with BinanceFuturesClient(config=config) as client:
            await client.get_klines("BTCUSDT", "15m", limit=200)

    request_call = next(iter(mocked.requests.values()))[0]
    sent_params = request_call.kwargs["params"]
    assert "startTime" not in sent_params
    assert "endTime" not in sent_params


@pytest.mark.asyncio
async def test_get_klines_sends_start_and_end_time_as_millisecond_timestamps(config):
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    end = datetime(2026, 6, 2, tzinfo=timezone.utc)

    with aioresponses() as mocked:
        mocked.get(_url_pattern("/fapi/v1/klines"), payload=[SAMPLE_CANDLE_ROW])
        async with BinanceFuturesClient(config=config) as client:
            await client.get_klines("BTCUSDT", "1h", start_time=start, end_time=end)

    request_call = next(iter(mocked.requests.values()))[0]
    sent_params = request_call.kwargs["params"]
    assert sent_params["startTime"] == int(start.timestamp() * 1000)
    assert sent_params["endTime"] == int(end.timestamp() * 1000)


@pytest.mark.asyncio
async def test_get_klines_accepts_start_time_without_end_time(config):
    with aioresponses() as mocked:
        mocked.get(_url_pattern("/fapi/v1/klines"), payload=[SAMPLE_CANDLE_ROW])
        async with BinanceFuturesClient(config=config) as client:
            await client.get_klines("BTCUSDT", "1h", start_time=datetime(2026, 6, 1, tzinfo=timezone.utc))

    request_call = next(iter(mocked.requests.values()))[0]
    sent_params = request_call.kwargs["params"]
    assert "startTime" in sent_params
    assert "endTime" not in sent_params


@pytest.mark.asyncio
async def test_get_exchange_info_filters_to_usdt_perpetuals_only(config):
    with aioresponses() as mocked:
        mocked.get(_url_pattern("/fapi/v1/exchangeInfo"), payload=SAMPLE_EXCHANGE_INFO)
        async with BinanceFuturesClient(config=config) as client:
            symbols = await client.get_exchange_info()

    assert [s.symbol for s in symbols] == ["BTCUSDT"]
    assert symbols[0].tick_size == 0.10
    assert symbols[0].step_size == 0.001
    assert symbols[0].min_notional_usdt == 5.0
    assert symbols[0].is_trading is True


@pytest.mark.asyncio
async def test_get_ticker_24hr_single_symbol(config):
    payload = {
        "symbol": "BTCUSDT", "lastPrice": "50250.00", "priceChangePercent": "2.5",
        "quoteVolume": "123456789.0", "highPrice": "51000.0", "lowPrice": "49000.0",
        "weightedAvgPrice": "50100.0",
    }
    with aioresponses() as mocked:
        mocked.get(_url_pattern("/fapi/v1/ticker/24hr"), payload=payload)
        async with BinanceFuturesClient(config=config) as client:
            tickers = await client.get_ticker_24hr("BTCUSDT")

    assert len(tickers) == 1
    assert tickers[0].symbol == "BTCUSDT"
    assert tickers[0].price_change_percent == 2.5


@pytest.mark.asyncio
async def test_get_ticker_24hr_all_symbols_returns_a_list(config):
    payload = [
        {"symbol": "BTCUSDT", "lastPrice": "1", "priceChangePercent": "1",
         "quoteVolume": "1", "highPrice": "1", "lowPrice": "1", "weightedAvgPrice": "1"},
        {"symbol": "ETHUSDT", "lastPrice": "1", "priceChangePercent": "1",
         "quoteVolume": "1", "highPrice": "1", "lowPrice": "1", "weightedAvgPrice": "1"},
    ]
    with aioresponses() as mocked:
        mocked.get(_url_pattern("/fapi/v1/ticker/24hr"), payload=payload)
        async with BinanceFuturesClient(config=config) as client:
            tickers = await client.get_ticker_24hr()

    assert {t.symbol for t in tickers} == {"BTCUSDT", "ETHUSDT"}


@pytest.mark.asyncio
async def test_get_funding_rate_parses_correctly(config):
    payload = {
        "symbol": "BTCUSDT", "markPrice": "50260.5", "lastFundingRate": "0.0001",
        "nextFundingTime": 1717228800000,
    }
    with aioresponses() as mocked:
        mocked.get(_url_pattern("/fapi/v1/premiumIndex"), payload=payload)
        async with BinanceFuturesClient(config=config) as client:
            funding = await client.get_funding_rate("BTCUSDT")

    assert funding.funding_rate == 0.0001
    assert funding.mark_price == 50260.5
    assert funding.next_funding_time.tzinfo is not None


@pytest.mark.asyncio
async def test_get_open_interest_parses_correctly(config):
    payload = {"symbol": "BTCUSDT", "openInterest": "45123.456", "time": 1717228800000}
    with aioresponses() as mocked:
        mocked.get(_url_pattern("/fapi/v1/openInterest"), payload=payload)
        async with BinanceFuturesClient(config=config) as client:
            open_interest = await client.get_open_interest("BTCUSDT")

    assert open_interest.open_interest == 45123.456


@pytest.mark.asyncio
async def test_get_book_ticker_parses_and_computes_spread(config):
    payload = {"symbol": "BTCUSDT", "bidPrice": "49999", "bidQty": "1.0", "askPrice": "50001", "askQty": "1.0"}
    with aioresponses() as mocked:
        mocked.get(_url_pattern("/fapi/v1/ticker/bookTicker"), payload=payload)
        async with BinanceFuturesClient(config=config) as client:
            book = await client.get_book_ticker("BTCUSDT")

    assert book.spread_percent == pytest.approx(0.004, abs=1e-6)


@pytest.mark.asyncio
async def test_book_ticker_is_never_cached(config):
    payload = {"symbol": "BTCUSDT", "bidPrice": "1", "bidQty": "1", "askPrice": "2", "askQty": "1"}
    with aioresponses() as mocked:
        mocked.get(_url_pattern("/fapi/v1/ticker/bookTicker"), payload=payload)
        mocked.get(_url_pattern("/fapi/v1/ticker/bookTicker"), payload=payload)
        async with BinanceFuturesClient(config=config) as client:
            await client.get_book_ticker("BTCUSDT")
            await client.get_book_ticker("BTCUSDT")
        # both registered responses must have been consumed (no leftovers) -> two real requests were made
        total_calls = sum(len(calls) for calls in mocked.requests.values())
        assert total_calls == 2


@pytest.mark.asyncio
async def test_klines_are_cached_within_ttl(config):
    with aioresponses() as mocked:
        mocked.get(_url_pattern("/fapi/v1/klines"), payload=[SAMPLE_CANDLE_ROW])
        async with BinanceFuturesClient(config=config) as client:
            first = await client.get_klines("BTCUSDT", "15m", limit=200)
            second = await client.get_klines("BTCUSDT", "15m", limit=200)

    assert first == second
    total_calls = sum(len(calls) for calls in mocked.requests.values())
    assert total_calls == 1  # second call served entirely from cache, no second HTTP request


@pytest.mark.asyncio
async def test_used_weight_header_updates_rate_limiter(config):
    payload = {"symbol": "BTCUSDT", "openInterest": "1.0", "time": 1}
    with aioresponses() as mocked:
        mocked.get(_url_pattern("/fapi/v1/openInterest"), payload=payload, headers={"X-MBX-USED-WEIGHT-1M": "42"})
        async with BinanceFuturesClient(config=config) as client:
            await client.get_open_interest("BTCUSDT")
            assert client.rate_limiter.last_external_usage == 42


# ── check_health (Module 20) ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_health_returns_true_on_successful_ping(config):
    with aioresponses() as mocked:
        mocked.get(_url_pattern("/fapi/v1/ping"), payload={})
        async with BinanceFuturesClient(config=config) as client:
            healthy = await client.check_health()

    assert healthy is True


@pytest.mark.asyncio
async def test_check_health_returns_false_on_api_error(config):
    with aioresponses() as mocked:
        mocked.get(_url_pattern("/fapi/v1/ping"), status=500, payload={"code": -1000, "msg": "boom"})
        async with BinanceFuturesClient(config=config) as client:
            healthy = await client.check_health()

    assert healthy is False


@pytest.mark.asyncio
async def test_check_health_is_never_cached(config):
    """Two consecutive health checks must both hit the network -- a stale cached 'healthy' would defeat the point."""
    with aioresponses() as mocked:
        mocked.get(_url_pattern("/fapi/v1/ping"), payload={})
        mocked.get(_url_pattern("/fapi/v1/ping"), payload={})
        async with BinanceFuturesClient(config=config) as client:
            await client.check_health()
            await client.check_health()

    total_calls = sum(len(calls) for calls in mocked.requests.values())
    assert total_calls == 2


@pytest.mark.asyncio
async def test_http_429_retries_then_succeeds(config, monkeypatch):
    import asyncio as asyncio_module

    async def fast_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio_module, "sleep", fast_sleep)

    payload = {"symbol": "BTCUSDT", "openInterest": "1.0", "time": 1}
    with aioresponses() as mocked:
        mocked.get(_url_pattern("/fapi/v1/openInterest"), status=429, headers={"Retry-After": "1"})
        mocked.get(_url_pattern("/fapi/v1/openInterest"), payload=payload)
        async with BinanceFuturesClient(config=config) as client:
            result = await client.get_open_interest("BTCUSDT")

    assert result.open_interest == 1.0


@pytest.mark.asyncio
async def test_http_429_raises_after_retries_exhausted(config, monkeypatch):
    import asyncio as asyncio_module

    async def fast_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio_module, "sleep", fast_sleep)

    with aioresponses() as mocked:
        for _ in range(config.api.max_retries + 1):
            mocked.get(_url_pattern("/fapi/v1/openInterest"), status=429, headers={"Retry-After": "1"})
        async with BinanceFuturesClient(config=config) as client:
            with pytest.raises(BinanceRateLimitError):
                await client.get_open_interest("BTCUSDT")


@pytest.mark.asyncio
async def test_http_400_raises_binance_data_error_without_retrying(config):
    with aioresponses() as mocked:
        mocked.get(
            _url_pattern("/fapi/v1/openInterest"),
            status=400,
            payload={"code": -1121, "msg": "Invalid symbol."},
        )
        async with BinanceFuturesClient(config=config) as client:
            with pytest.raises(BinanceDataError):
                await client.get_open_interest("BTCUSDT")

        # exactly one request -- BinanceDataError is not in retryable_exceptions
        total_calls = sum(len(calls) for calls in mocked.requests.values())
        assert total_calls == 1


@pytest.mark.asyncio
async def test_calling_without_context_manager_raises_runtime_error(config):
    client = BinanceFuturesClient(config=config)
    with pytest.raises(RuntimeError):
        await client.get_open_interest("BTCUSDT")


# ─────────────────────────────────────────────────────────────────────────
# SIGNED / TRADING ENDPOINTS (autonomous trading pivot)
# ─────────────────────────────────────────────────────────────────────────


@pytest.fixture
def signed_config() -> PlatformConfig:
    return PlatformConfig(api=APIConfig(binance_api_key="test-key", binance_api_secret="test-secret"))


def test_sign_params_produces_a_verifiable_hmac_sha256_signature(signed_config):
    """
    Calls `_sign_params()` directly rather than inspecting a mocked
    request's URL -- aioresponses re-sorts a matched request's query
    params alphabetically in its own internal bookkeeping (verified by
    inspecting the raw `aiohttp.ClientRequest` directly), which does NOT
    match what Binance actually receives on the wire. Testing the
    signing function in isolation sidesteps that entirely.
    """
    client = BinanceFuturesClient(config=signed_config)

    signed = client._sign_params({"symbol": "BTCUSDT", "side": "BUY"})

    assert "timestamp" in signed and "recvWindow" in signed and "signature" in signed
    unsigned_query = urllib.parse.urlencode(
        {k: v for k, v in signed.items() if k != "signature"}, doseq=True
    )
    expected_signature = hmac.new(
        b"test-secret", unsigned_query.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    assert signed["signature"] == expected_signature


@pytest.mark.asyncio
async def test_signed_endpoint_raises_without_configured_credentials(config):
    """`config` (module fixture) has no API key/secret -- signed calls must fail fast, not send an unsigned request."""
    async with BinanceFuturesClient(config=config) as client:
        with pytest.raises(BinanceAPIError):
            await client.get_account_balance()


@pytest.mark.asyncio
async def test_get_account_balance_parses_every_asset_and_sends_api_key_header(signed_config):
    with aioresponses() as mocked:
        mocked.get(
            _url_pattern("/fapi/v2/balance"),
            payload=[
                {"asset": "USDT", "balance": "12.34", "availableBalance": "10.00"},
                {"asset": "BNB", "balance": "0.5", "availableBalance": "0.5"},
            ],
        )
        async with BinanceFuturesClient(config=signed_config) as client:
            balances = await client.get_account_balance()

    assert len(balances) == 2
    usdt = next(b for b in balances if b.asset == "USDT")
    assert usdt.balance == pytest.approx(12.34)
    assert usdt.available_balance == pytest.approx(10.00)

    sent_headers = next(iter(mocked.requests.values()))[0].kwargs["headers"]
    assert sent_headers["X-MBX-APIKEY"] == "test-key"


@pytest.mark.asyncio
async def test_get_position_risk_returns_none_when_symbol_row_is_absent(signed_config):
    with aioresponses() as mocked:
        mocked.get(_url_pattern("/fapi/v2/positionRisk"), payload=[])
        async with BinanceFuturesClient(config=signed_config) as client:
            result = await client.get_position_risk("BTCUSDT")

    assert result is None


@pytest.mark.asyncio
async def test_get_position_risk_parses_the_row(signed_config):
    with aioresponses() as mocked:
        mocked.get(
            _url_pattern("/fapi/v2/positionRisk"),
            payload=[{
                "symbol": "BTCUSDT", "positionAmt": "0.010", "entryPrice": "50000.0",
                "markPrice": "50500.0", "unRealizedProfit": "5.0", "leverage": "10",
            }],
        )
        async with BinanceFuturesClient(config=signed_config) as client:
            result = await client.get_position_risk("BTCUSDT")

    assert result.position_amount == pytest.approx(0.01)
    assert result.is_flat is False
    assert result.leverage == 10


@pytest.mark.asyncio
async def test_set_margin_type_swallows_dash_4046_already_set_error(signed_config):
    """-4046 means the symbol is already in the requested margin type -- expected on every subsequent open_position() call, not a real failure."""
    with aioresponses() as mocked:
        mocked.post(
            _url_pattern("/fapi/v1/marginType"),
            status=400,
            payload={"code": -4046, "msg": "No need to change margin type."},
        )
        async with BinanceFuturesClient(config=signed_config) as client:
            await client.set_margin_type("BTCUSDT", "ISOLATED")  # must not raise


@pytest.mark.asyncio
async def test_set_margin_type_reraises_other_binance_errors(signed_config):
    with aioresponses() as mocked:
        mocked.post(
            _url_pattern("/fapi/v1/marginType"),
            status=400,
            payload={"code": -1121, "msg": "Invalid symbol."},
        )
        async with BinanceFuturesClient(config=signed_config) as client:
            with pytest.raises(BinanceDataError):
                await client.set_margin_type("BTCUSDT", "ISOLATED")


@pytest.mark.asyncio
async def test_place_order_market_entry_sends_quantity_and_no_stop_price(signed_config):
    with aioresponses() as mocked:
        mocked.post(
            _url_pattern("/fapi/v1/order"),
            payload={
                "orderId": 111, "clientOrderId": "colde-1-entry", "symbol": "BTCUSDT",
                "status": "FILLED", "avgPrice": "50000.0", "executedQty": "0.01",
            },
        )
        async with BinanceFuturesClient(config=signed_config) as client:
            result = await client.place_order(
                "BTCUSDT", "BUY", "MARKET", quantity=0.01, client_order_id="colde-1-entry"
            )

    assert result.order_id == 111
    assert result.avg_price == pytest.approx(50000.0)
    assert result.executed_qty == pytest.approx(0.01)


@pytest.mark.asyncio
async def test_place_order_stop_market_uses_close_position_not_quantity(signed_config):
    with aioresponses() as mocked:
        mocked.post(
            _url_pattern("/fapi/v1/order"),
            payload={
                "orderId": 112, "clientOrderId": "colde-1-sl", "symbol": "BTCUSDT",
                "status": "NEW", "avgPrice": "0", "executedQty": "0",
            },
        )
        async with BinanceFuturesClient(config=signed_config) as client:
            result = await client.place_order(
                "BTCUSDT", "SELL", "STOP_MARKET", stop_price=49000.0,
                close_position=True, client_order_id="colde-1-sl",
            )

    assert result.status == "NEW"
    assert result.avg_price == 0.0  # not filled yet -- a resting conditional order


@pytest.mark.asyncio
async def test_get_order_returns_none_for_dash_2013_order_does_not_exist(signed_config):
    with aioresponses() as mocked:
        mocked.get(
            _url_pattern("/fapi/v1/order"),
            status=400,
            payload={"code": -2013, "msg": "Order does not exist."},
        )
        async with BinanceFuturesClient(config=signed_config) as client:
            result = await client.get_order("BTCUSDT", 999)

    assert result is None


@pytest.mark.asyncio
async def test_cancel_all_open_orders_sends_a_delete_request(signed_config):
    with aioresponses() as mocked:
        mocked.delete(_url_pattern("/fapi/v1/allOpenOrders"), payload={"code": 200, "msg": "success"})
        async with BinanceFuturesClient(config=signed_config) as client:
            await client.cancel_all_open_orders("BTCUSDT")  # must not raise

    total_calls = sum(len(calls) for calls in mocked.requests.values())
    assert total_calls == 1


@pytest.mark.asyncio
async def test_signed_write_calls_send_once_directly_never_the_retry_wrapper(signed_config):
    """
    Design decision: `_signed_write()` must never go through
    `_send_with_retry()` -- a blind retry after a network failure risks
    placing the same order twice. Verified here by substituting both
    seams and checking which one actually got invoked, since an
    end-to-end HTTP-exception test can't distinguish "no retry happened"
    from "aioresponses ran out of registered mocks".
    """
    async with BinanceFuturesClient(config=signed_config) as client:
        calls = {"send_once": 0, "send_with_retry": 0}

        async def fake_send_once(method, path, params, *, headers=None):
            calls["send_once"] += 1
            return {"orderId": 1, "clientOrderId": "x", "symbol": "BTCUSDT", "status": "NEW", "avgPrice": "0", "executedQty": "0"}

        async def fake_send_with_retry(*args, **kwargs):
            calls["send_with_retry"] += 1
            return await fake_send_once(*args, **kwargs)

        client._send_once = fake_send_once
        client._send_with_retry = fake_send_with_retry

        await client.place_order("BTCUSDT", "BUY", "MARKET", quantity=0.01)

    assert calls["send_once"] == 1
    assert calls["send_with_retry"] == 0
