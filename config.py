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

# Генерация изображений по сохранённым фото (пусто = функция выключена)
IMAGE_MODEL = os.getenv("IMAGE_MODEL", "").strip()
IMAGE_TIMEOUT = float(os.getenv("IMAGE_TIMEOUT", "180") or "180")
# Лимит генераций на пользователя в сутки (админ без лимита; 0 = без лимита)
IMAGE_DAILY_LIMIT = int(os.getenv("IMAGE_DAILY_LIMIT", "10") or "10")
