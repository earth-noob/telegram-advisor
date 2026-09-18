import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError

import config
import db
import llm
import utils

log = logging.getLogger(__name__)

MAX_SLEEP_SECONDS = 6 * 3600
MIN_PAUSE_SECONDS = 5
RETRY_DELAY_SECONDS = 300

_reminders_event = asyncio.Event()
_topic_event = asyncio.Event()

POST_SYSTEM_PROMPT = (
    "Ты автор ежедневной тематической статьи для Telegram-группы. "
    "Пиши по-русски, содержательно и без воды, простым текстом без "
    "markdown-разметки. Структура: короткий заголовок, затем 3–5 абзацев "
    "(интересный факт или мысль, разбор, практический вывод), в конце вопрос "
    "для обсуждения. Объём 1500–2500 знаков. Каждый день выбирай новый "
    "ракурс темы."
)


def notify_reminders() -> None:
    """Будит цикл напоминаний (после создания/изменения/удаления)."""
    _reminders_event.set()


def notify_topic() -> None:
    """Будит цикл ежедневных постов (после смены темы)."""
    _topic_event.set()


async def _sleep_until(seconds: float, event: asyncio.Event) -> None:
    """Спит до пробуждения по событию, но не дольше seconds."""
    try:
        await asyncio.wait_for(event.wait(), timeout=max(seconds, 0.0))
    except TimeoutError:
        pass
    finally:
        event.clear()


async def generate_daily_post(topic: str) -> str:
    now_local = utils.now()
    user_message = f"Тема: {topic}. Дата: {now_local.strftime('%d.%m.%Y')}."
    return await llm.call_llm(
        [
            {"role": "system", "content": POST_SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ]
    )


async def send_post(bot: Bot, chat_id: int, topic: str) -> None:
    """Генерирует пост, отправляет (с разбивкой) и сохраняет в историю."""
    post = await generate_daily_post(topic)
    for chunk in utils.split_text(post):
        await bot.send_message(chat_id, chunk)
    await db.save_message(chat_id, None, post)


async def scheduler_loop(bot: Bot) -> None:
    log.info("Scheduler запущен")
    while True:
        try:
            await _tick(bot)
            next_at = await db.next_reminder_time()
            if next_at is None:
                delay = MAX_SLEEP_SECONDS
            else:
                delay = (next_at - utils.utcnow()).total_seconds()
                delay = min(max(delay, MIN_PAUSE_SECONDS), MAX_SLEEP_SECONDS)
            await _sleep_until(delay, _reminders_event)
        except asyncio.CancelledError:
            log.info("Scheduler остановлен")
            raise
        except Exception:
            log.exception("Ошибка итерации scheduler")
            await asyncio.sleep(MIN_PAUSE_SECONDS)


async def _tick(bot: Bot) -> None:
    reminders = await db.claim_due_reminders()
    for row in reminders:
        try:
            await bot.send_message(row["chat_id"], f"🔔 Напоминание: {row['text']}")
        except TelegramForbiddenError:
            log.warning(
                "Бот заблокирован пользователем, напоминание #%s отброшено",
                row["id"],
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Не удалось отправить напоминание #%s", row["id"])
            await db.unclaim_reminder(row["id"])


async def daily_posts_loop(bot: Bot) -> None:
    log.info("Daily posts loop запущен")
    while True:
        try:
            need_retry = await _daily_tick(bot)
            if need_retry:
                delay = RETRY_DELAY_SECONDS
            else:
                delay = min(
                    utils.seconds_until_daily(config.DAILY_POST_TIME),
                    MAX_SLEEP_SECONDS,
                )
            await _sleep_until(delay, _topic_event)
        except asyncio.CancelledError:
            log.info("Daily posts loop остановлен")
            raise
        except Exception:
            log.exception("Ошибка итерации daily posts loop")
            await asyncio.sleep(MIN_PAUSE_SECONDS)


async def _daily_tick(bot: Bot) -> bool:
    """Возвращает True, если был restore и нужен повтор через 300 секунд."""
    post_hour, post_minute = utils.parse_daily_post_time(config.DAILY_POST_TIME)
    now_local = utils.now()
    if (now_local.hour, now_local.minute) < (post_hour, post_minute):
        return False
    today = now_local.date()

    claimed = await db.claim_daily_post(today)
    need_retry = False
    for row in claimed:
        chat_id = row["chat_id"]
        topic = row["topic"]
        prev_date = row["prev_date"]
        try:
            await send_post(bot, chat_id, topic)
            log.info("Ежедневный пост отправлен в чат %s", chat_id)
        except TelegramForbiddenError:
            log.warning(
                "Бот удалён/заблокирован в чате %s — ежедневный пост отменён",
                chat_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(
                "Не удалось отправить ежедневный пост в чат %s — вернём на следующий тик",
                chat_id,
            )
            await db.restore_daily_post(chat_id, prev_date)
            need_retry = True
    return need_retry
