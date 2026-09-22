"""
infrastructure/macro/client.py

MacroDataClient: BTC/USDT dominance (CoinGecko) and DXY (Yahoo Finance),
ported from sinyal_kanali_2's MacroClient at the platform owner's explicit
request -- "Bitcoin analizi yaparken BTC'ye bakacaksın, USDT'ye bakacaksın"
(BTC dominance, USDT dominance) plus DXY, which sinyal_kanali_2 already
had wired into its own scoring.

Both endpoints are free, public, and require no API key -- but neither is
an official Binance endpoint, and NEITHER COULD BE EXERCISED FROM THIS
DEVELOPMENT SANDBOX (network access here is restricted to a fixed
allowlist of package/source-control domains; api.coingecko.com and
query1.finance.yahoo.com are not on it). This is the exact same
limitation sinyal_kanali_2's own author documented for this same code.
Written defensively so that mirrors that file's proven approach: ANY
failure (blocked, timed out, response shape changed, rate-limited,
anything) degrades to `None`, never raises, and never blocks signal
generation -- worst case, a macro input goes quiet and everything else
keeps working. Check logs after deploying to confirm these are actually
reaching CoinGecko/Yahoo from Railway's network (a sandbox limitation
here, not necessarily one there).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

import aiohttp

from config.loader import get_config
from config.schema import PlatformConfig
from infrastructure.macro.models import DominanceSnapshot, DxySnapshot
from system.logging_setup import get_logger

_logger = get_logger("trading")

_COINGECKO_GLOBAL_URL = "https://api.coingecko.com/api/v3/global"
_YAHOO_CHART_URL_TEMPLATE = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
_DXY_YAHOO_TICKER = "DX-Y.NYB"
# Yahoo's public chart endpoint 403s a default aiohttp/python User-Agent on
# some networks; sinyal_kanali_2 found a browser UA avoids that. Ported as-is.
_YAHOO_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json",
}
_DXY_MIN_CLOSES_REQUIRED = 50  # need a full SMA50 -- same floor sinyal_kanali_2 used


class MacroDataClient:
    """
    Async context manager, same shape as `BinanceFuturesClient`:

        async with MacroDataClient() as client:
            dominance = await client.get_dominance()

    No rate limiter/cache/retry machinery from the Binance client is
    reused here on purpose: those are sized around Binance's specific
    per-minute weight budget, which does not apply to these two
    unrelated, low-frequency (once-per-scan-cycle at most) sources.
    """

    def __init__(self, config: Optional[PlatformConfig] = None, session: Optional[aiohttp.ClientSession] = None) -> None:
        self._config = config or get_config()
        self._timeout = aiohttp.ClientTimeout(total=self._config.api.request_timeout_seconds)
        self._session = session
        self._owns_session = session is None

    async def __aenter__(self) -> "MacroDataClient":
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()

    async def get_dominance(self) -> Optional[DominanceSnapshot]:
        """BTC and USDT dominance from CoinGecko's `/global`. `None` on any failure -- never raises."""
        try:
            payload = await self._get_json(_COINGECKO_GLOBAL_URL)
            pct = payload["data"]["market_cap_percentage"]
            btc_pct = pct.get("btc")
            usdt_pct = pct.get("usdt")
            if btc_pct is None or usdt_pct is None:
                _logger.warning("CoinGecko /global response missing btc/usdt market_cap_percentage keys")
                return None
            return DominanceSnapshot(btc_pct=float(btc_pct), usdt_pct=float(usdt_pct), fetched_at=datetime.now(timezone.utc))
        except Exception:
            _logger.exception("CoinGecko dominance fetch failed (non-fatal, ignored)")
            return None

    async def get_dxy_snapshot(self) -> Optional[DxySnapshot]:
        """Latest DXY daily close plus its own SMA20/SMA50, from Yahoo Finance. `None` on any failure -- never raises."""
        try:
            payload = await self._get_json(
                _YAHOO_CHART_URL_TEMPLATE.format(ticker=_DXY_YAHOO_TICKER),
                params={"interval": "1d", "range": "3mo"},
                headers=_YAHOO_HEADERS,
            )
            closes = payload["chart"]["result"][0]["indicators"]["quote"][0]["close"]
            closes = [c for c in closes if c is not None]
            if len(closes) < _DXY_MIN_CLOSES_REQUIRED:
                _logger.warning("DXY fetch returned only %d closes (need %d for SMA50)", len(closes), _DXY_MIN_CLOSES_REQUIRED)
                return None
            sma20 = sum(closes[-20:]) / 20
            sma50 = sum(closes[-50:]) / 50
            return DxySnapshot(price=closes[-1], sma20=sma20, sma50=sma50, fetched_at=datetime.now(timezone.utc))
        except Exception:
            _logger.exception("DXY fetch failed (non-fatal, ignored)")
            return None

    async def _get_json(
        self, url: str, params: Optional[dict[str, str]] = None, headers: Optional[dict[str, str]] = None
    ) -> Any:
        assert self._session is not None, "MacroDataClient must be used as an async context manager"
        async with self._session.get(url, params=params, headers=headers) as response:
            response.raise_for_status()
            return await response.json(content_type=None)  # Yahoo/CoinGecko don't always send application/json
