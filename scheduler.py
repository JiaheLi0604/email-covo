"""
scheduler.py
-------------
Automated daily scheduler for EmailConvo.

Reads schedule settings from config.json and runs 2–3 conversation sessions
per weekday at randomly chosen times within a configurable window.

Usage:
    python scheduler.py

The process runs indefinitely (BlockingScheduler). Stop it with Ctrl+C.

Config options in config.json:
    {
        "auto_run": false,          # true → fire one run immediately on startup
        "schedule": {
            "enabled": true,
            "day": "mon-fri",
            "timezone": "America/New_York",
            "runs_per_day": { "min": 2, "max": 3 },
            "window":       { "start": "10:00", "end": "17:00" },
            "min_gap_minutes": 90,
            "mode": "daily"         # "daily" (default) or "test"
        }
    }

Daily mode:
    At startup, plan today's sessions immediately (restart-safe).
    Each morning at 9:50am ET, re-plan that day's sessions.
    Session times are deterministic per date (date-seeded RNG) so a process
    restart always regenerates the same times and replaces existing jobs
    without creating duplicates.

Test mode:
    Fires once after run_after_seconds (default 120). Useful for verifying
    the full scheduler → conversation path without waiting.

Restart safety:
    Job IDs are "session_YYYY-MM-DD_N". Adding a job with replace_existing=True
    overwrites any existing job with the same ID. Past times are filtered out.
    Restarting at 12:00pm regenerates the same three times, skips the 10:18am
    one (past), and re-schedules only the afternoon sessions.
"""

import logging
import random
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from main import load_config, run_conversation

# ---- Logging setup ----
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


def _scheduled_job() -> None:
    """
    Job function called by APScheduler on each scheduled fire.
    Reloads config.json on every run so changes take effect without
    restarting the scheduler process.
    """
    log.info("Session fired — loading config and starting conversation.")
    try:
        config = load_config()
    except Exception as e:
        log.error("Failed to load config.json: %s", e)
        return
    try:
        run_conversation(config)
    except Exception as e:
        log.error("Conversation failed: %s", e, exc_info=True)


def _plan_today(scheduler: BlockingScheduler, schedule_cfg: dict, tz: ZoneInfo) -> None:
    """
    Deterministically pick today's session times and register DateTrigger jobs.

    Uses today's date as the random seed so the same date always produces the
    same session times. Combined with deterministic job IDs and replace_existing=True,
    this makes restart safe — re-running _plan_today on the same day regenerates
    identical times, overwrites existing jobs, and skips any times already past.
    """
    now = datetime.now(tz)
    today = now.date()

    # Weekday check (0=Mon, 6=Sun)
    day_cfg = schedule_cfg.get("day", "mon-fri")
    if "mon-fri" in day_cfg or "monday" in day_cfg.lower():
        if today.weekday() >= 5:
            log.info("Today is %s — no sessions planned on weekends.", today.strftime("%A"))
            return

    # Deterministic RNG seeded by date
    seed = int(today.strftime("%Y%m%d"))
    rng = random.Random(seed)

    runs_cfg = schedule_cfg.get("runs_per_day", {})
    min_runs = int(runs_cfg.get("min", 2))
    max_runs = int(runs_cfg.get("max", 3))
    n = rng.randint(min_runs, max_runs)

    window_cfg = schedule_cfg.get("window", {})
    ws_h, ws_m = map(int, window_cfg.get("start", "10:00").split(":"))
    we_h, we_m = map(int, window_cfg.get("end", "17:00").split(":"))
    window_start_min = ws_h * 60 + ws_m
    window_end_min   = we_h * 60 + we_m
    min_gap = int(schedule_cfg.get("min_gap_minutes", 90))

    # Generate n session times with min_gap constraint
    session_minutes = []
    for _ in range(n):
        for _ in range(200):   # max attempts per slot before giving up
            candidate = rng.randint(window_start_min, window_end_min - 1)
            if all(abs(candidate - s) >= min_gap for s in session_minutes):
                session_minutes.append(candidate)
                break

    session_minutes.sort()

    log.info(
        "Planning %d session(s) for %s: %s ET",
        len(session_minutes),
        today,
        ", ".join(f"{m // 60:02d}:{m % 60:02d}" for m in session_minutes),
    )

    scheduled = 0
    for i, total_min in enumerate(session_minutes):
        run_dt = now.replace(
            hour=total_min // 60,
            minute=total_min % 60,
            second=0,
            microsecond=0,
        )
        job_id = f"session_{today}_{i}"

        if run_dt <= now:
            log.info(
                "  Session %d (%s ET) already past — skipping.",
                i + 1, run_dt.strftime("%H:%M"),
            )
            continue

        scheduler.add_job(
            _scheduled_job,
            trigger=DateTrigger(run_date=run_dt),
            id=job_id,
            name=f"Conversation session {i + 1} — {run_dt.strftime('%H:%M')} ET",
            replace_existing=True,
            misfire_grace_time=300,
            coalesce=True,
        )
        log.info(
            "  Scheduled session %d at %s ET (job: %s).",
            i + 1, run_dt.strftime("%H:%M"), job_id,
        )
        scheduled += 1

    if scheduled == 0:
        log.info("All sessions for today are in the past. Next plan runs at 9:50am tomorrow.")


def main() -> None:
    # --- Load config ---
    try:
        config = load_config()
    except Exception as e:
        log.error("Could not load config.json: %s", e)
        sys.exit(1)

    auto_run = config.get("auto_run", False)
    schedule_cfg = config.get("schedule", {})
    schedule_enabled = schedule_cfg.get("enabled", False)

    if not schedule_enabled and not auto_run:
        log.warning(
            "Neither 'schedule.enabled' nor 'auto_run' is set to true in config.json. "
            "Nothing to do. "
            "To run once now: python main.py  "
            "To enable the daily schedule: set 'schedule.enabled': true in config.json."
        )
        return

    # --- Optionally fire once immediately (useful for testing) ---
    if auto_run:
        log.info("auto_run=true — running conversation immediately.")
        try:
            run_conversation(config)
        except Exception as e:
            log.error("Immediate run failed: %s", e, exc_info=True)

    if not schedule_enabled:
        return

    schedule_mode = schedule_cfg.get("mode", "daily")
    scheduler = BlockingScheduler()

    if schedule_mode == "test":
        run_after_seconds = schedule_cfg.get("run_after_seconds", 120)
        fire_at = datetime.now() + timedelta(seconds=run_after_seconds)
        scheduler.add_job(
            _scheduled_job,
            trigger=DateTrigger(run_date=fire_at),
            id="test_conversation",
            name=f"Test conversation — firing in {run_after_seconds}s",
        )
        log.info(
            "TEST MODE — conversation will fire once in %d seconds (at %s). "
            "Press Ctrl+C to cancel.",
            run_after_seconds,
            fire_at.strftime("%H:%M:%S"),
        )

    else:
        # --- Daily multi-session scheduler ---
        day = schedule_cfg.get("day", "mon-fri")
        timezone_str = schedule_cfg.get("timezone", "America/New_York")
        tz = ZoneInfo(timezone_str)

        # Plan today's sessions immediately (restart-safe: same date seed, replace_existing=True)
        _plan_today(scheduler, schedule_cfg, tz)

        # Morning planner: re-plans each workday at 9:50am ET
        def _daily_planner_job() -> None:
            log.info("Daily planner fired — planning today's sessions.")
            try:
                cfg = load_config()
            except Exception as e:
                log.error("Daily planner could not load config: %s", e)
                return
            _plan_today(scheduler, cfg.get("schedule", {}), tz)

        scheduler.add_job(
            _daily_planner_job,
            trigger=CronTrigger(
                day_of_week=day,
                hour=9,
                minute=50,
                timezone=timezone_str,
            ),
            id="daily_planner",
            name=f"Daily session planner — 9:50am {timezone_str}",
            replace_existing=True,
            misfire_grace_time=300,
            coalesce=True,
        )

        log.info(
            "Scheduler started. Daily planner: 9:50am %s on %s. "
            "Window: %s–%s, %d–%d sessions/day, min gap %dmin. "
            "Press Ctrl+C to stop.",
            timezone_str, day,
            schedule_cfg.get("window", {}).get("start", "10:00"),
            schedule_cfg.get("window", {}).get("end", "17:00"),
            schedule_cfg.get("runs_per_day", {}).get("min", 2),
            schedule_cfg.get("runs_per_day", {}).get("max", 3),
            schedule_cfg.get("min_gap_minutes", 90),
        )

    try:
        scheduler.start()
    except KeyboardInterrupt:
        log.info("Scheduler stopped by user (Ctrl+C).")
        scheduler.shutdown(wait=False)


if __name__ == "__main__":
    main()
