"""Entry point: startup, shutdown, bot polling + web server."""

import asyncio

from aiogram import Bot
from aiohttp import web

from bot.cleanup import cleanup_session_files, periodic_cleanup, session_cleanup
from bot.config import API_URL, BOT_TOKEN, WEB_PORT, EXTERNAL_URL, log
from bot.state import bot, dp, sessions, web_files
from bot.web import create_web_app

# Import handlers to register them on the router
import bot.handlers  # noqa: F401


async def on_startup() -> None:
    log.info("Bot starting, using API at %s", API_URL)

    # Переключение с официального API на локальный (нужно один раз)
    try:
        official_bot = Bot(token=BOT_TOKEN)
        await official_bot.log_out()
        await official_bot.session.close()
        log.info("Logged out from official API, switched to local")
    except Exception as e:
        log.info("logOut skipped (already local or cooldown): %s", e)

    await bot.delete_webhook(drop_pending_updates=True)
    asyncio.create_task(periodic_cleanup())
    asyncio.create_task(session_cleanup())


async def on_shutdown() -> None:
    log.info("Bot shutting down, cleaning downloads...")
    # Don't remove web files on shutdown — they should persist
    for sid in list(sessions):
        if sid not in web_files:
            cleanup_session_files(sid)
    sessions.clear()


async def run_all() -> None:
    """Run bot polling and web server concurrently."""
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    # Web server
    app = create_web_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", WEB_PORT)
    await site.start()
    log.info("Web server started on port %s, external URL: %s", WEB_PORT, EXTERNAL_URL)

    # Bot polling
    try:
        await dp.start_polling(bot)
    finally:
        await runner.cleanup()


def main() -> None:
    asyncio.run(run_all())


if __name__ == "__main__":
    main()
