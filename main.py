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
UNLIMITED_USERNAME = "babajanyanak"


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
                    history_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                    style_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                    style_profile TEXT,
                    last_generation TEXT,
                    last_task_type TEXT,
                    last_input TEXT,
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


def is_unlimited_user(username: Optional[str]) -> bool:
    return (username or "").lower() == UNLIMITED_USERNAME.lower()


def get_user_row(user_id: int) -> dict:
    with get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM users WHERE telegram_id = %s",
                (user_id,),
            )
            row = cur.fetchone()
            if not row:
                raise ValueError(f"Пользователь {user_id} не найден")
            return row


def get_balance(user_id: int, username: Optional[str]) -> str:
    if is_unlimited_user(username):
        return "∞"
    row = get_user_row(user_id)
    return str(row["balance"])


def has_balance(user_id: int, username: Optional[str]) -> bool:
    if is_unlimited_user(username):
        return True
    row = get_user_row(user_id)
    return row["balance"] > 0


def decrease_balance(user_id: int, username: Optional[str]):
    if is_unlimited_user(username):
        return

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


def toggle_history(user_id: int) -> bool:
    with get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                UPDATE users
                SET history_enabled = NOT history_enabled,
                    updated_at = NOW()
                WHERE telegram_id = %s
                RETURNING history_enabled
                """,
                (user_id,),
            )
            row = cur.fetchone()
            conn.commit()
            return row["history_enabled"]


def toggle_style(user_id: int) -> bool:
    with get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                UPDATE users
                SET style_enabled = NOT style_enabled,
                    updated_at = NOW()
                WHERE telegram_id = %s
                RETURNING style_enabled
                """,
                (user_id,),
            )
            row = cur.fetchone()
            conn.commit()
            return row["style_enabled"]


def add_history(user_id: int, text: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO user_history (telegram_id, text) VALUES (%s, %s)",
                (user_id, text),
            )
            cur.execute(
                """
                DELETE FROM user_history
                WHERE id IN (
                    SELECT id
                    FROM user_history
                    WHERE telegram_id = %s
                    ORDER BY created_at DESC
                    OFFSET 10
                )
                """,
                (user_id,),
            )
            conn.commit()


def get_history(user_id: int) -> List[str]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT text
                FROM user_history
                WHERE telegram_id = %s
                ORDER BY created_at DESC
                LIMIT 10
                """,
                (user_id,),
            )
            rows = cur.fetchall()
            return [r[0] for r in reversed(rows)]


def save_style_profile(user_id: int, profile: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE users
                SET style_profile = %s,
                    style_enabled = TRUE,
                    updated_at = NOW()
                WHERE telegram_id = %s
                """,
                (profile, user_id),
            )
            conn.commit()


def save_last_result(user_id: int, task_type: str, original_input: str, result: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE users
                SET last_task_type = %s,
                    last_input = %s,
                    last_generation = %s,
                    updated_at = NOW()
                WHERE telegram_id = %s
                """,
                (task_type, original_input, result, user_id),
            )
            conn.commit()


# =========================
# Runtime state (MVP)
# =========================
user_modes = {}
pending_custom_improve = set()
pending_style_input = set()


def set_mode(user_id: int, mode: str):
    user_modes[user_id] = mode


def get_mode(user_id: int) -> str:
    return user_modes.get(user_id, "generate")


# =========================
# UI
# =========================
def get_main_menu(user_id: int) -> ReplyKeyboardMarkup:
    row = get_user_row(user_id)

    history_text = "🧠 История: Вкл" if row["history_enabled"] else "🧠 История: Выкл"
    style_text = (
        "🎨 Стиль: Вкл" if row["style_enabled"] and row["style_profile"]
        else "🎨 Стиль: Выкл" if row["style_profile"]
        else "🎨 Подключить канал"
    )

    keyboard = [
        [KeyboardButton(text="✍️ Сгенерировать пост"), KeyboardButton(text="💡 Идеи постов")],
        [KeyboardButton(text="♻️ Переписать текст"), KeyboardButton(text="📅 Контент-план")],
        [KeyboardButton(text=style_text), KeyboardButton(text=history_text)],
        [KeyboardButton(text="📊 Остаток генераций")],
    ]
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)


def get_result_actions(task_type: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="👍 Подходит", callback_data="result:ok"),
                InlineKeyboardButton(text="✨ Улучшить", callback_data="result:improve"),
                InlineKeyboardButton(text="🔁 Новый вариант", callback_data=f"result:regen:{task_type}"),
            ]
        ]
    )


def get_improve_actions() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✂️ Короче", callback_data="improve:shorter"),
                InlineKeyboardButton(text="🔥 Сильнее", callback_data="improve:stronger"),
            ],
            [
                InlineKeyboardButton(text="📚 Экспертнее", callback_data="improve:expert"),
                InlineKeyboardButton(text="💰 Продающе", callback_data="improve:sales"),
            ],
            [
                InlineKeyboardButton(text="⚡ Усилить начало", callback_data="improve:hook"),
                InlineKeyboardButton(text="📝 Свой комментарий", callback_data="improve:custom"),
            ],
        ]
    )


# =========================
# Prompt helpers
# =========================
def build_history_context(user_id: int) -> str:
    row = get_user_row(user_id)
    if not row["history_enabled"]:
        return ""

    history = get_history(user_id)
    if not history:
        return ""

    numbered = "\n".join([f"{i+1}. {item}" for i, item in enumerate(history)])
    return (
        "Учитывай контекст предыдущих запросов пользователя, "
        "но только если это делает ответ лучше:\n"
        f"{numbered}\n\n"
    )


def build_style_context(user_id: int) -> str:
    row = get_user_row(user_id)
    if not row["style_enabled"] or not row["style_profile"]:
        return ""
    return f"Пиши с учётом сохранённого стиля канала:\n{row['style_profile']}\n\n"


# =========================
# OpenAI
# =========================
def generate_post_sync(topic: str, user_id: int) -> str:
    print(f"[DEBUG] generate_post_sync started | topic={topic}")

    prompt = (
        "Ты профессиональный редактор Telegram-каналов.\n"
        "Пиши естественно, без клише, сухости и канцелярита.\n\n"
        f"{build_style_context(user_id)}"
        f"{build_history_context(user_id)}"
        f"Тема: {topic}\n\n"
        "Задача:\n"
        "- напиши пост на русском\n"
        "- 700–900 символов\n"
        "- короткие абзацы\n"
        "- хорошая структура\n"
        "- в конце добавь 3 заголовка\n"
    )
    response = client.responses.create(model=MODEL_NAME, input=prompt)
    print("[DEBUG] generate_post_sync finished")
    return response.output_text.strip()


def generate_ideas_sync(topic: str, user_id: int) -> str:
    print(f"[DEBUG] generate_ideas_sync started | topic={topic}")

    prompt = (
        "Ты контент-стратег Telegram-каналов.\n\n"
        f"{build_style_context(user_id)}"
        f"{build_history_context(user_id)}"
        f"Тематика канала: {topic}\n\n"
        "Сгенерируй 10 конкретных идей постов.\n"
        "- каждая идея в 1 строку\n"
        "- без воды\n"
        "- на русском\n"
    )
    response = client.responses.create(model=MODEL_NAME, input=prompt)
    print("[DEBUG] generate_ideas_sync finished")
    return response.output_text.strip()


def rewrite_text_sync(text: str, user_id: int) -> str:
    print("[DEBUG] rewrite_text_sync started")

    prompt = (
        "Ты редактор Telegram-постов.\n\n"
        f"{build_style_context(user_id)}"
        f"{build_history_context(user_id)}"
        "Перепиши текст так, чтобы он был:\n"
        "- легче\n"
        "- живее\n"
        "- лучше структурирован\n"
        "- с сохранением смысла\n\n"
        f"Текст:\n{text}"
    )
    response = client.responses.create(model=MODEL_NAME, input=prompt)
    print("[DEBUG] rewrite_text_sync finished")
    return response.output_text.strip()


def generate_content_plan_sync(topic: str, user_id: int) -> str:
    print(f"[DEBUG] generate_content_plan_sync started | topic={topic}")

    prompt = (
        "Ты контент-стратег Telegram-каналов.\n\n"
        f"{build_style_context(user_id)}"
        f"{build_history_context(user_id)}"
        f"Тема канала: {topic}\n\n"
        "Сделай контент-план на 30 постов.\n"
        "- 30 строк\n"
        "- конкретно\n"
        "- без воды\n"
        "- на русском\n"
    )
    response = client.responses.create(model=MODEL_NAME, input=prompt)
    print("[DEBUG] generate_content_plan_sync finished")
    return response.output_text.strip()


def analyze_style_sync(samples: str) -> str:
    print("[DEBUG] analyze_style_sync started")

    prompt = (
        "Проанализируй стиль Telegram-постов и сделай краткий профиль.\n"
        "Определи:\n"
        "- Tone of Voice\n"
        "- длину постов\n"
        "- использование эмодзи\n"
        "- структуру\n"
        "- тип подачи\n"
        "- что важно сохранить\n\n"
        f"Тексты:\n{samples}"
    )
    response = client.responses.create(model=MODEL_NAME, input=prompt)
    print("[DEBUG] analyze_style_sync finished")
    return response.output_text.strip()


def improve_last_text_sync(last_text: str, improve_type: str, user_id: int) -> str:
    print(f"[DEBUG] improve_last_text_sync started | improve_type={improve_type}")

    instructions = {
        "shorter": "Сделай текст короче и плотнее.",
        "stronger": "Сделай текст сильнее и убедительнее.",
        "expert": "Сделай текст более экспертным.",
        "sales": "Сделай текст более продающим без дешёвых манипуляций.",
        "hook": "Сделай начало заметно сильнее и цепляюще.",
    }
    prompt = (
        "Ты редактор Telegram-каналов.\n\n"
        f"{build_style_context(user_id)}"
        f"Задача: {instructions.get(improve_type, 'Улучши текст.')}\n\n"
        f"Текст:\n{last_text}"
    )
    response = client.responses.create(model=MODEL_NAME, input=prompt)
    print("[DEBUG] improve_last_text_sync finished")
    return response.output_text.strip()


def improve_with_custom_comment_sync(last_text: str, comment: str, user_id: int) -> str:
    print("[DEBUG] improve_with_custom_comment_sync started")

    prompt = (
        "Ты редактор Telegram-каналов.\n\n"
        f"{build_style_context(user_id)}"
        "Улучши текст по комментарию пользователя.\n\n"
        f"Комментарий:\n{comment}\n\n"
        f"Текущий текст:\n{last_text}"
    )
    response = client.responses.create(model=MODEL_NAME, input=prompt)
    print("[DEBUG] improve_with_custom_comment_sync finished")
    return response.output_text.strip()


# =========================
# Shared generation
# =========================
async def run_generation_task(message: Message, task_type: str, original_input: str):
    user_id = message.from_user.id
    username = message.from_user.username

    ensure_user(user_id, username)

    if not has_balance(user_id, username):
        await message.answer(
            "Похоже, бесплатные генерации закончились ✨\n\n"
            "Если хочешь продолжить, напиши владельцу бота — поможем открыть полный доступ.",
            reply_markup=get_main_menu(user_id),
        )
        return

    add_history(user_id, original_input)
    wait_msg = await message.answer("Секунду, собираю для тебя лучший вариант ✍️")

    print(f"[DEBUG] run_generation_task started | task_type={task_type} | user_id={user_id}")

    try:
        if task_type == "generate":
            print("[DEBUG] calling generate_post_sync")
            result = await asyncio.wait_for(
                asyncio.to_thread(generate_post_sync, original_input, user_id),
                timeout=60
            )
        elif task_type == "ideas":
            print("[DEBUG] calling generate_ideas_sync")
            result = await asyncio.wait_for(
                asyncio.to_thread(generate_ideas_sync, original_input, user_id),
                timeout=60
            )
        elif task_type == "rewrite":
            print("[DEBUG] calling rewrite_text_sync")
            result = await asyncio.wait_for(
                asyncio.to_thread(rewrite_text_sync, original_input, user_id),
                timeout=60
            )
        elif task_type == "content_plan":
            print("[DEBUG] calling generate_content_plan_sync")
            result = await asyncio.wait_for(
                asyncio.to_thread(generate_content_plan_sync, original_input, user_id),
                timeout=60
            )
        else:
            print("[DEBUG] calling generate_post_sync (fallback)")
            result = await asyncio.wait_for(
                asyncio.to_thread(generate_post_sync, original_input, user_id),
                timeout=60
            )

        print("[DEBUG] OpenAI result received")

        save_last_result(user_id, task_type, original_input, result)
        decrease_balance(user_id, username)

        try:
            await wait_msg.edit_text("Готово ✅")
        except Exception as edit_err:
            print("[WARN] wait_msg edit failed:", repr(edit_err))
            await message.answer("Готово ✅")

        await message.answer(
            "Подготовил вариант ниже 👇\n\n"
            "Посмотри и, если захочешь, сразу улучшим его в пару нажатий.",
            reply_markup=get_main_menu(user_id),
        )
        await message.answer(result)
        await message.answer(
            f"Осталось генераций: {get_balance(user_id, username)}",
            reply_markup=get_result_actions(task_type),
        )
        await message.answer(
            "Главное меню снова под рукой 👌",
            reply_markup=get_main_menu(user_id),
        )

    except asyncio.TimeoutError:
        print("[ERROR] OpenAI request timeout")
        await message.answer(
            "Сейчас ответ собирается дольше обычного ⏳\n\n"
            "Попробуй ещё раз через минуту — я всё подхвачу.",
            reply_markup=get_main_menu(user_id),
        )

    except Exception as e:
        print("[ERROR]", repr(e))
        await message.answer(
            "Поймал техническую заминку ⚠️\n\n"
            "Попробуй повторить запрос ещё раз чуть позже.",
            reply_markup=get_main_menu(user_id),
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
        "Я PosterAI — твой AI-редактор для Telegram-каналов.\n\n"
        "Помогаю быстро собирать сильный контент:\n"
        "✍️ пишу посты\n"
        "💡 предлагаю идеи\n"
        "♻️ улучшаю тексты\n"
        "📅 собираю контент-план\n\n"
        "Выбирай, с чего начнём:",
        reply_markup=get_main_menu(user_id),
    )


@dp.message(Command("help"))
async def help_command(message: Message):
    user_id = message.from_user.id
    username = message.from_user.username
    ensure_user(user_id, username)

    await message.answer(
        "Как здесь всё устроено 👌\n\n"
        "1. Выбираешь нужный сценарий\n"
        "2. Отправляешь тему или текст\n"
        "3. Получаешь результат\n"
        "4. При желании сразу улучшаешь его кнопками\n\n"
        "Я могу работать и как быстрый генератор, и как аккуратный редактор под твой стиль.",
        reply_markup=get_main_menu(user_id),
    )


@dp.message(Command("balance"))
async def balance_command(message: Message):
    user_id = message.from_user.id
    username = message.from_user.username
    ensure_user(user_id, username)

    await message.answer(
        f"Сейчас у тебя доступно генераций: {get_balance(user_id, username)} ✨",
        reply_markup=get_main_menu(user_id),
    )


# =========================
# Menu buttons
# =========================
@dp.message(F.text == "✍️ Сгенерировать пост")
async def generate_mode(message: Message):
    user_id = message.from_user.id
    set_mode(user_id, "generate")
    await message.answer(
        "Отлично, давай соберём новый пост ✍️\n\n"
        "Пришли тему, и я подготовлю черновик.",
        reply_markup=get_main_menu(user_id),
    )


@dp.message(F.text == "💡 Идеи постов")
async def ideas_mode(message: Message):
    user_id = message.from_user.id
    set_mode(user_id, "ideas")
    await message.answer(
        "Супер, поищем сильные идеи 💡\n\n"
        "Напиши тематику канала, и я предложу варианты.",
        reply_markup=get_main_menu(user_id),
    )


@dp.message(F.text == "♻️ Переписать текст")
async def rewrite_mode(message: Message):
    user_id = message.from_user.id
    set_mode(user_id, "rewrite")
    await message.answer(
        "Присылай текст — помогу сделать его сильнее, чище и удобнее для чтения ♻️",
        reply_markup=get_main_menu(user_id),
    )


@dp.message(F.text == "📅 Контент-план")
async def plan_mode(message: Message):
    user_id = message.from_user.id
    set_mode(user_id, "content_plan")
    await message.answer(
        "Соберём контент-план 📅\n\n"
        "Напиши тему канала, и я предложу структуру публикаций.",
        reply_markup=get_main_menu(user_id),
    )


@dp.message(F.text == "📊 Остаток генераций")
async def balance_button(message: Message):
    user_id = message.from_user.id
    username = message.from_user.username
    ensure_user(user_id, username)
    await message.answer(
        f"У тебя сейчас доступно генераций: {get_balance(user_id, username)} ✨",
        reply_markup=get_main_menu(user_id),
    )


@dp.message(F.text.in_(["🧠 История: Вкл", "🧠 История: Выкл"]))
async def toggle_history_handler(message: Message):
    user_id = message.from_user.id
    username = message.from_user.username
    ensure_user(user_id, username)

    enabled = toggle_history(user_id)
    text = (
        "Историю включил ✅\n\n"
        "Теперь буду учитывать последние 10 запросов, чтобы ответы были точнее и ближе к твоим задачам."
        if enabled else
        "Историю выключил ✅\n\n"
        "Следующие ответы буду строить только от текущего запроса — без опоры на прошлый контекст."
    )
    await message.answer(text, reply_markup=get_main_menu(user_id))


@dp.message(F.text == "🎨 Подключить канал")
async def connect_style(message: Message):
    user_id = message.from_user.id
    username = message.from_user.username
    ensure_user(user_id, username)

    pending_style_input.add(user_id)
    await message.answer(
        "Давай зафиксируем стиль канала 🎨\n\n"
        "Пришли 3–5 примеров постов одним сообщением, и я подстрою тон, подачу и структуру под них.",
        reply_markup=get_main_menu(user_id),
    )


@dp.message(F.text.in_(["🎨 Стиль: Вкл", "🎨 Стиль: Выкл"]))
async def toggle_style_handler(message: Message):
    user_id = message.from_user.id
    username = message.from_user.username
    ensure_user(user_id, username)

    row = get_user_row(user_id)
    if not row["style_profile"]:
        pending_style_input.add(user_id)
        await message.answer(
            "Сначала нужно собрать профиль стиля ✨\n\n"
            "Пришли 3–5 примеров постов канала, и я сохраню ориентиры.",
            reply_markup=get_main_menu(user_id),
        )
        return

    enabled = toggle_style(user_id)
    text = (
        "Стиль канала включил ✅\n\n"
        "В следующих ответах буду ориентироваться на сохранённый tone of voice и формат."
        if enabled else
        "Стиль канала выключил ✅\n\n"
        "Следующие тексты подготовлю без опоры на сохранённый стиль."
    )
    await message.answer(text, reply_markup=get_main_menu(user_id))


# =========================
# Result callbacks
# =========================
@dp.callback_query(F.data == "result:ok")
async def result_ok(callback: CallbackQuery):
    await callback.answer("Отлично 👌")
    await callback.message.answer(
        "Рад был помочь 🙌\n\n"
        "Зафиксировал твои предпочтения и постараюсь учитывать их в следующих запросах.",
        reply_markup=get_main_menu(callback.from_user.id),
    )


@dp.callback_query(F.data == "result:improve")
async def result_improve(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer(
        "Давай докрутим результат ✨\n\n"
        "Выбери, как именно хочешь его улучшить:",
        reply_markup=get_improve_actions(),
    )


@dp.callback_query(F.data.startswith("result:regen:"))
async def result_regen(callback: CallbackQuery):
    await callback.answer("Собираю ещё один вариант ✍️")
    user_id = callback.from_user.id
    username = callback.from_user.username
    ensure_user(user_id, username)

    row = get_user_row(user_id)
    if not row["last_task_type"] or not row["last_input"]:
        await callback.message.answer(
            "Не нашёл предыдущий запрос, на основе которого можно собрать новый вариант.",
            reply_markup=get_main_menu(user_id),
        )
        return

    if not has_balance(user_id, username):
        await callback.message.answer(
            "Похоже, генерации закончились ✨\n\n"
            "Если хочешь продолжить, напиши владельцу бота.",
            reply_markup=get_main_menu(user_id),
        )
        return

    await callback.message.answer("Секунду, собираю альтернативную версию...")

    try:
        if row["last_task_type"] == "generate":
            result = await asyncio.wait_for(
                asyncio.to_thread(generate_post_sync, row["last_input"], user_id),
                timeout=60
            )
        elif row["last_task_type"] == "ideas":
            result = await asyncio.wait_for(
                asyncio.to_thread(generate_ideas_sync, row["last_input"], user_id),
                timeout=60
            )
        elif row["last_task_type"] == "rewrite":
            result = await asyncio.wait_for(
                asyncio.to_thread(rewrite_text_sync, row["last_input"], user_id),
                timeout=60
            )
        elif row["last_task_type"] == "content_plan":
            result = await asyncio.wait_for(
                asyncio.to_thread(generate_content_plan_sync, row["last_input"], user_id),
                timeout=60
            )
        else:
            result = await asyncio.wait_for(
                asyncio.to_thread(generate_post_sync, row["last_input"], user_id),
                timeout=60
            )

        save_last_result(user_id, row["last_task_type"], row["last_input"], result)
        decrease_balance(user_id, username)

        await callback.message.answer(
            "Готово — вот ещё один вариант 👇",
            reply_markup=get_main_menu(user_id),
        )
        await callback.message.answer(result)
        await callback.message.answer(
            f"Осталось генераций: {get_balance(user_id, username)}",
            reply_markup=get_result_actions(row["last_task_type"]),
        )
        await callback.message.answer(
            "Главное меню снова доступно 👌",
            reply_markup=get_main_menu(user_id),
        )

    except asyncio.TimeoutError:
        print("[ERROR] Regenerate timeout")
        await callback.message.answer(
            "Сейчас ответ собирается дольше обычного ⏳\n\n"
            "Попробуй ещё раз через минуту.",
            reply_markup=get_main_menu(user_id),
        )

    except Exception as e:
        print("[ERROR]", repr(e))
        await callback.message.answer(
            "Поймал техническую заминку ⚠️\n\n"
            "Попробуй повторить действие чуть позже.",
            reply_markup=get_main_menu(user_id),
        )


@dp.callback_query(F.data.startswith("improve:"))
async def improve_handler(callback: CallbackQuery):
    user_id = callback.from_user.id
    username = callback.from_user.username
    ensure_user(user_id, username)

    row = get_user_row(user_id)
    if not row["last_generation"]:
        await callback.answer("Сначала нужен текст для доработки", show_alert=True)
        return

    improve_type = callback.data.split(":")[1]

    if improve_type == "custom":
        pending_custom_improve.add(user_id)
        await callback.answer()
        await callback.message.answer(
            "Напиши, что именно хочешь изменить 📝\n\n"
            "Чем точнее комментарий, тем лучше я подстрою результат.",
            reply_markup=get_main_menu(user_id),
        )
        return

    if not has_balance(user_id, username):
        await callback.answer("Генерации закончились", show_alert=True)
        return

    await callback.answer("Докручиваю ✨")
    await callback.message.answer("Секунду, улучшаю текст под твой запрос...")

    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(
                improve_last_text_sync,
                row["last_generation"],
                improve_type,
                user_id,
            ),
            timeout=60
        )

        save_last_result(user_id, row["last_task_type"], row["last_input"], result)
        decrease_balance(user_id, username)

        await callback.message.answer(
            "Готово — обновил версию 👇",
            reply_markup=get_main_menu(user_id),
        )
        await callback.message.answer(result)
        await callback.message.answer(
            f"Осталось генераций: {get_balance(user_id, username)}",
            reply_markup=get_result_actions(row["last_task_type"] or "generate"),
        )
        await callback.message.answer(
            "Главное меню снова доступно 👌",
            reply_markup=get_main_menu(user_id),
        )

    except asyncio.TimeoutError:
        print("[ERROR] Improve timeout")
        await callback.message.answer(
            "Сейчас улучшение занимает больше времени, чем обычно ⏳\n\n"
            "Попробуй ещё раз через минуту.",
            reply_markup=get_main_menu(user_id),
        )

    except Exception as e:
        print("[ERROR]", repr(e))
        await callback.message.answer(
            "Поймал техническую заминку ⚠️\n\n"
            "Попробуй ещё раз чуть позже.",
            reply_markup=get_main_menu(user_id),
        )


# =========================
# Text handler
# =========================
@dp.message(F.text)
async def text_handler(message: Message):
    user_id = message.from_user.id
    username = message.from_user.username
    ensure_user(user_id, username)
    text = message.text.strip()

    if user_id in pending_custom_improve:
        pending_custom_improve.discard(user_id)

        row = get_user_row(user_id)
        if not row["last_generation"]:
            await message.answer(
                "Не нашёл предыдущий текст для доработки.\n\n"
                "Давай сначала сгенерируем или улучшим материал.",
                reply_markup=get_main_menu(user_id),
            )
            return

        if not has_balance(user_id, username):
            await message.answer(
                "Похоже, генерации закончились ✨\n\n"
                "Если хочешь продолжить, напиши владельцу бота.",
                reply_markup=get_main_menu(user_id),
            )
            return

        await message.answer("Принял комментарий, адаптирую текст под него ✍️")

        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    improve_with_custom_comment_sync,
                    row["last_generation"],
                    text,
                    user_id,
                ),
                timeout=60
            )

            save_last_result(user_id, row["last_task_type"], row["last_input"], result)
            decrease_balance(user_id, username)

            await message.answer(
                "Готово — учёл твой комментарий 👇",
                reply_markup=get_main_menu(user_id),
            )
            await message.answer(result)
            await message.answer(
                f"Осталось генераций: {get_balance(user_id, username)}",
                reply_markup=get_result_actions(row["last_task_type"] or "generate"),
            )
            await message.answer(
                "Главное меню снова под рукой 👌",
                reply_markup=get_main_menu(user_id),
            )
            return

        except asyncio.TimeoutError:
            print("[ERROR] Custom improve timeout")
            await message.answer(
                "Сейчас ответ собирается дольше обычного ⏳\n\n"
                "Попробуй ещё раз через минуту.",
                reply_markup=get_main_menu(user_id),
            )
            return

        except Exception as e:
            print("[ERROR]", repr(e))
            await message.answer(
                "Поймал техническую заминку ⚠️\n\n"
                "Попробуй повторить действие чуть позже.",
                reply_markup=get_main_menu(user_id),
            )
            return

    if user_id in pending_style_input:
        await message.answer("Смотрю примеры и собираю профиль стиля 🎨")

        try:
            profile = await asyncio.wait_for(
                asyncio.to_thread(analyze_style_sync, text),
                timeout=60
            )
            save_style_profile(user_id, profile)
            pending_style_input.discard(user_id)

            await message.answer(
                "Готово — стиль зафиксировал ✅\n\n"
                "Теперь смогу лучше подстраивать подачу под твой канал.",
                reply_markup=get_main_menu(user_id),
            )
            await message.answer(f"Профиль стиля:\n\n{profile}")
            await message.answer(
                "Главное меню снова доступно 👌",
                reply_markup=get_main_menu(user_id),
            )
            return

        except asyncio.TimeoutError:
            print("[ERROR] Style analyze timeout")
            await message.answer(
                "Сейчас анализ стиля занимает больше времени, чем обычно ⏳\n\n"
                "Попробуй отправить примеры ещё раз через минуту.",
                reply_markup=get_main_menu(user_id),
            )
            return

        except Exception as e:
            print("[ERROR]", repr(e))
            await message.answer(
                "Не получилось обработать примеры стиля ⚠️\n\n"
                "Попробуй ещё раз чуть позже.",
                reply_markup=get_main_menu(user_id),
            )
            return

    mode = get_mode(user_id)

    if mode == "generate":
        await run_generation_task(message, "generate", text)
    elif mode == "ideas":
        await run_generation_task(message, "ideas", text)
    elif mode == "rewrite":
        await run_generation_task(message, "rewrite", text)
    elif mode == "content_plan":
        await run_generation_task(message, "content_plan", text)
    else:
        await run_generation_task(message, "generate", text)


# =========================
# Main
# =========================
async def main():
    init_db()
    print("[DEBUG] Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())