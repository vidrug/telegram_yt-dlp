"""Telegram yt-dlp Bot — скачивание видео через inline-кнопки."""

import asyncio
import logging
import os
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import json
from urllib.parse import quote

import yt_dlp
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    CallbackQuery,
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
EXTERNAL_URL = os.environ.get("EXTERNAL_URL", "http://localhost:8080").rstrip("/")
SHARED_DIR = Path(os.environ.get("SHARED_DIR", "/shared"))
DOWNLOAD_DIR = SHARED_DIR / "downloads"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024  # 2 GB
WEB_FILE_TTL = 8 * 3600  # 8 hours
WEB_PORT = 8080
FORMATS_PER_PAGE = 8
SESSION_TTL = 30 * 60  # 30 min
MAX_CONCURRENT_PER_USER = 2
PROGRESS_INTERVAL = 3  # seconds

executor = ThreadPoolExecutor(max_workers=4)

# ---------------------------------------------------------------------------
# Bot init (local API)
# ---------------------------------------------------------------------------
session = AiohttpSession(api=TelegramAPIServer.from_base(API_URL, is_local=True))
bot = Bot(
    token=BOT_TOKEN,
    session=session,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()
router = Router()
dp.include_router(router)

# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------
sessions: dict[str, dict] = {}  # session_id -> {url, formats, title, created, user_id}
user_downloads: dict[int, int] = {}  # user_id -> active download count
web_files: dict[str, dict] = {}  # session_id -> {path, created, filename}

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


class SponsorBlockCallback(CallbackData, prefix="sb"):
    session: str
    fmt: str
    remove: int  # 1 = yes, 0 = no


class RawFormatsCallback(CallbackData, prefix="rf"):
    session: str


class RetryCallback(CallbackData, prefix="rt"):
    session: str
    fmt: str
    remove: int  # sponsorblock: 1 = yes, 0 = no


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


def build_raw_format_table(formats: list[dict]) -> str:
    """Build a text table of all formats similar to yt-dlp -F output."""
    lines = []
    lines.append(f"{'ID':<6}{'EXT':<6}{'RES':<12}{'FPS':<5}{'VCODEC':<10}{'ACODEC':<8}{'SIZE':<9}{'NOTE'}")
    lines.append("─" * 70)
    for f in formats:
        fid = f.get("format_id", "?")
        ext = f.get("ext", "?")
        res = f.get("resolution") or f.get("format_note") or "?"
        fps = f.get("fps") or ""
        vcodec = f.get("vcodec") or "none"
        if vcodec == "none":
            vcodec = "-"
        else:
            vcodec = vcodec.split(".")[0][:8]
        acodec = f.get("acodec") or "none"
        if acodec == "none":
            acodec = "-"
        else:
            acodec = acodec.split(".")[0][:6]
        size = format_filesize(f.get("filesize") or f.get("filesize_approx"))
        note = f.get("format_note") or ""
        fps_s = str(int(fps)) if fps else ""
        lines.append(
            f"{fid:<6}{ext:<6}{res:<12}{fps_s:<5}{vcodec:<10}{acodec:<8}{size:<9}{note}"
        )
    return "\n".join(lines)


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

    rows.append([
        InlineKeyboardButton(
            text="📋 Все форматы",
            callback_data=RawFormatsCallback(session=session_id).pack(),
        ),
        InlineKeyboardButton(
            text="❌ Отмена",
            callback_data=CancelCallback(session=session_id).pack(),
        ),
    ])

    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def download_media(
    url: str,
    format_id: str,
    session_id: str,
    is_video_only: bool,
    sponsorblock: bool,
    loop: asyncio.AbstractEventLoop,
    progress_msg: Message,
) -> Path:
    """Download media file (blocking). Returns path to downloaded file."""
    out_dir = DOWNLOAD_DIR / session_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_template = str(out_dir / "%(id)s.%(ext)s")

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
        "continuedl": True,  # resume partial .part files
        "retries": 3,  # retry on transient errors
        "fragment_retries": 5,  # retry individual fragments (DASH/HLS)
        "concurrent_fragment_downloads": 4,  # download 4 fragments in parallel
    }
    if sponsorblock:
        ydl_opts["sponsorblock_remove"] = {"all"}
        ydl_opts["postprocessors"] = [{
            "key": "SponsorBlock",
            "categories": ["all"],
            "when": "after_filter",
        }, {
            "key": "ModifyChapters",
            "remove_sponsor_segments": ["all"],
        }]
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


async def send_local_file(
    chat_id: int, file_path: Path, title: str, file_type: str,
) -> None:
    """Send file to Telegram via curl (streams from disk, no RAM buffering)."""
    method_map = {
        "audio_only": "sendAudio",
        "video_audio": "sendVideo",
        "video_only": "sendVideo",
    }
    method = method_map.get(file_type, "sendDocument")
    url = f"{API_URL}/bot{BOT_TOKEN}/{method}"

    field_map = {
        "sendVideo": "video",
        "sendAudio": "audio",
        "sendDocument": "document",
    }
    field = field_map[method]

    file_size = file_path.stat().st_size
    log.info("Sending file: %s (%s bytes) via %s", file_path, file_size, method)

    cmd = [
        "curl", "-s", "-X", "POST", url,
        "-F", f"chat_id={chat_id}",
        "-F", f"{field}=@{file_path}",
    ]
    if method == "sendVideo":
        cmd.extend(["-F", f"caption={title}", "-F", "supports_streaming=true"])
    elif method == "sendAudio":
        cmd.extend(["-F", f"title={title}"])
    else:
        cmd.extend(["-F", f"caption={title}"])

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        raise RuntimeError(f"curl failed (code {proc.returncode}): {stderr.decode()}")

    result = json.loads(stdout)
    log.info("Bot API response: ok=%s", result.get("ok"))
    if not result.get("ok"):
        raise RuntimeError(
            f"Telegram API error: {result.get('description', result)}"
        )


# ---------------------------------------------------------------------------
# Web server for files > 2 GB
# ---------------------------------------------------------------------------

CHUNK_SIZE = 64 * 1024 * 1024  # 64 MB


async def handle_download(request: web.Request) -> web.StreamResponse:
    """Serve a file for download with Range support."""
    sid = request.match_info["session_id"]
    entry = web_files.get(sid)
    if not entry:
        raise web.HTTPNotFound(text="File not found or link expired.")

    file_path = entry["path"]
    if not file_path.exists():
        web_files.pop(sid, None)
        raise web.HTTPNotFound(text="File not found or link expired.")

    filename = entry["filename"]
    file_size = file_path.stat().st_size
    encoded = quote(filename)
    disposition = f"attachment; filename*=UTF-8''{encoded}"

    # Parse Range header
    range_header = request.headers.get("Range")
    start = 0
    end = file_size - 1

    if range_header and range_header.startswith("bytes="):
        range_spec = range_header[6:]
        parts = range_spec.split("-", 1)
        if parts[0]:
            start = int(parts[0])
        if parts[1]:
            end = int(parts[1])
        end = min(end, file_size - 1)

        if start >= file_size or start > end:
            return web.Response(
                status=416,
                headers={"Content-Range": f"bytes */{file_size}"},
            )

        response = web.StreamResponse(status=206)
        response.headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
    else:
        response = web.StreamResponse(status=200)

    response.content_type = "application/octet-stream"
    response.headers["Content-Disposition"] = disposition
    response.headers["Accept-Ranges"] = "bytes"
    response.content_length = end - start + 1
    await response.prepare(request)

    with open(file_path, "rb") as f:
        f.seek(start)
        remaining = end - start + 1
        while remaining > 0:
            chunk = f.read(min(CHUNK_SIZE, remaining))
            if not chunk:
                break
            await response.write(chunk)
            remaining -= len(chunk)

    return response


def create_web_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/dl/{session_id}", handle_download)
    return app


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

import shutil


def cleanup_session_files(session_id: str) -> None:
    d = DOWNLOAD_DIR / session_id
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)


async def periodic_cleanup() -> None:
    """Remove orphaned download dirs and expired web files."""
    while True:
        await asyncio.sleep(600)
        try:
            now = time.time()
            # Clean expired web files (8 hours)
            expired_web = [
                sid for sid, e in web_files.items()
                if now - e["created"] > WEB_FILE_TTL
            ]
            for sid in expired_web:
                web_files.pop(sid, None)
                cleanup_session_files(sid)
                log.info("Cleaned up expired web file: %s", sid)

            # Clean orphaned dirs (8 hours, skip active web files)
            for d in DOWNLOAD_DIR.iterdir():
                if d.is_dir() and d.name not in web_files:
                    if now - d.stat().st_mtime > WEB_FILE_TTL:
                        shutil.rmtree(d, ignore_errors=True)
                        sessions.pop(d.name, None)
                        log.info("Cleaned up orphaned dir: %s", d.name)
        except Exception as e:
            log.error("periodic_cleanup error: %s", e)


async def session_cleanup() -> None:
    """Remove expired sessions every 5 minutes."""
    while True:
        await asyncio.sleep(300)
        now = time.time()
        expired = []
        for sid, s in sessions.items():
            # Sessions with .part files (awaiting retry) live up to 8 hours
            has_parts = any(
                f.name.endswith(".part")
                for f in (DOWNLOAD_DIR / sid).iterdir()
            ) if (DOWNLOAD_DIR / sid).exists() else False
            ttl = WEB_FILE_TTL if has_parts else SESSION_TTL
            if now - s["created"] > ttl:
                expired.append(sid)
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
        "raw_formats": info.get("formats") or [],
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


@router.callback_query(RawFormatsCallback.filter())
async def handle_raw_formats(callback: CallbackQuery, callback_data: RawFormatsCallback) -> None:
    sid = callback_data.session
    s = sessions.get(sid)
    if not s:
        await callback.answer("⏰ Сессия истекла. Отправь ссылку заново.", show_alert=True)
        return

    user_id = callback.from_user.id
    if s["user_id"] != user_id:
        await callback.answer("🚫 Это не твой запрос.", show_alert=True)
        return

    await callback.answer()

    raw = s.get("raw_formats", [])
    table = build_raw_format_table(raw)

    # Split into chunks of max 4000 chars (Telegram limit ~4096)
    chunks = []
    current = ""
    for line in table.split("\n"):
        if len(current) + len(line) + 1 > 3900:
            chunks.append(current)
            current = line
        else:
            current = current + "\n" + line if current else line
    if current:
        chunks.append(current)

    for chunk in chunks:
        await bot.send_message(
            chat_id=callback.message.chat.id,
            text=f"<pre>{chunk}</pre>",
        )

    s["awaiting_format"] = True
    await bot.send_message(
        chat_id=callback.message.chat.id,
        text=(
            "Введи формат для скачивания, например:\n"
            "<code>251</code> — один формат\n"
            "<code>315+251</code> — видео + аудио\n"
            "<code>bestvideo+bestaudio</code> — лучшее качество\n\n"
            "Или отправь новую ссылку для отмены."
        ),
    )


@router.message(F.text.regexp(r"^[\w+]+$"))
async def handle_custom_format(message: Message) -> None:
    """Handle manual format input like '315+251'."""
    user_id = message.from_user.id
    # Find session awaiting format input for this user
    sid = None
    s = None
    for _sid, _s in sessions.items():
        if _s.get("user_id") == user_id and _s.get("awaiting_format"):
            sid = _sid
            s = _s
            break

    if not s:
        return  # Not awaiting format input, skip

    s["awaiting_format"] = False
    fmt_input = message.text.strip()

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="✅ Да, убрать рекламу",
                callback_data=SponsorBlockCallback(session=sid, fmt=fmt_input, remove=1).pack(),
            ),
        ],
        [
            InlineKeyboardButton(
                text="⏩ Нет, скачать как есть",
                callback_data=SponsorBlockCallback(session=sid, fmt=fmt_input, remove=0).pack(),
            ),
        ],
    ])

    await message.answer(
        f"Формат: <code>{fmt_input}</code>\n\n"
        "🔇 Убрать рекламные вставки (SponsorBlock)?",
        reply_markup=kb,
    )


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

    # Find selected format for label
    selected_format = None
    for fmts in s["groups"].values():
        for f in fmts:
            if f["format_id"] == fmt_id:
                selected_format = f
                break
        if selected_format:
            break

    if not selected_format:
        await callback.answer("❌ Формат не найден.", show_alert=True)
        return

    await callback.answer()

    label = format_button_label(selected_format)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="✅ Да, убрать рекламу",
                callback_data=SponsorBlockCallback(session=sid, fmt=fmt_id, remove=1).pack(),
            ),
        ],
        [
            InlineKeyboardButton(
                text="⏩ Нет, скачать как есть",
                callback_data=SponsorBlockCallback(session=sid, fmt=fmt_id, remove=0).pack(),
            ),
        ],
    ])

    await callback.message.edit_text(
        f"Выбран формат: <b>{label}</b>\n\n"
        "🔇 Убрать рекламные вставки (SponsorBlock)?",
        reply_markup=kb,
    )


@router.callback_query(SponsorBlockCallback.filter())
async def handle_sponsorblock(callback: CallbackQuery, callback_data: SponsorBlockCallback) -> None:
    sid = callback_data.session
    fmt_id = callback_data.fmt
    sponsorblock = callback_data.remove == 1
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
    is_custom = "+" in fmt_id or not fmt_id.isdigit()
    selected_format = None

    if not is_custom:
        for cat, fmts in s["groups"].items():
            for f in fmts:
                if f["format_id"] == fmt_id:
                    selected_format = f
                    is_video_only = cat == "video_only"
                    break
            if selected_format:
                break

    await callback.answer()

    if selected_format:
        label = format_button_label(selected_format)
        cat_label = classify_format(selected_format)
        if is_video_only:
            label += " (+ best audio)"
    else:
        # Custom format from raw input
        label = fmt_id
        cat_label = "video_audio"  # assume merged for custom formats

    sb_text = " | SponsorBlock ✅" if sponsorblock else ""

    progress_msg = await callback.message.edit_text(
        f"⬇️ Скачиваю: {label}{sb_text}\n\nПодготовка..."
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
            sponsorblock,
            loop,
            progress_msg,
        )

        if not file_path.exists():
            await progress_msg.edit_text("❌ Ошибка: файл не найден после скачивания.")
            return

        file_size = file_path.stat().st_size
        title = s.get("title", "video")

        if file_size > MAX_FILE_SIZE:
            # File too large for Telegram — serve via web
            filename = f"{title}.{file_path.suffix.lstrip('.')}"
            web_files[sid] = {
                "path": file_path,
                "created": time.time(),
                "filename": filename,
            }
            link = f"{EXTERNAL_URL}/dl/{sid}"
            await progress_msg.edit_text(
                f"📦 Файл слишком большой для Telegram "
                f"({format_filesize(file_size)}).\n\n"
                f"⬇️ <a href=\"{link}\">Скачать файл</a>\n\n"
                f"Ссылка действует 8 часов.",
            )
            # Don't cleanup — file served via web, cleaned by periodic_cleanup
            sessions.pop(sid, None)
            return

        await progress_msg.edit_text("📤 Отправляю файл...")

        await send_local_file(
            callback.message.chat.id, file_path, title, cat_label,
        )

        await progress_msg.delete()

    except yt_dlp.utils.DownloadError as e:
        log.warning("Download error for session %s: %s", sid, e)
        retry_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="🔄 Повторить скачивание",
                callback_data=RetryCallback(
                    session=sid, fmt=fmt_id, remove=1 if sponsorblock else 0,
                ).pack(),
            )],
            [InlineKeyboardButton(
                text="❌ Отмена",
                callback_data=CancelCallback(session=sid).pack(),
            )],
        ])
        err_short = str(e)[:200]
        try:
            await progress_msg.edit_text(
                f"❌ Ошибка скачивания:\n<code>{err_short}</code>\n\n"
                "Частичный файл сохранён. Нажми «Повторить» — скачивание продолжится с места остановки.",
                reply_markup=retry_kb,
            )
        except Exception:
            pass
        # DON'T cleanup files — keep .part for resume
        # DON'T remove session — needed for retry
        return
    except Exception as e:
        log.exception("Download/send error for session %s", sid)
        retry_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="🔄 Повторить скачивание",
                callback_data=RetryCallback(
                    session=sid, fmt=fmt_id, remove=1 if sponsorblock else 0,
                ).pack(),
            )],
            [InlineKeyboardButton(
                text="❌ Отмена",
                callback_data=CancelCallback(session=sid).pack(),
            )],
        ])
        err_short = str(e)[:200]
        try:
            await progress_msg.edit_text(
                f"❌ Произошла ошибка:\n<code>{err_short}</code>\n\n"
                "Нажми «Повторить» для возобновления скачивания.",
                reply_markup=retry_kb,
            )
        except Exception:
            pass
        return
    finally:
        user_downloads[user_id] = max(0, user_downloads.get(user_id, 1) - 1)
        # Cleanup only on success or if session was consumed (web/send)
        # On error we return early above, so this runs only on success path
        if sid not in web_files:
            cleanup_session_files(sid)
        sessions.pop(sid, None)


@router.callback_query(RetryCallback.filter())
async def handle_retry(callback: CallbackQuery, callback_data: RetryCallback) -> None:
    sid = callback_data.session
    fmt_id = callback_data.fmt
    sponsorblock = callback_data.remove == 1
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

    await callback.answer()

    # Determine format category (same logic as handle_sponsorblock)
    is_video_only = False
    is_custom = "+" in fmt_id or not fmt_id.isdigit()
    selected_format = None
    cat_label = "video_audio"

    if not is_custom:
        for cat, fmts in s["groups"].items():
            for f in fmts:
                if f["format_id"] == fmt_id:
                    selected_format = f
                    is_video_only = cat == "video_only"
                    cat_label = cat
                    break
            if selected_format:
                break

    label = format_button_label(selected_format) if selected_format else fmt_id
    sb_text = " | SponsorBlock" if sponsorblock else ""

    progress_msg = await callback.message.edit_text(
        f"🔄 Возобновляю скачивание: {label}{sb_text}\n\nПодготовка..."
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
            sponsorblock,
            loop,
            progress_msg,
        )

        if not file_path.exists():
            await progress_msg.edit_text("❌ Ошибка: файл не найден после скачивания.")
            return

        file_size = file_path.stat().st_size
        title = s.get("title", "video")

        if file_size > MAX_FILE_SIZE:
            filename = f"{title}.{file_path.suffix.lstrip('.')}"
            web_files[sid] = {
                "path": file_path,
                "created": time.time(),
                "filename": filename,
            }
            link = f"{EXTERNAL_URL}/dl/{sid}"
            await progress_msg.edit_text(
                f"📦 Файл слишком большой для Telegram "
                f"({format_filesize(file_size)}).\n\n"
                f"⬇️ <a href=\"{link}\">Скачать файл</a>\n\n"
                f"Ссылка действует 8 часов.",
            )
            sessions.pop(sid, None)
            return

        await progress_msg.edit_text("📤 Отправляю файл...")

        await send_local_file(
            callback.message.chat.id, file_path, title, cat_label,
        )

        await progress_msg.delete()

    except yt_dlp.utils.DownloadError as e:
        log.warning("Retry download error for session %s: %s", sid, e)
        retry_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="🔄 Повторить скачивание",
                callback_data=RetryCallback(
                    session=sid, fmt=fmt_id, remove=1 if sponsorblock else 0,
                ).pack(),
            )],
            [InlineKeyboardButton(
                text="❌ Отмена",
                callback_data=CancelCallback(session=sid).pack(),
            )],
        ])
        err_short = str(e)[:200]
        try:
            await progress_msg.edit_text(
                f"❌ Ошибка скачивания:\n<code>{err_short}</code>\n\n"
                "Нажми «Повторить» — скачивание продолжится с места остановки.",
                reply_markup=retry_kb,
            )
        except Exception:
            pass
        return
    except Exception as e:
        log.exception("Retry error for session %s", sid)
        retry_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="🔄 Повторить скачивание",
                callback_data=RetryCallback(
                    session=sid, fmt=fmt_id, remove=1 if sponsorblock else 0,
                ).pack(),
            )],
            [InlineKeyboardButton(
                text="❌ Отмена",
                callback_data=CancelCallback(session=sid).pack(),
            )],
        ])
        err_short = str(e)[:200]
        try:
            await progress_msg.edit_text(
                f"❌ Произошла ошибка:\n<code>{err_short}</code>\n\n"
                "Нажми «Повторить» для возобновления.",
                reply_markup=retry_kb,
            )
        except Exception:
            pass
        return
    finally:
        user_downloads[user_id] = max(0, user_downloads.get(user_id, 1) - 1)
        if sid not in web_files:
            cleanup_session_files(sid)
        sessions.pop(sid, None)


# ---------------------------------------------------------------------------
# Startup / Shutdown
# ---------------------------------------------------------------------------

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
