"""Structured, replaceable God Eyes source configuration."""
from __future__ import annotations

from .models import InstrumentType, MarketInstrument, NewsSourceConfig, SourceKind


# Configuration data only: services may replace this catalog without changing business logic.
DEFAULT_MARKET_UNIVERSE = (
    MarketInstrument("BTC-USD", "Bitcoin", InstrumentType.CRYPTO, provider_symbols={"kraken": "XBTUSD"},
                     entities=("Bitcoin",), exchange="global", liquidity_tier="high"),
    MarketInstrument("ETH-USD", "Ethereum", InstrumentType.CRYPTO, provider_symbols={"kraken": "ETHUSD"},
                     entities=("Ethereum",), exchange="global", liquidity_tier="high"),
    MarketInstrument("AAPL", "Apple", InstrumentType.EQUITY, entities=("Apple",), exchange="NASDAQ",
                     sector="Technology", industry="Consumer Electronics", liquidity_tier="high"),
    MarketInstrument("MSFT", "Microsoft", InstrumentType.EQUITY, entities=("Microsoft",), exchange="NASDAQ",
                     sector="Technology", industry="Software", liquidity_tier="high"),
    MarketInstrument("AIR.PA", "Airbus", InstrumentType.EQUITY, currency="EUR", entities=("Airbus",),
                     exchange="Euronext Paris", sector="Industrials", industry="Aerospace", liquidity_tier="high"),
    MarketInstrument("^GSPC", "S&P 500", InstrumentType.INDEX, entities=("S&P 500",), exchange="S&P DJI", liquidity_tier="high"),
    MarketInstrument("^STOXX50E", "EURO STOXX 50", InstrumentType.INDEX, currency="EUR", exchange="STOXX"),
    MarketInstrument("SPY", "SPDR S&P 500 ETF", InstrumentType.ETF, entities=("S&P 500",), exchange="NYSE Arca", liquidity_tier="high"),
    MarketInstrument("EURUSD=X", "EUR/USD", InstrumentType.FX, exchange="FX", liquidity_tier="high"),
    MarketInstrument("GC=F", "Gold Futures", InstrumentType.COMMODITY, exchange="COMEX", liquidity_tier="high"),
    MarketInstrument("CL=F", "WTI Crude Futures", InstrumentType.COMMODITY, exchange="NYMEX", liquidity_tier="high"),
)

CALIBRATION_QUALITY_THRESHOLDS = ((200, "high"), (50, "medium"), (20, "low"))

UNIVERSE_TIERS = ("core", "extended", "watchlist", "dynamic")


def market_universe(instruments=DEFAULT_MARKET_UNIVERSE, *, tiers: tuple[str, ...] | None = None,
                    active_only: bool = True) -> tuple[MarketInstrument, ...]:
    """Filter an externally replaceable catalog without hard-coded scanner logic."""
    selected = set(tiers or UNIVERSE_TIERS)
    return tuple(item for item in instruments if item.universe_tier in selected and (item.active or not active_only))


def calibration_quality_tier(sample_count: int) -> str:
    return next((label for threshold, label in CALIBRATION_QUALITY_THRESHOLDS if sample_count >= threshold), "insufficient")


# Conservative official/public feeds. Applications may replace this tuple entirely.
DEFAULT_NEWS_SOURCES = (
    NewsSourceConfig("fed_press", "Federal Reserve press releases",
                     "https://www.federalreserve.gov/feeds/press_all.xml", "rss", 0.95,
                     ("federalreserve.gov",), SourceKind.PRIMARY, True),
    NewsSourceConfig("fed_monetary", "Federal Reserve monetary policy",
                     "https://www.federalreserve.gov/feeds/press_monetary.xml", "rss", 0.98,
                     ("federalreserve.gov",), SourceKind.PRIMARY, True),
    NewsSourceConfig("ecb_press", "ECB press releases",
                     "https://www.ecb.europa.eu/rss/press.html", "rss", 0.95,
                     ("ecb.europa.eu",), SourceKind.PRIMARY, True),
    NewsSourceConfig("ecb_stats", "ECB statistical releases",
                     "https://www.ecb.europa.eu/rss/statpress.html", "rss", 0.95,
                     ("ecb.europa.eu",), SourceKind.PRIMARY, True),
    NewsSourceConfig("sec_press", "SEC press releases",
                     "https://www.sec.gov/news/pressreleases.rss", "rss", 0.95,
                     ("sec.gov",), SourceKind.PRIMARY, True),
    NewsSourceConfig("bis_press", "BIS press releases",
                     "https://www.bis.org/doclist/all_pressrels.rss", "rss", 0.92,
                     ("bis.org",), SourceKind.PRIMARY, True),
    NewsSourceConfig("imf_news", "IMF news",
                     "https://www.imf.org/en/News/RSS", "rss", 0.90,
                     ("imf.org",), SourceKind.PRIMARY, True),
    NewsSourceConfig("boe_news", "Bank of England news",
                     "https://www.bankofengland.co.uk/rss/news", "rss", 0.95,
                     ("bankofengland.co.uk",), SourceKind.PRIMARY, True),
    NewsSourceConfig("eurostat", "Eurostat releases",
                     "https://ec.europa.eu/eurostat/api/dissemination/rss/release-calendar.xml", "rss", 0.94,
                     ("ec.europa.eu",), SourceKind.PRIMARY, True),
    NewsSourceConfig("cftc_press", "CFTC press releases",
                     "https://www.cftc.gov/PressRoom/PressReleases/rss", "rss", 0.94,
                     ("cftc.gov",), SourceKind.PRIMARY, True),
    NewsSourceConfig("cointelegraph", "Cointelegraph",
                     "https://cointelegraph.com/rss", "rss", 0.72,
                     ("cointelegraph.com",), SourceKind.SECONDARY, False),
)
