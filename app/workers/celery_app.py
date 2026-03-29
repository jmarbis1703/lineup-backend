"""
Celery application and Beat schedule for LineUp background tasks.

Beat tasks (§7):
  sync_players                 — daily 06:00 UTC      §7.2
  sync_fixtures                — daily 06:00 UTC      §7.3
  check_finished_fixtures      — every 10 minutes     §7.4
  activate_pending_tournaments — every 1 minute       §7.5
  freeze_expired_tournaments   — every 5 minutes      §7.6
  liquidity_recalibration      — every 6 hours        §7.7
"""
from celery import Celery
from celery.schedules import crontab

from app.config import settings

celery_app = Celery(
    "lineup",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=[
        "app.workers.player_import",
        "app.workers.fixture_sync",
        "app.workers.oracle_update",
        "app.workers.tournaments",
        "app.workers.calibration",
        "app.workers.snapshot",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    worker_prefetch_multiplier=1,
    task_acks_late=True,
)

celery_app.conf.beat_schedule = {
    # §7.2 — Re-fetch squads; new players imported, missing deactivated
    "sync-players-daily": {
        "task": "app.workers.player_import.sync_players_task",
        "schedule": crontab(hour=6, minute=0),
    },
    # §7.3 — Pull upcoming fixtures for all 5 leagues
    "sync-fixtures-daily": {
        "task": "app.workers.fixture_sync.sync_fixtures_task",
        "schedule": crontab(hour=6, minute=0),
    },
    # §7.4 — Process newly finished fixtures; update oracle ratings
    "check-finished-fixtures": {
        "task": "app.workers.oracle_update.check_finished_fixtures_task",
        "schedule": 600.0,  # every 10 minutes
    },
    # §7.5 — Activate pending tournaments; lock starting_values
    "activate-pending-tournaments": {
        "task": "app.workers.tournaments.activate_pending_tournaments_task",
        "schedule": 60.0,  # every 1 minute
    },
    # §7.6 — Freeze expired active tournaments; write tournament_snapshots
    "freeze-expired-tournaments": {
        "task": "app.workers.tournaments.freeze_expired_tournaments_task",
        "schedule": 300.0,  # every 5 minutes
    },
    # §7.7 — Recalibrate b_min one-way ratchet; scale q vectors proportionally
    "liquidity-recalibration": {
        "task": "app.workers.calibration.liquidity_recalibration_task",
        "schedule": crontab(minute=0, hour="*/6"),
    },
    # §7.8 — Snapshot every user's portfolio value for history charts
    "snapshot-portfolios-hourly": {
        "task": "app.workers.snapshot.snapshot_all_portfolios_task",
        "schedule": crontab(minute=0),
    },
    # §9A — Refresh match ratings daily post-matchday; to add leagues for Step 7,
    # update MATCH_RATING_LEAGUES in app/workers/player_import.py
    "refresh-match-ratings-daily": {
        "task": "app.workers.player_import.refresh_match_ratings_task",
        "schedule": crontab(hour=7, minute=0),  # 07:00 UTC daily
    },
    # §market-stability — Recompute b floor from live user count every hour.
    # B_FLOOR decreases as the user base grows, keeping prices tight at scale
    # and stable during beta.  b_base and n_target tunable via env vars.
    "refresh-b-floor-hourly": {
        "task": "app.workers.player_import.refresh_b_floor_task",
        "schedule": crontab(minute=0),  # every hour on the hour
    },
}
