"""Cookie handling for yt-dlp.

Проблемы, которые решает этот модуль:

1. yt-dlp сохраняет cookie jar обратно в cookiefile после каждого запуска,
   то есть дописывает в исходный cookies.txt свежие куки. Поэтому каждый
   запуск работает с временной копией — оригинал остаётся как был.

2. Анонимные visitor-куки YouTube (VISITOR_INFO1_LIVE, YSC, GPS и т.п.)
   привязаны к IP, на котором были получены. При использовании с другого IP
   YouTube отдаёт "HTTP Error 403: Forbidden" на медиа-URL. Поэтому для
   YouTube куки передаются, только если в файле есть аккаунтные (SID и т.п.).

3. С датацентрового IP YouTube требует "Sign in to confirm you're not a bot".
   Анонимно это не обойти — бот просит пользователя прислать свои куки.
   Они хранятся отдельно для каждого пользователя и используются только
   для его запросов; общий COOKIES_FILE — запасной вариант.
"""

import os
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from urllib.parse import urlparse

from bot.config import COOKIES_FILE, SHARED_DIR, log

USER_COOKIES_DIR = SHARED_DIR / "cookies"
MAX_COOKIE_FILE_SIZE = 1024 * 1024  # 1 MB

YOUTUBE_HOSTS = ("youtube.com", "youtu.be", "youtube-nocookie.com")

# Куки залогиненной сессии Google
YOUTUBE_ACCOUNT_COOKIES = {"SID", "__Secure-1PSID", "__Secure-3PSID", "SAPISID", "LOGIN_INFO"}


def _cookie_rows(text: str) -> list[list[str]]:
    """Parse Netscape cookie file lines into [domain, ..., name, value] rows."""
    rows = []
    for line in text.splitlines():
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        elif line.startswith("#") or not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            # при копировании через буфер обмена табы часто превращаются в пробелы
            parts = line.split(None, 6)
        if len(parts) >= 7:
            rows.append([p.strip() for p in parts])
    return rows


def normalize_cookie_text(text: str) -> str:
    """Rewrite cookies as a clean tab-separated Netscape file."""
    lines = ["# Netscape HTTP Cookie File"]
    for text_line in text.splitlines():
        http_only = text_line.startswith("#HttpOnly_")
        for r in _cookie_rows(text_line):
            domain = ("#HttpOnly_" if http_only else "") + r[0]
            lines.append("\t".join([domain] + r[1:7]))
    return "\n".join(lines) + "\n"


def _has_youtube_account_cookies(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return any(
        "youtube.com" in r[0] and r[5] in YOUTUBE_ACCOUNT_COOKIES
        for r in _cookie_rows(text)
    )


def _is_youtube(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in YOUTUBE_HOSTS)


def user_cookie_path(user_id: int) -> Path:
    return USER_COOKIES_DIR / f"{user_id}.txt"


def _source_for(url: str, user_id: int | None) -> Path | None:
    candidates = []
    if user_id is not None:
        candidates.append(user_cookie_path(user_id))
    if COOKIES_FILE is not None:
        candidates.append(COOKIES_FILE)
    for path in candidates:
        if not path.exists():
            continue
        if _is_youtube(url) and not _has_youtube_account_cookies(path):
            continue
        return path
    return None


@contextmanager
def cookie_file_for(url: str, user_id: int | None = None) -> Iterator[str | None]:
    """Yield a throwaway cookie file path for this URL, or None if not used."""
    source = _source_for(url, user_id)
    if source is None:
        yield None
        return

    fd, tmp_path = tempfile.mkstemp(prefix="cookies-", suffix=".txt")
    os.close(fd)
    try:
        shutil.copyfile(source, tmp_path)
    except OSError as e:
        log.warning("cannot prepare cookie copy (%s), continuing without cookies", e)
        os.unlink(tmp_path)
        yield None
        return
    try:
        yield tmp_path
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Cookies, присланные пользователем
# ---------------------------------------------------------------------------

def needs_cookies(error: Exception | str) -> bool:
    """True, если yt-dlp отказал из-за отсутствия авторизации."""
    text = str(error)
    return "Sign in to confirm" in text or "--cookies" in text


def validate_cookie_file(text: str) -> str | None:
    """Return an error message for the user, or None if the file looks usable."""
    rows = _cookie_rows(text)
    if not rows:
        return "Это не похоже на cookies.txt (нужен формат Netscape — его выдаёт расширение)."
    if not any("youtube.com" in r[0] for r in rows):
        return "В файле нет куки youtube.com — экспортируй их, находясь на youtube.com."
    if not any("youtube.com" in r[0] and r[5] in YOUTUBE_ACCOUNT_COOKIES for r in rows):
        return (
            "В файле только анонимные куки YouTube — похоже, ты не был залогинен. "
            "Войди в аккаунт на youtube.com и экспортируй заново."
        )
    return None


def save_user_cookies(user_id: int, text: str) -> None:
    USER_COOKIES_DIR.mkdir(parents=True, exist_ok=True)
    path = user_cookie_path(user_id)
    path.write_text(normalize_cookie_text(text), encoding="utf-8")
    os.chmod(path, 0o600)


def delete_user_cookies(user_id: int) -> bool:
    try:
        user_cookie_path(user_id).unlink()
        return True
    except FileNotFoundError:
        return False


COOKIES_INSTRUCTION = (
    "🍪 <b>YouTube требует войти в аккаунт</b>\n"
    "С сервера бота YouTube пускает только с куками залогиненного аккаунта. "
    "Пришли их один раз — дальше всё будет работать.\n\n"
    "<b>Как достать куки (≈5 минут, с компьютера):</b>\n"
    "1. Лучше используй <b>запасной</b> Google-аккаунт — YouTube может ограничить "
    "аккаунт, чьи куки используются на сервере.\n"
    "2. Установи расширение <b>Get cookies.txt LOCALLY</b> "
    "(есть для Chrome и Firefox).\n"
    "3. Открой <b>приватное окно</b> (инкогнито) и разреши в нём расширение "
    "(в настройках расширения: «Разрешить в режиме инкогнито»).\n"
    "4. В этом окне зайди на youtube.com и войди в аккаунт.\n"
    "5. Нажми на значок расширения → <b>Export</b> — скачается файл cookies.txt.\n"
    "6. <b>Сразу закрой приватное окно, не выходя из аккаунта</b> — "
    "иначе куки станут недействительными.\n"
    "7. Пришли этот файл сюда <b>документом</b> (скрепка → файл) "
    "или просто вставь его содержимое сообщением.\n\n"
    "Куки используются только для твоих запросов, сообщение с файлом я сразу удалю. "
    "Удалить сохранённые куки: /forgetcookies"
)
