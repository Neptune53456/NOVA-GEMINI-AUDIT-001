"""Public market and RSS/Atom adapters."""
from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from typing import Any, Callable, Protocol
from urllib.parse import quote
import re
import xml.etree.ElementTree as ET
import time

from .models import (DataQuality, MarketCandle, MarketInstrument, MarketQuote, NewsItem,
                     NewsSourceConfig, SourceMetadata, as_utc, utc_now)
from .network import SafeHttpClient


class QuoteProvider(Protocol):
    name: str
    def quote(self, instrument: MarketInstrument) -> MarketQuote: ...
    def candles(self, instrument: MarketInstrument, interval: str, limit: int) -> list[MarketCandle]: ...


INTERVAL_SECONDS = {"1m": 60, "5m": 300, "1h": 3600, "1d": 86400}
FRESHNESS_SECONDS = {"crypto": 180, "equity": 900, "index": 900, "etf": 900, "fx": 300, "commodity": 1800}


class ProviderFailure(RuntimeError): pass
class StaleData(ProviderFailure): pass
class MalformedResponse(ProviderFailure): pass
class RateLimited(ProviderFailure): pass


class ResilientProviderChain:
    """Ordered fallback with bounded retry and a small in-memory circuit breaker."""
    def __init__(self, providers: list[QuoteProvider], *, retries: int = 2, base_delay: float = .1,
                 max_delay: float = 1.0, failure_threshold: int = 3, cooldown: float = 30,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> None:
        self.providers = providers; self.retries = max(0, retries); self.base_delay = max(0, base_delay)
        self.max_delay = max_delay; self.failure_threshold = max(1, failure_threshold); self.cooldown = cooldown
        self.sleep, self.clock = sleep, clock; self._state: dict[str, dict[str, float]] = {}
        self.metrics: dict[str, dict[str, int]] = {}

    def call(self, method: str, *args: Any) -> Any:
        errors = []
        for provider in self.providers:
            state = self._state.setdefault(provider.name, {"failures": 0, "opened": 0})
            stats = self.metrics.setdefault(provider.name, {"success": 0, "failure": 0, "rate_limited": 0, "stale": 0, "malformed": 0})
            if state["failures"] >= self.failure_threshold and self.clock() - state["opened"] < self.cooldown:
                errors.append(f"{provider.name}:circuit_open"); continue
            for attempt in range(self.retries + 1):
                try:
                    value = getattr(provider, method)(*args)
                    if isinstance(value, MarketQuote) and value.quality.stale: raise StaleData("stale_data")
                    state["failures"] = 0; stats["success"] += 1; return value
                except Exception as exc:
                    category = classify_provider_error(exc); stats["failure"] += 1
                    if category in stats: stats[category] += 1
                    errors.append(f"{provider.name}:{category}")
                    if category == "rate_limited" and attempt < self.retries:
                        self.sleep(min(self.max_delay, self.base_delay * (2 ** attempt))); continue
                    break
            state["failures"] += 1
            if state["failures"] >= self.failure_threshold: state["opened"] = self.clock()
        raise ProviderFailure(";".join(errors) or "no_provider")


def classify_provider_error(error: Exception) -> str:
    text = str(error).casefold()
    if isinstance(error, RateLimited) or "429" in text or "rate limit" in text: return "rate_limited"
    if isinstance(error, StaleData): return "stale"
    if isinstance(error, (MalformedResponse, KeyError, TypeError, ValueError, AssertionError)): return "malformed"
    return "provider_failure"

def detect_candle_gaps(candles:list[dict[str,Any]],interval:str,*,max_gaps:int=20)->list[tuple[datetime,datetime]]:
    seconds=INTERVAL_SECONDS[interval]; ordered=sorted({as_utc(c["opened_at"]) for c in candles})
    gaps=[]
    for left,right in zip(ordered,ordered[1:]):
        if (right-left).total_seconds()>seconds*1.5:
            gaps.append((left,right))
            if len(gaps)>=max_gaps:break
    return gaps

def normalize_candles(candles:list[MarketCandle])->list[MarketCandle]:
    unique={}
    for candle in sorted(candles,key=lambda c:c.opened_at):
        if candle.opened_at>utc_now():continue
        unique[(candle.instrument.symbol,candle.interval,candle.opened_at,candle.source.provider)]=candle
    return list(unique.values())


def _candle(instrument: MarketInstrument, interval: str, values: list[object], provider: str,
            url: str, retrieved: datetime | None = None) -> MarketCandle:
    retrieved = retrieved or utc_now()
    opened = as_utc(values[0])  # timestamp, open, high, low, close, volume
    complete = all(value is not None for value in values[1:5])
    source = SourceMetadata(provider, url, retrieved)
    return MarketCandle(instrument, interval, opened, float(values[1]), float(values[2]),
                        float(values[3]), float(values[4]), float(values[5]) if values[5] is not None else None,
                        source, DataQuality(opened, retrieved, INTERVAL_SECONDS[interval] * 2, complete,
                                            () if complete else ("incomplete_ohlc",)))


def _quote(instrument: MarketInstrument, price: object, observed: object, provider: str, url: str,
           *, change_percent: object = None) -> MarketQuote:
    retrieved = utc_now()
    timestamp = as_utc(observed)  # type: ignore[arg-type]
    source = SourceMetadata(provider, url, retrieved)
    return MarketQuote(instrument, float(price), timestamp, source,
                       DataQuality(timestamp, retrieved, FRESHNESS_SECONDS[instrument.kind.value], float(price) > 0,
                                   () if float(price) > 0 else ("non_positive_price",)),
                       float(change_percent) if change_percent is not None else None)


class CoinbaseProvider:
    name = "coinbase"
    def __init__(self, client: SafeHttpClient | None = None) -> None:
        self.client = client or SafeHttpClient({"api.exchange.coinbase.com"})

    def quote(self, instrument: MarketInstrument) -> MarketQuote:
        symbol = instrument.provider_symbol(self.name)
        url = f"https://api.exchange.coinbase.com/products/{quote(symbol, safe='-')}/ticker"
        data, final_url = self.client.get_json(url)
        assert isinstance(data, dict)
        return _quote(instrument, data["price"], data["time"], self.name, final_url)

    def candles(self, instrument: MarketInstrument, interval: str, limit: int) -> list[MarketCandle]:
        granularity = INTERVAL_SECONDS[interval]
        symbol = instrument.provider_symbol(self.name)
        url = (f"https://api.exchange.coinbase.com/products/{quote(symbol, safe='-')}/candles"
               f"?granularity={granularity}")
        data, final_url = self.client.get_json(url)
        assert isinstance(data, list)
        retrieved = utc_now()
        # Coinbase: time, low, high, open, close, volume.
        return [_candle(instrument, interval, [row[0], row[3], row[2], row[1], row[4], row[5]],
                        self.name, final_url, retrieved) for row in data[:limit]]

    def microstructure(self, instrument: MarketInstrument) -> dict[str, Any]:
        symbol = instrument.provider_symbol(self.name)
        url = f"https://api.exchange.coinbase.com/products/{quote(symbol, safe='-')}/book?level=2"
        data, _ = self.client.get_json(url)
        if not isinstance(data, dict): raise MalformedResponse("invalid_book")
        return data


class KrakenProvider:
    name = "kraken"
    def __init__(self, client: SafeHttpClient | None = None) -> None:
        self.client = client or SafeHttpClient({"api.kraken.com"})

    def quote(self, instrument: MarketInstrument) -> MarketQuote:
        symbol = instrument.provider_symbol(self.name)
        url = f"https://api.kraken.com/0/public/Ticker?pair={quote(symbol)}"
        data, final_url = self.client.get_json(url)
        assert isinstance(data, dict) and not data.get("error")
        values = next(iter(data["result"].values()))
        return _quote(instrument, values["c"][0], utc_now(), self.name, final_url)

    def candles(self, instrument: MarketInstrument, interval: str, limit: int) -> list[MarketCandle]:
        minutes = {"1m": 1, "5m": 5, "1h": 60, "1d": 1440}[interval]
        symbol = instrument.provider_symbol(self.name)
        url = f"https://api.kraken.com/0/public/OHLC?pair={quote(symbol)}&interval={minutes}"
        data, final_url = self.client.get_json(url)
        assert isinstance(data, dict) and not data.get("error")
        rows = next(value for key, value in data["result"].items() if key != "last")
        retrieved = utc_now()
        return [_candle(instrument, interval, [*row[:5], row[6]], self.name, final_url, retrieved)
                for row in rows[-limit:]]

    def microstructure(self, instrument: MarketInstrument) -> dict[str, Any]:
        symbol = instrument.provider_symbol(self.name)
        url = f"https://api.kraken.com/0/public/Depth?pair={quote(symbol)}&count=10"
        data, _ = self.client.get_json(url)
        if not isinstance(data, dict) or data.get("error"): raise MalformedResponse("invalid_book")
        return data


class YahooProvider:
    name = "yahoo"
    def __init__(self, client: SafeHttpClient | None = None) -> None:
        self.client = client or SafeHttpClient({"query1.finance.yahoo.com"})

    def quote(self, instrument: MarketInstrument) -> MarketQuote:
        symbol = instrument.provider_symbol(self.name)
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(symbol, safe='^.-=')}?interval=1m&range=1d"
        data, final_url = self.client.get_json(url)
        assert isinstance(data, dict)
        result = data["chart"]["result"][0]
        meta = result["meta"]
        return _quote(instrument, meta["regularMarketPrice"], meta["regularMarketTime"], self.name,
                      final_url, change_percent=meta.get("regularMarketChangePercent"))

    def candles(self, instrument: MarketInstrument, interval: str, limit: int) -> list[MarketCandle]:
        yahoo_interval = {"1m": "1m", "5m": "5m", "1h": "60m", "1d": "1d"}[interval]
        range_value = "1d" if interval in {"1m", "5m"} else ("1mo" if interval == "1h" else "1y")
        symbol = instrument.provider_symbol(self.name)
        url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(symbol, safe='^.-=')}"
               f"?interval={yahoo_interval}&range={range_value}")
        data, final_url = self.client.get_json(url)
        if not isinstance(data, dict) or not isinstance(data.get("chart"), dict):
            raise MalformedResponse("invalid_yahoo_chart")
        chart = data["chart"]
        if chart.get("error") is not None or not isinstance(chart.get("result"), list) or not chart["result"]:
            raise MalformedResponse("invalid_yahoo_result")
        result = chart["result"][0]
        if not isinstance(result, dict) or not isinstance(result.get("indicators"), dict):
            raise MalformedResponse("invalid_yahoo_indicators")
        quotes = result["indicators"].get("quote")
        if not isinstance(quotes, list) or not quotes or not isinstance(quotes[0], dict):
            raise MalformedResponse("invalid_yahoo_quotes")
        values = quotes[0]
        timestamps = result.get("timestamp")
        columns = [values.get(name) for name in ("open", "high", "low", "close")]
        if timestamps is None and all(column is None or column == [] for column in columns):
            return []
        if not isinstance(timestamps, list) or any(not isinstance(column, list) or len(column) != len(timestamps) for column in columns):
            raise MalformedResponse("invalid_yahoo_candles")
        retrieved = utc_now()
        volumes = values.get("volume")
        if volumes is not None and (not isinstance(volumes, list) or len(volumes) != len(timestamps)):
            raise MalformedResponse("invalid_yahoo_volume")
        rows = zip(timestamps, *columns, volumes if volumes is not None else [None] * len(timestamps))
        return [_candle(instrument, interval, list(row), self.name, final_url, retrieved)
                for row in list(rows)[-limit:] if all(value is not None for value in list(row)[1:5])]


class RssAtomProvider:
    def __init__(self, name: str, url: str, allowed_hosts: set[str], client: SafeHttpClient | None = None,
                 *, config: NewsSourceConfig | None = None) -> None:
        self.name, self.url, self.config = name, url, config
        self.client = client or SafeHttpClient(allowed_hosts)

    @classmethod
    def from_config(cls, config: NewsSourceConfig) -> "RssAtomProvider":
        return cls(config.source_id, config.url, set(config.allowed_domains), config=config)

    def fetch(self, instruments: list[MarketInstrument]) -> list[NewsItem]:
        body, _content_type, final_url = self.client.get_bytes(
            self.url, accept="application/rss+xml, application/atom+xml, application/xml, text/xml")
        root = ET.fromstring(body)
        retrieved = utc_now()
        result: list[NewsItem] = []
        for node in root.findall(".//item") + root.findall(".//{*}entry"):
            title = _text(node, "title")
            link = _text(node, "link") or next((item.get("href", "") for item in node.findall("{*}link")), "")
            published = _text(node, "pubDate") or _text(node, "published") or _text(node, "updated")
            summary = _text(node, "description") or _text(node, "summary")
            if not title or not link:
                continue
            haystack = f"{title} {summary}".casefold()
            tickers = tuple(i.symbol for i in instruments if i.symbol.casefold() in haystack)
            entities = tuple(sorted({entity for i in instruments for entity in i.entities if entity.casefold() in haystack}))
            themes = tuple(theme for theme in ("earnings", "merger", "regulation", "inflation", "crypto") if theme in haystack)
            when = _date(published, retrieved)
            source = SourceMetadata(self.name, final_url, retrieved, "rss_atom")
            result.append(NewsItem(NewsItem.stable_id(link, title), _clean(title), link, when,
                                   _clean(summary)[:2000], source, tickers, entities, themes))
        return result


def _text(node: ET.Element, name: str) -> str:
    child = node.find(name)
    if child is None:
        child = node.find(f"{{*}}{name}")
    return (child.text or "").strip() if child is not None else ""


def _clean(value: str) -> str:
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", value)).split())


def _date(value: str, fallback: datetime) -> datetime:
    if not value:
        return fallback
    try:
        return as_utc(parsedate_to_datetime(value))
    except (TypeError, ValueError, OverflowError):
        try:
            return as_utc(value)
        except (TypeError, ValueError):
            return fallback
