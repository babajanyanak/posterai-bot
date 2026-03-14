# NOTE:
# Updated production-like version for local testing:
# - aiogram bot
# - OpenAI generation
# - PostgreSQL
# - user history
# - YooKassa payments via API
# - webhook processing
# - redirect back to Telegram bot after payment
# - hidden reset command
# - Unlim hidden for babajanyanak

import os
import json
import uuid
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional, List

import aiohttp
from aiohttp import web, BasicAuth

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


# =========================
# CONFIG
# =========================

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID")
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY")
YOOKASSA_RETURN_URL = os.getenv("YOOKASSA_RETURN_URL")
YOOKASSA_WEBHOOK_URL = os.getenv("YOOKASSA_WEBHOOK_URL")

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
if not YOOKASSA_RETURN_URL:
    raise ValueError("Не найден YOOKASSA_RETURN_URL")
if not YOOKASSA_WEBHOOK_URL:
    raise ValueError("Не найден YOOKASSA_WEBHOOK_URL")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("posterai-bot")

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()
client = OpenAI(api_key=OPENAI_API_KEY)

MODEL_NAME = "gpt-4o-mini"
FREE_LIMIT = 10
ADMIN_USERNAME = "babajanyanak"
TELEGRAM_BOT_USERNAME = "PosteraAI_bot"

# Если username совпадает, Unlim не показываем в UX
HIDE_UNLIM_FOR = {"babajanyanak"}

# Скрытая команда сброса лимита
HIDDEN_RESET_COMMAND = "/flush_9147"

# История в генерации
HISTORY_DEPTH = 8


# =========================
# TARIFFS / PACKAGES
# =========================

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
                    id SERIAL PRIMARY KEY,
                    telegram_id BIGINT UNIQUE NOT NULL,
                    username TEXT,
                    balance INTEGER NOT NULL DEFAULT 10,
                    tariff_code TEXT NOT NULL DEFAULT 'free',
                    tariff_started_at TIMESTAMPTZ,
                    tariff_expires_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS user_history (
                    id SERIAL PRIMARY KEY,
                    telegram_id BIGINT NOT NULL,
                    role TEXT NOT NULL,
                    text TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
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

            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_users_telegram_id
                ON users (telegram_id);
                """
            )

            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_history_telegram_id_created
                ON user_history (telegram_id, created_at DESC);
                """
            )

            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_payments_telegram_id
                ON payments (telegram_id);
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
    row = get_user_row(user_id)
    return int(row["balance"]) > 0


def decrease_balance(user_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE users
                SET balance = GREATEST(balance - 1, 0),
                    updated_at = NOW()
                WHERE telegram_id = %s
                """,
                (user_id,),
            )
            conn.commit()


def set_balance(user_id: int, value: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE users
                SET balance = %s,
                    updated_at = NOW()
                WHERE telegram_id = %s
                """,
                (value, user_id),
            )
            conn.commit()


def add_balance(user_id: int, value: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE users
                SET balance = balance + %s,
                    updated_at = NOW()
                WHERE telegram_id = %s
                """,
                (value, user_id),
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


def get_last_history(user_id: int, limit: int = HISTORY_DEPTH) -> List[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT role, text, created_at
                FROM user_history
                WHERE telegram_id = %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (user_id, limit),
            )
            rows = cur.fetchall() or []
            rows.reverse()
            return rows


def save_history(user_id: int, role: str, text: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO user_history (telegram_id, role, text)
                VALUES (%s, %s, %s)
                """,
                (user_id, role, text),
            )
            conn.commit()


def get_payment(payment_id: str) -> Optional[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM payments
                WHERE payment_id = %s
                """,
                (payment_id,),
            )
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


# =========================
# UI
# =========================

def get_main_menu() -> ReplyKeyboardMarkup:
    keyboard = [
        [KeyboardButton(text="✍️ Сгенерировать пост"), KeyboardButton(text="💡 Идеи постов")],
        [KeyboardButton(text="♻️ Переписать текст"), KeyboardButton(text="📅 Контент-план")],
        [KeyboardButton(text="📊 Остаток генераций"), KeyboardButton(text="🕘 История")],
        [KeyboardButton(text="💳 Тарифы"), KeyboardButton(text="👤 Мой тариф")],
    ]
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)


def can_show_unlim(username: Optional[str]) -> bool:
    return (username or "").lower() not in HIDE_UNLIM_FOR


def tariffs_keyboard(username: Optional[str]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="⭐ Creator — 300 ₽ / месяц", callback_data="buy:creator_monthly")]
    ]

    if can_show_unlim(username):
        rows.append(
            [InlineKeyboardButton(text="🔥 Unlim — 800 ₽ / месяц", callback_data="buy:unlim_monthly")]
        )

    return InlineKeyboardMarkup(inline_keyboard=rows)


def buy_more_keyboard(username: Optional[str]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="+50 генераций — 99 ₽", callback_data="buy:pack_50")],
        [InlineKeyboardButton(text="+100 генераций — 179 ₽", callback_data="buy:pack_100")],
    ]

    if can_show_unlim(username):
        rows.append(
            [InlineKeyboardButton(text="🔥 Перейти на Unlim", callback_data="buy:unlim_monthly")]
        )

    return InlineKeyboardMarkup(inline_keyboard=rows)


def payment_link_keyboard(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 Перейти к оплате", url=url)]
        ]
    )


def format_my_tariff(user: dict) -> str:
    tariff_code = user.get("tariff_code") or "free"
    balance = int(user.get("balance") or 0)
    expires_at = user.get("tariff_expires_at")

    tariff_name_map = {
        "free": "Free",
        "creator_monthly": "Creator",
        "unlim_monthly": "Unlim",
    }

    title = tariff_name_map.get(tariff_code, tariff_code)

    expires_text = "—"
    if expires_at:
        expires_text = expires_at.strftime("%d.%m.%Y %H:%M")

    return (
        "👤 Ваш тариф\n\n"
        f"Текущий тариф: {title}\n"
        f"Остаток генераций: {balance}\n"
        f"Действует до: {expires_text}"
    )


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

    return payment_id, confirmation_url


async def apply_successful_payment(payment_row: dict):
    product_code = payment_row["product_code"]
    product = TARIFFS[product_code]
    telegram_id = int(payment_row["telegram_id"])

    if product["type"] == "subscription":
        activate_subscription(
            user_id=telegram_id,
            tariff_code=product_code,
            balance_add=product["balance_add"],
            days=product["days"],
        )
    else:
        add_balance(telegram_id, product["balance_add"])

    title = product["title"]

    try:
        await bot.send_message(
            telegram_id,
            (
                "✅ Оплата прошла успешно!\n\n"
                f"Начислили: {title}\n"
                "Изменения уже применили, можно продолжать работу 😉"
            ),
            reply_markup=get_main_menu(),
        )
    except Exception:
        logger.exception("Не удалось отправить пользователю сообщение об успешной оплате")


# =========================
# GENERATION
# =========================

def build_system_instruction(mode_title: str) -> str:
    return (
        "Ты AI-редактор для Telegram-каналов. "
        "Пиши по-русски, живо, структурно и без воды. "
        "Учитывай предыдущий контекст диалога пользователя. "
        f"Текущий режим: {mode_title}. "
        "Если уместно, предлагай 2–3 варианта или структуру. "
        "Не упоминай технические детали модели."
    )


def compose_input_with_history(user_id: int, mode_title: str, prompt: str) -> str:
    history = get_last_history(user_id, HISTORY_DEPTH)

    lines: List[str] = []
    lines.append(build_system_instruction(mode_title))
    lines.append("")
    lines.append("История пользователя:")
    if history:
        for item in history:
            role = item["role"]
            text = item["text"]
            lines.append(f"{role.upper()}: {text}")
    else:
        lines.append("История пока пустая.")

    lines.append("")
    lines.append(f"Новый запрос пользователя: {prompt}")
    lines.append("")
    lines.append("Сформируй лучший ответ для этого режима.")

    return "\n".join(lines)


async def generate_text(mode_title: str, user_id: int, prompt: str) -> str:
    full_input = compose_input_with_history(user_id, mode_title, prompt)

    response = await asyncio.to_thread(
        lambda: client.responses.create(
            model=MODEL_NAME,
            input=full_input,
        )
    )

    text = response.output_text.strip()
    return text


async def run_generation(message: Message, prompt: str, mode_title: str):
    user_id = message.from_user.id
    username = message.from_user.username

    ensure_user(user_id, username)

    if not has_balance(user_id):
        await message.answer(
            "Сейчас лимит генераций закончился ✨\n\n"
            "Можно докупить пакет генераций или перейти на тариф.",
            reply_markup=buy_more_keyboard(username),
        )
        return

    wait_msg = await message.answer("Собираю вариант…")

    try:
        text = await generate_text(mode_title=mode_title, user_id=user_id, prompt=prompt)

        save_history(user_id, "user", prompt)
        save_history(user_id, "assistant", text)

        decrease_balance(user_id)

        try:
            await wait_msg.edit_text("Готово ✅")
        except Exception:
            pass

        await message.answer(text, reply_markup=get_main_menu())

        balance_now = get_balance(user_id)
        if balance_now <= 3:
            await message.answer(
                f"Осталось генераций: {balance_now}\n\n"
                "Если используешь бот регулярно, удобнее подключить тариф 👇",
                reply_markup=tariffs_keyboard(username),
            )
        else:
            await message.answer(
                f"Осталось генераций: {balance_now}",
                reply_markup=get_main_menu(),
            )

    except Exception:
        logger.exception("Ошибка генерации")
        try:
            await wait_msg.edit_text("Не получилось подготовить ответ 🙏")
        except Exception:
            pass
        await message.answer(
            "Что-то пошло не так при генерации. Попробуй ещё раз.",
            reply_markup=get_main_menu(),
        )


# =========================
# STATES VIA SIMPLE MODE
# =========================

USER_MODES: dict[int, str] = {}


def set_mode(user_id: int, mode: str):
    USER_MODES[user_id] = mode


def get_mode(user_id: int) -> Optional[str]:
    return USER_MODES.get(user_id)


def clear_mode(user_id: int):
    USER_MODES.pop(user_id, None)


# =========================
# ADMIN hidden reset
# =========================

@dp.message(Command("flush_9147"))
async def hidden_reset(message: Message):
    username = message.from_user.username

    if (username or "").lower() != ADMIN_USERNAME.lower():
        return

    user_id = message.from_user.id
    ensure_user(user_id, username)
    set_balance(user_id, FREE_LIMIT)

    await message.answer("Лимит сброшен 👌", reply_markup=get_main_menu())


# =========================
# Commands / menu
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
        reply_markup=get_main_menu(),
    )


@dp.message(F.text == "💳 Тарифы")
async def tariffs(message: Message):
    username = message.from_user.username

    lines = [
        "Выбери подходящий тариф:\n",
        "Free — 0 ₽ / стартовый доступ",
        "Creator — 300 ₽ / месяц",
    ]

    if can_show_unlim(username):
        lines.append("Unlim — 800 ₽ / месяц")

    lines.append("")
    lines.append("Плюс можно докупить пакеты генераций.")

    await message.answer(
        "\n".join(lines),
        reply_markup=tariffs_keyboard(username),
    )

    await message.answer(
        "Дополнительные пакеты 👇",
        reply_markup=buy_more_keyboard(username),
    )


@dp.message(F.text == "👤 Мой тариф")
async def my_tariff(message: Message):
    user_id = message.from_user.id
    username = message.from_user.username
    ensure_user(user_id, username)
    user = get_user_row(user_id)

    await message.answer(
        format_my_tariff(user),
        reply_markup=get_main_menu(),
    )


@dp.message(F.text == "📊 Остаток генераций")
async def balance(message: Message):
    user_id = message.from_user.id
    username = message.from_user.username

    ensure_user(user_id, username)
    balance_now = get_balance(user_id)

    if balance_now <= 0:
        await message.answer(
            "Похоже, лимит генераций закончился ✨\n\n"
            "Чтобы продолжить работу, можно подключить тариф\n"
            "или докупить пакет генераций.",
            reply_markup=buy_more_keyboard(username),
        )
        return

    await message.answer(
        f"Осталось генераций: {balance_now}",
        reply_markup=get_main_menu(),
    )


@dp.message(F.text == "🕘 История")
async def history_view(message: Message):
    user_id = message.from_user.id
    username = message.from_user.username
    ensure_user(user_id, username)

    history = get_last_history(user_id, 10)

    if not history:
        await message.answer(
            "История пока пустая.",
            reply_markup=get_main_menu(),
        )
        return

    lines = ["🕘 Последние сообщения:\n"]
    for item in history[-10:]:
        role = "Вы" if item["role"] == "user" else "Бот"
        text = item["text"].replace("\n", " ").strip()
        if len(text) > 120:
            text = text[:120] + "…"
        lines.append(f"{role}: {text}")

    await message.answer(
        "\n".join(lines),
        reply_markup=get_main_menu(),
    )


@dp.message(F.text == "✍️ Сгенерировать пост")
async def generate_post(message: Message):
    set_mode(message.from_user.id, "post")
    await message.answer("Напиши тему поста или короткий бриф.", reply_markup=get_main_menu())


@dp.message(F.text == "💡 Идеи постов")
async def ideas_posts(message: Message):
    set_mode(message.from_user.id, "ideas")
    await message.answer("Напиши тему, нишу или продукт — подготовлю идеи постов.", reply_markup=get_main_menu())


@dp.message(F.text == "♻️ Переписать текст")
async def rewrite_text(message: Message):
    set_mode(message.from_user.id, "rewrite")
    await message.answer("Пришли текст, который нужно улучшить или переписать.", reply_markup=get_main_menu())


@dp.message(F.text == "📅 Контент-план")
async def content_plan(message: Message):
    set_mode(message.from_user.id, "plan")
    await message.answer("Напиши тему, период и цель — соберу контент-план.", reply_markup=get_main_menu())


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
            reply_markup=get_main_menu(),
        )
        await callback.answer()
        return

    product = TARIFFS[product_code]

    await callback.message.answer(
        (
            f"Подготовили оплату: {product['title']} ✅\n\n"
            f"Сумма: {product['price']} ₽\n"
            "Открой страницу оплаты по кнопке ниже."
        ),
        reply_markup=payment_link_keyboard(confirmation_url),
    )

    await callback.answer()


# =========================
# Fallback text handler
# =========================

@dp.message()
async def text_handler(message: Message):
    user_id = message.from_user.id
    username = message.from_user.username
    ensure_user(user_id, username)

    mode = get_mode(user_id)

    if mode == "post":
        await run_generation(message, message.text, "Написание поста")
        clear_mode(user_id)
        return

    if mode == "ideas":
        await run_generation(message, message.text, "Генерация идей постов")
        clear_mode(user_id)
        return

    if mode == "rewrite":
        await run_generation(message, message.text, "Переписывание и улучшение текста")
        clear_mode(user_id)
        return

    if mode == "plan":
        await run_generation(message, message.text, "Создание контент-плана")
        clear_mode(user_id)
        return

    await run_generation(message, message.text, "Свободный запрос")


# =========================
# HTTP: return + webhook
# =========================

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

    if not payment_id:
        return web.Response(status=400, text="missing payment id")

    payment_row = get_payment(payment_id)
    if not payment_row:
        logger.warning("Платёж %s не найден в БД", payment_id)
        return web.Response(status=200, text="ok")

    if payment_row["status"] == "succeeded":
        return web.Response(status=200, text="ok")

    if event == "payment.succeeded" and status == "succeeded":
        update_payment_status(payment_id, "succeeded", payload, paid=True)
        await apply_successful_payment(payment_row)
        return web.Response(status=200, text="ok")

    if event == "payment.canceled" or status == "canceled":
        update_payment_status(payment_id, "canceled", payload, paid=False)

        try:
            await bot.send_message(
                payment_row["telegram_id"],
                "Оплата не завершилась. Можно попробовать ещё раз в любой момент.",
                reply_markup=get_main_menu(),
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
# Main
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