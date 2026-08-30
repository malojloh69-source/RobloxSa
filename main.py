from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import sqlite3
import sys
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ButtonStyle, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

BASE_DIR = Path(__file__).resolve().parent
ASSETS_DIR = BASE_DIR / "assets"
DB_PATH = Path(os.getenv("BOT_DB_PATH", str(BASE_DIR / "bot.sqlite3")))
TOKEN_FILE = BASE_DIR / "bot_token.txt"

ROLE_USER = "user"
ROLE_GUARANTOR = "guarantor"
ROLE_VERIFIED = "verified_guarantor"

ROLE_LABELS = {
    ROLE_USER: "Обычный пользователь",
    ROLE_GUARANTOR: "Просто гарант",
    ROLE_VERIFIED: "Проверенный гарант",
}

ROLE_IMAGES = {
    ROLE_USER: ASSETS_DIR / "ordinary_user.jpg",
    ROLE_GUARANTOR: ASSETS_DIR / "guarantor.jpg",
    ROLE_VERIFIED: ASSETS_DIR / "verified_guarantor.jpg",
}

SCAM_REPORT_URL = "https://t.me/Roblox_Zona"


def custom_emoji(emoji_id: str, fallback: str) -> str:
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'


def without_custom_emoji_markup(text: str) -> str:
    text = re.sub(r'<tg-emoji\s+emoji-id="\d+">', "", text)
    return text.replace("</tg-emoji>", "")


async def answer_with_emoji_fallback(
    message: Message, text: str, **kwargs: Any
) -> Message:
    try:
        return await message.answer(text, **kwargs)
    except TelegramBadRequest:
        if "<tg-emoji" not in text:
            raise
        return await message.answer(without_custom_emoji_markup(text), **kwargs)


WELCOME_TEXT = f"""
<b>{custom_emoji("5190719950062393497", "👋")} Добро пожаловать в №1 Базу по Roblox {custom_emoji("5265000254100487294", "😖")}

{custom_emoji("5375552012219880409", "✅")} Это крупнейший проект, где обычные пользователи могут сливать мошенников, после наш проект помогает другим не попасться на скам.

{custom_emoji("5372981976804366741", "🤖")} Что умеет этот бот:

• {custom_emoji("5375253228524964274", "❌")} проверять репутацию пользователей в сфере, чтобы уменьшить шанс скама
• {custom_emoji("5334882760735598374", "📝")} делать посты через пост-бота (с премиум-стикерами и кнопкой)
• {custom_emoji("5199749070830197566", "🎁")} вы можете воспользоваться фондом и получить помощь в сложной ситуации, мы не помогаем абсолютно каждому, только по возможности

{custom_emoji("5280735858926822987", "🥇")} Мы — самая сильная и активная база по Roblox, созданная сообществом для сообщества.</b>

Нажимай кнопки ниже и начинай {custom_emoji("5445284980978621387", "🚀")}
""".strip()

UNAVAILABLE_TEXT = (
    f"{custom_emoji('5375253228524964274', '❌')} <b>Временно не работает</b>"
)

MENU_POST = "📝 Пост Бот"
MENU_FUND = "⚖️ Фонд"
MENU_GIVEAWAY = "🎁 Раздача - На стриме"
MENU_DATABASE = "⚜️ Карточка Базы"
MENU_SERVICES = "🛍 Услуги"
MENU_QUESTION = "❓ Задать вопрос"

MENU_UNAVAILABLE = {
    MENU_POST,
    MENU_FUND,
    MENU_GIVEAWAY,
    MENU_DATABASE,
    MENU_SERVICES,
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_bot_token() -> str:
    env_token = os.getenv("BOT_TOKEN", "").strip()
    if env_token:
        return env_token

    if TOKEN_FILE.exists():
        saved_token = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if saved_token:
            return saved_token

    if sys.stdin.isatty():
        entered_token = input("Введите токен Telegram-бота: ").strip()
        if not entered_token or ":" not in entered_token:
            raise RuntimeError("Введён некорректный токен")
        TOKEN_FILE.write_text(entered_token, encoding="utf-8")
        try:
            TOKEN_FILE.chmod(0o600)
        except OSError:
            pass
        return entered_token

    raise RuntimeError(
        "Не задан BOT_TOKEN. Установите переменную окружения или создайте bot_token.txt"
    )


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self.connection.executescript(
            """
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;

            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT NOT NULL DEFAULT 'Пользователь',
                role TEXT NOT NULL DEFAULT 'user'
                    CHECK (role IN ('user', 'guarantor', 'verified_guarantor')),
                proof_count INTEGER NOT NULL DEFAULT 0,
                proofs_url TEXT,
                roblox_nick TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_users_username
                ON users(username COLLATE NOCASE);

            CREATE TABLE IF NOT EXISTS admins (
                telegram_id INTEGER PRIMARY KEY,
                is_owner INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                FOREIGN KEY (telegram_id) REFERENCES users(telegram_id)
                    ON DELETE CASCADE
            );
            """
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def upsert_user(self, user: Any) -> None:
        if user is None or getattr(user, "is_bot", False):
            return
        telegram_id = int(user.id)
        username = getattr(user, "username", None)
        first_name = getattr(user, "first_name", None) or "Пользователь"
        self.connection.execute(
            """
            INSERT INTO users (telegram_id, username, first_name, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                updated_at = excluded.updated_at
            """,
            (telegram_id, username, first_name, now_iso()),
        )
        self.connection.commit()

    def ensure_placeholder(self, telegram_id: int) -> None:
        self.connection.execute(
            """
            INSERT OR IGNORE INTO users
                (telegram_id, first_name, updated_at)
            VALUES (?, 'Пользователь', ?)
            """,
            (telegram_id, now_iso()),
        )
        self.connection.commit()

    def get_user(self, telegram_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()

    def get_user_by_username(self, username: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM users WHERE username = ? COLLATE NOCASE",
            (username.lstrip("@"),),
        ).fetchone()

    def resolve_user(self, value: str) -> sqlite3.Row | None:
        value = value.strip()
        if not value:
            return None
        if value.startswith("@"):
            return self.get_user_by_username(value)
        if re.fullmatch(r"\d{4,20}", value):
            telegram_id = int(value)
            self.ensure_placeholder(telegram_id)
            return self.get_user(telegram_id)
        return self.get_user_by_username(value)

    def set_role(self, telegram_id: int, role: str) -> None:
        if role not in ROLE_LABELS:
            raise ValueError("Unknown role")
        self.ensure_placeholder(telegram_id)
        self.connection.execute(
            "UPDATE users SET role = ?, updated_at = ? WHERE telegram_id = ?",
            (role, now_iso(), telegram_id),
        )
        self.connection.commit()

    def set_profile_field(self, telegram_id: int, field: str, value: Any) -> None:
        allowed_fields = {"proof_count", "proofs_url", "roblox_nick"}
        if field not in allowed_fields:
            raise ValueError("Unsupported profile field")
        self.ensure_placeholder(telegram_id)
        self.connection.execute(
            f"UPDATE users SET {field} = ?, updated_at = ? WHERE telegram_id = ?",
            (value, now_iso(), telegram_id),
        )
        self.connection.commit()

    def admin_count(self) -> int:
        row = self.connection.execute("SELECT COUNT(*) AS count FROM admins").fetchone()
        return int(row["count"])

    def is_admin(self, telegram_id: int) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM admins WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
        return row is not None

    def is_owner(self, telegram_id: int) -> bool:
        row = self.connection.execute(
            "SELECT is_owner FROM admins WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
        return bool(row and row["is_owner"])

    def add_admin(self, telegram_id: int, owner: bool = False) -> None:
        self.ensure_placeholder(telegram_id)
        self.connection.execute(
            """
            INSERT INTO admins (telegram_id, is_owner, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                is_owner = MAX(admins.is_owner, excluded.is_owner)
            """,
            (telegram_id, int(owner), now_iso()),
        )
        self.connection.commit()

    def remove_admin(self, telegram_id: int) -> bool:
        if self.is_owner(telegram_id):
            return False
        cursor = self.connection.execute(
            "DELETE FROM admins WHERE telegram_id = ?", (telegram_id,)
        )
        self.connection.commit()
        return cursor.rowcount > 0

    def stats(self) -> dict[str, int]:
        rows = self.connection.execute(
            "SELECT role, COUNT(*) AS count FROM users GROUP BY role"
        ).fetchall()
        result = {role: 0 for role in ROLE_LABELS}
        for row in rows:
            result[row["role"]] = int(row["count"])
        result["admins"] = self.admin_count()
        result["total"] = sum(result[role] for role in ROLE_LABELS)
        return result


db = Database(DB_PATH)
router = Router()


class RememberUsersMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[Any, dict[str, Any]], Awaitable[Any]],
        event: Any,
        data: dict[str, Any],
    ) -> Any:
        db.upsert_user(getattr(event, "from_user", None))
        return await handler(event, data)


class QuestionFlow(StatesGroup):
    waiting_for_question = State()


class AdminFlow(StatesGroup):
    waiting_role_target = State()
    waiting_data_target = State()
    waiting_proof_count = State()
    waiting_proofs_url = State()
    waiting_roblox_nick = State()
    waiting_add_admin = State()
    waiting_remove_admin = State()


def menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=MENU_POST), KeyboardButton(text=MENU_FUND)],
            [KeyboardButton(text=MENU_GIVEAWAY), KeyboardButton(text=MENU_DATABASE)],
            [KeyboardButton(text=MENU_SERVICES)],
            [KeyboardButton(text=MENU_QUESTION)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Выберите действие",
    )


def check_keyboard(
    user: sqlite3.Row, include_custom_icons: bool = True
) -> InlineKeyboardMarkup:
    username = user["username"]
    if username:
        profile_url = f"https://t.me/{username}"
    else:
        profile_url = f"tg://user?id={user['telegram_id']}"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Ссылка на Профиль",
                    url=profile_url,
                    icon_custom_emoji_id=(
                        "5372870973374634436" if include_custom_icons else None
                    ),
                    style=ButtonStyle.PRIMARY,
                )
            ],
            [
                InlineKeyboardButton(
                    text="Слить Скамера",
                    url=SCAM_REPORT_URL,
                    icon_custom_emoji_id=(
                        "5249348160618274954" if include_custom_icons else None
                    ),
                    style=ButtonStyle.DANGER,
                )
            ],
        ]
    )


def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Назначить роль",
                    callback_data="adm:role",
                    style=ButtonStyle.PRIMARY,
                )
            ],
            [
                InlineKeyboardButton(text="Изменить данные", callback_data="adm:data"),
                InlineKeyboardButton(text="Статистика", callback_data="adm:stats"),
            ],
            [
                InlineKeyboardButton(
                    text="Добавить админа", callback_data="adm:add_admin"
                ),
                InlineKeyboardButton(
                    text="Удалить админа", callback_data="adm:remove_admin"
                ),
            ],
            [InlineKeyboardButton(text="Закрыть", callback_data="adm:close")],
        ]
    )


def role_keyboard(target_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Проверенный гарант",
                    callback_data=f"adm:setrole:{target_id}:{ROLE_VERIFIED}",
                    style=ButtonStyle.SUCCESS,
                )
            ],
            [
                InlineKeyboardButton(
                    text="Гарант",
                    callback_data=f"adm:setrole:{target_id}:{ROLE_GUARANTOR}",
                    style=ButtonStyle.PRIMARY,
                )
            ],
            [
                InlineKeyboardButton(
                    text="Обычный пользователь",
                    callback_data=f"adm:setrole:{target_id}:{ROLE_USER}",
                )
            ],
            [InlineKeyboardButton(text="Отмена", callback_data="adm:cancel")],
        ]
    )


def data_keyboard(target_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Количество пруфов",
                    callback_data=f"adm:field:{target_id}:proof_count",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Ссылка на пруфы",
                    callback_data=f"adm:field:{target_id}:proofs_url",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Ник в Roblox",
                    callback_data=f"adm:field:{target_id}:roblox_nick",
                )
            ],
            [InlineKeyboardButton(text="Назад", callback_data="adm:panel")],
        ]
    )


def format_identity(user: sqlite3.Row) -> str:
    username = user["username"]
    if username:
        return f"@{html.escape(username)}"
    name = html.escape(user["first_name"] or "Пользователь")
    return f'<a href="tg://user?id={user["telegram_id"]}">{name}</a>'


def format_check_caption(user: sqlite3.Row) -> str:
    role = user["role"]
    identity = format_identity(user)
    telegram_id = user["telegram_id"]

    if role == ROLE_USER:
        return (
            f"<b>{custom_emoji('5375303587016513290', '🙋‍♂️')} Обычный пользователь "
            f"{custom_emoji('5375303587016513290', '🙋‍♂️')}</b>\n\n"
            f"<b>{custom_emoji('5375189280756894560', '🪪')} Пользователь: {identity}</b>\n"
            f"<b>{custom_emoji('5375196981633260334', '🆔')} id: <code>{telegram_id}</code></b>\n\n"
            f"<b>{custom_emoji('5375404278229798503', '⚜️')} Roblox | Base | Emoji "
            f"{custom_emoji('5375404278229798503', '⚜️')}</b>"
        )

    if role == ROLE_VERIFIED:
        title_icon = "5372875620529245919"
        title = "Проверенный Гарант"
    else:
        title_icon = "5375552012219880409"
        title = "Просто гарант"

    proof_count = int(user["proof_count"] or 0)
    proofs_url = (user["proofs_url"] or "Не указаны").strip()
    roblox_nick = html.escape((user["roblox_nick"] or "Не указан").strip())

    if re.match(r"^https?://", proofs_url, re.IGNORECASE):
        safe_url = html.escape(proofs_url, quote=True)
        proofs_display = f'<a href="{safe_url}">Открыть пруфы</a>'
    else:
        proofs_display = html.escape(proofs_url)

    return (
        f"<b>{custom_emoji(title_icon, '✅')} {title} {custom_emoji(title_icon, '✅')}</b>\n\n"
        f"<b>{custom_emoji('5375189280756894560', '🪪')} Пользователь: {identity}</b>\n"
        f"<b>{custom_emoji('5375196981633260334', '🆔')} id: <code>{telegram_id}</code></b>\n\n"
        f"<b>{custom_emoji('5375132114742186051', '📑')} Всего пруфов: {proof_count}</b>\n"
        f"<b>{custom_emoji('5372870973374634436', '🔗')} Пруфы: {proofs_display}</b>\n"
        f"<b>{custom_emoji('5375416067915029784', '🎲')} Ник в RB: {roblox_nick}</b>\n\n"
        f"<b>{custom_emoji('5375404278229798503', '⚜️')} Roblox | Base | Emoji "
        f"{custom_emoji('5375404278229798503', '⚜️')}</b>"
    )


async def send_check_card(message: Message, user: sqlite3.Row) -> None:
    image_path = ROLE_IMAGES[user["role"]]
    if not image_path.exists():
        await message.answer("Не найден файл изображения для карточки.")
        return
    caption = format_check_caption(user)
    try:
        await message.answer_photo(
            photo=FSInputFile(image_path),
            caption=caption,
            reply_markup=check_keyboard(user),
        )
    except TelegramBadRequest:
        await message.answer_photo(
            photo=FSInputFile(image_path),
            caption=without_custom_emoji_markup(caption),
            reply_markup=check_keyboard(user, include_custom_icons=False),
        )


def command_argument(message: Message) -> str:
    text = message.text or ""
    parts = text.split(maxsplit=1)
    return parts[1].strip() if len(parts) == 2 else ""


def target_from_message(
    message: Message, default_self: bool = True
) -> sqlite3.Row | None:
    if message.reply_to_message and message.reply_to_message.from_user:
        db.upsert_user(message.reply_to_message.from_user)
        return db.get_user(message.reply_to_message.from_user.id)
    argument = command_argument(message)
    if argument:
        return db.resolve_user(argument.split()[0])
    if default_self and message.from_user:
        return db.get_user(message.from_user.id)
    return None


async def require_admin(callback: CallbackQuery) -> bool:
    if db.is_admin(callback.from_user.id):
        return True
    await callback.answer("Доступ запрещён", show_alert=True)
    return False


@router.message(CommandStart(ignore_case=True))
async def on_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await answer_with_emoji_fallback(
        message, WELCOME_TEXT, reply_markup=menu_keyboard()
    )


@router.message(Command("check", ignore_case=True))
async def on_check(message: Message, state: FSMContext) -> None:
    await state.clear()
    user = target_from_message(message)
    if user is None:
        await message.answer(
            "Пользователь пока неизвестен боту. Он должен сначала написать боту или появиться в чате с ботом."
        )
        return
    await send_check_card(message, user)


@router.message(Command("me", ignore_case=True))
async def on_me(message: Message, state: FSMContext) -> None:
    await state.clear()
    user = db.get_user(message.from_user.id)
    if user is not None:
        await send_check_card(message, user)


@router.message(Command("id", ignore_case=True))
async def on_id(message: Message, state: FSMContext) -> None:
    await state.clear()
    user = target_from_message(message)
    if user is None:
        await message.answer(
            "Не удалось определить пользователя. Ответьте командой на его сообщение или укажите известный @username."
        )
        return
    await answer_with_emoji_fallback(
        message,
        f"{custom_emoji('5375196981633260334', '🆔')} ID пользователя "
        f"{format_identity(user)}: <code>{user['telegram_id']}</code>",
    )


@router.message(F.text == MENU_QUESTION)
async def on_question_button(message: Message, state: FSMContext) -> None:
    await state.set_state(QuestionFlow.waiting_for_question)
    await message.answer("✍️ Напиши свой вопрос:")


@router.message(QuestionFlow.waiting_for_question)
async def on_question_received(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "Извините, но я консультирую только по вопросам проекта Roblox Baza | SA. "
        "Если у вас есть вопросы по этому проекту, пожалуйста, задавайте!\n\n"
        "<b>Спасибо, что пользуетесь Roblox Baza | SA.</b>",
        reply_markup=menu_keyboard(),
    )


@router.message(F.text.in_(MENU_UNAVAILABLE))
async def on_unavailable_button(message: Message) -> None:
    await answer_with_emoji_fallback(message, UNAVAILABLE_TEXT)


@router.message(Command("ClezzyKryt", ignore_case=False))
async def on_admin_access(message: Message, state: FSMContext) -> None:
    await state.clear()
    user_id = message.from_user.id
    if db.admin_count() == 0:
        db.add_admin(user_id, owner=True)
    if not db.is_admin(user_id):
        await message.answer("Доступ запрещён.")
        return
    await message.answer(
        "<b>Панель управления</b>\nВыберите действие:", reply_markup=admin_keyboard()
    )


@router.callback_query(F.data == "adm:panel")
async def admin_panel(callback: CallbackQuery, state: FSMContext) -> None:
    if not await require_admin(callback):
        return
    await state.clear()
    if callback.message:
        await callback.message.edit_text(
            "<b>Панель управления</b>\nВыберите действие:",
            reply_markup=admin_keyboard(),
        )
    await callback.answer()


@router.callback_query(F.data == "adm:role")
async def admin_choose_role_target(callback: CallbackQuery, state: FSMContext) -> None:
    if not await require_admin(callback):
        return
    await state.set_state(AdminFlow.waiting_role_target)
    if callback.message:
        await callback.message.edit_text(
            "Отправьте <b>@username</b> или числовой <b>Telegram ID</b> пользователя."
        )
    await callback.answer()


@router.message(AdminFlow.waiting_role_target)
async def admin_role_target_received(message: Message, state: FSMContext) -> None:
    if not db.is_admin(message.from_user.id):
        await state.clear()
        return
    target = db.resolve_user((message.text or "").strip())
    if target is None:
        await message.answer(
            "Пользователь не найден. Он должен сначала написать боту, либо укажите его числовой Telegram ID."
        )
        return
    await state.clear()
    await message.answer(
        f"Выберите роль для {format_identity(target)} (<code>{target['telegram_id']}</code>):",
        reply_markup=role_keyboard(target["telegram_id"]),
    )


@router.callback_query(F.data.startswith("adm:setrole:"))
async def admin_set_role(callback: CallbackQuery) -> None:
    if not await require_admin(callback):
        return
    parts = (callback.data or "").split(":", 3)
    if len(parts) != 4 or not parts[2].isdigit() or parts[3] not in ROLE_LABELS:
        await callback.answer("Некорректные данные", show_alert=True)
        return
    target_id = int(parts[2])
    role = parts[3]
    db.set_role(target_id, role)
    target = db.get_user(target_id)
    if callback.message and target:
        extra = "\nТеперь можно заполнить данные карточки." if role != ROLE_USER else ""
        await callback.message.edit_text(
            f"Роль пользователя {format_identity(target)} изменена на "
            f"<b>{html.escape(ROLE_LABELS[role])}</b>.{extra}",
            reply_markup=data_keyboard(target_id)
            if role != ROLE_USER
            else admin_keyboard(),
        )
    await callback.answer("Роль сохранена")


@router.callback_query(F.data == "adm:data")
async def admin_choose_data_target(callback: CallbackQuery, state: FSMContext) -> None:
    if not await require_admin(callback):
        return
    await state.set_state(AdminFlow.waiting_data_target)
    if callback.message:
        await callback.message.edit_text(
            "Отправьте <b>@username</b> или числовой <b>Telegram ID</b> пользователя."
        )
    await callback.answer()


@router.message(AdminFlow.waiting_data_target)
async def admin_data_target_received(message: Message, state: FSMContext) -> None:
    if not db.is_admin(message.from_user.id):
        await state.clear()
        return
    target = db.resolve_user((message.text or "").strip())
    if target is None:
        await message.answer(
            "Пользователь не найден. Укажите другой @username или Telegram ID."
        )
        return
    await state.clear()
    await message.answer(
        f"Что изменить у {format_identity(target)}?",
        reply_markup=data_keyboard(target["telegram_id"]),
    )


@router.callback_query(F.data.startswith("adm:field:"))
async def admin_select_field(callback: CallbackQuery, state: FSMContext) -> None:
    if not await require_admin(callback):
        return
    parts = (callback.data or "").split(":", 3)
    if len(parts) != 4 or not parts[2].isdigit():
        await callback.answer("Некорректные данные", show_alert=True)
        return
    target_id = int(parts[2])
    field = parts[3]
    await state.update_data(target_id=target_id)

    prompts = {
        "proof_count": (
            AdminFlow.waiting_proof_count,
            "Введите количество пруфов целым числом:",
        ),
        "proofs_url": (
            AdminFlow.waiting_proofs_url,
            "Отправьте ссылку на пруфы (http/https) или <code>-</code>, чтобы очистить:",
        ),
        "roblox_nick": (
            AdminFlow.waiting_roblox_nick,
            "Введите ник пользователя в Roblox или <code>-</code>, чтобы очистить:",
        ),
    }
    if field not in prompts:
        await callback.answer("Неизвестное поле", show_alert=True)
        return
    next_state, prompt = prompts[field]
    await state.set_state(next_state)
    if callback.message:
        await callback.message.edit_text(prompt)
    await callback.answer()


async def finish_field_edit(
    message: Message, state: FSMContext, field: str, value: Any
) -> None:
    data = await state.get_data()
    target_id = int(data["target_id"])
    db.set_profile_field(target_id, field, value)
    target = db.get_user(target_id)
    await state.clear()
    await message.answer(
        "Данные сохранены.",
        reply_markup=data_keyboard(target_id),
    )
    if target:
        await send_check_card(message, target)


@router.message(AdminFlow.waiting_proof_count)
async def admin_proof_count_received(message: Message, state: FSMContext) -> None:
    if not db.is_admin(message.from_user.id):
        await state.clear()
        return
    raw = (message.text or "").strip()
    if not raw.isdigit() or int(raw) > 1_000_000:
        await message.answer("Введите целое число от 0 до 1000000.")
        return
    await finish_field_edit(message, state, "proof_count", int(raw))


@router.message(AdminFlow.waiting_proofs_url)
async def admin_proofs_url_received(message: Message, state: FSMContext) -> None:
    if not db.is_admin(message.from_user.id):
        await state.clear()
        return
    raw = (message.text or "").strip()
    if raw == "-":
        value = None
    elif re.match(r"^https?://[^\s]+$", raw, re.IGNORECASE):
        value = raw
    else:
        await message.answer("Ссылка должна начинаться с http:// или https://")
        return
    await finish_field_edit(message, state, "proofs_url", value)


@router.message(AdminFlow.waiting_roblox_nick)
async def admin_roblox_nick_received(message: Message, state: FSMContext) -> None:
    if not db.is_admin(message.from_user.id):
        await state.clear()
        return
    raw = (message.text or "").strip()
    if len(raw) > 50:
        await message.answer("Ник слишком длинный. Максимум 50 символов.")
        return
    await finish_field_edit(message, state, "roblox_nick", None if raw == "-" else raw)


@router.callback_query(F.data == "adm:stats")
async def admin_stats(callback: CallbackQuery) -> None:
    if not await require_admin(callback):
        return
    stats = db.stats()
    text = (
        "<b>Статистика базы</b>\n\n"
        f"Всего пользователей: <b>{stats['total']}</b>\n"
        f"Проверенных гарантов: <b>{stats[ROLE_VERIFIED]}</b>\n"
        f"Гарантов: <b>{stats[ROLE_GUARANTOR]}</b>\n"
        f"Обычных пользователей: <b>{stats[ROLE_USER]}</b>\n"
        f"Администраторов: <b>{stats['admins']}</b>"
    )
    if callback.message:
        await callback.message.edit_text(text, reply_markup=admin_keyboard())
    await callback.answer()


@router.callback_query(F.data == "adm:add_admin")
async def admin_add_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    if not await require_admin(callback):
        return
    if not db.is_owner(callback.from_user.id):
        await callback.answer(
            "Только владелец может менять администраторов", show_alert=True
        )
        return
    await state.set_state(AdminFlow.waiting_add_admin)
    if callback.message:
        await callback.message.edit_text(
            "Отправьте @username или Telegram ID нового администратора."
        )
    await callback.answer()


@router.message(AdminFlow.waiting_add_admin)
async def admin_add_received(message: Message, state: FSMContext) -> None:
    if not db.is_owner(message.from_user.id):
        await state.clear()
        return
    target = db.resolve_user((message.text or "").strip())
    if target is None:
        await message.answer("Пользователь не найден. Укажите числовой Telegram ID.")
        return
    db.add_admin(target["telegram_id"])
    await state.clear()
    await message.answer(
        f"{format_identity(target)} добавлен в администраторы.",
        reply_markup=admin_keyboard(),
    )


@router.callback_query(F.data == "adm:remove_admin")
async def admin_remove_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    if not await require_admin(callback):
        return
    if not db.is_owner(callback.from_user.id):
        await callback.answer(
            "Только владелец может менять администраторов", show_alert=True
        )
        return
    await state.set_state(AdminFlow.waiting_remove_admin)
    if callback.message:
        await callback.message.edit_text(
            "Отправьте @username или Telegram ID администратора для удаления."
        )
    await callback.answer()


@router.message(AdminFlow.waiting_remove_admin)
async def admin_remove_received(message: Message, state: FSMContext) -> None:
    if not db.is_owner(message.from_user.id):
        await state.clear()
        return
    target = db.resolve_user((message.text or "").strip())
    if target is None or not db.is_admin(target["telegram_id"]):
        await message.answer("Такой администратор не найден.")
        return
    if not db.remove_admin(target["telegram_id"]):
        await message.answer("Владельца удалить нельзя.")
        return
    await state.clear()
    await message.answer(
        f"{format_identity(target)} удалён из администраторов.",
        reply_markup=admin_keyboard(),
    )


@router.callback_query(F.data.in_({"adm:cancel", "adm:close"}))
async def admin_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    if not await require_admin(callback):
        return
    await state.clear()
    if callback.message:
        if callback.data == "adm:close":
            await callback.message.edit_text("Панель управления закрыта.")
        else:
            await callback.message.edit_text(
                "<b>Панель управления</b>\nВыберите действие:",
                reply_markup=admin_keyboard(),
            )
    await callback.answer()


async def configure_commands(bot: Bot) -> None:
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Открыть главное меню"),
            BotCommand(command="check", description="Проверить пользователя"),
            BotCommand(command="me", description="Проверить себя"),
            BotCommand(command="id", description="Узнать Telegram ID"),
        ]
    )


async def main() -> None:
    bot_token = load_bot_token()
    if ":" not in bot_token:
        raise RuntimeError("Не задан корректный BOT_TOKEN")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    bot = Bot(token=bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dispatcher = Dispatcher(storage=MemoryStorage())
    router.message.outer_middleware(RememberUsersMiddleware())
    router.callback_query.outer_middleware(RememberUsersMiddleware())
    dispatcher.include_router(router)

    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await configure_commands(bot)
        await dispatcher.start_polling(
            bot, allowed_updates=dispatcher.resolve_used_update_types()
        )
    finally:
        db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
