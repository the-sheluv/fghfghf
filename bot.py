"""
🪐 ОРБИТ ЗНАКОМСТВА — Telegram-бот знакомств в одном файле.

Запуск:
    pip install "aiogram>=3.15" aiosqlite
    python bot.py

Рядом с bot.py положите картинку welcome.png (приветственный экран).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
import sys
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any, Optional, Sequence

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router, BaseMiddleware
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardMarkup,
    KeyboardButton,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    TelegramObject,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from aiogram.utils.media_group import MediaGroupBuilder

# ═══════════════════════════════════════════════════════════════════════════
#  НАСТРОЙКИ — заполните две строки ниже
# ═══════════════════════════════════════════════════════════════════════════

BOT_TOKEN = "8988477293:AAFo7t89ikg58dalnoSewK2kpsfpsQAXu9Y"   # токен от @BotFather
ADMIN_ID = 7521801228                                           # ваш ID от @userinfobot

# Если админов несколько — допишите ID через запятую: (111, 222, 333)
ADMIN_IDS: tuple[int, ...] = (ADMIN_ID,)

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "orbit.db"
WELCOME_IMAGE = BASE_DIR / "welcome.png"

# --- Возраст ---------------------------------------------------------------
# ЖЁСТКОЕ ОГРАНИЧЕНИЕ. Не понижать: сервис знакомств, где незнакомые взрослые
# видят фото пользователя и могут прислать ему голосовое сообщение, не должен
# быть доступен несовершеннолетним.
MIN_AGE = 18
MAX_AGE = 99

# --- Анкета ----------------------------------------------------------------
MAX_PHOTOS = 3
NAME_MAX_LEN = 32
CITY_MAX_LEN = 40
COUNTRIES = (("by", "🇧🇾 Беларусь"), ("ru", "🇷🇺 Россия"))

# --- Лента -----------------------------------------------------------------
FREE_DAILY_VIEWS = 30
PREMIUM_DAILY_VIEWS = 250
DISLIKE_COOLDOWN_DAYS = 10      # пропущенная анкета вернётся через 10 дней
PREMIUM_AGE_BONUS = 2           # премиуму чуть шире возрастное окно
BROADCAST_RATE = 0.05           # пауза между сообщениями рассылки, сек

# --- Premium (цены в звёздах Telegram) -------------------------------------
PREMIUM_PLANS: dict[str, dict[str, Any]] = {
    "week":  {"title": "Premium · 7 дней",   "days": 7,   "stars": 75},
    "month": {"title": "Premium · 30 дней",  "days": 30,  "stars": 250},
    "year":  {"title": "Premium · 365 дней", "days": 365, "stars": 1500},
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
)
log = logging.getLogger("orbit")


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def country_title(code: str) -> str:
    for c, title in COUNTRIES:
        if c == code:
            return title
    return code


# ═══════════════════════════════════════════════════════════════════════════
#  БАЗА ДАННЫХ
# ═══════════════════════════════════════════════════════════════════════════

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS users (
    tg_id          INTEGER PRIMARY KEY,
    username       TEXT,
    name           TEXT,
    age            INTEGER,
    country        TEXT,
    city           TEXT,
    is_active      INTEGER NOT NULL DEFAULT 0,
    is_banned      INTEGER NOT NULL DEFAULT 0,
    is_premium     INTEGER NOT NULL DEFAULT 0,
    premium_until  TEXT,
    views_date     TEXT,
    views_used     INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL,
    completed_at   TEXT
);

CREATE TABLE IF NOT EXISTS photos (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id  INTEGER NOT NULL REFERENCES users(tg_id) ON DELETE CASCADE,
    file_id  TEXT NOT NULL,
    position INTEGER NOT NULL,
    UNIQUE (user_id, position)
);

CREATE TABLE IF NOT EXISTS reactions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    from_id      INTEGER NOT NULL REFERENCES users(tg_id) ON DELETE CASCADE,
    to_id        INTEGER NOT NULL REFERENCES users(tg_id) ON DELETE CASCADE,
    kind         TEXT NOT NULL CHECK (kind IN ('like','dislike','message')),
    content_type TEXT,
    content      TEXT,
    status       TEXT NOT NULL DEFAULT 'pending',
    created_at   TEXT NOT NULL,
    UNIQUE (from_id, to_id)
);
CREATE INDEX IF NOT EXISTS idx_reactions_to ON reactions(to_id, status, kind);

CREATE TABLE IF NOT EXISTS matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_a INTEGER NOT NULL, user_b INTEGER NOT NULL,
    created_at TEXT NOT NULL, UNIQUE (user_a, user_b)
);

CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL, plan TEXT NOT NULL, stars INTEGER NOT NULL,
    charge_id TEXT, created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS support (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL, text TEXT,
    answered INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
);
"""

_db: Optional[aiosqlite.Connection] = None


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def today() -> str:
    return dt.date.today().isoformat()


def days_ago(days: int) -> str:
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat(timespec="seconds")


def db() -> aiosqlite.Connection:
    if _db is None:
        raise RuntimeError("init_db() не вызван")
    return _db


async def init_db() -> None:
    global _db
    _db = await aiosqlite.connect(DB_PATH)
    _db.row_factory = aiosqlite.Row
    await _db.executescript(SCHEMA)
    await _db.commit()


async def close_db() -> None:
    if _db is not None:
        await _db.close()


def _p(params: Any) -> Any:
    """dict → именованные параметры (:name), остальное → позиционные (?)."""
    return params if isinstance(params, Mapping) else tuple(params)


async def fetchone(sql: str, params: Any = ()) -> Optional[aiosqlite.Row]:
    async with db().execute(sql, _p(params)) as cur:
        return await cur.fetchone()


async def fetchall(sql: str, params: Any = ()) -> list[aiosqlite.Row]:
    async with db().execute(sql, _p(params)) as cur:
        return list(await cur.fetchall())


async def scalar(sql: str, params: Any = ()) -> int:
    row = await fetchone(sql, params)
    return int(row[0]) if row and row[0] is not None else 0


# --- Пользователи ----------------------------------------------------------
async def ensure_user(tg_id: int, username: str | None) -> aiosqlite.Row:
    await db().execute(
        "INSERT INTO users (tg_id, username, created_at) VALUES (?,?,?) "
        "ON CONFLICT(tg_id) DO UPDATE SET username = excluded.username",
        (tg_id, username, now()),
    )
    await db().commit()
    return await get_user(tg_id)  # type: ignore[return-value]


async def get_user(tg_id: int) -> Optional[aiosqlite.Row]:
    return await fetchone("SELECT * FROM users WHERE tg_id = ?", (tg_id,))


async def save_profile(tg_id: int, name: str, age: int, country: str, city: str,
                       photos: list[str]) -> None:
    if age < MIN_AGE:                       # страховка на уровне БД
        raise ValueError("Профиль младше 18 лет не может быть сохранён")
    await db().execute(
        "UPDATE users SET name=?, age=?, country=?, city=?, is_active=1, "
        "completed_at=COALESCE(completed_at, ?) WHERE tg_id=?",
        (name, age, country, city, now(), tg_id),
    )
    await db().execute("DELETE FROM photos WHERE user_id = ?", (tg_id,))
    await db().executemany(
        "INSERT INTO photos (user_id, file_id, position) VALUES (?,?,?)",
        [(tg_id, fid, i) for i, fid in enumerate(photos)],
    )
    await db().commit()


async def set_active(tg_id: int, active: bool) -> None:
    await db().execute("UPDATE users SET is_active=? WHERE tg_id=?", (int(active), tg_id))
    await db().commit()


async def set_banned(tg_id: int, banned: bool) -> None:
    await db().execute(
        "UPDATE users SET is_banned=?, is_active=CASE WHEN ? THEN 0 ELSE is_active END "
        "WHERE tg_id=?",
        (int(banned), int(banned), tg_id),
    )
    await db().commit()


async def get_photos(tg_id: int) -> list[str]:
    rows = await fetchall(
        "SELECT file_id FROM photos WHERE user_id=? ORDER BY position", (tg_id,)
    )
    return [r["file_id"] for r in rows]


# --- Premium ---------------------------------------------------------------
async def grant_premium(tg_id: int, days: int) -> str:
    row = await get_user(tg_id)
    base = dt.datetime.now(dt.timezone.utc)
    if row and row["premium_until"]:
        base = max(base, dt.datetime.fromisoformat(row["premium_until"]))
    until = base + dt.timedelta(days=days)
    await db().execute(
        "UPDATE users SET is_premium=1, premium_until=? WHERE tg_id=?",
        (until.isoformat(timespec="seconds"), tg_id),
    )
    await db().commit()
    return until.strftime("%d.%m.%Y")


async def expire_premium() -> int:
    cur = await db().execute(
        "UPDATE users SET is_premium=0 WHERE is_premium=1 AND premium_until < ?", (now(),)
    )
    await db().commit()
    return cur.rowcount or 0


async def log_payment(user_id: int, plan: str, stars: int, charge_id: str | None) -> None:
    await db().execute(
        "INSERT INTO payments (user_id, plan, stars, charge_id, created_at) VALUES (?,?,?,?,?)",
        (user_id, plan, stars, charge_id, now()),
    )
    await db().commit()


# --- Лимиты просмотров -----------------------------------------------------
async def take_view(tg_id: int) -> tuple[bool, int]:
    user = await get_user(tg_id)
    if user is None:
        return False, 0
    limit = PREMIUM_DAILY_VIEWS if user["is_premium"] else FREE_DAILY_VIEWS
    used = user["views_used"] if user["views_date"] == today() else 0
    if used >= limit:
        return False, 0
    await db().execute(
        "UPDATE users SET views_date=?, views_used=? WHERE tg_id=?", (today(), used + 1, tg_id)
    )
    await db().commit()
    return True, limit - used - 1


# --- Подбор анкет ----------------------------------------------------------
def age_bounds(age: int, premium: bool = False) -> tuple[int, int]:
    """Возрастное окно: чем старше, тем шире разброс. Ниже 18 не опускается."""
    window = 3 if age < 25 else 5 if age < 40 else 8
    if premium:
        window += PREMIUM_AGE_BONUS
    return max(MIN_AGE, age - window), min(MAX_AGE, age + window)


async def next_candidate(me: aiosqlite.Row) -> Optional[aiosqlite.Row]:
    """Сначала те, кто уже лайкнул меня, потом земляки, потом Premium, потом случайно."""
    low, high = age_bounds(me["age"], bool(me["is_premium"]))
    return await fetchone(
        """
        SELECT u.* FROM users u
         WHERE u.tg_id <> :me AND u.is_active = 1 AND u.is_banned = 0
           AND u.age BETWEEN :low AND :high
           AND u.country = :country
           AND NOT EXISTS (
                 SELECT 1 FROM reactions r
                  WHERE r.from_id = :me AND r.to_id = u.tg_id
                    AND (r.kind IN ('like','message') OR r.created_at > :cooldown))
         ORDER BY
           (SELECT COUNT(*) FROM reactions r2
             WHERE r2.from_id = u.tg_id AND r2.to_id = :me
               AND r2.kind IN ('like','message') AND r2.status = 'pending') DESC,
           (u.city = :city) DESC,
           u.is_premium DESC,
           RANDOM()
         LIMIT 1
        """,
        {"me": me["tg_id"], "low": low, "high": high, "country": me["country"],
         "city": me["city"], "cooldown": days_ago(DISLIKE_COOLDOWN_DAYS)},
    )


# --- Реакции и мэтчи -------------------------------------------------------
async def add_reaction(from_id: int, to_id: int, kind: str,
                       content_type: str | None = None, content: str | None = None) -> None:
    status = "pending" if kind in ("like", "message") else "declined"
    await db().execute(
        "INSERT INTO reactions (from_id,to_id,kind,content_type,content,status,created_at) "
        "VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(from_id,to_id) DO UPDATE SET kind=excluded.kind, "
        "content_type=excluded.content_type, content=excluded.content, "
        "status=excluded.status, created_at=excluded.created_at",
        (from_id, to_id, kind, content_type, content, status, now()),
    )
    await db().commit()


async def set_reaction_status(from_id: int, to_id: int, status: str) -> None:
    await db().execute(
        "UPDATE reactions SET status=? WHERE from_id=? AND to_id=?", (status, from_id, to_id)
    )
    await db().commit()


async def get_reaction(from_id: int, to_id: int) -> Optional[aiosqlite.Row]:
    return await fetchone("SELECT * FROM reactions WHERE from_id=? AND to_id=?", (from_id, to_id))


async def pending_likes(to_id: int) -> list[aiosqlite.Row]:
    return await fetchall(
        "SELECT r.* FROM reactions r JOIN users u ON u.tg_id = r.from_id "
        "WHERE r.to_id=? AND r.status='pending' AND r.kind IN ('like','message') "
        "AND u.is_banned=0 ORDER BY r.created_at DESC",
        (to_id,),
    )


async def pending_likes_count(to_id: int) -> int:
    return await scalar(
        "SELECT COUNT(*) FROM reactions WHERE to_id=? AND status='pending' "
        "AND kind IN ('like','message')",
        (to_id,),
    )


async def create_match(a: int, b: int) -> None:
    lo, hi = sorted((a, b))
    await db().execute(
        "INSERT OR IGNORE INTO matches (user_a,user_b,created_at) VALUES (?,?,?)", (lo, hi, now())
    )
    await db().commit()


# --- Поддержка и статистика ------------------------------------------------
async def add_ticket(user_id: int, text: str | None) -> int:
    cur = await db().execute(
        "INSERT INTO support (user_id,text,created_at) VALUES (?,?,?)", (user_id, text, now())
    )
    await db().commit()
    return int(cur.lastrowid)


async def close_ticket(ticket_id: int) -> None:
    await db().execute("UPDATE support SET answered=1 WHERE id=?", (ticket_id,))
    await db().commit()


async def all_user_ids() -> list[int]:
    return [r["tg_id"] for r in await fetchall("SELECT tg_id FROM users WHERE is_banned=0")]


async def get_stats() -> dict[str, int]:
    return {
        "users": await scalar("SELECT COUNT(*) FROM users"),
        "profiles": await scalar("SELECT COUNT(*) FROM users WHERE completed_at IS NOT NULL"),
        "active": await scalar("SELECT COUNT(*) FROM users WHERE is_active=1"),
        "new_today": await scalar(
            "SELECT COUNT(*) FROM users WHERE substr(created_at,1,10)=?", (today(),)),
        "profiles_today": await scalar(
            "SELECT COUNT(*) FROM users WHERE substr(completed_at,1,10)=?", (today(),)),
        "premium": await scalar("SELECT COUNT(*) FROM users WHERE is_premium=1"),
        "banned": await scalar("SELECT COUNT(*) FROM users WHERE is_banned=1"),
        "likes": await scalar("SELECT COUNT(*) FROM reactions WHERE kind IN ('like','message')"),
        "dislikes": await scalar("SELECT COUNT(*) FROM reactions WHERE kind='dislike'"),
        "matches": await scalar("SELECT COUNT(*) FROM matches"),
        "stars": await scalar("SELECT COALESCE(SUM(stars),0) FROM payments"),
        "tickets_open": await scalar("SELECT COUNT(*) FROM support WHERE answered=0"),
    }


# ═══════════════════════════════════════════════════════════════════════════
#  ТЕКСТЫ
# ═══════════════════════════════════════════════════════════════════════════

WELCOME = (
    "🪐 <b>Добро пожаловать в «Орбит Знакомства»!</b>\n\n"
    "Тысячи орбит, и где-то рядом — та самая, что пересечётся с вашей. "
    "Возможно, именно здесь вы найдёте свою половинку ✨\n\n"
    "Как это работает:\n"
    "1️⃣ Заполняете анкету — это займёт минуту\n"
    "2️⃣ Смотрите анкеты тех, кто рядом и близок по возрасту\n"
    "3️⃣ Ставите ❤️ или пишете сообщение — голосом, видео или текстом\n"
    "4️⃣ Если симпатия взаимна, мы знакомим вас лично\n\n"
    f"⚠️ Бот только для совершеннолетних: {MIN_AGE}+"
)
WELCOME_BACK = "🪐 С возвращением на орбиту, {name}!\nВыберите, что хотим сделать 👇"

ASK_NAME = "Как вас зовут?"
ASK_NAME_AGAIN = f"Имя должно быть от 2 до {NAME_MAX_LEN} символов и без ссылок. Попробуйте ещё раз:"
ASK_AGE = "Отлично, {name}! Сколько вам лет?"
ASK_AGE_AGAIN = "Введите возраст числом, например <b>24</b>:"

UNDERAGE = (
    "🚫 <b>Анкету создать не получится.</b>\n\n"
    f"«Орбит Знакомства» — сервис для взрослых, доступ открыт только с {MIN_AGE} лет. "
    "Это не формальность: здесь незнакомые люди видят ваши фото и могут писать вам лично.\n\n"
    "Если вам нет 18 — пожалуйста, найдите общение на площадках, рассчитанных на ваш возраст. "
    "А если вы ошиблись при вводе, нажмите /start и введите настоящий возраст."
)

ASK_COUNTRY = "Выберите вашу страну:"
ASK_CITY = "Напишите ваш город:"
ASK_CITY_AGAIN = f"Название города — до {CITY_MAX_LEN} символов, без ссылок. Ещё раз:"
ASK_PHOTO = "Теперь пришлите ваше фото 📸\n\nЛучше то, где хорошо видно лицо — такие анкеты смотрят чаще."
ASK_MORE_PHOTO = ("Отлично! Прикрепите ещё одно фото? ({current}/{total})\n\n"
                  "Можно отправить прямо сейчас или пропустить.")
PHOTO_ONLY = "Нужно именно фото 🙂 Отправьте его как изображение."
PHOTOS_FULL = "Больше {total} фото добавить нельзя, идём дальше 👇"
PROFILE_PREVIEW = "Так ваша анкета выглядит для других 👇"
PROFILE_SAVED = ("🚀 Анкета опубликована!\n\nТеперь её видят люди близкого возраста "
                 "из вашей страны. Удачи на орбите ✨")

NO_PROFILE = "Сначала заполните анкету 👇"
FEED_EMPTY = ("🌌 Пока анкет больше нет.\n\nЗагляните позже — новые люди появляются каждый день. "
              f"А те, кого вы пропустили, вернутся в ленту через {DISLIKE_COOLDOWN_DAYS} дней.")
LIMIT_REACHED = ("😴 На сегодня просмотры закончились ({limit} анкет).\n\n"
                 f"Лимит обновится завтра. С <b>Premium</b> доступно {PREMIUM_DAILY_VIEWS} анкет в день ⭐")

ASK_MESSAGE = ("✍️ Отправьте сообщение для этого человека.\n\n"
               "Можно текстом, голосовым 🎤 или видеосообщением (кружочком) 📹.\n"
               "Оно улетит вместе с вашей анкетой и будет считаться как ❤️")
MESSAGE_SENT = "📨 Отправлено! Если человек ответит взаимностью, вы узнаете первым."
LIKE_SENT = "❤️ Лайк отправлен!"

LIKE_NOTIFY = "💫 <b>Вы понравились:</b>"
LIKE_NOTIFY_MSG = "💫 <b>Вы понравились и вам оставили сообщение:</b>"
TRY_CHAT = "Попробуете пообщаться?"
MATCH_YOU = ("🎉 <b>Взаимно!</b>\n\n{name} тоже хочет пообщаться. "
             "Напишите первым — это всегда работает лучше ожидания 😉\n{link}")
MATCH_DECLINED = "Хорошо, эта анкета больше не появится."
NO_USERNAME = ("⚠️ У человека не открыт @username, поэтому прямой ссылки нет. "
               "Он получил вашу — напишет сам.")

LIKES_EMPTY = "Пока никто не поставил вам ❤️. Смотрите анкеты — симпатии приходят в ответ 😉"
LIKES_HEADER = "💌 Вас лайкнули: <b>{count}</b>\nПоказываю по одной 👇"

SUPPORT_ASK = ("🆘 <b>Поддержка</b>\n\nОпишите проблему одним сообщением — оно уйдёт "
               "администратору. Можно приложить фото или скриншот.")
SUPPORT_SENT = "✅ Сообщение отправлено. Ответ придёт сюда же."
SUPPORT_REPLY = "🆘 <b>Ответ поддержки:</b>\n\n{text}"

PREMIUM_INFO = (
    "⭐ <b>Orbit Premium</b>\n\nЧто даёт подписка:\n"
    f"• {PREMIUM_DAILY_VIEWS} анкет в день вместо {FREE_DAILY_VIEWS}\n"
    "• Звёздочка ⭐ рядом с именем в анкете\n"
    "• Приоритет в ленте — вашу анкету показывают выше\n"
    "• Шире возрастной диапазон подбора\n\n"
    "Оплата — звёздами Telegram ⭐. Выберите срок:"
)
PREMIUM_ACTIVE = "⭐ <b>Premium активен</b> до {until}\n\nСпасибо, что поддерживаете проект 💙"
PREMIUM_THANKS = ("🎉 <b>Спасибо за поддержку!</b>\n\nСтатус <b>Premium User</b> ⭐ активен "
                  f"до <b>{{until}}</b>.\nДневной лимит просмотров: {PREMIUM_DAILY_VIEWS}.")
CANCELLED = "Отменено."


def profile_card(user, note: str = "") -> str:
    star = " ⭐" if user["is_premium"] else ""
    text = (f"<b>{user['name']}</b>{star}, {user['age']}\n"
            f"📍 {user['city']}, {country_title(user['country'])}")
    if note:
        text += f"\n\n{note}"
    return text


# ═══════════════════════════════════════════════════════════════════════════
#  КЛАВИАТУРЫ (синие «пилюли» под сообщением — это inline-кнопки Telegram)
# ═══════════════════════════════════════════════════════════════════════════

REMOVE_KB = ReplyKeyboardRemove()

BTN_FEED = "🔍 Смотреть анкеты"
BTN_MY_PROFILE = "👤 Моя анкета"
BTN_LIKES = "💌 Мои симпатии"
BTN_PREMIUM = "⭐ Premium"
BTN_SUPPORT = "🆘 Поддержка"


def main_menu(likes: int = 0) -> ReplyKeyboardMarkup:
    likes_label = f"{BTN_LIKES} ({likes})" if likes else BTN_LIKES
    kb = ReplyKeyboardBuilder()
    kb.row(KeyboardButton(text=BTN_FEED))
    kb.row(KeyboardButton(text=BTN_MY_PROFILE), KeyboardButton(text=likes_label))
    kb.row(KeyboardButton(text=BTN_PREMIUM), KeyboardButton(text=BTN_SUPPORT))
    return kb.as_markup(resize_keyboard=True, input_field_placeholder="Выберите действие…")


def start_kb(has_profile: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Изменить анкету" if has_profile else "🚀 Заполнить анкету",
              callback_data="profile:fill")
    if has_profile:
        kb.button(text="🔍 Смотреть анкеты", callback_data="feed:start")
    kb.adjust(1)
    return kb.as_markup()


def country_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for code, title in COUNTRIES:
        kb.button(text=title, callback_data=f"country:{code}")
    kb.adjust(1)
    return kb.as_markup()


def photo_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📷 Прикрепить ещё", callback_data="photo:more")
    kb.button(text="➡️ Пропустить", callback_data="photo:skip")
    kb.adjust(2)
    return kb.as_markup()


def preview_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Всё верно, опубликовать", callback_data="profile:publish")
    kb.button(text="🔄 Заполнить заново", callback_data="profile:fill")
    kb.adjust(1)
    return kb.as_markup()


def my_profile_kb(is_active: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Изменить анкету", callback_data="profile:fill")
    kb.button(text="🙈 Скрыть из поиска" if is_active else "👀 Вернуть в поиск",
              callback_data="profile:toggle")
    kb.button(text="🔍 Смотреть анкеты", callback_data="feed:start")
    kb.adjust(1)
    return kb.as_markup()


def feed_kb(target_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="❤️", callback_data=f"feed:like:{target_id}")
    kb.button(text="💬 Написать", callback_data=f"feed:msg:{target_id}")
    kb.button(text="👎", callback_data=f"feed:dislike:{target_id}")
    kb.button(text="🚩 Пожаловаться", callback_data=f"feed:report:{target_id}")
    kb.button(text="🏠 В меню", callback_data="feed:stop")
    kb.adjust(3, 1, 1)
    return kb.as_markup()


def cancel_kb(callback: str = "feed:cancel") -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✖️ Отмена", callback_data=callback)
    return kb.as_markup()


def like_answer_kb(from_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="👍 Да", callback_data=f"like:yes:{from_id}")
    kb.button(text="👎 Нет", callback_data=f"like:no:{from_id}")
    kb.adjust(2)
    return kb.as_markup()


def premium_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for code, plan in PREMIUM_PLANS.items():
        kb.button(text=f"{plan['title']} — {plan['stars']} ⭐", callback_data=f"premium:{code}")
    kb.adjust(1)
    return kb.as_markup()


def admin_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Обновить статистику", callback_data="admin:stats")
    kb.button(text="📣 Рассылка", callback_data="admin:broadcast")
    kb.button(text="🔎 Найти пользователя", callback_data="admin:find")
    kb.adjust(1)
    return kb.as_markup()


def ticket_kb(ticket_id: int, user_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✍️ Ответить", callback_data=f"admin:reply:{user_id}:{ticket_id}")
    kb.button(text="🚫 Забанить", callback_data=f"admin:ban:{user_id}")
    kb.adjust(2)
    return kb.as_markup()


def user_manage_kb(user_id: int, banned: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="♻️ Разбанить" if banned else "🚫 Забанить",
              callback_data=f"admin:{'unban' if banned else 'ban'}:{user_id}")
    kb.button(text="✍️ Написать", callback_data=f"admin:reply:{user_id}:0")
    kb.adjust(2)
    return kb.as_markup()


# ═══════════════════════════════════════════════════════════════════════════
#  СОСТОЯНИЯ
# ═══════════════════════════════════════════════════════════════════════════

class Registration(StatesGroup):
    name = State()
    age = State()
    country = State()
    city = State()
    photo = State()


class Browsing(StatesGroup):
    feed = State()
    writing = State()


class SupportState(StatesGroup):
    waiting_message = State()


class AdminStates(StatesGroup):
    broadcast = State()
    reply_to_user = State()
    find_user = State()


# ═══════════════════════════════════════════════════════════════════════════
#  УТИЛИТЫ
# ═══════════════════════════════════════════════════════════════════════════

_LINK_RE = re.compile(r"(https?://|t\.me/|@[\w_]{4,}|www\.)", re.IGNORECASE)


def clean_name(raw: str | None) -> str | None:
    if not raw:
        return None
    value = " ".join(raw.split())
    if not (2 <= len(value) <= NAME_MAX_LEN) or _LINK_RE.search(value):
        return None
    return value


def clean_city(raw: str | None) -> str | None:
    if not raw:
        return None
    value = " ".join(raw.split())
    if not (2 <= len(value) <= CITY_MAX_LEN) or _LINK_RE.search(value):
        return None
    return value.title()


def parse_age(raw: str | None) -> int | None:
    """Число ли это вообще. Проверка на 18+ — отдельно, в хендлере."""
    if not raw:
        return None
    digits = raw.strip().split()[0]
    if not digits.isdigit():
        return None
    age = int(digits)
    return age if 1 <= age <= MAX_AGE else None


def profile_link(user) -> str:
    if user["username"]:
        return f"👉 @{user['username']}"
    return f'👉 <a href="tg://user?id={user["tg_id"]}">Написать в Telegram</a>'


async def send_profile(bot: Bot, chat_id: int, user, photos: Sequence[str], note: str = "",
                       header: str = "", keyboard: InlineKeyboardMarkup | None = None) -> list[int]:
    """Показывает анкету (альбом + карточка). Возвращает id сообщений."""
    sent: list[int] = []
    caption = profile_card(user, note)
    if header:
        caption = f"{header}\n\n{caption}"

    if len(photos) > 1:
        album = MediaGroupBuilder(caption=caption)
        for file_id in photos:
            album.add_photo(media=file_id)
        messages = await bot.send_media_group(chat_id, media=album.build())
        sent += [m.message_id for m in messages]
        if keyboard:
            tail = await bot.send_message(chat_id, "Ваш выбор 👇", reply_markup=keyboard)
            sent.append(tail.message_id)
    elif photos:
        msg = await bot.send_photo(chat_id, photos[0], caption=caption, reply_markup=keyboard)
        sent.append(msg.message_id)
    else:
        msg = await bot.send_message(chat_id, caption, reply_markup=keyboard)
        sent.append(msg.message_id)
    return sent


async def send_attachment(bot: Bot, chat_id: int, content_type: str | None,
                          content: str | None) -> None:
    if not content:
        return
    try:
        if content_type == "voice":
            await bot.send_voice(chat_id, content, caption="🎤 Голосовое сообщение")
        elif content_type == "video_note":
            await bot.send_message(chat_id, "📹 Видеосообщение:")
            await bot.send_video_note(chat_id, content)
        else:
            await bot.send_message(chat_id, f"💬 <i>{content}</i>")
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        log.warning("Вложение не доставлено в %s: %s", chat_id, exc)


async def cleanup(bot: Bot, chat_id: int, state: FSMContext) -> None:
    """Удаляет предыдущую карточку, чтобы лента не растягивалась."""
    data = await state.get_data()
    for message_id in data.get("last_ids", []):
        with suppress(TelegramBadRequest, TelegramForbiddenError):
            await bot.delete_message(chat_id, message_id)
    await state.update_data(last_ids=[])


async def safe_send(bot: Bot, chat_id: int, text: str, **kwargs) -> bool:
    try:
        await bot.send_message(chat_id, text, **kwargs)
        return True
    except (TelegramForbiddenError, TelegramBadRequest) as exc:
        log.info("Сообщение для %s не доставлено: %s", chat_id, exc)
        return False


# ═══════════════════════════════════════════════════════════════════════════
#  MIDDLEWARE
# ═══════════════════════════════════════════════════════════════════════════

class UserMiddleware(BaseMiddleware):
    """Авторегистрация пользователя + отсечение забаненных."""

    async def __call__(self, handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        tg_user = data.get("event_from_user")
        if tg_user is None or tg_user.is_bot:
            return await handler(event, data)

        row = await ensure_user(tg_user.id, tg_user.username)
        if row["is_banned"]:
            text = "🚫 Ваш доступ к боту ограничен администратором."
            if isinstance(event, Message):
                await event.answer(text)
            elif isinstance(event, CallbackQuery):
                await event.answer(text, show_alert=True)
            return None

        data["user"] = row
        return await handler(event, data)


class ThrottlingMiddleware(BaseMiddleware):
    """Простейший антифлуд."""

    def __init__(self, rate: float = 0.35) -> None:
        self.rate = rate
        self._last: dict[int, float] = {}

    async def __call__(self, handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        tg_user = data.get("event_from_user")
        if tg_user is not None:
            moment = asyncio.get_running_loop().time()
            if moment - self._last.get(tg_user.id, 0.0) < self.rate:
                if isinstance(event, CallbackQuery):
                    await event.answer("Не так быстро 🙂")
                return None
            self._last[tg_user.id] = moment
        return await handler(event, data)


# ═══════════════════════════════════════════════════════════════════════════
#  РОУТЕРЫ
# ═══════════════════════════════════════════════════════════════════════════

admin_router = Router(name="admin")
admin_router.message.filter(lambda m: is_admin(m.from_user.id))
admin_router.callback_query.filter(lambda c: is_admin(c.from_user.id))

router = Router(name="main")


# ───────────────────────────── СТАРТ И МЕНЮ ─────────────────────────────────
_welcome_file_id: str | None = None   # кешируем file_id, чтобы не грузить картинку каждый раз


async def show_welcome(message: Message, user) -> None:
    global _welcome_file_id
    has_profile = user["completed_at"] is not None
    markup = start_kb(has_profile)

    photo = _welcome_file_id or (FSInputFile(WELCOME_IMAGE) if WELCOME_IMAGE.exists() else None)
    if photo is not None:
        try:
            sent = await message.answer_photo(photo, caption=WELCOME, reply_markup=markup)
            if sent.photo:
                _welcome_file_id = sent.photo[-1].file_id
        except Exception:
            await message.answer(WELCOME, reply_markup=markup)
    else:
        await message.answer(WELCOME, reply_markup=markup)

    if has_profile:
        likes = await pending_likes_count(user["tg_id"])
        await message.answer(WELCOME_BACK.format(name=user["name"]), reply_markup=main_menu(likes))


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, user) -> None:
    await state.clear()
    await show_welcome(message, user)


@router.message(Command("menu"))
async def cmd_menu(message: Message, state: FSMContext, user) -> None:
    await state.clear()
    if user["completed_at"] is None:
        await message.answer(NO_PROFILE, reply_markup=start_kb(False))
        return
    likes = await pending_likes_count(user["tg_id"])
    await message.answer("Главное меню 👇", reply_markup=main_menu(likes))


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "🪐 <b>Орбит Знакомства</b>\n\n"
        "/start — перезапустить бота\n/menu — главное меню\n"
        "/myprofile — моя анкета\n/premium — подписка ⭐\n/support — поддержка\n\n"
        f"Сервис доступен только с {MIN_AGE} лет."
    )


@router.callback_query(F.data == "feed:stop")
async def back_to_menu(call: CallbackQuery, state: FSMContext, user) -> None:
    await state.clear()
    with suppress(TelegramBadRequest):
        await call.message.delete()
    likes = await pending_likes_count(user["tg_id"])
    await call.message.answer("Главное меню 👇", reply_markup=main_menu(likes))
    await call.answer()


# ───────────────────────────── АНКЕТА ───────────────────────────────────────
@router.callback_query(F.data == "profile:fill")
async def start_fill(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(Registration.name)
    await call.message.answer(ASK_NAME, reply_markup=REMOVE_KB)
    await call.answer()


@router.message(Command("fill"))
async def cmd_fill(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(Registration.name)
    await message.answer(ASK_NAME, reply_markup=REMOVE_KB)


@router.message(Registration.name, F.text)
async def step_name(message: Message, state: FSMContext) -> None:
    name = clean_name(message.text)
    if name is None:
        await message.answer(ASK_NAME_AGAIN)
        return
    await state.update_data(name=name)
    await state.set_state(Registration.age)
    await message.answer(ASK_AGE.format(name=name))


@router.message(Registration.age, F.text)
async def step_age(message: Message, state: FSMContext) -> None:
    """Здесь стоит возрастной фильтр: младше 18 анкету не заводит."""
    age = parse_age(message.text)
    if age is None:
        await message.answer(ASK_AGE_AGAIN)
        return

    if age < MIN_AGE:
        await state.clear()
        await message.answer(UNDERAGE, reply_markup=REMOVE_KB)
        return

    await state.update_data(age=age)
    await state.set_state(Registration.country)
    await message.answer(ASK_COUNTRY, reply_markup=country_kb())


@router.callback_query(Registration.country, F.data.startswith("country:"))
async def step_country(call: CallbackQuery, state: FSMContext) -> None:
    code = call.data.split(":")[1]
    await state.update_data(country=code)
    await state.set_state(Registration.city)
    with suppress(TelegramBadRequest):
        await call.message.edit_text(f"Страна: {country_title(code)}")
    await call.message.answer(ASK_CITY)
    await call.answer()


@router.message(Registration.city, F.text)
async def step_city(message: Message, state: FSMContext) -> None:
    city = clean_city(message.text)
    if city is None:
        await message.answer(ASK_CITY_AGAIN)
        return
    await state.update_data(city=city, photos=[])
    await state.set_state(Registration.photo)
    await message.answer(ASK_PHOTO)


@router.message(Registration.photo, F.photo)
async def step_photo(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    photos: list[str] = list(data.get("photos", []))

    if len(photos) >= MAX_PHOTOS:
        await message.answer(PHOTOS_FULL.format(total=MAX_PHOTOS))
        await show_preview(message, state)
        return

    photos.append(message.photo[-1].file_id)
    await state.update_data(photos=photos)

    if len(photos) >= MAX_PHOTOS:
        await message.answer(PHOTOS_FULL.format(total=MAX_PHOTOS))
        await show_preview(message, state)
        return

    await message.answer(
        ASK_MORE_PHOTO.format(current=len(photos), total=MAX_PHOTOS),
        reply_markup=photo_kb(),
    )


@router.message(Registration.photo)
async def step_photo_wrong(message: Message) -> None:
    await message.answer(PHOTO_ONLY)


@router.callback_query(Registration.photo, F.data == "photo:more")
async def photo_more(call: CallbackQuery) -> None:
    with suppress(TelegramBadRequest):
        await call.message.edit_reply_markup(reply_markup=None)
    await call.message.answer("Жду следующее фото 📸")
    await call.answer()


@router.callback_query(Registration.photo, F.data == "photo:skip")
async def photo_skip(call: CallbackQuery, state: FSMContext) -> None:
    with suppress(TelegramBadRequest):
        await call.message.edit_reply_markup(reply_markup=None)
    await show_preview(call.message, state)
    await call.answer()


async def show_preview(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    draft = {"name": data["name"], "age": data["age"], "city": data["city"],
             "country": data["country"], "is_premium": 0}
    await message.answer(PROFILE_PREVIEW)
    await send_profile(message.bot, message.chat.id, draft, data.get("photos", []),
                       keyboard=preview_kb())


@router.callback_query(F.data == "profile:publish")
async def publish(call: CallbackQuery, state: FSMContext, user) -> None:
    data = await state.get_data()
    if not data.get("name") or not data.get("age"):
        await call.answer("Анкета устарела, начните заново", show_alert=True)
        return

    if int(data["age"]) < MIN_AGE:          # повторная проверка перед записью
        await state.clear()
        await call.message.answer(UNDERAGE)
        await call.answer()
        return

    await save_profile(user["tg_id"], data["name"], int(data["age"]),
                       data["country"], data["city"], data.get("photos", []))
    await state.clear()
    with suppress(TelegramBadRequest):
        await call.message.edit_reply_markup(reply_markup=None)

    likes = await pending_likes_count(user["tg_id"])
    await call.message.answer(PROFILE_SAVED, reply_markup=main_menu(likes))
    await call.answer("Опубликовано 🚀")


@router.message(F.text == BTN_MY_PROFILE)
@router.message(Command("myprofile"))
async def my_profile(message: Message, user) -> None:
    fresh = await get_user(user["tg_id"])
    if fresh is None or fresh["completed_at"] is None:
        await message.answer(NO_PROFILE, reply_markup=start_kb(False))
        return

    photos = await get_photos(fresh["tg_id"])
    note = "" if fresh["is_active"] else "🙈 <i>Анкета скрыта из поиска</i>"
    if fresh["is_premium"] and fresh["premium_until"]:
        note += f"\n⭐ <i>Premium до {fresh['premium_until'][:10]}</i>"
    await send_profile(message.bot, message.chat.id, fresh, photos, note=note,
                       keyboard=my_profile_kb(bool(fresh["is_active"])))


@router.callback_query(F.data == "profile:toggle")
async def toggle_active(call: CallbackQuery, user) -> None:
    fresh = await get_user(user["tg_id"])
    new_state = not bool(fresh["is_active"])
    await set_active(user["tg_id"], new_state)
    with suppress(TelegramBadRequest):
        await call.message.edit_reply_markup(reply_markup=my_profile_kb(new_state))
    await call.answer("Анкета в поиске 👀" if new_state else "Анкета скрыта 🙈")


# ───────────────────────────── ЛЕНТА АНКЕТ ──────────────────────────────────
async def show_next(bot: Bot, chat_id: int, me_id: int, state: FSMContext) -> None:
    me = await get_user(me_id)
    if me is None or me["completed_at"] is None:
        await bot.send_message(chat_id, NO_PROFILE, reply_markup=start_kb(False))
        return

    allowed, left = await take_view(me_id)
    if not allowed:
        limit = PREMIUM_DAILY_VIEWS if me["is_premium"] else FREE_DAILY_VIEWS
        await state.clear()
        await bot.send_message(chat_id, LIMIT_REACHED.format(limit=limit),
                               reply_markup=premium_kb())
        return

    candidate = await next_candidate(me)
    if candidate is None:
        await state.clear()
        likes = await pending_likes_count(me_id)
        await bot.send_message(chat_id, FEED_EMPTY, reply_markup=main_menu(likes))
        return

    photos = await get_photos(candidate["tg_id"])
    incoming = await get_reaction(candidate["tg_id"], me_id)
    note = "💫 <i>Этот человек уже поставил вам ❤️</i>" if (
        incoming and incoming["status"] == "pending") else ""

    sent = await send_profile(bot, chat_id, candidate, photos, note=note,
                              keyboard=feed_kb(candidate["tg_id"]))
    await state.set_state(Browsing.feed)
    await state.update_data(last_ids=sent, current=candidate["tg_id"], left=left)


@router.message(F.text == BTN_FEED)
@router.message(Command("feed"))
async def open_feed(message: Message, state: FSMContext, user) -> None:
    await state.clear()
    await show_next(message.bot, message.chat.id, user["tg_id"], state)


@router.callback_query(F.data == "feed:start")
async def open_feed_cb(call: CallbackQuery, state: FSMContext, user) -> None:
    await state.clear()
    await call.answer()
    await show_next(call.bot, call.message.chat.id, user["tg_id"], state)


@router.callback_query(F.data.startswith("feed:like:"))
async def like(call: CallbackQuery, state: FSMContext, user) -> None:
    target_id = int(call.data.split(":")[2])
    await add_reaction(user["tg_id"], target_id, "like")
    await notify_like(call.bot, user["tg_id"], target_id)
    await call.answer(LIKE_SENT)
    await cleanup(call.bot, call.message.chat.id, state)
    await show_next(call.bot, call.message.chat.id, user["tg_id"], state)


@router.callback_query(F.data.startswith("feed:dislike:"))
async def dislike(call: CallbackQuery, state: FSMContext, user) -> None:
    target_id = int(call.data.split(":")[2])
    # Дизлайк живёт DISLIKE_COOLDOWN_DAYS дней — потом анкета снова может выпасть.
    await add_reaction(user["tg_id"], target_id, "dislike")
    await call.answer()
    await cleanup(call.bot, call.message.chat.id, state)
    await show_next(call.bot, call.message.chat.id, user["tg_id"], state)


@router.callback_query(F.data.startswith("feed:report:"))
async def report(call: CallbackQuery, state: FSMContext, user) -> None:
    target_id = int(call.data.split(":")[2])
    await add_reaction(user["tg_id"], target_id, "dislike")
    ticket_id = await add_ticket(user["tg_id"], f"🚩 Жалоба на анкету id={target_id}")

    for admin_id in ADMIN_IDS:
        await safe_send(
            call.bot, admin_id,
            f"🚩 <b>Жалоба #{ticket_id}</b>\n"
            f"От: <code>{user['tg_id']}</code> (@{user['username'] or '—'})\n"
            f"На пользователя: <code>{target_id}</code>",
            reply_markup=user_manage_kb(target_id, banned=False),
        )
    await call.answer("Спасибо, мы проверим эту анкету", show_alert=True)
    await cleanup(call.bot, call.message.chat.id, state)
    await show_next(call.bot, call.message.chat.id, user["tg_id"], state)


@router.callback_query(F.data.startswith("feed:msg:"))
async def ask_message(call: CallbackQuery, state: FSMContext) -> None:
    target_id = int(call.data.split(":")[2])
    await state.set_state(Browsing.writing)
    await state.update_data(target=target_id)
    await call.message.answer(ASK_MESSAGE, reply_markup=cancel_kb())
    await call.answer()


@router.message(Browsing.writing, F.text | F.voice | F.video_note)
async def take_message(message: Message, state: FSMContext, user) -> None:
    data = await state.get_data()
    target_id = data.get("target")
    if not target_id:
        await state.clear()
        await message.answer("Анкета потерялась, откройте ленту заново.")
        return

    if message.voice:
        content_type, content = "voice", message.voice.file_id
    elif message.video_note:
        content_type, content = "video_note", message.video_note.file_id
    else:
        content_type, content = "text", (message.text or "")[:500]

    await add_reaction(user["tg_id"], target_id, "message", content_type, content)
    await notify_like(message.bot, user["tg_id"], target_id, content_type, content)

    await message.answer(MESSAGE_SENT)
    await cleanup(message.bot, message.chat.id, state)
    await show_next(message.bot, message.chat.id, user["tg_id"], state)


@router.message(Browsing.writing)
async def take_message_wrong(message: Message) -> None:
    await message.answer("Можно текст, голосовое 🎤 или видеосообщение 📹.")


@router.callback_query(F.data == "feed:cancel")
async def cancel_message(call: CallbackQuery, state: FSMContext) -> None:
    with suppress(TelegramBadRequest):
        await call.message.delete()
    await state.set_state(Browsing.feed)
    await call.answer(CANCELLED)


async def notify_like(bot: Bot, from_id: int, to_id: int,
                      content_type: str | None = None, content: str | None = None) -> None:
    """«Вы понравились: …» + анкета + «Попробуете пообщаться?»"""
    sender = await get_user(from_id)
    target = await get_user(to_id)
    if sender is None or target is None or target["is_banned"]:
        return

    photos = await get_photos(from_id)
    header = LIKE_NOTIFY_MSG if content_type else LIKE_NOTIFY
    try:
        await send_profile(bot, to_id, sender, photos, header=header)
        if content_type:
            await send_attachment(bot, to_id, content_type, content)
        await bot.send_message(to_id, TRY_CHAT, reply_markup=like_answer_kb(from_id))
    except Exception:            # бот заблокирован — лайк всё равно сохранён в БД
        pass


# ───────────────────────────── ВХОДЯЩИЕ СИМПАТИИ ────────────────────────────
@router.message(F.text.startswith(BTN_LIKES))
@router.message(Command("likes"))
async def my_likes(message: Message, user) -> None:
    rows = await pending_likes(user["tg_id"])
    if not rows:
        await message.answer(LIKES_EMPTY)
        return

    await message.answer(LIKES_HEADER.format(count=len(rows)))
    for row in rows[:10]:                       # за раз показываем не больше десяти
        sender = await get_user(row["from_id"])
        if sender is None:
            continue
        photos = await get_photos(row["from_id"])
        await send_profile(message.bot, message.chat.id, sender, photos)
        if row["kind"] == "message":
            await send_attachment(message.bot, message.chat.id,
                                  row["content_type"], row["content"])
        await message.answer(TRY_CHAT, reply_markup=like_answer_kb(row["from_id"]))


@router.callback_query(F.data.startswith("like:yes:"))
async def accept_like(call: CallbackQuery, user) -> None:
    from_id = int(call.data.split(":")[2])
    partner = await get_user(from_id)
    if partner is None or partner["is_banned"]:
        await call.answer("Анкета больше недоступна", show_alert=True)
        return

    await set_reaction_status(from_id, user["tg_id"], "accepted")
    await add_reaction(user["tg_id"], from_id, "like")
    await set_reaction_status(user["tg_id"], from_id, "accepted")
    await create_match(user["tg_id"], from_id)

    me = await get_user(user["tg_id"])

    with suppress(TelegramBadRequest):
        await call.message.edit_reply_markup(reply_markup=None)
    await call.message.answer(
        MATCH_YOU.format(name=partner["name"], link=profile_link(partner)),
        disable_web_page_preview=True,
    )
    if not partner["username"]:
        await call.message.answer(NO_USERNAME)

    photos = await get_photos(user["tg_id"])
    try:
        await send_profile(
            call.bot, from_id, me, photos,
            header=f"🎉 <b>Взаимная симпатия!</b>\n{me['name']} ответил(а) вам взаимностью:",
        )
        await safe_send(call.bot, from_id,
                        MATCH_YOU.format(name=me["name"], link=profile_link(me)),
                        disable_web_page_preview=True)
    except Exception:
        pass

    await call.answer("Мэтч! 🎉")


@router.callback_query(F.data.startswith("like:no:"))
async def decline_like(call: CallbackQuery, user) -> None:
    from_id = int(call.data.split(":")[2])
    await set_reaction_status(from_id, user["tg_id"], "declined")
    # Отказ = дизлайк: анкета не вернётся в ленту 10 дней.
    await add_reaction(user["tg_id"], from_id, "dislike")
    with suppress(TelegramBadRequest):
        await call.message.edit_reply_markup(reply_markup=None)
    await call.message.answer(MATCH_DECLINED)
    await call.answer()


# ───────────────────────────── PREMIUM (Telegram Stars) ─────────────────────
@router.message(F.text == BTN_PREMIUM)
@router.message(Command("premium"))
async def premium_info(message: Message, user) -> None:
    fresh = await get_user(user["tg_id"])
    if fresh and fresh["is_premium"] and fresh["premium_until"]:
        await message.answer(PREMIUM_ACTIVE.format(until=fresh["premium_until"][:10]),
                             reply_markup=premium_kb())
        return
    await message.answer(PREMIUM_INFO, reply_markup=premium_kb())


@router.callback_query(F.data.startswith("premium:"))
async def send_invoice(call: CallbackQuery) -> None:
    code = call.data.split(":")[1]
    plan = PREMIUM_PLANS.get(code)
    if plan is None:
        await call.answer("Тариф не найден", show_alert=True)
        return

    await call.message.answer_invoice(
        title=plan["title"],
        description=(f"Статус Premium User ⭐ на {plan['days']} дн.: "
                     f"{PREMIUM_DAILY_VIEWS} анкет в день, приоритет в ленте "
                     f"и звёздочка в анкете."),
        payload=f"premium:{code}",
        currency="XTR",                 # звёзды Telegram
        prices=[LabeledPrice(label=plan["title"], amount=plan["stars"])],
        provider_token="",              # для XTR токен провайдера не нужен
    )
    await call.answer()


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery) -> None:
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def on_paid(message: Message, user) -> None:
    payment = message.successful_payment
    code = payment.invoice_payload.split(":")[-1]
    plan = PREMIUM_PLANS.get(code)
    if plan is None:
        log.error("Неизвестный payload оплаты: %s", payment.invoice_payload)
        await message.answer("Платёж получен, но тариф не распознан. Напишите в поддержку 🆘")
        return

    until = await grant_premium(user["tg_id"], plan["days"])
    await log_payment(user["tg_id"], code, payment.total_amount,
                      payment.telegram_payment_charge_id)

    likes = await pending_likes_count(user["tg_id"])
    await message.answer(PREMIUM_THANKS.format(until=until), reply_markup=main_menu(likes))

    for admin_id in ADMIN_IDS:
        await safe_send(
            message.bot, admin_id,
            f"💰 Оплата: <code>{user['tg_id']}</code> (@{user['username'] or '—'})\n"
            f"Тариф: {plan['title']} — {payment.total_amount} ⭐\n"
            f"charge_id: <code>{payment.telegram_payment_charge_id}</code>",
        )


# ───────────────────────────── ПОДДЕРЖКА ────────────────────────────────────
@router.message(F.text == BTN_SUPPORT)
@router.message(Command("support"))
async def open_support(message: Message, state: FSMContext) -> None:
    await state.set_state(SupportState.waiting_message)
    await message.answer(SUPPORT_ASK, reply_markup=cancel_kb("support:cancel"))


@router.callback_query(F.data == "support:cancel")
async def support_cancel(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    with suppress(TelegramBadRequest):
        await call.message.delete()
    await call.answer(CANCELLED)


@router.message(SupportState.waiting_message)
async def forward_to_admin(message: Message, state: FSMContext, user) -> None:
    await state.clear()
    ticket_id = await add_ticket(user["tg_id"], message.text or message.caption or "[медиа]")

    header = (f"🆘 <b>Обращение #{ticket_id}</b>\n"
              f"От: <code>{user['tg_id']}</code> (@{user['username'] or '—'})")
    for admin_id in ADMIN_IDS:
        await safe_send(message.bot, admin_id, header)
        try:
            await message.forward(admin_id)
        except Exception:
            await safe_send(message.bot, admin_id, message.text or "[медиа]")
        await safe_send(message.bot, admin_id, "Действия 👇",
                        reply_markup=ticket_kb(ticket_id, user["tg_id"]))

    likes = await pending_likes_count(user["tg_id"])
    await message.answer(SUPPORT_SENT, reply_markup=main_menu(likes))


# ───────────────────────────── АДМИН-ПАНЕЛЬ ─────────────────────────────────
def stats_text(data: dict[str, int]) -> str:
    return (
        "🛠 <b>Админ-панель «Орбит Знакомства»</b>\n\n"
        f"👥 Пользователей всего: <b>{data['users']}</b>\n"
        f"📝 Создано анкет: <b>{data['profiles']}</b>\n"
        f"👀 Анкет в поиске: <b>{data['active']}</b>\n\n"
        f"🆕 Новых за сегодня: <b>{data['new_today']}</b>\n"
        f"🆕 Анкет за сегодня: <b>{data['profiles_today']}</b>\n\n"
        f"❤️ Симпатий отправлено: <b>{data['likes']}</b>\n"
        f"👎 Пропусков: <b>{data['dislikes']}</b>\n"
        f"🎉 Взаимных мэтчей: <b>{data['matches']}</b>\n\n"
        f"⭐ Premium-пользователей: <b>{data['premium']}</b>\n"
        f"⭐ Всего звёзд получено: <b>{data['stars']}</b>\n\n"
        f"🚫 В бане: <b>{data['banned']}</b>\n"
        f"🆘 Открытых обращений: <b>{data['tickets_open']}</b>"
    )


@admin_router.message(Command("admin"))
async def admin_panel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(stats_text(await get_stats()), reply_markup=admin_kb())


@admin_router.callback_query(F.data == "admin:stats")
async def refresh_stats(call: CallbackQuery) -> None:
    with suppress(TelegramBadRequest):
        await call.message.edit_text(stats_text(await get_stats()), reply_markup=admin_kb())
    await call.answer("Обновлено")


@admin_router.callback_query(F.data == "admin:broadcast")
async def ask_broadcast(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminStates.broadcast)
    await call.message.answer("📣 Пришлите текст рассылки. Поддерживается HTML-разметка.",
                              reply_markup=cancel_kb("admin:cancel"))
    await call.answer()


@admin_router.message(AdminStates.broadcast, F.text)
async def do_broadcast(message: Message, state: FSMContext) -> None:
    await state.clear()
    user_ids = await all_user_ids()
    status = await message.answer(f"Отправляю… 0/{len(user_ids)}")

    sent = failed = 0
    for index, user_id in enumerate(user_ids, 1):
        ok = await safe_send(message.bot, user_id, message.html_text)
        sent += ok
        failed += not ok
        if index % 25 == 0:
            with suppress(TelegramBadRequest):
                await status.edit_text(f"Отправляю… {index}/{len(user_ids)}")
        await asyncio.sleep(BROADCAST_RATE)

    await status.edit_text(f"✅ Рассылка завершена\nДоставлено: {sent}\nНе доставлено: {failed}")


@admin_router.callback_query(F.data.startswith("admin:reply:"))
async def ask_reply(call: CallbackQuery, state: FSMContext) -> None:
    _, _, user_id, ticket_id = call.data.split(":")
    await state.set_state(AdminStates.reply_to_user)
    await state.update_data(target=int(user_id), ticket=int(ticket_id))
    await call.message.answer(f"✍️ Текст ответа пользователю <code>{user_id}</code>:",
                              reply_markup=cancel_kb("admin:cancel"))
    await call.answer()


@admin_router.message(AdminStates.reply_to_user, F.text)
async def do_reply(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    ok = await safe_send(message.bot, data["target"],
                         SUPPORT_REPLY.format(text=message.html_text))
    if data.get("ticket"):
        await close_ticket(data["ticket"])
    await message.answer("✅ Ответ доставлен" if ok else "❌ Пользователь недоступен")


@admin_router.callback_query(F.data.startswith("admin:ban:"))
async def ban_user(call: CallbackQuery) -> None:
    user_id = int(call.data.split(":")[2])
    await set_banned(user_id, True)
    await safe_send(call.bot, user_id, "🚫 Ваш доступ к боту ограничен администратором.")
    await call.message.answer(f"🚫 Пользователь <code>{user_id}</code> заблокирован")
    await call.answer("Забанен")


@admin_router.callback_query(F.data.startswith("admin:unban:"))
async def unban_user(call: CallbackQuery) -> None:
    user_id = int(call.data.split(":")[2])
    await set_banned(user_id, False)
    await safe_send(call.bot, user_id, "♻️ Доступ к боту восстановлен. Нажмите /start")
    await call.message.answer(f"♻️ Пользователь <code>{user_id}</code> разблокирован")
    await call.answer("Разбанен")


@admin_router.callback_query(F.data == "admin:find")
async def ask_find(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminStates.find_user)
    await call.message.answer("🔎 Пришлите ID пользователя:",
                              reply_markup=cancel_kb("admin:cancel"))
    await call.answer()


@admin_router.message(AdminStates.find_user, F.text)
async def do_find(message: Message, state: FSMContext) -> None:
    await state.clear()
    raw = (message.text or "").strip().lstrip("#")
    if not raw.isdigit():
        await message.answer("Нужен числовой ID.")
        return

    found = await get_user(int(raw))
    if found is None:
        await message.answer("Пользователь не найден.")
        return

    note = (f"ID: <code>{found['tg_id']}</code> · @{found['username'] or '—'}\n"
            f"Premium: {'да ⭐' if found['is_premium'] else 'нет'} · "
            f"Бан: {'да' if found['is_banned'] else 'нет'}\n"
            f"Регистрация: {found['created_at'][:10]}")
    if found["completed_at"]:
        photos = await get_photos(found["tg_id"])
        await send_profile(message.bot, message.chat.id, found, photos, note=note,
                           keyboard=user_manage_kb(found["tg_id"], bool(found["is_banned"])))
    else:
        await message.answer(f"Анкета не заполнена.\n{note}",
                             reply_markup=user_manage_kb(found["tg_id"], bool(found["is_banned"])))


@admin_router.message(Command("refund"))
async def refund(message: Message) -> None:
    """Возврат звёзд: /refund <user_id> <charge_id>"""
    parts = (message.text or "").split()
    if len(parts) != 3:
        await message.answer("Формат: <code>/refund user_id charge_id</code>")
        return
    try:
        await message.bot.refund_star_payment(user_id=int(parts[1]),
                                              telegram_payment_charge_id=parts[2])
        await message.answer("✅ Возврат выполнен")
    except Exception as exc:
        await message.answer(f"❌ Ошибка возврата: {exc}")


@admin_router.callback_query(F.data == "admin:cancel")
async def admin_cancel(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    with suppress(TelegramBadRequest):
        await call.message.delete()
    await call.answer(CANCELLED)


# ═══════════════════════════════════════════════════════════════════════════
#  ЗАПУСК
# ═══════════════════════════════════════════════════════════════════════════

COMMANDS = [
    BotCommand(command="start", description="🪐 Перезапустить бота"),
    BotCommand(command="menu", description="🏠 Главное меню"),
    BotCommand(command="feed", description="🔍 Смотреть анкеты"),
    BotCommand(command="myprofile", description="👤 Моя анкета"),
    BotCommand(command="likes", description="💌 Мои симпатии"),
    BotCommand(command="premium", description="⭐ Premium"),
    BotCommand(command="support", description="🆘 Поддержка"),
]


async def premium_watcher() -> None:
    """Раз в час снимает статус у тех, чья подписка истекла."""
    while True:
        try:
            expired = await expire_premium()
            if expired:
                log.info("Premium снят у %s пользователей", expired)
        except Exception as exc:                       # noqa: BLE001
            log.exception("Ошибка premium_watcher: %s", exc)
        await asyncio.sleep(3600)


async def main() -> None:
    if not BOT_TOKEN or BOT_TOKEN.startswith("123456789:AAE"):
        sys.exit("❌ Впишите BOT_TOKEN в начале файла (получить у @BotFather)")
    if ADMIN_ID == 123456789:
        log.warning("⚠️ ADMIN_ID не изменён — админка и поддержка работать не будут")

    await init_db()

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())

    dp.update.outer_middleware(UserMiddleware())
    dp.message.middleware(ThrottlingMiddleware())
    dp.callback_query.middleware(ThrottlingMiddleware())

    dp.include_router(admin_router)     # админка первой: у неё свои фильтры
    dp.include_router(router)

    await bot.set_my_commands(COMMANDS)
    await bot.delete_webhook(drop_pending_updates=True)

    watcher = asyncio.create_task(premium_watcher())
    me = await bot.get_me()
    log.info("Бот @%s запущен", me.username)

    try:
        await dp.start_polling(bot)
    finally:
        watcher.cancel()
        await close_db()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Остановлено")
