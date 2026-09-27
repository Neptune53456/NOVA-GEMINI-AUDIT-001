"""Fault-isolated orchestration, bounded cache and health reporting."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import asdict, replace
from hashlib import sha256
from datetime import datetime, timedelta, timezone
import os
from threading import Lock
from typing import Iterable
from urllib.parse import urlsplit

from nova_api.journal import EventJournal

from .config import DEFAULT_MARKET_UNIVERSE, DEFAULT_NEWS_SOURCES, calibration_quality_tier
from .enrichment import EnrichmentGate, ModelRouterEventEnricher, event_fingerprint
from .calibration import build_calibration, calibration_metrics
from .forecasting import EnsembleForecastEngine, build_feature_set, evaluate_forecast, reaction_features
from .intelligence import EventIntelligence
from .models import InstrumentType, MarketInstrument, MarketQuote, NewsItem, NewsSourceConfig, as_utc, public, utc_now
from .opportunities import build_opportunity
from .costs import CostEngine
from .star_finder import (StarFinderConfig, entry_timing, expected_net_return, position_decision,
                          rank_opportunities, rejection_gates, star_score, lifecycle_transition)
from .regimes import classify_regime, global_regime
from .similarity import retrieve_similar, similar_features
from .providers import CoinbaseProvider, KrakenProvider, QuoteProvider, RssAtomProvider, YahooProvider, classify_provider_error
from .portfolio import PaperPortfolio, benchmark_comparison, portfolio_metrics, walk_forward
from .trader import (PaperExecutionEngine, PaperLedger, ReplayEngine, STRATEGIES, TraderConfig, TraderMode,
                     approximate_attribution, capital_simulation, performance, strategy_competition,
                     position_action, missed_outcome)
from .social import BlueskyProvider, SocialSignalDetector, YouTubeFeedProvider
from .scheduler import GodEyeScheduler
from .storage import GodEyeStore
from .alerts import build_alert, confirmation_graph
from .alternative import (ALTERNATIVE_CAPABILITIES, MacroReleaseCollector, MacroSourceConfig,
                          SecEdgarCollector, SecEdgarConfig, alternative_to_event, capability_status)
from .integrity import GapRepair, assess_candles
from .live_forward import LiveForwardValidator
from .autonomous_research import evidence_maturity, learning_summary, watchdog
from .network import SafeHttpClient
from .market_intelligence import (MarketScanner, PatternEngine, aggregate_candles, classify_regime_v2,
    intelligence_quality, liquidity_metrics, multi_timeframe_context, parse_microstructure,
    quantitative_features, quantitative_divergences, historical_pattern_statistics,
    retrieve_similar_situations, signal_decay, fuse_event_evidence)


DEFAULT_UNIVERSE = DEFAULT_MARKET_UNIVERSE  # compatibility alias; catalog lives in config.py


class GodEyeService:
    def __init__(self, *, store: GodEyeStore | None = None, journal: EventJournal | None = None,
                 instruments: Iterable[MarketInstrument] | None = None,
                 crypto_providers: list[QuoteProvider] | None = None,
                 yahoo_provider: QuoteProvider | None = None,
                 news_providers: list[RssAtomProvider] | None = None,
                 source_configs: Iterable[NewsSourceConfig] | None = None,
                 candle_intervals: tuple[str, ...] = ("1m", "5m", "1h", "1d"),
                 candle_limit: int = 300, candle_retention: int = 2000,
                 cache_size: int = 256, scheduler: GodEyeScheduler | None = None,
                 event_enricher: ModelRouterEventEnricher | None = None,
                 llm_enabled: bool | None = None, enrichment_budget: int = 3,
                 social_providers: list[object] | None = None, paper_portfolio: PaperPortfolio | None = None,
                 alternative_collectors: dict[str, object] | None = None,
                 sec_config: SecEdgarConfig | None = None,
                 macro_configs: Iterable[MacroSourceConfig] = (),
                 retention_days: dict[str, int] | None = None) -> None:
        self.store = store or GodEyeStore()
        self.retention_days = dict(retention_days or {})
        self.journal = journal
        self.instruments = {item.symbol: item for item in (instruments or DEFAULT_MARKET_UNIVERSE) if item.active}
        self.crypto_providers = crypto_providers if crypto_providers is not None else [CoinbaseProvider(), KrakenProvider()]
        self.yahoo_provider = yahoo_provider or YahooProvider()
        configs = list(source_configs) if source_configs is not None else list(DEFAULT_NEWS_SOURCES)
        self.source_configs = {source.source_id: source for source in configs}
        self.news_providers = news_providers if news_providers is not None else [RssAtomProvider.from_config(c) for c in configs]
        self.store.register_sources(configs)
        self.intelligence = EventIntelligence(list(self.instruments.values()), self.source_configs)
        self.candle_intervals = tuple(i for i in candle_intervals if i in {"1m", "5m", "1h", "1d"})
        self.candle_limit, self.candle_retention = max(1, min(candle_limit, 1000)), max(1, candle_retention)
        self.cache_size = max(1, cache_size)
        self._quotes: OrderedDict[str, MarketQuote] = OrderedDict()
        self._last_errors: dict[str, str] = {}
        self._provider_health: dict[str, dict[str, object]] = {}
        self.llm_enabled = (os.environ.get("NOVA_GOD_EYE_LLM_ENABLED") == "1") if llm_enabled is None else llm_enabled
        self.event_enricher = event_enricher or ModelRouterEventEnricher()
        self.enrichment_gate, self.enrichment_budget = EnrichmentGate(), max(0, enrichment_budget)
        self.forecast_engine = EnsembleForecastEngine()
        self.pattern_engine = PatternEngine()
        self.cost_engine = CostEngine()
        self.star_finder_config = StarFinderConfig()
        self.market_scanner = MarketScanner()
        self._scanner_result: dict[str, object] = {"scanned": 0, "deep_analysis_count": 0,
                                                   "stage_a": [], "stage_b": []}
        self._microstructure: dict[str, dict[str, object]] = {}
        existing_ensemble = next((item for item in self.store.governance("ensemble")
                                  if item.get("item_key") == "active" and item.get("version") == self.forecast_engine.model_version), None)
        if existing_ensemble is None:
            created_at = utc_now().isoformat()
            self.store.save_governance("ensemble","active",self.forecast_engine.model_version,
                {"kind":"ensemble","item_key":"active","version":self.forecast_engine.model_version,
                 "status":"incumbent","source":"phase7-default","created_at":created_at,
                 "holdout_used_for_selection":False},created_at)
        self.minimum_calibration_samples = 20
        youtube_channels = tuple(filter(None, os.environ.get("NOVA_GOD_EYE_YOUTUBE_CHANNELS", "").split(",")))
        self.social_providers = social_providers if social_providers is not None else [BlueskyProvider(), *([YouTubeFeedProvider(youtube_channels)] if youtube_channels else [])]
        self.social_detector = SocialSignalDetector()
        # Historical event/walk-forward simulation only. The Trader ledger below is the
        # single authoritative runtime AUTO_PAPER book and the only public portfolio.
        self.simulation_portfolio = paper_portfolio or PaperPortfolio()
        self.paper_portfolio = self.simulation_portfolio  # compatibility for isolated simulations
        self.trader_kill_switch = False
        configured_mode = os.environ.get("NOVA_TRADER_MODE", TraderMode.OBSERVE.value)
        try: trader_mode = TraderMode(configured_mode)
        except ValueError: trader_mode = TraderMode.OBSERVE
        fallback_config=TraderConfig(mode=trader_mode)
        champions=sorted((v for v in self.store.governance("trader_champion") if v.get("status")=="active"),
                         key=lambda v:str(v.get("activated_at","")),reverse=True)
        if champions:
            try: fallback_config=self._validated_trader_config(champions[0],fallback_config)
            except (TypeError, ValueError): pass
        persisted_trader = self.store.trader_state()
        self.trader = PaperLedger.restore(persisted_trader,fallback_config)
        if champions and not persisted_trader: self.trader.config=replace(fallback_config,mode=self.trader.config.mode)
        self.strategy_ledgers={name:PaperLedger.restore(self.store.trader_state(name),
            replace(fallback_config,mode=TraderMode.OBSERVE)) for name in STRATEGIES if name!="NOVA_COMPOSITE"}
        self.strategy_ledgers["NOVA_COMPOSITE"]=self.trader
        self.execution_engine=PaperExecutionEngine(self.cost_engine)
        self.execution_engine.restore(self.store.trader_records("order"),self.store.trader_records("fill"))
        self.replay_engine=ReplayEngine()
        self.sec_config, self.macro_configs = sec_config, tuple(macro_configs)
        self.alternative_collectors = dict(alternative_collectors or {})
        if sec_config and sec_config.active and "sec_edgar" not in self.alternative_collectors:
            client = SafeHttpClient({"data.sec.gov"})
            self.alternative_collectors["sec_edgar"] = SecEdgarCollector(
                sec_config, lambda url, headers: client.get_json(url, headers)[0])
        active_macro = tuple(item for item in self.macro_configs if item.active)
        if active_macro and "macro_calendar" not in self.alternative_collectors:
            hosts = {str(urlsplit(item.url).hostname) for item in active_macro if urlsplit(item.url).hostname}
            client = SafeHttpClient(hosts)
            self.alternative_collectors["macro_calendar"] = MacroReleaseCollector(
                active_macro, lambda url, headers: client.get_json(url, headers)[0])
        self.live_forward = LiveForwardValidator(self.store)
        self._refresh_locks = {name: Lock() for name in ("market", "history", "news", "social", "alternative")}
        extra = {name: (lambda name=name: self.refresh_alternative(name), 900.0 if name == "sec_edgar" else 300.0)
                 for name in self.alternative_collectors}
        extra["star_finder"] = (self.run_star_finder, 300.0)
        extra["paper_positions"] = (self.reevaluate_paper_positions, 300.0)
        extra["outcome_resolution"] = (self.resolve_due_outcomes, 300.0)
        extra["storage_cleanup"] = (self.cleanup_storage, 900.0)
        self.scheduler = scheduler or GodEyeScheduler(self.refresh_market_pipeline, self.refresh_news,
            evaluation=self.continuous_evaluation, extra_tasks=extra,
            initial_state=self.store.scheduler_state(), save_state=self.store.save_scheduler_state)

    def cleanup_storage(self) -> dict[str, object]:
        return self.store.cleanup_retention(retention_days=self.retention_days)

    def refresh_alternative(self, source: str | None = None) -> dict[str, object]:
        return self._exclusive("alternative", lambda: self._refresh_alternative(source))

    def _refresh_alternative(self, source: str | None = None) -> dict[str, object]:
        names = [source] if source else sorted(self.alternative_collectors)
        inserted = events_inserted = 0; errors: dict[str, str] = {}
        for name in names:
            collector = self.alternative_collectors.get(name)
            if collector is None: continue
            try:
                items, cursor = collector.collect(self.store.alternative_checkpoint(name))
                inserted += self.store.save_alternative_items(items)
                events = []
                for item in items:
                    matched = [symbol for symbol, instrument in self.instruments.items()
                               if set(item.entities) & ({symbol, instrument.name, *instrument.entities})]
                    events.append(alternative_to_event(item, matched))
                events_inserted += self.store.save_events(events)
                self.store.save_alternative_checkpoint(name, cursor, utc_now().isoformat())
                self.store.record_source_result(name, success=True, timestamp=utc_now().isoformat())
            except Exception as error:
                category = type(error).__name__; errors[name] = category
                self.store.record_source_result(name, success=False, timestamp=utc_now().isoformat(), error=category)
        status = "success" if not errors else ("partial" if inserted else "error")
        self.store.record_run("alternative", status, utc_now().isoformat(), str(errors)[:1000] if errors else None)
        return {"status": status, "inserted": inserted, "events_inserted": events_inserted, "errors": errors}

    @classmethod
    def for_journal(cls, journal: EventJournal) -> "GodEyeService":
        return cls(store=GodEyeStore(journal.path.with_name("nova_god_eye.sqlite3")), journal=journal)

    def refresh_market(self, symbols: list[str] | None = None) -> dict[str, object]:
        return self._exclusive("market", lambda: self._refresh_market(symbols))

    def _refresh_market(self, symbols: list[str] | None = None) -> dict[str, object]:
        selected = [self.instruments[symbol] for symbol in symbols or list(self.instruments) if symbol in self.instruments]
        quotes: list[MarketQuote] = []
        errors: dict[str, list[str]] = {}
        for instrument in selected:
            providers = self.crypto_providers if instrument.kind == InstrumentType.CRYPTO else [self.yahoo_provider]
            for provider in providers:
                attempted_at = utc_now().isoformat()
                health = self._provider_health.setdefault(provider.name, {})
                health["last_attempt"] = attempted_at
                try:
                    quote = provider.quote(instrument)
                    self.store.save_quote(quote)
                    self._cache(quote)
                    quotes.append(quote)
                    self._last_errors.pop(provider.name, None)
                    health.update(status="ok", last_success=attempted_at, last_error=None)
                    self._journal("god_eye.provider.success", "success", provider=provider.name)
                    break
                except Exception as error:  # adapter boundary: one provider must not abort a refresh
                    category = classify_provider_error(error)
                    errors.setdefault(instrument.symbol, []).append(f"{provider.name}:{category}")
                    self._last_errors[provider.name] = category
                    health.update(status="degraded", last_error=category)
                    self._journal("god_eye.provider.failed", "error", provider=provider.name, error_category=category)
        status = "success" if not errors else ("partial" if quotes else "error")
        self.store.record_run("quotes", status, utc_now().isoformat(), str(errors) if errors else None)
        self._journal("god_eye.market.refresh", status)
        return {"status": status, "updated": len(quotes), "errors": errors, "quotes": [public(q) for q in quotes]}

    def refresh_history(self, symbols: list[str] | None = None) -> dict[str, object]:
        return self._exclusive("history", lambda: self._refresh_history(symbols))

    def _refresh_history(self, symbols: list[str] | None = None) -> dict[str, object]:
        selected = [self.instruments[s] for s in symbols or list(self.instruments) if s in self.instruments]
        inserted, errors = 0, {}
        for instrument in selected:
            providers = self.crypto_providers if instrument.kind == InstrumentType.CRYPTO else [self.yahoo_provider]
            for interval in self.candle_intervals:
                for provider in providers:
                    if not callable(getattr(provider, "candles", None)):
                        continue
                    try:
                        candles = provider.candles(instrument, interval, self.candle_limit)
                        inserted += self.store.save_candles(candles, retention=self.candle_retention)
                        self._last_errors.pop(f"{provider.name}:candles", None)
                        break
                    except Exception as error:
                        category = classify_provider_error(error)
                        errors.setdefault(f"{instrument.symbol}:{interval}", []).append(f"{provider.name}:{category}")
                        self._last_errors[f"{provider.name}:candles"] = category
        status = "success" if not errors else ("partial" if inserted else "error")
        self.evaluate_forecasts()
        self._advance_paper()
        self.store.record_run("candles", status, utc_now().isoformat(), str(errors) if errors else None)
        return {"status": status, "inserted": inserted, "errors": errors}

    def refresh_market_pipeline(self) -> dict[str, object]:
        quotes, candles = self.refresh_market(), self.refresh_history()
        status = "error" if quotes["status"] == candles["status"] == "error" else (
            "success" if quotes["status"] == candles["status"] == "success" else "partial")
        return {"status": status, "quotes": quotes, "candles": candles}

    def refresh_news(self) -> dict[str, object]:
        return self._exclusive("news", self._refresh_news)

    def _refresh_news(self) -> dict[str, object]:
        unique: dict[str, NewsItem] = {}
        errors: dict[str, str] = {}
        instruments = list(self.instruments.values())
        for provider in self.news_providers:
            try:
                for item in provider.fetch(instruments):
                    unique.setdefault(item.item_id, item)
                self._last_errors.pop(provider.name, None)
                self.store.record_source_result(provider.name, success=True, timestamp=utc_now().isoformat())
            except Exception as error:
                errors[provider.name] = type(error).__name__
                self._last_errors[provider.name] = type(error).__name__
                self.store.record_source_result(provider.name, success=False, timestamp=utc_now().isoformat(),
                                                error=type(error).__name__)
                self._journal("god_eye.provider.failed", "error", provider=provider.name,
                              error_category=type(error).__name__)
        inserted = self.store.save_news(list(unique.values()))
        events = self.intelligence.build(list(unique.values()), self.store.events(limit=500))
        event_count = self.store.save_events(events)
        budget = self.enrichment_budget
        for event in events:
            outcome = self.analyze_event(event.event_id, remaining_budget=budget)
            if outcome.get("enrichment_status") == "created":
                budget -= 1
        status = "success" if not errors else ("partial" if unique else "error")
        self.store.record_run("news", status, utc_now().isoformat(), str(errors) if errors else None)
        self._journal("god_eye.news.refresh", status)
        return {"status": status, "fetched": len(unique), "inserted": inserted,
                "events_inserted": event_count, "errors": errors}

    def get_market_snapshot(self, symbols: list[str] | None = None) -> dict[str, object]:
        wanted = symbols or list(self.instruments)
        cached = [public(self._quotes[symbol]) for symbol in wanted if symbol in self._quotes]
        present = {item["instrument"]["symbol"] for item in cached}
        missing = [symbol for symbol in wanted if symbol not in present]
        persisted = self.store.recent_quotes(symbols=missing) if missing else []
        quotes = cached + persisted
        now = utc_now()
        for quote in quotes:
            quality = quote.get("quality", {})
            observed = datetime.fromisoformat(str(quality.get("observed_at", quote["observed_at"])))
            age = max(0.0, (now - observed.astimezone(timezone.utc)).total_seconds())
            quality["age_seconds"] = age
            quality["stale"] = age > int(quality.get("max_age_seconds", 0))
            instrument = quote.get("instrument", {})
            kind = str(instrument.get("kind", "unknown")) if isinstance(instrument, dict) else "unknown"
            provider = quote.get("source", {}).get("provider") if isinstance(quote.get("source"), dict) else None
            market_closed = kind in {"equity", "index", "etf", "fx", "commodity"} and now.weekday() >= 5
            quality["freshness_status"] = (
                "PROVIDER_UNAVAILABLE" if provider in self._last_errors else
                "MARKET_CLOSED" if market_closed else
                "STALE" if quality["stale"] else "FRESH"
            )
        return {"as_of": now.isoformat(), "quotes": quotes}

    def refresh_social(self) -> dict[str, object]:
        return self._exclusive("social", self._refresh_social)

    def _refresh_social(self) -> dict[str, object]:
        posts, errors = [], {}
        for provider in self.social_providers:
            try:
                posts.extend(provider.fetch(list(self.instruments.values())))
                self.store.record_source_result(provider.name, success=True, timestamp=utc_now().isoformat())
                self._journal("god_eye.social.ingestion", "success", provider=provider.name)
            except Exception as error:
                errors[provider.name] = classify_provider_error(error)
                self.store.record_source_result(provider.name, success=False, timestamp=utc_now().isoformat(), error=errors[provider.name])
                self._journal("god_eye.social.ingestion", "error", provider=provider.name, error_category=errors[provider.name])
        unique = list({post.post_id: post for post in posts}.values())
        inserted = self.store.save_social_posts(unique)
        signals = self.social_detector.detect(unique)
        signal_count = self.store.save_social_signals(signals)
        if signals: self._journal("god_eye.social.signal", "success")
        self.store.save_events(self.intelligence.build([], self.store.events(limit=500), signals))
        status = "success" if not errors else ("partial" if unique else "error")
        self.store.record_run("social", status, utc_now().isoformat(), str(errors) if errors else None)
        return {"status": status, "fetched": len(unique), "inserted": inserted, "signals_inserted": signal_count, "errors": errors}

    def get_social(self, limit: int = 100) -> dict[str, object]:
        return {"items": self.store.social_posts(limit), "active_sources": [p.name for p in self.social_providers]}

    def get_social_signals(self, limit: int = 100) -> dict[str, object]:
        return {"signals": self.store.social_signals(limit)}

    def get_recent_news(self, limit: int = 50) -> dict[str, object]:
        return {"items": self.store.recent_news(limit=limit)}

    def get_events(self, limit: int = 100, event_type: str | None = None,
                   instrument: str | None = None, start: str | None = None,
                   end: str | None = None) -> dict[str, object]:
        return {"events": self.store.events(limit=limit, event_type=event_type,
            instrument=instrument, start=start, end=end)}

    def get_event(self, event_id: str) -> dict[str, object] | None:
        return self.store.event(event_id)

    def get_event_analysis(self, event_id: str) -> dict[str, object] | None:
        event = self.store.event(event_id)
        if event is None:
            return None
        cached = self.store.enrichment(event_id, event_fingerprint(event))
        return {"event": event, "analysis": cached["analysis"] if cached else None,
                "enrichment_status": "available" if cached else "not_available",
                "outcomes": self.store.outcomes_for_event(event_id)}

    def analyze_event(self, event_id: str, *, remaining_budget: int | None = None) -> dict[str, object]:
        event = self.store.event(event_id)
        if event is None:
            raise KeyError("event_not_found")
        fingerprint = event_fingerprint(event)
        cached = self.store.enrichment(event_id, fingerprint)
        if cached:
            self._journal("god_eye.enrichment.cached", "success")
            analysis, enrichment_status = cached["analysis"], "cached"
        else:
            eligible, reason = self.enrichment_gate.eligible(
                event, remaining_budget=self.enrichment_budget if remaining_budget is None else remaining_budget)
            if not self.llm_enabled or not eligible:
                enrichment_status = "skipped"
                analysis = None
                self._journal("god_eye.enrichment.skipped", "success", error_category=("disabled" if not self.llm_enabled else reason))
            else:
                evidence = self.store.news_by_ids(list(event.get("evidence_refs", [])))
                self._journal("god_eye.enrichment.requested", "running")
                try:
                    analysis, meta = self.event_enricher.enrich(event, evidence)
                    self.store.save_enrichment(event_id, fingerprint, utc_now().isoformat(), analysis,
                                               model=meta.get("model"), provider=meta.get("provider"))
                    usage = meta.get("usage", {})
                    self._journal("god_eye.enrichment.completed", "success", provider=meta.get("provider"),
                                  model=meta.get("model"), input_tokens=usage.get("prompt_tokens") or usage.get("input_tokens"),
                                  output_tokens=usage.get("completion_tokens") or usage.get("output_tokens"))
                    enrichment_status = "created"
                except Exception as error:
                    analysis, enrichment_status = None, "failed"
                    self._journal("god_eye.enrichment.failed", "error", error_category=type(error).__name__)
        outcomes, forecasts = self._derive_event_products(event, analysis)
        return {"event": event, "enrichment_status": enrichment_status, "analysis": analysis,
                "outcomes": outcomes, "forecasts": forecasts}

    def _derive_event_products(self, event: dict[str, object], analysis: dict[str, object] | None) -> tuple[list[dict], list[dict]]:
        outcomes, forecasts = [], []
        published = datetime.fromisoformat(str(event["published_at"]))
        for instrument in event.get("instruments", []):
            candles = self._best_history(str(instrument))
            if not candles:
                continue
            outcome = reaction_features(str(event["event_id"]), str(instrument), published, candles)
            self.store.save_outcome(outcome); outcomes.append(public(outcome))
            try:
                features = build_feature_set(str(instrument), candles, event, analysis, as_of=utc_now())
            except ValueError:
                continue
            intelligence = self.analyze_market(str(instrument), as_of=features.as_of, persist=True)
            primary_features = intelligence.get("features", {}).get("1h") or next(
                iter(intelligence.get("features", {}).values()), {})
            features = replace(features,
                quantitative_features=dict(primary_features.get("values", {})),
                patterns=tuple(intelligence.get("patterns", [])),
                multi_timeframe_context=dict(intelligence.get("multi_timeframe_context", {})),
                microstructure=dict(intelligence.get("microstructure", {})),
                intelligence_quality=float(intelligence.get("quality", {}).get("quality_score", 0)))
            for horizon in ("1h", "24h", "7d"):
                local = classify_regime(candles)
                histories = {symbol: self._best_history(symbol) for symbol in self.instruments}
                global_value = global_regime({k: v for k, v in histories.items() if v})
                regime_payload = {"instrument": instrument, "local_regime": local["regime"],
                                  "global_regime": global_value["regime"], "detected_at": features.as_of.isoformat()}
                self.store.save_regime(f"{instrument}|{features.as_of.isoformat()}", regime_payload, features.as_of.isoformat())
                self._journal("god_eye.regime.detected", "success")
                all_events = self.store.events(limit=500)
                outcome_map = {str(e["event_id"]): self.store.outcomes_for_event(str(e["event_id"])) for e in all_events}
                matches = retrieve_similar(event, all_events, outcome_map, horizon=horizon, as_of=features.as_of)
                history_features = similar_features(matches)
                self._journal("god_eye.similar_events.retrieved", "success")
                enriched = replace(features, market_regime=str(local["regime"]), local_regime=str(local["regime"]),
                    global_regime=str(global_value["regime"]), similar_historical_reactions=tuple(m["event_id"] for m in matches),
                    similar_event_count=history_features["count"], similar_return_mean=history_features["mean_return"],
                    similar_return_median=history_features["median_return"],
                    similar_positive_ratio=history_features["positive_ratio"], similar_downside=history_features["downside"],
                    similarity_confidence=history_features["confidence"])
                calibration = self._calibration_for(self.forecast_engine.model_id, horizon)
                forecast = self.forecast_engine.forecast(enriched, horizon, calibration)
                if self.store.save_forecast(forecast):
                    self._journal("god_eye.forecast.created", "success")
                opportunity = build_opportunity(public(forecast), downside=history_features["downside"],
                    volatility=enriched.volatility, data_quality=min(enriched.source_quality, 1.0),
                    liquidity=min(1.0, enriched.relative_volume) if enriched.relative_volume is not None else None,
                    minimum_samples=self.minimum_calibration_samples)
                self.store.save_opportunity(opportunity, features.as_of.isoformat())
                self._journal("god_eye.opportunity.created" if opportunity["status"] == "eligible" else
                              "god_eye.opportunity.rejected", "success", error_category=(None if opportunity["status"] == "eligible" else ",".join(opportunity["reasons"])))
                if opportunity["status"] == "eligible" and candles:
                    decision = self.paper_portfolio.open(opportunity, float(candles[0]["close"]), features.as_of)
                    self._journal("god_eye.paper.entry" if decision["status"] == "opened" else "god_eye.paper.rejection",
                                  "success", error_category=(None if decision["status"] == "opened" else ",".join(decision["reasons"])))
                forecasts.append(public(forecast))
        return outcomes, forecasts

    def _best_history(self, instrument: str) -> list[dict[str, object]]:
        for interval in ("1m", "5m", "1h", "1d"):
            candles = self.store.history(instrument, interval=interval, limit=2000)
            if candles:
                return candles
        return []

    def _ensure_forecast(self, instrument: str, now: datetime) -> dict[str, object] | None:
        horizon="1h"; calibration=self._calibration_for(self.forecast_engine.model_id,horizon)
        calibration_version=getattr(calibration,"version",None) if calibration else None
        current=self.store.compatible_forecast(instrument=instrument,horizon=horizon,
            model_id=self.forecast_engine.model_id,model_version=self.forecast_engine.model_version,
            calibration_version=calibration_version,
            created_after=(now-timedelta(seconds=self.star_finder_config.maximum_age_seconds)).isoformat())
        if current:return current
        history=self._best_history(instrument)
        if len(history)<2:return None
        event={"event_type":"market_scan","source_quality_score":1.0,"novelty_score":0.0}
        features=build_feature_set(instrument,history,event,as_of=now)
        forecast=self.forecast_engine.forecast(features,horizon,calibration)
        self.store.save_forecast(forecast)
        self._journal("god_eye.forecast.automatic","success")
        return self.store.forecast(forecast.forecast_id)

    def _persist_trader_state(self, at: datetime) -> None:
        self.store.save_trader_state(self.trader.portfolio_id,at.isoformat(),self.trader.state())

    def _auto_paper(self, opportunities: list[dict[str, object]], now: datetime) -> list[dict[str, object]]:
        if self.trader.config.mode != TraderMode.AUTO_PAPER:return []
        actions=[]
        for opportunity in opportunities:
            if opportunity.get("status")!="QUALIFIED":continue
            if opportunity.get("risk_approved") is False or self.trader_kill_switch: continue
            price=float(opportunity["reference_price"]); notional=self.trader.size(opportunity)
            cost=dict(opportunity.get("cost_estimate",{}))
            if not cost.get("known",False) and cost.get("config",{}).get("unknown_policy")=="degrade":
                notional *= max(0.1, 1-float(cost.get("cost_uncertainty",1)))
            if notional<self.star_finder_config.minimum_notional:continue
            key=f"auto|{opportunity['opportunity_id']}|ENTER"
            request={"idempotency_key":key,"instrument":opportunity["instrument"],"side":"buy" if opportunity["direction"]=="up" else "sell",
                "quantity":notional/price,"price":price,"asset_class":opportunity["asset_class"],"venue":opportunity.get("venue"),
                "strategy":"NOVA_COMPOSITE","opportunity_id":opportunity["opportunity_id"],"model_version":opportunity.get("model_version")}
            result=self.execution_engine.execute(request,microstructure=self._microstructure.get(str(opportunity["instrument"])),available_cash=self.trader.cash,kill_switch=self.trader_kill_switch,at=now)
            order=result.get("order",{}); self.store.save_trader_record("order",str(order.get("order_id",key)),str(order.get("created_at",now.isoformat())),self.trader.portfolio_id,order)
            for fill in result.get("fills",[]):
                self.store.save_trader_record("fill",str(fill["fill_id"]),str(fill["timestamp"]),self.trader.portfolio_id,fill)
                self.trader.apply_fill(fill,asset_class=str(opportunity["asset_class"])); self._mirror_strategy_fill(fill,str(opportunity["asset_class"])); attribution=approximate_attribution(opportunity,fill)
                self.store.save_trader_record("attribution",str(fill["fill_id"]),str(fill["timestamp"]),self.trader.portfolio_id,attribution)
            if result.get("fills"):
                self.store.save_lifecycle(lifecycle_transition(str(opportunity["opportunity_id"]),"QUALIFIED","ENTER",reasons=["auto_paper_fill"],at=now,trade_ref=str(order.get("order_id"))))
                self.store.save_lifecycle(lifecycle_transition(str(opportunity["opportunity_id"]),"ENTER","OPEN",reasons=["paper_position_open"],at=now,
                    portfolio_ref=self.trader.portfolio_id,trade_ref=str(order.get("order_id"))))
                history=self._best_history(str(opportunity["instrument"])); snapshot=max(history,key=lambda v:str(v["opened_at"])) if history else {}
                self._save_replay_stage("market_snapshot",str(opportunity["opportunity_id"]),now,snapshot)
                forecast=self.store.forecast(str(opportunity.get("forecast_id",""))) or {"forecast_id":opportunity.get("forecast_id"),"model_version":opportunity.get("model_version")}
                self._save_replay_stage("forecast",str(opportunity.get("forecast_id",opportunity["opportunity_id"])),now,forecast)
                self._save_replay_stage("opportunity",str(opportunity["opportunity_id"]),now,opportunity)
                self._save_replay_stage("decision",f"{opportunity['opportunity_id']}|ENTER",now,{"opportunity_id":opportunity["opportunity_id"],"action":"ENTER"})
                self._save_replay_stage("order",str(order.get("order_id",key)),now,order)
                for fill in result.get("fills",[]): self._save_replay_stage("fill",str(fill["fill_id"]),now,fill)
                self._save_replay_stage("portfolio_transition",f"{opportunity['opportunity_id']}|OPEN",now,
                    {"opportunity_id":opportunity["opportunity_id"],"status":"OPEN","portfolio":self.trader.public()})
            actions.append(result)
        self._persist_trader_state(now);return actions

    def _save_replay_stage(self,kind:str,key:str,at:datetime,value:dict[str,object])->None:
        payload={"kind":kind,"payload":dict(value),"timestamp":at.isoformat()}
        self.store.save_trader_record("replay",f"{kind}|{key}",at.isoformat(),self.trader.portfolio_id,payload)

    def _mirror_strategy_fill(self,fill:dict[str,object],asset_class:str)->None:
        """Apply identical point-in-time execution assumptions to isolated research ledgers."""
        for name,ledger in self.strategy_ledgers.items():
            if name=="NOVA_COMPOSITE": continue
            shadow={**fill,"fill_id":sha256(f"{fill['fill_id']}|{name}".encode()).hexdigest(),
                "order_id":sha256(f"{fill['order_id']}|{name}".encode()).hexdigest(),"strategy":name}
            before=len(ledger.closed); ledger.apply_fill(shadow,asset_class=asset_class)
            self.store.save_trader_record("fill",str(shadow["fill_id"]),str(shadow["timestamp"]),name,shadow)
            if len(ledger.closed)>before:
                trade={**ledger.closed[-1],"timestamp":ledger.closed[-1]["closed_at"],"strategy":name,"portfolio_id":name,
                    "asset_class":asset_class}
                self.store.save_trader_record("trade",str(trade["position_id"]),str(trade["closed_at"]),name,trade)
            self.store.save_trader_state(name,str(shadow["timestamp"]),ledger.state())

    def reevaluate_paper_positions(self,now:datetime|None=None)->dict[str,object]:
        """Reevaluate every open Trader position and execute only deterministic paper actions."""
        at=as_utc(now or utc_now()); actions=[]
        for position in list(self.trader.positions.values()):
            history=self._best_history(position.instrument)
            eligible=[v for v in history if as_utc(v["opened_at"])<=at]
            if not eligible: continue
            latest=max(eligible,key=lambda row:str(row["opened_at"])); price=float(latest["close"])
            self.trader.mark({position.instrument:price},at)
            opportunity=self.store.star_opportunity(position.opportunity_id)
            decision=position_action(position,opportunity,risk_ok=self.trader.risk_budget()["state"]!="halt-new-entries",now=at,config=self.trader.config)
            decision.update(position_id=position.position_id,opportunity_id=position.opportunity_id,instrument=position.instrument,timestamp=at.isoformat())
            self.store.save_decision(decision,at.isoformat()); self._save_replay_stage("decision",f"{position.position_id}|{at.isoformat()}",at,decision)
            action=str(decision["action"]); previous=self.store.lifecycle(position.opportunity_id)
            current=str(previous[-1]["to_status"]) if previous else "OPEN"
            if action=="HOLD":
                if current in {"OPEN","HOLD","ADD","REDUCE"}: self.store.save_lifecycle(lifecycle_transition(position.opportunity_id,current,"HOLD",reasons=[str(decision["reason"])],at=at))
                actions.append({"decision":decision,"execution":None}); continue
            quantity=abs(position.quantity) * (.5 if action=="REDUCE" else 1.0 if action=="EXIT" else .25)
            side=("buy" if position.quantity<0 else "sell") if action in {"REDUCE","EXIT"} else ("buy" if position.quantity>0 else "sell")
            key=f"auto|{position.opportunity_id}|{action}|{at.isoformat()}"
            result=self.execution_engine.execute({"idempotency_key":key,"instrument":position.instrument,"side":side,
                "quantity":quantity,"price":price,"asset_class":position.asset_class,"venue":position.venue,
                "strategy":position.strategy,"opportunity_id":position.opportunity_id},
                microstructure=self._microstructure.get(position.instrument),available_cash=max(self.trader.cash,quantity*price*1.1),at=at)
            order=result.get("order",{}); self.store.save_trader_record("order",str(order.get("order_id",key)),str(order.get("created_at",at.isoformat())),self.trader.portfolio_id,order)
            before=len(self.trader.closed)
            for fill in result.get("fills",[]):
                self.store.save_trader_record("fill",str(fill["fill_id"]),str(fill["timestamp"]),self.trader.portfolio_id,fill)
                self.trader.apply_fill(fill,asset_class=position.asset_class); self._mirror_strategy_fill(fill,position.asset_class); self._save_replay_stage("fill",str(fill["fill_id"]),at,fill)
            if result.get("fills"):
                self.store.save_lifecycle(lifecycle_transition(position.opportunity_id,current,action,reasons=[str(decision["reason"])],at=at,trade_ref=str(order.get("order_id"))))
                if action=="EXIT" and len(self.trader.closed)>before:
                    closed=dict(self.trader.closed[-1]); closed.update(timestamp=closed["closed_at"],asset_class=position.asset_class,
                        horizon=str((opportunity or {}).get("horizon","1h")),regime=str((opportunity or {}).get("regime","unknown")))
                    self.store.save_trader_record("trade",position.position_id,closed["closed_at"],self.trader.portfolio_id,closed)
                    self.store.save_lifecycle(lifecycle_transition(position.opportunity_id,"EXIT","CLOSED",reasons=["full_paper_fill"],at=at,trade_ref=position.position_id))
                    due=(at+timedelta(hours=1)).isoformat(); pending={**closed,"status":"pending","due_at":due,"attempts":0}
                    self.store.save_trader_record("outcome_pending",position.position_id,at.isoformat(),self.trader.portfolio_id,pending)
                    self._save_replay_stage("portfolio_transition",f"{position.opportunity_id}|CLOSED",at,{"opportunity_id":position.opportunity_id,"status":"CLOSED"})
            actions.append({"decision":decision,"execution":result})
        self._persist_trader_state(at)
        return {"status":"ok","processed":len(actions),"actions":actions,"paper_only":True}

    def resolve_due_outcomes(self,now:datetime|None=None)->dict[str,object]:
        """Resolve due closed trades and sampled missed decisions without future data."""
        at=as_utc(now or utc_now()); resolved=0; retried=0
        existing={str(v.get("position_id") or v.get("opportunity_id")) for v in self.store.trader_records("outcome",limit=10000)}
        retries=self.store.trader_records("outcome_retry",limit=10000); exhausted=0
        pending_rows=self.store.trader_records("outcome_pending",limit=10000)
        pending_rows.sort(key=lambda v:(str(v.get("due_at","")),str(v.get("position_id",""))))
        for pending in pending_rows:
            key=str(pending.get("position_id"));
            if key in existing or as_utc(pending["due_at"])>at: continue
            history=[v for v in self._best_history(str(pending.get("position_id","")).split("|",1)[-1]) if as_utc(v["opened_at"])<=at]
            if not history:
                attempts=sum(v.get("position_id")==key for v in retries)
                if attempts>=3: exhausted+=1; continue
                status="exhausted" if attempts>=2 else "retry_scheduled"
                retry={"position_id":key,"attempt":attempts+1,"status":status,"attempted_at":at.isoformat(),
                    "next_due_at":None if status=="exhausted" else (at+timedelta(minutes=15*(2**attempts))).isoformat(),"paper_only":True}
                self.store.save_trader_record("outcome_retry",f"{key}|{attempts+1}",at.isoformat(),self.trader.portfolio_id,retry)
                retried+=1
                if status=="exhausted": exhausted+=1
                continue
            after=[v for v in history if as_utc(v["opened_at"])>=as_utc(pending["closed_at"])]
            prices=[float(v["close"]) for v in after]
            entry=float(pending.get("entry_price",0)); exit_price=float(pending.get("exit_price",0))
            direction=int(pending.get("direction",1))
            outcome={**pending,"status":"resolved","resolved_at":at.isoformat(),"net_result":float(pending["realized_pnl"]),
                "mfe":float(pending.get("mfe",0)),"mae":float(pending.get("mae",0)),
                "capture_ratio":pending.get("exit_capture_ratio"),
                "entry_quality":((float(history[0]["close"])-entry)/entry*direction if entry else None),
                "exit_quality":((max(prices)-exit_price)/exit_price*direction if prices and exit_price else None),
                "post_exit_bounded_movement":({"minimum":min(prices),"maximum":max(prices)} if prices else None),"paper_only":True}
            inserted=self.store.save_trader_record("outcome",key,at.isoformat(),self.trader.portfolio_id,outcome)
            if not inserted: continue
            oid=str(pending["opportunity_id"]); self.store.save_lifecycle(lifecycle_transition(oid,"CLOSED","RESOLVED",reasons=["scheduled_outcomes_collected"],at=at,trade_ref=key))
            self._save_replay_stage("outcome",key,at,outcome); resolved+=1
            attribution={"method":"realized_analytical_v1","causal":False,"opportunity_id":oid,"components":{
                "entry_timing":outcome["entry_quality"],"exit_timing":outcome["exit_quality"],"sizing":pending.get("closed_quantity"),
                "fees":-float(pending.get("fees",0)),"slippage":-float(pending.get("slippage",0))},"paper_only":True}
            self.store.save_trader_record("attribution",f"{key}|resolved",at.isoformat(),self.trader.portfolio_id,attribution)
        for decision in self.store.decisions(limit=500):
            if decision.get("action") not in {"WAIT","IGNORE","REJECTED"}: continue
            if decision.get("due_at") and as_utc(decision["due_at"])>at: continue
            oid=str(decision.get("opportunity_id") or ""); marker=f"missed|{oid}|{decision.get('timestamp','')}"
            if marker in existing: continue
            opportunity=self.store.star_opportunity(oid); history=self._best_history(str(decision.get("instrument") or (opportunity or {}).get("instrument","")))
            if not opportunity or len(history)<2: continue
            ordered=sorted((v for v in history if as_utc(v["opened_at"])<=at),key=lambda v:str(v["opened_at"]))
            if len(ordered)<2: continue
            outcome=missed_outcome(decision,float(ordered[-2]["close"]),float(ordered[-1]["close"]),costs=float(opportunity.get("expected_costs",0) or 0))
            outcome.update(position_id=marker,resolved_at=at.isoformat()); self.store.save_trader_record("outcome",marker,at.isoformat(),self.trader.portfolio_id,outcome); resolved+=1
        return {"status":"degraded" if exhausted else "ok","resolved":resolved,"retry_pending":retried,
                "exhausted_needs_data":exhausted,"paper_only":True}

    def _advance_paper(self) -> None:
        hours = {"5m": 1/12, "1h": 1, "4h": 4, "24h": 24, "7d": 168}
        for position_id, position in list(self.paper_portfolio.positions.items()):
            candles = self._best_history(position.instrument)
            if not candles: continue
            latest = max(candles, key=lambda row: str(row["opened_at"]))
            at = datetime.fromisoformat(str(latest["opened_at"]))
            if at >= position.entry_at + timedelta(hours=hours.get(position.horizon, 24)):
                trade = self.paper_portfolio.close(position_id, float(latest["close"]), at)
                if trade:
                    self.store.save_paper_trade(trade)
                    self._journal("god_eye.paper.exit", "success")
            else:
                self.paper_portfolio.snapshot(at, {position.instrument: float(latest["close"])})
                self._journal("god_eye.portfolio.snapshot", "success")

    def evaluate_forecasts(self) -> int:
        evaluated = 0
        for forecast in self.store.forecasts(limit=500):
            if forecast.get("status") == "evaluated":
                continue
            value = evaluate_forecast(forecast, self._best_history(str(forecast["instrument"])))
            if value and self.store.save_evaluation(str(forecast["forecast_id"]), value):
                evaluated += 1
                self._journal("god_eye.forecast.evaluated", "success")
        return evaluated

    def get_forecasts(self, limit: int = 100, instrument: str | None = None,
                      start: str | None = None, end: str | None = None) -> dict[str, object]:
        return {"forecasts": self.store.forecasts(limit=limit, instrument=instrument, start=start, end=end)}

    def get_forecast(self, forecast_id: str) -> dict[str, object] | None:
        return self.store.forecast(forecast_id)

    def get_performance(self) -> dict[str, object]:
        return {"metrics": self.store.performance()}

    def rebuild_calibrations(self) -> list[dict[str, object]]:
        rows = self.store.resolved_forecasts(limit=5000)
        keys = sorted({(str(r["model_id"]), str(r["horizon"])) for r in rows})
        result = []
        for model_id, horizon in keys:
            model = build_calibration(rows, model_id, horizon, minimum_samples=self.minimum_calibration_samples)
            if model is None:
                self._journal("god_eye.calibration.rejected", "success", error_category="insufficient_samples")
                continue
            metrics = calibration_metrics(model, rows)
            value = {**asdict(model), "calibration_version": model.version, "metrics": metrics}
            self.store.save_calibration(value, utc_now().isoformat()); result.append(value)
            self._journal("god_eye.calibration.built", "success", model=model_id)
        return result

    def _calibration_for(self, model_id: str, horizon: str):
        rows = self.store.resolved_forecasts(limit=5000)
        return build_calibration(rows, model_id, horizon, minimum_samples=self.minimum_calibration_samples)

    def get_calibration(self) -> dict[str, object]:
        calibrations = self.rebuild_calibrations() or self.store.calibrations()
        for value in calibrations:
            value["quality_tier"] = calibration_quality_tier(int(value.get("sample_count", 0)))
        return {"minimum_sample_count": self.minimum_calibration_samples,
                "quality_thresholds": {"insufficient": "<20", "low": "20-49", "medium": "50-199", "high": ">=200"},
                "calibrations": calibrations}

    def get_regimes(self) -> dict[str, object]:
        return {"regimes": self.store.regimes()}

    def get_similar_events(self, event_id: str, horizon: str = "24h", top_k: int = 5) -> dict[str, object] | None:
        event = self.store.event(event_id)
        if event is None: return None
        events = self.store.events(limit=500)
        outcomes = {str(e["event_id"]): self.store.outcomes_for_event(str(e["event_id"])) for e in events}
        matches = retrieve_similar(event, events, outcomes, horizon=horizon,
                                   as_of=datetime.fromisoformat(str(event["published_at"])), top_k=top_k)
        return {"event_id": event_id, "horizon": horizon, "matches": matches, "features": similar_features(matches)}

    def get_opportunities(self, include_rejected: bool = False, limit: int = 100) -> dict[str, object]:
        return {"opportunities": self.store.opportunities(include_rejected=include_rejected, limit=limit)}

    def get_opportunity(self, opportunity_id: str) -> dict[str, object] | None:
        return self.store.opportunity(opportunity_id)

    def get_portfolio(self) -> dict[str, object]:
        return self.get_trader_portfolio()

    def get_portfolio_trades(self, limit: int = 500) -> dict[str, object]:
        return {"trades": self.store.trader_records("trade",limit=limit),"paper_only":True}

    def get_walk_forward(self) -> dict[str, object]:
        opportunities = self.store.opportunities(include_rejected=True, limit=500)
        prices = {}
        for symbol in self.instruments:
            rows = self.store.history(symbol, interval="1d", limit=2000)
            prices[symbol] = [(datetime.fromisoformat(str(r["opened_at"])), float(r["close"])) for r in rows]
        result = walk_forward(opportunities, prices, self.paper_portfolio.config)
        self._journal("god_eye.walk_forward.run", "success")
        return public(result)

    def get_benchmarks(self) -> dict[str, object]:
        result = self.get_walk_forward()
        self._journal("god_eye.benchmark.comparison", "success")
        return {"benchmarks": result["benchmarks"], "strategy_metrics": result["metrics"]}

    def continuous_evaluation(self) -> dict[str, object]:
        champion=self.sync_trader_champion(); resolved=self.evaluate_forecasts(); calibrations=self.rebuild_calibrations(); self._advance_paper(); repairs=self.repair_data_gaps()
        self.store.record_run("evaluation","success",utc_now().isoformat())
        return {"status":"success","champion":champion,"forecasts_resolved":resolved,"calibrations_updated":len(calibrations),"gap_repair":repairs}

    @staticmethod
    def _validated_trader_config(champion: dict[str, object], base: TraderConfig) -> TraderConfig:
        raw = dict(champion.get("trader_config") or {})
        forbidden = {"mode", "version"}; supported = set(TraderConfig.__dataclass_fields__) - forbidden
        unknown = set(raw) - supported
        if unknown or any(key in raw for key in forbidden): raise ValueError("unsupported_trader_config")
        candidate = replace(TraderConfig(mode=base.mode), **raw, version=str(champion.get("version") or ""))
        fractions = (candidate.max_position_fraction, candidate.max_gross_exposure,
                     candidate.cash_reserve_fraction, candidate.liquidity_fraction)
        drawdowns = (candidate.warning_drawdown, candidate.defensive_drawdown, candidate.halt_drawdown)
        if (not candidate.version or not all(0 < float(v) <= 1 for v in fractions)
                or not (0 <= drawdowns[0] < drawdowns[1] < drawdowns[2] <= .30)
                or candidate.max_position_fraction > .25 or candidate.action_cooldown_seconds < 0
                or not (0 < candidate.minimum_action_fraction <= 1)):
            raise ValueError("invalid_trader_config_bounds")
        return candidate

    def sync_trader_champion(self, now: datetime | None = None) -> dict[str, object]:
        """Apply the latest accepted activation/rollback without rewriting open positions."""
        at = as_utc(now or utc_now())
        champions = {str(v.get("version")): v for v in self.store.governance("trader_champion")
                     if v.get("status") == "active" and v.get("version")}
        intents = [(str(v.get("activated_at") or v.get("created_at") or ""), "activation", v)
                   for v in champions.values()]
        intents += [(str(v.get("rolled_back_at") or ""), "rollback", v)
                    for v in self.store.governance("rollback") if v.get("rollback_target")]
        if not intents: return {"status":"unchanged","version":self.trader.config.version,"paper_only":True}
        _, reason, intent = max(intents, key=lambda item:item[0])
        target = str(intent.get("rollback_target")) if reason == "rollback" else str(intent.get("version"))
        champion = champions.get(target)
        if champion is None:
            return {"status":"rejected","reason":"champion_version_not_found","version":self.trader.config.version,"paper_only":True}
        if target == self.trader.config.version:
            return {"status":"unchanged","version":target,"paper_only":True}
        try: candidate = self._validated_trader_config(champion, self.trader.config)
        except (TypeError, ValueError) as error:
            return {"status":"rejected","reason":str(error),"version":self.trader.config.version,"paper_only":True}
        old = self.trader.config.version; state = self.trader.state()
        state["config"] = {**state["config"], **{k:v for k,v in candidate.__dict__.items() if k != "mode"},
                           "mode":self.trader.config.mode.value}
        audit = {"kind":"trader_champion_applied","old_version":old,"new_version":target,
                 "campaign":champion.get("campaign"),"reason":intent.get("reason") or reason,
                 "applied_at":at.isoformat(),"paper_only":True}
        self.store.persist_champion_application(self.trader.portfolio_id,at.isoformat(),state,audit)
        self.trader.config = replace(candidate,mode=self.trader.config.mode)
        return {"status":"applied","old_version":old,"version":target,"paper_only":True}

    def repair_data_gaps(self,max_instruments:int=5)->dict[str,int]:
        totals={"repaired":0,"failed":0,"pending":0,"calls":0}; repair=GapRepair(self.store,max_gaps=5,max_calls=2)
        for instrument in list(self.instruments.values())[:max_instruments]:
            providers=self.crypto_providers if instrument.kind==InstrumentType.CRYPTO else [self.yahoo_provider]
            if not providers:continue
            for interval in self.candle_intervals:
                if len(self.store.history(instrument.symbol,interval=interval,limit=2000))<2:continue
                result=repair.run(instrument,interval,providers[0])
                for key in totals:totals[key]+=int(result[key])
        return totals

    def get_alerts(self,limit:int=100)->dict[str,object]: return {"alerts":self.store.alerts(limit)}

    def acknowledge_alert(self,alert_id:str)->dict[str,object]:
        return {"acknowledged":self.store.acknowledge_alert(alert_id,utc_now().isoformat())}

    def get_governance(self)->dict[str,object]: return {"items":self.store.governance(),"model_version":self.forecast_engine.model_version,
                                                        "ensemble_version":self.forecast_engine.engine_version}

    def get_influence_graph(self)->dict[str,object]:
        return confirmation_graph(self.store.events(limit=500),self.store.social_posts(500))

    def get_research(self,kind:str)->dict[str,object]: return {"items":self.store.governance(kind)}
    def get_live_forward(self)->dict[str,object]:
        runs = self.store.live_forward_runs(); return {"runs": runs, "current": next((v for v in runs if v["status"] == "running"), None)}
    def get_alternative_health(self)->dict[str,object]:
        caps = capability_status(self.sec_config, self.macro_configs) if (self.sec_config or self.macro_configs) else dict(ALTERNATIVE_CAPABILITIES)
        for name in self.alternative_collectors: caps.setdefault(name, {}).update(active=True, configured=True)
        stats = {row["source_id"]: row for row in self.store.operational_health()["source_stats"]}
        return {"sources": caps, "health": {name: stats.get(name, {"source_id": name, "status": "never"}) for name in caps}}
    def get_data_integrity(self)->dict[str,object]:
        values=[]
        for symbol in self.instruments:
            rows=self.store.history(symbol,limit=2000)
            if rows: values.append({"instrument":symbol,**assess_candles(rows,str(rows[0].get("interval","1d")))})
        return {"instruments":values,"backfill_checkpoints":self.store.backfill_checkpoints()}

    def _timeframe_rows(self, instrument: str, timeframe: str, as_of: datetime) -> list[dict[str, object]]:
        direct = self.store.history(instrument, interval=timeframe, limit=2000)
        if direct:
            return direct
        for source in ("1m", "5m", "1h", "1d"):
            if source == timeframe: continue
            rows = self.store.history(instrument, interval=source, limit=2000)
            if rows and {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440}[source] < {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440}[timeframe]:
                return aggregate_candles(rows, timeframe, as_of=as_of)
        return []

    def refresh_microstructure(self, symbol: str) -> dict[str, object]:
        instrument = self.instruments.get(symbol)
        if instrument is None or instrument.kind != InstrumentType.CRYPTO:
            value = {"status": "unavailable", "instrument": symbol, "reason": "unsupported_asset_class"}
        else:
            value = {"status": "unavailable", "instrument": symbol, "reason": "provider_unavailable"}
            for provider in self.crypto_providers:
                method = getattr(provider, "microstructure", None)
                if not callable(method): continue
                try:
                    value = parse_microstructure(provider.name, method(instrument), instrument=symbol, observed_at=utc_now())
                    if value["status"] == "available": break
                except Exception as error:
                    value = {"status": "unavailable", "instrument": symbol, "provider": provider.name,
                             "reason": classify_provider_error(error), "observed_at": utc_now().isoformat()}
        self._microstructure[symbol] = value
        return value

    def analyze_market(self, instrument: str, *, as_of: datetime | None = None, persist: bool = False) -> dict[str, object]:
        if instrument not in self.instruments: raise KeyError("instrument_not_found")
        as_of = as_of or utc_now(); features = {}
        for timeframe in ("1m", "5m", "15m", "1h", "4h", "1d"):
            rows = self._timeframe_rows(instrument, timeframe, as_of)
            value = quantitative_features(rows, instrument=instrument, timeframe=timeframe, as_of=as_of)
            if value["quality"] != "insufficient": features[timeframe] = value
        patterns = []
        for timeframe, feature in features.items():
            patterns.extend(self.pattern_engine.detect(feature))
            patterns.extend(quantitative_divergences(self._timeframe_rows(instrument, timeframe, as_of),
                instrument=instrument, timeframe=timeframe, as_of=as_of))
        context = multi_timeframe_context(features)
        primary = features.get("1h") or features.get("5m") or next(iter(features.values()), {})
        regime = classify_regime_v2(primary)
        micro = self._microstructure.get(instrument, {"status": "unavailable", "instrument": instrument})
        relative_volume = primary.get("values", {}).get("relative_volume") if primary else None
        liquidity = liquidity_metrics(micro, relative_volume)
        quality = intelligence_quality(freshness=1.0, integrity=1.0 if features else 0.0,
            source_diversity=min(1.0, len(self.source_configs)/5), sample_count=int(primary.get("sample_count", 0)) if primary else 0,
            calibration_quality=0.0, regime_confidence=float(regime["confidence"]),
            microstructure_available=micro.get("status") == "available", contradictions=bool(context["conflict"]))
        value = {"instrument": instrument, "as_of": as_utc(as_of).isoformat(), "features": features,
                 "patterns": patterns, "multi_timeframe_context": context, "regime": regime,
                 "microstructure": micro, "liquidity": liquidity, "quality": quality,
                 "point_in_time": True, "version": "market-intelligence-v4"}
        if persist:
            key = sha256(f"{instrument}|{value['as_of']}|market-intelligence-v4".encode()).hexdigest()
            self.store.save_intelligence(key, instrument, str(value["as_of"]), "market_intelligence", value)
        return value

    def run_scanner(self, symbols: list[str] | None = None, *, priority: list[str] | None = None) -> dict[str, object]:
        now=utc_now(); candidates=[]
        for symbol in symbols or list(self.instruments):
            if symbol not in self.instruments: continue
            intelligence=self.analyze_market(symbol,as_of=now)
            primary=intelligence["features"].get("1h") or intelligence["features"].get("5m") or next(iter(intelligence["features"].values()),None)
            if primary:
                candidates.append({**primary,"freshness":1.0,"liquidity":float(intelligence["liquidity"].get("liquidity_score") or .5),
                                   "event_activity":0.0})
        result=self.market_scanner.scan(candidates,now=now,priority=priority or [])
        for selected in result["stage_b"]:
            self.analyze_market(str(selected["instrument"]),as_of=now,persist=True)
        self._scanner_result=result
        return result

    def get_scanner(self)->dict[str,object]: return dict(self._scanner_result)

    def _event_fusion_for(self, instrument: str, now: datetime, intelligence: dict[str, object] | None = None) -> dict[str, object]:
        evidence=[]
        for event in self.store.events(limit=200):
            if instrument in event.get("instruments", []):
                for ref in event.get("evidence_refs", []) or event.get("source_ids", []):
                    evidence.append({"source_id":ref,"origin_id":ref,"published_at":event["published_at"],
                        "source_quality":event.get("source_quality_score",0),"kind":"news_or_corporate"})
        for signal in self.store.social_signals(limit=200):
            if instrument in signal.get("instruments", []) or instrument in signal.get("tickers", []):
                evidence.append({"source_id":signal.get("platform","social"),"origin_id":signal.get("signal_id"),
                    "published_at":signal.get("detected_at",now),"source_quality":signal.get("quality",.3),"kind":"social"})
        for item in self.store.alternative_items(limit=200):
            if instrument in item.get("instruments", []):
                evidence.append({"source_id":item.get("source"),"origin_id":item.get("item_id"),
                    "published_at":item.get("published_at",now),"source_quality":item.get("source_quality",.8),"kind":"filing_or_macro"})
        if intelligence:
            for index, pattern in enumerate(intelligence.get("patterns", [])):
                evidence.append({"source_id":"pattern_engine","origin_id":f"pattern:{instrument}:{index}:{pattern.get('detected_at')}",
                    "published_at":pattern.get("detected_at",now),"source_quality":pattern.get("strength",.5),"kind":"pattern_or_market_anomaly"})
            micro=intelligence.get("microstructure", {})
            if micro.get("status")=="available":
                evidence.append({"source_id":micro.get("provider","microstructure"),"origin_id":f"microstructure:{instrument}:{micro.get('observed_at')}",
                    "published_at":micro.get("observed_at",now),"source_quality":.8,"kind":"microstructure"})
        return fuse_event_evidence(evidence, now=now)

    def run_star_finder(self, symbols: list[str] | None = None) -> dict[str, object]:
        now=utc_now(); scan=self.run_scanner(symbols, priority=[p.instrument for p in self.trader.positions.values()])
        built=[]
        for selected in scan.get("stage_b", []):
            symbol=str(selected["instrument"]); intelligence=self.analyze_market(symbol,as_of=now,persist=True)
            instrument=self.instruments[symbol]; history=self._best_history(symbol)
            if not history: continue
            latest=max(history,key=lambda row:str(row["opened_at"])); reference=float(latest["close"])
            forecast=self._ensure_forecast(symbol,now)
            if not forecast: continue
            venue=(intelligence.get("microstructure") or {}).get("provider") or instrument.exchange
            notional=min(self.star_finder_config.capital*.1,1000.0)
            cost=self.cost_engine.estimate(asset_class=instrument.kind.value,venue=str(venue) if venue else None,
                size=notional,price=reference,microstructure=intelligence.get("microstructure"))
            returns=[float(f["evaluation"]["actual_return"]) for f in self.store.resolved_forecasts(limit=5000)
                     if f.get("instrument")==symbol and f.get("evaluation",{}).get("actual_return") is not None]
            net=expected_net_return(forecast,cost,historical_returns=returns); fusion=self._event_fusion_for(symbol,now,intelligence)
            age=(now-as_utc(forecast["created_at"])).total_seconds(); liquidity=float(intelligence["liquidity"].get("liquidity_score") or .5)
            relevant_providers=self.crypto_providers if instrument.kind==InstrumentType.CRYPTO else [self.yahoo_provider]
            known_health=[self._provider_health.get(p.name,{}) for p in relevant_providers]
            observed_health=[h for h in known_health if h]
            unavailable=bool(observed_health) and all(h.get("status")=="degraded" for h in observed_health)
            provider_reasons=[f"{p.name}:{self._provider_health.get(p.name,{}).get('last_error')}" for p in relevant_providers
                              if self._provider_health.get(p.name,{}).get("status")=="degraded"]
            risk=self.trader.risk_budget(uncertainty=float(net["uncertainty"])); risk_approved=bool(risk["new_entries_allowed"]) and not self.trader_kill_switch
            opportunity={"opportunity_id":sha256(f"{forecast['forecast_id']}|{cost['config_version']}".encode()).hexdigest(),
                "instrument":symbol,"asset_class":instrument.kind.value,"venue":venue,"detected_at":now.isoformat(),
                "created_at":now.isoformat(),"horizon":forecast["horizon"],"direction":forecast["direction"],
                "reference_price":reference,"expected_entry":self.cost_engine.execution_prices(reference,forecast["direction"],cost)["realistic_entry_price"],
                **net,"downside":net["downside_quantile"],"upside":net["upside_quantile"],
                "intelligence_quality":intelligence["quality"]["quality_score"],"liquidity_quality":liquidity,
                "cost_estimate":cost,"regime":intelligence["regime"],"regime_stability":intelligence["regime"]["confidence"],
                "supporting_patterns":intelligence["patterns"],"supporting_events":fusion["evidence"],
                "evidence_diversity":fusion["evidence_diversity"],"contradiction_status":fusion["contradiction_status"],
                "forecast_id":forecast["forecast_id"],"model_version":forecast["model_version"],"sample_count":net["sample_count"],"uncertainty":net["uncertainty"],
                "calibrated_probability":net["calibrated_probability"],"stale":age>self.star_finder_config.maximum_age_seconds,
                "provider_degraded":unavailable,"provider_dependency_reasons":provider_reasons,
                "risk_approved":risk_approved,"risk_state":risk,"kill_switch":self.trader_kill_switch,
                "paper_only":True,"rejection_reasons":[]}
            opportunity["rejection_reasons"]=rejection_gates(opportunity,self.star_finder_config)
            opportunity["status"]="REJECTED" if opportunity["rejection_reasons"] else "QUALIFIED"
            opportunity["star_score"]=star_score(opportunity)["score"]; opportunity["score_explanation"]=star_score(opportunity)
            if opportunity["status"]=="QUALIFIED":
                opportunity["entry_confirmed"]=True
            opportunity["entry_analysis"]=entry_timing(opportunity,now=now)
            inserted=self.store.save_star_opportunity(opportunity)
            if inserted:
                self.store.save_lifecycle(lifecycle_transition(opportunity["opportunity_id"],None,"DETECTED",reasons=["scanner_candidate"],at=now))
                target="QUALIFIED" if opportunity["status"]=="QUALIFIED" else "REJECTED"
                self.store.save_lifecycle(lifecycle_transition(opportunity["opportunity_id"],"DETECTED",target,
                    reasons=opportunity["rejection_reasons"] or ["hard_gates_passed"],at=now))
            if opportunity["status"]=="QUALIFIED":
                self.store.save_alert(build_alert("star_finder.qualified","info",f"Qualified paper opportunity: {symbol}",[opportunity["opportunity_id"]],now))
            elif int(str(opportunity["opportunity_id"])[0],16)<4:  # deterministic bounded 25% sample
                self.store.save_decision({"opportunity_id":opportunity["opportunity_id"],"instrument":symbol,
                    "action":"REJECTED","reason":opportunity["rejection_reasons"],"timestamp":now.isoformat(),
                    "due_at":(now+timedelta(hours=1)).isoformat(),"sampled":True,"paper_only":True},now.isoformat())
            built.append(opportunity)
        ranking=rank_opportunities(self.store.star_opportunities(include_rejected=True,limit=500))
        self.store.save_ranking_snapshot(ranking)
        decisions=[]
        by_symbol={value["instrument"]:value for value in ranking["opportunities"]}
        for position in self.trader.positions.values():
            decision=position_decision(public(position),by_symbol.get(position.instrument)); self.store.save_decision(decision,now.isoformat()); decisions.append(decision)
        auto_actions=self._auto_paper(built,now)
        self._scanner_result={**scan,"star_finder_generated":len(built),"auto_paper_actions":len(auto_actions)}
        return {"status":"ok","mode":"paper","scan":scan,"ranking":ranking,"position_decisions":decisions,"auto_paper":auto_actions}

    def get_star_finder_status(self)->dict[str,object]:
        return {"mode":"paper_only","scheduler":self.scheduler.status(),"cost_config_version":self.cost_engine.config.version,
                "star_finder_version":self.star_finder_config.version,"latest_ranking":self.store.ranking_snapshots(1)}
    def get_trader_status(self)->dict[str,object]:
        return {"mode":self.trader.config.mode,"paper_only":True,"automation_enabled":self.trader.config.mode==TraderMode.AUTO_PAPER,
                "new_entries_paused":self.trader_kill_switch or not self.trader.risk_budget()["new_entries_allowed"],
                "risk":self.trader.risk_budget(),"kill_switch":self.trader_kill_switch,
                "broker":"FakeSandboxBroker","strategies":list(STRATEGIES),"kill_switch_checked_before_execution":True}
    def set_trader_mode(self,mode:str)->dict[str,object]:
        self.trader.config=replace(self.trader.config,mode=TraderMode(mode));self._persist_trader_state(utc_now())
        return self.get_trader_status()
    def get_trader_portfolio(self)->dict[str,object]: return self.trader.public()
    def get_trader_positions(self)->dict[str,object]: return {"positions":self.trader.public()["positions"],"paper_only":True}
    def get_trader_records(self,kind:str,limit:int=500)->dict[str,object]:
        return {kind:self.store.trader_records(kind,limit=limit),"paper_only":True}
    def get_trader_performance(self)->dict[str,object]: return performance(self.trader)
    def simulate_capital(self,capital:float)->dict[str,object]:
        return capital_simulation(self.store.trader_records("trade",limit=1000),capital)
    def replay_trader(self)->dict[str,object]: return self.replay_engine.replay(self.store.trader_records("replay",limit=1000),starting_capital=self.trader.config.starting_capital)
    def get_strategy_competition(self)->dict[str,object]:
        result=strategy_competition(self.store.trader_records("trade",portfolio_id=None,limit=10000),starting_capital=self.trader.config.starting_capital)
        for name,ledger in self.strategy_ledgers.items(): result["strategies"][name].update(performance(ledger))
        result["automatic_promotion"]=False
        return result
    def get_strategy_ledgers(self)->dict[str,object]:
        return {"ledgers":{name:{"portfolio":ledger.public(),"performance":performance(ledger)} for name,ledger in self.strategy_ledgers.items()},"paper_only":True}
    def get_attribution(self)->dict[str,object]: return {"items":self.store.trader_records("attribution",limit=1000),"causal":False,"paper_only":True}
    def get_decision_learning(self)->dict[str,object]: return learning_summary(self.store.trader_records("decision_dataset",limit=1000))
    def get_research_status(self)->dict[str,object]:
        items=self.store.governance();champions=[v for v in items if v.get("status")=="incumbent"]
        challengers=[v for v in items if v.get("kind")=="candidate" or v.get("status")=="candidate"]
        return {"champion":champions[:1],"challengers":challengers,"experiments":[v for v in items if v.get("kind")=="experiment"],
                "paper_only":True,"free_form_self_modification":False}
    def get_endurance_health(self)->dict[str,object]:
        scheduler=self.scheduler.status();forecasts=self.store.forecasts(limit=500);resolved=self.store.resolved_forecasts(limit=5000)
        trades=self.store.trader_records("trade",limit=1000)
        maturity=evidence_maturity(resolved_forecasts=len(resolved),resolved_trades=len(trades),duration_days=0,
            regimes=len({str(v.get("local_regime")) for v in resolved}),assets=len({str(v.get("instrument")) for v in resolved}))
        outcomes=self.store.trader_records("outcome",limit=1000); pending=self.store.trader_records("outcome_pending",limit=1000)
        retries=self.store.trader_records("outcome_retry",limit=1000)
        closed_ids={str(v.get("position_id")) for v in outcomes}
        backlogs={"open_positions_not_reevaluated":sum(not p.last_action_at for p in self.trader.positions.values()),
            "closed_trades_not_resolved":sum(str(v.get("position_id")) not in closed_ids for v in pending),
            "missed_outcomes":sum(v.get("action") in {"WAIT","IGNORE","REJECTED"} for v in self.store.decisions(limit=500)),
            "strategy_ledgers_stalled":sum(len(ledger.fills)<len(self.trader.fills)
                for name,ledger in self.strategy_ledgers.items() if name!="NOVA_COMPOSITE"),
            "research_campaign_stalled":sum(v.get("status")=="running" for v in self.store.governance("god_eye_campaign"))}
        return {"scheduler":scheduler,"watchdog":watchdog(scheduler,now_timestamp=utc_now().timestamp(),backlogs=backlogs),
                "backlogs":backlogs,"forecast_backlog":len(forecasts)-len(resolved),"evidence_maturity":maturity,
                "outcome_state":{"pending":len(pending),"resolved":len(outcomes),
                                 "retry_pending":sum(v.get("status")=="retry_scheduled" for v in retries),
                                 "exhausted":sum(v.get("status")=="exhausted" for v in retries)},
                "storage_cleanup":self.store.retention_health(),"paper_only":True}

    def engineering_readiness(self, *, golden_e2e_green: bool = False,
                              chaos_matrix_green: bool = False) -> dict[str, object]:
        health=self.get_endurance_health(); scheduler=dict(health["scheduler"])
        retention=dict(health["storage_cleanup"]); operational=self.store.operational_health()
        critical={name:value for name,value in dict(scheduler.get("tasks",{})).items()
                  if value.get("lane")=="critical"}
        checks={
            "authoritative_portfolio_consistent":self.get_portfolio()==self.get_trader_portfolio(),
            "scheduler_alive":bool(scheduler.get("scheduler_alive")),
            "critical_tasks_not_starved":bool(scheduler.get("lanes",{}).get("critical")) and bool(critical),
            "provider_dependency_gates_operational":bool(chaos_matrix_green),
            "risk_gates_operational":bool(chaos_matrix_green),
            "champion_activation_operational":any(v.get("new_version")==self.trader.config.version
                for v in self.store.governance("trader_champion_applied")),
            "outcome_backlog_healthy":int(health["backlogs"]["closed_trades_not_resolved"])==0,
            "storage_cleanup_healthy":bool(retention.get("healthy")) and retention.get("last_cleanup") is not None,
            "no_unresolved_integrity_error":operational.get("db_status")=="ok",
            "golden_e2e_green":bool(golden_e2e_green),
            "chaos_matrix_green":bool(chaos_matrix_green),
            "paper_only_boundary_intact":self.get_trader_status().get("paper_only") is True,
        }
        blockers=[name for name,passed in checks.items() if not passed]
        return {"status":"ENGINEERING_READY_FOR_24H_SUPERVISED_PAPER" if not blockers else "ENDURANCE_BLOCKED",
                "checks":checks,"blocker_reasons":blockers,"paper_only":True}
    def get_ranked_opportunities(self,limit:int=100)->dict[str,object]:
        return rank_opportunities(self.store.star_opportunities(include_rejected=True,limit=limit),limit=limit)
    def get_star_opportunity(self,opportunity_id:str)->dict[str,object]|None: return self.store.star_opportunity(opportunity_id)
    def get_opportunity_history(self,opportunity_id:str)->dict[str,object]: return {"transitions":self.store.lifecycle(opportunity_id)}
    def get_costs(self)->dict[str,object]: return {"config":self.cost_engine.public_config(),"snapshots":self.store.cost_snapshots(100)}
    def compare_venues(self,instrument:str,size:float=1000)->dict[str,object]:
        if instrument not in self.instruments: raise KeyError("instrument_not_found")
        history=self._best_history(instrument)
        if not history:return {"instrument":instrument,"venues":[],"reason":"price_unavailable"}
        price=float(max(history,key=lambda row:str(row["opened_at"]))["close"]); item=self.instruments[instrument]
        venues=[]
        for venue in self.cost_engine.config.venue_costs:
            if item.kind != InstrumentType.CRYPTO: continue
            venues.append(self.cost_engine.estimate(asset_class=item.kind.value,venue=venue,size=size,price=price,
                microstructure=self._microstructure.get(instrument)))
        return {"instrument":instrument,"reference_price":price,"venues":sorted(venues,key=lambda v:v["total_round_trip_cost"]),"execution":False}
    def get_patterns(self,instrument:str|None=None,limit:int=100,start:str|None=None,end:str|None=None)->dict[str,object]:
        snapshots=self.store.intelligence("market_intelligence",instrument=instrument,limit=limit)
        patterns=[p for snapshot in snapshots for p in snapshot.get("patterns",[])]
        def in_range(value:dict[str,object])->bool:
            timestamp=str(value.get("timestamp") or value.get("detected_at") or value.get("created_at") or "")
            return (not start or timestamp>=start) and (not end or timestamp<=end)
        return {"patterns":[p for p in patterns if in_range(p)][:limit]}
    def get_market_intelligence(self,instrument:str)->dict[str,object]: return self.analyze_market(instrument)
    def get_microstructure(self,instrument:str)->dict[str,object]: return dict(self._microstructure.get(instrument,{"status":"unavailable","instrument":instrument}))
    def get_pattern_statistics(self)->dict[str,object]:
        occurrences=[]
        for forecast in self.store.resolved_forecasts(limit=5000):
            for pattern in forecast.get("feature_snapshot",{}).get("patterns",[]):
                occurrences.append({**pattern,"horizon":forecast["horizon"],"forward_return":forecast.get("evaluation",{}).get("actual_return")})
        statistics=historical_pattern_statistics(occurrences)
        return {"minimum_samples":20,"statistics":statistics,"signal_decay":signal_decay(occurrences)}
    def get_market_memory(self,instrument:str,minimum_samples:int=3)->dict[str,object]:
        current=self.analyze_market(instrument); primary=current["features"].get("1h") or current["features"].get("5m") or next(iter(current["features"].values()),None)
        if not primary: return {"instrument":instrument,"similar_cases":[],"sample_count":0,"sample_sufficient":False}
        candidates=[]
        for row in self.store.resolved_forecasts(limit=5000):
            if row.get("instrument")!=instrument: continue
            snapshot=row.get("feature_snapshot",{}); values=snapshot.get("quantitative_features",{})
            if not values: continue
            candidates.append({"situation_id":row["forecast_id"],"timestamp":row["created_at"],"features":values,
                "regime":snapshot.get("local_regime","unknown"),"subsequent_returns":{row["horizon"]:row["evaluation"]["actual_return"]}})
        target={"features":primary["values"],"regime":current["regime"]["regime"]}
        return {"instrument":instrument,**retrieve_similar_situations(target,candidates,as_of=as_utc(current["as_of"]),minimum_samples=minimum_samples)}

    def get_history(self, instrument: str, interval: str | None = None, limit: int = 500,
                    start: str | None = None, end: str | None = None) -> dict[str, object]:
        rows=self.store.history(instrument,interval=interval,limit=limit,start=start,end=end)
        unique={str(row["opened_at"]):row for row in sorted(rows,key=lambda row:(str(row["opened_at"]),str(row.get("source",{}).get("provider",""))))}
        candles=[unique[key] for key in sorted(unique)]
        latest=candles[-1] if candles else None
        source=dict(latest.get("source",{})) if latest else {}
        return {"instrument":instrument,"timeframe":interval,"available_timeframes":self.store.candle_intervals(instrument),
            "candles":candles,"count":len(candles),"freshness":{"latest_at":latest.get("opened_at") if latest else None,
            "retrieved_at":source.get("retrieved_at"),"provider":source.get("provider"),"stale":latest.get("quality",{}).get("stale") if latest else None}}

    def get_trade_markers(self,instrument:str,limit:int=500)->dict[str,object]:
        fills=[v for v in self.store.trader_records("fill",limit=limit) if v.get("instrument")==instrument]
        transitions={}
        for opportunity_id in {str(v.get("opportunity_id")) for v in fills if v.get("opportunity_id")}:
            for transition in self.store.lifecycle(opportunity_id):
                if transition.get("trade_ref") and transition.get("to_status") in {"ENTER","ADD","REDUCE","EXIT"}:
                    transitions[str(transition["trade_ref"])]=transition
        decisions={(str(v.get("opportunity_id")),str(v.get("timestamp"))):v for v in self.store.decisions(limit=limit)
                   if v.get("instrument")==instrument}
        markers=[]
        for fill in fills:
            transition=transitions.get(str(fill.get("order_id")))
            if not transition: continue
            decision=decisions.get((str(fill.get("opportunity_id")),str(fill.get("timestamp"))),{}); action=str(transition["to_status"])
            markers.append({"action":action,"timestamp":fill.get("timestamp"),"execution_price":fill.get("execution_price"),
                "quantity":fill.get("quantity"),"notional":fill.get("notional"),"fees":fill.get("fees"),
                "slippage":fill.get("slippage"),"strategy":fill.get("strategy"),"opportunity_id":fill.get("opportunity_id"),
                "position_id":f'{fill.get("strategy")}|{instrument}',"trade_id":transition.get("trade_ref"),
                "reason":decision.get("reason") or transition.get("reasons"),"lifecycle_status":action})
        return {"instrument":instrument,"markers":sorted(markers,key=lambda v:str(v.get("timestamp"))),"paper_only":True}

    def get_trade_detail(self,trade_id:str)->dict[str,object]|None:
        trades=self.store.trader_records("trade",limit=1000)
        trade=next((v for v in trades if str(v.get("position_id"))==trade_id),None)
        position=next((v for v in self.trader.public()["positions"] if str(v.get("position_id"))==trade_id),None)
        base=trade or position
        if base is None:return None
        opportunity_id=str(base.get("opportunity_id") or "")
        instrument=str(base.get("instrument") or trade_id.rsplit("|",1)[-1])
        fills=[v for v in self.store.trader_records("fill",limit=1000)
               if v.get("instrument")==instrument and v.get("opportunity_id")==opportunity_id and v.get("strategy")==base.get("strategy")]
        outcomes=[v for v in self.store.trader_records("outcome",limit=1000) if str(v.get("position_id"))==trade_id]
        return {"trade":base,"instrument":instrument,"opportunity":self.store.star_opportunity(opportunity_id),
            "timeline":self.store.lifecycle(opportunity_id),"fills":sorted(fills,key=lambda v:str(v.get("timestamp"))),
            "outcome":outcomes[0] if outcomes else None,"paper_only":True}

    def get_data_health(self) -> dict[str, object]:
        quotes = self.get_market_snapshot()["quotes"]
        states = [quote.get("quality", {}).get("freshness_status", "UNKNOWN") for quote in quotes]
        stale = sum(1 for state in states if state == "STALE")
        market_closed = sum(1 for state in states if state == "MARKET_CLOSED")
        operational = self.store.operational_health()
        source_stats = operational.pop("source_stats")
        sources_ok = sum(1 for item in source_stats if item["last_success"] and
                         (not item["last_error"] or item["last_success"] >= item["last_error"]))
        scheduler_status = self.scheduler.status()
        backlog = sum(1 for task in scheduler_status["tasks"].values()
                      if task["last_status"] in {"never", "error"})
        next_market_refresh = scheduler_status["tasks"].get("market", {}).get("next_run_at")
        market_providers = []
        for provider in [*self.crypto_providers, self.yahoo_provider]:
            detail = self._provider_health.get(provider.name, {})
            provider_quotes = [quote for quote in quotes if quote.get("source", {}).get("provider") == provider.name]
            market_providers.append({"name": provider.name, "status": detail.get("status", "unknown"),
                "last_success": detail.get("last_success"), "last_attempt": detail.get("last_attempt"),
                "last_error": detail.get("last_error"),
                "freshness": sorted({str(q.get("quality", {}).get("freshness_status", "UNKNOWN")) for q in provider_quotes}),
                "next_retry": next_market_refresh if detail.get("status") == "degraded" else None})
        return {"status": "degraded" if self._last_errors else "ok", "quote_count": len(quotes),
                "stale_quotes": stale, "market_closed_quotes": market_closed,
                "fresh_quotes": sum(1 for state in states if state == "FRESH"),
                "delayed_quotes": sum(1 for state in states if state == "DELAYED"),
                "unknown_quotes": sum(1 for state in states if state == "UNKNOWN"),
                "provider_unavailable_quotes": sum(1 for state in states if state == "PROVIDER_UNAVAILABLE"),
                "provider_errors": dict(self._last_errors), "provider_health": market_providers,
                "providers": sorted({p.name for p in [*self.crypto_providers, self.yahoo_provider]}),
                "sources": self.store.source_health(), "sources_ok": sources_ok,
                "sources_ko": sum(1 for item in source_stats if item["last_error"] and
                                  (not item["last_success"] or item["last_error"] > item["last_success"])),
                "source_catalog": [public(config) for config in self.source_configs.values()],
                "source_stats": source_stats, "scheduler": scheduler_status, "backlog": backlog,
                "recent_errors": [run for run in operational["recent_runs"] if run["status"] != "success"][:20],
                **operational}

    def _exclusive(self, stream: str, action):
        lock = self._refresh_locks[stream]
        if not lock.acquire(blocking=False):
            return {"status": "skipped", "reason": "refresh_already_running"}
        try:
            return action()
        finally:
            lock.release()

    def _cache(self, quote: MarketQuote) -> None:
        self._quotes[quote.instrument.symbol] = quote
        self._quotes.move_to_end(quote.instrument.symbol)
        while len(self._quotes) > self.cache_size:
            self._quotes.popitem(last=False)

    def _journal(self, event: str, status: str, *, provider: str | None = None, model: str | None = None,
                 input_tokens: int | None = None, output_tokens: int | None = None,
                 error_category: str | None = None) -> None:
        if self.journal:
            self.journal.append(event, status=status, provider=provider, model=model,
                                input_tokens=input_tokens, output_tokens=output_tokens,
                                error_category=error_category)
