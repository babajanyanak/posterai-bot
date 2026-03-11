import os
import asyncio
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton

from openai import OpenAI

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not TELEGRAM_BOT_TOKEN:
    raise ValueError("Не найден TELEGRAM_BOT_TOKEN в .env")

if not OPENAI_API_KEY:
    raise ValueError("Не найден OPENAI_API_KEY в .env")

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()
client = OpenAI(api_key=OPENAI_API_KEY)

FREE_LIMIT = 10
users = {}
user_modes = {}

MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="✍️ Сгенерировать пост")],
        [KeyboardButton(text="💡 Идеи постов")],
        [KeyboardButton(text="♻️ Переписать текст")],
        [KeyboardButton(text="📊 Остаток генераций")],
    ],
    resize_keyboard=True
)


def get_balance(user_id: int) -> int:
    if user_id not in users:
        users[user_id] = FREE_LIMIT
    return users[user_id]


def decrease_balance(user_id: int) -> None:
    users[user_id] = get_balance(user_id) - 1


def set_mode(user_id: int, mode: str) -> None:
    user_modes[user_id] = mode


def get_mode(user_id: int) -> str:
    return user_modes.get(user_id, "generate")


def generate_post_sync(topic: str) -> str:
    response = client.responses.create(
        model="gpt-4o-mini",
        input=(
            "Ты редактор Telegram-каналов.\n"
            "Напиши готовый пост на русском языке.\n"
            "Требования:\n"
            "- 700–900 символов\n"
            "- живой, понятный язык\n"
            "- хорошая структура\n"
            "- в конце добавь 3 варианта заголовка\n\n"
            f"Тема: {topic}"
        ),
    )
    return response.output_text.strip()


def generate_ideas_sync(topic: str) -> str:
    response = client.responses.create(
        model="gpt-4o-mini",
        input=(
            "Ты контент-стратег для Telegram-каналов.\n"
            "Сгенерируй 10 идей постов на русском языке.\n"
            "Требования:\n"
            "- список должен быть конкретным\n"
            "- каждая идея в 1 строку\n"
            "- без воды\n\n"
            f"Тематика канала: {topic}"
        ),
    )
    return response.output_text.strip()


def rewrite_text_sync(text: str) -> str:
    response = client.responses.create(
        model="gpt-4o-mini",
        input=(
            "Ты редактор постов для Telegram.\n"
            "Перепиши текст на русском языке так, чтобы он:\n"
            "- читался легче\n"
            "- был более структурированным\n"
            "- звучал живо и современно\n"
            "- сохранил исходный смысл\n\n"
            f"Текст:\n{text}"
        ),
    )
    return response.output_text.strip()


@dp.message(Command("start"))
async def start(message: Message):
    user_id = message.from_user.id
    get_balance(user_id)
    set_mode(user_id, "generate")

    await message.answer(
        "Привет 👋\n\n"
        "Я PosterAI — AI-редактор постов для Telegram.\n\n"
        "Что я умею:\n"
        "✍️ генерировать посты\n"
        "💡 придумывать идеи\n"
        "♻️ переписывать тексты\n\n"
        f"У тебя есть {FREE_LIMIT} бесплатных генераций.\n"
        "Выбери действие кнопкой ниже.",
        reply_markup=MENU
    )


@dp.message(Command("help"))
async def help_command(message: Message):
    await message.answer(
        "Как пользоваться:\n\n"
        "1. Нажми кнопку действия\n"
        "2. Отправь тему или текст\n"
        "3. Получи результат\n\n"
        "Команды:\n"
        "/start — запустить бота\n"
        "/help — помощь\n"
        "/balance — остаток генераций",
        reply_markup=MENU
    )


@dp.message(Command("balance"))
async def balance_command(message: Message):
    balance = get_balance(message.from_user.id)
    await message.answer(f"Осталось генераций: {balance}", reply_markup=MENU)


@dp.message(F.text == "📊 Остаток генераций")
async def balance_button(message: Message):
    balance = get_balance(message.from_user.id)
    await message.answer(f"Осталось генераций: {balance}", reply_markup=MENU)


@dp.message(F.text == "✍️ Сгенерировать пост")
async def generate_mode(message: Message):
    set_mode(message.from_user.id, "generate")
    await message.answer(
        "Отправь тему поста.\n\nПример:\nКак выбрать нишу для Telegram-канала",
        reply_markup=MENU
    )


@dp.message(F.text == "💡 Идеи постов")
async def ideas_mode(message: Message):
    set_mode(message.from_user.id, "ideas")
    await message.answer(
        "Отправь тематику канала.\n\nПример:\nНедвижимость, инвестиции, маркетинг",
        reply_markup=MENU
    )


@dp.message(F.text == "♻️ Переписать текст")
async def rewrite_mode(message: Message):
    set_mode(message.from_user.id, "rewrite")
    await message.answer(
        "Отправь текст, который нужно переписать.",
        reply_markup=MENU
    )


@dp.message(F.text)
async def text_handler(message: Message):
    user_id = message.from_user.id
    balance = get_balance(user_id)

    if balance <= 0:
        await message.answer(
            "Бесплатные генерации закончились.\nНапиши владельцу бота для доступа к полной версии.",
            reply_markup=MENU
        )
        return

    mode = get_mode(user_id)
    wait_msg = await message.answer("Генерирую...", reply_markup=MENU)

    try:
        if mode == "generate":
            result = await asyncio.to_thread(generate_post_sync, message.text)
        elif mode == "ideas":
            result = await asyncio.to_thread(generate_ideas_sync, message.text)
        elif mode == "rewrite":
            result = await asyncio.to_thread(rewrite_text_sync, message.text)
        else:
            result = await asyncio.to_thread(generate_post_sync, message.text)

        decrease_balance(user_id)

        await wait_msg.edit_text("Готово ✅")
        await message.answer(result, reply_markup=MENU)
        await message.answer(
            f"Осталось генераций: {get_balance(user_id)}",
            reply_markup=MENU
        )

    except Exception as e:
        print("[ERROR]", repr(e))
        await wait_msg.edit_text("Ошибка генерации ❌")
        await message.answer(f"Текст ошибки: {repr(e)}", reply_markup=MENU)


async def main():
    print("[DEBUG] Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())