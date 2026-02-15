"""Download media via yt-dlp and send files to Telegram via curl."""

import asyncio
import json
import time
from pathlib import Path

import yt_dlp
from aiogram.types import Message

from bot.config import API_URL, BOT_TOKEN, DOWNLOAD_DIR, PROGRESS_INTERVAL, log
from bot.formats import format_filesize


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
