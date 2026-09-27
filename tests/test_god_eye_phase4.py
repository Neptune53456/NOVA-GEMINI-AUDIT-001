from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from nova_api.app import create_app
from nova_api.god_eye.calibration import build_calibration, calibration_metrics, chronological_split
from nova_api.god_eye.forecasting import EnsembleForecastEngine
from nova_api.god_eye.models import ForecastFeatureSet
from nova_api.god_eye.opportunities import build_opportunity
from nova_api.god_eye.regimes import classify_regime
from nova_api.god_eye.service import GodEyeService
from nova_api.god_eye.similarity import retrieve_similar, similar_features
from nova_api.god_eye.storage import GodEyeStore
from nova_api.journal import EventJournal

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def resolved_rows(count=30, model_id="m"):
    rows = []
    for index in range(count):
        score = -1 + 2 * index / max(1, count - 1)
        rows.append({"forecast_id": str(index), "model_id": model_id, "horizon": "1h", "raw_score": score,
                     "created_at": (NOW + timedelta(hours=index)).isoformat(),
                     "evaluation": {"actual_direction": "up" if score > 0 else "down"}})
    return rows


def test_temporal_calibration_threshold_and_metrics_have_no_lookahead():
    rows = resolved_rows()
    train, evaluation = chronological_split(rows)
    assert max(r["created_at"] for r in train) < min(r["created_at"] for r in evaluation)
    assert build_calibration(rows[:19], "m", "1h", minimum_samples=20) is None
    model = build_calibration(rows, "m", "1h", minimum_samples=20)
    assert model is not None and model.sample_count == 24
    assert 0 <= model.probability(.8) <= 1
    metrics = calibration_metrics(model, rows)
    assert metrics["sample_count"] == 6 and metrics["brier_score"] is not None and metrics["log_loss"] is not None


def test_regime_and_similar_events_are_deterministic_and_past_only():
    candles = [{"opened_at": (NOW + timedelta(hours=i)).isoformat(), "close": 100 + i * 2} for i in range(30)]
    assert classify_regime(candles)["regime"] == "trending_up"
    target = {"event_id": "now", "event_type": "macro", "entities": ["X"], "source_quality_score": .8,
              "novelty_score": .5, "published_at": NOW.isoformat()}
    past = {**target, "event_id": "past", "published_at": (NOW - timedelta(days=1)).isoformat()}
    future = {**target, "event_id": "future", "published_at": (NOW + timedelta(days=1)).isoformat()}
    outcomes = {"past": [{"reactions": {"1h": {"return": .03}}}],
                "future": [{"reactions": {"1h": {"return": 1.0}}}]}
    matches = retrieve_similar(target, [past, future], outcomes, horizon="1h", as_of=NOW)
    assert [m["event_id"] for m in matches] == ["past"]
    assert similar_features(matches)["mean_return"] == .03


def test_v2_probability_is_calibrator_owned_and_opportunity_filters():
    model_id = EnsembleForecastEngine.model_id
    model = build_calibration(resolved_rows(model_id=model_id), model_id, "1h", minimum_samples=20)
    features = ForecastFeatureSet("X", NOW, .03, 110, 100, .01, (.01,), 1.0, "macro", .9, .8,
                                  "positive", .8, local_regime="trending_up", similar_event_count=3,
                                  similar_return_median=.04, similar_positive_ratio=.8, similarity_confidence=.8)
    forecast = EnsembleForecastEngine().forecast(features, "1h", model)
    assert forecast.probability_up == model.probability(forecast.raw_score)
    eligible = build_opportunity(forecast.__dict__, downside=-.01, volatility=.02, data_quality=.9,
                                 minimum_samples=20, fees=0, slippage=0, as_of=NOW)
    rejected = build_opportunity({**forecast.__dict__, "probability_up": None,
                                  "calibration_sample_count": 0}, downside=-.01, volatility=.02, data_quality=.9)
    assert eligible["status"] == "eligible" and rejected["status"] == "rejected"
    assert "insufficient_calibration" in rejected["reasons"]


def test_phase4_persistence_idempotence_and_apis(tmp_path):
    store = GodEyeStore(tmp_path / "god.sqlite3")
    value = {"opportunity_id": "o1", "instrument": "X", "horizon": "1h", "direction": "up",
             "probability": .7, "expected_return": .02, "expected_value_net": .01,
             "downside_estimate": -.01, "uncertainty": .2, "data_quality": .9,
             "opportunity_score": 12.0, "reasons": [], "status": "eligible"}
    assert store.save_opportunity(value, NOW.isoformat()) is True
    assert store.save_opportunity(value, NOW.isoformat()) is False
    service = GodEyeService(store=store, instruments=[], news_providers=[], crypto_providers=[], llm_enabled=False)
    client = TestClient(create_app(journal=EventJournal(tmp_path / "journal.sqlite3"), god_eye_service=service))
    assert client.get("/api/v1/god-eye/opportunities").json()["opportunities"][0]["opportunity_id"] == "o1"
    assert client.get("/api/v1/god-eye/opportunities/o1").status_code == 200
    assert client.get("/api/v1/god-eye/calibration").status_code == 200
    assert client.get("/api/v1/god-eye/regimes").status_code == 200
    assert client.get("/api/v1/god-eye/events/missing/similar").status_code == 404
