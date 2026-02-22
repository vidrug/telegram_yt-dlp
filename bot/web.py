"""Web server for serving files > 2 GB via download links."""

import time
from urllib.parse import quote

from aiohttp import web

from bot.config import log
from bot.formats import format_filesize
from bot.state import web_files

CHUNK_SIZE = 64 * 1024 * 1024  # 64 MB


async def handle_download(request: web.Request) -> web.StreamResponse:
    """Serve a file for download with Range support."""
    sid = request.match_info["session_id"]
    entry = web_files.get(sid)
    if not entry:
        raise web.HTTPNotFound(text="File not found or link expired.")

    file_path = entry["path"]
    if not file_path.exists():
        web_files.pop(sid, None)
        raise web.HTTPNotFound(text="File not found or link expired.")

    filename = entry["filename"]
    file_size = file_path.stat().st_size
    encoded = quote(filename)
    disposition = f"attachment; filename*=UTF-8''{encoded}"

    # Parse Range header
    range_header = request.headers.get("Range")
    start = 0
    end = file_size - 1

    if range_header and range_header.startswith("bytes="):
        range_spec = range_header[6:]
        parts = range_spec.split("-", 1)
        if parts[0]:
            start = int(parts[0])
        if parts[1]:
            end = int(parts[1])
        end = min(end, file_size - 1)

        if start >= file_size or start > end:
            return web.Response(
                status=416,
                headers={"Content-Range": f"bytes */{file_size}"},
            )

        response = web.StreamResponse(status=206)
        response.headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
    else:
        response = web.StreamResponse(status=200)

    total_to_send = end - start + 1
    log.info(
        "Serving %s (%s) to %s",
        filename, format_filesize(total_to_send), request.remote,
    )

    response.content_type = "application/octet-stream"
    response.headers["Content-Disposition"] = disposition
    response.headers["Accept-Ranges"] = "bytes"
    response.content_length = total_to_send
    await response.prepare(request)

    sent = 0
    t_start = time.monotonic()
    last_log = t_start

    with open(file_path, "rb") as f:
        f.seek(start)
        remaining = total_to_send
        while remaining > 0:
            chunk = f.read(min(CHUNK_SIZE, remaining))
            if not chunk:
                break
            await response.write(chunk)
            sent += len(chunk)
            remaining -= len(chunk)

            now = time.monotonic()
            if now - last_log >= 5:
                elapsed = now - t_start
                speed = sent / elapsed if elapsed > 0 else 0
                pct = sent / total_to_send * 100
                log.info(
                    "Serving %s: %s/%s (%.1f%%) | %s/s",
                    filename, format_filesize(sent), format_filesize(total_to_send),
                    pct, format_filesize(speed),
                )
                last_log = now

    elapsed = time.monotonic() - t_start
    speed = sent / elapsed if elapsed > 0 else 0
    log.info(
        "Done serving %s: %s in %.1fs | avg %s/s",
        filename, format_filesize(sent), elapsed, format_filesize(speed),
    )

    return response


def create_web_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/dl/{session_id}{ext:.*}", handle_download)
    return app
