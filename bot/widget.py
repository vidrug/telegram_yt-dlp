"""HTML-виджет (Bot API 10.4): выбор формата и живой прогресс прямо в сообщении.

aiogram 3.31 знает только Bot API 10.3, поэтому методы виджетов описаны здесь
вручную, а апдейты widget_query перехватываются outer-middleware до диспетчера
(иначе он отбрасывает их как неизвестный тип).

HTML отправленного виджета изменить нельзя, поэтому bot/widget.html — тонкий
рендерер, а все тексты, статусы и кнопки задаются здесь, в chat_data:
  {"s": "pick", "v": [[качество, детали], ...], "a": [...], "y": 1}  — выбор формата
  {"s": "job", "l": что качаем, "t": статус, "k": "e"|"ok", "p": % (-1 — бегущая
   полоска), "d": детали, "b": [[текст, запрос], ...]}               — ход скачивания
Запросы виджета: d:<v|a><i>:<sb> — скачать, r — повторить, x — отменить,
b — вернуться к выбору формата.
"""

import asyncio
import json
import re
import threading
import zlib
from html import escape
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiogram.exceptions import TelegramAPIError
from aiogram.methods.base import TelegramMethod
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update

from bot.callbacks import ClassicCallback, RawFormatsCallback
from bot.config import MAX_CONCURRENT_PER_USER, USE_WIDGETS, WIDGET_PROGRESS, log
from bot.cookies import is_youtube
from bot.downloader import progress_info, progress_track
from bot.formats import fmt_bytes, format_filesize, video_quality
from bot.jobs import SerialUI, cookie_prompt, run_download
from bot.state import bot, dp, sessions, user_downloads, widget_sessions

MAX_DATA_BYTES = 1024  # chat_data и ответ на запрос
MAX_VIDEO_ROWS = 9     # с вкладками и SponsorBlock — в пределах ~560 px высоты
MAX_AUDIO_ROWS = 4


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
    if not (USE_WIDGETS or WIDGET_PROGRESS):
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





def _video_rank(f: dict) -> tuple:
    # Внутри одного качества: известный размер (не HLS), затем H.264 — Telegram
    # проигрывает его прямо в чате на всех клиентах, VP9/AV1 в MP4 — не везде.
    return (
        fmt_bytes(f) > 0,
        not str(f.get("protocol") or "").startswith("m3u8"),
        str(f.get("vcodec") or "").startswith("avc1"),
        f.get("tbr") or 0,
    )


def _audio_options(groups: dict[str, list[dict]]) -> list[tuple[str, str, str, str]]:
    fmts = [f for f in groups.get("audio_only", []) if "drc" not in str(f["format_id"]).lower()]
    if not fmts:
        fmts = groups.get("audio_only", [])
    # дубляжи и автоперевод — только через «Все форматы», здесь язык оригинала
    top_lang = max((f.get("language_preference") or -1 for f in fmts), default=-1)
    out, seen = [], set()
    for f in sorted(fmts, key=lambda f: f.get("abr") or f.get("tbr") or 0, reverse=True):
        if (f.get("language_preference") or -1) != top_lang:
            continue
        abr = f.get("abr") or f.get("tbr")
        key = (f.get("ext"), round(abr or 0))
        if key in seen:
            continue
        seen.add(key)
        q = f"{round(abr)} kbps" if abr else (f.get("format_note") or str(f["format_id"]))
        out.append((f["format_id"], "audio_only", q,
                    f"{(f.get('ext') or '?').upper()} · {format_filesize(fmt_bytes(f))}"))
        if len(out) >= MAX_AUDIO_ROWS:
            break
    return out


def build_options(groups: dict[str, list[dict]]) -> dict[str, list[tuple[str, str, str, str]]]:
    """Сжатый список для виджета: по одному формату на качество.

    Возвращает {"v": [...], "a": [...]} из кортежей
    (format_id, категория, качество, детали).
    """
    candidates = [(f, cat) for cat in ("video_audio", "video_only") for f in groups.get(cat, [])]
    # без высоты при наличии других — это HLS-аудио, ошибочно похожее на видео (ext mp4, кодеков нет)
    if any(f.get("height") for f, _ in candidates):
        candidates = [(f, cat) for f, cat in candidates if f.get("height")]

    best: dict[str, tuple[dict, str]] = {}
    for f, cat in candidates:
        q = video_quality(f)
        if q not in best or _video_rank(f) > _video_rank(best[q][0]):
            best[q] = (f, cat)
    video = sorted(
        best.items(),
        key=lambda kv: (kv[1][0].get("height") or 0, kv[1][0].get("fps") or 0),
        reverse=True,
    )[:MAX_VIDEO_ROWS]

    audio = _audio_options(groups)
    audio_bytes = max((fmt_bytes(f) for f in groups.get("audio_only", [])), default=0)

    rows = []
    for q, (f, cat) in video:
        ext = "MP4" if cat == "video_only" else (f.get("ext") or "?").upper()
        codec = str(f.get("vcodec") or "").split(".")[0]
        parts = [ext]
        if codec and not codec.startswith(("avc1", "none")):
            parts.append({"vp09": "VP9", "vp9": "VP9", "av01": "AV1"}.get(codec, codec.upper()))
        size = fmt_bytes(f)
        if cat == "video_only" and size and audio_bytes:
            # к видео без звука при скачивании приклеится лучшая аудиодорожка
            parts.append(f"≈{format_filesize(size + audio_bytes)}")
        else:
            parts.append(format_filesize(size))
        rows.append((f["format_id"], cat, q, " · ".join(parts)))

    return {"v": rows, "a": audio}


def pick_state(s: dict) -> dict:
    opts = s["wopts"]
    state = {
        "s": "pick",
        "v": [[q, m] for _, _, q, m in opts["v"]],
        "a": [[q, m] for _, _, q, m in opts["a"]],
        "y": int(is_youtube(s["url"])),
    }
    # chat_data — максимум 1024 байта: урезаем длинный список с конца
    while len(_dump(state).encode()) > MAX_DATA_BYTES:
        longest = "v" if len(state["v"]) >= len(state["a"]) else "a"
        state[longest].pop()
        opts[longest].pop()
    return state


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
    if not (available and USE_WIDGETS):
        return False
    s["wopts"] = build_options(s["groups"])
    if not s["wopts"]["v"] and not s["wopts"]["a"]:
        return False

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📋 Все форматы", callback_data=RawFormatsCallback(session=sid).pack()),
        InlineKeyboardButton(text="⌨️ Кнопками", callback_data=ClassicCallback(session=sid).pack()),
    ]])
    try:
        msg = await bot(SendWidget(
            chat_id=chat_id,
            html=WIDGET_HTML,
            chat_data=_dump(pick_state(s)),
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
    """Ход скачивания в виджете.

    standalone — виджет только для прогресса (формат выбран кнопками или в «Все
    форматы»): «Другого формата» в нём нет, а после отправки файла он удаляется,
    как раньше удалялось текстовое сообщение о прогрессе.
    """

    def __init__(self, chat_id: int, message_id: int, label: str, sponsorblock: bool,
                 standalone: bool = False) -> None:
        super().__init__()
        self.chat_id = chat_id
        self.message_id = message_id
        self.label = label + (" · ✂️ без рекламы" if sponsorblock else "")
        self.standalone = standalone
        self.back = () if standalone else (("Другой формат", "b"),)

    def _job(self, t: str, p: float | None = None, d: str | None = None,
             b: tuple[tuple[str, str], ...] = (), k: str | None = None,
             w: str = "") -> Callable[[], Awaitable[None]]:
        state: dict[str, Any] = {"s": "job", "l": self.label, "t": t}
        if p is not None:
            state["p"] = round(p, 1)
        if w:
            state["w"] = w  # цвет полоски: видео — розовая, звук — оранжевая
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
        await self._progress(self._job(title, -1 if pct is None else pct, details,
                                       b=(("Отменить", "x"),), w=progress_track(d)))

    async def on_stage(self, text: str) -> None:
        # ffmpeg не прервать — кнопки «Отменить» на этом этапе нет
        await self._step(self._job(text, -1))

    async def sending(self) -> None:
        await self._step(self._job("📤 Отправляю в Telegram…", -1))

    async def delivered(self) -> None:
        if not self.standalone:
            await self._step(self._job("✅ Готово — файл ниже", k="ok", b=self.back))
            return

        async def remove() -> None:
            widget_sessions.pop((self.chat_id, self.message_id), None)
            try:
                await bot.delete_message(self.chat_id, self.message_id)
            except TelegramAPIError as e:
                log.warning("progress widget delete failed: %s", e)
        await self._step(remove)

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
        await self._step(self._job("❌ Файл не найден после скачивания", k="e", b=(("Повторить", "r"),) + self.back))

    async def failed(self, error: Exception, resumable: bool) -> None:
        text = str(error).removeprefix("ERROR: ")[:160]
        if resumable:
            text += " — частичный файл сохранён, «Повторить» докачает с места остановки"
        await self._step(self._job(
            "❌ Ошибка скачивания", k="e", d=text,
            b=(("Повторить", "r"),) + self.back,
        ))

    async def need_cookies(self, url: str, user_id: int) -> None:
        await self._step(self._job("🍪 YouTube требует вход — инструкция ниже", k="e", b=self.back))
        await bot.send_message(self.chat_id, cookie_prompt(user_id, url), disable_web_page_preview=True)

    async def cancelled(self) -> None:
        await self._step(self._job("⏹ Скачивание отменено", b=(("Скачать заново", "r"),) + self.back))


async def start_progress(
    chat_id: int, sid: str, s: dict, user_id: int, *, fmt_id: str, label: str, sponsorblock: bool,
    category: str | None = None, send_as: str | None = None,
) -> bool:
    """Показать прогресс виджетом и начать скачивание. False — показать прогресс текстом."""
    if not (available and WIDGET_PROGRESS):
        return False
    first = {"s": "job", "l": label + (" · ✂️ без рекламы" if sponsorblock else ""),
             "t": "⏳ Подготовка…", "p": -1, "b": [["Отменить", "x"]]}
    try:
        msg = await bot(SendWidget(
            chat_id=chat_id, html=WIDGET_HTML, chat_data=_dump(first),
            caption=session_caption(s), parse_mode="HTML", allow_queries=True,
        ))
    except TelegramAPIError as e:
        log.warning("progress widget failed, falling back to text: %s", e)
        return False
    widget_sessions[(chat_id, msg.message_id)] = sid
    s["wsel"] = {"fmt": fmt_id, "label": label, "sb": sponsorblock,
                 "category": category, "send_as": send_as, "standalone": True}
    _start_job(chat_id, msg.message_id, sid, s, user_id)
    return True


# ---------------------------------------------------------------------------
# Запросы от виджета
# ---------------------------------------------------------------------------

_tasks: set[asyncio.Task] = set()


def _start_job(chat_id: int, message_id: int, sid: str, s: dict, user_id: int) -> None:
    sel = s["wsel"]
    cancel = threading.Event()
    s["job"] = cancel
    ui = WidgetUI(chat_id, message_id, sel["label"], sel["sb"], standalone=sel["standalone"])

    async def runner() -> None:
        try:
            await ui.start()
            await run_download(
                ui, chat_id=chat_id, user_id=user_id, sid=sid, s=s,
                fmt_id=sel["fmt"], sponsorblock=sel["sb"], keep_session=True, cancel=cancel,
                category=sel["category"], send_as=sel["send_as"],
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

    if cmd in ("d", "r"):
        if s.get("job"):
            await _answer(query_id, "Уже скачиваю")
            return
        if cmd == "d":
            key, _, sb_flag = arg.partition(":")
            tab, idx = key[:1], key[1:]
            opts = s.get("wopts", {}).get(tab) or []
            if not idx.isdigit() or int(idx) >= len(opts):
                await _answer(query_id, "Формат не найден — отправь ссылку заново")
                return
            fmt_id, _, quality, details = opts[int(idx)]
            s["wsel"] = {
                "fmt": fmt_id, "label": f"{quality} · {details.split(' · ')[0]}", "sb": sb_flag == "1",
                "category": None, "send_as": None, "standalone": False,
            }
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
        if "wopts" not in s:  # виджет только с прогрессом — выбора формата в нём нет
            await _answer(query_id, "Выбери формат кнопками в сообщении выше")
            return
        if s.get("job"):
            await _answer(query_id, "Дождись окончания или отмени скачивание")
            return
        await _answer(query_id)
        await set_state(chat_id, message_id, pick_state(s))

    else:
        await _answer(query_id, "Неизвестная команда")


def _is_download_query(query: dict) -> bool:
    # Запросы панели владельца (bot/widgets.py) начинаются с цифры — версии её протокола.
    # Наши (d:…, r, x, b) — никогда, и это верно даже для виджетов, отправленных до рестарта.
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
