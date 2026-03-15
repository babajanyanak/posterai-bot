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
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")
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

CURRENT_YEAR = 2026

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
    waiting_for_web_search_confirm = State()

class SettingsStates(StatesGroup):
    waiting_for_style_sample = State()

class ChannelContextStates(StatesGroup):
    waiting_for_niche = State()
    waiting_for_audience = State()
    waiting_for_goal = State()

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
                    user_style_profile TEXT,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_channel_context (
                    telegram_id BIGINT PRIMARY KEY,
                    niche TEXT,
                    audience TEXT,
                    goal TEXT,
                    updated_at TIMESTAMP DEFAULT NOW()
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
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS user_style_profile TEXT")
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

def save_user_style_profile(user_id: int, profile: str):
    ensure_user_exists(user_id)
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET user_style_profile=%s WHERE telegram_id=%s", (profile, user_id))
        conn.commit()

def get_user_style_profile(user_id: int) -> Optional[str]:
    user = get_user(user_id)
    return user.get("user_style_profile")

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
# CHANNEL CONTEXT
# =========================

def save_channel_context(user_id: int, niche: str, audience: str, goal: str):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO user_channel_context (telegram_id, niche, audience, goal, updated_at)
                VALUES (%s, %s, %s, %s, NOW())
                ON CONFLICT (telegram_id)
                DO UPDATE SET niche=EXCLUDED.niche, audience=EXCLUDED.audience,
                              goal=EXCLUDED.goal, updated_at=NOW()
                """,
                (user_id, niche, audience, goal),
            )
        conn.commit()

def get_channel_context(user_id: int) -> Optional[dict]:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM user_channel_context WHERE telegram_id=%s", (user_id,))
            row = cur.fetchone()
            return dict(row) if row else None

def clear_channel_context(user_id: int):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM user_channel_context WHERE telegram_id=%s", (user_id,))
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

def clear_history(user_id: int, category: Optional[str] = None):
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
    # Also clear the generated style profile
    save_user_style_profile(user_id, None)

def get_style_samples(user_id: int, limit: int = 10) -> list[str]:
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

def save_refinement_history(session_id: int, role: str, content: str):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO session_refinement_history (session_id, role, content) VALUES (%s,%s,%s)",
                (session_id, role, content),
            )
        conn.commit()

def get_refinement_history(session_id: int) -> list[dict]:
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

def create_payment_record(user_id: int, amount: int, tariff: str, status: str, payment_id: Optional[str] = None):
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
# PROMPT ENGINEERING
# =========================

# --- Base system prompt ---
BASE_SYSTEM_PROMPT = f"""You are a professional social media content writer.
You specialize in writing engaging posts for Telegram channels and other social platforms.
You always write in Russian unless explicitly asked otherwise.

Your task is to generate high-quality content that is engaging, easy to read, structured for social media and optimized for audience attention.

Rules:
1. Write concise and natural text.
2. Use short paragraphs (1–2 sentences).
3. Avoid generic AI phrases like "В современном мире", "Не секрет, что", "Давайте разберёмся".
4. Avoid outdated references unless the user explicitly asks.
5. Focus on clarity and value for the reader.
6. Adapt tone of voice if the user provided style examples.
7. Prefer modern style suitable for Telegram and social media.
8. Do not add meta-commentary — return only the final text.
9. If the user's input contains typos, grammatical errors or misspellings, silently correct them before processing. Do not mention the corrections.

Post structure: Hook → Main idea → Value → Optional call to action.

Formatting:
- Short paragraphs
- Optional emoji (not too many, only if natural)
- Readable structure

Always prioritize natural human-like writing.
Current year: {CURRENT_YEAR}"""


def build_system_messages(user_id: int) -> list[dict]:
    """
    Build the full system message list:
    1. Base system prompt
    2. User style profile (if exists and memory enabled)
    3. Channel context (if exists)
    """
    user = get_user(user_id)
    memory_enabled = bool(user.get("memory_enabled", True))
    messages = [{"role": "system", "content": BASE_SYSTEM_PROMPT}]

    # Style profile
    style_profile = get_user_style_profile(user_id) if memory_enabled else None
    if style_profile:
        messages.append({
            "role": "system",
            "content": f"User writing style profile:\n{style_profile}"
        })

    # Channel context
    ctx = get_channel_context(user_id)
    if ctx:
        ctx_text = (
            f"Channel context:\n"
            f"- Niche: {ctx.get('niche', '—')}\n"
            f"- Audience: {ctx.get('audience', '—')}\n"
            f"- Content goal: {ctx.get('goal', '—')}"
        )
        messages.append({"role": "system", "content": ctx_text})

    return messages


def build_post_prompt(user_prompt: str, memory_text: Optional[str] = None) -> str:
    parts = []
    if memory_text:
        parts.append(f"Context from previous session:\n{memory_text}\n")
    parts.append(f"""Create a social media post.

Topic:
{user_prompt}

Platform:
Telegram

Goal:
engaging post that delivers value

Requirements:
- strong hook in the first line
- practical value for the reader
- clear structure with short paragraphs
- concise and natural language
- optional emoji if appropriate""")
    return "\n".join(parts)


def build_post_outline_prompt(user_prompt: str) -> str:
    return f"""Create a brief outline for a Telegram post.

Topic:
{user_prompt}

Return only the outline — 3–5 bullet points covering:
- Hook idea
- Main point
- Supporting argument or example
- Value / takeaway
- Optional CTA

No extra commentary, just the outline."""


def build_post_from_outline_prompt(user_prompt: str, outline: str, memory_text: Optional[str] = None) -> str:
    parts = []
    if memory_text:
        parts.append(f"Context from previous session:\n{memory_text}\n")
    parts.append(f"""Write a Telegram post based on the following outline.

Topic:
{user_prompt}

Outline:
{outline}

Requirements:
- strong hook in the first line
- short paragraphs (1–2 sentences each)
- practical value for the reader
- natural, human-like language
- optional emoji if appropriate
- do not copy the outline literally — write naturally

Return only the final post text.""")
    return "\n".join(parts)


def build_ideas_prompt(user_prompt: str, memory_text: Optional[str] = None) -> str:
    parts = []
    if memory_text:
        parts.append(f"Context from previous session:\n{memory_text}\n")
    parts.append(f"""Generate 10 content ideas for a Telegram channel.

Topic / niche:
{user_prompt}

Platform:
Telegram

Each idea should:
- be specific and concrete
- have an attention-grabbing angle
- be suitable for social media format
- avoid generic or obvious topics

Format: numbered list, one idea per line, short title + one sentence description.""")
    return "\n".join(parts)


def build_rewrite_prompt(user_prompt: str) -> str:
    return f"""Rewrite the following text.

Goals:
- improve clarity and readability
- improve engagement
- adapt for social media (Telegram format)
- keep the original meaning intact
- remove bureaucratic language and clichés

Style:
Short paragraphs, natural language, suitable for Telegram.

Text:
{user_prompt}

Return only the rewritten text."""


def build_style_analysis_prompt(samples: list[str]) -> str:
    joined = "\n\n---\n\n".join(samples)
    return f"""Analyze the writing style of the following texts written by the same author.

Extract and describe:
- Tone of voice (e.g. friendly, expert, casual, formal)
- Formality level (formal / semi-casual / casual)
- Sentence length (short / medium / long)
- Emoji usage (none / rare / moderate / frequent)
- Typical post structure (e.g. hook → story → takeaway)
- Writing patterns and habits (e.g. uses lists, rhetorical questions, personal stories)
- Vocabulary style (simple / professional / slang)

Return a concise style profile in plain text. No bullet point headers needed — write naturally in 5–8 sentences.

Texts:
{joined}"""


def get_refinement_instruction(refinement_type: str, extra: str = "") -> str:
    mapping = {
        "shorter": (
            "Shorten the text significantly. Remove everything secondary — keep only the core message. "
            "The result must be noticeably shorter, not just slightly trimmed."
        ),
        "selling": "Make the text more persuasive: strengthen the offer, highlight the value, increase the reader's motivation to act.",
        "lively": "Make the text more lively: lighter, more natural, more emotional. Remove dryness and bureaucratic language.",
        "cta": "Add a clear call to action at the end. Choose the most fitting one: subscribe, write, try, visit — based on context.",
        "telegram": "Adapt the text for Telegram format: short paragraphs, conversational tone, easy to read on mobile.",
        "web": "Adapt the text for web format: structured paragraphs, neutral professional tone suitable for a website or news article.",
        "audience": f"Adapt the text for the following audience: {extra}. Consider their language, pain points and interests.",
        "deeper": "Make the ideas less obvious and more insightful. Add expert depth and non-standard angles.",
        "bolder": "Make the ideas more provocative, vivid and attention-grabbing. Don't be afraid of bold statements.",
        "cleaner": "Clean up the text: remove bureaucratic language, simplify wording, make it easier to read.",
        "structure": "Improve the structure: add logical blocks, improve the flow, make the text easier to scan.",
        "custom": extra,
    }
    return mapping.get(refinement_type, "Improve the text.")

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
            [KeyboardButton(text="📡 Настроить канал")],
            [KeyboardButton(text=memory_button)],
            [KeyboardButton(text="♻️ Сбросить память")],
            [KeyboardButton(text="✍️ Копировать стиль")],
            [KeyboardButton(text="🗑 Очистить стиль")],
            [KeyboardButton(text="⬅️ Назад в меню")],
        ],
        resize_keyboard=True,
    )

def channel_context_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Изменить", callback_data="channel_context_edit")],
            [InlineKeyboardButton(text="🗑 Удалить", callback_data="channel_context_clear")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="channel_context_back")],
        ]
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
        "• Первые 2 настройки не расходуют генерации", "",
        "Creator — 349 ₽/мес",
        f"• {CREATOR_GENERATIONS_DEFAULT} генераций в месяц",
        "• Подходит для регулярной работы", "",
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
    has_profile = bool(get_user_style_profile(user["telegram_id"]))
    ctx = get_channel_context(user["telegram_id"])
    ctx_text = f"{ctx['niche']} · {ctx['audience']} · {ctx['goal']}" if ctx else "не настроен"
    profile_text = "есть" if has_profile else "нет"
    return (
        "⚙️ Настройка бота\n\n"
        f"📡 Контекст канала: {ctx_text}\n"
        f"🧠 Память: {memory_enabled}\n"
        f"✍️ Примеров стиля: {style_count} · Профиль стиля: {profile_text}\n\n"
        "Здесь можно настроить канал, управлять памятью и стилем."
    )

def get_channel_context_text(ctx: Optional[dict]) -> str:
    if not ctx:
        return (
            "📡 Контекст канала не настроен.\n\n"
            "Укажи нишу, аудиторию и цель — и я буду учитывать это в каждой генерации."
        )
    return (
        f"📡 Контекст канала\n\n"
        f"Ниша: {ctx.get('niche', '—')}\n"
        f"Аудитория: {ctx.get('audience', '—')}\n"
        f"Цель контента: {ctx.get('goal', '—')}"
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
# TAVILY WEB SEARCH
# =========================

TAVILY_SEARCH_URL = "https://api.tavily.com/search"

async def tavily_search(query: str, max_results: int = 5) -> Optional[str]:
    """Search the web via Tavily and return a formatted context string."""
    if not TAVILY_API_KEY:
        logger.warning("TAVILY_API_KEY is not set — skipping web search")
        return None
    payload = {
        "api_key": TAVILY_API_KEY,
        "query": query,
        "search_depth": "basic",
        "max_results": max_results,
        "include_answer": True,
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(TAVILY_SEARCH_URL, json=payload, timeout=15) as resp:
                if resp.status != 200:
                    logger.error("Tavily error | status=%s", resp.status)
                    return None
                data = await resp.json()

        parts = []

        # Top-level answer if available
        answer = data.get("answer")
        if answer:
            parts.append(f"Summary: {answer}")

        # Individual results
        for r in data.get("results", [])[:max_results]:
            title = r.get("title", "")
            content = r.get("content", "")
            url = r.get("url", "")
            if content:
                snippet = content[:300].strip()
                parts.append(f"- {title}: {snippet} ({url})")

        if not parts:
            return None

        return "\n".join(parts)

    except Exception as e:
        logger.exception("Tavily search failed: %s", e)
        return None


def web_search_confirm_keyboard(category: str, prompt_key: str) -> InlineKeyboardMarkup:
    """Inline keyboard asking whether to use web search."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="да", callback_data=f"ws_yes:{category}:{prompt_key}"),
                InlineKeyboardButton(text="нет", callback_data=f"ws_no:{category}:{prompt_key}"),
            ]
        ]
    )

# =========================
# COMMON SENDERS
# =========================

async def send_main_menu(message: Message, text: str):
    await message.answer(text, reply_markup=main_menu_keyboard())

async def send_main_menu_from_callback(callback: CallbackQuery, text: str):
    await callback.message.answer(text, reply_markup=main_menu_keyboard())

# =========================
# OPENAI CALLS
# =========================

def call_openai(system_messages: list[dict], user_messages: list[dict],
                max_tokens: int = 600, temperature: float = 0.7) -> str:
    messages = system_messages + user_messages
    response = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
    )
    if not response.choices:
        raise RuntimeError("OpenAI не вернул choices")
    msg = response.choices[0].message
    if not msg or not msg.content:
        raise RuntimeError("OpenAI не вернул текстовый ответ")
    return msg.content.strip()


async def analyze_style_and_save(user_id: int, samples: list[str]):
    """Analyze style samples and save the resulting profile."""
    prompt = build_style_analysis_prompt(samples)
    system = [{"role": "system", "content": "You are an expert writing style analyst. Be concise and specific."}]
    user_msgs = [{"role": "user", "content": prompt}]
    profile = await asyncio.to_thread(call_openai, system, user_msgs, 300, 0.3)
    save_user_style_profile(user_id, profile)
    logger.info("Style profile saved for user_id=%s", user_id)
    return profile


async def run_generation(user_id: int, category: str, user_prompt: str,
                         web_context: Optional[str] = None) -> str:
    system_messages = build_system_messages(user_id)

    # Inject web search context as an extra system message if available
    if web_context:
        system_messages.append({
            "role": "system",
            "content": (
                "Web search results for additional context "
                "(use as factual background only, do not copy verbatim, do not mention sources explicitly):\n"
                + web_context
            )
        })

    user = get_user(user_id)
    memory_enabled = bool(user.get("memory_enabled", True))
    memory_text = get_category_memory(user_id, category) if memory_enabled else None

    if category == CATEGORY_POST:
        # Two-step: outline → post
        outline_prompt = build_post_outline_prompt(user_prompt)
        logger.info("OpenAI outline started | user_id=%s", user_id)
        outline = await asyncio.to_thread(
            call_openai, system_messages,
            [{"role": "user", "content": outline_prompt}],
            200, 0.5,
        )
        logger.info("OpenAI outline done | user_id=%s", user_id)

        post_prompt = build_post_from_outline_prompt(user_prompt, outline, memory_text)
        result = await asyncio.to_thread(
            call_openai, system_messages,
            [{"role": "user", "content": post_prompt}],
            600, 0.7,
        )

    elif category == CATEGORY_IDEAS:
        prompt = build_ideas_prompt(user_prompt, memory_text)
        result = await asyncio.to_thread(
            call_openai, system_messages,
            [{"role": "user", "content": prompt}],
            800, 0.9,
        )

    elif category == CATEGORY_REWRITE:
        prompt = build_rewrite_prompt(user_prompt)
        result = await asyncio.to_thread(
            call_openai, system_messages,
            [{"role": "user", "content": prompt}],
            600, 0.7,
        )

    else:
        result = await asyncio.to_thread(
            call_openai, system_messages,
            [{"role": "user", "content": user_prompt}],
            600, 0.7,
        )

    logger.info("OpenAI generation success | user_id=%s | category=%s", user_id, category)
    return result


async def run_refinement(session_id: int, refinement_type: str, extra: str = "") -> str:
    """Multi-turn refinement using full session history."""
    history = get_refinement_history(session_id)
    instruction = get_refinement_instruction(refinement_type, extra)

    messages = [{"role": h["role"], "content": h["content"]} for h in history]
    messages.append({"role": "user", "content": instruction})

    system = [{
        "role": "system",
        "content": (
            "You are a professional Russian-language editor. "
            "You have the full history of this text and all previous edits. "
            "Apply the new instruction to the latest version of the text, taking all context into account. "
            "Return only the final text in Russian. No commentary."
        )
    }]

    logger.info("OpenAI refinement started | session_id=%s | type=%s", session_id, refinement_type)
    result = await asyncio.to_thread(call_openai, system, messages, 600, 0.7)
    logger.info("OpenAI refinement success | session_id=%s", session_id)

    save_refinement_history(session_id, "user", instruction)
    save_refinement_history(session_id, "assistant", result)
    return result

# =========================
# GENERATION FLOW
# =========================

async def start_generation_flow(message: Message, category: str, user_prompt: str,
                                web_context: Optional[str] = None):
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
        result_text = await run_generation(user_id, category, user_prompt, web_context=web_context)
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
        update_generation_session_text(session_id, result_text, increment_refinement=False)
        # Reset refinement history for new variant
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM session_refinement_history WHERE session_id=%s", (session_id,))
                cur.execute("UPDATE generation_sessions SET refinement_count=0 WHERE id=%s", (session_id,))
            conn.commit()
        save_refinement_history(session_id, "assistant", result_text)
        save_history(user_id, session["category"], "assistant", result_text, is_final=True)

        track_event(user_id, "generation_success", category=session["category"], value="regenerate")
        await wait_msg.delete()
        await callback.message.answer(
            f"🔁 Другой вариант готов:\n\n{result_text}",
            reply_markup=result_inline_keyboard(session_id),
        )
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

        updated = get_generation_session(session_id)
        free_left = max(FREE_REFINEMENTS_COUNT - int(updated.get("refinement_count", 0)), 0)
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

        updated = get_generation_session(session_id)
        free_left = max(FREE_REFINEMENTS_COUNT - int(updated.get("refinement_count", 0)), 0)
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
        "• настроить контекст вашего канала\n"
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
            cur.execute("UPDATE users SET generations_left=%s WHERE telegram_id=%s",
                        (FREE_GENERATIONS_DEFAULT, message.from_user.id))
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
    text = (
        "🧠 Память включена.\nБуду учитывать прошлый контекст и стиль."
        if new_value
        else "🧠 Память выключена.\nНовые генерации без учёта прошлого контекста.\nИстория и стиль сохранены — при включении вернутся."
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
        "Пришли пример своего текста.\n\nЯ сохраню его и проанализирую твой стиль ✍️",
        reply_markup=settings_keyboard(get_user(message.from_user.id)),
    )

@dp.message(F.text == "🗑 Очистить стиль")
async def clear_style(message: Message):
    clear_style_samples(message.from_user.id)
    await message.answer(
        "✅ Примеры стиля и профиль очищены.\nТеперь буду опираться только на новые вводные.",
        reply_markup=settings_keyboard(get_user(message.from_user.id)),
    )

@dp.message(SettingsStates.waiting_for_style_sample)
async def receive_style_sample(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if not text:
        await message.answer("Нужен именно текстовый пример ✍️")
        return
    add_style_sample(message.from_user.id, text)
    style_count = len(get_style_samples(message.from_user.id))
    await message.answer(
        f"✅ Сохранил пример ({style_count} шт.).\n\nМожешь прислать ещё или нажать «Готово» — я проанализирую стиль.",
        reply_markup=style_sample_keyboard(),
    )

# =========================
# CHANNEL CONTEXT HANDLERS
# =========================

@dp.message(F.text == "📡 Настроить канал")
async def menu_channel_context(message: Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    ctx = get_channel_context(user_id)
    await message.answer(
        get_channel_context_text(ctx),
        reply_markup=channel_context_keyboard(),
    )

@dp.callback_query(F.data == "channel_context_edit")
async def channel_context_edit(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ChannelContextStates.waiting_for_niche)
    await callback.answer()
    await callback.message.answer(
        "📡 Настроим контекст канала.\n\n"
        "Шаг 1 из 3\n"
        "Какая ниша вашего канала?\n\n"
        "Например: маркетинг, недвижимость, IT, финансы, фитнес"
    )

@dp.callback_query(F.data == "channel_context_clear")
async def channel_context_clear_callback(callback: CallbackQuery):
    clear_channel_context(callback.from_user.id)
    await callback.answer()
    await callback.message.answer(
        "✅ Контекст канала удалён.",
        reply_markup=settings_keyboard(get_user(callback.from_user.id)),
    )

@dp.callback_query(F.data == "channel_context_back")
async def channel_context_back(callback: CallbackQuery):
    await callback.answer()
    user = get_user(callback.from_user.id)
    await callback.message.answer(get_settings_text(user), reply_markup=settings_keyboard(user))

@dp.message(ChannelContextStates.waiting_for_niche)
async def handle_niche_input(message: Message, state: FSMContext):
    niche = (message.text or "").strip()
    if not niche:
        await message.answer("Напиши нишу канала текстом.")
        return
    await state.update_data(niche=niche)
    await state.set_state(ChannelContextStates.waiting_for_audience)
    await message.answer(
        "Шаг 2 из 3\n"
        "Кто ваша аудитория?\n\n"
        "Например: предприниматели, маркетологи, молодые мамы, IT-специалисты"
    )

@dp.message(ChannelContextStates.waiting_for_audience)
async def handle_audience_context_input(message: Message, state: FSMContext):
    audience = (message.text or "").strip()
    if not audience:
        await message.answer("Напиши аудиторию канала текстом.")
        return
    await state.update_data(audience=audience)
    await state.set_state(ChannelContextStates.waiting_for_goal)
    await message.answer(
        "Шаг 3 из 3\n"
        "Какая цель вашего контента?\n\n"
        "Например: рост экспертизы, продажи, вовлечённость, обучение аудитории"
    )

@dp.message(ChannelContextStates.waiting_for_goal)
async def handle_goal_input(message: Message, state: FSMContext):
    goal = (message.text or "").strip()
    if not goal:
        await message.answer("Напиши цель контента текстом.")
        return
    data = await state.get_data()
    await state.clear()

    user_id = message.from_user.id
    save_channel_context(user_id, data["niche"], data["audience"], goal)
    track_event(user_id, "channel_context_saved")

    ctx = get_channel_context(user_id)
    await message.answer(
        f"✅ Контекст канала сохранён!\n\n"
        f"Ниша: {ctx['niche']}\n"
        f"Аудитория: {ctx['audience']}\n"
        f"Цель: {ctx['goal']}\n\n"
        "Теперь буду учитывать это в каждой генерации 🎯",
        reply_markup=settings_keyboard(get_user(user_id)),
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
    user_id = callback.from_user.id
    samples = get_style_samples(user_id)

    if not samples:
        await callback.message.answer(
            "Нет сохранённых примеров стиля.",
            reply_markup=settings_keyboard(get_user(user_id)),
        )
        return

    wait_msg = await callback.message.answer("⏳ Анализирую твой стиль...")
    try:
        profile = await analyze_style_and_save(user_id, samples)
        await wait_msg.delete()
        await callback.message.answer(
            f"✅ Профиль стиля сохранён!\n\n{profile}\n\nБуду учитывать это в следующих генерациях 😉",
            reply_markup=settings_keyboard(get_user(user_id)),
        )
    except Exception as e:
        logger.exception("Style analysis failed: %s", e)
        await wait_msg.delete()
        await callback.message.answer(
            "Не удалось проанализировать стиль 😅 Попробуй позже.",
            reply_markup=settings_keyboard(get_user(user_id)),
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
    await state.update_data(pending_category=CATEGORY_POST, pending_prompt=user_prompt)
    await state.set_state(GenerationStates.waiting_for_web_search_confirm)
    await message.answer(
        "Учитывать ли информацию из интернета?",
        reply_markup=web_search_confirm_keyboard(CATEGORY_POST, "pending"),
    )

@dp.message(GenerationStates.waiting_for_ideas_prompt)
async def handle_ideas_prompt(message: Message, state: FSMContext):
    user_prompt = (message.text or "").strip()
    if not user_prompt:
        await message.answer("Пришлите тему, нишу или продукт текстом 💡")
        return
    await state.update_data(pending_category=CATEGORY_IDEAS, pending_prompt=user_prompt)
    await state.set_state(GenerationStates.waiting_for_web_search_confirm)
    await message.answer(
        "Учитывать ли информацию из интернета?",
        reply_markup=web_search_confirm_keyboard(CATEGORY_IDEAS, "pending"),
    )

@dp.message(GenerationStates.waiting_for_rewrite_prompt)
async def handle_rewrite_prompt(message: Message, state: FSMContext):
    user_prompt = (message.text or "").strip()
    if not user_prompt:
        await message.answer("Пришлите текст, который нужно переписать 🔁")
        return
    await state.clear()
    await start_generation_flow(message, CATEGORY_REWRITE, user_prompt)

# =========================
# WEB SEARCH CONFIRM CALLBACKS
# =========================

@dp.callback_query(F.data.startswith("ws_yes:"))
async def ws_yes_callback(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    category = data.get("pending_category")
    user_prompt = data.get("pending_prompt")
    await state.clear()

    if not category or not user_prompt:
        await callback.message.answer("Что-то пошло не так. Попробуй ещё раз.", reply_markup=main_menu_keyboard())
        return

    search_msg = await callback.message.answer("🔍 Ищу актуальную информацию...")
    web_context = await tavily_search(user_prompt)
    await search_msg.delete()

    if not web_context:
        await callback.message.answer("Не удалось найти информацию. Генерирую без неё.")

    await start_generation_flow(callback.message, category, user_prompt, web_context=web_context)


@dp.callback_query(F.data.startswith("ws_no:"))
async def ws_no_callback(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    category = data.get("pending_category")
    user_prompt = data.get("pending_prompt")
    await state.clear()

    if not category or not user_prompt:
        await callback.message.answer("Что-то пошло не так. Попробуй ещё раз.", reply_markup=main_menu_keyboard())
        return

    await start_generation_flow(callback.message, category, user_prompt)


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
            f"💳 Платёж для тарифа Creator готов.\n\nПерейдите по ссылке:\n{confirmation_url}\n\n"
            "После оплаты тариф активируется автоматически ✨",
            reply_markup=main_menu_keyboard(),
        )
    except Exception as e:
        logger.exception("Failed to create Creator payment: %s", e)
        await callback.answer("Не удалось создать платёж", show_alert=True)
        await callback.message.answer("Не получилось создать платёж 😅 Попробуем чуть позже?", reply_markup=main_menu_keyboard())

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
            f"💳 Платёж для тарифа Unlim готов.\n\nПерейдите по ссылке:\n{confirmation_url}\n\n"
            "После оплаты тариф активируется автоматически ✨",
            reply_markup=main_menu_keyboard(),
        )
    except Exception as e:
        logger.exception("Failed to create Unlim payment: %s", e)
        await callback.answer("Не удалось создать платёж", show_alert=True)
        await callback.message.answer("Не получилось создать платёж 😅 Попробуем чуть позже?", reply_markup=main_menu_keyboard())

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
            f"💳 Платёж на +50 генераций готов.\n\nПерейдите по ссылке:\n{confirmation_url}\n\n"
            "После оплаты генерации зачислятся автоматически ✨",
            reply_markup=main_menu_keyboard(),
        )
    except Exception as e:
        logger.exception("Failed to create gens_50 payment: %s", e)
        await callback.answer("Не удалось создать платёж", show_alert=True)
        await callback.message.answer("Не получилось создать платёж 😅 Попробуем чуть позже?", reply_markup=main_menu_keyboard())

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
            f"💳 Платёж на +100 генераций готов.\n\nПерейдите по ссылке:\n{confirmation_url}\n\n"
            "После оплаты генерации зачислятся автоматически ✨",
            reply_markup=main_menu_keyboard(),
        )
    except Exception as e:
        logger.exception("Failed to create gens_100 payment: %s", e)
        await callback.answer("Не удалось создать платёж", show_alert=True)
        await callback.message.answer("Не получилось создать платёж 😅 Попробуем чуть позже?", reply_markup=main_menu_keyboard())

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
    return {
        TARIFF_CREATOR: "Оплата тарифа Creator",
        TARIFF_UNLIM: "Оплата тарифа Unlim",
        "gens_50": "Покупка 50 генераций",
        "gens_100": "Покупка 100 генераций",
    }.get(tariff, "Оплата тарифа")

async def create_yookassa_payment(user_id: int, tariff: str) -> dict:
    if not YOOKASSA_SHOP_ID or not YOOKASSA_SECRET_KEY:
        raise RuntimeError("YOOKASSA credentials are not set")
    amount_int, amount_str = get_tariff_price_and_amount(tariff)
    payload = {
        "amount": {"value": amount_str, "currency": "RUB"},
        "capture": True,
        "confirmation": {"type": "redirect", "return_url": PAYMENT_RETURN_URL},
        "description": get_tariff_description(tariff),
        "metadata": {"telegram_id": str(user_id), "tariff": tariff},
    }
    auth = aiohttp.BasicAuth(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY)
    headers = {"Idempotence-Key": str(uuid.uuid4()), "Content-Type": "application/json"}
    logger.info("Creating YooKassa payment | user_id=%s | tariff=%s", user_id, tariff)
    async with aiohttp.ClientSession(auth=auth) as session:
        async with session.post(YOOKASSA_API_URL, json=payload, headers=headers, timeout=30) as resp:
            text = await resp.text()
            if resp.status not in (200, 201):
                logger.error("YooKassa failed | status=%s | body=%s", resp.status, text)
                raise RuntimeError(f"YooKassa error: {resp.status} {text}")
            data = json.loads(text)
    payment_id = data.get("id")
    if not payment_id:
        raise RuntimeError("YooKassa payment_id missing")
    create_payment_record(user_id=user_id, amount=amount_int, tariff=tariff, status="pending", payment_id=payment_id)
    track_event(user_id, "payment_created", value=tariff, meta={"payment_id": payment_id, "amount": amount_int})
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
            text = "✅ Оплата прошла!\n\n+50 генераций зачислено 🚀"
        elif tariff == "gens_100":
            text = "✅ Оплата прошла!\n\n+100 генераций зачислено 🚀"
        else:
            text = f"✅ Оплата прошла!\n\nТариф {get_tariff_title(tariff)} активирован.\nМожно работать без пауз 🚀"
        await bot.send_message(chat_id=user_id, text=text, reply_markup=main_menu_keyboard())
    except Exception as e:
        logger.exception("Failed to notify user: %s", e)

# =========================
# WEBHOOK / HTTP SERVER
# =========================

async def health_handler(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})

async def yookassa_webhook_handler(request: web.Request) -> web.Response:
    try:
        payload = await request.json()
        logger.info("YooKassa webhook: %s", payload)
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
            record = get_payment_by_payment_id(payment_id)
            if record:
                tariff = tariff or record.get("tariff")
                telegram_id_raw = telegram_id_raw or record.get("telegram_id")
            if not tariff or not telegram_id_raw:
                logger.error("Webhook missing tariff/telegram_id | payment_id=%s", payment_id)
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