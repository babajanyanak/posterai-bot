import os
import json
import uuid
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

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
HISTORY_LIMIT_PER_CATEGORY = 20

YOOKASSA_API_URL = "https://api.yookassa.ru/v3/payments"

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
    waiting_for_audience_input = State()
    waiting_for_custom_refinement = State()

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
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id BIGINT PRIMARY KEY,
                    tariff TEXT DEFAULT 'free',
                    generations_left INT DEFAULT 10,
                    plan_expires_at TIMESTAMP,
                    memory_enabled BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_history (
                    id SERIAL PRIMARY KEY,
                    telegram_id BIGINT,
                    category TEXT,
                    role TEXT,
                    text TEXT,
                    is_final BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS analytics_events (
                    id SERIAL PRIMARY KEY,
                    telegram_id BIGINT,
                    event_name TEXT NOT NULL,
                    category TEXT,
                    value TEXT,
                    meta JSONB,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS payments (
                    id SERIAL PRIMARY KEY,
                    telegram_id BIGINT,
                    amount INT,
                    tariff TEXT,
                    status TEXT,
                    payment_id TEXT,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS generation_sessions (
                    id SERIAL PRIMARY KEY,
                    telegram_id BIGINT,
                    category TEXT,
                    original_prompt TEXT,
                    generated_text TEXT,
                    refinement_count INT DEFAULT 0,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            # Stores full refinement history per session for multi-turn context
            cur.execute("""
                CREATE TABLE IF NOT EXISTS session_refinement_history (
                    id SERIAL PRIMARY KEY,
                    session_id INT,
                    role TEXT,
                    content TEXT,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS style_posts (
                    id SERIAL PRIMARY KEY,
                    telegram_id BIGINT,
                    text TEXT,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_style_samples (
                    id SERIAL PRIMARY KEY,
                    telegram_id BIGINT,
                    text TEXT,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_category_memory (
                    telegram_id BIGINT,
                    category TEXT,
                    last_prompt TEXT,
                    PRIMARY KEY (telegram_id, category)
                )
            """)

            # migrations
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
            cur.execute("ALTER TABLE payments ALTER COLUMN product_code DROP NOT NULL")
            cur.execute("ALTER TABLE payments ALTER COLUMN idempotence_key DROP NOT NULL")
            cur.execute("ALTER TABLE generation_sessions ADD COLUMN IF NOT EXISTS category TEXT")
            cur.execute("ALTER TABLE generation_sessions ADD COLUMN IF NOT EXISTS original_prompt TEXT")
            cur.execute("ALTER TABLE generation_sessions ADD COLUMN IF NOT EXISTS generated_text TEXT")
            cur.execute("ALTER TABLE generation_sessions ADD COLUMN IF NOT EXISTS refinement_count INT DEFAULT 0")
            cur.execute("ALTER TABLE generation_sessions ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT NOW()")
            cur.execute("ALTER TABLE user_style_samples ADD COLUMN IF NOT EXISTS text TEXT")
            cur.execute("ALTER TABLE user_style_samples ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT NOW()")

            cur.execute("CREATE INDEX IF NOT EXISTS idx_analytics_events_telegram_id ON analytics_events (telegram_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_analytics_events_event_name ON analytics_events (event_name)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_analytics_events_created_at ON analytics_events (created_at)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_user_history_telegram_id ON user_history (telegram_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_generation_sessions_telegram_id ON generation_sessions (telegram_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_session_refinement_history_session_id ON session_refinement_history (session_id)")

        conn.commit()
    logger.info("DB initialized")

# =========================
# ANALYTICS
# =========================

def track_event(user_id: int, event_name: str, category: Optional[str] = None,
                value: Optional[str] = None, meta: Optional[dict] = None):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO analytics_events (telegram_id, event_name, category, value, meta) VALUES (%s,%s,%s,%s,%s)",
                    (user_id, event_name, category, value, json.dumps(meta or {}, ensure_ascii=False)),
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
                "INSERT INTO users (telegram_id, tariff, generations_left, memory_enabled) VALUES (%s,%s,%s,%s) ON CONFLICT (telegram_id) DO NOTHING",
                (user_id, TARIFF_FREE, FREE_GENERATIONS_DEFAULT, True),
            )
        conn.commit()

def get_user(user_id: int) -> dict:
    ensure_user_exists(user_id)
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE telegram_id = %s", (user_id,))
            row = cur.fetchone()
            return dict(row) if row else {}

def update_user_tariff(user_id: int, tariff: str, generations_left: Optional[int] = None,
                       plan_expires_at: Optional[datetime] = None):
    ensure_user_exists(user_id)
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET tariff=%s, generations_left=COALESCE(%s,generations_left), plan_expires_at=%s WHERE telegram_id=%s",
                (tariff, generations_left, plan_expires_at, user_id),
            )
        conn.commit()

def set_memory_enabled(user_id: int, enabled: bool):
    ensure_user_exists(user_id)
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET memory_enabled=%s WHERE telegram_id=%s", (enabled, user_id))
        conn.commit()

def is_subscription_active(user: dict) -> bool:
    plan_expires_at = user.get("plan_expires_at")
    if not plan_expires_at:
        return False
    return plan_expires_at > datetime.now()

def refresh_expired_plan_if_needed(user_id: int):
    user = get_user(user_id)
    if user.get("tariff", TARIFF_FREE) == TARIFF_FREE:
        return
    plan_expires_at = user.get("plan_expires_at")
    if plan_expires_at and plan_expires_at <= datetime.now():
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE users SET tariff=%s, generations_left=%s, plan_expires_at=NULL WHERE telegram_id=%s",
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
    if user.get("tariff") == TARIFF_UNLIM and is_subscription_active(user):
        return True
    return user.get("generations_left", 0) > 0

def spend_generation(user_id: int, amount: int = 1) -> bool:
    refresh_expired_plan_if_needed(user_id)
    user = get_user(user_id)
    if user.get("tariff") == TARIFF_UNLIM and is_subscription_active(user):
        return True
    if user.get("generations_left", 0) < amount:
        return False
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET generations_left=GREATEST(generations_left-%s,0) WHERE telegram_id=%s",
                (amount, user_id),
            )
        conn.commit()
    return True

def add_generations(user_id: int, amount: int):
    ensure_user_exists(user_id)
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET generations_left=generations_left+%s WHERE telegram_id=%s",
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
                "INSERT INTO user_history (telegram_id, category, role, text, is_final) VALUES (%s,%s,%s,%s,%s)",
                (user_id, category, role, text, is_final),
            )
        conn.commit()

def get_last_history_items(user_id: int, category: Optional[str] = None,
                           limit: int = HISTORY_LIMIT_PER_CATEGORY) -> list[dict]:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            if category:
                cur.execute(
                    "SELECT * FROM user_history WHERE telegram_id=%s AND category=%s ORDER BY created_at DESC LIMIT %s",
                    (user_id, category, limit),
                )
            else:
                cur.execute(
                    "SELECT * FROM user_history WHERE telegram_id=%s ORDER BY created_at DESC LIMIT %s",
                    (user_id, limit),
                )
            return [dict(r) for r in (cur.fetchall() or [])]

def clear_history(user_id: int, category: Optional[str] = None):
    """Delete history and category memory. Style samples are NOT touched."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            if category:
                cur.execute("DELETE FROM user_history WHERE telegram_id=%s AND category=%s", (user_id, category))
                cur.execute("DELETE FROM user_category_memory WHERE telegram_id=%s AND category=%s", (user_id, category))
            else:
                cur.execute("DELETE FROM user_history WHERE telegram_id=%s", (user_id,))
                cur.execute("DELETE FROM user_category_memory WHERE telegram_id=%s", (user_id,))
        conn.commit()

def save_category_memory(user_id: int, category: str, prompt: str):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO user_category_memory (telegram_id, category, last_prompt) VALUES (%s,%s,%s) "
                "ON CONFLICT (telegram_id, category) DO UPDATE SET last_prompt=EXCLUDED.last_prompt",
                (user_id, category, prompt),
            )
        conn.commit()

def get_category_memory(user_id: int, category: str) -> Optional[str]:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT last_prompt FROM user_category_memory WHERE telegram_id=%s AND category=%s",
                (user_id, category),
            )
            row = cur.fetchone()
            return row["last_prompt"] if row else None

def add_style_sample(user_id: int, text: str):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO user_style_samples (telegram_id, text) VALUES (%s,%s)", (user_id, text))
        conn.commit()

def clear_style_samples(user_id: int):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM user_style_samples WHERE telegram_id=%s", (user_id,))
        conn.commit()

def get_style_samples(user_id: int, limit: int = 5) -> list[str]:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT text FROM user_style_samples WHERE telegram_id=%s ORDER BY created_at DESC LIMIT %s",
                (user_id, limit),
            )
            return [r["text"] for r in (cur.fetchall() or [])]

# =========================
# GENERATION SESSIONS
# =========================

def create_generation_session(user_id: int, category: str, original_prompt: str, generated_text: str) -> int:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO generation_sessions (telegram_id, category, original_prompt, generated_text, refinement_count) "
                "VALUES (%s,%s,%s,%s,0) RETURNING id",
                (user_id, category, original_prompt, generated_text),
            )
            row = cur.fetchone()
        conn.commit()
    session_id = int(row["id"])
    # Save the initial generated text as first assistant message in refinement history
    save_refinement_history(session_id, "assistant", generated_text)
    return session_id

def get_generation_session(session_id: int) -> Optional[dict]:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM generation_sessions WHERE id=%s", (session_id,))
            row = cur.fetchone()
            return dict(row) if row else None

def update_generation_session_text(session_id: int, generated_text: str, increment_refinement: bool = False):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            if increment_refinement:
                cur.execute(
                    "UPDATE generation_sessions SET generated_text=%s, refinement_count=refinement_count+1 WHERE id=%s",
                    (generated_text, session_id),
                )
            else:
                cur.execute(
                    "UPDATE generation_sessions SET generated_text=%s WHERE id=%s",
                    (generated_text, session_id),
                )
        conn.commit()

# =========================
# SESSION REFINEMENT HISTORY (multi-turn context)
# =========================

def save_refinement_history(session_id: int, role: str, content: str):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO session_refinement_history (session_id, role, content) VALUES (%s,%s,%s)",
                (session_id, role, content),
            )
        conn.commit()

def get_refinement_history(session_id: int) -> list[dict]:
    """Returns full conversation history for this session ordered oldest first."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT role, content FROM session_refinement_history WHERE session_id=%s ORDER BY created_at ASC",
                (session_id,),
            )
            return [dict(r) for r in (cur.fetchall() or [])]

# =========================
# PAYMENTS
# =========================

def create_payment_record(user_id: int, amount: int, tariff: str, status: str,
                          payment_id: Optional[str] = None, generations: Optional[int] = None):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO payments (telegram_id, amount, tariff, status, payment_id) VALUES (%s,%s,%s,%s,%s)",
                (user_id, amount, tariff, status, payment_id),
            )
        conn.commit()

def update_payment_status(payment_id: str, status: str):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE payments SET status=%s WHERE payment_id=%s", (status, payment_id))
        conn.commit()

def get_payment_by_payment_id(payment_id: str) -> Optional[dict]:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM payments WHERE payment_id=%s ORDER BY created_at DESC LIMIT 1",
                (payment_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None

# =========================
# TEXT / PROMPTS HELPERS
# =========================

def get_system_prompt(category: str) -> str:
    base = (
        "Ты сильный русскоязычный редактор и контент-стратег. "
        "Пишешь понятно, живо, по делу, без канцелярита. "
        "Учитывай, что тексты нужны для Telegram и digital-среды. "
        "Если уместно, делай текст более вовлекающим, но без перегиба."
    )
    if category == CATEGORY_POST:
        return base + (
            " Сгенерируй готовый пост на русском языке. "
            "Он должен быть читабельным, современным, с хорошей структурой, "
            "естественными абзацами и сильным первым экраном."
        )
    if category == CATEGORY_IDEAS:
        return base + (
            " Сгенерируй список идей постов на русском языке. "
            "Идеи должны быть конкретными, небанальными и пригодными для Telegram. "
            "Пронумеруй их."
        )
    if category == CATEGORY_REWRITE:
        return base + (
            " Перепиши текст на русском языке. "
            "Сохраняй смысл, но делай подачу сильнее, чище и удобнее для восприятия."
        )
    return base

def build_user_prompt(category: str, user_prompt: str,
                      memory_text: Optional[str] = None,
                      style_samples: Optional[list] = None) -> str:
    parts = []
    if memory_text:
        parts.append(f"Контекст прошлой генерации пользователя по этой категории:\n{memory_text}")
    if style_samples:
        joined = "\n\n---\n\n".join(style_samples)
        parts.append(
            "Примеры пользовательского стиля. "
            "Не копируй слово в слово, но учитывай ритм, подачу и тон:\n" + joined
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

def get_refinement_instruction(refinement_type: str, extra: str = "") -> str:
    """Returns just the instruction string for a refinement type."""
    mapping = {
        "shorter": (
            "Сократи текст радикально — убери всё второстепенное, оставь только суть. "
            "Текст должен стать заметно короче, а не чуть-чуть."
        ),
        "selling": (
            "Сделай текст более продающим: усили оффер, ценность и мотивацию читателя."
        ),
        "lively": (
            "Сделай текст живее: легче, естественнее и эмоциональнее. Убери сухость и канцелярит."
        ),
        "cta": (
            "Добавь в текст чёткий призыв к действию (CTA). "
            "Например: подпишись, напиши, попробуй, перейди — выбери подходящий по контексту."
        ),
        "telegram": (
            "Адаптируй текст под формат Telegram: "
            "короткие абзацы, удобный ритм, разговорный стиль, высокая читаемость с телефона."
        ),
        "web": (
            "Адаптируй текст под веб-формат: структурированные абзацы, подходящий тон для сайта или новостного топика, "
            "без излишней разговорности, с чётким изложением."
        ),
        "audience": (
            f"Адаптируй текст для следующей аудитории: {extra}. Учти их язык, боли и интересы."
        ),
        "deeper": (
            "Сделай идеи менее банальными и более содержательными. "
            "Добавь экспертную глубину и нестандартные углы."
        ),
        "bolder": (
            "Сделай идеи провокационнее, ярче и цепляющими. Не бойся смелых формулировок."
        ),
        "cleaner": (
            "Сделай текст чище: убери канцелярит, упрости язык, сделай его легче для восприятия."
        ),
        "structure": (
            "Улучши структуру текста: добавь логические блоки, "
            "улучши последовательность, сделай текст более читабельным."
        ),
        "custom": extra,
    }
    return mapping.get(refinement_type, "Улучши текст.")

# =========================
# KEYBOARDS
# =========================

def main_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="✍️ Сгенерировать пост"), KeyboardButton(text="💡 Идеи для постов")],
            [KeyboardButton(text="🔁 Переписать текст"), KeyboardButton(text="⚙️ Настроить бота")],
            [KeyboardButton(text="📊 Остаток генераций"), KeyboardButton(text="💳 Тарифы")],
        ],
        resize_keyboard=True,
    )

def settings_keyboard(user: dict) -> ReplyKeyboardMarkup:
    memory_enabled = bool(user.get("memory_enabled", True))
    memory_button = "✅ Память включена" if memory_enabled else "⛔ Память выключена"
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=memory_button)],
            [KeyboardButton(text="♻️ Сбросить память")],
            [KeyboardButton(text="✍️ Копировать стиль")],
            [KeyboardButton(text="🗑 Очистить стиль")],
            [KeyboardButton(text="⬅️ Назад в меню")],
        ],
        resize_keyboard=True,
    )

def style_sample_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="➕ Добавить ещё", callback_data="style_add_more"),
                InlineKeyboardButton(text="✅ Готово", callback_data="style_done"),
            ]
        ]
    )

def hide_unlim_for_username(username: Optional[str]) -> bool:
    return (username or "") == SPECIAL_RESET_USERNAME

def tariffs_inline_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 Creator — 349 ₽/мес", callback_data="buy_creator")],
            [InlineKeyboardButton(text="💳 Unlim — 799 ₽/мес", callback_data="buy_unlim")],
            [InlineKeyboardButton(text="➕ 50 генераций — 99 ₽", callback_data="buy_gens_50")],
            [InlineKeyboardButton(text="➕ 100 генераций — 179 ₽", callback_data="buy_gens_100")],
            [InlineKeyboardButton(text="🏠 В меню", callback_data="back_to_menu")],
        ]
    )

def tariffs_inline_keyboard_for_user(username: Optional[str]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="💳 Creator — 349 ₽/мес", callback_data="buy_creator")]]
    if not hide_unlim_for_username(username):
        rows.append([InlineKeyboardButton(text="💳 Unlim — 799 ₽/мес", callback_data="buy_unlim")])
    rows.append([InlineKeyboardButton(text="➕ 50 генераций — 99 ₽", callback_data="buy_gens_50")])
    rows.append([InlineKeyboardButton(text="➕ 100 генераций — 179 ₽", callback_data="buy_gens_100")])
    rows.append([InlineKeyboardButton(text="🏠 В меню", callback_data="back_to_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def result_inline_keyboard(session_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✏️ Доработать", callback_data=f"refine:{session_id}"),
                InlineKeyboardButton(text="🔁 Другой вариант", callback_data=f"regen:{session_id}"),
            ],
            [InlineKeyboardButton(text="🏠 В меню", callback_data="back_to_menu")],
        ]
    )

def refinement_inline_keyboard(session_id: int, category: str) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="✂️ Короче", callback_data=f"refine_type:{session_id}:shorter"),
            InlineKeyboardButton(text="💰 Продающим", callback_data=f"refine_type:{session_id}:selling"),
        ],
        [
            InlineKeyboardButton(text="✨ Живее", callback_data=f"refine_type:{session_id}:lively"),
            InlineKeyboardButton(text="📣 С CTA", callback_data=f"refine_type:{session_id}:cta"),
        ],
        [
            InlineKeyboardButton(text="📱 Под Telegram", callback_data=f"refine_type:{session_id}:telegram"),
            InlineKeyboardButton(text="🌐 Для сайта", callback_data=f"refine_type:{session_id}:web"),
        ],
        [
            InlineKeyboardButton(text="🎯 Для конкретной ЦА", callback_data=f"refine_type:{session_id}:audience"),
        ],
    ]
    if category == CATEGORY_IDEAS:
        rows.append([
            InlineKeyboardButton(text="🧠 Глубже", callback_data=f"refine_type:{session_id}:deeper"),
            InlineKeyboardButton(text="⚡ Смелее", callback_data=f"refine_type:{session_id}:bolder"),
        ])
    elif category == CATEGORY_REWRITE:
        rows.append([
            InlineKeyboardButton(text="🧼 Чище", callback_data=f"refine_type:{session_id}:cleaner"),
            InlineKeyboardButton(text="🧱 Структурнее", callback_data=f"refine_type:{session_id}:structure"),
        ])
    rows.append([
        InlineKeyboardButton(text="✏️ Своя правка", callback_data=f"refine_type:{session_id}:custom"),
        InlineKeyboardButton(text="🏠 В меню", callback_data="back_to_menu"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)

# =========================
# UI TEXT HELPERS
# =========================

def get_tariff_title(tariff: str) -> str:
    return {"creator": "Creator", "unlim": "Unlim"}.get(tariff, "Free")

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
        lines += ["", "🔐 Для вас доступна служебная команда обновления лимита."]
    lines += ["", "Рад был помочь! Сохраняем ваши предпочтения — в следующих генерациях учтём 😉"]
    return "\n".join(lines)

def get_tariffs_text(user: dict, telegram_username: Optional[str] = None) -> str:
    lines = [
        "💳 Тарифы PosteraAI", "",
        "Free",
        f"• {FREE_GENERATIONS_DEFAULT} генераций на старте",
        "• Базовые режимы",
        "• Первые 2 настройки не расходуют генерации", "",
        "Creator — 349 ₽/мес",
        f"• {CREATOR_GENERATIONS_DEFAULT} генераций в месяц",
        "• Подходит для регулярной работы",
        "• Удобно для контент-потока", "",
        "Unlim — 799 ₽/мес",
        "• Безлимитные генерации",
        "• Для активного ежедневного использования", "",
        "Докупить генерации",
        "• +50 генераций — 99 ₽",
        "• +100 генераций — 179 ₽", "",
        "Выберите подходящий вариант 👇",
    ]
    return "\n".join(lines)

def get_settings_text(user: dict) -> str:
    memory_enabled = "включена" if bool(user.get("memory_enabled", True)) else "выключена"
    style_count = len(get_style_samples(user["telegram_id"]))
    return (
        "⚙️ Настройка бота\n\n"
        f"🧠 Память: {memory_enabled}\n"
        f"✍️ Примеров стиля сохранено: {style_count}\n\n"
        "Здесь можно управлять памятью, добавить примеры своих текстов или очистить стиль."
    )

def get_prompt_request_text(category: str) -> str:
    if category == CATEGORY_POST:
        return (
            "✍️ Напиши тему поста или короткий бриф.\n\n"
            "Можно указать:\n• тему\n• продукт\n• цель поста\n• аудиторию"
        )
    if category == CATEGORY_IDEAS:
        return "💡 Напиши тему, нишу или продукт.\n\nЯ предложу идеи постов для канала."
    if category == CATEGORY_REWRITE:
        return "🔁 Отправь текст, который нужно переписать или улучшить."
    return "Отправьте текст."

def get_refinement_menu_text(session: dict) -> str:
    refinement_count = int(session.get("refinement_count", 0))
    free_left = max(FREE_REFINEMENTS_COUNT - refinement_count, 0)
    category = session.get("category", "")

    base_options = (
        "✂️ Короче — убирает лишнее, сокращает радикально\n"
        "💰 Продающим — усиливает оффер и мотивацию\n"
        "✨ Живее — легче, естественнее, эмоциональнее\n"
        "📣 С CTA — добавляет призыв к действию\n"
        "📱 Под Telegram — адаптирует под формат\n"
        "🌐 Для сайта — адаптирует под веб-формат\n"
        "🎯 Для конкретной ЦА — уточнит аудиторию\n"
    )
    extra = ""
    if category == CATEGORY_IDEAS:
        extra = "🧠 Глубже — менее банально, экспертнее\n⚡ Смелее — провокационнее и ярче\n"
    elif category == CATEGORY_REWRITE:
        extra = "🧼 Чище — убирает канцелярит\n🧱 Структурнее — улучшает логику и блоки\n"

    text = f"✏️ Выберите, как доработать результат:\n\n{base_options}{extra}✏️ Своя правка — напиши свою инструкцию"

    if free_left > 0:
        text += f"\n\nВы можете настроить ответ под себя. Первые {FREE_REFINEMENTS_COUNT} настройки не расходуют генерации. Осталось бесплатно: {free_left}"
    return text

def get_limit_exceeded_text() -> str:
    return "Похоже, лимит генераций закончился 😅\n\nМожно выбрать тариф и продолжить без пауз 👇"

# =========================
# COMMON SENDERS
# =========================

async def send_main_menu(message: Message, text: str):
    await message.answer(text, reply_markup=main_menu_keyboard())

async def send_main_menu_from_callback(callback: CallbackQuery, text: str):
    await callback.message.answer(text, reply_markup=main_menu_keyboard())

# =========================
# OPENAI / GENERATION
# =========================

def call_openai_text(system_prompt: str, messages: list) -> str:
    """Call OpenAI with a full messages list (supports multi-turn)."""
    full_messages = [{"role": "system", "content": system_prompt}] + messages
    response = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=full_messages,
        temperature=0.7,
    )
    if not response.choices:
        raise RuntimeError("OpenAI не вернул choices")
    msg = response.choices[0].message
    if not msg or not msg.content:
        raise RuntimeError("OpenAI не вернул текстовый ответ")
    return msg.content.strip()

def call_openai_simple(system_prompt: str, user_prompt: str) -> str:
    """Simple single-turn call."""
    return call_openai_text(system_prompt, [{"role": "user", "content": user_prompt}])

def build_generation_payload(user_id: int, category: str, user_prompt: str) -> tuple[str, str]:
    user = get_user(user_id)
    memory_enabled = bool(user.get("memory_enabled", True))
    memory_text = get_category_memory(user_id, category) if memory_enabled else None
    # Style samples: always use them regardless of memory toggle (separate setting)
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
    result = await asyncio.to_thread(call_openai_simple, system_prompt, final_user_prompt)
    logger.info("OpenAI generation success | user_id=%s | category=%s", user_id, category)
    return result

async def run_refinement(session_id: int, refinement_type: str, extra: str = "") -> str:
    """
    Run a refinement using full multi-turn history so each iteration builds on all previous ones.
    The history stored in session_refinement_history looks like:
      assistant: <initial text>
      user: <refinement instruction 1>
      assistant: <refined text 1>
      user: <refinement instruction 2>
      ...
    """
    history = get_refinement_history(session_id)
    instruction = get_refinement_instruction(refinement_type, extra)

    # Build messages for OpenAI: existing history + new user instruction
    messages = [{"role": h["role"], "content": h["content"]} for h in history]
    messages.append({"role": "user", "content": instruction})

    system_prompt = (
        "Ты сильный русскоязычный редактор. "
        "Учитывай всю историю правок этого текста. "
        "Каждая новая инструкция применяется к последней версии текста с учётом всего контекста. "
        "Отвечай только готовым финальным текстом на русском языке без комментариев."
    )

    logger.info("OpenAI refinement started | session_id=%s | type=%s", session_id, refinement_type)
    result = await asyncio.to_thread(call_openai_text, system_prompt, messages)
    logger.info("OpenAI refinement success | session_id=%s", session_id)

    # Save instruction and result to history
    save_refinement_history(session_id, "user", instruction)
    save_refinement_history(session_id, "assistant", result)

    return result

async def start_generation_flow(message: Message, category: str, user_prompt: str):
    user_id = message.from_user.id
    refresh_expired_plan_if_needed(user_id)
    user = get_user(user_id)

    if not can_spend_generation(user) or not spend_generation(user_id, 1):
        track_event(user_id, "limit_reached", category=category)
        await message.answer(get_limit_exceeded_text(), reply_markup=tariffs_inline_keyboard())
        return

    track_event(user_id, "generation_started", category=category, meta={"prompt_length": len(user_prompt)})
    save_history(user_id, category, "user", user_prompt)
    save_category_memory(user_id, category, user_prompt)

    wait_msg = await message.answer("⏳ Готовлю результат... Обычно это занимает пару секунд.")

    try:
        result_text = await run_generation(user_id, category, user_prompt)
        save_history(user_id, category, "assistant", result_text, is_final=True)
        session_id = create_generation_session(user_id, category, user_prompt, result_text)
        track_event(user_id, "generation_success", category=category, meta={"response_length": len(result_text)})

        label = (
            "Вот вариант поста:" if category == CATEGORY_POST
            else "Вот идеи постов:" if category == CATEGORY_IDEAS
            else "Вот переработанный текст:"
        )
        await wait_msg.delete()
        await message.answer(f"{label}\n\n{result_text}", reply_markup=result_inline_keyboard(session_id))
        await message.answer(
            "Вы можете настроить ответ под себя. Первые 2 настройки не расходуют генерации.",
            reply_markup=main_menu_keyboard(),
        )
    except Exception as e:
        logger.exception("Generation failed: %s", e)
        track_event(user_id, "generation_failed", category=category, meta={"error": str(e)[:500]})
        await wait_msg.delete()
        await message.answer("Кажется произошла ошибка 😅 Попробуем ещё раз?", reply_markup=main_menu_keyboard())

async def regenerate_from_session(callback: CallbackQuery, session_id: int):
    session = get_generation_session(session_id)
    if not session:
        await callback.answer("Сессия не найдена", show_alert=True)
        return

    user_id = callback.from_user.id
    refresh_expired_plan_if_needed(user_id)
    user = get_user(user_id)

    if not can_spend_generation(user) or not spend_generation(user_id, 1):
        track_event(user_id, "limit_reached", category=session["category"])
        await callback.message.answer(get_limit_exceeded_text(), reply_markup=tariffs_inline_keyboard())
        await callback.answer()
        return

    track_event(user_id, "generation_started", category=session["category"], value="regenerate")
    await callback.answer("Делаю другой вариант ✨")
    wait_msg = await callback.message.answer("⏳ Делаю новый вариант...")

    try:
        result_text = await run_generation(user_id, session["category"], session["original_prompt"])
        # Reset session: new text, reset refinement history
        update_generation_session_text(session_id, result_text, increment_refinement=False)
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM session_refinement_history WHERE session_id=%s", (session_id,))
            conn.commit()
        save_refinement_history(session_id, "assistant", result_text)
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE generation_sessions SET refinement_count=0 WHERE id=%s", (session_id,))
            conn.commit()
        save_history(user_id, session["category"], "assistant", result_text, is_final=True)
        track_event(user_id, "generation_success", category=session["category"], value="regenerate")
        await wait_msg.delete()
        await callback.message.answer(f"🔁 Другой вариант готов:\n\n{result_text}", reply_markup=result_inline_keyboard(session_id))
        await callback.message.answer(
            "Вы можете настроить ответ под себя. Первые 2 настройки не расходуют генерации.",
            reply_markup=main_menu_keyboard(),
        )
    except Exception as e:
        logger.exception("Regeneration failed: %s", e)
        await wait_msg.delete()
        await callback.message.answer("Кажется произошла ошибка 😅 Попробуем ещё раз?", reply_markup=main_menu_keyboard())

def refinement_requires_generation(user_id: int, session: dict) -> bool:
    if int(session.get("refinement_count", 0)) < FREE_REFINEMENTS_COUNT:
        return False
    user = get_user(user_id)
    if user.get("tariff") == TARIFF_UNLIM and is_subscription_active(user):
        return False
    return True

async def apply_refinement(callback: CallbackQuery, session_id: int, refinement_type: str, extra: str = ""):
    session = get_generation_session(session_id)
    if not session:
        await callback.answer("Сессия не найдена", show_alert=True)
        return

    user_id = callback.from_user.id

    need_gen = refinement_requires_generation(user_id, session)
    if need_gen:
        refresh_expired_plan_if_needed(user_id)
        user = get_user(user_id)
        if not can_spend_generation(user) or not spend_generation(user_id, 1):
            track_event(user_id, "limit_reached", category=session["category"], value="refinement")
            await callback.message.answer(
                "Бесплатные настройки уже закончились, а лимит генераций тоже на нуле 😅\n\nВыберите тариф, и продолжим 👇",
                reply_markup=tariffs_inline_keyboard(),
            )
            await callback.answer()
            return

    track_event(user_id, "generation_started", category=session["category"], value=f"refinement:{refinement_type}")
    await callback.answer("Докручиваю ✨")
    wait_msg = await callback.message.answer("⏳ Дорабатываю результат...")

    try:
        result_text = await run_refinement(session_id, refinement_type, extra)
        update_generation_session_text(session_id, result_text, increment_refinement=True)
        save_history(user_id, session["category"], "assistant", result_text, is_final=True)
        track_event(user_id, "generation_success", category=session["category"], value=f"refinement:{refinement_type}")

        await wait_msg.delete()
        await callback.message.answer(f"✨ Обновил результат:\n\n{result_text}", reply_markup=result_inline_keyboard(session_id))

        # Re-fetch session to get updated count
        updated_session = get_generation_session(session_id)
        refinement_count_after = int(updated_session.get("refinement_count", 0))
        free_left = max(FREE_REFINEMENTS_COUNT - refinement_count_after, 0)
        if free_left > 0:
            await callback.message.answer(
                f"Вы можете настроить ответ под себя. Осталось бесплатных настроек: {free_left}",
                reply_markup=main_menu_keyboard(),
            )
        else:
            await callback.message.answer("Готово! Если захотите, можем ещё подкрутить результат 😉", reply_markup=main_menu_keyboard())
    except Exception as e:
        logger.exception("Refinement failed: %s", e)
        await wait_msg.delete()
        await callback.message.answer("Кажется произошла ошибка 😅 Попробуем ещё раз?", reply_markup=main_menu_keyboard())

async def apply_refinement_from_message(message: Message, session_id: int, refinement_type: str, extra: str = ""):
    """Same as apply_refinement but triggered from a Message (two-step flow)."""
    session = get_generation_session(session_id)
    if not session:
        await message.answer("Сессия не найдена.", reply_markup=main_menu_keyboard())
        return

    user_id = message.from_user.id

    need_gen = refinement_requires_generation(user_id, session)
    if need_gen:
        refresh_expired_plan_if_needed(user_id)
        user = get_user(user_id)
        if not can_spend_generation(user) or not spend_generation(user_id, 1):
            track_event(user_id, "limit_reached", category=session["category"], value="refinement")
            await message.answer(
                "Бесплатные настройки уже закончились, а лимит генераций тоже на нуле 😅\n\nВыберите тариф, и продолжим 👇",
                reply_markup=tariffs_inline_keyboard(),
            )
            return

    track_event(user_id, "generation_started", category=session["category"], value=f"refinement:{refinement_type}")
    wait_msg = await message.answer("⏳ Дорабатываю результат...")

    try:
        result_text = await run_refinement(session_id, refinement_type, extra)
        update_generation_session_text(session_id, result_text, increment_refinement=True)
        save_history(user_id, session["category"], "assistant", result_text, is_final=True)
        track_event(user_id, "generation_success", category=session["category"], value=f"refinement:{refinement_type}")

        await wait_msg.delete()
        await message.answer(f"✨ Обновил результат:\n\n{result_text}", reply_markup=result_inline_keyboard(session_id))

        updated_session = get_generation_session(session_id)
        refinement_count_after = int(updated_session.get("refinement_count", 0))
        free_left = max(FREE_REFINEMENTS_COUNT - refinement_count_after, 0)
        if free_left > 0:
            await message.answer(
                f"Вы можете настроить ответ под себя. Осталось бесплатных настроек: {free_left}",
                reply_markup=main_menu_keyboard(),
            )
        else:
            await message.answer("Готово! Если захотите, можем ещё подкрутить результат 😉", reply_markup=main_menu_keyboard())
    except Exception as e:
        logger.exception("Refinement (from message) failed: %s", e)
        await wait_msg.delete()
        await message.answer("Кажется произошла ошибка 😅 Попробуем ещё раз?", reply_markup=main_menu_keyboard())

# =========================
# COMMANDS
# =========================

@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    user_id = message.from_user.id
    ensure_user_exists(user_id)
    refresh_expired_plan_if_needed(user_id)
    track_event(user_id, "main_menu_opened")
    await state.clear()
    text = (
        "👋 Привет! Я PosteraAI\n\n"
        "AI-ассистент для контент-менеджеров, авторов и предпринимателей, "
        "который помогает создавать сильные тексты быстрее.\n\n"
        "Подхожу для работы с:\n"
        "• Telegram\n• соцсетями\n• рассылками\n• лендингами\n• любым регулярным контентом\n\n"
        "Что я умею\n"
        "✍️ Генерировать посты по теме\n"
        "✂️ Сокращать текст без потери смысла\n"
        "💰 Усиливать продающий смысл\n"
        "🎯 Переписывать текст под нужный стиль\n"
        "🧠 Помогать структурировать идеи для контента\n\n"
        "Я подстраиваюсь под ваш стиль\n\n"
        "Вы можете:\n"
        "• настроить формат ответов\n"
        "• задать tone of voice\n"
        "• прислать примеры своих текстов\n"
        "Я проанализирую их и буду генерировать тексты в вашем стиле.\n\n"
        "👇 Выберите действие в меню ниже"
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
    await message.answer(get_my_tariff_text(user, telegram_username=message.from_user.username), reply_markup=main_menu_keyboard())

@dp.message(Command("balance"))
async def cmd_balance(message: Message):
    user_id = message.from_user.id
    refresh_expired_plan_if_needed(user_id)
    await message.answer(get_generations_left_text(get_user(user_id)), reply_markup=main_menu_keyboard())

@dp.message(Command("settings"))
async def cmd_settings_cmd(message: Message, state: FSMContext):
    user = get_user(message.from_user.id)
    await state.clear()
    await message.answer(get_settings_text(user), reply_markup=settings_keyboard(user))

@dp.message(Command("tariffs"))
async def cmd_tariffs(message: Message):
    user_id = message.from_user.id
    track_event(user_id, "payment_screen_opened", value="tariffs")
    await message.answer(
        get_tariffs_text(get_user(user_id), telegram_username=message.from_user.username),
        reply_markup=tariffs_inline_keyboard_for_user(message.from_user.username),
    )

@dp.message(Command(SPECIAL_RESET_COMMAND.lstrip("/")))
async def cmd_special_reset(message: Message):
    if (message.from_user.username or "") != SPECIAL_RESET_USERNAME:
        return
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET generations_left=%s WHERE telegram_id=%s", (FREE_GENERATIONS_DEFAULT, message.from_user.id))
        conn.commit()
    await message.answer("✅ Лимит обновлён. Можно продолжать работу 😉", reply_markup=main_menu_keyboard())

# =========================
# MAIN MENU BUTTONS
# =========================

@dp.message(F.text == "✍️ Сгенерировать пост")
async def menu_generate_post(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(GenerationStates.waiting_for_post_prompt)
    track_event(message.from_user.id, "category_selected", category=CATEGORY_POST)
    await message.answer(get_prompt_request_text(CATEGORY_POST), reply_markup=main_menu_keyboard())

@dp.message(F.text == "💡 Идеи для постов")
async def menu_post_ideas(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(GenerationStates.waiting_for_ideas_prompt)
    track_event(message.from_user.id, "category_selected", category=CATEGORY_IDEAS)
    await message.answer(get_prompt_request_text(CATEGORY_IDEAS), reply_markup=main_menu_keyboard())

@dp.message(F.text == "🔁 Переписать текст")
async def menu_rewrite_text(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(GenerationStates.waiting_for_rewrite_prompt)
    track_event(message.from_user.id, "category_selected", category=CATEGORY_REWRITE)
    await message.answer(get_prompt_request_text(CATEGORY_REWRITE), reply_markup=main_menu_keyboard())

@dp.message(F.text == "📊 Остаток генераций")
async def menu_balance(message: Message):
    user_id = message.from_user.id
    refresh_expired_plan_if_needed(user_id)
    await message.answer(get_generations_left_text(get_user(user_id)), reply_markup=main_menu_keyboard())

@dp.message(F.text == "💳 Тарифы")
async def menu_tariffs(message: Message):
    user_id = message.from_user.id
    track_event(user_id, "payment_screen_opened", value="tariffs")
    await message.answer(
        get_tariffs_text(get_user(user_id), telegram_username=message.from_user.username),
        reply_markup=tariffs_inline_keyboard_for_user(message.from_user.username),
    )

@dp.message(F.text == "⚙️ Настроить бота")
async def menu_settings(message: Message, state: FSMContext):
    user = get_user(message.from_user.id)
    await state.clear()
    await message.answer(get_settings_text(user), reply_markup=settings_keyboard(user))

@dp.message(F.text == "⬅️ Назад в меню")
async def back_to_menu_message(message: Message, state: FSMContext):
    await state.clear()
    await send_main_menu(message, "Главное меню снова перед вами 👇")

# =========================
# SETTINGS HANDLERS
# =========================

@dp.message(F.text.in_(["✅ Память включена", "⛔ Память выключена"]))
async def toggle_memory(message: Message):
    user = get_user(message.from_user.id)
    new_value = not bool(user.get("memory_enabled", True))
    set_memory_enabled(message.from_user.id, new_value)
    # History and style are preserved — only the flag changes
    text = (
        "🧠 Память включена.\nБуду учитывать прошлый контекст в этой категории."
        if new_value
        else "🧠 Память выключена.\nНовые генерации будут без учёта прошлого контекста.\nИстория и стиль сохранены — при включении вернутся."
    )
    await message.answer(text, reply_markup=settings_keyboard(get_user(message.from_user.id)))

@dp.message(F.text == "♻️ Сбросить память")
async def reset_memory_handler(message: Message):
    clear_history(message.from_user.id)
    await message.answer(
        "✅ Память сброшена.\nИстория запросов по всем категориям очищена.",
        reply_markup=settings_keyboard(get_user(message.from_user.id)),
    )

@dp.message(F.text == "✍️ Копировать стиль")
async def copy_style_start(message: Message, state: FSMContext):
    await state.set_state(SettingsStates.waiting_for_style_sample)
    await message.answer(
        "Пришли пример своего текста.\n\n"
        "Я сохраню его и буду писать в похожем стиле ✍️",
        reply_markup=settings_keyboard(get_user(message.from_user.id)),
    )

@dp.message(F.text == "🗑 Очистить стиль")
async def clear_style(message: Message):
    clear_style_samples(message.from_user.id)
    await message.answer(
        "✅ Примеры стиля очищены.\nТеперь буду опираться только на новые вводные.",
        reply_markup=settings_keyboard(get_user(message.from_user.id)),
    )

@dp.message(SettingsStates.waiting_for_style_sample)
async def receive_style_sample(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if not text:
        await message.answer("Нужен именно текстовый пример ✍️")
        return
    add_style_sample(message.from_user.id, text)
    # Stay in state, show add more / done buttons
    style_count = len(get_style_samples(message.from_user.id))
    await message.answer(
        f"✅ Сохранил пример стиля ({style_count} шт.).\n\nМожешь прислать ещё или нажать «Готово».",
        reply_markup=style_sample_keyboard(),
    )

# =========================
# STYLE SAMPLE INLINE CALLBACKS
# =========================

@dp.callback_query(F.data == "style_add_more")
async def style_add_more_callback(callback: CallbackQuery, state: FSMContext):
    await state.set_state(SettingsStates.waiting_for_style_sample)
    await callback.answer()
    await callback.message.answer("Пришли следующий пример текста ✍️")

@dp.callback_query(F.data == "style_done")
async def style_done_callback(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()
    user = get_user(callback.from_user.id)
    style_count = len(get_style_samples(callback.from_user.id))
    await callback.message.answer(
        f"✅ Готово! Сохранено примеров стиля: {style_count}.\nБуду учитывать подачу в следующих генерациях 😉",
        reply_markup=settings_keyboard(user),
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
        await message.answer("Пришлите текст, который нужно переписать 🔁")
        return
    await state.clear()
    await start_generation_flow(message, CATEGORY_REWRITE, user_prompt)

# =========================
# TWO-STEP REFINEMENT INPUTS
# =========================

@dp.message(GenerationStates.waiting_for_audience_input)
async def handle_audience_input(message: Message, state: FSMContext):
    audience = (message.text or "").strip()
    if not audience:
        await message.answer("Напиши, для какой аудитории адаптировать текст.")
        return
    data = await state.get_data()
    session_id = data.get("pending_session_id")
    await state.clear()
    if not session_id:
        await message.answer("Не удалось найти сессию. Попробуй ещё раз.", reply_markup=main_menu_keyboard())
        return
    await apply_refinement_from_message(message, session_id, "audience", extra=audience)

@dp.message(GenerationStates.waiting_for_custom_refinement)
async def handle_custom_refinement_input(message: Message, state: FSMContext):
    instruction = (message.text or "").strip()
    if not instruction:
        await message.answer("Напиши свою инструкцию для правки.")
        return
    data = await state.get_data()
    session_id = data.get("pending_session_id")
    await state.clear()
    if not session_id:
        await message.answer("Не удалось найти сессию. Попробуй ещё раз.", reply_markup=main_menu_keyboard())
        return
    await apply_refinement_from_message(message, session_id, "custom", extra=instruction)

# =========================
# INLINE CALLBACKS
# =========================

@dp.callback_query(F.data == "back_to_menu")
async def back_to_menu_callback(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()
    await send_main_menu_from_callback(callback, "Возвращаемся в меню 👇")

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
    await callback.answer()
    await callback.message.answer(
        get_refinement_menu_text(session),
        reply_markup=refinement_inline_keyboard(session_id, session.get("category", CATEGORY_POST)),
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
async def refine_type_callback(callback: CallbackQuery, state: FSMContext):
    try:
        parts = callback.data.split(":")
        session_id = int(parts[1])
        refinement_type = parts[2]
    except Exception:
        await callback.answer("Не удалось применить доработку", show_alert=True)
        return

    if refinement_type == "audience":
        await state.set_state(GenerationStates.waiting_for_audience_input)
        await state.update_data(pending_session_id=session_id)
        await callback.answer()
        await callback.message.answer(
            "🎯 Для какой аудитории адаптировать текст?\n\n"
            "Например:\n• предприниматели\n• маркетологи\n• владельцы Telegram-каналов"
        )
        return

    if refinement_type == "custom":
        await state.set_state(GenerationStates.waiting_for_custom_refinement)
        await state.update_data(pending_session_id=session_id)
        await callback.answer()
        await callback.message.answer(
            "✏️ Напишите свою инструкцию для правки.\n\n"
            "Например:\n• сделай дерзче\n• добавь больше фактов\n• сделай мягче"
        )
        return

    await apply_refinement(callback, session_id, refinement_type)

# =========================
# PAYMENT CALLBACKS
# =========================

@dp.callback_query(F.data == "buy_creator")
async def buy_creator_callback(callback: CallbackQuery):
    user_id = callback.from_user.id
    track_event(user_id, "payment_screen_opened", value=TARIFF_CREATOR)
    try:
        payment_data = await create_yookassa_payment(user_id, TARIFF_CREATOR)
        confirmation_url = (payment_data.get("confirmation") or {}).get("confirmation_url")
        if not confirmation_url:
            raise RuntimeError("confirmation_url not found")
        await callback.answer()
        await callback.message.answer(
            f"💳 Платёж для тарифа Creator готов.\n\nПерейдите по ссылке для оплаты:\n{confirmation_url}\n\n"
            "После успешной оплаты тариф активируется автоматически ✨",
            reply_markup=main_menu_keyboard(),
        )
    except Exception as e:
        logger.exception("Failed to create Creator payment: %s", e)
        await callback.answer("Не удалось создать платёж", show_alert=True)
        await callback.message.answer("Не получилось создать платёж 😅 Попробуем ещё раз чуть позже?", reply_markup=main_menu_keyboard())

@dp.callback_query(F.data == "buy_unlim")
async def buy_unlim_callback(callback: CallbackQuery):
    user_id = callback.from_user.id
    if (callback.from_user.username or "") == SPECIAL_RESET_USERNAME:
        await callback.answer("Этот тариф сейчас недоступен", show_alert=True)
        return
    track_event(user_id, "payment_screen_opened", value=TARIFF_UNLIM)
    try:
        payment_data = await create_yookassa_payment(user_id, TARIFF_UNLIM)
        confirmation_url = (payment_data.get("confirmation") or {}).get("confirmation_url")
        if not confirmation_url:
            raise RuntimeError("confirmation_url not found")
        await callback.answer()
        await callback.message.answer(
            f"💳 Платёж для тарифа Unlim готов.\n\nПерейдите по ссылке для оплаты:\n{confirmation_url}\n\n"
            "После успешной оплаты тариф активируется автоматически ✨",
            reply_markup=main_menu_keyboard(),
        )
    except Exception as e:
        logger.exception("Failed to create Unlim payment: %s", e)
        await callback.answer("Не удалось создать платёж", show_alert=True)
        await callback.message.answer("Не получилось создать платёж 😅 Попробуем ещё раз чуть позже?", reply_markup=main_menu_keyboard())

@dp.callback_query(F.data == "buy_gens_50")
async def buy_gens_50_callback(callback: CallbackQuery):
    user_id = callback.from_user.id
    track_event(user_id, "payment_screen_opened", value="gens_50")
    try:
        payment_data = await create_yookassa_payment(user_id, "gens_50")
        confirmation_url = (payment_data.get("confirmation") or {}).get("confirmation_url")
        if not confirmation_url:
            raise RuntimeError("confirmation_url not found")
        await callback.answer()
        await callback.message.answer(
            f"💳 Платёж на +50 генераций готов.\n\nПерейдите по ссылке для оплаты:\n{confirmation_url}\n\n"
            "После оплаты генерации зачислятся автоматически ✨",
            reply_markup=main_menu_keyboard(),
        )
    except Exception as e:
        logger.exception("Failed to create gens_50 payment: %s", e)
        await callback.answer("Не удалось создать платёж", show_alert=True)
        await callback.message.answer("Не получилось создать платёж 😅 Попробуем ещё раз чуть позже?", reply_markup=main_menu_keyboard())

@dp.callback_query(F.data == "buy_gens_100")
async def buy_gens_100_callback(callback: CallbackQuery):
    user_id = callback.from_user.id
    track_event(user_id, "payment_screen_opened", value="gens_100")
    try:
        payment_data = await create_yookassa_payment(user_id, "gens_100")
        confirmation_url = (payment_data.get("confirmation") or {}).get("confirmation_url")
        if not confirmation_url:
            raise RuntimeError("confirmation_url not found")
        await callback.answer()
        await callback.message.answer(
            f"💳 Платёж на +100 генераций готов.\n\nПерейдите по ссылке для оплаты:\n{confirmation_url}\n\n"
            "После оплаты генерации зачислятся автоматически ✨",
            reply_markup=main_menu_keyboard(),
        )
    except Exception as e:
        logger.exception("Failed to create gens_100 payment: %s", e)
        await callback.answer("Не удалось создать платёж", show_alert=True)
        await callback.message.answer("Не получилось создать платёж 😅 Попробуем ещё раз чуть позже?", reply_markup=main_menu_keyboard())

# =========================
# FALLBACK
# =========================

@dp.message()
async def fallback_handler(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if not text:
        await message.answer("Пока умею работать в текстовом формате 🙂", reply_markup=main_menu_keyboard())
        return
    await state.clear()
    await message.answer(
        "Я готов помочь 👌\n\nВыберите режим в меню:\n✍️ Сгенерировать пост\n💡 Идеи для постов\n🔁 Переписать текст",
        reply_markup=main_menu_keyboard(),
    )

# =========================
# YOOKASSA
# =========================

def get_tariff_price_and_amount(tariff: str) -> tuple[int, str]:
    prices = {
        TARIFF_CREATOR: (349, "349.00"),
        TARIFF_UNLIM: (799, "799.00"),
        "gens_50": (99, "99.00"),
        "gens_100": (179, "179.00"),
    }
    if tariff not in prices:
        raise ValueError(f"Unknown tariff: {tariff}")
    return prices[tariff]

def get_tariff_description(tariff: str) -> str:
    descriptions = {
        TARIFF_CREATOR: "Оплата тарифа Creator",
        TARIFF_UNLIM: "Оплата тарифа Unlim",
        "gens_50": "Покупка 50 генераций",
        "gens_100": "Покупка 100 генераций",
    }
    return descriptions.get(tariff, "Оплата тарифа")

async def create_yookassa_payment(user_id: int, tariff: str) -> dict:
    if not YOOKASSA_SHOP_ID or not YOOKASSA_SECRET_KEY:
        raise RuntimeError("YOOKASSA credentials are not set")

    amount_int, amount_str = get_tariff_price_and_amount(tariff)
    idempotence_key = str(uuid.uuid4())

    payload = {
        "amount": {"value": amount_str, "currency": "RUB"},
        "capture": True,
        "confirmation": {"type": "redirect", "return_url": PAYMENT_RETURN_URL},
        "description": get_tariff_description(tariff),
        "metadata": {"telegram_id": str(user_id), "tariff": tariff},
    }

    auth = aiohttp.BasicAuth(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY)
    headers = {"Idempotence-Key": idempotence_key, "Content-Type": "application/json"}

    logger.info("Creating YooKassa payment | user_id=%s | tariff=%s", user_id, tariff)

    async with aiohttp.ClientSession(auth=auth) as session:
        async with session.post(YOOKASSA_API_URL, json=payload, headers=headers, timeout=30) as resp:
            text = await resp.text()
            if resp.status not in (200, 201):
                logger.error("YooKassa create payment failed | status=%s | body=%s", resp.status, text)
                raise RuntimeError(f"YooKassa error: {resp.status} {text}")
            data = json.loads(text)

    payment_id = data.get("id")
    if not payment_id:
        raise RuntimeError("YooKassa payment_id missing in response")

    create_payment_record(user_id=user_id, amount=amount_int, tariff=tariff, status="pending", payment_id=payment_id)
    track_event(user_id, "payment_created", value=tariff, meta={"payment_id": payment_id, "amount": amount_int})
    logger.info("YooKassa payment created | payment_id=%s | user_id=%s", payment_id, user_id)
    return data

def activate_tariff_for_user(user_id: int, tariff: str):
    expires_at = datetime.now() + timedelta(days=30)
    if tariff == TARIFF_CREATOR:
        update_user_tariff(user_id=user_id, tariff=TARIFF_CREATOR,
                           generations_left=CREATOR_GENERATIONS_DEFAULT, plan_expires_at=expires_at)
    elif tariff == TARIFF_UNLIM:
        update_user_tariff(user_id=user_id, tariff=TARIFF_UNLIM,
                           generations_left=999999, plan_expires_at=expires_at)
    elif tariff == "gens_50":
        add_generations(user_id, 50)
    elif tariff == "gens_100":
        add_generations(user_id, 100)
    else:
        raise ValueError(f"Unknown tariff: {tariff}")

async def notify_user_payment_success(user_id: int, tariff: str):
    try:
        if tariff == "gens_50":
            text = "✅ Оплата прошла успешно!\n\n+50 генераций зачислено на ваш счёт 🚀"
        elif tariff == "gens_100":
            text = "✅ Оплата прошла успешно!\n\n+100 генераций зачислено на ваш счёт 🚀"
        else:
            text = f"✅ Оплата прошла успешно!\n\nТариф {get_tariff_title(tariff)} активирован.\nМожно продолжать работу без пауз 🚀"
        await bot.send_message(chat_id=user_id, text=text, reply_markup=main_menu_keyboard())
    except Exception as e:
        logger.exception("Failed to notify user about payment success: %s", e)

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
            if payment_record:
                tariff = tariff or payment_record.get("tariff")
                telegram_id_raw = telegram_id_raw or payment_record.get("telegram_id")

            if not tariff or not telegram_id_raw:
                logger.error("Webhook missing tariff or telegram_id | payment_id=%s", payment_id)
                return web.json_response({"ok": False, "error": "missing_metadata"}, status=400)

            user_id = int(telegram_id_raw)
            activate_tariff_for_user(user_id, tariff)
            track_event(user_id, "payment_success", value=tariff, meta={"payment_id": payment_id})
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
    await web.TCPSite(runner, host="0.0.0.0", port=PORT).start()
    logger.info("HTTP server started on 0.0.0.0:%s", PORT)
    return runner

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