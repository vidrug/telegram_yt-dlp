"""HTML-виджет (Bot API 10.4): выбор формата и живой прогресс прямо в сообщении.

aiogram 3.31 знает только Bot API 10.3, поэтому методы виджетов описаны здесь
вручную, а апдейты widget_query перехватываются outer-middleware до диспетчера
(иначе он отбрасывает их как неизвестный тип).

HTML отправленного виджета изменить нельзя, поэтому bot/widget.html — тонкий
рендерер, а все тексты, статусы и кнопки задаются здесь, в chat_data:
  {"s": "pick", "c": [кодеки], "r": [[качество, [ячейка по кодекам]], ...],
   "a": [[звук, МБ], ...], "y": 1}                                    — выбор формата
  {"s": "job", "l": что качаем, "t": статус, "k": "e"|"ok", "p": % (-1 — бегущая
   полоска), "w": "v"|"a", "d": детали, "b": [[текст, запрос], ...]}  — ход скачивания
Запросы виджета: g:<строка.столбец>:<звук>:<sb> — скачать (любой из двух ключей может
быть пустым), r — повторить, x — отменить, b — вернуться к выбору формата.
"""

import asyncio
import json
import re
import threading
import zlib
from collections import Counter
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
from bot import fulltable
from bot.formats import CODEC_ORDER, codec_name, fmt_bytes, format_filesize, pair_selector, video_quality
from bot.jobs import SerialUI, cookie_prompt, run_download
from bot.state import bot, dp, sessions, user_downloads, widget_sessions

MAX_DATA_BYTES = 1024  # chat_data и ответ на запрос


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


_REGEX_BEFORE = set("(,=:[!&|?{};+-*%<>~^") | {""}
# перевод строки не нужен после этих символов (выражение продолжается) и перед вторыми:
# автоподстановка «;» там невозможна. После «}» и «)» он нужен — там может кончаться оператор
_NO_NL_AFTER = set("{([,;=:?&|+-*%<>!~^")
_NO_NL_BEFORE = set(")]},;.?:")


def _is_word(ch: str) -> bool:
    return bool(ch) and (ch.isalnum() or ch in "_$" or ord(ch) > 127)


def minify_js(src: str) -> str:
    """Убрать из JS комментарии и лишние пробелы, не трогая строки, шаблоны и регулярки.

    Переводы строк остаются (по одному): код виджета полагается на автоподстановку «;».
    Пробел остаётся только между двумя символами слов и в «+ +» / «- -».
    """
    out: list[str] = []
    stack: list[int] = []  # глубина фигурных скобок внутри каждого открытого ${…} шаблона
    gap = ""               # пропущенные пробелы: "", " " или "\n"
    i, n = 0, len(src)

    def last() -> str:
        return out[-1][-1] if out else ""

    def put(tok: str) -> None:
        nonlocal gap
        tail = out[-1][-2:] if out else ""
        # «n--» / «i++» в конце строки — конец оператора, перевод строки нужен
        if gap == "\n" and out and tok[0] not in _NO_NL_BEFORE and (
                last() not in _NO_NL_AFTER or tail in ("++", "--")):
            out.append("\n")
        elif gap and ((_is_word(last()) and _is_word(tok[0])) or (last() and last() in "+-" and tok[0] == last())):
            out.append(" ")
        gap = ""
        out.append(tok)

    def template(start: int) -> int:
        """Скопировать шаблонную строку с позиции после «`» до «`» или «${». Вернуть позицию после."""
        j = start
        while j < n:
            if src[j] == "\\":
                j += 2
            elif src[j] == "`":
                out.append(src[start:j + 1])
                return j + 1
            elif src.startswith("${", j):
                out.append(src[start:j + 2])
                stack.append(0)
                return j + 2
            else:
                j += 1
        raise ValueError("unterminated template literal")

    while i < n:
        ch = src[i]
        if ch in " \t\r\n":
            if ch == "\n" or (gap == "\n"):
                gap = "\n"
            elif not gap:
                gap = " "
            i += 1
        elif src.startswith("//", i):
            i = src.find("\n", i)
            i = n if i < 0 else i
        elif src.startswith("/*", i):
            i = src.index("*/", i) + 2
            gap = gap or " "
        elif ch in "'\"":
            j = i + 1
            while src[j] != ch:
                j += 2 if src[j] == "\\" else 1
            put(src[i:j + 1])
            i = j + 1
        elif ch == "`":
            put("`")
            i = template(i + 1)
        elif ch == "/" and (last() in _REGEX_BEFORE or "".join(out).endswith(("return", "typeof"))):
            j, in_class = i + 1, False
            while in_class or src[j] != "/":
                if src[j] == "\\":
                    j += 1
                elif src[j] == "[":
                    in_class = True
                elif src[j] == "]":
                    in_class = False
                j += 1
            j += 1
            while j < n and src[j].isalpha():  # флаги
                j += 1
            put(src[i:j])
            i = j
        elif ch == "{" and stack:
            stack[-1] += 1
            put(ch)
            i += 1
        elif ch == "}" and stack:
            if stack[-1] == 0:  # конец ${…} — дальше снова текст шаблона
                stack.pop()
                put("}")
                i = template(i + 1)
            else:
                stack[-1] -= 1
                put(ch)
                i += 1
        else:
            put(ch)
            i += 1
    return "".join(out)


def _load_html() -> str:
    html = Path(__file__).with_name("widget.html").read_text(encoding="utf-8")
    html = re.sub(r"<!--.*?-->", "", html, flags=re.S)
    # комментарии и переводы строк в CSS тоже уходят в Telegram и съедают лимит 4 КБ
    html = re.sub(r"<style>.*?</style>", lambda m: re.sub(r"\n+", "", re.sub(r"/\*.*?\*/", "", m.group(0), flags=re.S)),
                  html, count=1, flags=re.S)
    html = re.sub(r"(?<=<script>)(.*?)(?=</script>)", lambda m: minify_js(m.group(1)), html, count=1, flags=re.S)
    return html.strip()


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
# Матрица форматов для виджета: строки — качество, столбцы — кодеки, плюс звук
# ---------------------------------------------------------------------------

MAX_ROWS = 8    # с быстрыми кнопками, звуком и SponsorBlock — в пределах 600 px высоты виджета
MAX_COLS = 3
MAX_AUDIO = 2   # ячейки звука в одну строку под матрицей; остальное — в «Все форматы»


def _cell_rank(f: dict, separate_audio: bool) -> tuple:
    # В ячейке — лучший из форматов этого качества и кодека: при отдельном звуке —
    # видео без звука (звук выберут сами), затем известный размер, не HLS, битрейт
    return (
        not (separate_audio and fulltable._muxed(f)),
        fmt_bytes(f) > 0,
        not str(f.get("protocol") or "").startswith("m3u8"),
        f.get("tbr") or 0,
    )


def _mb(f: dict) -> float:
    size = fmt_bytes(f) / 2 ** 20
    return round(size, 1) if size < 10 else round(size)


def build_matrix(s: dict) -> dict | None:
    """Разложить форматы сессии для виджета. None — показывать нечего."""
    video, audio = fulltable.split_formats(s.get("raw_formats") or [])

    # звук: язык оригинала, без DRC, по одному на кодек и битрейт, сначала с известным размером.
    # AAC — первым: виджет подставляет первый звук к выбранному видео, а AAC в MP4 Telegram
    # проигрывает везде (как H.264 — первый столбец матрицы)
    top = max((f.get("language_preference") or -1 for f in audio), default=-1)
    candidates = sorted(
        (f for f in audio
         if (f.get("language_preference") or -1) == top and "drc" not in str(f["format_id"]).lower()),
        key=lambda f: (not str(f.get("acodec") or "").startswith("mp4a"),
                       -(f.get("abr") or f.get("tbr") or 0), fmt_bytes(f) == 0),
    ) or audio
    sound, seen = [], set()
    for f in candidates:  # лучший по битрейту из каждого семейства: AAC, Opus…
        family = fulltable._audio_kind(f).split()[0]
        if family not in seen:
            seen.add(family)
            sound.append(f)
    sound = sound[:MAX_AUDIO]

    rows: dict[str, dict[str, int]] = {}  # качество -> кодек -> индекс в video (порядок — от лучшего)
    for i, f in enumerate(video):
        cell = rows.setdefault(video_quality(f), {})
        codec = codec_name(f)
        if codec not in cell or _cell_rank(f, bool(sound)) > _cell_rank(video[cell[codec]], bool(sound)):
            cell[codec] = i
    quals = list(rows)[:MAX_ROWS]
    usage = Counter(c for q in quals for c in rows[q])
    keep = {c for c, _ in usage.most_common(MAX_COLS)}
    # столбцы — от самого совместимого кодека: «Лучшее/Среднее/Минимум» берут первый столбец с форматом
    codecs = sorted(keep, key=lambda c: (CODEC_ORDER.index(c) if c in CODEC_ORDER else len(CODEC_ORDER), -usage[c]))
    quals = [q for q in quals if any(c in rows[q] for c in codecs)]
    if not quals and not sound:
        return None
    return {
        "video": video, "audio": sound, "quals": quals, "codecs": codecs,
        "grid": [[rows[q].get(c) for c in codecs] for q in quals],
    }


def _cell_value(f: dict) -> float | list:
    # размер в МБ (0 — неизвестен); [МБ, 1] — звук уже внутри
    return [_mb(f), 1] if fulltable._muxed(f) else _mb(f)


def pick_state(s: dict) -> dict:
    m = s["wm"]
    state = {
        "s": "pick",
        "c": m["codecs"],
        "r": [[q, [None if i is None else _cell_value(m["video"][i]) for i in row]]
              for q, row in zip(m["quals"], m["grid"])],
        "a": [[fulltable._audio_kind(f), _mb(f)] for f in m["audio"]],
        "y": int(is_youtube(s["url"])),
    }
    # chat_data — максимум 1024 байта: урезаем матрицу снизу (индексы строк в запросах не сдвигаются)
    while len(_dump(state).encode()) > MAX_DATA_BYTES and state["r"]:
        state["r"].pop()
    return state


def selection(s: dict, vkey: str, akey: str) -> tuple[str, str, str] | None:
    """Ключи из запроса виджета («строка.столбец», номер звука) -> (селектор, как отправить, подпись).

    ValueError / IndexError — ключ не из этой матрицы.
    """
    m = s["wm"]
    v = a = None
    if vkey:
        r, c = (int(x) for x in vkey.split("."))
        if r < 0 or c < 0 or m["grid"][r][c] is None:
            raise IndexError(vkey)
        v = m["video"][m["grid"][r][c]]
    if akey:
        if int(akey) < 0:
            raise IndexError(akey)
        a = m["audio"][int(akey)]
    muxed = bool(v and fulltable._muxed(v))
    if muxed:
        a = None  # звук уже внутри
    return pair_selector(
        str(v["format_id"]) if v else None, f"{video_quality(v)} {codec_name(v)}" if v else "", muxed,
        str(a["format_id"]) if a else None, fulltable._audio_kind(a) if a else "",
    )


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
    matrix = build_matrix(s)
    if matrix is None:
        return False
    s["wm"] = matrix

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
            state["w"] = w  # цвет полоски: видео — красная, звук — циановая
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

    if cmd in ("g", "r"):
        if s.get("job"):
            await _answer(query_id, "Уже скачиваю")
            return
        if cmd == "g":
            vkey, akey, sb_flag = (arg.split(":") + ["", ""])[:3]
            try:
                chosen = selection(s, vkey, akey) if "wm" in s else None
            except (ValueError, IndexError):
                chosen = None
            if not chosen:
                await _answer(query_id, "Формат не найден — отправь ссылку заново")
                return
            selector, send_as, label = chosen
            s["wsel"] = {
                "fmt": selector, "label": label, "sb": sb_flag == "1",
                "category": "pair", "send_as": send_as, "standalone": False,
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
        if "wm" not in s:  # виджет только с прогрессом — выбора формата в нём нет
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
