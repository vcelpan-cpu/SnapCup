from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from config import CUTOFF_HOUR, CUTOFF_MINUTE, TZ, ADMIN_CHAT_ID
from handlers.admin import lock_and_report


def setup_scheduler(bot: Bot) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=TZ)
    scheduler.add_job(
        lock_and_report,
        trigger=CronTrigger(hour=CUTOFF_HOUR, minute=CUTOFF_MINUTE, timezone=TZ),
        args=[bot, ADMIN_CHAT_ID],
        id="daily_cutoff",
        replace_existing=True,
    )
    scheduler.start()
    return scheduler
