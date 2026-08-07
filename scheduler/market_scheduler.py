"""
Market scheduler — keeps the pipeline running on Indian market hours
without the Claude Desktop app needing to be open.

Deploy this on Railway (or any VPS) and it runs 24/7. The host must
stay alive; your laptop does not.

Schedule:
- Research cycle: 9:15, 10:15, 11:15, 12:15, 13:15, 14:15, 15:15 IST
  (7 runs on every market day — one per hour during trading hours)
- End-of-day monitor: 15:35 IST (5 min after market close)
- Weekly audit: Friday 16:00 IST

Indian market holidays are NOT yet handled — the cycle will run and
find nothing tradeable on those days, which is harmless but wastes API
calls. TODO: plug in an NSE holiday list to gate the cycle properly.
"""

import logging
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

IST = pytz.timezone("Asia/Kolkata")
scheduler = BlockingScheduler(timezone=IST)


@scheduler.scheduled_job(
    CronTrigger(day_of_week="mon-fri", hour=7, minute=30, timezone=IST),
    id="kite_token_refresh",
    name="Daily Kite token refresh",
    max_instances=1,
    coalesce=True,
)
def kite_token_refresh():
    """
    7:30 AM IST — refresh the Kite access token before markets open at 9:15.
    Runs automatically. Requires KITE_USER_ID, KITE_PASSWORD, KITE_TOTP_SECRET in env.
    Failure is logged but does NOT crash the scheduler.
    """
    log.info("Refreshing Kite access token...")
    try:
        from setup.auto_refresh_kite_token import auto_refresh_token
        auto_refresh_token()
    except Exception as e:
        log.error(
            f"Kite token refresh FAILED: {e}\n"
            "Trading will fail today until the token is refreshed manually.\n"
            "Run: python setup/auto_refresh_kite_token.py"
        )


@scheduler.scheduled_job(
    CronTrigger(
        day_of_week="mon-fri",
        hour="9,10,11,12,13,14,15",
        minute=15,
        timezone=IST,
    ),
    id="research_cycle",
    name="Hourly research cycle",
    max_instances=1,        # don't stack if one run is still going
    coalesce=True,          # if missed (host restart), run once, not many
)
def research_cycle():
    log.info("Starting research cycle...")
    try:
        from main import run_cycle
        run_cycle()
    except Exception as e:
        log.error(f"Research cycle error: {e}", exc_info=True)


@scheduler.scheduled_job(
    CronTrigger(day_of_week="mon-fri", hour=15, minute=35, timezone=IST),
    id="eod_monitor",
    name="End-of-day position monitor",
)
def end_of_day_monitor():
    log.info("Running end-of-day position monitor...")
    try:
        from monitor.position_monitor import check_open_positions
        check_open_positions()
    except Exception as e:
        log.error(f"EOD monitor error: {e}", exc_info=True)


@scheduler.scheduled_job(
    CronTrigger(day_of_week="fri", hour=16, minute=0, timezone=IST),
    id="weekly_audit",
    name="Weekly auditor",
)
def weekly_audit():
    log.info("Running weekly audit...")
    try:
        from auditor.weekly_audit import run_weekly_audit
        from datetime import datetime, timedelta
        today = datetime.now(IST)
        week_start = (today - timedelta(days=4)).strftime("%Y-%m-%d")
        week_end = today.strftime("%Y-%m-%d")
        run_weekly_audit(week_start, week_end)
    except Exception as e:
        log.error(f"Weekly audit error: {e}", exc_info=True)


if __name__ == "__main__":
    log.info("Scheduler starting. Press Ctrl+C to stop.")
    scheduler.start()
