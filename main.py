import os
import json
import uuid
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional, List

from dotenv import load_dotenv

from aiohttp import web
import aiohttp
from aiohttp import BasicAuth

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

from openai import OpenAI

import psycopg
from psycopg.rows import dict_row


load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("posterai-bot")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID")
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY")
YOOKASSA_RETURN_URL = os.getenv("YOOKASSA_RETURN_URL")
YOOKASSA_WEBHOOK_URL = os.getenv("YOOKASSA_WEBHOOK_URL")

APP_HOST = os.getenv("APP_HOST", "0.0.0.0")
APP_PORT = int(os.getenv("PORT", os.getenv("APP_PORT", "8000")))

MODEL_NAME = "gpt-4o-mini"
FREE_LIMIT = 10
TELEGRAM_BOT_USERNAME = "PosteraAI_bot"
ADMIN_USERNAME = "babajanyanak"

if not TELEGRAM_BOT_TOKEN:
    raise ValueError("Не найден TELEGRAM_BOT_TOKEN")
if not OPENAI_API_KEY:
    raise ValueError("Не найден OPENAI_API_KEY")
if not DATABASE_URL:
    raise ValueError("Не найден DATABASE_URL")
if not YOOKASSA_SHOP_ID:
    raise ValueError("Не найден YOOKASSA_SHOP_ID")
if not YOOKASSA_SECRET_KEY:
    raise ValueError("Не найден YOOKASSA_SECRET_KEY")
if not YOOKASSA_RETURN_URL:
    raise ValueError("Не найден YOOKASSA_RETURN_URL")

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
client = OpenAI(api_key=OPENAI_API_KEY)


# =========================
# FSM
# =========================

class BotStates(StatesGroup):
    waiting_post_prompt = State()
    waiting_ideas_prompt = State()
    waiting_rewrite_text = State()

    waiting_refine_custom = State()
    waiting_refine_audience = State()

    waiting_style_sample = State()


# =========================
# CONSTANTS
# =========================

FREE_REFINES = 2
MEMORY_LIMIT_PER_CATEGORY = 20

CATEGORY_POST = "post"
CATEGORY_IDEAS = "ideas"
CATEGORY_REWRITE = "rewrite"

TARIFFS = {
    "creator_monthly": {
        "title": "Creator",
        "price": Decimal("300.00"),
        "type": "subscription",
        "balance_add": 100,
        "days": 30,
        "description": "PosteraAI Creator — 100 генераций на 30 дней",
    },
    "unlim_monthly": {
        "title": "Unlim",
        "price": Decimal("800.00"),
        "type": "subscription",
        "balance_add": 999999,
        "days": 30,
        "description": "PosteraAI Unlim — безлимит на 30 дней",
    },
    "pack_50": {
        "title": "+50 генераций",
        "price": Decimal("99.00"),
        "type": "package",
        "balance_add": 50,
        "days": None,
        "description": "PosteraAI — пакет 50 генераций",
    },
    "pack_100": {
        "title": "+100 генераций",
        "price": Decimal("179.00"),
        "type": "package",
        "balance_add": 100,
        "days": None,
        "description": "PosteraAI — пакет 100 генераций",
    },
}

HIDE_UNLIM_FOR = {"babajanyanak"}


# =========================
# DB
# =========================

def get_conn():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id BIGINT PRIMARY KEY,
                    username TEXT,
                    balance INTEGER NOT NULL DEFAULT 10,
                    tariff_code TEXT NOT NULL DEFAULT 'free',
                    tariff_started_at TIMESTAMPTZ,
                    tariff_expires_at TIMESTAMPTZ,
                    memory_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS user_history (
                    id BIGSERIAL PRIMARY KEY,
                    telegram_id BIGINT NOT NULL,
                    category TEXT NOT NULL,
                    role TEXT NOT NULL,
                    text TEXT NOT NULL,
                    is_final BOOLEAN NOT NULL DEFAULT FALSE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS user_style_samples (
                    id BIGSERIAL PRIMARY KEY,
                    telegram_id BIGINT NOT NULL,
                    text TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS user_category_preferences (
                    id BIGSERIAL PRIMARY KEY,
                    telegram_id BIGINT NOT NULL,
                    category TEXT NOT NULL,
                    preference_key TEXT NOT NULL,
                    preference_value TEXT NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (telegram_id, category, preference_key)
                );
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS generation_sessions (
                    id BIGSERIAL PRIMARY KEY,
                    telegram_id BIGINT NOT NULL,
                    category TEXT NOT NULL,
                    original_prompt TEXT,
                    current_text TEXT NOT NULL,
                    final_text TEXT NOT NULL,
                    refines_used INTEGER NOT NULL DEFAULT 0,
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS payments (
                    id BIGSERIAL PRIMARY KEY,
                    payment_id TEXT UNIQUE NOT NULL,
                    telegram_id BIGINT NOT NULL,
                    username TEXT,
                    product_code TEXT NOT NULL,
                    amount NUMERIC(10,2) NOT NULL,
                    currency TEXT NOT NULL DEFAULT 'RUB',
                    status TEXT NOT NULL,
                    is_test BOOLEAN NOT NULL DEFAULT FALSE,
                    idempotence_key TEXT NOT NULL,
                    confirmation_url TEXT,
                    raw_payload JSONB,
                    paid_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """
            )

            conn.commit()


def ensure_user(user_id: int, username: Optional[str]):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (telegram_id, username, balance, tariff_code)
                VALUES (%s, %s, %s, 'free')
                ON CONFLICT (telegram_id)
                DO UPDATE SET
                    username = EXCLUDED.username,
                    updated_at = NOW();
                """,
                (user_id, username, FREE_LIMIT),
            )
            conn.commit()


def get_user_row(user_id: int) -> dict:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM users WHERE telegram_id = %s",
                (user_id,),
            )
            row = cur.fetchone()
            if not row:
                raise ValueError("User not found")
            return row


def get_balance(user_id: int) -> int:
    return int(get_user_row(user_id)["balance"])


def has_balance(user_id: int) -> bool:
    return get_balance(user_id) > 0


def decrease_balance(user_id: int, amount: int = 1):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE users
                SET balance = GREATEST(balance - %s, 0),
                    updated_at = NOW()
                WHERE telegram_id = %s
                """,
                (amount, user_id),
            )
            conn.commit()


def add_balance(user_id: int, amount: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE users
                SET balance = balance + %s,
                    updated_at = NOW()
                WHERE telegram_id = %s
                """,
                (amount, user_id),
            )
            conn.commit()


def set_balance(user_id: int, amount: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE users
                SET balance = %s,
                    updated_at = NOW()
                WHERE telegram_id = %s
                """,
                (amount, user_id),
            )
            conn.commit()


def set_memory_enabled(user_id: int, enabled: bool):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE users
                SET memory_enabled = %s,
                    updated_at = NOW()
                WHERE telegram_id = %s
                """,
                (enabled, user_id),
            )
            conn.commit()


def reset_user_memory(user_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM user_history WHERE telegram_id = %s", (user_id,))
            cur.execute("DELETE FROM user_style_samples WHERE telegram_id = %s", (user_id,))
            cur.execute("DELETE FROM user_category_preferences WHERE telegram_id = %s", (user_id,))
            conn.commit()


def add_style_sample(user_id: int, text: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO user_style_samples (telegram_id, text)
                VALUES (%s, %s)
                """,
                (user_id, text),
            )
            conn.commit()


def clear_style_samples(user_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM user_style_samples WHERE telegram_id = %s",
                (user_id,),
            )
            conn.commit()


def get_style_samples(user_id: int) -> List[str]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT text
                FROM user_style_samples
                WHERE telegram_id = %s
                ORDER BY id ASC
                """,
                (user_id,),
            )
            rows = cur.fetchall() or []
            return [r["text"] for r in rows]


def save_history(user_id: int, category: str, role: str, text: str, is_final: bool = False):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO user_history (telegram_id, category, role, text, is_final)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (user_id, category, role, text, is_final),
            )
            conn.commit()


def get_history(user_id: int, category: str, limit: int = MEMORY_LIMIT_PER_CATEGORY) -> List[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT role, text, is_final, created_at
                FROM user_history
                WHERE telegram_id = %s AND category = %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (user_id, category, limit),
            )
            rows = cur.fetchall() or []
            rows.reverse()
            return rows


def save_preference(user_id: int, category: str, key: str, value: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO user_category_preferences (
                    telegram_id, category, preference_key, preference_value
                )
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (telegram_id, category, preference_key)
                DO UPDATE SET
                    preference_value = EXCLUDED.preference_value,
                    updated_at = NOW()
                """,
                (user_id, category, key, value),
            )
            conn.commit()


def get_preferences(user_id: int, category: str) -> List[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT preference_key, preference_value
                FROM user_category_preferences
                WHERE telegram_id = %s AND category = %s
                ORDER BY updated_at DESC
                """,
                (user_id, category),
            )
            return cur.fetchall() or []


def create_generation_session(user_id: int, category: str, original_prompt: str, text: str) -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE generation_sessions
                SET is_active = FALSE,
                    updated_at = NOW()
                WHERE telegram_id = %s
                """,
                (user_id,),
            )

            cur.execute(
                """
                INSERT INTO generation_sessions (
                    telegram_id, category, original_prompt, current_text, final_text, refines_used, is_active
                )
                VALUES (%s, %s, %s, %s, %s, 0, TRUE)
                RETURNING id
                """,
                (user_id, category, original_prompt, text, text),
            )
            row = cur.fetchone()
            conn.commit()
            return int(row["id"])


def get_active_session(user_id: int) -> Optional[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM generation_sessions
                WHERE telegram_id = %s AND is_active = TRUE
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (user_id,),
            )
            return cur.fetchone()


def update_session_text(session_id: int, new_text: str, increase_refine: bool):
    with get_conn() as conn:
        with conn.cursor() as cur:
            if increase_refine:
                cur.execute(
                    """
                    UPDATE generation_sessions
                    SET current_text = %s,
                        final_text = %s,
                        refines_used = refines_used + 1,
                        updated_at = NOW()
                    WHERE id = %s
                    """,
                    (new_text, new_text, session_id),
                )
            else:
                cur.execute(
                    """
                    UPDATE generation_sessions
                    SET current_text = %s,
                        final_text = %s,
                        updated_at = NOW()
                    WHERE id = %s
                    """,
                    (new_text, new_text, session_id),
                )
            conn.commit()


def close_active_session(user_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE generation_sessions
                SET is_active = FALSE,
                    updated_at = NOW()
                WHERE telegram_id = %s AND is_active = TRUE
                """,
                (user_id,),
            )
            conn.commit()


def get_payment(payment_id: str) -> Optional[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM payments WHERE payment_id = %s", (payment_id,))
            return cur.fetchone()


def save_payment(
    *,
    payment_id: str,
    telegram_id: int,
    username: Optional[str],
    product_code: str,
    amount: Decimal,
    status: str,
    is_test: bool,
    idempotence_key: str,
    confirmation_url: Optional[str],
    raw_payload: dict,
):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO payments (
                    payment_id, telegram_id, username, product_code, amount,
                    currency, status, is_test, idempotence_key,
                    confirmation_url, raw_payload
                )
                VALUES (%s, %s, %s, %s, %s, 'RUB', %s, %s, %s, %s, %s)
                ON CONFLICT (payment_id)
                DO UPDATE SET
                    status = EXCLUDED.status,
                    confirmation_url = EXCLUDED.confirmation_url,
                    raw_payload = EXCLUDED.raw_payload;
                """,
                (
                    payment_id,
                    telegram_id,
                    username,
                    product_code,
                    amount,
                    status,
                    is_test,
                    idempotence_key,
                    confirmation_url,
                    json.dumps(raw_payload, ensure_ascii=False),
                ),
            )
            conn.commit()


def update_payment_status(payment_id: str, status: str, raw_payload: dict, paid: bool = False):
    with get_conn() as conn:
        with conn.cursor() as cur:
            if paid:
                cur.execute(
                    """
                    UPDATE payments
                    SET status = %s,
                        raw_payload = %s,
                        paid_at = NOW()
                    WHERE payment_id = %s
                    """,
                    (status, json.dumps(raw_payload, ensure_ascii=False), payment_id),
                )
            else:
                cur.execute(
                    """
                    UPDATE payments
                    SET status = %s,
                        raw_payload = %s
                    WHERE payment_id = %s
                    """,
                    (status, json.dumps(raw_payload, ensure_ascii=False), payment_id),
                )
            conn.commit()


def activate_subscription(user_id: int, tariff_code: str, balance_add: int, days: int):
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=days)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE users
                SET tariff_code = %s,
                    tariff_started_at = %s,
                    tariff_expires_at = %s,
                    balance = %s,
                    updated_at = NOW()
                WHERE telegram_id = %s
                """,
                (tariff_code, now, expires_at, balance_add, user_id),
            )
            conn.commit()


# =========================
# UI
# =========================

def can_show_unlim(username: Optional[str]) -> bool:
    return (username or "").lower() not in HIDE_UNLIM_FOR


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="✍️ Сгенерировать пост"), KeyboardButton(text="💡 Идеи постов")],
            [KeyboardButton(text="♻️ Переписать текст"), KeyboardButton(text="📊 Остаток генераций")],
            [KeyboardButton(text="🕘 Настройка бота"), KeyboardButton(text="💳 Тарифы")],
            [KeyboardButton(text="👤 Мой тариф")],
        ],
        resize_keyboard=True,
    )


def back_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="⬅️ Назад"), KeyboardButton(text="🏠 Главное меню"), KeyboardButton(text="❌ Отмена")]
        ],
        resize_keyboard=True,
    )


def tariffs_keyboard(username: Optional[str]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="⭐ Creator — 300 ₽ / месяц", callback_data="buy:creator_monthly")],
        [InlineKeyboardButton(text="+50 генераций — 99 ₽", callback_data="buy:pack_50")],
        [InlineKeyboardButton(text="+100 генераций — 179 ₽", callback_data="buy:pack_100")],
    ]

    if can_show_unlim(username):
        rows.insert(1, [InlineKeyboardButton(text="🔥 Unlim — 800 ₽ / месяц", callback_data="buy:unlim_monthly")])

    rows.append(
        [
            InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:menu"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def settings_keyboard(user: dict) -> InlineKeyboardMarkup:
    memory_label = "✅ Память включена" if user["memory_enabled"] else "⛔ Память выключена"

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=memory_label, callback_data="settings:memory_toggle")],
            [InlineKeyboardButton(text="♻️ Сбросить память", callback_data="settings:memory_reset")],
            [InlineKeyboardButton(text="✍️ Копировать стиль", callback_data="settings:style_copy")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:menu")],
        ]
    )


def style_copy_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить ещё", callback_data="style:add_more")],
            [InlineKeyboardButton(text="♻️ Сбросить", callback_data="style:reset")],
            [InlineKeyboardButton(text="✅ Финал", callback_data="style:finish")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:menu")],
        ]
    )


def post_refine_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✂️ Короче", callback_data="refine:post:shorter"),
                InlineKeyboardButton(text="💰 Продающим", callback_data="refine:post:sales"),
            ],
            [
                InlineKeyboardButton(text="✨ Живее", callback_data="refine:post:lively"),
                InlineKeyboardButton(text="📣 С CTA", callback_data="refine:post:cta"),
            ],
            [
                InlineKeyboardButton(text="📱 Под Telegram", callback_data="refine:post:telegram"),
                InlineKeyboardButton(text="🎯 Для ЦА", callback_data="refine:post:audience"),
            ],
            [
                InlineKeyboardButton(text="🔁 Другой вариант", callback_data="refine:post:alt"),
                InlineKeyboardButton(text="✏️ Своя правка", callback_data="refine:post:custom"),
            ],
            [
                InlineKeyboardButton(text="⬅️ Назад", callback_data="nav:back"),
                InlineKeyboardButton(text="🏠 Меню", callback_data="nav:menu"),
                InlineKeyboardButton(text="❌ Отмена", callback_data="nav:cancel"),
            ],
        ]
    )


def ideas_refine_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✂️ Короче", callback_data="refine:ideas:shorter"),
                InlineKeyboardButton(text="💰 Продающим", callback_data="refine:ideas:sales"),
            ],
            [
                InlineKeyboardButton(text="✨ Живее", callback_data="refine:ideas:lively"),
                InlineKeyboardButton(text="📣 С CTA", callback_data="refine:ideas:cta"),
            ],
            [
                InlineKeyboardButton(text="📱 Под Telegram", callback_data="refine:ideas:telegram"),
                InlineKeyboardButton(text="🎯 Для ЦА", callback_data="refine:ideas:audience"),
            ],
            [
                InlineKeyboardButton(text="🧠 Глубже", callback_data="refine:ideas:deeper"),
                InlineKeyboardButton(text="⚡ Смелее", callback_data="refine:ideas:bolder"),
            ],
            [
                InlineKeyboardButton(text="🔁 Другой вариант", callback_data="refine:ideas:alt"),
                InlineKeyboardButton(text="✏️ Своя правка", callback_data="refine:ideas:custom"),
            ],
            [
                InlineKeyboardButton(text="⬅️ Назад", callback_data="nav:back"),
                InlineKeyboardButton(text="🏠 Меню", callback_data="nav:menu"),
                InlineKeyboardButton(text="❌ Отмена", callback_data="nav:cancel"),
            ],
        ]
    )


def rewrite_refine_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✂️ Короче", callback_data="refine:rewrite:shorter"),
                InlineKeyboardButton(text="💰 Продающим", callback_data="refine:rewrite:sales"),
            ],
            [
                InlineKeyboardButton(text="✨ Живее", callback_data="refine:rewrite:lively"),
                InlineKeyboardButton(text="📣 С CTA", callback_data="refine:rewrite:cta"),
            ],
            [
                InlineKeyboardButton(text="📱 Под Telegram", callback_data="refine:rewrite:telegram"),
                InlineKeyboardButton(text="🎯 Для ЦА", callback_data="refine:rewrite:audience"),
            ],
            [
                InlineKeyboardButton(text="🧼 Чище", callback_data="refine:rewrite:cleaner"),
                InlineKeyboardButton(text="🧱 Структурнее", callback_data="refine:rewrite:structured"),
            ],
            [
                InlineKeyboardButton(text="🔁 Другой вариант", callback_data="refine:rewrite:alt"),
                InlineKeyboardButton(text="✏️ Своя правка", callback_data="refine:rewrite:custom"),
            ],
            [
                InlineKeyboardButton(text="⬅️ Назад", callback_data="nav:back"),
                InlineKeyboardButton(text="🏠 Меню", callback_data="nav:menu"),
                InlineKeyboardButton(text="❌ Отмена", callback_data="nav:cancel"),
            ],
        ]
    )


def keyboard_for_category(category: str) -> InlineKeyboardMarkup:
    if category == CATEGORY_POST:
        return post_refine_keyboard()
    if category == CATEGORY_IDEAS:
        return ideas_refine_keyboard()
    return rewrite_refine_keyboard()


def payment_link_keyboard(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 Перейти к оплате", url=url)],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:menu")],
        ]
    )


def format_tariff(user: dict) -> str:
    tariff_code = user.get("tariff_code") or "free"
    balance = int(user.get("balance") or 0)
    expires_at = user.get("tariff_expires_at")

    tariff_map = {
        "free": "Free",
        "creator_monthly": "Creator",
        "unlim_monthly": "Unlim",
    }

    expires_text = "—"
    if expires_at:
        expires_text = expires_at.strftime("%d.%m.%Y %H:%M")

    return (
        "👤 Мой тариф\n\n"
        f"Текущий тариф: {tariff_map.get(tariff_code, tariff_code)}\n"
        f"Остаток генераций: {balance}\n"
        f"Действует до: {expires_text}"
    )


# =========================
# GENERATION HELPERS
# =========================

def build_system_prompt(category: str, user_id: int) -> str:
    user = get_user_row(user_id)
    memory_enabled = bool(user.get("memory_enabled", False))
    style_samples = get_style_samples(user_id)
    preferences = get_preferences(user_id, category)

    category_titles = {
        CATEGORY_POST: "Написание постов для Telegram-канала",
        CATEGORY_IDEAS: "Генерация идей постов для Telegram-канала",
        CATEGORY_REWRITE: "Переписывание и улучшение текста для Telegram-канала",
    }

    parts = [
        f"Ты AI-редактор для Telegram-каналов. Режим: {category_titles.get(category, category)}.",
        "Пиши по-русски. Ответ должен быть полезным, живым и применимым.",
    ]

    if memory_enabled:
        history = get_history(user_id, category, MEMORY_LIMIT_PER_CATEGORY)
        if history:
            parts.append("Учитывай предыдущие предпочтения пользователя в этой категории.")

            final_texts = [h["text"] for h in history if h["is_final"]]
            if final_texts:
                parts.append("Последние финальные варианты пользователя:")
                for text in final_texts[-5:]:
                    parts.append(f"- {text[:500]}")

    if preferences:
        parts.append("Предпочтения пользователя по tone of voice:")
        for pref in preferences[:10]:
            parts.append(f"- {pref['preference_key']}: {pref['preference_value']}")

    if style_samples:
        parts.append("Примеры стиля пользователя:")
        for sample in style_samples[:5]:
            parts.append(f"- {sample[:500]}")

    return "\n".join(parts)


async def generate_ai_text(category: str, user_id: int, prompt: str) -> str:
    full_input = f"{build_system_prompt(category, user_id)}\n\nЗапрос пользователя:\n{prompt}"

    response = await asyncio.to_thread(
        lambda: client.responses.create(
            model=MODEL_NAME,
            input=full_input,
        )
    )
    return response.output_text.strip()


async def apply_refinement(category: str, user_id: int, current_text: str, instruction: str) -> str:
    prompt = (
        f"Текущий текст:\n{current_text}\n\n"
        f"Задача: {instruction}\n\n"
        "Сохрани смысл, но переработай текст согласно задаче."
    )
    return await generate_ai_text(category, user_id, prompt)


def should_charge_refine(session_row: dict) -> bool:
    used = int(session_row["refines_used"] or 0)
    return used >= FREE_REFINES


def save_final_from_session(user_id: int, category: str):
    session_row = get_active_session(user_id)
    if not session_row:
        return

    final_text = session_row["final_text"]
    save_history(user_id, category, "assistant", final_text, is_final=True)


async def start_generation_flow(
    message: Message,
    category: str,
    user_prompt: str,
):
    user_id = message.from_user.id
    username = message.from_user.username

    ensure_user(user_id, username)

    if not has_balance(user_id):
        await message.answer(
            "Сейчас лимит генераций закончился ✨\n\n"
            "Можно подключить тариф или докупить пакет генераций 👇",
            reply_markup=main_menu(),
        )
        await message.answer(
            "Выбери подходящий вариант:",
            reply_markup=tariffs_keyboard(username),
        )
        return

    wait_msg = await message.answer("Собираю вариант… ✍️")

    text = await generate_ai_text(category, user_id, user_prompt)

    decrease_balance(user_id, 1)

    save_history(user_id, category, "user", user_prompt)
    save_history(user_id, category, "assistant", text, is_final=False)
    create_generation_session(user_id, category, user_prompt, text)

    try:
        await wait_msg.edit_text("Готово ✅")
    except Exception:
        pass

    intro_map = {
        CATEGORY_POST: "Вот вариант поста:\n\n",
        CATEGORY_IDEAS: "Вот идеи:\n\n",
        CATEGORY_REWRITE: "Вот улучшенный вариант:\n\n",
    }

    await message.answer(
        f"{intro_map.get(category, '')}{text}",
        reply_markup=keyboard_for_category(category),
    )

    await message.answer(
        "Первые 2 доработки бесплатны.\nНачиная с 3-й списывается генерация.",
        reply_markup=main_menu(),
    )

    # =========================
# COMMANDS / MAIN MENU
# =========================

@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    ensure_user(message.from_user.id, message.from_user.username)

    await message.answer(
        "Привет 👋\n\n"
        "Я PosterAI — AI-редактор для Telegram-каналов.\n\n"
        "Помогаю создавать контент:\n"
        "✍️ пишу посты\n"
        "💡 генерирую идеи\n"
        "♻️ переписываю тексты\n\n"
        "Можно быстро доработать любой результат и обучить меня своему стилю.\n\n"
        "Выбирай действие 👇",
        reply_markup=main_menu(),
    )


@dp.message(Command("flush_9147"))
async def hidden_reset(message: Message):
    username = (message.from_user.username or "").lower()
    if username != ADMIN_USERNAME.lower():
        return

    ensure_user(message.from_user.id, message.from_user.username)
    set_balance(message.from_user.id, FREE_LIMIT)
    await message.answer("Лимит сброшен 👌", reply_markup=main_menu())


@dp.message(F.text == "🏠 Главное меню")
async def go_main_menu(message: Message, state: FSMContext):
    await state.clear()
    close_active_session(message.from_user.id)
    await message.answer("Главное меню 👇", reply_markup=main_menu())


@dp.message(F.text == "❌ Отмена")
async def cancel_any(message: Message, state: FSMContext):
    await state.clear()
    close_active_session(message.from_user.id)
    await message.answer("Ок, остановились 👌", reply_markup=main_menu())


@dp.message(F.text == "⬅️ Назад")
async def back_generic(message: Message, state: FSMContext):
    current = await state.get_state()

    if current == BotStates.waiting_post_prompt.state:
        await state.clear()
        await message.answer("Вернулись назад.", reply_markup=main_menu())
        return

    if current == BotStates.waiting_ideas_prompt.state:
        await state.clear()
        await message.answer("Вернулись назад.", reply_markup=main_menu())
        return

    if current == BotStates.waiting_rewrite_text.state:
        await state.clear()
        await message.answer("Вернулись назад.", reply_markup=main_menu())
        return

    if current in (BotStates.waiting_refine_custom.state, BotStates.waiting_refine_audience.state):
        await state.clear()
        session_row = get_active_session(message.from_user.id)
        if session_row:
            await message.answer(
                f"Возвращаю тебя к текущему результату:\n\n{session_row['current_text']}",
                reply_markup=keyboard_for_category(session_row["category"]),
            )
        else:
            await message.answer("Главное меню 👇", reply_markup=main_menu())
        return

    if current == BotStates.waiting_style_sample.state:
        await state.clear()
        ensure_user(message.from_user.id, message.from_user.username)
        user = get_user_row(message.from_user.id)
        await message.answer("Здесь можно настроить поведение бота.", reply_markup=main_menu())
        await message.answer("Настройки 👇", reply_markup=settings_keyboard(user))
        return

    await state.clear()
    await message.answer("Главное меню 👇", reply_markup=main_menu())


@dp.message(F.text == "✍️ Сгенерировать пост")
async def start_post_flow(message: Message, state: FSMContext):
    await state.set_state(BotStates.waiting_post_prompt)
    await message.answer(
        "Напиши тему поста или короткий бриф.\n\n"
        "Можно указать:\n"
        "• тему\n"
        "• продукт\n"
        "• цель поста\n"
        "• аудиторию",
        reply_markup=back_menu(),
    )


@dp.message(BotStates.waiting_post_prompt)
async def handle_post_prompt(message: Message, state: FSMContext):
    await state.clear()
    await start_generation_flow(message, CATEGORY_POST, message.text)


@dp.message(F.text == "💡 Идеи постов")
async def start_ideas_flow(message: Message, state: FSMContext):
    await state.set_state(BotStates.waiting_ideas_prompt)
    await message.answer(
        "Напиши тему, нишу или продукт.\n\n"
        "Я предложу идеи постов для канала.",
        reply_markup=back_menu(),
    )


@dp.message(BotStates.waiting_ideas_prompt)
async def handle_ideas_prompt(message: Message, state: FSMContext):
    await state.clear()
    await start_generation_flow(message, CATEGORY_IDEAS, message.text)


@dp.message(F.text == "♻️ Переписать текст")
async def start_rewrite_flow(message: Message, state: FSMContext):
    await state.set_state(BotStates.waiting_rewrite_text)
    await message.answer(
        "Отправь текст, который нужно переписать или улучшить.",
        reply_markup=back_menu(),
    )


@dp.message(BotStates.waiting_rewrite_text)
async def handle_rewrite_prompt(message: Message, state: FSMContext):
    await state.clear()
    await start_generation_flow(message, CATEGORY_REWRITE, message.text)


@dp.message(F.text == "📊 Остаток генераций")
async def show_balance(message: Message):
    ensure_user(message.from_user.id, message.from_user.username)
    user = get_user_row(message.from_user.id)

    text = (
        f"Осталось генераций: {user['balance']}\n"
        f"Тариф: {user['tariff_code']}"
    )

    await message.answer(text, reply_markup=main_menu())

    if int(user["balance"]) <= 3:
        await message.answer(
            "Если используешь бот регулярно, удобнее подключить тариф 👇",
            reply_markup=tariffs_keyboard(message.from_user.username),
        )


@dp.message(F.text == "👤 Мой тариф")
async def show_my_tariff(message: Message):
    ensure_user(message.from_user.id, message.from_user.username)
    user = get_user_row(message.from_user.id)
    await message.answer(format_tariff(user), reply_markup=main_menu())


@dp.message(F.text == "💳 Тарифы")
async def show_tariffs(message: Message):
    ensure_user(message.from_user.id, message.from_user.username)

    lines = [
        "Выбери подходящий тариф:\n",
        "Free — 10 генераций",
        "Creator — 300 ₽ / месяц",
    ]
    if can_show_unlim(message.from_user.username):
        lines.append("Unlim — 800 ₽ / месяц")

    lines.append("")
    lines.append("Плюс можно докупить пакеты генераций.")

    await message.answer(
        "\n".join(lines),
        reply_markup=main_menu(),
    )
    await message.answer(
        "Доступные варианты 👇",
        reply_markup=tariffs_keyboard(message.from_user.username),
    )


@dp.message(F.text == "🕘 Настройка бота")
async def show_settings(message: Message):
    ensure_user(message.from_user.id, message.from_user.username)
    user = get_user_row(message.from_user.id)

    await message.answer(
        "Здесь можно настроить поведение бота.",
        reply_markup=main_menu(),
    )
    await message.answer(
        "Настройки 👇",
        reply_markup=settings_keyboard(user),
    )


# =========================
# STYLE COPY
# =========================

@dp.callback_query(F.data == "settings:style_copy")
async def settings_style_copy(callback: CallbackQuery, state: FSMContext):
    await state.set_state(BotStates.waiting_style_sample)
    await callback.message.answer(
        "Пришли несколько своих постов.\n\n"
        "Я проанализирую их и буду писать в похожем стиле.",
        reply_markup=back_menu(),
    )
    await callback.message.answer(
        "Управление стилем 👇",
        reply_markup=style_copy_keyboard(),
    )
    await callback.answer()


@dp.message(BotStates.waiting_style_sample)
async def collect_style_sample(message: Message):
    ensure_user(message.from_user.id, message.from_user.username)
    add_style_sample(message.from_user.id, message.text)
    await message.answer(
        "Пример стиля добавлен 👌\n\n"
        "Можно прислать ещё один текст или завершить обучение.",
        reply_markup=style_copy_keyboard(),
    )


@dp.callback_query(F.data == "style:add_more")
async def style_add_more(callback: CallbackQuery):
    await callback.message.answer(
        "Пришли ещё один пример поста.",
        reply_markup=back_menu(),
    )
    await callback.answer()


@dp.callback_query(F.data == "style:reset")
async def style_reset(callback: CallbackQuery):
    clear_style_samples(callback.from_user.id)
    await callback.message.answer(
        "Все примеры стиля удалены.",
        reply_markup=style_copy_keyboard(),
    )
    await callback.answer()


@dp.callback_query(F.data == "style:finish")
async def style_finish(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    samples = get_style_samples(callback.from_user.id)

    if not samples:
        await callback.message.answer(
            "Пока нет примеров стиля. Сначала добавь хотя бы один текст.",
            reply_markup=main_menu(),
        )
        await callback.answer()
        return

    await callback.message.answer(
        "Стиль сохранён 👌\n\nТеперь я буду учитывать его при генерации постов.",
        reply_markup=main_menu(),
    )
    await callback.answer()


# =========================
# MEMORY SETTINGS
# =========================

@dp.callback_query(F.data == "settings:memory_toggle")
async def settings_memory_toggle(callback: CallbackQuery):
    ensure_user(callback.from_user.id, callback.from_user.username)
    user = get_user_row(callback.from_user.id)
    new_value = not bool(user["memory_enabled"])
    set_memory_enabled(callback.from_user.id, new_value)

    user = get_user_row(callback.from_user.id)
    status = "включена" if user["memory_enabled"] else "выключена"

    await callback.message.answer(
        f"Память теперь {status}.",
        reply_markup=settings_keyboard(user),
    )
    await callback.answer()


@dp.callback_query(F.data == "settings:memory_reset")
async def settings_memory_reset(callback: CallbackQuery):
    reset_user_memory(callback.from_user.id)
    await callback.message.answer(
        "Память сброшена. Накопленные предпочтения очищены.",
        reply_markup=main_menu(),
    )
    await callback.answer()

    # =========================
# REFINEMENTS
# =========================

REFINE_LABELS = {
    "shorter": "короче",
    "sales": "продающим",
    "lively": "живее",
    "cta": "с CTA",
    "telegram": "под Telegram",
    "audience": "для конкретной ЦА",
    "alt": "другой вариант",
    "custom": "своя правка",
    "deeper": "глубже",
    "bolder": "смелее",
    "cleaner": "чище",
    "structured": "структурнее",
}


def refine_instruction(category: str, action: str, extra: Optional[str] = None) -> str:
    if action == "shorter":
        return "Сделай текст короче без потери смысла."
    if action == "sales":
        return "Сделай текст более продающим: усили ценность, оффер и мотивацию."
    if action == "lively":
        return "Сделай текст живее, легче, естественнее и эмоциональнее."
    if action == "cta":
        return "Добавь сильный, уместный призыв к действию."
    if action == "telegram":
        return "Сделай формат более телеграмным: живой ритм, удобное чтение, естественная подача."
    if action == "audience":
        return f"Адаптируй текст под эту аудиторию: {extra}."
    if action == "alt":
        return "Сделай другой вариант с тем же смыслом."
    if action == "custom":
        return f"Примени эту пользовательскую правку: {extra}."
    if action == "deeper":
        return "Сделай идеи глубже, менее банальными, более содержательными."
    if action == "bolder":
        return "Сделай идеи смелее, ярче и цепляющее."
    if action == "cleaner":
        return "Сделай текст чище: убери тяжеловесность, мусор и канцелярит."
    if action == "structured":
        return "Сделай текст структурнее и логичнее."
    return "Переработай текст, сохранив смысл."


async def run_refine(
    callback: CallbackQuery,
    state: FSMContext,
    category: str,
    action: str,
    extra: Optional[str] = None,
):
    ensure_user(callback.from_user.id, callback.from_user.username)

    session_row = get_active_session(callback.from_user.id)
    if not session_row:
        await callback.message.answer("Нет активного результата для доработки.", reply_markup=main_menu())
        await callback.answer()
        return

    charge = should_charge_refine(session_row)
    if charge and not has_balance(callback.from_user.id):
        await callback.message.answer(
            "Сейчас лимит генераций закончился ✨\n\n"
            "Можно подключить тариф или докупить пакет генераций 👇",
            reply_markup=main_menu(),
        )
        await callback.message.answer(
            "Выбери вариант:",
            reply_markup=tariffs_keyboard(callback.from_user.username),
        )
        await callback.answer()
        return

    current_text = session_row["current_text"]
    instruction = refine_instruction(category, action, extra)

    wait = await callback.message.answer("Дорабатываю вариант… ✍️")
    new_text = await apply_refinement(category, callback.from_user.id, current_text, instruction)

    if charge:
        decrease_balance(callback.from_user.id, 1)

    update_session_text(session_row["id"], new_text, increase_refine=True)
    save_history(callback.from_user.id, category, "assistant", new_text, is_final=False)
    save_preference(callback.from_user.id, category, action, extra or "true")
    save_final_from_session(callback.from_user.id, category)

    try:
        await wait.edit_text("Готово ✅")
    except Exception:
        pass

    await callback.message.answer(new_text, reply_markup=keyboard_for_category(category))
    await callback.answer()


@dp.callback_query(F.data.startswith("refine:"))
async def refine_router(callback: CallbackQuery, state: FSMContext):
    _, category, action = callback.data.split(":")

    if action == "custom":
        await state.update_data(refine_category=category, refine_action=action)
        await state.set_state(BotStates.waiting_refine_custom)
        await callback.message.answer(
            "Напиши, как именно изменить текст.\n\n"
            "Например:\n"
            "• сделай более дерзким\n"
            "• добавь больше фактов\n"
            "• сделай мягче",
            reply_markup=back_menu(),
        )
        await callback.answer()
        return

    if action == "audience":
        await state.update_data(refine_category=category, refine_action=action)
        await state.set_state(BotStates.waiting_refine_audience)
        await callback.message.answer(
            "Для какой аудитории адаптировать текст?\n\n"
            "Например:\n"
            "• предприниматели\n"
            "• маркетологи\n"
            "• владельцы Telegram-каналов",
            reply_markup=back_menu(),
        )
        await callback.answer()
        return

    await run_refine(callback, state, category, action)


@dp.message(BotStates.waiting_refine_custom)
async def refine_custom_input(message: Message, state: FSMContext):
    data = await state.get_data()
    category = data["refine_category"]

    await state.clear()

    fake_callback = type("FakeCallback", (), {
        "from_user": message.from_user,
        "message": message,
        "answer": (lambda *args, **kwargs: asyncio.sleep(0)),
    })()

    await run_refine(fake_callback, state, category, "custom", message.text)


@dp.message(BotStates.waiting_refine_audience)
async def refine_audience_input(message: Message, state: FSMContext):
    data = await state.get_data()
    category = data["refine_category"]

    await state.clear()

    fake_callback = type("FakeCallback", (), {
        "from_user": message.from_user,
        "message": message,
        "answer": (lambda *args, **kwargs: asyncio.sleep(0)),
    })()

    await run_refine(fake_callback, state, category, "audience", message.text)


# =========================
# NAVIGATION CALLBACKS
# =========================

@dp.callback_query(F.data == "nav:menu")
async def nav_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    close_active_session(callback.from_user.id)
    await callback.message.answer("Главное меню 👇", reply_markup=main_menu())
    await callback.answer()


@dp.callback_query(F.data == "nav:cancel")
async def nav_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    close_active_session(callback.from_user.id)
    await callback.message.answer("Ок, остановились 👌", reply_markup=main_menu())
    await callback.answer()


@dp.callback_query(F.data == "nav:back")
async def nav_back(callback: CallbackQuery):
    session_row = get_active_session(callback.from_user.id)
    if session_row:
        await callback.message.answer(
            f"Текущий вариант:\n\n{session_row['current_text']}",
            reply_markup=keyboard_for_category(session_row["category"]),
        )
    else:
        await callback.message.answer("Главное меню 👇", reply_markup=main_menu())
    await callback.answer()


# =========================
# YOOKASSA
# =========================

async def create_yookassa_payment(
    *,
    telegram_id: int,
    username: Optional[str],
    product_code: str,
) -> tuple[str, str]:
    product = TARIFFS[product_code]
    idempotence_key = str(uuid.uuid4())

    payload = {
        "amount": {
            "value": f"{product['price']:.2f}",
            "currency": "RUB",
        },
        "capture": True,
        "confirmation": {
            "type": "redirect",
            "return_url": YOOKASSA_RETURN_URL,
        },
        "description": product["description"],
        "metadata": {
            "telegram_id": str(telegram_id),
            "username": username or "",
            "product_code": product_code,
            "source": "posterai_bot",
        },
    }

    headers = {
        "Idempotence-Key": idempotence_key,
        "Content-Type": "application/json",
    }

    auth = BasicAuth(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY)

    async with aiohttp.ClientSession(auth=auth) as session:
        async with session.post(
            "https://api.yookassa.ru/v3/payments",
            json=payload,
            headers=headers,
            timeout=30,
        ) as resp:
            text = await resp.text()
            if resp.status not in (200, 201):
                logger.error("YooKassa create payment failed: %s", text)
                raise RuntimeError(f"Не удалось создать платёж: {text}")

            data = json.loads(text)

    payment_id = data["id"]
    confirmation_url = data["confirmation"]["confirmation_url"]
    status = data["status"]
    is_test = bool(data.get("test", True))

    save_payment(
        payment_id=payment_id,
        telegram_id=telegram_id,
        username=username,
        product_code=product_code,
        amount=product["price"],
        status=status,
        is_test=is_test,
        idempotence_key=idempotence_key,
        confirmation_url=confirmation_url,
        raw_payload=data,
    )

    logger.info(
        "Payment created | payment_id=%s | telegram_id=%s | product_code=%s | status=%s",
        payment_id,
        telegram_id,
        product_code,
        status,
    )

    return payment_id, confirmation_url


async def apply_successful_payment(payment_row: dict):
    product_code = payment_row["product_code"]
    product = TARIFFS[product_code]
    telegram_id = int(payment_row["telegram_id"])

    logger.info("apply_successful_payment started | payment_row=%s", payment_row)

    if product["type"] == "subscription":
        activate_subscription(
            user_id=telegram_id,
            tariff_code=product_code,
            balance_add=product["balance_add"],
            days=product["days"],
        )
    else:
        add_balance(telegram_id, product["balance_add"])

    await bot.send_message(
        telegram_id,
        f"✅ Оплата прошла успешно!\n\nНачислили: {product['title']}\nМожно продолжать работу 👇",
        reply_markup=main_menu(),
    )

    logger.info("apply_successful_payment finished | telegram_id=%s", telegram_id)


@dp.callback_query(F.data.startswith("buy:"))
async def buy_callback(callback: CallbackQuery):
    product_code = callback.data.split(":", 1)[1]

    if product_code not in TARIFFS:
        await callback.answer("Неизвестный продукт", show_alert=True)
        return

    if product_code == "unlim_monthly" and not can_show_unlim(callback.from_user.username):
        await callback.answer("Этот тариф сейчас недоступен", show_alert=True)
        return

    ensure_user(callback.from_user.id, callback.from_user.username)

    try:
        _, confirmation_url = await create_yookassa_payment(
            telegram_id=callback.from_user.id,
            username=callback.from_user.username,
            product_code=product_code,
        )
    except Exception:
        logger.exception("Ошибка создания платежа")
        await callback.message.answer(
            "Не получилось подготовить оплату. Попробуй ещё раз чуть позже.",
            reply_markup=main_menu(),
        )
        await callback.answer()
        return

    product = TARIFFS[product_code]

    await callback.message.answer(
        f"Подготовили оплату: {product['title']} ✅\n\n"
        f"Сумма: {product['price']} ₽\n"
        "Открой страницу оплаты по кнопке ниже.",
        reply_markup=payment_link_keyboard(confirmation_url),
    )
    await callback.answer()


async def payment_return_handler(request: web.Request) -> web.Response:
    raise web.HTTPFound(f"https://t.me/{TELEGRAM_BOT_USERNAME}")


async def yookassa_webhook_handler(request: web.Request) -> web.Response:
    try:
        payload = await request.json()
    except Exception:
        logger.exception("Некорректный webhook JSON")
        return web.Response(status=400, text="invalid json")

    logger.info("Webhook received: %s", json.dumps(payload, ensure_ascii=False))

    event = payload.get("event")
    obj = payload.get("object", {})
    payment_id = obj.get("id")
    status = obj.get("status")

    logger.info(
        "Webhook parsed | event=%s | payment_id=%s | status=%s",
        event,
        payment_id,
        status,
    )

    if not payment_id:
        return web.Response(status=400, text="missing payment id")

    payment_row = get_payment(payment_id)
    logger.info("Payment lookup result: %s", "found" if payment_row else "not found")

    if not payment_row:
        logger.warning("Платёж %s не найден в БД", payment_id)
        return web.Response(status=200, text="ok")

    if payment_row["status"] == "succeeded":
        return web.Response(status=200, text="ok")

    if event == "payment.succeeded" and status == "succeeded":
        update_payment_status(payment_id, "succeeded", payload, paid=True)

        logger.info(
            "Applying successful payment | payment_id=%s | telegram_id=%s | product_code=%s",
            payment_id,
            payment_row["telegram_id"],
            payment_row["product_code"],
        )

        await apply_successful_payment(payment_row)

        logger.info("Payment applied successfully | payment_id=%s", payment_id)
        return web.Response(status=200, text="ok")

    if event == "payment.canceled" or status == "canceled":
        update_payment_status(payment_id, "canceled", payload, paid=False)

        try:
            await bot.send_message(
                payment_row["telegram_id"],
                "Оплата не завершилась. Можно попробовать ещё раз в любой момент.",
                reply_markup=main_menu(),
            )
        except Exception:
            logger.exception("Не удалось отправить сообщение об отмене оплаты")

        return web.Response(status=200, text="ok")

    update_payment_status(payment_id, status or payment_row["status"], payload, paid=False)
    return web.Response(status=200, text="ok")


async def start_http_server():
    app = web.Application()
    app.router.add_get("/payment-return", payment_return_handler)
    app.router.add_post("/yookassa/webhook", yookassa_webhook_handler)

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(runner, APP_HOST, APP_PORT)
    await site.start()

    logger.info("HTTP server started on %s:%s", APP_HOST, APP_PORT)
    return runner


# =========================
# FALLBACK TEXT
# =========================

@dp.message()
async def fallback_text(message: Message):
    ensure_user(message.from_user.id, message.from_user.username)
    await message.answer(
        "Не понял команду. Выбери действие из меню 👇",
        reply_markup=main_menu(),
    )


# =========================
# MAIN
# =========================

async def main():
    init_db()
    logger.info("DB initialized")

    http_runner = await start_http_server()

    try:
        logger.info("Bot polling started")
        await dp.start_polling(bot)
    finally:
        await http_runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())