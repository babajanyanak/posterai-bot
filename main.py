import os
import json
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional, List

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.memory import MemoryStorage

from aiohttp import web

from openai import OpenAI

import psycopg
from psycopg.rows import dict_row


load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("posterai")


TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID")
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY")
YOOKASSA_RETURN_URL = os.getenv("YOOKASSA_RETURN_URL")

APP_HOST = os.getenv("APP_HOST", "0.0.0.0")
APP_PORT = int(os.getenv("PORT", os.getenv("APP_PORT", "8000")))

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


bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
client = OpenAI(api_key=OPENAI_API_KEY)


MODEL_NAME = "gpt-4o-mini"

FREE_LIMIT = 10


# =========================
# FSM
# =========================

class GeneratePost(StatesGroup):
    waiting_topic = State()
    custom_edit = State()
    audience = State()


class IdeasState(StatesGroup):
    waiting_topic = State()
    custom_edit = State()
    audience = State()


class RewriteState(StatesGroup):
    waiting_text = State()
    custom_edit = State()
    audience = State()


class StyleState(StatesGroup):
    collecting_posts = State()


# =========================
# DB
# =========================


def get_conn():
    return psycopg.connect(DATABASE_URL)


def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id BIGINT PRIMARY KEY,
                    username TEXT,
                    balance INTEGER DEFAULT 10,
                    tariff TEXT DEFAULT 'free',
                    memory_enabled BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT NOW()
                );
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS user_history (
                    id SERIAL PRIMARY KEY,
                    telegram_id BIGINT,
                    category TEXT,
                    text TEXT,
                    created_at TIMESTAMP DEFAULT NOW()
                );
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS style_posts (
                    id SERIAL PRIMARY KEY,
                    telegram_id BIGINT,
                    text TEXT
                );
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS payments (
                    id SERIAL PRIMARY KEY,
                    payment_id TEXT,
                    telegram_id BIGINT,
                    product_code TEXT,
                    status TEXT,
                    created_at TIMESTAMP DEFAULT NOW()
                );
                """
            )

            conn.commit()


def ensure_user(user_id: int, username: Optional[str]):
    with get_conn() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                INSERT INTO users (telegram_id, username)
                VALUES (%s, %s)
                ON CONFLICT (telegram_id)
                DO UPDATE SET username = EXCLUDED.username
                """,
                (user_id, username),
            )

            conn.commit()


def get_balance(user_id: int) -> int:
    with get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:

            cur.execute(
                "SELECT balance FROM users WHERE telegram_id=%s",
                (user_id,),
            )

            row = cur.fetchone()

            return row["balance"] if row else 0


def decrease_balance(user_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                UPDATE users
                SET balance = balance - 1
                WHERE telegram_id=%s
                """,
                (user_id,),
            )

            conn.commit()
# =========================
# UI
# =========================


def main_menu():

    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="✍️ Сгенерировать пост"),
                KeyboardButton(text="💡 Идеи постов"),
            ],
            [
                KeyboardButton(text="♻️ Переписать текст"),
                KeyboardButton(text="📊 Остаток генераций"),
            ],
            [
                KeyboardButton(text="🕘 Настройка бота"),
                KeyboardButton(text="💳 Тарифы"),
            ],
            [
                KeyboardButton(text="👤 Мой тариф"),
            ],
        ],
        resize_keyboard=True,
    )


def navigation_buttons():

    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="⬅️ Назад"),
                KeyboardButton(text="🏠 Главное меню"),
                KeyboardButton(text="❌ Отмена"),
            ]
        ],
        resize_keyboard=True,
    )


def edit_buttons():

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✂️ Короче", callback_data="shorter"),
                InlineKeyboardButton(text="💰 Продающим", callback_data="sales"),
            ],
            [
                InlineKeyboardButton(text="✨ Живее", callback_data="lively"),
                InlineKeyboardButton(text="📣 С CTA", callback_data="cta"),
            ],
            [
                InlineKeyboardButton(text="📱 Под Telegram", callback_data="telegram"),
                InlineKeyboardButton(text="🎯 Для конкретной ЦА", callback_data="audience"),
            ],
            [
                InlineKeyboardButton(text="🔁 Другой вариант", callback_data="alt"),
                InlineKeyboardButton(text="✏️ Своя правка", callback_data="custom"),
            ],
        ]
    )
# =========================
# GENERATION
# =========================


async def ai_generate(prompt: str):

    response = await asyncio.to_thread(
        lambda: client.responses.create(
            model=MODEL_NAME,
            input=prompt,
        )
    )

    return response.output_text


# =========================
# COMMANDS
# =========================


@dp.message(Command("start"))
async def start(message: Message):

    ensure_user(message.from_user.id, message.from_user.username)

    await message.answer(
        """
Привет 👋

Я PosterAI — AI-редактор для Telegram-каналов.

Помогаю создавать контент:
✍️ пишу посты
💡 генерирую идеи
♻️ переписываю тексты

Выбирай действие 👇
""",
        reply_markup=main_menu(),
    )


@dp.message(F.text == "✍️ Сгенерировать пост")
async def post_start(message: Message, state: FSMContext):

    await state.set_state(GeneratePost.waiting_topic)

    await message.answer(
        """
Напиши тему поста или короткий бриф.

Можно указать:
• тему
• продукт
• цель поста
• аудиторию
""",
        reply_markup=navigation_buttons(),
    )


@dp.message(GeneratePost.waiting_topic)
async def post_generate(message: Message):

    text = message.text

    prompt = f"Напиши пост для Telegram на тему: {text}"

    await message.answer("Собираю вариант… ✍️")

    result = await ai_generate(prompt)

    await message.answer(result, reply_markup=edit_buttons())


# =========================
# PAYMENTS (YOOKASSA)
# =========================


async def payment_return_handler(request):

    raise web.HTTPFound("https://t.me/PosteraAI_bot")


async def webhook_handler(request):

    payload = await request.json()

    logger.info("Webhook received: %s", json.dumps(payload))

    event = payload["event"]

    if event == "payment.succeeded":

        payment_id = payload["object"]["id"]

        with get_conn() as conn:
            with conn.cursor(row_factory=dict_row) as cur:

                cur.execute(
                    "SELECT telegram_id FROM payments WHERE payment_id=%s",
                    (payment_id,),
                )

                row = cur.fetchone()

                if row:

                    telegram_id = row["telegram_id"]

                    cur.execute(
                        """
                        UPDATE users
                        SET balance = balance + 100
                        WHERE telegram_id=%s
                        """,
                        (telegram_id,),
                    )

                    conn.commit()

                    await bot.send_message(
                        telegram_id,
                        "✅ Оплата прошла успешно!\n\nГенерации начислены.",
                    )

    return web.Response(text="ok")


# =========================
# HTTP SERVER
# =========================


async def start_http():

    app = web.Application()

    app.router.add_get("/payment-return", payment_return_handler)
    app.router.add_post("/yookassa/webhook", webhook_handler)

    runner = web.AppRunner(app)

    await runner.setup()

    site = web.TCPSite(runner, APP_HOST, APP_PORT)

    await site.start()

    logger.info("HTTP server started on %s:%s", APP_HOST, APP_PORT)


# =========================
# MAIN
# =========================


async def main():

    init_db()

    await start_http()

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())