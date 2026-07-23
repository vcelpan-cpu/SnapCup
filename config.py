import os
from datetime import time
from zoneinfo import ZoneInfo

BOT_TOKEN = os.environ["BOT_TOKEN"]

_raw_db_url = os.environ["DATABASE_URL"]
if _raw_db_url.startswith("postgresql+asyncpg://"):
    DATABASE_URL = _raw_db_url
elif _raw_db_url.startswith("postgresql://"):
    DATABASE_URL = _raw_db_url.replace("postgresql://", "postgresql+asyncpg://", 1)
else:
    DATABASE_URL = _raw_db_url.replace("postgres://", "postgresql+asyncpg://", 1)

# Чат/группа кухни, куда падают отчёты и заявки на подтверждение новых кофеен
ADMIN_CHAT_ID = int(os.environ["ADMIN_CHAT_ID"])

# ID телеграм-аккаунтов, которые могут управлять каталогом и подтверждать кофейни
ADMIN_USER_IDS = {int(x) for x in os.environ.get("ADMIN_USER_IDS", "").split(",") if x.strip()}

TZ = ZoneInfo(os.environ.get("TZ_NAME", "Europe/Moscow"))

# Время, после которого приём заказов на сегодня закрывается и уходит отчёт на кухню
CUTOFF_HOUR = int(os.environ.get("CUTOFF_HOUR", 15))
CUTOFF_MINUTE = int(os.environ.get("CUTOFF_MINUTE", 0))
CUTOFF_TIME = time(CUTOFF_HOUR, CUTOFF_MINUTE, tzinfo=TZ)
