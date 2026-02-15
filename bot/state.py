"""Shared mutable state: bot instance, dispatcher, router, executor, in-memory dicts."""

from concurrent.futures import ThreadPoolExecutor

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode

from bot.config import API_URL, BOT_TOKEN

executor = ThreadPoolExecutor(max_workers=4)

session = AiohttpSession(api=TelegramAPIServer.from_base(API_URL, is_local=True))
bot = Bot(
    token=BOT_TOKEN,
    session=session,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()
router = Router()
dp.include_router(router)

# In-memory state
sessions: dict[str, dict] = {}  # session_id -> {url, formats, title, created, user_id}
user_downloads: dict[int, int] = {}  # user_id -> active download count
web_files: dict[str, dict] = {}  # session_id -> {path, created, filename}
