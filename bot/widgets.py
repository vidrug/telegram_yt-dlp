"""Живая панель прокси/VPN прямо в сообщении — HTML-виджет Bot API 10.4 (/panel).

Только для OWNER_ID в личке (в проде виджеты и так работают только в личке с ботом,
сообщение защищено от пересылки). Виджет (bot/widget_panel.html) сам спрашивает бота
запросами widget_query (≤64 байт), бот отвечает answerWidgetQuery компактным JSON (≤1 КБ).
Каждый запрос начинается с версии протокола PROTO — код отправленного виджета неизменяем,
и старые сообщения после смены формата получают «пришли /panel заново», а не ломаются.

    o           обзор: плитки, кто в сети, проблемы (тексты собирает бот — экономим 4 КБ виджета)
    p           люди: прокси + VPN одним списком (имена склеены в панели)
    u:<имя>     карточка: состояние, трафик, графики 24 ч и 7 дней, ссылка для «копировать»
    t:1|7|30    общий трафик за 24 ч / 7 / 30 дней (прокси и VPN), кто сколько
    a           «когда»: лента активности каждого за сутки по получасам, как на /stats
    x           закрыть: бот удаляет это сообщение с виджетом (действие)
    sl:<имя>    прислать в чат ссылку прокси        (действие)
    sc:<имя>    прислать в чат .conf VPN             (действие)
    ad:p|ad:v   спросить имя нового пользователя     (действие, дальше — обычный /config-флоу)

Данные — с панели: /api/stats, /api/provision/list, /api/series (почасовые ряды из БД).
aiogram 3.31 ещё не знает 10.4: методы описаны здесь вручную, апдейт widget_query
перехватывает outer-middleware до роутеров, а в allowed_updates он добавлен в __main__.
Нужен локальный telegram-bot-api >= 10.4, иначе запросы виджета до бота не дойдут.
"""
import json
import math
import re
import time
from html import escape
from pathlib import Path

from aiogram import F
from aiogram.filters import Command
from aiogram.methods.base import TelegramMethod
from aiogram.types import Message

from bot import provision, status
from bot.config import OWNER_ID, log
from bot.state import bot, dp, router


def _minify(html: str) -> str:
    """Убрать отступы и лишние переводы строк (лимит Telegram — 4096 байт после zlib-9).
    Безопасно для нашего JS: перенос убирается только после ; { , ( > и перед + } ] ) , ."""
    html = re.sub(r"\n\s*", "\n", html)
    html = re.sub(r"\n(?=[+}\]),.])", "", html)
    return re.sub(r"([;{,(>])\n", r"\1", html)


HTML = _minify(Path(__file__).with_name("widget_panel.html").read_text("utf-8"))
CAPTION = "📊 Панель china — прокси и VPN. Обновляется сама, пока на экране."
PROTO = "3"
VS = {"online": 1, "recent": 2, "idle": 3, "never": 4}
RANGES = {"1": (1800, 48), "7": (14400, 42), "30": (86400, 30)}  # шаг бакета, число бакетов
SCALE = 94  # высота столбика в единицах viewBox: 100 минус зазор между прокси и VPN
TZ = 10800  # время в виджете — МСК, как в панели
STATUS_TEXT = {"ok": "всё в порядке", "warn": "есть предупреждения", "bad": "есть проблемы"}
MAX_ANSWER = 1024


class SendWidget(TelegramMethod[Message]):
    __returning__ = Message
    __api_method__ = "sendWidget"

    chat_id: int
    html: str
    caption: str | None = None
    parse_mode: str | None = None
    chat_data: str | None = None
    allow_queries: bool | None = None


class AnswerWidgetQuery(TelegramMethod[bool]):
    __returning__ = bool
    __api_method__ = "answerWidgetQuery"

    widget_query_id: str
    data: str | None = None


def _j(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _fits(obj) -> bool:
    return len(_j(obj).encode()) <= MAX_ANSWER


def _rb(n) -> str:
    """Байты по-русски — так же, как B() в виджете."""
    n, i = float(n or 0), 0
    while n >= 1024 and i < 4:
        n, i = n / 1024, i + 1
    unit = "БКМГТ"[i] + ("Б" if i else "")
    return f"{n:.0f} {unit}" if i == 0 or n >= 10 else f"{n:.1f} {unit}"


def _ago(s) -> str:
    if s is None:
        return "никогда"
    s = int(s)
    return ("только что" if s < 60 else f"{s // 60} мин" if s < 3600
            else f"{s // 3600} ч" if s < 86400 else f"{s // 86400} дн")


def _avatar(name: str) -> int:
    """Индекс цвета аватарки (7 цветов как в Telegram) — тот же хеш, что был в виджете."""
    c = 0
    for ch in name:
        c = (c * 31 + ord(ch)) % 7
    return c


def _ps(u: dict | None) -> int:
    """Состояние прокси-пользователя: 0 нет, 1 в сети, 2 был <24ч, 3 давно, 4 ни разу, 5 отключён."""
    if not u:
        return 0
    if not u.get("enabled", True):
        return 5
    if status.live(u):
        return 1
    q = u.get("quiet_sec")
    return 4 if q is None else 2 if q < 86400 else 3


def _users(d: dict) -> dict:
    return (d.get("user_stats") or {}).get("users") or {}


def _peers(d: dict) -> dict:
    return {x["name"]: x for x in ((d.get("vpn") or {}).get("peers") or [])}


def _ts(d: dict) -> int:
    return int(d.get("generated_ts") or time.time())


def _overview(d: dict) -> dict:
    h = d.get("health") or {}
    p = d.get("proxy") or {}
    pi = d.get("proxy_info") or {}
    us, vpn = _users(d), d.get("vpn") or {}
    mask = d.get("masking") or {}
    sni = (h.get("trend") or {}).get("unknown_sni") or {}
    dom = h.get("domain") or {}
    pn = sorted(n for n, u in us.items() if status.live(u))
    vn = [x["name"] for x in vpn.get("peers") or [] if x.get("online")]
    on = sorted(set(pn) | set(vn))
    st = h.get("status", "na")
    o = {"t": _ts(d), "s": st, "sl": STATUS_TEXT.get(st, "нет данных"),
         "k": [["📡 ПРОКСИ В СЕТИ", f"{len(pn)} из {len(us)}"],
               ["🔐 VPN В СЕТИ", f"{len(vn)} из {vpn.get('peers_total', 0)}"],
               ["🔌 СОЕДИНЕНИЙ", f"{p.get('active', 0)} / {p.get('max', 0)}"],
               ["🕵️ СКАНЕРЫ / ЧАС", f"{sni.get('1h', '?')} · сутки {sni.get('24h', '?')}"],
               ["🎭 МАСКИРОВКА · СЕРТ", f"{'ok' if mask.get('healthy') else '⚠️ СБОЙ'} · {(mask.get('cert') or {}).get('days_left', '?')} дн"],
               ["🖥 CPU · RAM", f"{round(d.get('cpu') or 0)}% · {round(d.get('mem_pct') or 0)}%"]],
         "n": ("сейчас в сети: " + ", ".join(on)) if on else "сейчас никого в сети",
         "pb": [[str(c.get("label")), c.get("state"), str(c.get("value"))[:48]]
                for c in h.get("checks") or [] if c.get("state") in ("warn", "bad")][:6],
         "al": list((h.get("alerts") or {}).get("firing") or []),
         "ft": f"{dom.get('name') or ''} → {', '.join(dom.get('resolved_ips') or ['?'])} ({h.get('mode') or '?'}) · "
               f"v{d.get('proxy_version') or '?'} · " + (f"аптайм {pi.get('uptime')}" if pi.get("online") else "ПРОКСИ НЕ ЗАПУЩЕН")}
    if not o["pb"]:
        o["pb"] = [["все проверки в норме", "ok", ""]]
    while not _fits(o) and len(o["pb"]) > 1:
        o["pb"].pop()
    return o


def _since(state: int, sec) -> str:
    """«в сети» / «18 мин назад» / «не подключался» / «отключён» — одна формулировка для списка и карточки."""
    if state == 1:
        return "в сети"
    if state == 5:
        return "⛔ отключён"
    if sec is None:
        return "не подключался"
    return _ago(sec) + ("" if int(sec) < 60 else " назад")


async def _people(d: dict) -> dict:
    us, peers = _users(d), _peers(d)
    try:
        s = await _series(1800, 48)
    except Exception as e:
        log.warning("widget: series for people: %r", e)
        s = {"proxy": {}, "vpn": {}}
    rows = []
    for n in set(us) | set(peers):
        u, v = us.get(n), peers.get(n)
        ps, vs = _ps(u), (VS.get(v.get("status"), 3) if v else 0)
        parts = []
        if u:
            parts.append("📡 " + _since(ps, u.get("quiet_sec")))
        if v:
            parts.append("🔐 " + _since(vs, v.get("last_handshake_ago")))
        day = sum(s["proxy"].get(n) or []) + sum(s["vpn"].get(n) or [])
        rank = min(ps or 9, vs or 9)
        rows.append([n, 1 if rank == 1 else 0, " · ".join(parts), _rb(day) if day else "", _avatar(n), rank])
    rows.sort(key=lambda r: (r[5], r[0]))
    out = {"t": _ts(d), "u": [r[:5] for r in rows]}
    while not _fits(out) and out["u"]:
        out["u"].pop()
    return out


async def _series(step: int, n: int) -> dict:
    return await provision._get(f"/api/series?step={step}&n={n}")


def _sum(lists, n: int) -> list[int]:
    lists = list(lists)
    return [sum(col) for col in zip(*lists)] if lists else [0] * n


def _chart(p: list[int], v: list[int], first: int, step: int) -> dict:
    """Столбики «прокси + VPN» в масштабе 0..99 от максимума суммы (мелкое не пропадает — минимум 1)."""
    mx = max((a + b for a, b in zip(p, v)), default=0)

    def sc(xs):
        return [0 if not x else max(1, round(x * SCALE / mx)) for x in xs] if mx else [0] * len(xs)
    g = {"p": sc(p), "mx": mx, "f": first, "s": step}
    if any(v):
        g["v"] = sc(v)
    return g


async def _traffic(r: str) -> dict:
    r = r if r in RANGES else "1"
    step, n = RANGES[r]
    s = await _series(step, n)
    tp, tv = _sum(s["proxy"].values(), n), _sum(s["vpn"].values(), n)
    per = {}
    for k, xs in s["proxy"].items():
        per.setdefault(k, [0, 0])[0] += sum(xs)
    for k, xs in s["vpn"].items():
        per.setdefault(k, [0, 0])[1] += sum(xs)
    top = sorted(([k, a, b] for k, (a, b) in per.items() if a + b), key=lambda x: -(x[1] + x[2]))[:8]
    m = (top[0][1] + top[0][2]) if top else 1
    out = {"t": s["generated_ts"], "r": r, "g": _chart(tp, tv, s["first"], step),
           "tot": _rb(sum(tp) + sum(tv)), "sp": f"📡 прокси {_rb(sum(tp))} · 🔐 VPN {_rb(sum(tv))}",
           "u": [[k, round(a * 100 / m, 1), round(b * 100 / m, 1), _rb(a + b)] for k, a, b in top]}
    while not _fits(out) and out["u"]:
        out["u"].pop()
    return out


async def _activity(d: dict) -> dict:
    """Лента «когда»: 48 получасов, яркость 1..9 от максимума самого человека (как полосы на /stats)."""
    s = await _series(1800, 48)
    rows = []
    for name in set(s["proxy"]) | set(s["vpn"]):
        p, v = s["proxy"].get(name) or [0] * 48, s["vpn"].get(name) or [0] * 48
        xs = [a + b for a, b in zip(p, v)]
        tot = sum(xs)
        if not tot:
            continue
        mx = max(xs)
        lv = "".join("0" if not x else str(min(9, max(1, math.ceil(9 * math.sqrt(x / mx))))) for x in xs)
        rows.append([name, lv, tot, (1 if any(p) else 0) | (2 if any(v) else 0)])
    rows.sort(key=lambda x: -x[2])
    for x in rows:
        x[2] = _rb(x[2])
    known = set(_users(d)) | set(_peers(d))
    quiet = len(known - {x[0] for x in rows})
    out = {"t": s["generated_ts"], "f": s["first"], "u": rows, "hd": "трафик за сутки по получасам: ярче — больше"}
    while not _fits(out) and out["u"]:
        out["u"].pop()
        quiet += 1
    out["q"] = f"тихо: {quiet}" if quiet else ""
    return out


async def _person(name: str, d: dict) -> dict:
    us, peers = _users(d), _peers(d)
    u, v = us.get(name), peers.get(name)
    if not u and not v:
        return {"err": f"не знаю «{name}»"}
    try:
        prov = await provision._get("/api/provision/list")
    except Exception as e:  # без ссылки/флага конфига карточка всё равно полезна
        log.warning("widget: provision list: %r", e)
        prov = {}
    pl = next((x for x in prov.get("proxy", []) if x["name"] == name), None)
    vc = next((x for x in prov.get("vpn", []) if x["name"] == name), None)
    out = {"t": _ts(d), "n": name, "ci": _avatar(name),
           "p": {"st": "нет доступа"}, "v": {"st": "нет ключа"}}
    ps = _ps(u)
    vs = VS.get(v.get("status"), 3) if v else 0
    out["on"] = 1 if 1 in (ps, vs) else 0
    if u:
        q, ss = u.get("quiet_sec"), u.get("sessions") or 0
        r = u.get("ratio_7d") if u.get("ratio_7d") is not None else u.get("ratio_24h")
        verdict = {"ok": "здоровое", "mid": "нормальное", "bad": "⚠️ плохое", "idle": "мало данных"}.get(u.get("ratio_state"), "?")
        out["p"] = {"st": _since(ps, q) + (f" · {ss} сес." if ps == 1 and ss else ""),
                    "m": f"сутки ↓{_rb(u.get('down_24h'))} ↑{_rb(u.get('up_24h'))}\n"
                         f"неделя ↓{_rb(u.get('down_7d'))} ↑{_rb(u.get('up_7d'))}\n"
                         f"вниз/вверх {r if r is not None else '—'}, {verdict}"}
    if v:
        out["v"] = {"st": _since(vs, v.get("last_handshake_ago")), "ip": v.get("ip"),
                    "m": f"{v.get('ip')} · с рестарта\n↓{_rb(v.get('tx'))} ↑{_rb(v.get('rx'))}"}
        out["cf"] = 1 if (vc or {}).get("has_config") else 0
    if out["on"]:
        out["sub"] = "в сети через " + " и ".join(x for x, st in (("прокси", ps), ("VPN", vs)) if st == 1)
    else:
        seen = [x for x in ((u or {}).get("quiet_sec"), v.get("last_handshake_ago") if v else None) if x is not None]
        out["sub"] = f"был {_ago(min(seen))} назад" if seen else "ещё не подключался"
    try:
        for key, (step, n) in (("h", (1800, 48)), ("w", (86400, 7))):
            s = await _series(step, n)
            out[key] = _chart(s["proxy"].get(name) or [0] * n, s["vpn"].get(name) or [0] * n, s["first"], step)
    except Exception as e:
        log.warning("widget: series for %s: %r", name, e)
    if pl and pl.get("enabled", True) and pl.get("tme_link") and len(pl["tme_link"]) <= 256:
        out["l"] = pl["tme_link"]  # уходит только в этот виджет у владельца — для кнопки «копировать»
    for key in ("h", "w"):        # не влезло в 1 КБ — жертвуем графиками, а не ссылкой
        if not _fits(out):
            out.pop(key, None)
    return out


async def _answer(qid: str, obj: dict) -> None:
    if "t" in obj:
        obj["tm"] = time.strftime("%H:%M", time.gmtime(int(obj["t"]) + TZ))
    s = _j(obj)
    if len(s.encode()) > MAX_ANSWER:
        log.warning("widget answer too large: %d bytes", len(s.encode()))
        s = _j({"err": "ответ не влез в 1 КБ"})
    try:
        await bot(AnswerWidgetQuery(widget_query_id=qid, data=s))
    except Exception as e:
        log.warning("answerWidgetQuery failed: %r", e)


async def _on_query(wq: dict) -> None:
    qid = wq.get("id")
    if not qid:
        return
    uid = (wq.get("from") or {}).get("id")
    chat = wq.get("chat") or {}
    data = str(wq.get("data") or "")
    if not OWNER_ID or uid != OWNER_ID or chat.get("type") != "private":
        return await _answer(qid, {"err": "нет доступа"})
    if not data.startswith(PROTO):
        return await _answer(qid, {"err": "виджет устарел — пришли /panel заново"})
    cmd, _, arg = data[len(PROTO):].partition(":")
    try:
        if cmd in ("o", "p", "u", "a"):
            d = await status._panel()
            out = (_overview(d) if cmd == "o" else await _people(d) if cmd == "p"
                   else await _activity(d) if cmd == "a" else await _person(arg, d))
            return await _answer(qid, out)
        if cmd == "t":
            return await _answer(qid, await _traffic(arg))
        if cmd == "x":
            await _answer(qid, {"ok": "закрываю…"})
            try:
                await bot.delete_message(chat["id"], wq["message_id"])
            except Exception as e:
                log.warning("widget close failed: %r", e)
            return
        if cmd in ("sl", "sc", "ad"):
            # сначала ответить виджету (15 с на ответ), потом уже слать сообщение в чат
            await _answer(qid, {"ok": {"sl": "ссылка — в чате ↓", "sc": "конфиг — в чате ↓",
                                       "ad": "имя — сообщением в чат ↓"}[cmd]})
            try:
                if cmd == "sl":
                    await provision.send_proxy_link(chat["id"], arg)
                elif cmd == "sc":
                    await provision.send_vpn_config(chat["id"], arg)
                else:
                    await provision.ask_new_name(chat["id"], uid, {"p": "proxy", "v": "vpn"}[arg])
            except Exception as e:
                log.warning("widget action %s failed: %r", cmd, e)
                await bot.send_message(chat["id"], f"⚠️ не получилось: {escape(str(e))[:200]}")
            return
        await _answer(qid, {"err": "неизвестный запрос"})
    except Exception as e:
        log.warning("widget query %r failed: %r", data[:16], e)
        await _answer(qid, {"err": f"панель недоступна: {e}"[:200]})


@dp.update.outer_middleware()
async def _widget_updates(handler, update, data):
    """widget_query для aiogram 3.31 — неизвестный тип апдейта: обрабатываем его здесь, до роутеров."""
    wq = (update.model_extra or {}).get("widget_query")
    # Наши запросы всегда начинаются с цифры (PROTO). Остальные — виджета скачивания (bot/widget.py),
    # их не трогаем: тогда порядок двух middleware в __main__ перестаёт иметь значение.
    if wq is None or not str(wq.get("data") or "")[:1].isdigit():
        return await handler(update, data)
    await _on_query(wq)
    return None


@router.message(Command("panel"), F.chat.type == "private", F.from_user.id == OWNER_ID)
async def cmd_panel(m: Message) -> None:
    if not status._is_owner(m):
        return
    first = None
    try:
        first = _j(_overview(await status._panel()))  # первая отрисовка — сразу, без запроса
    except Exception as e:
        log.warning("panel: no initial data: %r", e)
    try:
        await bot(SendWidget(chat_id=m.chat.id, html=HTML, caption=CAPTION, allow_queries=True,
                             chat_data=first if first and len(first.encode()) <= MAX_ANSWER else None))
    except Exception as e:
        log.warning("sendWidget failed: %r", e)
        await m.answer(f"⚠️ виджет не отправился: {escape(str(e))[:200]}\nТекстом: /status")
