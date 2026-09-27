from datetime import datetime, timedelta, timezone
import json

from nova_api.god_eye.service import GodEyeService
from nova_api.god_eye.storage import GodEyeStore


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def service(path):
    return GodEyeService(store=GodEyeStore(path), instruments=[], crypto_providers=[],
        news_providers=[], source_configs=[], social_providers=[])


def champion(version, fraction, at):
    return {"kind":"trader_champion","version":version,"status":"active",
        "campaign":"endurance","activated_at":at.isoformat(),
        "trader_config":{"max_position_fraction":fraction}}


def test_champion_hot_reload_rollback_restart_and_invalid_are_atomic(tmp_path):
    path=tmp_path/"champion.sqlite"; app=service(path)
    app.store.save_governance("trader_champion","active","v1",champion("v1",.10,NOW),NOW.isoformat())
    assert app.sync_trader_champion(NOW)["version"] == "v1"
    position_marker = app.trader.positions.copy()

    v2_at=NOW+timedelta(minutes=1)
    app.store.save_governance("trader_champion","active","v2",champion("v2",.20,v2_at),v2_at.isoformat())
    assert app.sync_trader_champion(v2_at)["status"] == "applied"
    assert app.trader.config.version == "v2" and app.trader.positions == position_marker
    restarted=service(path)
    assert restarted.trader.config.version == "v2"

    rollback_at=NOW+timedelta(minutes=2)
    rollback={"current":"v2","rollback_target":"v1","reason":"drift","rolled_back_at":rollback_at.isoformat()}
    app.store.save_governance("rollback","v2","1",rollback,rollback_at.isoformat())
    assert app.sync_trader_champion(rollback_at)["version"] == "v1"
    assert service(path).trader.config.version == "v1"

    invalid_at=NOW+timedelta(minutes=3)
    invalid=champion("v3",.9,invalid_at)
    app.store.save_governance("trader_champion","active","v3",invalid,invalid_at.isoformat())
    rejected=app.sync_trader_champion(invalid_at)
    assert rejected["status"] == "rejected" and app.trader.config.version == "v1"
    assert app.sync_trader_champion(invalid_at)["status"] == "rejected"


def test_retention_is_bounded_and_protects_unresolved_replay_and_state(tmp_path):
    store=GodEyeStore(tmp_path/"retention.sqlite"); old=(NOW-timedelta(days=400)).isoformat()
    recent=(NOW-timedelta(days=1)).isoformat()
    with store._connect() as db:
        for index in range(6):
            at=old if index < 5 else recent
            db.execute("INSERT INTO quotes VALUES (?,?,?,?)",(f"Q{index}",at,"fake","{}"))
        for fid,status in (("resolved-old","evaluated"),("unresolved-old","pending"),("replay-old","evaluated")):
            payload=json.dumps({"forecast_id":fid,"status":status})
            db.execute("INSERT INTO forecasts VALUES (?,?,?)",(fid,old,payload))
        for fid in ("resolved-old","replay-old"):
            db.execute("INSERT INTO forecast_evaluations VALUES (?,?,?)",(fid,old,"{}"))
        replay=json.dumps({"kind":"forecast","payload":{"forecast_id":"replay-old"}})
        db.execute("INSERT INTO trader_records VALUES (?,?,?,?,?)",("replay","forecast|replay-old",old,"NOVA_COMPOSITE",replay))
        db.execute("INSERT INTO trader_state VALUES (?,?,?)",("NOVA_COMPOSITE",old,json.dumps({"paper_only":True})))
        db.execute("INSERT INTO governance VALUES (?,?,?,?,?)",("trader_champion","active","v1",old,"{}"))
    first=store.cleanup_retention(now=NOW,retention_days={"quotes":30,"forecasts":30},batch_size=2)
    assert first["by_table"]["quotes"] == 2 and first["by_table"]["forecasts"] == 1
    with store._connect() as db:
        forecasts={row[0] for row in db.execute("SELECT forecast_id FROM forecasts")}
        assert forecasts == {"unresolved-old","replay-old"}
        assert db.execute("SELECT COUNT(*) FROM trader_state").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM governance").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM quotes WHERE observed_at=?",(recent,)).fetchone()[0] == 1
    assert store.retention_health()["last_cleanup"] == NOW.isoformat()


def test_repeated_ingestion_growth_is_reclaimed_incrementally(tmp_path):
    store=GodEyeStore(tmp_path/"growth.sqlite"); old=(NOW-timedelta(days=60)).isoformat()
    with store._connect() as db:
        for index in range(25): db.execute("INSERT INTO quotes VALUES (?,?,?,?)",(f"S{index}",old,"fake","{}"))
    deleted=0
    for _ in range(5): deleted += int(store.cleanup_retention(now=NOW,retention_days={"quotes":1},batch_size=5)["by_table"]["quotes"])
    assert deleted == 25
    with store._connect() as db: assert db.execute("SELECT COUNT(*) FROM quotes").fetchone()[0] == 0


def test_engineering_readiness_is_fail_closed_and_machine_readable(tmp_path):
    app=service(tmp_path/"ready.sqlite")
    blocked=app.engineering_readiness()
    assert blocked["status"] == "ENDURANCE_BLOCKED"
    assert {"scheduler_alive","golden_e2e_green","chaos_matrix_green"} <= set(blocked["blocker_reasons"])
    app.store.save_governance("trader_champion","active","v1",champion("v1",.10,NOW),NOW.isoformat())
    assert app.sync_trader_champion(NOW)["status"] == "applied"
    app.cleanup_storage(); app.scheduler.start()
    try:
        ready=app.engineering_readiness(golden_e2e_green=True,chaos_matrix_green=True)
        assert ready["status"] == "ENGINEERING_READY_FOR_24H_SUPERVISED_PAPER"
        assert all(ready["checks"].values()) and ready["paper_only"] is True
    finally: app.scheduler.stop()
