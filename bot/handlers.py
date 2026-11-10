"""Telegram message and callback handlers."""

import asyncio
import io
import secrets
import time
from html import escape

from aiogram import F
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot import fulltable
from bot.callbacks import (
    CancelCallback,
    ClassicCallback,
    FormatCallback,
    FullPickCallback,
    PageCallback,
    RawFormatsCallback,
    RetryCallback,
    SponsorBlockCallback,
)
from bot.cleanup import cleanup_session_files
from bot.config import MAX_CONCURRENT_PER_USER, log
from bot.cookies import (
    MAX_COOKIE_FILE_SIZE,
    delete_user_cookies,
    needs_cookies,
    save_user_cookies,
    validate_cookie_file,
)
from bot.downloader import progress_text, safe_edit
from bot.formats import (
    build_format_keyboard,
    build_raw_format_table,
    extract_formats,
    filter_and_group,
    format_button_label,
    format_filesize,
)
from bot.jobs import SerialUI, cookie_prompt, find_format, run_download
from bot.state import awaiting_cookies, bot, executor, router, sessions, user_downloads
from bot.widget import send_picker, session_caption


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        "👋 Привет! Отправь мне ссылку на видео, и я покажу доступные форматы для скачивания."
    )


# ---------------------------------------------------------------------------
# Cookies: YouTube с датацентрового IP требует вход в аккаунт
# ---------------------------------------------------------------------------

async def _ask_for_cookies(msg: Message, url: str, user_id: int) -> None:
    """Запомнить ссылку и показать инструкцию, как прислать куки."""
    text = cookie_prompt(user_id, url)
    try:
        await msg.edit_text(text, disable_web_page_preview=True)
    except TelegramAPIError:
        await msg.answer(text, disable_web_page_preview=True)


async def _accept_cookies(message: Message, text: str) -> None:
    user_id = message.from_user.id
    # в сообщении секреты аккаунта — не оставляем их в чате
    try:
        await message.delete()
    except TelegramAPIError:
        pass

    error = validate_cookie_file(text)
    if error:
        await message.answer(f"❌ {escape(error)}\n\nПришли файл ещё раз.")
        return

    save_user_cookies(user_id, text)
    log.info("user %s: cookies saved", user_id)
    url = awaiting_cookies.pop(user_id, None)
    if url:
        await message.answer("✅ Куки сохранены, пробую ещё раз…")
        await _process_url(message, url, user_id)
    else:
        await message.answer("✅ Куки сохранены. Присылай ссылку.")


def _looks_like_cookies(text: str) -> bool:
    return "youtube.com" in text and text.count("\n") >= 2 and (
        "\t" in text or "TRUE" in text or "FALSE" in text
    )


@router.message(Command("forgetcookies"))
async def cmd_forget_cookies(message: Message) -> None:
    awaiting_cookies.pop(message.from_user.id, None)
    if delete_user_cookies(message.from_user.id):
        await message.answer("🗑 Твои куки удалены.")
    else:
        await message.answer("У меня нет сохранённых куки от тебя.")


@router.message(F.document)
async def handle_cookie_document(message: Message) -> None:
    doc = message.document
    name = (doc.file_name or "").lower()
    if message.from_user.id not in awaiting_cookies and "cookie" not in name:
        raise SkipHandler
    if doc.file_size and doc.file_size > MAX_COOKIE_FILE_SIZE:
        await message.answer("❌ Файл слишком большой для cookies.txt.")
        return

    buf = io.BytesIO()
    try:
        await bot.download(doc, destination=buf)
    except Exception:
        log.exception("cannot download cookie file from user %s", message.from_user.id)
        await message.answer("❌ Не удалось получить файл. Попробуй вставить его содержимое текстом.")
        return
    await _accept_cookies(message, buf.getvalue().decode("utf-8", errors="replace"))


@router.message(F.text.func(_looks_like_cookies))
async def handle_cookie_text(message: Message) -> None:
    await _accept_cookies(message, message.text)


# ---------------------------------------------------------------------------
# URL
# ---------------------------------------------------------------------------

URL_PATTERN = r"https?://\S+"


@router.message(F.text.regexp(URL_PATTERN))
async def handle_url(message: Message) -> None:
    await _process_url(message, message.text.strip(), message.from_user.id)


async def _process_url(message: Message, url: str, user_id: int) -> None:
    status_msg = await message.answer("⏳ Получаю список форматов...")

    loop = asyncio.get_running_loop()
    try:
        info = await loop.run_in_executor(executor, extract_formats, url, user_id)
    except Exception as e:
        if needs_cookies(e):
            await _ask_for_cookies(status_msg, url, user_id)
            return
        await status_msg.edit_text(f"❌ Ошибка при получении форматов:\n<code>{escape(str(e)[:500])}</code>")
        return

    groups = filter_and_group(info)
    total_formats = sum(len(v) for v in groups.values())
    if total_formats == 0:
        await status_msg.edit_text("❌ Не найдено доступных форматов для этого URL.")
        return

    session_id = secrets.token_hex(4)  # 8 hex chars
    s = sessions[session_id] = {
        "url": url,
        "groups": groups,
        "raw_formats": info.get("formats") or [],
        "title": info.get("title", "video"),
        "duration": info.get("duration"),
        "created": time.time(),
        "user_id": user_id,
    }

    # Виджеты в продакшене работают только в личке с ботом
    if message.chat.type == "private" and await send_picker(message.chat.id, session_id, s):
        try:
            await status_msg.delete()
        except TelegramAPIError:
            pass
        return

    kb = build_format_keyboard(session_id, groups, page=0)
    await status_msg.edit_text(_classic_header(s), reply_markup=kb)


def _classic_header(s: dict) -> str:
    total = sum(len(v) for v in s["groups"].values())
    return (
        f"{session_caption(s)}\n\n"
        f"📋 Найдено форматов: {total}\nВыбери формат для скачивания:"
    )


@router.callback_query(ClassicCallback.filter())
async def handle_classic(callback: CallbackQuery, callback_data: ClassicCallback) -> None:
    """Кнопка под виджетом: классическая клавиатура (для клиентов без виджетов)."""
    sid = callback_data.session
    s = sessions.get(sid)
    if not s:
        await callback.answer("⏰ Сессия истекла. Отправь ссылку заново.", show_alert=True)
        return
    if s["user_id"] != callback.from_user.id:
        await callback.answer("🚫 Это не твой запрос.", show_alert=True)
        return
    await callback.answer()
    await callback.message.answer(
        _classic_header(s), reply_markup=build_format_keyboard(sid, s["groups"], page=0),
    )


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
    s["awaiting_format"] = True  # ручной ввод формата текстом работает и при таблице

    try:
        # Интерактивная нативная таблица (Bot API 10.3+): ID формата — кнопка
        fulltable.prepare(s)
        table_msg = await bot.send_rich_message(
            chat_id=callback.message.chat.id,
            rich_message=fulltable.render(sid, s),
        )
        s["full_msg"] = table_msg.message_id
        return
    except TelegramAPIError as e:
        # Локальный bot-api сервер без Bot API 10.3 — откат на <pre>-таблицу
        log.warning("send_rich_message failed (%s), falling back to <pre> table", e)
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


@router.callback_query(FullPickCallback.filter())
async def handle_full_pick(callback: CallbackQuery, callback_data: FullPickCallback) -> None:
    """Тапы по полной таблице «Все форматы»: выбор видео/звука, SponsorBlock, скачать."""
    sid = callback_data.session
    s = sessions.get(sid)
    if not s or "full" not in s:
        await callback.answer("⏰ Сессия истекла. Отправь ссылку заново.", show_alert=True)
        return
    user_id = callback.from_user.id
    if s["user_id"] != user_id:
        await callback.answer("🚫 Это не твой запрос.", show_alert=True)
        return

    if callback_data.act == "go":
        chosen = fulltable.selection(s)
        if not chosen:
            await callback.answer("Выбери видео и/или звук", show_alert=True)
            return
        if user_downloads.get(user_id, 0) >= MAX_CONCURRENT_PER_USER:
            await callback.answer(
                f"⏳ Макс. {MAX_CONCURRENT_PER_USER} параллельных скачивания. Подожди.",
                show_alert=True,
            )
            return
        await callback.answer()
        selector, send_as, label = chosen
        sponsorblock = s["full_sel"]["sb"]
        text = f"⬇️ Скачиваю: {escape(label)}{' | SponsorBlock ✅' if sponsorblock else ''}\n\nПодготовка..."
        # с таблицы — новое сообщение (таблица остаётся для следующего выбора), с «Повторить» — то же
        if callback.message.message_id == s.get("full_msg"):
            progress_msg = await callback.message.answer(text)
        else:
            progress_msg = await callback.message.edit_text(text)
        retry_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Повторить скачивание",
                                  callback_data=FullPickCallback(session=sid, act="go").pack())],
        ])
        await run_download(
            TextUI(progress_msg, sid, selector, sponsorblock, retry_kb=retry_kb),
            chat_id=callback.message.chat.id, user_id=user_id, sid=sid, s=s,
            fmt_id=selector, sponsorblock=sponsorblock, keep_session=True,
            category="pair", send_as=send_as,
        )
        return

    hint = fulltable.toggle(s, callback_data.act, callback_data.idx)
    if hint:
        await callback.answer(hint, show_alert=True)
        return
    await callback.answer()
    try:
        await callback.message.edit_text(rich_message=fulltable.render(sid, s))
    except TelegramAPIError as e:
        if "message is not modified" not in str(e):
            log.warning("full table edit failed: %s", e)


# (?!/) — не перехватывать команды (/status и т.п.): слэш допустим только внутри
@router.message(F.text.regexp(r"^(?!/)[\w+\-/\[\]<>=.,*]+$"))
async def handle_custom_format(message: Message) -> None:
    """Handle manual format input like '315+251', '139-0', 'bv*[height<=720]+ba/b'."""
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
        # Не ждём ввода формата — отдаём сообщение следующим хендлерам
        raise SkipHandler

    s["awaiting_format"] = False
    fmt_input = message.text.strip()

    try:
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
    except ValueError:
        # callback_data ограничена 64 байтами
        s["awaiting_format"] = True
        await message.answer("❌ Строка формата слишком длинная, попробуй короче.")
        return

    await message.answer(
        f"Формат: <code>{escape(fmt_input)}</code>\n\n"
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
    selected_format, _ = find_format(s, fmt_id)
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
        f"Выбран формат: <b>{escape(label)}</b>\n\n"
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


class TextUI(SerialUI):
    """Ход скачивания правками обычного текстового сообщения (классический режим)."""

    def __init__(self, msg: Message, sid: str, fmt_id: str, sponsorblock: bool,
                 retry_kb: InlineKeyboardMarkup | None = None) -> None:
        super().__init__()
        self.msg = msg
        self.retry_kb = retry_kb or _build_retry_kb(sid, fmt_id, sponsorblock)

    async def _edit(self, text: str, **kwargs) -> None:
        try:
            await self.msg.edit_text(text, **kwargs)
        except TelegramAPIError as e:
            log.warning("status edit failed: %s", e)

    async def on_progress(self, d: dict) -> None:
        await self._progress(lambda: safe_edit(self.msg, progress_text(d)))

    async def on_stage(self, text: str) -> None:
        await self._step(lambda: safe_edit(self.msg, text))

    async def sending(self) -> None:
        await self._step(lambda: self._edit("📤 Отправляю файл..."))

    async def delivered(self) -> None:
        async def delete() -> None:
            try:
                await self.msg.delete()
            except TelegramAPIError:
                pass
        await self._step(delete)

    async def big_file(self, link: str, size: int) -> None:
        await self._step(lambda: self._edit(
            f"📦 Файл слишком большой для Telegram ({format_filesize(size)}).\n\n"
            f"⬇️ <a href=\"{escape(link)}\">Скачать файл</a>\n\n"
            f"Ссылка действует 8 часов.",
        ))

    async def missing_file(self) -> None:
        await self._step(lambda: self._edit("❌ Ошибка: файл не найден после скачивания."))

    async def failed(self, error: Exception, resumable: bool) -> None:
        err_short = escape(str(error)[:200])
        if resumable:
            text = (
                f"❌ Ошибка скачивания:\n<code>{err_short}</code>\n\n"
                "Частичный файл сохранён. Нажми «Повторить» — скачивание продолжится с места остановки."
            )
        else:
            text = (
                f"❌ Произошла ошибка:\n<code>{err_short}</code>\n\n"
                "Нажми «Повторить» для возобновления скачивания."
            )
        await self._step(lambda: self._edit(text, reply_markup=self.retry_kb))

    async def need_cookies(self, url: str, user_id: int) -> None:
        await self._step(lambda: _ask_for_cookies(self.msg, url, user_id))

    async def cancelled(self) -> None:
        await self._step(lambda: self._edit("❌ Отменено."))


async def _execute_download(
    callback: CallbackQuery,
    sid: str,
    fmt_id: str,
    sponsorblock: bool,
    s: dict,
    progress_msg: Message,
) -> None:
    """Common download logic for both initial download and retry."""
    await run_download(
        TextUI(progress_msg, sid, fmt_id, sponsorblock),
        chat_id=callback.message.chat.id,
        user_id=callback.from_user.id,
        sid=sid,
        s=s,
        fmt_id=fmt_id,
        sponsorblock=sponsorblock,
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

    await callback.answer()

    # Build label for progress message
    selected_format, _ = find_format(s, fmt_id)
    label = format_button_label(selected_format) if selected_format else fmt_id
    sb_text = " | SponsorBlock ✅" if sponsorblock else ""

    progress_msg = await callback.message.edit_text(
        f"⬇️ Скачиваю: {escape(label)}{sb_text}\n\nПодготовка..."
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
    selected_format, _ = find_format(s, fmt_id)
    label = format_button_label(selected_format) if selected_format else fmt_id
    sb_text = " | SponsorBlock" if sponsorblock else ""

    progress_msg = await callback.message.edit_text(
        f"🔄 Возобновляю скачивание: {escape(label)}{sb_text}\n\nПодготовка..."
    )

    await _execute_download(callback, sid, fmt_id, sponsorblock, s, progress_msg)
