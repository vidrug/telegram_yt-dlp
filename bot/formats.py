"""Format helpers: classification, labels, keyboard building, raw table."""

import yt_dlp
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.callbacks import (
    CancelCallback,
    FormatCallback,
    PageCallback,
    RawFormatsCallback,
)
from bot.config import FORMATS_PER_PAGE


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
    # Formats with no codec info but with a video extension (e.g. Instagram)
    ext = f.get("ext", "")
    if ext in ("mp4", "webm", "mkv", "mov", "avi", "flv"):
        return "video_audio"
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

    # "Best quality" button on first page only
    if page == 0:
        rows.append([InlineKeyboardButton(
            text="⭐ Лучшее качество (bestvideo+bestaudio)",
            callback_data=FormatCallback(
                session=session_id, fmt="bestvideo+bestaudio",
            ).pack(),
        )])

    for kind, item in page_items:
        if kind == "section":
            rows.append([InlineKeyboardButton(text=f"— {item} —", callback_data="noop")])
        else:
            label = format_button_label(item)
            cb = FormatCallback(session=session_id, fmt=item["format_id"])
            rows.append([InlineKeyboardButton(text=label, callback_data=cb.pack())])

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
