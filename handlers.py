import logging
import re
from datetime import date

from aiogram import Bot, F, Router
from aiogram.dispatcher.middlewares.base import BaseMiddleware
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import BufferedInputFile, Message, User
from aiogram.utils.chat_action import ChatActionSender

import config
import db
import llm
import scheduler
import utils

log = logging.getLogger(__name__)

router = Router(name="main")

DENY_TEXT = "Соре бро, тебе нельзя говорить со мной, пока не разрешит трицератопс."

ADVISOR_SYSTEM_PROMPT = (
    "Ты — личный советник пользователя в Telegram. "
    "Отвечай по-русски, по делу и дружелюбно, без воды."
)

GROUP_SYSTEM_PROMPT = (
    "Ты советник в групповом чате. Тебе показана недавняя переписка группы: "
    "строки вида [Имя]: текст — реплики участников. Ответь на последнее "
    "обращение к тебе, учитывая контекст беседы. "
    "Отвечай по-русски, по делу, без воды."
)

LLM_ERROR_TEXT = "Не получилось получить ответ, попробуй ещё раз"

UNKNOWN_USER_TEXT = (
    "Не знаю такого пользователя. Пусть напишет боту или в группу, "
    "либо используй числовой ID или ответь этой командой на его сообщение."
)

ADMIN_REMOVE_TEXT = "Админа удалить нельзя"

MAX_MESSAGE_LEN = 4096

REMIND_HINT_TEXT = (
    "Не понял формат. Пример: «Напомни мне позвонить маме через 2 часа».\n"
    "Понимаю: через 30 минут, через 2 часа, через 3 дня, через неделю, "
    "«через час», «через полчаса», «через 90 мин», «через 2 ч»."
)

EDIT_USAGE_TEXT = "Использование: /edit_reminder ID [новый текст] [через 2 часа]"

DELETE_USAGE_TEXT = "Использование: /delete_reminder ID"

TOPIC_SAVED_TEXT = "Тема сохранена. Ежедневный пост будет в {time} МСК."

TOPIC_EMPTY_TEXT = "Тема не задана"

TOPIC_OFF_TEXT = "Тема сброшена, ежедневные посты выключены."

TOPIC_REQUIRED_TEXT = "Сначала задай тему через /topic"

_REMIND_TRIGGER_RE = r"(?i)^\s*напомни(?:ть)?\b"
_REMIND_PREFIX_RE = re.compile(r"(?i)^\s*напомни(?:ть)?\s+(?:мне\s+)?")

_bot_info: User | None = None


class SeenUserMiddleware(BaseMiddleware):
    """Запоминает каждого человека, который написал боту (outer middleware)."""

    async def __call__(self, handler, event: Message, data: dict):
        user = event.from_user
        if user is not None and not user.is_bot:
            try:
                await db.upsert_seen_user(user.id, user.username, user.first_name)
            except Exception:
                log.exception("Не удалось запомнить пользователя %s", user.id)
        return await handler(event, data)


async def is_allowed(user_id: int) -> bool:
    return await db.is_user_allowed(user_id)


async def _get_bot_info(bot: Bot) -> User:
    global _bot_info
    if _bot_info is None:
        _bot_info = await bot.get_me()
    return _bot_info


async def _resolve_user_id(message: Message, arg: str | None) -> int | None:
    if arg:
        arg = arg.strip()
        if arg.isdigit():
            return int(arg)
        if arg.startswith("@") and len(arg) > 1:
            return await db.find_user_by_username(arg[1:])
        return None
    reply = message.reply_to_message
    if reply is not None and reply.from_user is not None and not reply.from_user.is_bot:
        return reply.from_user.id
    return None


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer("Бот запущен и работает.")


HELP_TEXT = (
    "Что я умею.\n\n"
    "В личке просто напиши вопрос — отвечу. В группе упомяни меня (@имя) "
    "или ответь на моё сообщение.\n\n"
    "Команды:\n"
    "/start — проверка, что бот жив\n"
    "/help — список команд\n"
    "/model — какая модель отвечает\n"
    "/reminders — активные напоминания\n"
    "/edit_reminder ID [текст] [через ...] — изменить напоминание\n"
    "/delete_reminder ID — удалить напоминание\n"
    "/topic <тема> — тема ежедневного поста в группе, /topic off — выключить\n"
    "/photos — сохранённые фото\n"
    "/photo ID — показать сохранённое фото\n"
    "/regen ID описание — новая версия фото через ИИ\n\n"
    "Фото: пришли мне его в личку (как фото, не файлом) — сохраню и дам номер.\n"
    "Напоминание: напиши в личке «Напомни мне позвонить маме через 2 часа»."
)

HELP_ADMIN_TEXT = (
    "\n\nДля админа:\n"
    "/post_now — сразу отправить пост по теме (в группе)\n"
    "/allow <id | @username | ответом> — разрешить пользователя\n"
    "/disallow <id | @username | ответом> — убрать разрешение\n"
    "/allowed — список разрешённых"
)

UNKNOWN_COMMAND_TEXT = "Не знаю такой команды. Список: /help"

GROUP_ONLY_TEXT = "Эта команда работает только в группе."


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    text = HELP_TEXT
    if message.from_user is not None and message.from_user.id == config.ADMIN_ID:
        text += HELP_ADMIN_TEXT
    await message.answer(text)


@router.message(Command("model"))
async def cmd_model(message: Message) -> None:
    if not await is_allowed(message.from_user.id):
        await message.answer(DENY_TEXT)
        return
    await message.answer(f"Модель: {config.LLM_MODEL}")


@router.message(Command("allow"))
async def cmd_allow(message: Message, command: CommandObject) -> None:
    if message.from_user.id != config.ADMIN_ID:
        return
    target = await _resolve_user_id(message, command.args)
    if target is None:
        await message.answer(UNKNOWN_USER_TEXT)
        return
    await db.allow_user(target, message.from_user.id)
    names = await db.get_user_names([target])
    name = names.get(target)
    suffix = f" ({name})" if name else ""
    await message.answer(f"Разрешил {target}{suffix}")


@router.message(Command("disallow"))
async def cmd_disallow(message: Message, command: CommandObject) -> None:
    if message.from_user.id != config.ADMIN_ID:
        return
    target = await _resolve_user_id(message, command.args)
    if target is None:
        await message.answer(UNKNOWN_USER_TEXT)
        return
    if target == config.ADMIN_ID:
        await message.answer(ADMIN_REMOVE_TEXT)
        return
    removed = await db.disallow_user(target)
    if not removed:
        await message.answer("Этого пользователя и так нет в списке разрешённых.")
        return
    names = await db.get_user_names([target])
    name = names.get(target)
    suffix = f" ({name})" if name else ""
    await message.answer(f"Убрал {target}{suffix}")


@router.message(Command("allowed"))
async def cmd_allowed(message: Message) -> None:
    if message.from_user.id != config.ADMIN_ID:
        return
    rows = await db.list_allowed()
    lines = ["Разрешённые пользователи:", f"{config.ADMIN_ID} — админ"]
    for row in rows:
        if row["user_id"] == config.ADMIN_ID:
            continue
        name = f" (@{row['username']})" if row["username"] else ""
        lines.append(f"{row['user_id']}{name} — с {utils.format_dt(row['added_at'])}")
    await message.answer("\n".join(lines))


@router.message(Command("topic"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_topic(message: Message, command: CommandObject) -> None:
    if not await is_allowed(message.from_user.id):
        await message.answer(DENY_TEXT)
        return
    chat_id = message.chat.id
    await db.ensure_chat(chat_id, message.chat.type)
    args = (command.args or "").strip()
    if not args:
        topic = await db.get_topic(chat_id)
        await message.answer(topic if topic else TOPIC_EMPTY_TEXT)
        return
    if args.lower() == "off":
        await db.set_topic(chat_id, None)
        await message.answer(TOPIC_OFF_TEXT)
        return
    await db.set_topic(chat_id, args)
    scheduler.notify_topic()
    await message.answer(
        TOPIC_SAVED_TEXT.format(time=config.DAILY_POST_TIME)
    )


@router.message(Command("post_now"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_post_now(message: Message) -> None:
    if message.from_user.id != config.ADMIN_ID:
        return
    chat_id = message.chat.id
    topic = await db.get_topic(chat_id)
    if not topic:
        await message.answer(TOPIC_REQUIRED_TEXT)
        return
    try:
        async with ChatActionSender(chat_id=chat_id, bot=message.bot):
            await scheduler.send_post(message.bot, chat_id, topic)
    except TelegramForbiddenError:
        log.warning("Бот не может отправить пост в чат %s", chat_id)
    except llm.LLMError:
        log.exception("Ошибка генерации поста в чате %s", chat_id)
        await message.answer(LLM_ERROR_TEXT)


@router.message(Command("reminders"))
async def cmd_reminders(message: Message) -> None:
    if not await is_allowed(message.from_user.id):
        await message.answer(DENY_TEXT)
        return
    rows = await db.list_reminders(message.from_user.id)
    if not rows:
        await message.answer("Активных напоминаний нет.")
        return
    lines = ["🔔 Активные напоминания:", ""]
    for row in rows:
        lines.append(f"#{row['id']}")
        lines.append(row["text"])
        lines.append(f"{utils.format_dt(row['remind_at_utc'])} МСК")
        lines.append("")
    await message.answer("\n".join(lines).rstrip())


@router.message(Command("edit_reminder"))
async def cmd_edit_reminder(message: Message, command: CommandObject) -> None:
    if not await is_allowed(message.from_user.id):
        await message.answer(DENY_TEXT)
        return
    args = (command.args or "").strip()
    parts = args.split(None, 1)
    if not parts or not parts[0].isdigit():
        await message.answer(EDIT_USAGE_TEXT)
        return
    reminder_id = int(parts[0])
    rest = parts[1].strip() if len(parts) > 1 else ""

    new_text: str | None = None
    new_remind_at = None
    if rest:
        delay, cleaned = utils.extract_delay(rest)
        cleaned = cleaned.strip()
        if delay is not None:
            new_remind_at = utils.utc_after(delay)
            if cleaned:
                new_text = cleaned[0].upper() + cleaned[1:]
        elif cleaned:
            new_text = cleaned[0].upper() + cleaned[1:]

    if new_text is None and new_remind_at is None:
        await message.answer(EDIT_USAGE_TEXT)
        return

    updated = await db.update_reminder(
        reminder_id,
        message.from_user.id,
        text=new_text,
        remind_at_utc=new_remind_at,
    )
    if not updated:
        await message.answer(f"Напоминание #{reminder_id} не найдено")
        return
    scheduler.notify_reminders()
    await message.answer(f"Обновил напоминание #{reminder_id}.")


@router.message(Command("delete_reminder"))
async def cmd_delete_reminder(message: Message, command: CommandObject) -> None:
    if not await is_allowed(message.from_user.id):
        await message.answer(DENY_TEXT)
        return
    args = (command.args or "").strip()
    if not args.isdigit():
        await message.answer(DELETE_USAGE_TEXT)
        return
    reminder_id = int(args)
    deleted = await db.delete_reminder(reminder_id, message.from_user.id)
    if not deleted:
        await message.answer(f"Напоминание #{reminder_id} не найдено")
        return
    scheduler.notify_reminders()
    await message.answer(f"Напоминание #{reminder_id} удалено.")


@router.message(F.chat.type == "private", F.text.regexp(_REMIND_TRIGGER_RE))
async def handle_remind(message: Message) -> None:
    if not await is_allowed(message.from_user.id):
        await message.answer(DENY_TEXT)
        return

    rest = _REMIND_PREFIX_RE.sub("", message.text, count=1)
    delay, cleaned = utils.extract_delay(rest)
    reminder_text = cleaned.strip()
    if delay is None or not reminder_text:
        await message.answer(REMIND_HINT_TEXT)
        return

    reminder_text = reminder_text[0].upper() + reminder_text[1:]
    remind_at_utc = utils.utc_after(delay)
    await db.add_reminder(
        message.chat.id, message.from_user.id, reminder_text, remind_at_utc
    )
    scheduler.notify_reminders()
    await message.answer(f"Записал. Напомню {utils.format_dt(remind_at_utc)} МСК.")


@router.message(F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def private_advisor(message: Message) -> None:
    user_id = message.from_user.id
    chat_id = message.chat.id

    if not await is_allowed(user_id):
        await message.answer(DENY_TEXT)
        return

    await db.ensure_chat(chat_id, message.chat.type)
    await db.save_message(chat_id, user_id, message.text)

    history = await db.get_history(chat_id, limit=20)
    llm_messages: list[dict] = [{"role": "system", "content": ADVISOR_SYSTEM_PROMPT}]
    for row in history:
        role = "user" if row["user_id"] is not None else "assistant"
        llm_messages.append({"role": role, "content": row["text"]})

    try:
        async with ChatActionSender(chat_id=chat_id, bot=message.bot):
            reply = await llm.call_llm(llm_messages)
    except llm.LLMError:
        log.exception("Ошибка LLM в личном чате %s", chat_id)
        await message.answer(LLM_ERROR_TEXT)
        return

    await db.save_message(chat_id, None, reply)
    for chunk in utils.split_text(reply, MAX_MESSAGE_LEN):
        await message.answer(chunk)


@router.message(F.chat.type.in_({"group", "supergroup"}), F.text, ~F.text.startswith("/"))
async def group_mention(message: Message) -> None:
    user = message.from_user
    if user is None or user.is_bot:
        return
    chat_id = message.chat.id

    await db.ensure_chat(chat_id, message.chat.type)
    await db.save_message(chat_id, user.id, message.text)

    bot_info = await _get_bot_info(message.bot)
    bot_username = bot_info.username or ""
    text = message.text

    reply_to_bot = (
        message.reply_to_message is not None
        and message.reply_to_message.from_user is not None
        and message.reply_to_message.from_user.id == bot_info.id
    )
    mentioned = bool(bot_username) and f"@{bot_username}".lower() in text.lower()
    if not (mentioned or reply_to_bot):
        return

    if not await is_allowed(user.id):
        await message.answer(DENY_TEXT)
        return

    history = await db.get_history(chat_id, limit=30)
    user_ids = sorted({row["user_id"] for row in history if row["user_id"] is not None})
    names = await db.get_user_names(user_ids)

    llm_messages: list[dict] = [{"role": "system", "content": GROUP_SYSTEM_PROMPT}]
    for row in history:
        if row["user_id"] is None:
            llm_messages.append({"role": "assistant", "content": row["text"]})
        else:
            name = names.get(row["user_id"], "Участник")
            llm_messages.append({"role": "user", "content": f"[{name}]: {row['text']}"})

    if llm_messages and llm_messages[-1]["role"] == "user" and bot_username:
        llm_messages[-1]["content"] = (
            llm_messages[-1]["content"].replace(f"@{bot_username}", "").strip()
        )

    try:
        async with ChatActionSender(chat_id=chat_id, bot=message.bot):
            reply = await llm.call_llm(llm_messages)
    except llm.LLMError:
        log.exception("Ошибка LLM в группе %s", chat_id)
        await message.answer(LLM_ERROR_TEXT)
        return

    await db.save_message(chat_id, None, reply)
    chunks = utils.split_text(reply, MAX_MESSAGE_LEN)
    await message.reply(chunks[0])
    for chunk in chunks[1:]:
        await message.answer(chunk)


PHOTO_SAVED_TEXT = (
    "Сохранил фото #{id}. Новая версия через ИИ: /regen {id} описание "
    "(или ответь на фото командой /regen описание)."
)

REGEN_USAGE_TEXT = (
    "Использование:\n"
    "/regen ID описание — по сохранённому фото\n"
    "или ответь на фото командой /regen описание, "
    "или пришли фото с подписью /regen описание.\n"
    "Список фото: /photos"
)

REGEN_DEFAULT_PROMPT = (
    "Создай новую версию этого изображения: сохрани сюжет и композицию, "
    "но перерисуй его заново, с чуть другими деталями."
)

REGEN_DISABLED_TEXT = "Генерация картинок не настроена (нет переменной IMAGE_MODEL)."

REGEN_ERROR_TEXT = "Не получилось сгенерировать изображение, попробуй ещё раз позже."

REGEN_LIMIT_TEXT = "Дневной лимит генераций исчерпан ({limit}). Завтра можно снова."

PHOTO_NOT_FOUND_TEXT = "Фото #{id} не найдено."

NO_PHOTOS_TEXT = "Сохранённых фото нет. Просто пришли мне фото."

_regen_counts: dict[tuple[int, date], int] = {}


def _regen_try_take(user_id: int) -> bool:
    """Списывает одну генерацию из дневного лимита; админ и лимит 0 — без ограничений."""
    if user_id == config.ADMIN_ID or config.IMAGE_DAILY_LIMIT <= 0:
        return True
    today = utils.now().date()
    for key in [k for k in _regen_counts if k[1] != today]:
        del _regen_counts[key]
    used = _regen_counts.get((user_id, today), 0)
    if used >= config.IMAGE_DAILY_LIMIT:
        return False
    _regen_counts[(user_id, today)] = used + 1
    return True


def _regen_refund(user_id: int) -> None:
    key = (user_id, utils.now().date())
    if key in _regen_counts and _regen_counts[key] > 0:
        _regen_counts[key] -= 1


@router.message(F.chat.type == "private", F.photo)
async def private_photo(message: Message) -> None:
    if not await is_allowed(message.from_user.id):
        await message.answer(DENY_TEXT)
        return
    photo = message.photo[-1]
    photo_id = await db.save_photo(
        message.chat.id,
        message.from_user.id,
        photo.file_id,
        photo.file_unique_id,
        message.caption,
    )
    await message.answer(PHOTO_SAVED_TEXT.format(id=photo_id))


@router.message(Command("photos"), F.chat.type == "private")
async def cmd_photos(message: Message) -> None:
    if not await is_allowed(message.from_user.id):
        await message.answer(DENY_TEXT)
        return
    rows = await db.list_photos(message.from_user.id, limit=10)
    if not rows:
        await message.answer(NO_PHOTOS_TEXT)
        return
    lines = ["🖼 Сохранённые фото (последние 10):", ""]
    for row in rows:
        caption = (row["caption"] or "").strip().replace("\n", " ")
        if len(caption) > 40:
            caption = caption[:40] + "…"
        line = f"#{row['id']} — {utils.format_dt(row['created_at'])} МСК"
        if caption:
            line += f" — {caption}"
        lines.append(line)
    lines.append("")
    lines.append("Показать: /photo ID. Новая версия: /regen ID описание")
    await message.answer("\n".join(lines))


@router.message(Command("photo"), F.chat.type == "private")
async def cmd_photo(message: Message, command: CommandObject) -> None:
    if not await is_allowed(message.from_user.id):
        await message.answer(DENY_TEXT)
        return
    args = (command.args or "").strip()
    if not args.isdigit():
        await message.answer("Использование: /photo ID (список: /photos)")
        return
    row = await db.get_photo(int(args), message.from_user.id)
    if row is None:
        await message.answer(PHOTO_NOT_FOUND_TEXT.format(id=args))
        return
    try:
        await message.answer_photo(row["file_id"], caption=f"#{row['id']}")
    except TelegramBadRequest:
        log.exception("Не удалось отправить сохранённое фото #%s", row["id"])
        await message.answer("Не удалось отправить это фото.")


@router.message(Command("regen"), F.chat.type == "private")
async def cmd_regen(message: Message, command: CommandObject) -> None:
    user_id = message.from_user.id
    chat_id = message.chat.id
    if not await is_allowed(user_id):
        await message.answer(DENY_TEXT)
        return
    if not config.IMAGE_MODEL:
        await message.answer(REGEN_DISABLED_TEXT)
        return

    args = (command.args or "").strip()
    source_photo = None
    if message.photo:
        source_photo = message.photo[-1]
    elif message.reply_to_message is not None and message.reply_to_message.photo:
        source_photo = message.reply_to_message.photo[-1]

    if source_photo is not None:
        file_id = source_photo.file_id
        source_id = await db.save_photo(
            chat_id, user_id, file_id, source_photo.file_unique_id, None
        )
        prompt = args
    else:
        parts = args.split(None, 1)
        if not parts or not parts[0].isdigit():
            await message.answer(REGEN_USAGE_TEXT)
            return
        source_id = int(parts[0])
        row = await db.get_photo(source_id, user_id)
        if row is None:
            await message.answer(PHOTO_NOT_FOUND_TEXT.format(id=source_id))
            return
        file_id = row["file_id"]
        prompt = parts[1].strip() if len(parts) > 1 else ""

    prompt = prompt or REGEN_DEFAULT_PROMPT

    if not _regen_try_take(user_id):
        await message.answer(REGEN_LIMIT_TEXT.format(limit=config.IMAGE_DAILY_LIMIT))
        return

    status = await message.answer("Генерирую, это может занять до пары минут…")
    try:
        async with ChatActionSender.upload_photo(chat_id=chat_id, bot=message.bot):
            buffer = await message.bot.download(file_id)
            image = buffer.read()
            result = await llm.edit_image(image, prompt)
    except TelegramBadRequest:
        _regen_refund(user_id)
        log.exception("Не удалось скачать фото #%s из Telegram", source_id)
        await message.answer("Не удалось скачать фото из Telegram (возможно, файл слишком большой).")
        return
    except llm.LLMError as e:
        _regen_refund(user_id)
        log.exception("Ошибка генерации изображения для фото #%s", source_id)
        text = REGEN_ERROR_TEXT
        if user_id == config.ADMIN_ID:
            text += f"\n\n{e}"
        await message.answer(text)
        return
    finally:
        try:
            await status.delete()
        except TelegramBadRequest:
            pass

    caption = f"Новая версия фото #{source_id}"
    upload = BufferedInputFile(result, filename="regen.png")
    try:
        sent = await message.answer_photo(upload, caption=caption)
    except TelegramBadRequest:
        # слишком большая картинка для «фото» — отправим файлом
        sent = await message.answer_document(
            BufferedInputFile(result, filename="regen.png"), caption=caption
        )

    if sent.photo:
        new_photo = sent.photo[-1]
        new_id = await db.save_photo(
            chat_id,
            user_id,
            new_photo.file_id,
            new_photo.file_unique_id,
            f"Новая версия #{source_id}",
        )
        await message.answer(f"Сохранил как #{new_id}. Ещё раз: /regen {new_id} описание")


@router.message(F.chat.type == "private", F.text.startswith("/"))
async def unknown_command(message: Message) -> None:
    command = message.text.split()[0].split("@")[0].lower()
    if command in ("/topic", "/post_now"):
        await message.answer(GROUP_ONLY_TEXT)
        return
    await message.answer(UNKNOWN_COMMAND_TEXT)
