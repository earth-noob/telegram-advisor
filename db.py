import logging
import ssl
from datetime import date, datetime
from urllib.parse import unquote, urlsplit

import asyncpg

import config

log = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None

_SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS chats (
        chat_id BIGINT PRIMARY KEY,
        type TEXT NOT NULL,
        topic TEXT,
        daily_post_date DATE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS messages (
        id BIGSERIAL PRIMARY KEY,
        chat_id BIGINT NOT NULL,
        user_id BIGINT,
        text TEXT NOT NULL,
        ts TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_messages_chat_ts ON messages (chat_id, ts)",
    """
    CREATE TABLE IF NOT EXISTS reminders (
        id BIGSERIAL PRIMARY KEY,
        chat_id BIGINT NOT NULL,
        user_id BIGINT NOT NULL,
        text TEXT NOT NULL,
        remind_at_utc TIMESTAMPTZ NOT NULL,
        sent BOOLEAN NOT NULL DEFAULT FALSE
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_reminders_pending ON reminders (remind_at_utc, sent)",
    """
    CREATE TABLE IF NOT EXISTS allowed_users (
        user_id BIGINT PRIMARY KEY,
        added_by BIGINT NOT NULL,
        added_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS seen_users (
        user_id BIGINT PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        last_seen TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
]


def _ssl_context(sslmode: str) -> ssl.SSLContext | None:
    if sslmode in ("disable", "allow", "prefer"):
        return None
    ctx = ssl.create_default_context()
    if sslmode == "require":
        # Аналог libpq: шифрование обязательно, сертификат не проверяем
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _connection_kwargs() -> dict:
    parts = urlsplit(config.DATABASE_URL)
    query = dict(pair.split("=", 1) for pair in parts.query.split("&") if "=" in pair)
    sslmode = query.get("sslmode")
    if sslmode is None:
        host = parts.hostname or ""
        sslmode = "disable" if host in ("localhost", "127.0.0.1", "::1") else "require"
    return {
        "host": parts.hostname,
        "port": parts.port or 5432,
        "user": unquote(parts.username or ""),
        "password": unquote(parts.password or ""),
        "database": parts.path.lstrip("/") or None,
        "ssl": _ssl_context(sslmode),
    }


async def init_db() -> None:
    """Создаёт общий на всё приложение пул подключений к PostgreSQL."""
    global _pool
    if _pool is not None:
        return
    if not config.DATABASE_URL:
        raise RuntimeError("DATABASE_URL не задан — подключение к БД невозможно")
    try:
        _pool = await asyncpg.create_pool(
            **_connection_kwargs(),
            min_size=1,
            max_size=5,
            statement_cache_size=0,
            command_timeout=30,
            max_inactive_connection_lifetime=240,
        )
    except Exception as e:
        _pool = None
        log.error("Не удалось подключиться к PostgreSQL: %s", e)
        raise RuntimeError(f"Database connection failed: {e}") from e
    print("Database connected")


async def create_tables() -> None:
    """Создаёт таблицы и индексы, если их ещё нет."""
    try:
        pool = get_pool()
        async with pool.acquire() as conn:
            for statement in _SCHEMA_STATEMENTS:
                await conn.execute(statement)
    except Exception as e:
        log.error("Не удалось создать таблицы: %s", e)
        raise RuntimeError(f"Database tables initialization failed: {e}") from e
    print("Database tables initialized")


async def close_db() -> None:
    """Закрывает общий пул подключений."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        log.info("PostgreSQL pool closed")


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Пул БД не инициализирован (сначала вызови init_db)")
    return _pool


async def ensure_chat(chat_id: int, chat_type: str) -> None:
    """Создаёт чат, если его нет, иначе обновляет тип."""
    await get_pool().execute(
        """
        INSERT INTO chats (chat_id, type) VALUES ($1, $2)
        ON CONFLICT (chat_id) DO UPDATE SET type = EXCLUDED.type
        """,
        chat_id,
        chat_type,
    )


async def save_message(chat_id: int, user_id: int | None, text: str) -> None:
    """Сохраняет сообщение; для ответов бота user_id = NULL."""
    await get_pool().execute(
        "INSERT INTO messages (chat_id, user_id, text) VALUES ($1, $2, $3)",
        chat_id,
        user_id,
        text,
    )


async def get_history(chat_id: int, limit: int = 20) -> list[dict]:
    """Последние limit сообщений чата в хронологическом порядке."""
    rows = await get_pool().fetch(
        """
        SELECT user_id, text
        FROM (
            SELECT user_id, text, ts, id
            FROM messages
            WHERE chat_id = $1
            ORDER BY ts DESC, id DESC
            LIMIT $2
        ) last_messages
        ORDER BY ts ASC, id ASC
        """,
        chat_id,
        limit,
    )
    return [dict(row) for row in rows]


async def add_reminder(chat_id: int, user_id: int, text: str, remind_at_utc: datetime) -> int:
    return await get_pool().fetchval(
        """
        INSERT INTO reminders (chat_id, user_id, text, remind_at_utc)
        VALUES ($1, $2, $3, $4)
        RETURNING id
        """,
        chat_id,
        user_id,
        text,
        remind_at_utc,
    )


async def list_reminders(user_id: int) -> list[asyncpg.Record]:
    """Активные (sent = FALSE) напоминания пользователя."""
    return await get_pool().fetch(
        """
        SELECT id, chat_id, text, remind_at_utc
        FROM reminders
        WHERE user_id = $1 AND sent = FALSE
        ORDER BY remind_at_utc
        """,
        user_id,
    )


async def update_reminder(
    reminder_id: int,
    user_id: int,
    text: str | None = None,
    remind_at_utc: datetime | None = None,
) -> bool:
    """Обновляет текст и/или время своего неотправленного напоминания."""
    sets: list[str] = []
    args: list[object] = []
    if text is not None:
        args.append(text)
        sets.append(f"text = ${len(args)}")
    if remind_at_utc is not None:
        args.append(remind_at_utc)
        sets.append(f"remind_at_utc = ${len(args)}")
    if not sets:
        return False
    args.append(reminder_id)
    id_ph = f"${len(args)}"
    args.append(user_id)
    user_ph = f"${len(args)}"
    tag = await get_pool().execute(
        f"""
        UPDATE reminders SET {", ".join(sets)}
        WHERE id = {id_ph} AND user_id = {user_ph} AND sent = FALSE
        """,
        *args,
    )
    return tag == "UPDATE 1"


async def delete_reminder(reminder_id: int, user_id: int) -> bool:
    """Удаляет своё неотправленное напоминание."""
    tag = await get_pool().execute(
        "DELETE FROM reminders WHERE id = $1 AND user_id = $2 AND sent = FALSE",
        reminder_id,
        user_id,
    )
    return tag == "DELETE 1"


async def claim_due_reminders(limit: int = 20) -> list[asyncpg.Record]:
    """Атомарно помечает due-напоминания как sent и возвращает их.

    FOR UPDATE SKIP LOCKED — чтобы при параллельных воркерах одно
    напоминание не забрали дважды.
    """
    return await get_pool().fetch(
        """
        UPDATE reminders SET sent = TRUE
        WHERE id IN (
            SELECT id
            FROM reminders
            WHERE sent = FALSE AND remind_at_utc <= NOW()
            ORDER BY remind_at_utc
            LIMIT $1
            FOR UPDATE SKIP LOCKED
        )
        RETURNING id, chat_id, user_id, text
        """,
        limit,
    )


async def unclaim_reminder(reminder_id: int) -> None:
    """Возвращает напоминание в очередь, если отправить не удалось."""
    await get_pool().execute(
        "UPDATE reminders SET sent = FALSE WHERE id = $1",
        reminder_id,
    )


async def upsert_seen_user(user_id: int, username: str | None, first_name: str | None) -> None:
    """Запоминает пользователя: username/first_name/last_seen."""
    await get_pool().execute(
        """
        INSERT INTO seen_users (user_id, username, first_name, last_seen)
        VALUES ($1, $2, $3, NOW())
        ON CONFLICT (user_id) DO UPDATE
        SET username = COALESCE(EXCLUDED.username, seen_users.username),
            first_name = COALESCE(EXCLUDED.first_name, seen_users.first_name),
            last_seen = NOW()
        """,
        user_id,
        username,
        first_name,
    )


async def find_user_by_username(username: str) -> int | None:
    """Ищет user_id по username (без @, регистр не важен)."""
    row = await get_pool().fetchrow(
        "SELECT user_id FROM seen_users WHERE lower(username) = lower($1)",
        username,
    )
    return row["user_id"] if row else None


async def get_user_names(user_ids: list[int]) -> dict[int, str]:
    """user_id -> '@username' или first_name."""
    rows = await get_pool().fetch(
        "SELECT user_id, username, first_name FROM seen_users WHERE user_id = ANY($1)",
        user_ids,
    )
    names: dict[int, str] = {}
    for row in rows:
        if row["username"]:
            names[row["user_id"]] = f"@{row['username']}"
        elif row["first_name"]:
            names[row["user_id"]] = row["first_name"]
    return names


async def allow_user(user_id: int, added_by: int) -> None:
    await get_pool().execute(
        """
        INSERT INTO allowed_users (user_id, added_by)
        VALUES ($1, $2)
        ON CONFLICT (user_id)
        DO UPDATE SET added_by = EXCLUDED.added_by, added_at = NOW()
        """,
        user_id,
        added_by,
    )


async def disallow_user(user_id: int) -> bool:
    tag = await get_pool().execute(
        "DELETE FROM allowed_users WHERE user_id = $1",
        user_id,
    )
    return tag == "DELETE 1"


async def list_allowed() -> list[asyncpg.Record]:
    return await get_pool().fetch(
        """
        SELECT a.user_id, a.added_by, a.added_at, s.username
        FROM allowed_users a
        LEFT JOIN seen_users s ON s.user_id = a.user_id
        ORDER BY a.added_at
        """
    )


async def is_user_allowed(user_id: int) -> bool:
    if user_id == config.ADMIN_ID:
        return True
    return await get_pool().fetchval(
        "SELECT EXISTS (SELECT 1 FROM allowed_users WHERE user_id = $1)",
        user_id,
    )


async def next_reminder_time() -> datetime | None:
    """Ближайшее время неотправленного напоминания (UTC) или None."""
    return await get_pool().fetchval(
        "SELECT min(remind_at_utc) FROM reminders WHERE sent = FALSE"
    )


async def set_topic(chat_id: int, topic: str | None) -> None:
    """Обновляет тему чата (None — сбросить)."""
    await get_pool().execute(
        "UPDATE chats SET topic = $2 WHERE chat_id = $1",
        chat_id,
        topic,
    )


async def get_topic(chat_id: int) -> str | None:
    return await get_pool().fetchval(
        "SELECT topic FROM chats WHERE chat_id = $1",
        chat_id,
    )


async def claim_daily_post(today: date) -> list[asyncpg.Record]:
    """Атомарно забирает чаты для ежедневного поста.

    Для всех чатов с темой, по которым пост сегодня ещё не отправлялся,
    выставляет daily_post_date = today и возвращает (chat_id, topic, prev_date).
    """
    return await get_pool().fetch(
        """
        WITH claimed AS (
            SELECT chat_id, topic, daily_post_date AS prev_date
            FROM chats
            WHERE topic IS NOT NULL
              AND (daily_post_date IS NULL OR daily_post_date < $1)
            FOR UPDATE SKIP LOCKED
        )
        UPDATE chats c
        SET daily_post_date = $1
        FROM claimed
        WHERE c.chat_id = claimed.chat_id
        RETURNING c.chat_id, claimed.topic, claimed.prev_date
        """,
        today,
    )


async def restore_daily_post(chat_id: int, prev_date: date | None) -> None:
    """Возвращает прежнее daily_post_date, если пост отправить не удалось."""
    await get_pool().execute(
        "UPDATE chats SET daily_post_date = $2 WHERE chat_id = $1",
        chat_id,
        prev_date,
    )
