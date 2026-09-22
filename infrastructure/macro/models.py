"""
infrastructure/macro/models.py

Typed response models for the two external, non-Binance data sources
ported from sinyal_kanali_2's MacroClient: CoinGecko's free `/global`
endpoint (BTC/USDT dominance) and Yahoo Finance's public chart API (DXY).

Frozen, like infrastructure/binance/models.py -- these represent a fact
observed at a point in time and are never mutated after parsing. Both are
RAW readings only; neither classifies a trend itself (Rising/Falling,
Bullish/Bearish) -- that is `MacroIntelligenceEngine`'s job, same
raw-data-vs-classification split as `BitcoinIntelligenceEngine`'s
`_classify_trend`. CoinGecko's `/global` in particular is a snapshot, not
a series -- there is no "trend" to extract at the parsing boundary even in
principle; a trend only exists by comparing this snapshot against a
previously-stored one, which needs engine-level state, not client-level.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class DominanceSnapshot:
    """
    BTC and USDT's current share of total crypto market cap, as a
    percentage (e.g. btc_pct=52.3 means BTC is 52.3% of all crypto market
    cap right now). Source: CoinGecko `/api/v3/global`, no key required.
    """

    btc_pct: float
    usdt_pct: float
    fetched_at: datetime


@dataclass(frozen=True)
class DxySnapshot:
    """
    US Dollar Index (DXY), daily timeframe: latest close plus its own
    20-day and 50-day simple moving averages -- enough for the engine to
    classify Bullish/Bearish/Mixed the same way `BitcoinIntelligenceEngine
    ._classify_trend` reads price vs EMA20 vs EMA50, just SMA-based here
    to match what sinyal_kanali_2 already validated. Source: Yahoo
    Finance's public chart API, ticker DX-Y.NYB, no key required.
    """

    price: float
    sma20: float
    sma50: float
    fetched_at: datetime
