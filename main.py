"""
PosteraAI — Telegram Bot
Stack: aiogram 3.x | PostgreSQL (psycopg3) | OpenAI API | YooKassa | Railway
"""

# ===========================================================================
# IMPORTS
# ===========================================================================
import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiohttp
import psycopg
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    PreCheckoutQuery,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from openai import AsyncOpenAI
from yookassa import Configuration, Payment

# ===========================================================================
# LOGGING
# ===========================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("postera_ai")

# ===========================================================================
# ENVIRONMENT VARIABLES
# ===========================================================================
TELEGRAM_BOT_TOKEN: str = os.environ["TELEGRAM_BOT_TOKEN"]
OPENAI_API_KEY: str = os.environ["OPENAI_API_KEY"]
DATABASE_URL: str = os.environ["DATABASE_URL"]
YOOKASSA_SHOP_ID: str = os.environ["YOOKASSA_SHOP_ID"]
YOOKASSA_SECRET_KEY: str = os.environ["YOOKASSA_SECRET_KEY"]
PAYMENT_RETURN_URL: str = os.environ.get("PAYMENT_RETURN_URL", "https://t.me/PosteraAI_bot")
PORT: int = int(os.environ.get("PORT", "8080"))

# YooKassa configuration
Configuration.account_id = YOOKASSA_SHOP_ID
Configuration.secret_key = YOOKASSA_SECRET_KEY

# OpenAI client
openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)

# Tariff definitions
TARIFFS = {
    "creator": {
        "name": "Creator 🚀",
        "price": 490,
        "generations": 100,
        "days": 30,
        "description": "100 генераций в месяц",
    },
    "unlim": {
        "name": "Unlim ∞",
        "price": 990,
        "generations": 999999,
        "days": 30,
        "description": "Безлимитные генерации",
    },
}

FREE_GENERATIONS = 10
FREE_REFINEMENTS = 2  # first two refinements are free per session

# ===========================================================================
# DATABASE
# ===========================================================================

async def get_conn() -> psycopg.AsyncConnection:
    """Return a new async database connection."""
    return await psycopg.AsyncConnection.connect(DATABASE_URL)


async def init_db() -> None:
    """Create all tables and run migrations if they don't exist."""
    logger.info("Initialising database …")
    async with await get_conn() as conn:
        async with conn.cursor() as cur:
            # users
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id   BIGINT PRIMARY KEY,
                    tariff        TEXT      DEFAULT 'free',
                    generations_left INT    DEFAULT 10,
                    plan_expires_at  TIMESTAMP,
                    memory_enabled   BOOLEAN DEFAULT TRUE,
                    created_at    TIMESTAMP DEFAULT NOW()
                )
            """)
            # migrations
            await cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS memory_enabled BOOLEAN DEFAULT TRUE")
            await cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS plan_expires_at TIMESTAMP")

            # user_history
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS user_history (
                    id          SERIAL PRIMARY KEY,
                    telegram_id BIGINT,
                    category    TEXT,
                    role        TEXT,
                    text        TEXT,
                    is_final    BOOLEAN DEFAULT FALSE,
                    created_at  TIMESTAMP DEFAULT NOW()
                )
            """)

            # analytics_events
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS analytics_events (
                    id          SERIAL PRIMARY KEY,
                    telegram_id BIGINT,
                    event_name  TEXT,
                    category    TEXT,
                    value       TEXT,
                    meta        JSONB,
                    created_at  TIMESTAMP DEFAULT NOW()
                )
            """)

            # payments
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS payments (
                    id          SERIAL PRIMARY KEY,
                    telegram_id BIGINT,
                    amount      INT,
                    tariff      TEXT,
                    status      TEXT,
                    payment_id  TEXT,
                    created_at  TIMESTAMP DEFAULT NOW()
                )
            """)

            # generation_sessions
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS generation_sessions (
                    id               SERIAL PRIMARY KEY,
                    telegram_id      BIGINT,
                    category         TEXT,
                    original_prompt  TEXT,
                    generated_text   TEXT,
                    refinement_count INT DEFAULT 0,
                    created_at       TIMESTAMP DEFAULT NOW()
                )
            """)

            # style_posts
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS style_posts (
                    id          SERIAL PRIMARY KEY,
                    telegram_id BIGINT,
                    text        TEXT,
                    created_at  TIMESTAMP DEFAULT NOW()
                )
            """)

            # user_style_samples
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS user_style_samples (
                    id          SERIAL PRIMARY KEY,
                    telegram_id BIGINT,
                    text        TEXT,
                    created_at  TIMESTAMP DEFAULT NOW()
                )
            """)

            # user_category_memory
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS user_category_memory (
                    telegram_id BIGINT,
                    category    TEXT,
                    last_prompt TEXT,
                    PRIMARY KEY (telegram_id, category)
                )
            """)

        await conn.commit()
    logger.info("Database ready ✅")


# ===========================================================================
# ANALYTICS
# ===========================================================================

async def track_event(
    user_id: int,
    event_name: str,
    category: Optional[str] = None,
    value: Optional[str] = None,
    meta: Optional[dict] = None,
) -> None:
    try:
        async with await get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO analytics_events (telegram_id, event_name, category, value, meta)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (user_id, event_name, category, value, json.dumps(meta) if meta else None),
                )
            await conn.commit()
    except Exception as exc:
        logger.error("track_event error: %s", exc)


# ===========================================================================
# USER HELPERS
# ===========================================================================

async def get_or_create_user(telegram_id: int) -> dict:
    async with await get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT * FROM users WHERE telegram_id = %s", (telegram_id,))
            row = await cur.fetchone()
            if row is None:
                await cur.execute(
                    """
                    INSERT INTO users (telegram_id, tariff, generations_left, memory_enabled)
                    VALUES (%s, 'free', %s, TRUE)
                    RETURNING *
                    """,
                    (telegram_id, FREE_GENERATIONS),
                )
                row = await cur.fetchone()
                await conn.commit()
            cols = [desc[0] for desc in cur.description]
            return dict(zip(cols, row))


async def get_user(telegram_id: int) -> Optional[dict]:
    try:
        async with await get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT * FROM users WHERE telegram_id = %s", (telegram_id,))
                row = await cur.fetchone()
                if row is None:
                    return None
                cols = [desc[0] for desc in cur.description]
                return dict(zip(cols, row))
    except Exception as exc:
        logger.error("get_user error: %s", exc)
        return None


async def decrement_generation(telegram_id: int) -> None:
    async with await get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE users SET generations_left = generations_left - 1 WHERE telegram_id = %s AND generations_left > 0",
                (telegram_id,),
            )
        await conn.commit()


async def has_generations(user: dict) -> bool:
    if user["tariff"] == "unlim":
        return True
    expires = user.get("plan_expires_at")
    if expires and expires < datetime.now(timezone.utc).replace(tzinfo=None):
        return user["generations_left"] > 0
    return user["generations_left"] > 0


async def save_history(telegram_id: int, category: str, role: str, text: str, is_final: bool = False) -> None:
    try:
        async with await get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO user_history (telegram_id, category, role, text, is_final) VALUES (%s,%s,%s,%s,%s)",
                    (telegram_id, category, role, text, is_final),
                )
            await conn.commit()
    except Exception as exc:
        logger.error("save_history error: %s", exc)


async def get_style_samples(telegram_id: int) -> list[str]:
    try:
        async with await get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT text FROM user_style_samples WHERE telegram_id = %s ORDER BY created_at DESC LIMIT 5",
                    (telegram_id,),
                )
                rows = await cur.fetchall()
                return [r[0] for r in rows]
    except Exception as exc:
        logger.error("get_style_samples error: %s", exc)
        return []


async def save_style_sample(telegram_id: int, text: str) -> None:
    async with await get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO user_style_samples (telegram_id, text) VALUES (%s, %s)",
                (telegram_id, text),
            )
        await conn.commit()


async def clear_style_samples(telegram_id: int) -> None:
    async with await get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM user_style_samples WHERE telegram_id = %s", (telegram_id,))
        await conn.commit()


async def get_category_memory(telegram_id: int, category: str) -> Optional[str]:
    try:
        async with await get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT last_prompt FROM user_category_memory WHERE telegram_id = %s AND category = %s",
                    (telegram_id, category),
                )
                row = await cur.fetchone()
                return row[0] if row else None
    except Exception as exc:
        logger.error("get_category_memory error: %s", exc)
        return None


async def set_category_memory(telegram_id: int, category: str, prompt: str) -> None:
    try:
        async with await get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO user_category_memory (telegram_id, category, last_prompt)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (telegram_id, category) DO UPDATE SET last_prompt = EXCLUDED.last_prompt
                    """,
                    (telegram_id, category, prompt),
                )
            await conn.commit()
    except Exception as exc:
        logger.error("set_category_memory error: %s", exc)


async def create_session(telegram_id: int, category: str, prompt: str, result: str) -> int:
    async with await get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO generation_sessions (telegram_id, category, original_prompt, generated_text)
                VALUES (%s, %s, %s, %s) RETURNING id
                """,
                (telegram_id, category, prompt, result),
            )
            row = await cur.fetchone()
            await conn.commit()
            return row[0]


async def get_session(session_id: int) -> Optional[dict]:
    try:
        async with await get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT * FROM generation_sessions WHERE id = %s", (session_id,))
                row = await cur.fetchone()
                if row is None:
                    return None
                cols = [desc[0] for desc in cur.description]
                return dict(zip(cols, row))
    except Exception as exc:
        logger.error("get_session error: %s", exc)
        return None


async def update_session_text(session_id: int, new_text: str) -> None:
    async with await get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE generation_sessions SET generated_text = %s, refinement_count = refinement_count + 1 WHERE id = %s",
                (new_text, session_id),
            )
        await conn.commit()


# ===========================================================================
# KEYBOARDS
# ===========================================================================

def main_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="✍️ Сгенерировать пост"), KeyboardButton(text="💡 Идеи постов")],
            [KeyboardButton(text="♻️ Переписать текст")],
            [KeyboardButton(text="👤 Мой тариф"), KeyboardButton(text="📊 Остаток генераций")],
            [KeyboardButton(text="💳 Тарифы"), KeyboardButton(text="🕘 Настройка бота")],
        ],
        resize_keyboard=True,
    )


def post_actions_keyboard(session_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✏️ Доработать", callback_data=f"refine:{session_id}"),
                InlineKeyboardButton(text="🔁 Перегенерировать", callback_data=f"regen:{session_id}"),
            ],
            [
                InlineKeyboardButton(text="📋 Скопировать", callback_data=f"copy:{session_id}"),
                InlineKeyboardButton(text="🏠 В меню", callback_data="menu"),
            ],
        ]
    )


def refinement_options_keyboard(session_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✂️ Короче", callback_data=f"ref_opt:shorter:{session_id}")],
            [InlineKeyboardButton(text="📱 Под Telegram", callback_data=f"ref_opt:telegram:{session_id}")],
            [InlineKeyboardButton(text="💰 Продающим", callback_data=f"ref_opt:selling:{session_id}")],
            [InlineKeyboardButton(text="🎯 Добавить структуру", callback_data=f"ref_opt:structure:{session_id}")],
            [InlineKeyboardButton(text="✨ Улучшить стиль", callback_data=f"ref_opt:style:{session_id}")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data=f"back:{session_id}")],
        ]
    )


def settings_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить пример текста", callback_data="settings:add_example")],
            [InlineKeyboardButton(text="🗑 Очистить стиль", callback_data="settings:clear_style")],
            [InlineKeyboardButton(text="🏠 В меню", callback_data="menu")],
        ]
    )


def tariffs_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Creator — 490₽/мес", callback_data="buy:creator")],
            [InlineKeyboardButton(text="∞ Unlim — 990₽/мес", callback_data="buy:unlim")],
            [InlineKeyboardButton(text="🏠 В меню", callback_data="menu")],
        ]
    )


# ===========================================================================
# FSM STATES
# ===========================================================================

class GenState(StatesGroup):
    waiting_topic = State()
    waiting_niche = State()
    waiting_rewrite = State()
    waiting_style_example = State()


# ===========================================================================
# OPENAI GENERATION
# ===========================================================================

REFINEMENT_PROMPTS = {
    "shorter": "Сократи этот текст без потери смысла. Убери воду и лишние слова.",
    "telegram": "Перепиши текст так, чтобы он звучал живо и разговорно, как пост в Telegram от реального человека. Добавь эмодзи там, где уместно.",
    "selling": "Переделай текст в продающий: усиль оффер, добавь чёткий призыв к действию, обрати внимание на боль и выгоду.",
    "structure": "Структурируй текст: добавь абзацы, списки, заголовки там, где это уместно. Текст должен легко читаться.",
    "style": "Улучши стиль текста: сделай его более выразительным, ярким и запоминающимся.",
}


async def call_openai(messages: list[dict], user_id: int) -> str:
    """Call OpenAI responses endpoint and return text."""
    logger.info("OpenAI call for user %s, messages=%d", user_id, len(messages))
    try:
        response = await openai_client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=messages,
            max_tokens=1500,
        )
        return response.choices[0].message.content.strip()
    except Exception as exc:
        logger.error("OpenAI error for user %s: %s", user_id, exc)
        raise


def build_generation_messages(
    mode: str,
    prompt: str,
    style_samples: list[str],
    memory_prompt: Optional[str],
) -> list[dict]:
    """Build the message list for OpenAI depending on mode."""
    system_parts = [
        "Ты — умный помощник для создания контента в социальных сетях. "
        "Пиши живо, по-человечески, без воды."
    ]
    if style_samples:
        samples_text = "\n\n".join(style_samples)
        system_parts.append(
            f"Вот примеры стиля пользователя — старайся писать похожим образом:\n\n{samples_text}"
        )
    if memory_prompt:
        system_parts.append(f"Предыдущая тема пользователя в этой категории: {memory_prompt}")

    system_content = "\n\n".join(system_parts)

    if mode == "post":
        user_content = (
            f"Напиши готовый пост для социальных сетей на тему: «{prompt}».\n"
            "Пост должен быть живым, вовлекающим, с эмодзи. Без вступлений и объяснений — только сам пост."
        )
    elif mode == "ideas":
        user_content = (
            f"Придумай 7 идей для постов в нише: «{prompt}».\n"
            "Каждая идея — одно предложение. Пронумеруй список. Идеи должны быть конкретными и цепляющими."
        )
    elif mode == "rewrite":
        user_content = (
            f"Перепиши следующий текст так, чтобы он звучал свежо, живо и по-человечески, "
            f"сохрани смысл, но улучши подачу:\n\n{prompt}"
        )
    else:
        user_content = prompt

    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_content},
    ]


async def generate_content(
    bot: Bot,
    message: Message,
    user: dict,
    mode: str,
    prompt: str,
    state: FSMContext,
) -> None:
    """Core generation flow."""
    telegram_id = user["telegram_id"]
    category = mode

    await track_event(telegram_id, "generation_started", category=category, value=prompt)

    thinking_msg = await message.answer("⏳ Генерирую для тебя... Секунду!")

    try:
        style_samples = await get_style_samples(telegram_id)
        memory_prompt: Optional[str] = None
        if user.get("memory_enabled"):
            memory_prompt = await get_category_memory(telegram_id, category)

        messages = build_generation_messages(mode, prompt, style_samples, memory_prompt)

        result = await call_openai(messages, telegram_id)

        # Save history
        await save_history(telegram_id, category, "user", prompt)
        await save_history(telegram_id, category, "assistant", result, is_final=True)

        # Update memory
        await set_category_memory(telegram_id, category, prompt)

        # Decrement generation unless unlim
        if user["tariff"] != "unlim":
            await decrement_generation(telegram_id)

        # Create session
        session_id = await create_session(telegram_id, category, prompt, result)

        await track_event(telegram_id, "generation_success", category=category)

        await thinking_msg.delete()
        await message.answer(
            f"✨ Готово! Вот твой результат:\n\n{result}",
            reply_markup=post_actions_keyboard(session_id),
        )

    except Exception as exc:
        logger.error("generate_content error for user %s: %s", telegram_id, exc)
        await track_event(telegram_id, "generation_failed", category=category, value=str(exc))
        try:
            await thinking_msg.delete()
        except Exception:
            pass
        await message.answer(
            "Кажется, произошла ошибка 😅 Попробуем ещё раз?",
            reply_markup=main_menu_keyboard(),
        )

    await state.clear()


# ===========================================================================
# ROUTER & HANDLERS
# ===========================================================================

router = Router()


# ---- START ----------------------------------------------------------------

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    user = await get_or_create_user(message.from_user.id)
    await state.clear()
    await message.answer(
        f"Привет, {message.from_user.first_name}! 👋\n\n"
        "Я — *PosteraAI*, твой помощник для создания крутых постов 🚀\n\n"
        f"У тебя сейчас *{user['generations_left']}* генераций. "
        "Давай создадим что-нибудь классное?",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu_keyboard(),
    )


# ---- MAIN MENU BUTTONS ---------------------------------------------------

@router.message(F.text == "✍️ Сгенерировать пост")
async def btn_generate_post(message: Message, state: FSMContext) -> None:
    user = await get_or_create_user(message.from_user.id)
    if not await has_generations(user):
        await track_event(message.from_user.id, "limit_reached", category="post")
        await message.answer(
            "😔 Генерации закончились...\n\n"
            "Но это легко исправить — выбери тариф и продолжай создавать! 💪",
            reply_markup=tariffs_keyboard(),
        )
        return
    await track_event(message.from_user.id, "category_selected", category="post")
    await state.set_state(GenState.waiting_topic)
    await message.answer(
        "✍️ О чём будем писать пост?\n\n"
        "Напиши тему, ключевую мысль или что хочешь донести до аудитории — и я всё сделаю 😎",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(F.text == "💡 Идеи постов")
async def btn_ideas(message: Message, state: FSMContext) -> None:
    user = await get_or_create_user(message.from_user.id)
    if not await has_generations(user):
        await track_event(message.from_user.id, "limit_reached", category="ideas")
        await message.answer(
            "😔 Генерации закончились...\n\n"
            "Выбери тариф, чтобы получить больше идей! 💡",
            reply_markup=tariffs_keyboard(),
        )
        return
    await track_event(message.from_user.id, "category_selected", category="ideas")
    await state.set_state(GenState.waiting_niche)
    await message.answer(
        "💡 Отлично! Укажи нишу или тему — и я придумаю 7 крутых идей для постов 🔥\n\n"
        "Например: *фитнес*, *личные финансы*, *онлайн-образование*…",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(F.text == "♻️ Переписать текст")
async def btn_rewrite(message: Message, state: FSMContext) -> None:
    user = await get_or_create_user(message.from_user.id)
    if not await has_generations(user):
        await track_event(message.from_user.id, "limit_reached", category="rewrite")
        await message.answer(
            "😔 Генерации закончились...\n\n"
            "Один из тарифов поможет тебе продолжить! 🚀",
            reply_markup=tariffs_keyboard(),
        )
        return
    await track_event(message.from_user.id, "category_selected", category="rewrite")
    await state.set_state(GenState.waiting_rewrite)
    await message.answer(
        "♻️ Пришли мне текст, который нужно переписать — я сделаю его живым и классным! ✨",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(F.text == "👤 Мой тариф")
async def btn_my_tariff(message: Message) -> None:
    user = await get_or_create_user(message.from_user.id)
    await track_event(message.from_user.id, "my_tariff_opened")
    tariff_name = {
        "free": "Бесплатный 🆓",
        "creator": "Creator 🚀",
        "unlim": "Unlim ∞",
    }.get(user["tariff"], user["tariff"])

    expires_str = ""
    if user.get("plan_expires_at"):
        expires_str = f"\n📅 Действует до: *{user['plan_expires_at'].strftime('%d.%m.%Y')}*"

    gen_str = "∞ безлимит" if user["tariff"] == "unlim" else str(user["generations_left"])

    await message.answer(
        f"👤 *Твой тариф:* {tariff_name}{expires_str}\n"
        f"⚡ Генераций осталось: *{gen_str}*\n\n"
        "Хочешь больше? Загляни в тарифы! 😉",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu_keyboard(),
    )


@router.message(F.text == "📊 Остаток генераций")
async def btn_remaining(message: Message) -> None:
    user = await get_or_create_user(message.from_user.id)
    gen_str = "∞ безлимит" if user["tariff"] == "unlim" else str(user["generations_left"])
    await message.answer(
        f"📊 У тебя осталось *{gen_str}* генераций.\n\n"
        "Каждый пост — шаг к крутому контенту! 💪",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu_keyboard(),
    )


@router.message(F.text == "💳 Тарифы")
async def btn_tariffs(message: Message) -> None:
    await track_event(message.from_user.id, "payment_screen_opened")
    await message.answer(
        "💳 *Выбери тариф:*\n\n"
        "🚀 *Creator* — 490₽/мес\n"
        "100 генераций в месяц. Идеально для регулярного постинга!\n\n"
        "∞ *Unlim* — 990₽/мес\n"
        "Безлимитные генерации. Твори без ограничений!\n\n"
        "Выбирай — и вперёд! 🔥",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=tariffs_keyboard(),
    )


@router.message(F.text == "🕘 Настройка бота")
async def btn_settings(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "🕘 *Настройки бота*\n\n"
        "Здесь ты можешь научить меня писать в твоём стиле!\n"
        "Добавь примеры своих постов — и я буду учитывать их при генерации 😉",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=settings_keyboard(),
    )


# ---- FSM: GENERATION INPUTS ---------------------------------------------

@router.message(GenState.waiting_topic)
async def handle_topic(message: Message, state: FSMContext, bot: Bot) -> None:
    user = await get_or_create_user(message.from_user.id)
    await generate_content(bot, message, user, "post", message.text, state)


@router.message(GenState.waiting_niche)
async def handle_niche(message: Message, state: FSMContext, bot: Bot) -> None:
    user = await get_or_create_user(message.from_user.id)
    await generate_content(bot, message, user, "ideas", message.text, state)


@router.message(GenState.waiting_rewrite)
async def handle_rewrite(message: Message, state: FSMContext, bot: Bot) -> None:
    user = await get_or_create_user(message.from_user.id)
    await generate_content(bot, message, user, "rewrite", message.text, state)


@router.message(GenState.waiting_style_example)
async def handle_style_example(message: Message, state: FSMContext) -> None:
    await save_style_sample(message.from_user.id, message.text)
    await state.clear()
    await message.answer(
        "✅ Пример добавлен! Рад был помочь — в следующих генерациях учтём твой стиль 😉",
        reply_markup=main_menu_keyboard(),
    )


# ---- INLINE CALLBACKS ----------------------------------------------------

@router.callback_query(F.data == "menu")
async def cb_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.answer("🏠 Главное меню — выбирай, что делаем дальше!", reply_markup=main_menu_keyboard())
    await callback.answer()


@router.callback_query(F.data.startswith("copy:"))
async def cb_copy(callback: CallbackQuery) -> None:
    session_id = int(callback.data.split(":")[1])
    session = await get_session(session_id)
    if session:
        await callback.message.answer(
            f"📋 Вот твой текст для копирования:\n\n{session['generated_text']}",
            reply_markup=main_menu_keyboard(),
        )
    await callback.answer("Текст отправлен! 👆")


@router.callback_query(F.data.startswith("back:"))
async def cb_back(callback: CallbackQuery) -> None:
    session_id = int(callback.data.split(":")[1])
    session = await get_session(session_id)
    if session:
        await callback.message.edit_reply_markup(reply_markup=post_actions_keyboard(session_id))
    await callback.answer()


@router.callback_query(F.data.startswith("refine:"))
async def cb_refine(callback: CallbackQuery) -> None:
    session_id = int(callback.data.split(":")[1])
    await callback.message.edit_reply_markup(reply_markup=refinement_options_keyboard(session_id))
    await callback.answer("Выбери, что улучшить 👇")


@router.callback_query(F.data.startswith("regen:"))
async def cb_regen(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    session_id = int(callback.data.split(":")[1])
    session = await get_session(session_id)
    if not session:
        await callback.answer("Сессия не найдена 😔")
        return

    user = await get_or_create_user(callback.from_user.id)
    if not await has_generations(user):
        await callback.message.answer(
            "😔 Генерации закончились! Выбери тариф, чтобы продолжить.",
            reply_markup=tariffs_keyboard(),
        )
        await callback.answer()
        return

    await callback.answer("🔁 Перегенерирую...")
    style_samples = await get_style_samples(callback.from_user.id)
    memory_prompt = await get_category_memory(callback.from_user.id, session["category"])
    messages = build_generation_messages(session["category"], session["original_prompt"], style_samples, memory_prompt)

    thinking_msg = await callback.message.answer("⏳ Пробую по-другому... Один момент!")
    try:
        result = await call_openai(messages, callback.from_user.id)
        await update_session_text(session_id, result)
        if user["tariff"] != "unlim":
            await decrement_generation(callback.from_user.id)
        await track_event(callback.from_user.id, "generation_success", category=session["category"])
        await thinking_msg.delete()
        await callback.message.answer(
            f"🔁 Вот новый вариант:\n\n{result}",
            reply_markup=post_actions_keyboard(session_id),
        )
    except Exception as exc:
        logger.error("regen error: %s", exc)
        await thinking_msg.delete()
        await callback.message.answer("Кажется, произошла ошибка 😅 Попробуем ещё раз?")


@router.callback_query(F.data.startswith("ref_opt:"))
async def cb_refinement_option(callback: CallbackQuery, bot: Bot) -> None:
    _, option, session_id_str = callback.data.split(":")
    session_id = int(session_id_str)
    session = await get_session(session_id)
    if not session:
        await callback.answer("Сессия не найдена 😔")
        return

    user = await get_or_create_user(callback.from_user.id)

    # Check if refinement is free (first 2 are free)
    is_free_refinement = session["refinement_count"] < FREE_REFINEMENTS
    if not is_free_refinement and not await has_generations(user):
        await callback.message.answer(
            "😔 Генерации закончились! Первые 2 доработки — бесплатно, дальше нужны генерации.\n\n"
            "Выбери тариф, чтобы продолжить 👇",
            reply_markup=tariffs_keyboard(),
        )
        await callback.answer()
        return

    await callback.answer("✏️ Дорабатываю...")
    refinement_instruction = REFINEMENT_PROMPTS.get(option, "Улучши текст.")
    messages = [
        {"role": "system", "content": "Ты помогаешь улучшать тексты для социальных сетей. Возвращай только готовый текст без пояснений."},
        {"role": "user", "content": f"{refinement_instruction}\n\nТекст:\n{session['generated_text']}"},
    ]
    thinking_msg = await callback.message.answer("⏳ Дорабатываю текст... Сейчас будет лучше!")
    try:
        result = await call_openai(messages, callback.from_user.id)
        await update_session_text(session_id, result)
        if not is_free_refinement and user["tariff"] != "unlim":
            await decrement_generation(callback.from_user.id)
        await thinking_msg.delete()
        await callback.message.answer(
            f"✨ Готово! Вот улучшенная версия:\n\n{result}",
            reply_markup=post_actions_keyboard(session_id),
        )
    except Exception as exc:
        logger.error("refinement error: %s", exc)
        await thinking_msg.delete()
        await callback.message.answer("Кажется, произошла ошибка 😅 Попробуем ещё раз?")


# ---- SETTINGS CALLBACKS --------------------------------------------------

@router.callback_query(F.data == "settings:add_example")
async def cb_add_example(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(GenState.waiting_style_example)
    await callback.message.answer(
        "📝 Пришли мне пример своего поста или текста.\n"
        "Я запомню стиль и буду писать похожим образом 😊",
        reply_markup=ReplyKeyboardRemove(),
    )
    await callback.answer()


@router.callback_query(F.data == "settings:clear_style")
async def cb_clear_style(callback: CallbackQuery) -> None:
    await clear_style_samples(callback.from_user.id)
    await callback.message.answer(
        "🗑 Стиль очищен! Теперь буду писать без привязки к примерам.\n"
        "Захочешь — добавь новые 😉",
        reply_markup=main_menu_keyboard(),
    )
    await callback.answer("Готово!")


# ---- PAYMENT CALLBACKS ---------------------------------------------------

@router.callback_query(F.data.startswith("buy:"))
async def cb_buy(callback: CallbackQuery) -> None:
    tariff_key = callback.data.split(":")[1]
    tariff = TARIFFS.get(tariff_key)
    if not tariff:
        await callback.answer("Тариф не найден 😔")
        return

    await track_event(callback.from_user.id, "payment_created", value=tariff_key)

    idempotence_key = str(uuid.uuid4())
    try:
        payment = Payment.create(
            {
                "amount": {"value": str(tariff["price"]) + ".00", "currency": "RUB"},
                "confirmation": {
                    "type": "redirect",
                    "return_url": PAYMENT_RETURN_URL,
                },
                "capture": True,
                "description": f"PosteraAI — тариф {tariff['name']} для пользователя {callback.from_user.id}",
                "metadata": {
                    "telegram_id": str(callback.from_user.id),
                    "tariff": tariff_key,
                },
            },
            idempotence_key,
        )

        async with await get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO payments (telegram_id, amount, tariff, status, payment_id) VALUES (%s,%s,%s,%s,%s)",
                    (callback.from_user.id, tariff["price"], tariff_key, "pending", payment.id),
                )
            await conn.commit()

        logger.info("Payment created: %s for user %s", payment.id, callback.from_user.id)

        pay_url = payment.confirmation.confirmation_url
        pay_keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="💳 Перейти к оплате", url=pay_url)],
                [InlineKeyboardButton(text="🏠 В меню", callback_data="menu")],
            ]
        )
        await callback.message.answer(
            f"✅ Отлично! Ссылка для оплаты тарифа *{tariff['name']}* готова!\n\n"
            f"💰 Сумма: *{tariff['price']}₽*\n\n"
            "После оплаты тариф активируется автоматически — можешь сразу творить! 🚀",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=pay_keyboard,
        )
    except Exception as exc:
        logger.error("Payment creation error for user %s: %s", callback.from_user.id, exc)
        await callback.message.answer(
            "😔 Не получилось создать платёж... Попробуй немного позже или напиши в поддержку.",
            reply_markup=main_menu_keyboard(),
        )

    await callback.answer()


# ===========================================================================
# YOOKASSA WEBHOOK
# ===========================================================================

async def handle_yookassa_webhook(request: web.Request) -> web.Response:
    """Handle YooKassa payment notifications."""
    try:
        body = await request.json()
        logger.info("YooKassa webhook received: %s", json.dumps(body)[:500])

        event = body.get("event")
        obj = body.get("object", {})
        payment_id = obj.get("id")
        status = obj.get("status")
        metadata = obj.get("metadata", {})

        if event == "payment.succeeded" and status == "succeeded":
            telegram_id_str = metadata.get("telegram_id")
            tariff_key = metadata.get("tariff")

            if not telegram_id_str or not tariff_key:
                logger.warning("Webhook missing metadata: %s", metadata)
                return web.Response(status=200)

            telegram_id = int(telegram_id_str)
            tariff = TARIFFS.get(tariff_key)
            if not tariff:
                logger.warning("Unknown tariff in webhook: %s", tariff_key)
                return web.Response(status=200)

            expires_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=tariff["days"])
            gens = tariff["generations"]

            async with await get_conn() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        UPDATE users
                        SET tariff = %s,
                            generations_left = %s,
                            plan_expires_at = %s
                        WHERE telegram_id = %s
                        """,
                        (tariff_key, gens, expires_at, telegram_id),
                    )
                    await cur.execute(
                        "UPDATE payments SET status = 'succeeded' WHERE payment_id = %s",
                        (payment_id,),
                    )
                await conn.commit()

            await track_event(telegram_id, "payment_success", value=tariff_key)
            logger.info("Tariff %s activated for user %s", tariff_key, telegram_id)

            # Notify user via bot
            bot: Bot = request.app["bot"]
            try:
                gen_str = "∞ безлимитных" if tariff_key == "unlim" else str(gens)
                await bot.send_message(
                    telegram_id,
                    f"🎉 Оплата прошла успешно!\n\n"
                    f"✅ Тариф *{tariff['name']}* активирован!\n"
                    f"⚡ У тебя теперь *{gen_str}* генераций!\n\n"
                    "Теперь ты в числе лучших — врывайся и создавай контент! 🚀",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=main_menu_keyboard(),
                )
            except Exception as exc:
                logger.error("Could not notify user %s after payment: %s", telegram_id, exc)

    except Exception as exc:
        logger.error("Webhook processing error: %s", exc)

    return web.Response(status=200)


async def handle_health(request: web.Request) -> web.Response:
    return web.Response(text="OK", status=200)


# ===========================================================================
# AIOHTTP SERVER
# ===========================================================================

async def create_aiohttp_app(bot: Bot) -> web.Application:
    app = web.Application()
    app["bot"] = bot
    app.router.add_get("/health", handle_health)
    app.router.add_post("/yookassa/webhook", handle_yookassa_webhook)
    return app


# ===========================================================================
# MAIN ENTRY POINT
# ===========================================================================

async def main() -> None:
    logger.info("PosteraAI starting up 🚀")

    # Init database
    await init_db()

    # Create bot and dispatcher
    bot = Bot(token=TELEGRAM_BOT_TOKEN, parse_mode=ParseMode.HTML)
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)
    dp.include_router(router)

    # Create aiohttp app
    aiohttp_app = await create_aiohttp_app(bot)
    runner = web.AppRunner(aiohttp_app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()
    logger.info("HTTP server started on port %s", PORT)

    # Start polling
    logger.info("Starting Telegram polling…")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await runner.cleanup()
        await bot.session.close()
        logger.info("PosteraAI shut down.")


if __name__ == "__main__":
    asyncio.run(main())