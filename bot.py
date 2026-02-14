"""Telegram yt-dlp Bot — скачивание видео через inline-кнопки."""

import asyncio
import logging
import os
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yt_dlp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BOT_TOKEN = os.environ["BOT_TOKEN"]
API_URL = os.environ.get("TELEGRAM_API_URL", "http://telegram-bot-api:8081")
SHARED_DIR = Path(os.environ.get("SHARED_DIR", "/shared"))
DOWNLOAD_DIR = SHARED_DIR / "downloads"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024  # 2 GB
FORMATS_PER_PAGE = 8
SESSION_TTL = 30 * 60  # 30 min
MAX_CONCURRENT_PER_USER = 2
PROGRESS_INTERVAL = 3  # seconds

executor = ThreadPoolExecutor(max_workers=4)

# ---------------------------------------------------------------------------
# Bot init (local API)
# ---------------------------------------------------------------------------
session = AiohttpSession(api=TelegramAPIServer.from_base(API_URL, is_local=True))
bot = Bot(token=BOT_TOKEN, session=session, parse_mode=ParseMode.HTML)
dp = Dispatcher()
router = Router()
dp.include_router(router)

# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------
sessions: dict[str, dict] = {}  # session_id -> {url, formats, title, created, user_id}
user_downloads: dict[int, int] = {}  # user_id -> active download count

# ---------------------------------------------------------------------------
# CallbackData
# ---------------------------------------------------------------------------

class FormatCallback(CallbackData, prefix="f"):
    session: str  # 8 chars
    fmt: str      # format_id


class PageCallback(CallbackData, prefix="p"):
    session: str
    page: int


class CancelCallback(CallbackData, prefix="c"):
    session: str


# ---------------------------------------------------------------------------
# Format helpers
# ---------------------------------------------------------------------------

def classify_format(f: dict) -> str | None:
    vcodec = f.get("vcodec", "none")
    acodec = f.get("acodec", "none")
    has_video = vcodec != "none" and vcodec is not None
    has_audio = acodec != "none" and acodec is not None
    if has_video and has_audio:
        return "video_audio"
    if has_video:
        return "video_only"
    if has_audio:
        return "audio_only"
    return None


def format_filesize(size: float | int | None) -> str:
    if not size:
        return "?"
    if size < 1024:
        return f"{size}B"
    if size < 1024 ** 2:
        return f"{size / 1024:.0f}KB"
    if size < 1024 ** 3:
        return f"{size / 1024 ** 2:.1f}MB"
    return f"{size / 1024 ** 3:.2f}GB"


def format_button_label(f: dict) -> str:
    parts = []
    ext = f.get("ext", "?")
    res = f.get("resolution") or f.get("format_note") or ""
    fps = f.get("fps")
    size = f.get("filesize") or f.get("filesize_approx")
    parts.append(ext.upper())
    if res:
        parts.append(res)
    if fps and fps > 30:
        parts.append(f"{fps}fps")
    parts.append(format_filesize(size))
    return " | ".join(parts)


def extract_formats(url: str) -> dict:
    """Run yt-dlp extract_info (blocking)."""
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
    return info


def filter_and_group(info: dict) -> dict[str, list[dict]]:
    formats = info.get("formats") or []
    groups: dict[str, list[dict]] = {
        "video_audio": [],
        "video_only": [],
        "audio_only": [],
    }
    seen = set()
    for f in formats:
        fid = f.get("format_id")
        if not fid or fid in seen:
            continue
        cat = classify_format(f)
        if cat is None:
            continue
        seen.add(fid)
        groups[cat].append(f)
    # Sort: best quality first (higher resolution / tbr)
    for cat in groups:
        groups[cat].sort(key=lambda x: x.get("tbr") or x.get("abr") or 0, reverse=True)
    return groups


def build_format_keyboard(
    session_id: str, groups: dict[str, list[dict]], page: int = 0
) -> InlineKeyboardMarkup:
    all_formats = []
    section_labels = {
        "video_audio": "🎬 Video + Audio",
        "video_only": "📹 Video Only",
        "audio_only": "🎵 Audio Only",
    }
    for cat in ("video_audio", "video_only", "audio_only"):
        fmts = groups.get(cat, [])
        if fmts:
            all_formats.append(("section", section_labels[cat]))
            for f in fmts:
                all_formats.append(("format", f))

    total = len(all_formats)
    start = page * FORMATS_PER_PAGE
    end = start + FORMATS_PER_PAGE
    page_items = all_formats[start:end]

    rows: list[list[InlineKeyboardButton]] = []
    for kind, item in page_items:
        if kind == "section":
            rows.append([InlineKeyboardButton(text=f"— {item} —", callback_data="noop")])
        else:
            label = format_button_label(item)
            cb = FormatCallback(session=session_id, fmt=item["format_id"])
            rows.append([InlineKeyboardButton(text=label, callback_data=cb.pack())])

    # Pagination
    nav = []
    total_pages = (total + FORMATS_PER_PAGE - 1) // FORMATS_PER_PAGE
    if page > 0:
        nav.append(InlineKeyboardButton(
            text="⬅️",
            callback_data=PageCallback(session=session_id, page=page - 1).pack(),
        ))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(
            text="➡️",
            callback_data=PageCallback(session=session_id, page=page + 1).pack(),
        ))
    if nav:
        rows.append(nav)

    rows.append([InlineKeyboardButton(
        text="❌ Отмена",
        callback_data=CancelCallback(session=session_id).pack(),
    )])

    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def download_media(
    url: str,
    format_id: str,
    session_id: str,
    is_video_only: bool,
    loop: asyncio.AbstractEventLoop,
    progress_msg: Message,
) -> Path:
    """Download media file (blocking). Returns path to downloaded file."""
    out_dir = DOWNLOAD_DIR / session_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_template = str(out_dir / "%(title).80s.%(ext)s")

    fmt = f"{format_id}+bestaudio" if is_video_only else format_id

    last_update = [0.0]

    def progress_hook(d: dict) -> None:
        if d.get("status") != "downloading":
            return
        now = time.monotonic()
        if now - last_update[0] < PROGRESS_INTERVAL:
            return
        last_update[0] = now

        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        downloaded = d.get("downloaded_bytes", 0)
        speed = d.get("speed")
        eta = d.get("eta")

        parts = ["⬇️ Скачивание..."]
        if total:
            pct = downloaded / total * 100
            parts.append(f"{pct:.1f}%")
            parts.append(f"({format_filesize(downloaded)} / {format_filesize(total)})")
        if speed:
            parts.append(f"| {format_filesize(speed)}/s")
        if eta:
            parts.append(f"| ETA {eta}s")

        text = " ".join(parts)
        asyncio.run_coroutine_threadsafe(
            safe_edit(progress_msg, text), loop
        )

    ydl_opts = {
        "format": fmt,
        "outtmpl": out_template,
        "merge_output_format": "mp4" if is_video_only else None,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [progress_hook],
    }
    # Remove None values
    ydl_opts = {k: v for k, v in ydl_opts.items() if v is not None}

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)

    # Find the downloaded file
    filename = ydl.prepare_filename(info)
    # yt-dlp might change extension after merge
    result = Path(filename)
    if not result.exists():
        # Try common post-merge extensions
        for ext in ("mp4", "mkv", "webm", "m4a", "mp3", "opus"):
            candidate = result.with_suffix(f".{ext}")
            if candidate.exists():
                return candidate
        # Fallback: pick any file in the directory
        files = list(out_dir.iterdir())
        if files:
            return files[0]
    return result


async def safe_edit(msg: Message, text: str) -> None:
    try:
        await msg.edit_text(text)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

import shutil


def cleanup_session_files(session_id: str) -> None:
    d = DOWNLOAD_DIR / session_id
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)


async def periodic_cleanup() -> None:
    """Remove orphaned download dirs older than 1 hour."""
    while True:
        await asyncio.sleep(3600)
        try:
            now = time.time()
            for d in DOWNLOAD_DIR.iterdir():
                if d.is_dir() and (now - d.stat().st_mtime > 3600):
                    shutil.rmtree(d, ignore_errors=True)
                    log.info("Cleaned up orphaned dir: %s", d.name)
        except Exception as e:
            log.error("periodic_cleanup error: %s", e)


async def session_cleanup() -> None:
    """Remove expired sessions every 5 minutes."""
    while True:
        await asyncio.sleep(300)
        now = time.time()
        expired = [sid for sid, s in sessions.items() if now - s["created"] > SESSION_TTL]
        for sid in expired:
            sessions.pop(sid, None)
            cleanup_session_files(sid)
        if expired:
            log.info("Cleaned up %d expired sessions", len(expired))


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        "👋 Привет! Отправь мне ссылку на видео, и я покажу доступные форматы для скачивания."
    )


URL_PATTERN = r"https?://\S+"


@router.message(F.text.regexp(URL_PATTERN))
async def handle_url(message: Message) -> None:
    url = message.text.strip()
    status_msg = await message.answer("⏳ Получаю список форматов...")

    loop = asyncio.get_running_loop()
    try:
        info = await loop.run_in_executor(executor, extract_formats, url)
    except Exception as e:
        await status_msg.edit_text(f"❌ Ошибка при получении форматов:\n<code>{e}</code>")
        return

    groups = filter_and_group(info)
    total_formats = sum(len(v) for v in groups.values())
    if total_formats == 0:
        await status_msg.edit_text("❌ Не найдено доступных форматов для этого URL.")
        return

    session_id = secrets.token_hex(4)  # 8 hex chars
    sessions[session_id] = {
        "url": url,
        "groups": groups,
        "title": info.get("title", "video"),
        "created": time.time(),
        "user_id": message.from_user.id,
    }

    title = info.get("title", "")
    duration = info.get("duration")
    header = f"🎬 <b>{title}</b>"
    if duration:
        mins, secs = divmod(int(duration), 60)
        header += f"\n⏱ {mins}:{secs:02d}"
    header += f"\n\n📋 Найдено форматов: {total_formats}\nВыбери формат для скачивания:"

    kb = build_format_keyboard(session_id, groups, page=0)
    await status_msg.edit_text(header, reply_markup=kb)


@router.callback_query(lambda c: c.data == "noop")
async def handle_noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(PageCallback.filter())
async def handle_page(callback: CallbackQuery, callback_data: PageCallback) -> None:
    sid = callback_data.session
    page = callback_data.page
    s = sessions.get(sid)
    if not s:
        await callback.answer("⏰ Сессия истекла. Отправь ссылку заново.", show_alert=True)
        return

    kb = build_format_keyboard(sid, s["groups"], page=page)
    await callback.message.edit_reply_markup(reply_markup=kb)
    await callback.answer()


@router.callback_query(CancelCallback.filter())
async def handle_cancel(callback: CallbackQuery, callback_data: CancelCallback) -> None:
    sid = callback_data.session
    sessions.pop(sid, None)
    cleanup_session_files(sid)
    await callback.message.edit_text("❌ Отменено.")
    await callback.answer()


@router.callback_query(FormatCallback.filter())
async def handle_format_select(callback: CallbackQuery, callback_data: FormatCallback) -> None:
    sid = callback_data.session
    fmt_id = callback_data.fmt
    s = sessions.get(sid)
    if not s:
        await callback.answer("⏰ Сессия истекла. Отправь ссылку заново.", show_alert=True)
        return

    user_id = callback.from_user.id
    if s["user_id"] != user_id:
        await callback.answer("🚫 Это не твой запрос.", show_alert=True)
        return

    current = user_downloads.get(user_id, 0)
    if current >= MAX_CONCURRENT_PER_USER:
        await callback.answer(
            f"⏳ Макс. {MAX_CONCURRENT_PER_USER} параллельных скачивания. Подожди.",
            show_alert=True,
        )
        return

    # Determine format category
    is_video_only = False
    selected_format = None
    for cat, fmts in s["groups"].items():
        for f in fmts:
            if f["format_id"] == fmt_id:
                selected_format = f
                is_video_only = cat == "video_only"
                break
        if selected_format:
            break

    if not selected_format:
        await callback.answer("❌ Формат не найден.", show_alert=True)
        return

    await callback.answer()

    label = format_button_label(selected_format)
    cat_label = classify_format(selected_format)
    if is_video_only:
        label += " (+ best audio)"

    progress_msg = await callback.message.edit_text(
        f"⬇️ Скачиваю: {label}\n\nПодготовка..."
    )

    user_downloads[user_id] = current + 1
    loop = asyncio.get_running_loop()

    try:
        file_path = await loop.run_in_executor(
            executor,
            download_media,
            s["url"],
            fmt_id,
            sid,
            is_video_only,
            loop,
            progress_msg,
        )

        if not file_path.exists():
            await progress_msg.edit_text("❌ Ошибка: файл не найден после скачивания.")
            return

        file_size = file_path.stat().st_size
        if file_size > MAX_FILE_SIZE:
            await progress_msg.edit_text(
                f"❌ Файл слишком большой: {format_filesize(file_size)} (макс. 2 ГБ)."
            )
            cleanup_session_files(sid)
            return

        await progress_msg.edit_text("📤 Отправляю файл...")

        # Send via local API (file path as string)
        input_file = FSInputFile(file_path)
        title = s.get("title", "video")

        if cat_label == "audio_only":
            await bot.send_audio(
                chat_id=callback.message.chat.id,
                audio=input_file,
                title=title,
            )
        elif cat_label in ("video_audio", "video_only"):
            await bot.send_video(
                chat_id=callback.message.chat.id,
                video=input_file,
                caption=title,
                supports_streaming=True,
            )
        else:
            await bot.send_document(
                chat_id=callback.message.chat.id,
                document=input_file,
                caption=title,
            )

        await progress_msg.delete()

    except yt_dlp.utils.DownloadError as e:
        await safe_edit(progress_msg, f"❌ Ошибка скачивания:\n<code>{e}</code>")
    except Exception as e:
        log.exception("Download/send error for session %s", sid)
        await safe_edit(progress_msg, f"❌ Произошла ошибка:\n<code>{e}</code>")
    finally:
        user_downloads[user_id] = max(0, user_downloads.get(user_id, 1) - 1)
        cleanup_session_files(sid)
        sessions.pop(sid, None)


# ---------------------------------------------------------------------------
# Startup / Shutdown
# ---------------------------------------------------------------------------

async def on_startup() -> None:
    log.info("Bot starting, using API at %s", API_URL)
    await bot.delete_webhook(drop_pending_updates=True)
    asyncio.create_task(periodic_cleanup())
    asyncio.create_task(session_cleanup())


async def on_shutdown() -> None:
    log.info("Bot shutting down, cleaning downloads...")
    for sid in list(sessions):
        cleanup_session_files(sid)
    sessions.clear()


def main() -> None:
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    dp.run_polling(bot)


if __name__ == "__main__":
    main()
