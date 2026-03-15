import os
import json
import uuid
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional, Any

import aiohttp
from aiohttp import web
import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from openai import OpenAI

load_dotenv()

# =========================
# CONFIG
# =========================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")
YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID", "")
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY", "")
PAYMENT_RETURN_URL = os.getenv("PAYMENT_RETURN_URL", "")
PORT = int(os.getenv("PORT", "8080"))

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY is not set")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

CATEGORY_POST = "post"
CATEGORY_IDEAS = "ideas"
CATEGORY_REWRITE = "rewrite"

TARIFF_FREE = "free"
TARIFF_CREATOR = "creator"
TARIFF_UNLIM = "unlim"

FREE_GENERATIONS_DEFAULT = 10
CREATOR_GENERATIONS_DEFAULT = 200

FREE_REFINEMENTS_COUNT = 2

YOOKASSA_API_URL = "https://api.yookassa.ru/v3/payments"

BOT_USERNAME_FALLBACK = "@PosteraAI_bot"

# Для скрытой команды сброса лимита
SPECIAL_RESET_USERNAME = "babajanyanak"
SPECIAL_RESET_COMMAND = "/refresh_capsule_314"

# =========================
# LOGGING
# =========================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("posterai-bot")

# =========================
# OPENAI / BOT
# =========================

client = OpenAI(api_key=OPENAI_API_KEY)
bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# =========================
# FSM STATES
# =========================

class GenerationStates(StatesGroup):
    waiting_for_post_prompt = State()
    waiting_for_ideas_prompt = State()
    waiting_for_rewrite_prompt = State()
    waiting_for_refinement_prompt = State()
    waiting_for_style_sample = State()

class SettingsStates(StatesGroup):
    waiting_for_style_sample = State()

# =========================
# DB HELPERS
# =========================

def get_db_connection():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)

def init_db():
    logger.info("Initializing database...")
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            # users
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id BIGINT PRIMARY KEY,
                    tariff TEXT DEFAULT 'free',
                    generations_left INT DEFAULT 10,
                    plan_expires_at TIMESTAMP,
                    memory_enabled BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT NOW()
                )
                """
            )

            # user_history
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS user_history (
                    id SERIAL PRIMARY KEY,
                    telegram_id BIGINT,
                    category TEXT,
                    role TEXT,
                    text TEXT,
                    is_final BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT NOW()
                )
                """
            )

            # analytics_events
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS analytics_events (
                    id SERIAL PRIMARY KEY,
                    telegram_id BIGINT,
                    event_name TEXT NOT NULL,
                    category TEXT,
                    value TEXT,
                    meta JSONB,
                    created_at TIMESTAMP DEFAULT NOW()
                )
                """
            )

            # payments
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS payments (
                    id SERIAL PRIMARY KEY,
                    telegram_id BIGINT,
                    amount INT,
                    tariff TEXT,
                    status TEXT,
                    payment_id TEXT,
                    created_at TIMESTAMP DEFAULT NOW()
                )
                """
            )

            # generation_sessions
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS generation_sessions (
                    id SERIAL PRIMARY KEY,
                    telegram_id BIGINT,
                    category TEXT,
                    original_prompt TEXT,
                    generated_text TEXT,
                    refinement_count INT DEFAULT 0,
                    created_at TIMESTAMP DEFAULT NOW()
                )
                """
            )

            # style_posts
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS style_posts (
                    id SERIAL PRIMARY KEY,
                    telegram_id BIGINT,
                    text TEXT,
                    created_at TIMESTAMP DEFAULT NOW()
                )
                """
            )

            # user_style_samples
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS user_style_samples (
                    id SERIAL PRIMARY KEY,
                    telegram_id BIGINT,
                    text TEXT,
                    created_at TIMESTAMP DEFAULT NOW()
                )
                """
            )

            # user_category_memory
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS user_category_memory (
                    telegram_id BIGINT,
                    category TEXT,
                    last_prompt TEXT,
                    PRIMARY KEY (telegram_id, category)
                )
                """
            )

            # ===== migrations =====
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS tariff TEXT DEFAULT 'free'")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS generations_left INT DEFAULT 10")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS plan_expires_at TIMESTAMP")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS memory_enabled BOOLEAN DEFAULT TRUE")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT NOW()")

            cur.execute("ALTER TABLE user_history ADD COLUMN IF NOT EXISTS category TEXT")
            cur.execute("ALTER TABLE user_history ADD COLUMN IF NOT EXISTS role TEXT")
            cur.execute("ALTER TABLE user_history ADD COLUMN IF NOT EXISTS text TEXT")
            cur.execute("ALTER TABLE user_history ADD COLUMN IF NOT EXISTS is_final BOOLEAN DEFAULT FALSE")
            cur.execute("ALTER TABLE user_history ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT NOW()")

            cur.execute("ALTER TABLE analytics_events ADD COLUMN IF NOT EXISTS category TEXT")
            cur.execute("ALTER TABLE analytics_events ADD COLUMN IF NOT EXISTS value TEXT")
            cur.execute("ALTER TABLE analytics_events ADD COLUMN IF NOT EXISTS meta JSONB")
            cur.execute("ALTER TABLE analytics_events ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT NOW()")

            cur.execute("ALTER TABLE payments ADD COLUMN IF NOT EXISTS amount INT")
            cur.execute("ALTER TABLE payments ADD COLUMN IF NOT EXISTS tariff TEXT")
            cur.execute("ALTER TABLE payments ADD COLUMN IF NOT EXISTS status TEXT")
            cur.execute("ALTER TABLE payments ADD COLUMN IF NOT EXISTS payment_id TEXT")
            cur.execute("ALTER TABLE payments ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT NOW()")

            cur.execute("ALTER TABLE generation_sessions ADD COLUMN IF NOT EXISTS category TEXT")
            cur.execute("ALTER TABLE generation_sessions ADD COLUMN IF NOT EXISTS original_prompt TEXT")
            cur.execute("ALTER TABLE generation_sessions ADD COLUMN IF NOT EXISTS generated_text TEXT")
            cur.execute("ALTER TABLE generation_sessions ADD COLUMN IF NOT EXISTS refinement_count INT DEFAULT 0")
            cur.execute("ALTER TABLE generation_sessions ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT NOW()")

            cur.execute("ALTER TABLE user_style_samples ADD COLUMN IF NOT EXISTS text TEXT")
            cur.execute("ALTER TABLE user_style_samples ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT NOW()")

            # indexes
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_analytics_events_telegram_id
                ON analytics_events (telegram_id)
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_analytics_events_event_name
                ON analytics_events (event_name)
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_analytics_events_created_at
                ON analytics_events (created_at)
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_user_history_telegram_id
                ON user_history (telegram_id)
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_generation_sessions_telegram_id
                ON generation_sessions (telegram_id)
                """
            )

        conn.commit()
    logger.info("DB initialized")

# =========================
# ANALYTICS
# =========================

def track_event(
    user_id: int,
    event_name: str,
    category: Optional[str] = None,
    value: Optional[str] = None,
    meta: Optional[dict] = None,
):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO analytics_events (telegram_id, event_name, category, value, meta)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        user_id,
                        event_name,
                        category,
                        value,
                        json.dumps(meta or {}, ensure_ascii=False),
                    ),
                )
            conn.commit()
    except Exception as e:
        logger.exception("Failed to track event: %s", e)

# =========================
# USER / TARIFF HELPERS
# =========================

def ensure_user_exists(user_id: int):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (telegram_id, tariff, generations_left, memory_enabled)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (telegram_id) DO NOTHING
                """,
                (user_id, TARIFF_FREE, FREE_GENERATIONS_DEFAULT, True),
            )
        conn.commit()

def get_user(user_id: int) -> dict:
    ensure_user_exists(user_id)
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM users WHERE telegram_id = %s",
                (user_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else {}

def update_user_tariff(
    user_id: int,
    tariff: str,
    generations_left: Optional[int] = None,
    plan_expires_at: Optional[datetime] = None,
):
    ensure_user_exists(user_id)
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE users
                SET tariff = %s,
                    generations_left = COALESCE(%s, generations_left),
                    plan_expires_at = %s
                WHERE telegram_id = %s
                """,
                (tariff, generations_left, plan_expires_at, user_id),
            )
        conn.commit()

def set_memory_enabled(user_id: int, enabled: bool):
    ensure_user_exists(user_id)
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE users
                SET memory_enabled = %s
                WHERE telegram_id = %s
                """,
                (enabled, user_id),
            )
        conn.commit()

def is_subscription_active(user: dict) -> bool:
    plan_expires_at = user.get("plan_expires_at")
    if not plan_expires_at:
        return False
    return plan_expires_at > datetime.now()

def refresh_expired_plan_if_needed(user_id: int):
    user = get_user(user_id)
    tariff = user.get("tariff", TARIFF_FREE)
    plan_expires_at = user.get("plan_expires_at")

    if tariff == TARIFF_FREE:
        return

    if plan_expires_at and plan_expires_at <= datetime.now():
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE users
                    SET tariff = %s,
                        generations_left = %s,
                        plan_expires_at = NULL
                    WHERE telegram_id = %s
                    """,
                    (TARIFF_FREE, FREE_GENERATIONS_DEFAULT, user_id),
                )
            conn.commit()

def get_generations_left_text(user: dict) -> str:
    tariff = user.get("tariff", TARIFF_FREE)
    generations_left = user.get("generations_left", 0)
    plan_expires_at = user.get("plan_expires_at")

    if tariff == TARIFF_UNLIM and is_subscription_active(user):
        return "♾ Безлимит активен"
    if tariff == TARIFF_CREATOR and is_subscription_active(user):
        expires_text = plan_expires_at.strftime("%d.%m.%Y") if plan_expires_at else "—"
        return f"📦 Тариф Creator активен до {expires_text}\n📊 Осталось генераций: {generations_left}"
    return f"📊 Осталось генераций: {generations_left}"

def can_spend_generation(user: dict) -> bool:
    tariff = user.get("tariff", TARIFF_FREE)

    if tariff == TARIFF_UNLIM and is_subscription_active(user):
        return True

    generations_left = user.get("generations_left", 0)
    return generations_left > 0

def spend_generation(user_id: int, amount: int = 1) -> bool:
    refresh_expired_plan_if_needed(user_id)
    user = get_user(user_id)

    if user.get("tariff") == TARIFF_UNLIM and is_subscription_active(user):
        return True

    generations_left = user.get("generations_left", 0)
    if generations_left < amount:
        return False

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE users
                SET generations_left = GREATEST(generations_left - %s, 0)
                WHERE telegram_id = %s
                """,
                (amount, user_id),
            )
        conn.commit()
    return True

def add_generations(user_id: int, amount: int):
    ensure_user_exists(user_id)
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE users
                SET generations_left = generations_left + %s
                WHERE telegram_id = %s
                """,
                (amount, user_id),
            )
        conn.commit()

# =========================
# HISTORY / MEMORY / STYLE
# =========================

def save_history(user_id: int, category: str, role: str, text: str, is_final: bool = False):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO user_history (telegram_id, category, role, text, is_final)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (user_id, category, role, text, is_final),
            )
        conn.commit()

def get_last_history_items(user_id: int, category: Optional[str] = None, limit: int = 10) -> list[dict]:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            if category:
                cur.execute(
                    """
                    SELECT * FROM user_history
                    WHERE telegram_id = %s AND category = %s
                    ORDER BY created_at DESC
                    LIMIT %s
                    """,
                    (user_id, category, limit),
                )
            else:
                cur.execute(
                    """
                    SELECT * FROM user_history
                    WHERE telegram_id = %s
                    ORDER BY created_at DESC
                    LIMIT %s
                    """,
                    (user_id, limit),
                )
            rows = cur.fetchall() or []
            return [dict(r) for r in rows]

def save_category_memory(user_id: int, category: str, prompt: str):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO user_category_memory (telegram_id, category, last_prompt)
                VALUES (%s, %s, %s)
                ON CONFLICT (telegram_id, category)
                DO UPDATE SET last_prompt = EXCLUDED.last_prompt
                """,
                (user_id, category, prompt),
            )
        conn.commit()

def get_category_memory(user_id: int, category: str) -> Optional[str]:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT last_prompt
                FROM user_category_memory
                WHERE telegram_id = %s AND category = %s
                """,
                (user_id, category),
            )
            row = cur.fetchone()
            return row["last_prompt"] if row else None

def add_style_sample(user_id: int, text: str):
    with get_db_connection() as conn:
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
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM user_style_samples
                WHERE telegram_id = %s
                """,
                (user_id,),
            )
        conn.commit()

def get_style_samples(user_id: int, limit: int = 5) -> list[str]:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT text
                FROM user_style_samples
                WHERE telegram_id = %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (user_id, limit),
            )
            rows = cur.fetchall() or []
            return [r["text"] for r in rows]

# =========================
# GENERATION SESSIONS
# =========================

def create_generation_session(user_id: int, category: str, original_prompt: str, generated_text: str) -> int:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO generation_sessions (telegram_id, category, original_prompt, generated_text, refinement_count)
                VALUES (%s, %s, %s, %s, 0)
                RETURNING id
                """,
                (user_id, category, original_prompt, generated_text),
            )
            row = cur.fetchone()
        conn.commit()
    return int(row["id"])

def get_last_generation_session(user_id: int) -> Optional[dict]:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM generation_sessions
                WHERE telegram_id = %s
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (user_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None

def get_generation_session(session_id: int) -> Optional[dict]:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM generation_sessions
                WHERE id = %s
                """,
                (session_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None

def update_generation_session_text(session_id: int, generated_text: str, increment_refinement: bool = False):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            if increment_refinement:
                cur.execute(
                    """
                    UPDATE generation_sessions
                    SET generated_text = %s,
                        refinement_count = refinement_count + 1
                    WHERE id = %s
                    """,
                    (generated_text, session_id),
                )
            else:
                cur.execute(
                    """
                    UPDATE generation_sessions
                    SET generated_text = %s
                    WHERE id = %s
                    """,
                    (generated_text, session_id),
                )
        conn.commit()

# =========================
# PAYMENTS
# =========================

def create_payment_record(user_id: int, amount: int, tariff: str, status: str, payment_id: Optional[str] = None):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO payments (telegram_id, amount, tariff, status, payment_id)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (user_id, amount, tariff, status, payment_id),
            )
        conn.commit()

def update_payment_status(payment_id: str, status: str):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE payments
                SET status = %s
                WHERE payment_id = %s
                """,
                (status, payment_id),
            )
        conn.commit()

def get_payment_by_payment_id(payment_id: str) -> Optional[dict]:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM payments
                WHERE payment_id = %s
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (payment_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None

# =========================
# TEXT / PROMPTS HELPERS
# =========================

def escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )

def get_system_prompt(category: str) -> str:
    base = (
        "Ты сильный русскоязычный редактор и контент-стратег. "
        "Пишешь понятно, живо, по делу, без канцелярита. "
        "Учитывай, что тексты нужны для Telegram и digital-среды. "
        "Если уместно, делай текст более вовлекающим, но без перегиба."
    )

    if category == CATEGORY_POST:
        return (
            base
            + " Сгенерируй готовый пост на русском языке. "
              "Он должен быть читабельным, современным, с хорошей структурой, "
              "естественными абзацами и сильным первым экраном."
        )

    if category == CATEGORY_IDEAS:
        return (
            base
            + " Сгенерируй идеи постов на русском языке. "
              "Идеи должны быть конкретными, небанальными и пригодными для Telegram."
        )

    if category == CATEGORY_REWRITE:
        return (
            base
            + " Перепиши текст на русском языке. "
              "Сохраняй смысл, но делай подачу сильнее, чище и удобнее для восприятия."
        )

    return base

def build_user_prompt(
    category: str,
    user_prompt: str,
    memory_text: Optional[str] = None,
    style_samples: Optional[list[str]] = None,
) -> str:
    parts = []

    if memory_text:
        parts.append(
            f"Контекст прошлой генерации пользователя по этой категории:\n{memory_text}"
        )

    if style_samples:
        joined = "\n\n---\n\n".join(style_samples)
        parts.append(
            "Примеры пользовательского стиля. "
            "Не копируй слово в слово, но учитывай ритм, подачу и тон:\n"
            f"{joined}"
        )

    if category == CATEGORY_POST:
        parts.append(f"Задача: создать пост по теме:\n{user_prompt}")
    elif category == CATEGORY_IDEAS:
        parts.append(f"Задача: придумать идеи постов по теме/нише:\n{user_prompt}")
    elif category == CATEGORY_REWRITE:
        parts.append(f"Задача: переписать этот текст:\n{user_prompt}")
    else:
        parts.append(user_prompt)

    return "\n\n".join(parts)

def build_refinement_prompt(refinement_type: str, current_text: str) -> str:
    mapping = {
        "shorter": (
            "Сократи текст без потери смысла. Убери лишнее, сделай компактнее и чище."
        ),
        "telegram": (
            "Адаптируй текст под Telegram: сделай живее, разговорнее, удобнее для чтения с телефона."
        ),
        "selling": (
            "Сделай текст более продающим: усили оффер, ценность, призыв к действию."
        ),
        "structure": (
            "Добавь больше структуры: сильные абзацы, списки при необходимости, ясные акценты."
        ),
        "style": (
            "Улучши стиль текста: сделай формулировки сильнее, чище и выразительнее."
        ),
    }
    instruction = mapping.get(refinement_type, "Улучши текст.")
    return f"{instruction}\n\nТекущий текст:\n{current_text}"

# =========================
# KEYBOARDS
# =========================

def main_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="✍️ Сгенерировать пост"),
                KeyboardButton(text="💡 Идеи постов"),
            ],
            [
                KeyboardButton(text="♻️ Переписать текст"),
            ],
            [
                KeyboardButton(text="👤 Мой тариф"),
                KeyboardButton(text="📊 Остаток генераций"),
            ],
            [
                KeyboardButton(text="💳 Тарифы"),
                KeyboardButton(text="🕘 Настройка бота"),
            ],
        ],
        resize_keyboard=True,
    )

def settings_keyboard(user: dict) -> ReplyKeyboardMarkup:
    memory_enabled = bool(user.get("memory_enabled", True))
    memory_button = "🧠 Память: вкл" if memory_enabled else "🧠 Память: выкл"

    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Добавить пример текста")],
            [KeyboardButton(text="🗑 Очистить стиль")],
            [KeyboardButton(text=memory_button)],
            [KeyboardButton(text="⬅️ Назад в меню")],
        ],
        resize_keyboard=True,
    )

def tariffs_inline_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="💳 Оплатить Creator", callback_data="buy_creator"),
            ],
            [
                InlineKeyboardButton(text="💳 Оплатить Unlim", callback_data="buy_unlim"),
            ],
            [
                InlineKeyboardButton(text="🏠 В меню", callback_data="back_to_menu"),
            ],
        ]
    )

def result_inline_keyboard(session_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✏️ Доработать", callback_data=f"refine:{session_id}"),
                InlineKeyboardButton(text="🔁 Перегенерировать", callback_data=f"regen:{session_id}"),
            ],
            [
                InlineKeyboardButton(text="📋 Скопировать", callback_data=f"copy:{session_id}"),
                InlineKeyboardButton(text="🏠 В меню", callback_data="back_to_menu"),
            ],
        ]
    )

def refinement_inline_keyboard(session_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✂️ Короче", callback_data=f"refine_type:{session_id}:shorter")],
            [InlineKeyboardButton(text="📱 Под Telegram", callback_data=f"refine_type:{session_id}:telegram")],
            [InlineKeyboardButton(text="💰 Продающим", callback_data=f"refine_type:{session_id}:selling")],
            [InlineKeyboardButton(text="🎯 Добавить структуру", callback_data=f"refine_type:{session_id}:structure")],
            [InlineKeyboardButton(text="✨ Улучшить стиль", callback_data=f"refine_type:{session_id}:style")],
            [InlineKeyboardButton(text="🏠 В меню", callback_data="back_to_menu")],
        ]
    )

# =========================
# UI TEXT HELPERS
# =========================

def get_tariff_title(tariff: str) -> str:
    if tariff == TARIFF_CREATOR:
        return "Creator"
    if tariff == TARIFF_UNLIM:
        return "Unlim"
    return "Free"

def get_my_tariff_text(user: dict, telegram_username: Optional[str] = None) -> str:
    tariff = user.get("tariff", TARIFF_FREE)
    generations_left = user.get("generations_left", 0)
    plan_expires_at = user.get("plan_expires_at")

    lines = [f"👤 Ваш тариф: {get_tariff_title(tariff)}"]

    if tariff == TARIFF_UNLIM and is_subscription_active(user):
        expires_text = plan_expires_at.strftime("%d.%m.%Y") if plan_expires_at else "—"
        lines.append(f"♾ Безлимит активен до {expires_text}")
    elif tariff == TARIFF_CREATOR and is_subscription_active(user):
        expires_text = plan_expires_at.strftime("%d.%m.%Y") if plan_expires_at else "—"
        lines.append(f"📦 Creator активен до {expires_text}")
        lines.append(f"📊 Осталось генераций: {generations_left}")
    else:
        lines.append(f"📊 Осталось генераций: {generations_left}")

    if telegram_username == SPECIAL_RESET_USERNAME:
        lines.append("")
        lines.append("🔐 Для вас доступна служебная команда обновления лимита.")

    lines.append("")
    lines.append("Рад был помочь! Сохраняем ваши предпочтения — в следующих генерациях учтём 😉")
    return "\n".join(lines)

def get_tariffs_text(user: dict) -> str:
    lines = [
        "💳 Тарифы PosteraAI",
        "",
        "Free",
        f"• {FREE_GENERATIONS_DEFAULT} генераций на старте",
        "• Базовые режимы",
        "• Первые 2 доработки результата бесплатно",
        "",
        "Creator",
        f"• {CREATOR_GENERATIONS_DEFAULT} генераций в месяц",
        "• Подходит для регулярной работы",
        "• Удобно для контент-потока",
        "",
        "Unlim",
        "• Безлимитные генерации",
        "• Для активного ежедневного использования",
        "",
        "Выберите подходящий вариант 👇",
    ]
    return "\n".join(lines)

def get_settings_text(user: dict) -> str:
    memory_enabled = "включена" if bool(user.get("memory_enabled", True)) else "выключена"
    style_samples = get_style_samples(user["telegram_id"])
    style_count = len(style_samples)

    return (
        "🕘 Настройка бота\n\n"
        f"🧠 Память: {memory_enabled}\n"
        f"✍️ Примеров стиля сохранено: {style_count}\n\n"
        "Здесь можно добавить примеры своих текстов, очистить стиль или управлять памятью."
    )

# =========================
# COMMON SENDERS
# =========================

async def send_main_menu(message: Message, text: str):
    await message.answer(text, reply_markup=main_menu_keyboard())

async def send_main_menu_from_callback(callback: CallbackQuery, text: str):
    await callback.message.answer(text, reply_markup=main_menu_keyboard())

def get_prompt_request_text(category: str) -> str:
    if category == CATEGORY_POST:
        return (
            "✍️ Отправьте тему, тезисы или короткое описание — и я соберу готовый пост.\n\n"
            "Например:\n"
            "«Сделай пост о том, почему CRM в девелопменте — это не просто база клиентов»"
        )
    if category == CATEGORY_IDEAS:
        return (
            "💡 Отправьте тему, нишу или продукт — и я предложу идеи постов.\n\n"
            "Например:\n"
            "«Идеи постов для Telegram-канала про недвижимость бизнес-класса»"
        )
    if category == CATEGORY_REWRITE:
        return (
            "♻️ Пришлите текст, который нужно переписать.\n\n"
            "Я сделаю его чище, сильнее и удобнее для чтения."
        )
    return "Отправьте текст."

# =========================
# END PART 1
# =========================

# =========================
# OPENAI / GENERATION
# =========================

def call_openai_text(system_prompt: str, user_prompt: str) -> str:
    response = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.7,
    )

    if not response.choices:
        raise RuntimeError("OpenAI не вернул choices")

    message = response.choices[0].message
    if not message or not message.content:
        raise RuntimeError("OpenAI не вернул текстовый ответ")

    return message.content.strip()

def build_generation_payload(user_id: int, category: str, user_prompt: str) -> tuple[str, str]:
    user = get_user(user_id)
    memory_enabled = bool(user.get("memory_enabled", True))

    memory_text = get_category_memory(user_id, category) if memory_enabled else None
    style_samples = get_style_samples(user_id, limit=5)

    system_prompt = get_system_prompt(category)
    final_user_prompt = build_user_prompt(
        category=category,
        user_prompt=user_prompt,
        memory_text=memory_text,
        style_samples=style_samples,
    )
    return system_prompt, final_user_prompt

async def run_generation(user_id: int, category: str, user_prompt: str) -> str:
    system_prompt, final_user_prompt = build_generation_payload(user_id, category, user_prompt)

    logger.info("OpenAI generation started | user_id=%s | category=%s", user_id, category)
    result_text = await asyncio.to_thread(
        call_openai_text,
        system_prompt,
        final_user_prompt,
    )
    logger.info("OpenAI generation success | user_id=%s | category=%s", user_id, category)
    return result_text

def get_limit_exceeded_text() -> str:
    return (
        "Похоже, лимит генераций закончился 😅\n\n"
        "Можно выбрать тариф и продолжить без пауз 👇"
    )

async def start_generation_flow(message: Message, category: str, user_prompt: str):
    user_id = message.from_user.id
    refresh_expired_plan_if_needed(user_id)
    user = get_user(user_id)

    if not can_spend_generation(user):
        track_event(user_id, "limit_reached", category=category)
        await message.answer(
            get_limit_exceeded_text(),
            reply_markup=tariffs_inline_keyboard(),
        )
        return

    spent = spend_generation(user_id, 1)
    if not spent:
        track_event(user_id, "limit_reached", category=category)
        await message.answer(
            get_limit_exceeded_text(),
            reply_markup=tariffs_inline_keyboard(),
        )
        return

    track_event(
        user_id,
        "generation_started",
        category=category,
        meta={"prompt_length": len(user_prompt)},
    )

    save_history(user_id, category, "user", user_prompt)
    save_category_memory(user_id, category, user_prompt)

    wait_msg = await message.answer("⏳ Готовлю результат... Обычно это занимает пару секунд.")

    try:
        result_text = await run_generation(user_id, category, user_prompt)

        save_history(user_id, category, "assistant", result_text, is_final=True)
        session_id = create_generation_session(user_id, category, user_prompt, result_text)

        track_event(
            user_id,
            "generation_success",
            category=category,
            meta={"response_length": len(result_text)},
        )

        await wait_msg.delete()
        await message.answer(
            f"✅ Готово!\n\n{result_text}",
            reply_markup=result_inline_keyboard(session_id),
        )
        await message.answer(
            "Рад был помочь! Сохраняем ваши предпочтения — в следующих генерациях учтём 😉",
            reply_markup=main_menu_keyboard(),
        )
    except Exception as e:
        logger.exception("Generation failed: %s", e)
        track_event(
            user_id,
            "generation_failed",
            category=category,
            meta={"error": str(e)[:500]},
        )
        await wait_msg.delete()
        await message.answer(
            "Кажется произошла ошибка 😅 Попробуем ещё раз?",
            reply_markup=main_menu_keyboard(),
        )

async def regenerate_from_session(callback: CallbackQuery, session_id: int):
    session = get_generation_session(session_id)
    if not session:
        await callback.answer("Сессия не найдена", show_alert=True)
        return

    user_id = callback.from_user.id
    refresh_expired_plan_if_needed(user_id)
    user = get_user(user_id)

    if not can_spend_generation(user):
        track_event(user_id, "limit_reached", category=session["category"])
        await callback.message.answer(
            get_limit_exceeded_text(),
            reply_markup=tariffs_inline_keyboard(),
        )
        await callback.answer()
        return

    spent = spend_generation(user_id, 1)
    if not spent:
        track_event(user_id, "limit_reached", category=session["category"])
        await callback.message.answer(
            get_limit_exceeded_text(),
            reply_markup=tariffs_inline_keyboard(),
        )
        await callback.answer()
        return

    track_event(
        user_id,
        "generation_started",
        category=session["category"],
        value="regenerate",
        meta={"prompt_length": len(session["original_prompt"] or "")},
    )

    await callback.answer("Перегенерирую ✨")
    wait_msg = await callback.message.answer("⏳ Делаю новый вариант...")

    try:
        result_text = await run_generation(user_id, session["category"], session["original_prompt"])

        update_generation_session_text(session_id, result_text, increment_refinement=False)
        save_history(user_id, session["category"], "assistant", result_text, is_final=True)

        track_event(
            user_id,
            "generation_success",
            category=session["category"],
            value="regenerate",
            meta={"response_length": len(result_text)},
        )

        await wait_msg.delete()
        await callback.message.answer(
            f"🔁 Новый вариант готов:\n\n{result_text}",
            reply_markup=result_inline_keyboard(session_id),
        )
        await callback.message.answer(
            "Если нужно, можем ещё докрутить текст 😉",
            reply_markup=main_menu_keyboard(),
        )
    except Exception as e:
        logger.exception("Regeneration failed: %s", e)
        track_event(
            user_id,
            "generation_failed",
            category=session["category"],
            value="regenerate",
            meta={"error": str(e)[:500]},
        )
        await wait_msg.delete()
        await callback.message.answer(
            "Кажется произошла ошибка 😅 Попробуем ещё раз?",
            reply_markup=main_menu_keyboard(),
        )

def refinement_requires_generation(user_id: int, session: dict) -> bool:
    refinement_count = int(session.get("refinement_count", 0))
    if refinement_count < FREE_REFINEMENTS_COUNT:
        return False

    user = get_user(user_id)
    if user.get("tariff") == TARIFF_UNLIM and is_subscription_active(user):
        return False

    return True

async def apply_refinement(callback: CallbackQuery, session_id: int, refinement_type: str):
    session = get_generation_session(session_id)
    if not session:
        await callback.answer("Сессия не найдена", show_alert=True)
        return

    user_id = callback.from_user.id
    current_text = session.get("generated_text", "")

    if not current_text.strip():
        await callback.answer("Нет текста для доработки", show_alert=True)
        return

    need_generation = refinement_requires_generation(user_id, session)
    if need_generation:
        refresh_expired_plan_if_needed(user_id)
        user = get_user(user_id)

        if not can_spend_generation(user):
            track_event(user_id, "limit_reached", category=session["category"], value="refinement")
            await callback.message.answer(
                "Бесплатные доработки уже закончились, а лимит генераций тоже на нуле 😅\n\nВыберите тариф, и продолжим 👇",
                reply_markup=tariffs_inline_keyboard(),
            )
            await callback.answer()
            return

        spent = spend_generation(user_id, 1)
        if not spent:
            track_event(user_id, "limit_reached", category=session["category"], value="refinement")
            await callback.message.answer(
                "Бесплатные доработки уже закончились, а лимит генераций тоже на нуле 😅\n\nВыберите тариф, и продолжим 👇",
                reply_markup=tariffs_inline_keyboard(),
            )
            await callback.answer()
            return

    track_event(
        user_id,
        "generation_started",
        category=session["category"],
        value=f"refinement:{refinement_type}",
        meta={"session_id": session_id, "current_length": len(current_text)},
    )

    await callback.answer("Докручиваю ✨")
    wait_msg = await callback.message.answer("⏳ Дорабатываю результат...")

    try:
        system_prompt = (
            "Ты сильный русскоязычный редактор. "
            "Сохраняй смысл, усиливай подачу, не уходи в канцелярит. "
            "Отвечай только готовым финальным текстом на русском языке."
        )
        user_prompt = build_refinement_prompt(refinement_type, current_text)

        result_text = await asyncio.to_thread(
            call_openai_text,
            system_prompt,
            user_prompt,
        )

        update_generation_session_text(session_id, result_text, increment_refinement=True)
        save_history(user_id, session["category"], "assistant", result_text, is_final=True)

        track_event(
            user_id,
            "generation_success",
            category=session["category"],
            value=f"refinement:{refinement_type}",
            meta={"response_length": len(result_text)},
        )

        await wait_msg.delete()
        await callback.message.answer(
            f"✨ Обновил результат:\n\n{result_text}",
            reply_markup=result_inline_keyboard(session_id),
        )

        refinement_count_after = int(session.get("refinement_count", 0)) + 1
        if refinement_count_after < FREE_REFINEMENTS_COUNT:
            free_left = FREE_REFINEMENTS_COUNT - refinement_count_after
            await callback.message.answer(
                f"🎁 Бесплатных доработок осталось: {free_left}",
                reply_markup=main_menu_keyboard(),
            )
        else:
            await callback.message.answer(
                "Готово! Если захотите, можем ещё подкрутить результат 😉",
                reply_markup=main_menu_keyboard(),
            )
    except Exception as e:
        logger.exception("Refinement failed: %s", e)
        track_event(
            user_id,
            "generation_failed",
            category=session["category"],
            value=f"refinement:{refinement_type}",
            meta={"error": str(e)[:500]},
        )
        await wait_msg.delete()
        await callback.message.answer(
            "Кажется произошла ошибка 😅 Попробуем ещё раз?",
            reply_markup=main_menu_keyboard(),
        )

# =========================
# COMMANDS / COMMON HANDLERS
# =========================

@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    user_id = message.from_user.id
    ensure_user_exists(user_id)
    refresh_expired_plan_if_needed(user_id)
    track_event(user_id, "main_menu_opened")

    await state.clear()

    text = (
        "Привет! Я PosteraAI 👋\n\n"
        "Помогу быстро подготовить пост, придумать идеи или переписать текст под нужную задачу.\n\n"
        "Выберите, с чем помочь 👇"
    )
    await message.answer(text, reply_markup=main_menu_keyboard())

@dp.message(Command("menu"))
async def cmd_menu(message: Message, state: FSMContext):
    await state.clear()
    track_event(message.from_user.id, "main_menu_opened")
    await send_main_menu(message, "Главное меню уже здесь 👇")

@dp.message(Command("reset"))
async def cmd_reset(message: Message, state: FSMContext):
    await state.clear()
    await send_main_menu(message, "Контекст текущего шага сбросил. Продолжаем 👌")

@dp.message(Command("mytariff"))
async def cmd_my_tariff(message: Message):
    user_id = message.from_user.id
    refresh_expired_plan_if_needed(user_id)
    user = get_user(user_id)
    track_event(user_id, "my_tariff_opened")

    username = message.from_user.username
    await message.answer(
        get_my_tariff_text(user, telegram_username=username),
        reply_markup=main_menu_keyboard(),
    )

@dp.message(Command("balance"))
async def cmd_balance(message: Message):
    user_id = message.from_user.id
    refresh_expired_plan_if_needed(user_id)
    user = get_user(user_id)
    await message.answer(
        get_generations_left_text(user),
        reply_markup=main_menu_keyboard(),
    )

@dp.message(Command("settings"))
async def cmd_settings(message: Message, state: FSMContext):
    user_id = message.from_user.id
    user = get_user(user_id)
    await state.clear()
    await message.answer(
        get_settings_text(user),
        reply_markup=settings_keyboard(user),
    )

@dp.message(Command(SPECIAL_RESET_COMMAND.lstrip("/")))
async def cmd_special_reset(message: Message):
    username = message.from_user.username or ""
    if username != SPECIAL_RESET_USERNAME:
        return

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE users
                SET generations_left = %s
                WHERE telegram_id = %s
                """,
                (FREE_GENERATIONS_DEFAULT, message.from_user.id),
            )
        conn.commit()

    await message.answer(
        "✅ Лимит обновлён. Можно продолжать работу 😉",
        reply_markup=main_menu_keyboard(),
    )

# =========================
# MAIN MENU BUTTONS
# =========================

@dp.message(F.text == "✍️ Сгенерировать пост")
async def menu_generate_post(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(GenerationStates.waiting_for_post_prompt)
    track_event(message.from_user.id, "category_selected", category=CATEGORY_POST)
    await message.answer(
        get_prompt_request_text(CATEGORY_POST),
        reply_markup=main_menu_keyboard(),
    )

@dp.message(F.text == "💡 Идеи постов")
async def menu_post_ideas(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(GenerationStates.waiting_for_ideas_prompt)
    track_event(message.from_user.id, "category_selected", category=CATEGORY_IDEAS)
    await message.answer(
        get_prompt_request_text(CATEGORY_IDEAS),
        reply_markup=main_menu_keyboard(),
    )

@dp.message(F.text == "♻️ Переписать текст")
async def menu_rewrite_text(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(GenerationStates.waiting_for_rewrite_prompt)
    track_event(message.from_user.id, "category_selected", category=CATEGORY_REWRITE)
    await message.answer(
        get_prompt_request_text(CATEGORY_REWRITE),
        reply_markup=main_menu_keyboard(),
    )

@dp.message(F.text == "👤 Мой тариф")
async def menu_my_tariff(message: Message):
    user_id = message.from_user.id
    refresh_expired_plan_if_needed(user_id)
    user = get_user(user_id)
    track_event(user_id, "my_tariff_opened")

    await message.answer(
        get_my_tariff_text(user, telegram_username=message.from_user.username),
        reply_markup=main_menu_keyboard(),
    )

@dp.message(F.text == "📊 Остаток генераций")
async def menu_balance(message: Message):
    user_id = message.from_user.id
    refresh_expired_plan_if_needed(user_id)
    user = get_user(user_id)
    await message.answer(
        get_generations_left_text(user),
        reply_markup=main_menu_keyboard(),
    )

@dp.message(F.text == "🕘 Настройка бота")
async def menu_settings(message: Message, state: FSMContext):
    user = get_user(message.from_user.id)
    await state.clear()
    await message.answer(
        get_settings_text(user),
        reply_markup=settings_keyboard(user),
    )

@dp.message(F.text == "⬅️ Назад в меню")
async def back_to_menu_message(message: Message, state: FSMContext):
    await state.clear()
    await send_main_menu(message, "Главное меню снова перед вами 👇")

# =========================
# SETTINGS
# =========================

@dp.message(F.text == "➕ Добавить пример текста")
async def add_style_sample_start(message: Message, state: FSMContext):
    await state.set_state(SettingsStates.waiting_for_style_sample)
    await message.answer(
        "Пришлите один или несколько примеров вашего текста.\n\n"
        "Я сохраню подачу и буду учитывать её в следующих генерациях ✍️",
        reply_markup=settings_keyboard(get_user(message.from_user.id)),
    )

@dp.message(F.text == "🗑 Очистить стиль")
async def clear_style(message: Message):
    clear_style_samples(message.from_user.id)
    await message.answer(
        "✅ Примеры стиля очищены.\nТеперь буду опираться только на новые вводные.",
        reply_markup=settings_keyboard(get_user(message.from_user.id)),
    )

@dp.message(F.text.in_(["🧠 Память: вкл", "🧠 Память: выкл"]))
async def toggle_memory(message: Message):
    user = get_user(message.from_user.id)
    new_value = not bool(user.get("memory_enabled", True))
    set_memory_enabled(message.from_user.id, new_value)

    text = (
        "🧠 Память включена.\nБуду учитывать прошлый контекст в этой категории."
        if new_value
        else "🧠 Память выключена.\nНовые генерации будут без учёта прошлого контекста."
    )
    updated_user = get_user(message.from_user.id)
    await message.answer(
        text,
        reply_markup=settings_keyboard(updated_user),
    )

@dp.message(SettingsStates.waiting_for_style_sample)
async def receive_style_sample(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if not text:
        await message.answer("Нужен именно текстовый пример ✍️")
        return

    add_style_sample(message.from_user.id, text)
    await state.clear()
    await message.answer(
        "✅ Сохранил пример стиля.\nБуду учитывать подачу в следующих ответах 😉",
        reply_markup=settings_keyboard(get_user(message.from_user.id)),
    )

# =========================
# GENERATION INPUTS
# =========================

@dp.message(GenerationStates.waiting_for_post_prompt)
async def handle_post_prompt(message: Message, state: FSMContext):
    user_prompt = (message.text or "").strip()
    if not user_prompt:
        await message.answer("Пришлите тему или вводные текстом ✍️")
        return

    await state.clear()
    await start_generation_flow(message, CATEGORY_POST, user_prompt)

@dp.message(GenerationStates.waiting_for_ideas_prompt)
async def handle_ideas_prompt(message: Message, state: FSMContext):
    user_prompt = (message.text or "").strip()
    if not user_prompt:
        await message.answer("Пришлите тему, нишу или продукт текстом 💡")
        return

    await state.clear()
    await start_generation_flow(message, CATEGORY_IDEAS, user_prompt)

@dp.message(GenerationStates.waiting_for_rewrite_prompt)
async def handle_rewrite_prompt(message: Message, state: FSMContext):
    user_prompt = (message.text or "").strip()
    if not user_prompt:
        await message.answer("Пришлите текст, который нужно переписать ♻️")
        return

    await state.clear()
    await start_generation_flow(message, CATEGORY_REWRITE, user_prompt)

# =========================
# INLINE NAVIGATION
# =========================

@dp.callback_query(F.data == "back_to_menu")
async def back_to_menu_callback(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()
    await send_main_menu_from_callback(callback, "Возвращаемся в меню 👇")

@dp.callback_query(F.data.startswith("copy:"))
async def copy_result_callback(callback: CallbackQuery):
    try:
        session_id = int(callback.data.split(":")[1])
    except Exception:
        await callback.answer("Не удалось скопировать", show_alert=True)
        return

    session = get_generation_session(session_id)
    if not session:
        await callback.answer("Сессия не найдена", show_alert=True)
        return

    text = session.get("generated_text", "").strip()
    if not text:
        await callback.answer("Текст пустой", show_alert=True)
        return

    await callback.answer("Отправляю текст отдельным сообщением 👌")
    await callback.message.answer(
        f"📋 Вот текст для удобного копирования:\n\n{text}",
        reply_markup=result_inline_keyboard(session_id),
    )

@dp.callback_query(F.data.startswith("refine:"))
async def refine_callback(callback: CallbackQuery):
    try:
        session_id = int(callback.data.split(":")[1])
    except Exception:
        await callback.answer("Не удалось открыть доработки", show_alert=True)
        return

    session = get_generation_session(session_id)
    if not session:
        await callback.answer("Сессия не найдена", show_alert=True)
        return

    refinement_count = int(session.get("refinement_count", 0))
    free_left = max(FREE_REFINEMENTS_COUNT - refinement_count, 0)

    text = (
        "✏️ Выберите, как доработать результат:\n\n"
        "✂️ Короче — сокращает без потери смысла\n"
        "📱 Под Telegram — делает живее и разговорнее\n"
        "💰 Продающим — усиливает оффер и CTA\n"
        "🎯 Добавить структуру — делает текст удобнее для чтения\n"
        "✨ Улучшить стиль — усиливает формулировки"
    )

    if free_left > 0:
        text += f"\n\n🎁 Бесплатных доработок осталось: {free_left}"

    await callback.answer()
    await callback.message.answer(
        text,
        reply_markup=refinement_inline_keyboard(session_id),
    )

@dp.callback_query(F.data.startswith("regen:"))
async def regenerate_callback(callback: CallbackQuery):
    try:
        session_id = int(callback.data.split(":")[1])
    except Exception:
        await callback.answer("Не удалось перегенерировать", show_alert=True)
        return

    await regenerate_from_session(callback, session_id)

@dp.callback_query(F.data.startswith("refine_type:"))
async def refine_type_callback(callback: CallbackQuery):
    try:
        _, session_id_str, refinement_type = callback.data.split(":")
        session_id = int(session_id_str)
    except Exception:
        await callback.answer("Не удалось применить доработку", show_alert=True)
        return

    await apply_refinement(callback, session_id, refinement_type)

# =========================
# FALLBACK TEXT
# =========================

@dp.message()
async def fallback_handler(message: Message, state: FSMContext):
    text = (message.text or "").strip()

    if not text:
        await message.answer(
            "Пока умею работать в текстовом формате 🙂",
            reply_markup=main_menu_keyboard(),
        )
        return

    await state.clear()
    await message.answer(
        "Я готов помочь 👌\n\n"
        "Выберите режим в меню:\n"
        "✍️ Сгенерировать пост\n"
        "💡 Идеи постов\n"
        "♻️ Переписать текст",
        reply_markup=main_menu_keyboard(),
    )

# =========================
# END PART 2
# =========================

# =========================
# YOOKASSA
# =========================

def get_tariff_price_and_amount(tariff: str) -> tuple[int, str]:
    if tariff == TARIFF_CREATOR:
        return 990, "990.00"
    if tariff == TARIFF_UNLIM:
        return 1990, "1990.00"
    raise ValueError("Unknown tariff")

def get_tariff_description(tariff: str) -> str:
    if tariff == TARIFF_CREATOR:
        return "Оплата тарифа Creator"
    if tariff == TARIFF_UNLIM:
        return "Оплата тарифа Unlim"
    return "Оплата тарифа"

async def create_yookassa_payment(user_id: int, tariff: str) -> Optional[dict]:
    if not YOOKASSA_SHOP_ID or not YOOKASSA_SECRET_KEY:
        raise RuntimeError("YOOKASSA credentials are not set")

    amount_int, amount_str = get_tariff_price_and_amount(tariff)
    idempotence_key = str(uuid.uuid4())

    payload = {
        "amount": {
            "value": amount_str,
            "currency": "RUB",
        },
        "capture": True,
        "confirmation": {
            "type": "redirect",
            "return_url": PAYMENT_RETURN_URL,
        },
        "description": get_tariff_description(tariff),
        "metadata": {
            "telegram_id": str(user_id),
            "tariff": tariff,
        },
    }

    auth = aiohttp.BasicAuth(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY)
    headers = {
        "Idempotence-Key": idempotence_key,
        "Content-Type": "application/json",
    }

    logger.info("Creating YooKassa payment | user_id=%s | tariff=%s", user_id, tariff)

    async with aiohttp.ClientSession(auth=auth) as session:
        async with session.post(
            YOOKASSA_API_URL,
            json=payload,
            headers=headers,
            timeout=30,
        ) as resp:
            text = await resp.text()
            if resp.status not in (200, 201):
                logger.error("YooKassa create payment failed | status=%s | body=%s", resp.status, text)
                raise RuntimeError(f"YooKassa error: {resp.status} {text}")

            data = json.loads(text)

    payment_id = data.get("id")
    if not payment_id:
        raise RuntimeError("YooKassa payment_id missing in response")

    create_payment_record(
        user_id=user_id,
        amount=amount_int,
        tariff=tariff,
        status="pending",
        payment_id=payment_id,
    )

    track_event(
        user_id,
        "payment_created",
        value=tariff,
        meta={"payment_id": payment_id, "amount": amount_int},
    )

    logger.info("YooKassa payment created | payment_id=%s | user_id=%s", payment_id, user_id)
    return data

def activate_tariff_for_user(user_id: int, tariff: str):
    expires_at = datetime.now() + timedelta(days=30)

    if tariff == TARIFF_CREATOR:
        update_user_tariff(
            user_id=user_id,
            tariff=TARIFF_CREATOR,
            generations_left=CREATOR_GENERATIONS_DEFAULT,
            plan_expires_at=expires_at,
        )
    elif tariff == TARIFF_UNLIM:
        update_user_tariff(
            user_id=user_id,
            tariff=TARIFF_UNLIM,
            generations_left=999999,
            plan_expires_at=expires_at,
        )
    else:
        raise ValueError(f"Unknown tariff: {tariff}")

async def notify_user_payment_success(user_id: int, tariff: str):
    try:
        text = (
            f"✅ Оплата прошла успешно!\n\n"
            f"Тариф {get_tariff_title(tariff)} активирован.\n"
            f"Можно продолжать работу без пауз 🚀"
        )
        await bot.send_message(
            chat_id=user_id,
            text=text,
            reply_markup=main_menu_keyboard(),
        )
    except Exception as e:
        logger.exception("Failed to notify user about payment success: %s", e)

# =========================
# PAYMENT CALLBACKS
# =========================

@dp.callback_query(F.data == "buy_creator")
async def buy_creator_callback(callback: CallbackQuery):
    user_id = callback.from_user.id
    track_event(user_id, "payment_screen_opened", value=TARIFF_CREATOR)

    try:
        payment_data = await create_yookassa_payment(user_id, TARIFF_CREATOR)
        confirmation = payment_data.get("confirmation", {}) or {}
        confirmation_url = confirmation.get("confirmation_url")

        if not confirmation_url:
            raise RuntimeError("confirmation_url not found")

        await callback.answer()
        await callback.message.answer(
            "💳 Платёж для тарифа Creator готов.\n\n"
            f"Перейдите по ссылке для оплаты:\n{confirmation_url}\n\n"
            "После успешной оплаты тариф активируется автоматически ✨",
            reply_markup=main_menu_keyboard(),
        )
    except Exception as e:
        logger.exception("Failed to create Creator payment: %s", e)
        await callback.answer("Не удалось создать платёж", show_alert=True)
        await callback.message.answer(
            "Не получилось создать платёж 😅 Попробуем ещё раз чуть позже?",
            reply_markup=main_menu_keyboard(),
        )

@dp.callback_query(F.data == "buy_unlim")
async def buy_unlim_callback(callback: CallbackQuery):
    user_id = callback.from_user.id
    username = callback.from_user.username or ""

    # по твоему правилу: для babajanyanak скрываем unlim как полноценную опцию
    if username == SPECIAL_RESET_USERNAME:
        await callback.answer("Этот тариф сейчас недоступен", show_alert=True)
        return

    track_event(user_id, "payment_screen_opened", value=TARIFF_UNLIM)

    try:
        payment_data = await create_yookassa_payment(user_id, TARIFF_UNLIM)
        confirmation = payment_data.get("confirmation", {}) or {}
        confirmation_url = confirmation.get("confirmation_url")

        if not confirmation_url:
            raise RuntimeError("confirmation_url not found")

        await callback.answer()
        await callback.message.answer(
            "💳 Платёж для тарифа Unlim готов.\n\n"
            f"Перейдите по ссылке для оплаты:\n{confirmation_url}\n\n"
            "После успешной оплаты тариф активируется автоматически ✨",
            reply_markup=main_menu_keyboard(),
        )
    except Exception as e:
        logger.exception("Failed to create Unlim payment: %s", e)
        await callback.answer("Не удалось создать платёж", show_alert=True)
        await callback.message.answer(
            "Не получилось создать платёж 😅 Попробуем ещё раз чуть позже?",
            reply_markup=main_menu_keyboard(),
        )

# =========================
# WEBHOOK / HTTP SERVER
# =========================

async def health_handler(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})

async def yookassa_webhook_handler(request: web.Request) -> web.Response:
    try:
        payload = await request.json()
        logger.info("YooKassa webhook received: %s", payload)

        event_type = payload.get("event")
        obj = payload.get("object", {}) or {}
        payment_id = obj.get("id")
        payment_status = obj.get("status")

        metadata = obj.get("metadata", {}) or {}
        tariff = metadata.get("tariff")
        telegram_id_raw = metadata.get("telegram_id")

        if not payment_id:
            return web.json_response({"ok": False, "error": "payment_id_missing"}, status=400)

        update_payment_status(payment_id, payment_status or "unknown")

        if event_type == "payment.succeeded" and payment_status == "succeeded":
            payment_record = get_payment_by_payment_id(payment_id)

            # если metadata вдруг не пришла, подстрахуемся из записи в БД
            if payment_record:
                if not tariff:
                    tariff = payment_record.get("tariff")
                if not telegram_id_raw:
                    telegram_id_raw = payment_record.get("telegram_id")

            if not tariff or not telegram_id_raw:
                logger.error("Webhook missing tariff or telegram_id | payment_id=%s", payment_id)
                return web.json_response({"ok": False, "error": "missing_metadata"}, status=400)

            user_id = int(telegram_id_raw)

            activate_tariff_for_user(user_id, tariff)
            track_event(
                user_id,
                "payment_success",
                value=tariff,
                meta={"payment_id": payment_id},
            )

            await notify_user_payment_success(user_id, tariff)

        return web.json_response({"ok": True})
    except Exception as e:
        logger.exception("Webhook handler failed: %s", e)
        return web.json_response({"ok": False, "error": str(e)}, status=500)

async def start_http_server() -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/health", health_handler)
    app.router.add_post("/yookassa/webhook", yookassa_webhook_handler)

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()

    logger.info("HTTP server started on 0.0.0.0:%s", PORT)
    return runner

# =========================
# EXTRA PATCHES / UX FIXES
# =========================

def hide_unlim_for_username(username: Optional[str]) -> bool:
    return (username or "") == SPECIAL_RESET_USERNAME

def tariffs_inline_keyboard_for_user(username: Optional[str]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="💳 Оплатить Creator", callback_data="buy_creator")],
    ]

    if not hide_unlim_for_username(username):
        rows.append([InlineKeyboardButton(text="💳 Оплатить Unlim", callback_data="buy_unlim")])

    rows.append([InlineKeyboardButton(text="🏠 В меню", callback_data="back_to_menu")])

    return InlineKeyboardMarkup(inline_keyboard=rows)

# Переопределим выдачу тарифов с учетом скрытия Unlim для нужного пользователя
@dp.message(F.text == "💳 Тарифы")
async def menu_tariffs_v2(message: Message):
    user_id = message.from_user.id
    track_event(user_id, "payment_screen_opened", value="tariffs")
    await message.answer(
        get_tariffs_text(get_user(user_id)),
        reply_markup=tariffs_inline_keyboard_for_user(message.from_user.username),
    )

@dp.message(Command("tariffs"))
async def cmd_tariffs_v2(message: Message):
    user_id = message.from_user.id
    track_event(user_id, "payment_screen_opened", value="tariffs")
    await message.answer(
        get_tariffs_text(get_user(user_id)),
        reply_markup=tariffs_inline_keyboard_for_user(message.from_user.username),
    )

# =========================
# SAFE OVERRIDES
# =========================

async def safe_delete_message(message: Message):
    try:
        await message.delete()
    except Exception:
        pass

# =========================
# MAIN
# =========================

async def main():
    logger.info("Bot starting...")

    init_db()
    logger.info("DB initialized")

    await start_http_server()
    logger.info("Bot polling started")

    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped")

# =========================
# FALLBACK
# =========================

@dp.message()
async def fallback_handler(message: Message, state: FSMContext):
    text = (message.text or "").strip()

    await state.clear()

    await message.answer(
        "Я готов помочь 👌\n\n"
        "Выберите режим в меню:",
        reply_markup=main_menu_keyboard(),
    )