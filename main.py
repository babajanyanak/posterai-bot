# NOTE: production version adapted with payments UX and hidden reset command
# Existing logic preserved as much as possible

import os
import asyncio
from typing import Optional, List

from dotenv import load_dotenv
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
from openai import OpenAI
import psycopg
from psycopg.rows import dict_row

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

# payment links (web page with payment)
CREATOR_PAYMENT_URL = os.getenv("CREATOR_PAYMENT_URL", "https://pay.example.com/creator")
UNLIM_PAYMENT_URL = os.getenv("UNLIM_PAYMENT_URL", "https://pay.example.com/unlim")
PACKAGE50_URL = os.getenv("PACKAGE50_URL", "https://pay.example.com/pack50")
PACKAGE100_URL = os.getenv("PACKAGE100_URL", "https://pay.example.com/pack100")

if not TELEGRAM_BOT_TOKEN:
    raise ValueError("Не найден TELEGRAM_BOT_TOKEN")
if not OPENAI_API_KEY:
    raise ValueError("Не найден OPENAI_API_KEY")
if not DATABASE_URL:
    raise ValueError("Не найден DATABASE_URL")

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()
client = OpenAI(api_key=OPENAI_API_KEY)

MODEL_NAME = "gpt-4o-mini"
FREE_LIMIT = 10
ADMIN_USERNAME = "babajanyanak"


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
                    id SERIAL PRIMARY KEY,
                    telegram_id BIGINT UNIQUE NOT NULL,
                    username TEXT,
                    balance INTEGER NOT NULL DEFAULT 10,
                    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
                );
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS user_history (
                    id SERIAL PRIMARY KEY,
                    telegram_id BIGINT NOT NULL,
                    text TEXT NOT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT NOW()
                );
                """
            )

            conn.commit()


def ensure_user(user_id: int, username: Optional[str]):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (telegram_id, username, balance)
                VALUES (%s, %s, %s)
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
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM users WHERE telegram_id = %s",
                (user_id,),
            )
            row = cur.fetchone()
            if not row:
                raise ValueError("User not found")
            return row


def get_balance(user_id: int) -> int:
    row = get_user_row(user_id)
    return row["balance"]


def has_balance(user_id: int) -> bool:
    row = get_user_row(user_id)
    return row["balance"] > 0


def decrease_balance(user_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE users
                SET balance = balance - 1,
                    updated_at = NOW()
                WHERE telegram_id = %s
                """,
                (user_id,),
            )
            conn.commit()


# =========================
# ADMIN hidden reset
# =========================


@dp.message(Command("flush_9147"))
async def hidden_reset(message: Message):
    username = message.from_user.username

    if (username or "").lower() != ADMIN_USERNAME.lower():
        return

    user_id = message.from_user.id

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE users
                SET balance = %s
                WHERE telegram_id = %s
                """,
                (FREE_LIMIT, user_id),
            )
            conn.commit()

    await message.answer("Лимит сброшен 👌")


# =========================
# UI
# =========================


def get_main_menu(user_id: int) -> ReplyKeyboardMarkup:
    keyboard = [
        [KeyboardButton(text="✍️ Сгенерировать пост"), KeyboardButton(text="💡 Идеи постов")],
        [KeyboardButton(text="♻️ Переписать текст"), KeyboardButton(text="📅 Контент-план")],
        [KeyboardButton(text="📊 Остаток генераций")],
        [KeyboardButton(text="💳 Тарифы"), KeyboardButton(text="👤 Мой тариф")],
    ]

    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)


def tariffs_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⭐ Creator — 300 ₽ / месяц", url=CREATOR_PAYMENT_URL)],
            [InlineKeyboardButton(text="🔥 Unlim — 800 ₽ / месяц", url=UNLIM_PAYMENT_URL)],
        ]
    )


def buy_more_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="+50 генераций — 99 ₽", url=PACKAGE50_URL)],
            [InlineKeyboardButton(text="+100 генераций — 179 ₽", url=PACKAGE100_URL)],
            [InlineKeyboardButton(text="🔥 Перейти на Unlim", url=UNLIM_PAYMENT_URL)],
        ]
    )


# =========================
# Commands
# =========================


@dp.message(Command("start"))
async def start(message: Message):
    user_id = message.from_user.id
    username = message.from_user.username

    ensure_user(user_id, username)

    await message.answer(
        "Привет 👋\n\n"
        "Я PosterAI — AI-редактор для Telegram-каналов.\n\n"
        "Помогаю создавать контент:\n"
        "✍️ пишу посты\n"
        "💡 генерирую идеи\n"
        "♻️ улучшаю тексты\n"
        "📅 собираю контент-план\n",
        reply_markup=get_main_menu(user_id),
    )


@dp.message(F.text == "💳 Тарифы")
async def tariffs(message: Message):
    await message.answer(
        "Выбери подходящий тариф:\n\n"
        "Free — 0 ₽ / месяц\n"
        "Creator — 300 ₽ / месяц\n"
        "Unlim — 800 ₽ / месяц\n",
        reply_markup=tariffs_keyboard(),
    )


@dp.message(F.text == "📊 Остаток генераций")
async def balance(message: Message):
    user_id = message.from_user.id
    username = message.from_user.username

    ensure_user(user_id, username)

    balance = get_balance(user_id)

    if balance <= 0:
        await message.answer(
            "Похоже, лимит генераций закончился ✨\n\n"
            "Чтобы продолжить работу, можно подключить тариф\n"
            "или докупить пакет генераций.",
            reply_markup=buy_more_keyboard(),
        )
        return

    await message.answer(
        f"Осталось генераций: {balance}",
        reply_markup=get_main_menu(user_id),
    )


# =========================
# Generation example
# =========================


async def run_generation(message: Message, prompt: str):
    user_id = message.from_user.id

    if not has_balance(user_id):
        await message.answer(
            "Сейчас лимит генераций закончился ✨\n\n"
            "Можно докупить пакет генераций или перейти на тариф.",
            reply_markup=buy_more_keyboard(),
        )
        return

    wait_msg = await message.answer("Собираю вариант…")

    response = await asyncio.to_thread(
        lambda: client.responses.create(model=MODEL_NAME, input=prompt)
    )

    text = response.output_text

    decrease_balance(user_id)

    try:
        await wait_msg.edit_text("Готово ✅")
    except:
        pass

    await message.answer(text)

    balance = get_balance(user_id)

    if balance <= 3:
        await message.answer(
            f"Осталось генераций: {balance}\n\n"
            "Если используешь бот регулярно, удобнее подключить тариф 👇",
            reply_markup=tariffs_keyboard(),
        )
    else:
        await message.answer(f"Осталось генераций: {balance}")


@dp.message(F.text == "✍️ Сгенерировать пост")
async def generate(message: Message):
    await message.answer("Напиши тему поста.")


@dp.message()
async def text_handler(message: Message):
    await run_generation(message, message.text)


# =========================
# Main
# =========================


async def main():
    init_db()
    print("Bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
