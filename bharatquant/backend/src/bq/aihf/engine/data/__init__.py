"""v2 data pipeline — data provider protocol and response models.

The Financial Datasets client and its x402 wallet are not vendored: this port
implements `DataClient` over BharatQuant's own vendor router (see
`bq/aihf/bridge/data.py`). What remains here is the protocol, its error type,
the response models, the disk cache and the screener vocabulary.
"""

from bq.aihf.engine.data.cached import CachedDataClient
from bq.aihf.engine.data.models import (
    CompanyFacts,
    CompanyNews,
    Earnings,
    EarningsData,
    EarningsRecord,
    Filing,
    FinancialMetrics,
    InsiderTrade,
    Price,
    PriceSnapshot,
)
from bq.aihf.engine.data.protocol import DataClient, DataClientError
from bq.aihf.engine.data.screener import (
    OrderBy,
    ScreenerFields,
    ScreenFilter,
    ScreenRow,
)

__all__ = [
    "CachedDataClient",
    "CompanyFacts",
    "CompanyNews",
    "DataClient",
    "DataClientError",
    "Earnings",
    "EarningsData",
    "EarningsRecord",
    "Filing",
    "FinancialMetrics",
    "InsiderTrade",
    "OrderBy",
    "Price",
    "PriceSnapshot",
    "ScreenFilter",
    "ScreenRow",
    "ScreenerFields",
]
