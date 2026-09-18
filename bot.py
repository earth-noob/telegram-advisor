import asyncio
import logging
import os

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

import config
import db
import scheduler
from handlers import SeenUserMiddleware, router

log = logging.getLogger(__name__)


async def _start_health_server(port: int) -> web.AppRunner:
    async def ok(_request: web.Request) -> web.Response:
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/", ok)
    app.router.add_get("/health", ok)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    return runner


async def main() -> None:
    if not config.BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан")

    bot = Bot(token=config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=None))
    dp = Dispatcher()
    dp.message.outer_middleware(SeenUserMiddleware())
    dp.include_router(router)

    health_runner: web.AppRunner | None = None
    background_tasks: list[asyncio.Task] = []
    try:
        try:
            await db.init_db()
            await db.create_tables()
        except Exception as e:
            log.error("Инициализация БД не удалась: %s", e)
            raise SystemExit(1) from e

        port_raw = os.getenv("PORT")
        if port_raw:
            try:
                port = int(port_raw)
            except ValueError:
                log.warning("PORT=%r не число — health-сервер не запущен", port_raw)
            else:
                health_runner = await _start_health_server(port)
                log.info("Health server: http://0.0.0.0:%d/health", port)

        background_tasks.append(asyncio.create_task(scheduler.scheduler_loop(bot)))
        background_tasks.append(asyncio.create_task(scheduler.daily_posts_loop(bot)))
        await bot.delete_webhook(drop_pending_updates=False)
        log.info("Запуск long polling")
        await dp.start_polling(bot)
    finally:
        for task in background_tasks:
            task.cancel()
        if health_runner is not None:
            await health_runner.cleanup()
        await db.close_db()
        await bot.session.close()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
