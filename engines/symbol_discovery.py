"""
engines/symbol_discovery.py

Symbol Discovery Engine (SRS Part 7 COIN DISCOVERY ENGINE, Module 5).

Orchestrates Module 4 (`BinanceFuturesClient`) and Module 3
(`CoinRepository`) to keep the `coins` table in sync with what Binance
actually lists: every USDT-M perpetual currently on the exchange gets
upserted (SRS Part 7: "The bot must automatically discover new coins ...
The bot must detect delisted coins"). Symbols Binance no longer reports
are marked inactive, never deleted (SRS: "Do not delete historical
data" -- a delisted coin's `coin_statistics`/`trades` history stays
queryable).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from core.models import Coin
from infrastructure.binance.client import BinanceFuturesClient
from infrastructure.database.repositories.coin_repository import CoinRepository
from system.logging_setup import get_logger

_logger = get_logger("trading")


@dataclass(frozen=True)
class SymbolSyncResult:
    """Outcome of one `SymbolDiscoveryEngine.sync()` call."""

    total_symbols: int
    new_symbols: tuple[str, ...]
    delisted_symbols: tuple[str, ...]


class SymbolDiscoveryEngine:
    """Keeps `coins` in sync with Binance's current USDT-M perpetual listing."""

    def __init__(self, client: BinanceFuturesClient, coin_repository: CoinRepository) -> None:
        self._client = client
        self._coin_repository = coin_repository

    async def sync(self) -> SymbolSyncResult:
        """
        Fetch the current exchange symbol list, upsert every symbol seen,
        and mark previously-active symbols Binance no longer lists as
        inactive.

        Returns:
            A `SymbolSyncResult` summarizing what changed, for logging
            and for the future daily report's "new / delisted coins" line
            (SRS Part 15).
        """
        exchange_symbols = await self._client.get_exchange_info()
        now = datetime.now(timezone.utc)

        exchange_symbol_names = {info.symbol for info in exchange_symbols}
        previously_active_names = {coin.symbol for coin in self._coin_repository.list_active()}

        new_symbols: list[str] = []
        for info in exchange_symbols:
            existing = self._coin_repository.get(info.symbol)
            if existing is None:
                new_symbols.append(info.symbol)

            self._coin_repository.upsert(
                Coin(
                    symbol=info.symbol,
                    base_asset=info.base_asset,
                    quote_asset=info.quote_asset,
                    status=info.status,
                    is_active=info.is_trading,
                    first_seen_at=existing.first_seen_at if existing else now,
                    last_seen_at=now,
                )
            )

        delisted_symbols = sorted(previously_active_names - exchange_symbol_names)
        for symbol in delisted_symbols:
            coin = self._coin_repository.get(symbol)
            if coin is not None:
                coin.is_active = False
                coin.status = "DELISTED"
                coin.last_seen_at = now
                self._coin_repository.upsert(coin)

        if new_symbols:
            _logger.info(
                "Symbol discovery: %d new symbol(s): %s",
                len(new_symbols),
                ", ".join(sorted(new_symbols)),
            )
        if delisted_symbols:
            _logger.info(
                "Symbol discovery: %d symbol(s) delisted: %s",
                len(delisted_symbols),
                ", ".join(delisted_symbols),
            )

        return SymbolSyncResult(
            total_symbols=len(exchange_symbol_names),
            new_symbols=tuple(sorted(new_symbols)),
            delisted_symbols=tuple(delisted_symbols),
        )
