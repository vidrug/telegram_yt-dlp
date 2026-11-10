"""HTML-виджет (Bot API 10.4): выбор формата и живой прогресс прямо в сообщении.

aiogram 3.31 знает только Bot API 10.3, поэтому методы виджетов описаны здесь
вручную, а апдейты widget_query перехватываются outer-middleware до диспетчера
(иначе он отбрасывает их как неизвестный тип).

HTML отправленного виджета изменить нельзя, поэтому bot/widget.html — тонкий
рендерер, а все тексты, статусы и кнопки задаются здесь, в chat_data:
  {"s": "tbl", "c": [кодеки — столбцы], "r": [качества — строки],
   "v": [[строка, столбец, МБ, 🔊?], ...], "a": [[звук, МБ], ...],
   "dv": видео по умолчанию, "da": звук по умолчанию, "y": SponsorBlock?}  — таблица
  {"s": "job", "l": что качаем, "t": статус, "k": "e"|"ok", "p": % (-1 — бегущая
   полоска), "d": детали, "b": [[текст, запрос], ...]}                    — ход скачивания
Запросы виджета: g:<видео>:<звук>:<sb> — скачать пару (индексы, пусто — без
дорожки), r — повторить, x — отменить, b — вернуться к таблице.
"""

import asyncio
import json
import re
import threading
import zlib
from html import escape
from pathlib import Path
from typing import Any, Awaitable, Callable, NamedTuple

from aiogram.exceptions import TelegramAPIError
from aiogram.methods.base import TelegramMethod
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update

from bot.callbacks import ClassicCallback, RawFormatsCallback
from bot.config import MAX_CONCURRENT_PER_USER, USE_WIDGETS, log
from bot.cookies import is_youtube
from bot.downloader import progress_info
from bot.formats import format_filesize
from bot.jobs import SerialUI, cookie_prompt, run_download
from bot.state import bot, dp, sessions, user_downloads, widget_sessions

MAX_DATA_BYTES = 1024  # chat_data и ответ на запрос
MAX_TABLE_ROWS = 9     # с блоком звука и кнопкой — в пределах ~560 px высоты
MAX_TABLE_COLS = 4
MAX_AUDIO = 5
CODEC_ORDER = ["H.264", "HEVC", "VP9", "AV1", "VP8"]


# ---------------------------------------------------------------------------
# Методы Bot API 10.4
# ---------------------------------------------------------------------------

class SendWidget(TelegramMethod[Message]):
    __returning__ = Message
    __api_method__ = "sendWidget"

    chat_id: int | str
    html: str
    chat_data: str | None = None
    caption: str | None = None
    parse_mode: str | None = None
    allow_queries: bool | None = None
    reply_markup: InlineKeyboardMarkup | None = None


class EditMessageWidgetData(TelegramMethod[bool]):
    __returning__ = bool
    __api_method__ = "editMessageWidgetData"

    chat_id: int | str
    message_id: int
    chat_data: str | None = None


class AnswerWidgetQuery(TelegramMethod[bool]):
    __returning__ = bool
    __api_method__ = "answerWidgetQuery"

    widget_query_id: str
    data: str | None = None


def _load_html() -> str:
    html = Path(__file__).with_name("widget.html").read_text(encoding="utf-8")
    return re.sub(r"<!--.*?-->", "", html, flags=re.S).strip()


WIDGET_HTML = _load_html()

# Включаются после probe(), если локальный Bot API сервер знает методы 10.4
available = False


async def probe() -> bool:
    """Проверить, поддерживает ли Bot API сервер виджеты (ничего не отправляя)."""
    global available
    available = False
    if not USE_WIDGETS:
        return False
    compressed = len(zlib.compress(WIDGET_HTML.encode(), 9))
    if compressed > 4096:
        log.error("widget.html is %d bytes compressed (limit 4096), widgets disabled", compressed)
        return False
    try:
        await bot(AnswerWidgetQuery(widget_query_id="0"))
        available = True
    except TelegramAPIError as e:
        # неизвестный метод — 404; известный с фиктивным id — Bad Request
        available = "method not found" not in str(e).lower()
    except Exception as e:
        log.warning("widget probe failed: %s", e)
    log.info("HTML widgets: %s", "enabled" if available else "unavailable on this Bot API server")
    return available


def _dump(state: dict) -> str:
    data = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
    if len(data.encode()) > MAX_DATA_BYTES and "d" in state:
        state = {k: v for k, v in state.items() if k != "d"}
        data = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
    return data


async def set_state(chat_id: int, message_id: int, state: dict) -> None:
    try:
        await bot(EditMessageWidgetData(chat_id=chat_id, message_id=message_id, chat_data=_dump(state)))
    except TelegramAPIError as e:
        if "message is not modified" not in str(e):
            log.warning("widget state update failed: %s", e)


async def _answer(query_id: str, error: str | None = None) -> None:
    data = json.dumps({"e": error}, ensure_ascii=False) if error else None
    try:
        await bot(AnswerWidgetQuery(widget_query_id=query_id, data=data))
    except TelegramAPIError as e:
        log.warning("answerWidgetQuery failed: %s", e)


# ---------------------------------------------------------------------------
# Список форматов для виджета
# ---------------------------------------------------------------------------

def _bytes(f: dict) -> int:
    return f.get("filesize") or f.get("filesize_approx") or 0


def _video_quality(f: dict) -> str:
    height, fps = f.get("height"), f.get("fps")
    if height:
        return f"{height}p{int(fps) if fps and fps > 30 else ''}"
    return f.get("format_note") or f.get("resolution") or str(f["format_id"])


def _video_rank(f: dict, prefer_separate: bool) -> tuple:
    # Внутри одной ячейки (качество × кодек): отдельная видеодорожка, если есть
    # звук на выбор, затем известный размер (не HLS) и битрейт.
    return (
        (f.get("acodec") in (None, "none")) == prefer_separate,
        _bytes(f) > 0,
        not str(f.get("protocol") or "").startswith("m3u8"),
        f.get("tbr") or 0,
    )


def _codec(f: dict) -> str:
    vcodec = str(f.get("vcodec") or "").lower()
    for prefix, name in (("avc", "H.264"), ("h264", "H.264"), ("hev", "HEVC"), ("hvc", "HEVC"),
                         ("h265", "HEVC"), ("vp09", "VP9"), ("vp9", "VP9"), ("vp8", "VP8"), ("av01", "AV1")):
        if vcodec.startswith(prefix):
            return name
    return (f.get("ext") or "?").upper()  # сайты без сведений о кодеке (VK, Instagram)


def _audio_label(f: dict) -> str:
    acodec = str(f.get("acodec") or "").lower()
    family = next((name for prefix, name in (
        ("mp4a", "AAC"), ("opus", "Opus"), ("vorbis", "Vorbis"), ("mp3", "MP3"),
        ("ac-3", "AC3"), ("ec-3", "EAC3"), ("flac", "FLAC"),
    ) if acodec.startswith(prefix)), (f.get("ext") or "?").upper())
    abr = f.get("abr") or f.get("tbr")
    return f"{family} {round(abr)}k" if abr else family


class VideoCell(NamedTuple):
    fmt_id: str
    row: int
    col: int
    size: int
    muxed: bool  # звук уже внутри (360p у YouTube, всё у VK/Instagram)


class AudioOpt(NamedTuple):
    fmt_id: str
    label: str
    size: int


def _audio_options(groups: dict[str, list[dict]]) -> list[AudioOpt]:
    fmts = [f for f in groups.get("audio_only", []) if "drc" not in str(f["format_id"]).lower()]
    if not fmts:
        fmts = groups.get("audio_only", [])
    # дубляжи и автоперевод — только через «Все форматы», здесь язык оригинала
    top_lang = max((f.get("language_preference") or -1 for f in fmts), default=-1)
    out, seen = [], set()
    for f in sorted(fmts, key=lambda f: f.get("abr") or f.get("tbr") or 0, reverse=True):
        if (f.get("language_preference") or -1) != top_lang:
            continue
        label = _audio_label(f)
        if label in seen:
            continue
        seen.add(label)
        out.append(AudioOpt(str(f["format_id"]), label, _bytes(f)))
        if len(out) >= MAX_AUDIO:
            break
    return out


def build_table(groups: dict[str, list[dict]], max_rows: int = MAX_TABLE_ROWS) -> dict:
    """Матрица «качество × кодек» с одним форматом в ячейке и список звука.

    Возвращает {"rows", "cols", "video": [VideoCell], "audio": [AudioOpt], "dv", "da"},
    индексы video/audio — те же, что видит виджет.
    """
    audio = _audio_options(groups)
    candidates = [f for cat in ("video_audio", "video_only") for f in groups.get(cat, [])]
    # без высоты при наличии других — это HLS-аудио, ошибочно похожее на видео (ext mp4, кодеков нет)
    if any(f.get("height") for f in candidates):
        candidates = [f for f in candidates if f.get("height")]

    best: dict[tuple[str, str], dict] = {}
    for f in candidates:
        key = (_video_quality(f), _codec(f))
        if key not in best or _video_rank(f, bool(audio)) > _video_rank(best[key], bool(audio)):
            best[key] = f

    order = {}
    for (quality, _), f in best.items():
        order[quality] = max(order.get(quality, (0, 0)), (f.get("height") or 0, f.get("fps") or 0))
    rows = sorted(order, key=order.get, reverse=True)[:max_rows]

    used = [codec for (quality, codec) in best if quality in rows]
    cols = sorted(set(used), key=lambda c: (CODEC_ORDER.index(c) if c in CODEC_ORDER else len(CODEC_ORDER), -used.count(c)))
    cols = cols[:MAX_TABLE_COLS]

    video = []
    for ri, quality in enumerate(rows):
        for ci, codec in enumerate(cols):
            f = best.get((quality, codec))
            if f:
                muxed = f.get("acodec") not in (None, "none") or not audio
                video.append(VideoCell(str(f["format_id"]), ri, ci, _bytes(f), muxed))

    # по умолчанию — лучшее H.264 + AAC: так Telegram проигрывает файл прямо в чате
    dv = next((i for i, v in enumerate(video) if cols[v.col] == "H.264"), 0 if video else None)
    da = None
    if audio and not (dv is not None and video[dv].muxed):
        da = next((i for i, a in enumerate(audio) if a.label.startswith("AAC")), 0)
    return {"rows": rows, "cols": cols, "video": video, "audio": audio, "dv": dv, "da": da}


def _mb(size: int) -> float:
    return round(size / 1024 / 1024, 1)


def table_state(s: dict) -> dict:
    last_dv, last_da = s.get("wlast", (None, None))
    for max_rows in range(MAX_TABLE_ROWS, 0, -1):
        t = build_table(s["groups"], max_rows)
        state = {
            "s": "tbl",
            "c": t["cols"],
            "r": t["rows"],
            "v": [[v.row, v.col, _mb(v.size), int(v.muxed)] for v in t["video"]],
            "a": [[a.label, _mb(a.size)] for a in t["audio"]],
            "dv": t["dv"],
            "da": t["da"],
            "y": int(is_youtube(s["url"])),
        }
        if "wlast" in s:  # «Другой формат» — начинаем с прошлого выбора
            state["dv"] = last_dv if last_dv is None or last_dv < len(t["video"]) else t["dv"]
            state["da"] = last_da if last_da is None or last_da < len(t["audio"]) else t["da"]
        # chat_data — максимум 1024 байта: урезаем таблицу снизу
        if len(_dump(state).encode()) <= MAX_DATA_BYTES:
            break
    s["wtable"] = t
    return state


def selection(t: dict, vi: int | None, ai: int | None) -> tuple[str, str, str] | None:
    """Индексы из виджета -> (селектор yt-dlp, как отправить файл, подпись)."""
    v = t["video"][vi] if vi is not None else None
    a = t["audio"][ai] if ai is not None and not (v and v.muxed) else None
    if v is None and a is None:
        return None
    if v:
        vlabel = f"{t['rows'][v.row]} {t['cols'][v.col]}"
    if v and v.muxed:
        return f"{v.fmt_id}/best", "video_audio", f"{vlabel} со звуком"
    if v and a:
        # фолбэки — на случай, если id сменился между показом таблицы и скачиванием
        return (f"{v.fmt_id}+{a.fmt_id}/{v.fmt_id}+bestaudio/bestvideo*+bestaudio/best",
                "video_audio", f"{vlabel} + {a.label}")
    if v:
        return f"{v.fmt_id}/bestvideo*", "video_only", f"{vlabel} без звука"
    return f"{a.fmt_id}/bestaudio/best", "audio_only", f"Звук · {a.label}"


def session_caption(s: dict) -> str:
    title = s.get("title") or "video"
    caption = f"🎬 <b>{escape(title[:300])}</b>"
    if s.get("duration"):
        mins, secs = divmod(int(s["duration"]), 60)
        caption += f"\n⏱ {mins}:{secs:02d}"
    return caption


async def send_picker(chat_id: int, sid: str, s: dict) -> bool:
    """Отправить виджет выбора формата. False — откатиться на классические кнопки."""
    global available
    if not available:
        return False
    state = table_state(s)
    if not state["v"] and not state["a"]:
        return False

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📋 Все форматы", callback_data=RawFormatsCallback(session=sid).pack()),
        InlineKeyboardButton(text="⌨️ Кнопками", callback_data=ClassicCallback(session=sid).pack()),
    ]])
    try:
        msg = await bot(SendWidget(
            chat_id=chat_id,
            html=WIDGET_HTML,
            chat_data=_dump(state),
            caption=session_caption(s),
            parse_mode="HTML",
            allow_queries=True,
            reply_markup=kb,
        ))
    except TelegramAPIError as e:
        log.warning("sendWidget failed, falling back to keyboard: %s", e)
        if "method not found" in str(e).lower():
            available = False
        return False

    widget_sessions[(chat_id, msg.message_id)] = sid
    return True


# ---------------------------------------------------------------------------
# Ход скачивания в виджете
# ---------------------------------------------------------------------------

class WidgetUI(SerialUI):
    def __init__(self, chat_id: int, message_id: int, label: str, sponsorblock: bool) -> None:
        super().__init__()
        self.chat_id = chat_id
        self.message_id = message_id
        self.label = label + (" · ✂️ без рекламы" if sponsorblock else "")

    def _job(self, t: str, p: float | None = None, d: str | None = None,
             b: tuple[tuple[str, str], ...] = (), k: str | None = None) -> Callable[[], Awaitable[None]]:
        state: dict[str, Any] = {"s": "job", "l": self.label, "t": t}
        if p is not None:
            state["p"] = round(p, 1)
        if d:
            state["d"] = d
        if b:
            state["b"] = [list(x) for x in b]
        if k:
            state["k"] = k
        return lambda: set_state(self.chat_id, self.message_id, state)

    async def start(self) -> None:
        await self._progress(self._job("⏳ Подготовка…", -1, b=(("Отменить", "x"),)))

    async def on_progress(self, d: dict) -> None:
        title, pct, details = progress_info(d)
        await self._progress(self._job(title, -1 if pct is None else pct, details, b=(("Отменить", "x"),)))

    async def on_stage(self, text: str) -> None:
        # ffmpeg не прервать — кнопки «Отменить» на этом этапе нет
        await self._step(self._job(text, -1))

    async def sending(self) -> None:
        await self._step(self._job("📤 Отправляю в Telegram…", -1))

    async def delivered(self) -> None:
        await self._step(self._job("✅ Готово — файл ниже", k="ok", b=(("Другой формат", "b"),)))

    async def big_file(self, link: str, size: int) -> None:
        await self._step(self._job(
            f"📦 {format_filesize(size)} — больше лимита Telegram", k="ok",
            d="Ссылка на скачивание — в сообщении ниже, действует 8 часов",
        ))
        await bot.send_message(
            self.chat_id,
            f"⬇️ <a href=\"{escape(link)}\">Скачать файл</a> ({format_filesize(size)})\n\nСсылка действует 8 часов.",
        )

    async def missing_file(self) -> None:
        await self._step(self._job("❌ Файл не найден после скачивания", k="e", b=(("Другой формат", "b"),)))

    async def failed(self, error: Exception, resumable: bool) -> None:
        text = str(error).removeprefix("ERROR: ")[:160]
        if resumable:
            text += " — частичный файл сохранён, «Повторить» докачает с места остановки"
        await self._step(self._job(
            "❌ Ошибка скачивания", k="e", d=text,
            b=(("Повторить", "r"), ("Другой формат", "b")),
        ))

    async def need_cookies(self, url: str, user_id: int) -> None:
        await self._step(self._job("🍪 YouTube требует вход — инструкция ниже", k="e", b=(("Другой формат", "b"),)))
        await bot.send_message(self.chat_id, cookie_prompt(user_id, url), disable_web_page_preview=True)

    async def cancelled(self) -> None:
        await self._step(self._job("⏹ Скачивание отменено", b=(("Другой формат", "b"),)))


# ---------------------------------------------------------------------------
# Запросы от виджета
# ---------------------------------------------------------------------------

_tasks: set[asyncio.Task] = set()


def _start_job(chat_id: int, message_id: int, sid: str, s: dict, user_id: int) -> None:
    selector, send_as, label, sponsorblock = s["wsel"]
    cancel = threading.Event()
    s["job"] = cancel
    ui = WidgetUI(chat_id, message_id, label, sponsorblock)

    async def runner() -> None:
        try:
            await ui.start()
            await run_download(
                ui, chat_id=chat_id, user_id=user_id, sid=sid, s=s,
                fmt_id=selector, sponsorblock=sponsorblock, keep_session=True, cancel=cancel,
                category="pair", send_as=send_as,
            )
        finally:
            s.pop("job", None)

    task = asyncio.create_task(runner())
    _tasks.add(task)  # держим ссылку, иначе задачу может собрать GC
    task.add_done_callback(_tasks.discard)


async def handle_query(q: dict) -> None:
    query_id = q.get("id") or ""
    data = q.get("data") or ""
    user_id = (q.get("from") or {}).get("id")
    chat_id = (q.get("chat") or {}).get("id")
    message_id = q.get("message_id")

    sid = widget_sessions.get((chat_id, message_id))
    s = sessions.get(sid) if sid else None
    if s is None:
        widget_sessions.pop((chat_id, message_id), None)
        await _answer(query_id, "Сессия истекла — отправь ссылку заново")
        if chat_id and message_id:
            await set_state(chat_id, message_id, {"s": "job", "t": "⏰ Сессия истекла — отправь ссылку заново"})
        return
    if s["user_id"] != user_id:
        await _answer(query_id, "Это не твой запрос")
        return

    cmd, _, arg = data.partition(":")

    if cmd in ("g", "r"):
        if s.get("job"):
            await _answer(query_id, "Уже скачиваю")
            return
        if cmd == "g":
            t = s.get("wtable")
            parts = arg.split(":")
            if t is None or len(parts) != 3:
                await _answer(query_id, "Формат не найден — отправь ссылку заново")
                return
            vi, ai, sb_flag = parts
            try:
                vi_ = int(vi) if vi else None
                ai_ = int(ai) if ai else None
                chosen = selection(t, vi_, ai_)
            except (ValueError, IndexError):
                chosen = None
            if chosen is None:
                await _answer(query_id, "Выбери видео и/или звук")
                return
            selector, send_as, label = chosen
            s["wsel"] = (selector, send_as, label, sb_flag == "1")
            s["wlast"] = (vi_, ai_)
        elif not s.get("wsel"):
            await _answer(query_id, "Сначала выбери формат")
            return
        if user_downloads.get(user_id, 0) >= MAX_CONCURRENT_PER_USER:
            await _answer(query_id, f"Не больше {MAX_CONCURRENT_PER_USER} скачиваний сразу — подожди")
            return
        await _answer(query_id)
        _start_job(chat_id, message_id, sid, s, user_id)

    elif cmd == "x":
        await _answer(query_id)
        cancel = s.get("job")
        if cancel is not None:
            cancel.set()  # хук прогресса yt-dlp прервёт скачивание

    elif cmd == "b":
        if s.get("job"):
            await _answer(query_id, "Дождись окончания или отмени скачивание")
            return
        await _answer(query_id)
        await set_state(chat_id, message_id, table_state(s))

    else:
        await _answer(query_id, "Неизвестная команда")


def _is_download_query(query: dict) -> bool:
    # Запросы панели владельца (bot/widgets.py) начинаются с цифры — версии её протокола.
    # Наши (g:…, r, x, b) — никогда, и это верно даже для виджетов, отправленных до рестарта.
    return not (query.get("data") or "")[:1].isdigit()


async def widget_query_middleware(handler, event, data):
    """Outer-middleware на dp.update: забирает widget_query виджетов скачивания.

    Остальные (панель /panel) пропускает дальше — к middleware из bot/widgets.py,
    поэтому этот модуль должен импортироваться раньше неё (см. __main__).
    """
    query = (event.model_extra or {}).get("widget_query") if isinstance(event, Update) else None
    if query is None or not _is_download_query(query):
        return await handler(event, data)
    try:
        await handle_query(query)
    except Exception:
        log.exception("widget query failed")
    return None


dp.update.outer_middleware(widget_query_middleware)
