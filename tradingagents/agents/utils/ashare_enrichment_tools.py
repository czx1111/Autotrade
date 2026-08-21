"""A-share enrichment tools exposed to the analysts.

These wrap the A-share-specific data endpoints registered in the vendor router
(northbound capital flow, margin trading, the dragon-tiger list, retail social
sentiment, and a full-market snapshot). They only resolve under the ``akshare``
vendor and degrade to a graceful string otherwise, so binding them in a
US-market run is harmless.
"""

from typing import Annotated

from langchain_core.tools import tool

from tradingagents.dataflows.interface import route_to_vendor


@tool
def get_northbound_flow(
    days: Annotated[int, "Number of recent trading days to return; omit for 10"] = 10,
) -> str:
    """沪深港通北向资金净流入 (net northbound capital flow).

    Net daily inflow of overseas capital into A-shares via the Stock Connect
    programme — a widely watched proxy for foreign positioning. Uses the
    configured ashare_enrichment vendor.
    """
    return route_to_vendor("get_northbound_flow", days)


@tool
def get_margin_trading(
    code: Annotated[str | None, "Optional A-share code; omit for market-wide totals"] = None,
) -> str:
    """融资融券余额 (margin trading balances).

    Outstanding margin-financed long/short balances for the market or a single
    stock — a leverage and sentiment gauge. Uses the configured
    ashare_enrichment vendor.
    """
    return route_to_vendor("get_margin_trading", code)


@tool
def get_dragon_tiger_list(
    date: Annotated[str | None, "Trade date in yyyymmdd; omit for the latest session"] = None,
) -> str:
    """龙虎榜 (dragon-tiger list).

    Stocks with the day's largest institutional/block trades, with the seat and
    buy/sell amounts disclosed. A momentum and "smart-money" signal. Uses the
    configured ashare_enrichment vendor.
    """
    return route_to_vendor("get_dragon_tiger_list", date)


@tool
def get_social_sentiment(
    code: Annotated[str, "A-share code (e.g. 600519) or Chinese name"],
) -> str:
    """个股股吧评论情绪 (retail social sentiment from EastMoney comments).

    Recent retail-trader comments for one stock — a proxy for retail mood.
    Uses the configured ashare_enrichment vendor.
    """
    return route_to_vendor("get_social_sentiment", code)


@tool
def get_share_unlock(
    code: Annotated[str, "A-share code (e.g. 600519)"],
) -> str:
    """限售解禁批次 (restricted-share release schedule for one stock).

    Upcoming lock-up expiries with release dates and share counts — a key
    supply/demand risk: large imminent unlocks often pressure the price.
    Uses the configured ashare_enrichment vendor.
    """
    return route_to_vendor("get_share_unlock", code)


@tool
def get_ashare_market_snapshot() -> str:
    """A-share 全市场快照 (indices, sector boards).

    Major index levels (上证/深证/创业板/沪深300/中证500) and industry-board
    performance for a broad market read. Uses the configured ashare_enrichment
    vendor.
    """
    return route_to_vendor("get_ashare_market_snapshot")
