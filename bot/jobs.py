"""Скачивание и доставка файла — общая логика для текстового интерфейса и виджета.

Интерфейс (DownloadUI) решает только, как показать каждый этап: правкой
текстового сообщения (handlers.TextUI) или состоянием виджета (widget.WidgetUI).
"""

import asyncio
import threading
import time
from typing import Awaitable, Callable, Protocol

import yt_dlp

from bot.cleanup import cleanup_session_files
from bot.config import EXTERNAL_URL, MAX_FILE_SIZE, log
from bot.cookies import COOKIES_INSTRUCTION, needs_cookies, user_cookie_path
from bot.downloader import ProgressSink, download_media, send_local_file
from bot.state import awaiting_cookies, executor, sessions, user_downloads, web_files


def find_format(s: dict, fmt_id: str) -> tuple[dict | None, str]:
    """Найти формат по id в группах сессии. Возвращает (формат, категория).

    format_id не обязан быть числовым (hls-1080, http-720p и т.п. на
    не-YouTube сайтах), поэтому ищем по точному совпадению, а не по isdigit().
    """
    for cat, fmts in s["groups"].items():
        for f in fmts:
            if f["format_id"] == fmt_id:
                return f, cat
    return None, "video_audio"


def cookie_prompt(user_id: int, url: str) -> str:
    """Запомнить ссылку до получения куки и вернуть текст инструкции."""
    awaiting_cookies[user_id] = url
    prefix = ""
    if user_cookie_path(user_id).exists():
        prefix = "⚠️ Сохранённые куки больше не работают — нужны свежие.\n\n"
    return prefix + COOKIES_INSTRUCTION


class DownloadUI(ProgressSink, Protocol):
    async def sending(self) -> None: ...

    async def delivered(self) -> None: ...

    async def big_file(self, link: str, size: int) -> None: ...

    async def missing_file(self) -> None: ...

    async def failed(self, error: Exception, resumable: bool) -> None: ...

    async def need_cookies(self, url: str, user_id: int) -> None: ...

    async def cancelled(self) -> None: ...


class SerialUI:
    """Основа для DownloadUI: порядок правок.

    Прогресс приходит из потока yt-dlp через run_coroutine_threadsafe и может
    опоздать: правка «99%» не должна перетереть «Отправляю файл» или итог.
    Все правки идут по очереди, а после первого не-прогрессного этапа
    прогресс больше не показывается.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._progress_closed = False

    async def _progress(self, show: Callable[[], Awaitable[None]]) -> None:
        async with self._lock:
            if not self._progress_closed:
                await show()

    async def _step(self, show: Callable[[], Awaitable[None]]) -> None:
        async with self._lock:
            self._progress_closed = True
            await show()


async def run_download(
    ui: DownloadUI,
    *,
    chat_id: int,
    user_id: int,
    sid: str,
    s: dict,
    fmt_id: str,
    sponsorblock: bool,
    keep_session: bool = False,
    cancel: threading.Event | None = None,
) -> None:
    """Скачать формат и доставить файл. Все этапы показываются через ui."""
    selected_format, cat_label = find_format(s, fmt_id)
    category = cat_label if selected_format else "custom"

    user_downloads[user_id] = user_downloads.get(user_id, 0) + 1
    loop = asyncio.get_running_loop()
    success = False

    try:
        file_path = await loop.run_in_executor(
            executor,
            download_media,
            s["url"],
            fmt_id,
            sid,
            category,
            sponsorblock,
            loop,
            ui,
            user_id,
            cancel,
        )

        if not file_path.exists():
            await ui.missing_file()
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
            link = f"{EXTERNAL_URL}/dl/{sid}{file_path.suffix}"
            await ui.big_file(link, file_size)
            # каталог сессии теперь занят веб-файлом — новую загрузку в нём не начинаем
            sessions.pop(sid, None)
            success = True
            return

        await ui.sending()
        await send_local_file(chat_id, file_path, title, cat_label)
        await ui.delivered()
        success = True

    except Exception as e:
        # yt-dlp может завернуть DownloadCancelled из хука в DownloadError — сверяемся с флагом
        if isinstance(e, yt_dlp.utils.DownloadCancelled) or (cancel is not None and cancel.is_set()):
            log.info("session %s: download cancelled", sid)
            cleanup_session_files(sid)
            await ui.cancelled()
        elif isinstance(e, yt_dlp.utils.DownloadError):
            log.warning("Download error for session %s: %s", sid, e)
            if needs_cookies(e):
                await ui.need_cookies(s["url"], user_id)
            else:
                # .part-файлы и сессию не трогаем — «Повторить» докачает с места остановки
                await ui.failed(e, resumable=True)
        else:
            log.exception("Download/send error for session %s", sid)
            await ui.failed(e, resumable=False)
    finally:
        user_downloads[user_id] = max(0, user_downloads.get(user_id, 1) - 1)
        if success:
            if sid not in web_files:
                cleanup_session_files(sid)
            if not keep_session:
                sessions.pop(sid, None)
