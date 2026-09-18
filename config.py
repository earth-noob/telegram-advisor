import os

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
VIBECODE_API_KEY = os.getenv("VIBECODE_API_KEY", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or "0")

TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")
DAILY_POST_TIME = os.getenv("DAILY_POST_TIME", "09:00")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-5.6-terra")
LLM_BASE_URL = "https://vibecode.moe/v1"
