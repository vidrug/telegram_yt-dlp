"""Выдача и создание доступов из бота: /config — ссылки прокси и .conf файлы VPN.

Только для OWNER_ID в личке. Вся работа с ключами — на стороне панели (PANEL_URL);
бот лишь показывает кнопки и отдаёт результат. Мутации защищены токеном PANEL_TOKEN.
"""
import asyncio
from html import escape

import aiohttp
from aiogram import F
from aiogram.filters import Command
from aiogram.types import (BufferedInputFile, CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

from bot.config import OWNER_ID, PANEL_TOKEN, PANEL_URL, log
from bot.state import router

# user_id -> {"kind": "proxy"|"vpn"} — ждём следующим сообщением имя нового пользователя
_awaiting: dict[int, dict] = {}
DOT = {"online": "🟢", "recent": "🟡", "idle": "⚪", "never": "⚫"}


def _owner(obj) -> bool:
    u = getattr(obj, "from_user", None)
    return bool(OWNER_ID) and u is not None and u.id == OWNER_ID


async def _get(path: str) -> dict:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25)) as s:
        async with s.get(f"{PANEL_URL}{path}") as r:
            data = await r.json()
            if r.status != 200:
                raise RuntimeError(str(data.get("error") or f"HTTP {r.status}"))
            return data


async def _post(path: str, payload: dict) -> dict:
    headers = {"x-api-token": PANEL_TOKEN} if PANEL_TOKEN else {}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as s:
        async with s.post(f"{PANEL_URL}{path}", json=payload, headers=headers) as r:
            data = await r.json()
            if r.status != 200:
                raise RuntimeError(str(data.get("error") or f"HTTP {r.status}"))
            return data


def _menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📡 Прокси — ссылка", callback_data="cfg:proxy")],
        [InlineKeyboardButton(text="🔐 VPN — конфиг", callback_data="cfg:vpn")],
    ])


def _people_kb(items, kind: str) -> InlineKeyboardMarkup:
    rows, row = [], []
    for it in items:
        row.append(InlineKeyboardButton(text=it["label"], callback_data=f"cfg:{kind}:{it['name']}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="➕ Добавить нового", callback_data=f"cfg:new:{kind}")])
    rows.append([InlineKeyboardButton(text="‹ Назад", callback_data="cfg:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("config"), F.chat.type == "private", F.from_user.id == OWNER_ID)
async def cmd_config(m: Message) -> None:
    _awaiting.pop(m.from_user.id, None)
    await m.answer("Что выдать?", reply_markup=_menu())


@router.callback_query(F.data == "cfg:menu")
async def cb_menu(c: CallbackQuery) -> None:
    if not _owner(c):
        return await c.answer()
    _awaiting.pop(c.from_user.id, None)
    await c.message.edit_text("Что выдать?", reply_markup=_menu())
    await c.answer()


@router.callback_query(F.data == "cfg:proxy")
async def cb_proxy_list(c: CallbackQuery) -> None:
    if not _owner(c):
        return await c.answer()
    try:
        d = await _get("/api/provision/list")
    except Exception as e:
        return await c.answer(f"панель: {e}"[:180], show_alert=True)
    items = [{"name": x["name"], "label": ("" if x.get("enabled", True) else "⛔ ") + x["name"]}
             for x in d.get("proxy", [])]
    await c.message.edit_text(f"📡 Прокси — кому выдать ссылку? ({len(items)})", reply_markup=_people_kb(items, "proxy"))
    await c.answer()


@router.callback_query(F.data == "cfg:vpn")
async def cb_vpn_list(c: CallbackQuery) -> None:
    if not _owner(c):
        return await c.answer()
    try:
        d = await _get("/api/provision/list")
    except Exception as e:
        return await c.answer(f"панель: {e}"[:180], show_alert=True)
    items = [{"name": p["name"], "label": DOT.get(p.get("status"), "⚪") + " " + p["name"] + ("" if p["has_config"] else " ⚠")}
             for p in d.get("vpn", [])]
    nxt = d.get("vpn_next_ip") or "?"
    await c.message.edit_text(
        f"🔐 VPN — чей конфиг прислать? ({len(items)})\n⚠ — файла конфига нет на сервере\nследующий свободный адрес: {nxt}",
        reply_markup=_people_kb(items, "vpn"))
    await c.answer()


@router.callback_query(F.data.startswith("cfg:proxy:"))
async def cb_proxy_give(c: CallbackQuery) -> None:
    if not _owner(c):
        return await c.answer()
    name = c.data.split(":", 2)[2]
    try:
        d = await _get("/api/provision/list")
    except Exception as e:
        return await c.answer(f"панель: {e}"[:180], show_alert=True)
    it = next((x for x in d.get("proxy", []) if x["name"] == name), None)
    if not it or not it.get("tme_link"):
        return await c.answer("ссылки нет — пользователь отключён?", show_alert=True)
    await c.message.answer(
        f"📡 <b>{escape(name)}</b> — ссылка на прокси\n\n<code>{escape(it['tme_link'])}</code>\n\n"
        "Переслать ему целиком: тап по ссылке добавит и включит прокси.",
        disable_web_page_preview=True)
    await c.answer("готово")


@router.callback_query(F.data.startswith("cfg:vpn:"))
async def cb_vpn_give(c: CallbackQuery) -> None:
    if not _owner(c):
        return await c.answer()
    name = c.data.split(":", 2)[2]
    try:
        d = await _get(f"/api/provision/vpn/{name}")
    except Exception as e:
        return await c.answer(f"нет конфига: {e}"[:180], show_alert=True)
    await c.message.answer_document(
        BufferedInputFile(d["config"].encode("utf-8"), filename=f"{name}.conf"),
        caption=f"🔐 <b>{escape(name)}</b> — конфиг AmneziaWG\nИмпорт в приложении: «Добавить» → из файла.")
    await c.answer("готово")


@router.callback_query(F.data.startswith("cfg:new:"))
async def cb_new(c: CallbackQuery) -> None:
    if not _owner(c):
        return await c.answer()
    kind = c.data.split(":", 2)[2]
    _awaiting[c.from_user.id] = {"kind": kind}
    warn = ("\n\n⚠️ Создание перезапустит прокси — у всех на несколько секунд оборвётся связь."
            if kind == "proxy" else "\n\nVPN добавляется без перезапуска, никого не оборвёт.")
    await c.message.answer(
        f"Имя нового {'пользователя прокси' if kind == 'proxy' else 'пира VPN'}?"
        + ("\nЛатиница, цифры, _ и -" if kind == "proxy" else "\nМожно по-русски, до 32 символов")
        + warn + "\n\nОтправь имя сообщением или /cancel.")
    await c.answer()


@router.message(Command("cancel"), F.chat.type == "private", F.from_user.id == OWNER_ID)
async def cmd_cancel(m: Message) -> None:
    if _awaiting.pop(m.from_user.id, None):
        await m.answer("отменено")


@router.message(F.chat.type == "private", F.from_user.id == OWNER_ID, F.text,
                lambda m: m.from_user.id in _awaiting and not (m.text or "").startswith("/"))
async def on_new_name(m: Message) -> None:
    st = _awaiting.pop(m.from_user.id, None)
    if not st:
        return
    name = (m.text or "").strip()
    kind = st["kind"]
    note = await m.answer("создаю…" + (" прокси перезапустится" if kind == "proxy" else ""))
    try:
        if kind == "proxy":
            d = await _post("/api/provision/proxy/add", {"name": name})
            link = d.get("tme_link") or ""
            await note.edit_text(
                f"✅ Пользователь <b>{escape(name)}</b> создан, прокси перезапущен.\n\n<code>{escape(link)}</code>",
                disable_web_page_preview=True)
        else:
            d = await _post("/api/provision/vpn/add", {"name": name})
            await note.edit_text(f"✅ Пир <b>{escape(name)}</b> создан — адрес {d.get('ip')}, без перезапуска.")
            await m.answer_document(
                BufferedInputFile(d["config"].encode("utf-8"), filename=f"{name}.conf"),
                caption=f"🔐 <b>{escape(name)}</b> — конфиг AmneziaWG")
    except Exception as e:
        log.warning("provision %s failed: %r", kind, e)
        await note.edit_text(f"⚠️ не получилось: {escape(str(e))[:200]}")
