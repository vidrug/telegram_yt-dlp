"""Telegram message and callback handlers."""

import asyncio
import secrets
import time
import yt_dlp
from aiogram import F
from aiogram.filters import CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot.callbacks import (
    CancelCallback,
    FormatCallback,
    PageCallback,
    RawFormatsCallback,
    RetryCallback,
    SponsorBlockCallback,
)
from bot.cleanup import cleanup_session_files
from bot.config import EXTERNAL_URL, MAX_CONCURRENT_PER_USER, MAX_FILE_SIZE, log
from bot.downloader import download_media, safe_edit, send_local_file
from bot.formats import (
    build_format_keyboard,
    build_raw_format_table,
    extract_formats,
    filter_and_group,
    format_button_label,
    format_filesize,
)
from bot.state import bot, executor, router, sessions, user_downloads, web_files


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
        # Custom format string (e.g. bestvideo+bestaudio)
        label = fmt_id
    else:
        label = format_button_label(selected_format)

    await callback.answer()
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


# ---------------------------------------------------------------------------
# Download execution (shared logic for sponsorblock and retry)
# ---------------------------------------------------------------------------

def _build_retry_kb(sid: str, fmt_id: str, sponsorblock: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
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


async def _execute_download(
    callback: CallbackQuery,
    sid: str,
    fmt_id: str,
    sponsorblock: bool,
    s: dict,
    progress_msg: Message,
    is_retry: bool = False,
) -> None:
    """Common download logic for both initial download and retry."""
    user_id = callback.from_user.id

    # Determine format category
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

    current = user_downloads.get(user_id, 0)
    user_downloads[user_id] = current + 1
    loop = asyncio.get_running_loop()
    success = False

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
            ext = file_path.suffix  # e.g. ".mp4"
            link = f"{EXTERNAL_URL}/dl/{sid}{ext}"
            await progress_msg.edit_text(
                f"📦 Файл слишком большой для Telegram "
                f"({format_filesize(file_size)}).\n\n"
                f"⬇️ <a href=\"{link}\">Скачать файл</a>\n\n"
                f"Ссылка действует 8 часов.",
            )
            sessions.pop(sid, None)
            success = True
            return

        await progress_msg.edit_text("📤 Отправляю файл...")

        await send_local_file(
            callback.message.chat.id, file_path, title, cat_label,
        )

        await progress_msg.delete()
        success = True

    except yt_dlp.utils.DownloadError as e:
        log.warning("Download error for session %s: %s", sid, e)
        retry_kb = _build_retry_kb(sid, fmt_id, sponsorblock)
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
        retry_kb = _build_retry_kb(sid, fmt_id, sponsorblock)
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
        if success:
            if sid not in web_files:
                cleanup_session_files(sid)
            sessions.pop(sid, None)


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

    await callback.answer()

    # Build label for progress message
    is_custom = "+" in fmt_id or not fmt_id.isdigit()
    selected_format = None
    if not is_custom:
        for fmts in s["groups"].values():
            for f in fmts:
                if f["format_id"] == fmt_id:
                    selected_format = f
                    break
            if selected_format:
                break

    label = format_button_label(selected_format) if selected_format else fmt_id
    sb_text = " | SponsorBlock ✅" if sponsorblock else ""

    progress_msg = await callback.message.edit_text(
        f"⬇️ Скачиваю: {label}{sb_text}\n\nПодготовка..."
    )

    await _execute_download(callback, sid, fmt_id, sponsorblock, s, progress_msg)


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

    # Build label
    is_custom = "+" in fmt_id or not fmt_id.isdigit()
    selected_format = None
    if not is_custom:
        for fmts in s["groups"].values():
            for f in fmts:
                if f["format_id"] == fmt_id:
                    selected_format = f
                    break
            if selected_format:
                break

    label = format_button_label(selected_format) if selected_format else fmt_id
    sb_text = " | SponsorBlock" if sponsorblock else ""

    progress_msg = await callback.message.edit_text(
        f"🔄 Возобновляю скачивание: {label}{sb_text}\n\nПодготовка..."
    )

    await _execute_download(callback, sid, fmt_id, sponsorblock, s, progress_msg, is_retry=True)
