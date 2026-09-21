"""Cookie handling for yt-dlp.

Две проблемы, которые решает этот модуль:

1. yt-dlp сохраняет cookie jar обратно в cookiefile после каждого запуска,
   то есть дописывает в примонтированный пользовательский cookies.txt свежие
   куки. Поэтому каждый запуск работает с временной копией — оригинал
   остаётся ровно таким, каким его положил пользователь.

2. Анонимные visitor-куки YouTube (VISITOR_INFO1_LIVE, YSC, GPS и т.п.)
   привязаны к IP, на котором были получены. При использовании с другого IP
   YouTube отдаёт "HTTP Error 403: Forbidden" на медиа-URL. Аккаунтной
   сессии они не дают, поэтому для YouTube ходим вообще без cookies.
"""

import os
import shutil
import tempfile
from contextlib import contextmanager
from typing import Iterator
from urllib.parse import urlparse

from bot.config import COOKIES_FILE, log

COOKIE_FREE_HOSTS = ("youtube.com", "youtu.be", "youtube-nocookie.com")


def _is_cookie_free(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in COOKIE_FREE_HOSTS)


@contextmanager
def cookie_file_for(url: str) -> Iterator[str | None]:
    """Yield a throwaway cookie file path for this URL, or None if not used."""
    if COOKIES_FILE is None or _is_cookie_free(url):
        yield None
        return

    fd, tmp_path = tempfile.mkstemp(prefix="cookies-", suffix=".txt")
    os.close(fd)
    try:
        shutil.copyfile(COOKIES_FILE, tmp_path)
        yield tmp_path
    except OSError as e:
        log.warning("cannot prepare cookie copy (%s), continuing without cookies", e)
        yield None
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
