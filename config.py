"""Настройки из файла .env."""
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _int(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    return int(value) if value else default


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = _int("OWNER_ID", 0)  # единственный пользователь бота

LLM_API_KEY = os.getenv("LLM_API_KEY", "").strip()
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "").strip() or "https://api.deepseek.com"
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-flash").strip()

DB_PATH = str(BASE_DIR / os.getenv("DB_PATH", "news.db"))
TZ = ZoneInfo(os.getenv("TIMEZONE", "Asia/Almaty"))
RETENTION_DAYS = _int("RETENTION_DAYS", 30)
COLLECT_MINUTES = _int("COLLECT_MINUTES", 60)   # обход источников не чаще раза в час
FRESH_HOURS = _int("FRESH_HOURS", 36)           # статьи старше не отправляются в LLM (сводка — за сутки)
MAX_LLM_ARTICLES = _int("MAX_LLM_ARTICLES", 150)  # предел статей на один обход — защита от лишних трат
QUIET_FROM = _int("QUIET_FROM", 23)             # ночная тишина для срочных: с 23:00
QUIET_TO = _int("QUIET_TO", 8)                  # до 08:00


def check() -> list[str]:
    return [name for name, value in
            [("BOT_TOKEN", BOT_TOKEN), ("OWNER_ID", OWNER_ID), ("LLM_API_KEY", LLM_API_KEY)] if not value]
