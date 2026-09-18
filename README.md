# Telegram-бот: личный советник, напоминания, групповой режим (aiogram 3)

Long polling (без webhook). Postgres (Neon), LLM через OpenAI-compatible API
(vibecode.moe). Логика времени — Europe/Moscow, в БД — UTC.

## Что умеет

- Личный советник в ЛС с историей диалога в БД
- Напоминания: «напомни ... через 30 минут / 2 часа / 3 дня / неделю»,
  фоновая проверка раз в минуту, редактирование и удаление
- Группы: читает переписку, отвечает при обращении (@упоминание или reply),
  контекст — последние 30 сообщений
- Ежедневный тематический пост в группу (по умолчанию 09:00 МСК)
- Контроль доступа: отвечают только админ (ADMIN_ID) и разрешённые пользователи

## Команды

| Команда | Описание |
|---|---|
| `/start` | проверка, что бот жив |
| `напомни [мне] <текст> через <время>` | создать напоминание |
| `/reminders` | список активных напоминаний |
| `/edit_reminder ID [новый текст] [через 2 часа]` | изменить текст и/или время |
| `/delete_reminder ID` | удалить напоминание |
| `/topic <текст>` | задать тему группы (в группе; `/topic` — показать, `/topic off` — выключить) |
| `/post_now` | сразу отправить пост по теме (только админ) |
| `/allow <id \| @username \| reply>` | разрешить пользователя (только админ) |
| `/disallow <id \| @username \| reply>` | убрать разрешение (только админ) |
| `/allowed` | список разрешённых (только админ) |

## Локальный запуск (Windows PowerShell)

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env    # заполни BOT_TOKEN, DATABASE_URL, VIBECODE_API_KEY, ADMIN_ID
python bot.py
```

## Настройка BotFather

1. `/newbot` — получить `BOT_TOKEN`.
2. Чтобы бот видел все сообщения в группе: `/setprivacy` → выбрать бота → **Disable**.
3. После смены privacy **удалить бота из группы и добавить заново** — иначе он не начнёт получать историю.

## Postgres (Neon)

1. Создать проект на [neon.tech](https://neon.tech).
2. Скопировать Pooled connection string в `.env` (реальные креды нигде не коммитить):

   ```
   DATABASE_URL=postgresql://USER:PASSWORD@HOST/DBNAME?sslmode=require
   ```

3. Таблицы (`chats`, `messages`, `reminders`, `allowed_users`, `seen_users`) создаются автоматически при первом запуске.

## Деплой на Render

- Тип сервиса: **Background Worker** (постоянный процесс).
  Возможен **Web Service** — тогда бот поднимает health-эндпоинт
  (`GET /` и `GET /health` → `ok`) на порту из переменной `PORT`.
- Build Command: `pip install -r requirements.txt`
- Start Command: `python bot.py`
- Переменные окружения — внести через **Environment** в интерфейсе Render
  (не файлом):

  | Переменная | Значение |
  |---|---|
  | `BOT_TOKEN` | токен от @BotFather |
  | `VIBECODE_API_KEY` | ключ LLM |
  | `DATABASE_URL` | строка подключения Neon |
  | `ADMIN_ID` | твой Telegram user_id |
  | `TIMEZONE` | `Europe/Moscow` |
  | `DAILY_POST_TIME` | `09:00` |
  | `LLM_MODEL` | `gpt-5.6-terra` |
  | `PYTHON_VERSION` | `3.12.8` |

- **Важно:** перед запуском на Render останови локального бота — два клиента
  на один токен конфликтуют (Telegram вернёт 409 Conflict).
