"""Cleanup routines for sessions, download dirs, and web files."""

import asyncio
import shutil
import time

from bot.config import DOWNLOAD_DIR, SESSION_TTL, WEB_FILE_TTL, log
from bot.state import sessions, web_files


def cleanup_session_files(session_id: str) -> None:
    d = DOWNLOAD_DIR / session_id
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)


async def periodic_cleanup() -> None:
    """Remove orphaned download dirs and expired web files."""
    while True:
        await asyncio.sleep(600)
        try:
            now = time.time()
            # Clean expired web files (8 hours)
            expired_web = [
                sid for sid, e in web_files.items()
                if now - e["created"] > WEB_FILE_TTL
            ]
            for sid in expired_web:
                web_files.pop(sid, None)
                cleanup_session_files(sid)
                log.info("Cleaned up expired web file: %s", sid)

            # Clean orphaned dirs (8 hours, skip active web files)
            for d in DOWNLOAD_DIR.iterdir():
                if d.is_dir() and d.name not in web_files:
                    if now - d.stat().st_mtime > WEB_FILE_TTL:
                        shutil.rmtree(d, ignore_errors=True)
                        sessions.pop(d.name, None)
                        log.info("Cleaned up orphaned dir: %s", d.name)
        except Exception as e:
            log.error("periodic_cleanup error: %s", e)


async def session_cleanup() -> None:
    """Remove expired sessions every 5 minutes."""
    while True:
        await asyncio.sleep(300)
        now = time.time()
        expired = []
        for sid, s in sessions.items():
            # Sessions with .part files (awaiting retry) live up to 8 hours
            has_parts = any(
                f.name.endswith(".part")
                for f in (DOWNLOAD_DIR / sid).iterdir()
            ) if (DOWNLOAD_DIR / sid).exists() else False
            ttl = WEB_FILE_TTL if has_parts else SESSION_TTL
            if now - s["created"] > ttl:
                expired.append(sid)
        for sid in expired:
            sessions.pop(sid, None)
            cleanup_session_files(sid)
        if expired:
            log.info("Cleaned up %d expired sessions", len(expired))
