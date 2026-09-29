"""Download media via yt-dlp and send files to Telegram via curl."""

import asyncio
import json
import time
from pathlib import Path

import yt_dlp
from aiogram.types import Message

from bot.config import API_URL, BOT_TOKEN, DOWNLOAD_DIR, PROGRESS_INTERVAL, log
from bot.cookies import cookie_file_for
from bot.formats import format_filesize


POSTPROCESSOR_STAGES = {
    "Merger": "🔄 Склеиваю видео и аудио…",
    "SponsorBlock": "✂️ Ищу рекламные вставки…",
    "ModifyChapters": "✂️ Вырезаю рекламу…",
    "FFmpegFixupM4a": "🔧 Исправляю контейнер…",
    "FFmpegFixupM3u8": "🔧 Исправляю контейнер…",
}

PROGRESS_BAR_WIDTH = 14


def _progress_bar(pct: float) -> str:
    filled = min(PROGRESS_BAR_WIDTH, int(pct / 100 * PROGRESS_BAR_WIDTH))
    return "▓" * filled + "░" * (PROGRESS_BAR_WIDTH - filled)


def _format_eta(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}:{secs:02d}"


def _progress_text(d: dict) -> str:
    # при video+audio yt-dlp качает дорожки по очереди, у каждой свои 0–100%
    info = d.get("info_dict") or {}
    if info.get("vcodec") not in (None, "none"):
        what = "видео"
    elif info.get("acodec") not in (None, "none"):
        what = "аудио"
    else:
        what = ""
    title = f"⬇️ Скачиваю {what}".rstrip()

    total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
    downloaded = d.get("downloaded_bytes") or 0
    if total:
        total = max(total, downloaded)  # total_bytes_estimate бывает меньше факта
    details = []
    if total:
        pct = min(100.0, downloaded / total * 100)
        title += f" — {pct:.0f}%"
        details.append(f"{format_filesize(downloaded)} / {format_filesize(total)}")
    else:
        details.append(format_filesize(downloaded))
    if d.get("speed"):
        details.append(f"{format_filesize(d['speed'])}/s")
    if d.get("eta"):
        details.append(f"осталось {_format_eta(d['eta'])}")

    lines = [title]
    if total:
        lines.append(_progress_bar(pct))
    lines.append(" · ".join(details))
    return "\n".join(lines)


def download_media(
    url: str,
    format_id: str,
    session_id: str,
    category: str,  # video_audio | video_only | audio_only | custom
    sponsorblock: bool,
    loop: asyncio.AbstractEventLoop,
    progress_msg: Message,
    user_id: int | None = None,
) -> Path:
    """Download media file (blocking). Returns path to downloaded file."""
    out_dir = DOWNLOAD_DIR / session_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_template = str(out_dir / "%(id)s.%(ext)s")

    # Между показом списка и скачиванием происходит повторная экстракция,
    # и выбранный id может исчезнуть (Instagram меняет id между запросами,
    # YouTube ротирует клиентов). Поэтому для кнопочных форматов добавляем
    # фолбэки на ближайшую альтернативу; ручной ввод — как есть.
    if format_id == "best":
        fmt = "bestvideo*+bestaudio/best"
    elif category == "custom":
        fmt = format_id
    elif category == "video_only":
        fmt = f"{format_id}+bestaudio/{format_id}/bestvideo*+bestaudio/best"
    elif category == "audio_only":
        fmt = f"{format_id}/bestaudio/best"
    else:  # video_audio
        fmt = f"{format_id}/bestvideo*+bestaudio/best"

    log.info("session %s: downloading %r via selector %r", session_id, format_id, fmt)

    last_update = [0.0]

    def show(text: str) -> None:
        asyncio.run_coroutine_threadsafe(safe_edit(progress_msg, text), loop)

    def progress_hook(d: dict) -> None:
        if d.get("status") != "downloading":
            return
        now = time.monotonic()
        if now - last_update[0] < PROGRESS_INTERVAL:
            return
        last_update[0] = now
        show(_progress_text(d))

    def postprocessor_hook(d: dict) -> None:
        # склейка/вырезание рекламы идут десятки секунд — без статуса
        # сообщение выглядит зависшим на последних процентах скачивания
        if d.get("status") != "started":
            return
        stage = POSTPROCESSOR_STAGES.get(d.get("postprocessor"))
        if stage:
            show(stage)

    ydl_opts = {
        "format": fmt,
        "outtmpl": out_template,
        "merge_output_format": "mp4" if category == "video_only" else None,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,  # консольный прогресс yt-dlp засорял логи контейнера
        "progress_hooks": [progress_hook],
        "postprocessor_hooks": [postprocessor_hook],
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

    with cookie_file_for(url, user_id) as cookie_path:
        if cookie_path:
            ydl_opts["cookiefile"] = cookie_path
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)

    # Find the downloaded file
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
    except Exception as e:
        # раньше ошибки глотались молча — и пропавший прогресс было не отладить
        if "message is not modified" not in str(e):
            log.warning("progress edit failed: %s: %s", type(e).__name__, e)


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

    # --form-string для текстовых полей: title, начинающийся с "@" или "<",
    # иначе трактуется curl как "загрузить из файла", а ";" режет значение на опции
    cmd = [
        "curl", "-s", "-X", "POST", url,
        "--form-string", f"chat_id={chat_id}",
        "-F", f"{field}=@{file_path}",
    ]
    if method == "sendVideo":
        cmd.extend(["--form-string", f"caption={title}", "--form-string", "supports_streaming=true"])
    elif method == "sendAudio":
        cmd.extend(["--form-string", f"title={title}"])
    else:
        cmd.extend(["--form-string", f"caption={title}"])

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
