"""Configuration constants and environment variables."""

import logging
import os
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
API_URL = os.environ.get("TELEGRAM_API_URL", "http://telegram-bot-api:8081")
EXTERNAL_URL = os.environ.get("EXTERNAL_URL", "http://localhost:8080").rstrip("/")
SHARED_DIR = Path(os.environ.get("SHARED_DIR", "/shared"))
DOWNLOAD_DIR = SHARED_DIR / "downloads"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024  # 2 GB
WEB_FILE_TTL = 8 * 3600  # 8 hours
WEB_PORT = 8080
FORMATS_PER_PAGE = 8
SESSION_TTL = 2 * 3600  # 2 hours
MAX_CONCURRENT_PER_USER = 2
PROGRESS_INTERVAL = 3  # seconds
COOKIES_FILE = Path(os.environ.get("COOKIES_FILE", "/app/cookies.txt"))
COOKIES_FILE = COOKIES_FILE if COOKIES_FILE.exists() else None

# Панель прокси (на хосте) и владелец — для команд /status, /stats, /vpn, /config
PANEL_URL = os.environ.get("PANEL_URL", "http://host.docker.internal:1080").rstrip("/")
OWNER_ID = int(os.environ.get("OWNER_ID") or 0)
PANEL_TOKEN = os.environ.get("PANEL_TOKEN", "")
