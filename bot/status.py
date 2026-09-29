"""Команды владельца: /status, /stats [user], /vpn — сводка с панели прокси (только для OWNER_ID).

Данные берутся из /api/stats панели (PANEL_URL); ничего не пишет, секреты не показывает.
"""
import asyncio
import time
from html import escape

import aiohttp
from aiogram import F
from aiogram.filters import Command, CommandObject
from aiogram.types import BotCommand, BotCommandScopeChat, Message

from bot.config import OWNER_ID, PANEL_URL, log
from bot.state import bot, router

ICON = {"ok": "🟢", "warn": "🟠", "bad": "🔴", "na": "⚪"}
_cache = {"ts": 0.0, "data": None}


def _is_owner(m: Message) -> bool:
    """Второй рубеж (первый — фильтр F.from_user.id == OWNER_ID в декораторах): только владелец, только личка.
    Для всех остальных этих команд не существует — сообщение уходит в обычные хендлеры скачивания."""
    return bool(OWNER_ID) and m.from_user is not None and m.from_user.id == OWNER_ID and m.chat.type == "private"


async def _panel() -> dict:
    """GET /api/stats с кэшем 5 с; при недоступности — исключение с понятным текстом."""
    if time.time() - _cache["ts"] < 5 and _cache["data"] is not None:
        return _cache["data"]
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        async with s.get(f"{PANEL_URL}/api/stats") as r:
            if r.status != 200:
                raise RuntimeError(f"панель ответила HTTP {r.status}")
            data = await r.json()
    _cache.update(ts=time.time(), data=data)
    return data


def _b(n) -> str:
    n = float(n or 0)
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"


def _ago(sec) -> str:
    if sec is None:
        return "никогда"
    sec = int(sec)
    if sec < 60:
        return "только что"
    if sec < 3600:
        return f"{sec // 60} мин назад"
    if sec < 86400:
        return f"{sec // 3600} ч назад"
    return f"{sec // 86400} дн назад"


def _fmt_status(d: dict) -> str:
    h = d.get("health") or {}
    p = d.get("proxy") or {}
    pi = d.get("proxy_info") or {}
    us = (d.get("user_stats") or {}).get("users") or {}
    vpn = d.get("vpn") or {}
    mask = d.get("masking") or {}
    st = h.get("status", "na")
    lines = [f"{ICON.get(st, '⚪')} <b>china</b> · v{escape(str(d.get('proxy_version') or '?'))} · "
             f"{'online' if pi.get('online') else 'OFFLINE'} {escape(str(pi.get('uptime') or ''))}"]
    bad = [c for c in (h.get("checks") or []) if c.get("state") in ("warn", "bad")]
    if bad:
        lines.append("")
        for c in bad:
            lines.append(f"{ICON[c['state']]} {escape(str(c.get('label')))}: {escape(str(c.get('value')))}")
    else:
        lines.append("все проверки в норме")
    lines.append("")
    online = sorted(n for n, v in us.items() if v.get("online"))
    lines.append(f"👥 прокси: {len(online)} онлайн из {len(us)}" + (f" — {', '.join(escape(n) for n in online)}" if online else ""))
    lines.append(f"📊 соединений {p.get('active', 0)}/{p.get('max', 0)} · сканеры {((h.get('trend') or {}).get('unknown_sni') or {}).get('1h', '?')}/ч")
    peers = vpn.get("peers") or []
    von = [x["name"] for x in peers if x.get("online")]
    lines.append(f"🔐 VPN: {vpn.get('peers_online', 0)} онлайн из {vpn.get('peers_total', 0)}" + (f" — {', '.join(escape(n) for n in von)}" if von else ""))
    cert = (mask.get("cert") or {}).get("days_left")
    lines.append(f"🧰 маскировка {'ok' if mask.get('healthy') else 'ДЕГРАДАЦИЯ'} · сертификат {cert if cert is not None else '?'} дн")
    dom = h.get("domain") or {}
    lines.append(f"🌐 {escape(str(dom.get('name') or 'домен'))} → "
                 f"{', '.join(escape(i) for i in (dom.get('resolved_ips') or ['?']))} ({escape(str(h.get('mode') or '?'))})")
    al = (h.get("alerts") or {})
    if al.get("firing"):
        lines.append(f"🔔 активные алерты: {escape(', '.join(al['firing']))}")
    return "\n".join(lines)


def _fmt_user(name: str, d: dict) -> str:
    us = (d.get("user_stats") or {}).get("users") or {}
    u = us.get(name)
    peers = {x["name"]: x for x in ((d.get("vpn") or {}).get("peers") or [])}
    v = peers.get(name)
    if not u and not v:
        known = ", ".join(sorted(set(us) | set(peers)))
        return f"не знаю пользователя <b>{escape(name)}</b>. Известные: {escape(known)}"
    lines = [f"<b>{escape(name)}</b>"]
    if u:
        state = "🟢 онлайн" if u.get("online") else ("⚪ " + _ago(u.get("quiet_sec")))
        lines.append(f"📡 прокси — {state}" + (f" ({u.get('sessions')} сессии)" if u.get("online") else ""))
        lines.append(f"24ч ↓{_b(u.get('down_24h'))} ↑{_b(u.get('up_24h'))} · 7д ↓{_b(u.get('down_7d'))} ↑{_b(u.get('up_7d'))}")
        r = u.get("ratio_7d") if u.get("ratio_7d") is not None else u.get("ratio_24h")
        rs = u.get("ratio_state")
        verdict = {"ok": "здоровое", "mid": "нормальное", "bad": "ПЛОХОЕ (как у мамы до починки)", "idle": "мало данных"}.get(rs, "?")
        lines.append(f"вниз/вверх {r if r is not None else '—'} — {verdict} · активных дней {u.get('active_days_14d', 0)}/14"
                     + (", ежедневный" if u.get("daily_user") else ""))
        if not u.get("enabled", True):
            lines.append("⛔ пользователь отключён")
    else:
        lines.append("📡 прокси: нет такого пользователя")
    if v:
        st = {"online": "🟢 онлайн", "recent": "🟡 недавно", "idle": "⚪ давно", "never": "⚪ ни разу"}.get(v.get("status"), "?")
        lines.append(f"🔐 VPN {v.get('ip')} — {st}, рукопожатие {_ago(v.get('last_handshake_ago'))}")
        lines.append(f"с рестарта ↓{_b(v.get('tx'))} ↑{_b(v.get('rx'))}" + (f" · оператор {escape(str(v.get('endpoint')))}" if v.get("endpoint") else ""))
    else:
        lines.append("🔐 VPN: ключа нет")
    return "\n".join(lines)


def _fmt_vpn(d: dict) -> str:
    vpn = d.get("vpn") or {}
    if not vpn.get("installed", False):
        return "VPN недоступен: " + escape(str(vpn.get("error") or "нет данных"))
    lines = [f"🔐 <b>VPN</b>: {vpn.get('peers_online', 0)} онлайн из {vpn.get('peers_total', 0)}, порт udp {vpn.get('listen_port')}"]
    for p in vpn.get("peers") or []:
        st = {"online": "🟢", "recent": "🟡", "idle": "⚪", "never": "⚫"}.get(p.get("status"), "?")
        lines.append(f"{st} {escape(p['name'])} — {_ago(p.get('last_handshake_ago'))}, ↓{_b(p.get('tx'))}")
    return "\n".join(lines)


async def _reply(m: Message, fn, *args):
    try:
        d = await _panel()
        text = fn(d, *args) if args else fn(d)
    except Exception as e:
        log.warning("status: panel error: %r", e)
        text = f"⚠️ панель недоступна: {escape(str(e))[:120]}"
    await m.answer(text[:4000], disable_web_page_preview=True)


@router.message(Command("status"), F.chat.type == "private", F.from_user.id == OWNER_ID)
async def cmd_status(m: Message) -> None:
    if not _is_owner(m):
        return
    await _reply(m, _fmt_status)


@router.message(Command("stats"), F.chat.type == "private", F.from_user.id == OWNER_ID)
async def cmd_stats(m: Message, command: CommandObject) -> None:
    if not _is_owner(m):
        return
    name = (command.args or "").strip()
    if not name:
        d = await _panel()
        us = (d.get("user_stats") or {}).get("users") or {}
        rows = []
        for n, u in sorted(us.items(), key=lambda kv: -(kv[1].get("down_24h") or 0)):
            st = "🟢" if u.get("online") else "⚪"
            rows.append(f"{st} {escape(n)}: ↓{_b(u.get('down_24h'))} за 24ч, {_ago(u.get('quiet_sec')) if not u.get('online') else 'онлайн'}")
        await m.answer("👥 <b>Прокси, 24ч</b>\n" + "\n".join(rows) + "\n\n<code>/stats имя</code> — подробно", disable_web_page_preview=True)
        return
    await _reply(m, lambda d: _fmt_user(name, d))


@router.message(Command("vpn"), F.chat.type == "private", F.from_user.id == OWNER_ID)
async def cmd_vpn(m: Message) -> None:
    if not _is_owner(m):
        return
    await _reply(m, _fmt_vpn)


async def register_owner_commands() -> None:
    """Меню команд только в чате владельца (остальные пользователи бота их не видят)."""
    if not OWNER_ID:
        return
    try:
        await bot.set_my_commands(
            [BotCommand(command="status", description="Здоровье прокси/VPN одной строкой"),
             BotCommand(command="stats", description="Трафик пользователей (или /stats имя)"),
             BotCommand(command="vpn", description="Пиры VPN"),
             BotCommand(command="config", description="Выдать ссылку прокси / конфиг VPN, добавить нового")],
            scope=BotCommandScopeChat(chat_id=OWNER_ID))
    except Exception as e:
        log.warning("set_my_commands failed: %r", e)
