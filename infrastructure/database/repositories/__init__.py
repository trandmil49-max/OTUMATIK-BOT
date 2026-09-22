"""
infrastructure/database/repositories/ -- one repository class per table
family (SRS Part 20 Clean Architecture: repositories are the ONLY code
that ever writes raw SQL; nothing above this layer knows a database
exists).
"""

from infrastructure.database.repositories.base_repository import BaseRepository
from infrastructure.database.repositories.coin_repository import (
    CoinProfileRepository,
    CoinRepository,
    CoinStatisticsRepository,
)
from infrastructure.database.repositories.market_repository import (
    BtcStatisticsRepository,
    MarketStatisticsRepository,
)
from infrastructure.database.repositories.rejection_repository import (
    MissedOpportunityRepository,
    RejectionRepository,
)
from infrastructure.database.repositories.report_repository import (
    FilterPerformanceRepository,
    ReportRepository,
)
from infrastructure.database.repositories.signal_repository import SignalRepository
from infrastructure.database.repositories.system_repository import (
    BotHealthRepository,
    ConfigSnapshotRepository,
    ErrorEventRepository,
)
from infrastructure.database.repositories.trade_repository import TradeRepository

__all__ = [
    "BaseRepository",
    "CoinRepository",
    "CoinProfileRepository",
    "CoinStatisticsRepository",
    "SignalRepository",
    "TradeRepository",
    "RejectionRepository",
    "MissedOpportunityRepository",
    "BtcStatisticsRepository",
    "MarketStatisticsRepository",
    "ReportRepository",
    "FilterPerformanceRepository",
    "BotHealthRepository",
    "ErrorEventRepository",
    "ConfigSnapshotRepository",
]
