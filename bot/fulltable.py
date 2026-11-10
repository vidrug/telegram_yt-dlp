"""«📋 Все форматы»: полная нативная таблица (rich message), где ID формата — кнопка.

Тап по ID видео и по ID звука выбирает пару (выбранные подсвечены), внизу —
итог и «Скачать». Выбор хранится в сессии (s["full_sel"]), а сообщение
перерисовывается editMessageText(rich_message=...). Ручной ввод формата
текстом (315+251) по-прежнему работает.
"""

from aiogram.types import (
    InputRichBlockButtons,
    InputRichBlockParagraph,
    InputRichBlockSectionHeading,
    InputRichBlockTable,
    InputRichMessage,
    RichBlockTableCell,
    RichMessageButton,
    RichTextBold,
    RichTextButton,
)

from bot.callbacks import FullPickCallback
from bot.cookies import is_youtube
from bot.formats import (
    CODEC_ORDER,
    audio_label,
    classify_format,
    codec_name,
    fmt_bytes,
    format_filesize,
    pair_selector,
    video_quality,
)


def split_formats(raw_formats: list[dict]) -> tuple[list[dict], list[dict]]:
    """Все форматы -> (видео, звук), в порядке показа."""
    seen, usable = set(), []
    for f in raw_formats:
        fid = f.get("format_id")
        if fid is None or fid in seen or classify_format(f) is None:  # None — раскадровки
            continue
        seen.add(fid)
        usable.append(f)

    has_height = any(f.get("height") for f in usable)
    video, audio = [], []
    for f in usable:
        # без высоты при наличии других — HLS-звук, ошибочно похожий на видео (ext mp4, кодеков нет)
        if classify_format(f) == "audio_only" or (has_height and not f.get("height")):
            audio.append(f)
        else:
            video.append(f)

    def codec_rank(f: dict) -> int:
        name = codec_name(f)
        return CODEC_ORDER.index(name) if name in CODEC_ORDER else len(CODEC_ORDER)

    video.sort(key=lambda f: (
        -(f.get("height") or 0), -(f.get("fps") or 0), codec_rank(f),
        str(f.get("protocol") or "").startswith("m3u8"), -(f.get("tbr") or 0),
    ))
    audio.sort(key=lambda f: (
        -(f.get("language_preference") or -1), "drc" in str(f["format_id"]).lower(),
        -(f.get("abr") or f.get("tbr") or 0),
    ))
    return video, audio


def _muxed(f: dict) -> bool:
    return f.get("acodec") not in (None, "none") or (
        f.get("vcodec") is None and f.get("acodec") is None  # VK/Instagram: кодеки неизвестны, звук внутри
    )


def _audio_tags(f: dict) -> str:
    note = (f.get("format_note") or "").lower()
    tags = [f.get("language") or ""]
    if "original" in note:
        tags.append("ориг.")
    elif "dubbed" in note:
        tags.append("дубляж")
    if "drc" in str(f["format_id"]).lower() or "drc" in note:
        tags.append("DRC")
    return " · ".join(t for t in tags if t) or "—"


def _size(f: dict) -> str:
    size = fmt_bytes(f)
    if size:
        return format_filesize(size)
    return "HLS" if str(f.get("protocol") or "").startswith("m3u8") else "?"


def _cell(text, align: str = "left", header: bool = False) -> RichBlockTableCell:
    return RichBlockTableCell(align=align, valign="middle", text=text, is_header=header or None)


def _id_cell(sid: str, kind: str, idx: int, fid: str, selected: bool) -> RichBlockTableCell:
    button = RichMessageButton(
        text=str(fid),
        callback_data=FullPickCallback(session=sid, act=kind, idx=idx).pack(),
        style="primary" if selected else "link",
    )
    return _cell(RichTextButton(button=button))


def _audio_kind(f: dict) -> str:
    if f.get("acodec") in (None, "none"):  # HLS-звук без сведений о кодеке
        return "HLS"
    return audio_label(f)


def _video_label(f: dict) -> str:
    return f"{video_quality(f)} {codec_name(f)} ({f['format_id']})"


def _audio_name(f: dict) -> str:
    return f"{_audio_kind(f)} ({f['format_id']})"


def prepare(s: dict) -> None:
    """Разложить форматы сессии для таблицы; выбор начинается пустым."""
    video, audio = split_formats(s.get("raw_formats") or [])
    s["full"] = {"video": video, "audio": audio}
    s["full_sel"] = {"v": None, "a": None, "sb": False}


def selection(s: dict) -> tuple[str, str, str] | None:
    full, sel = s["full"], s["full_sel"]
    v = full["video"][sel["v"]] if sel["v"] is not None else None
    a = full["audio"][sel["a"]] if sel["a"] is not None and not (v and _muxed(v)) else None
    return pair_selector(
        str(v["format_id"]) if v else None, _video_label(v) if v else "", bool(v and _muxed(v)),
        str(a["format_id"]) if a else None, _audio_name(a) if a else "",
    )


def toggle(s: dict, act: str, idx: int) -> str | None:
    """Применить тап. Возвращает текст подсказки, если тап ничего не меняет."""
    full, sel = s["full"], s["full_sel"]
    if act == "v" and 0 <= idx < len(full["video"]):
        sel["v"] = None if sel["v"] == idx else idx
        if sel["v"] is not None and _muxed(full["video"][idx]):
            sel["a"] = None  # звук уже внутри
    elif act == "a" and 0 <= idx < len(full["audio"]):
        if sel["v"] is not None and _muxed(full["video"][sel["v"]]):
            return "У выбранного видео звук уже внутри — сначала сними его выбор"
        sel["a"] = None if sel["a"] == idx else idx
    elif act == "sb":
        sel["sb"] = not sel["sb"]
    elif act == "clr":
        sel.update(v=None, a=None)
    else:
        return "Формат не найден — нажми «Все форматы» ещё раз"
    return None


def render(sid: str, s: dict) -> InputRichMessage:
    full, sel = s["full"], s["full_sel"]
    blocks = []

    if full["video"]:
        rows = [[_cell(RichTextBold(text=h), align, header=True) for h, align in (
            ("ID", "left"), ("Качество", "left"), ("Кодек", "left"), ("Размер", "right"))]]
        for i, f in enumerate(full["video"]):
            codec = codec_name(f) + (" 🔊" if _muxed(f) else "")
            rows.append([
                _id_cell(sid, "v", i, f["format_id"], sel["v"] == i),
                _cell(video_quality(f)),
                _cell(codec),
                _cell(_size(f), "right"),
            ])
        blocks += [InputRichBlockSectionHeading(text="🎬 Видео", size=4),
                   InputRichBlockTable(cells=rows, is_striped=True, is_compact=True)]

    if full["audio"]:
        rows = [[_cell(RichTextBold(text=h), align, header=True) for h, align in (
            ("ID", "left"), ("Звук", "left"), ("Язык", "left"), ("Размер", "right"))]]
        for i, f in enumerate(full["audio"]):
            rows.append([
                _id_cell(sid, "a", i, f["format_id"], sel["a"] == i),
                _cell(_audio_kind(f)),
                _cell(_audio_tags(f)),
                _cell(_size(f), "right"),
            ])
        blocks += [InputRichBlockSectionHeading(text="🎵 Звук", size=4),
                   InputRichBlockTable(cells=rows, is_striped=True, is_compact=True)]

    chosen = selection(s)
    if chosen:
        _, _, label = chosen
        size = 0
        if sel["v"] is not None:
            size += fmt_bytes(full["video"][sel["v"]])
        if sel["a"] is not None and not (sel["v"] is not None and _muxed(full["video"][sel["v"]])):
            size += fmt_bytes(full["audio"][sel["a"]])
        summary = ["Выбрано: ", RichTextBold(text=label)]
        if size:
            summary.append(f" ≈ {format_filesize(size)}")
    else:
        summary = ["Тапни ID видео и ID звука — или что-то одно"]
    blocks.append(InputRichBlockParagraph(text=summary))

    def button(text: str, act: str, style: str | None = None) -> RichMessageButton:
        return RichMessageButton(
            text=text, style=style, callback_data=FullPickCallback(session=sid, act=act).pack(),
        )

    buttons = []
    if chosen:
        buttons.append(button("⬇️ Скачать", "go", "success"))
    if is_youtube(s["url"]):
        buttons.append(button(f"✂️ Реклама: {'вырезать' if sel['sb'] else 'оставить'}", "sb"))
    if chosen:
        buttons.append(button("✖ Сбросить", "clr"))
    if buttons:
        blocks.append(InputRichBlockButtons(buttons=buttons))

    blocks.append(InputRichBlockParagraph(
        text="Можно и текстом: пришли формат, например 315+251 или bestvideo+bestaudio",
    ))
    return InputRichMessage(blocks=blocks)
