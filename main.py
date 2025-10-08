
from aiogram import Bot, Dispatcher, F, Router, types
from aiogram.filters import Command
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
import asyncio
import logging
import os

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("Environment variable BOT_TOKEN is not set")

bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)
logging.basicConfig(level=logging.INFO)

class Form(StatesGroup):
    name = State()
    goal = State()

@router.message(Command(commands=["start", "menu"]))
async def start(message: types.Message, state: FSMContext):
    kb = [
        [KeyboardButton(text="🔍 Найти рядом")],
        [KeyboardButton(text="📝 Моя анкета")],
        [KeyboardButton(text="⚙️ Настройки")],
        [KeyboardButton(text="💖 Помочь проекту")]
    ]
    await message.answer("Меню:", reply_markup=ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True))

@router.message(F.text == "📝 Моя анкета")
async def start_form(message: types.Message, state: FSMContext):
    await message.answer("Введите своё имя:")
    await state.set_state(Form.name)

@router.message(Form.name)
async def process_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text)
    await message.answer("Выберите цель знакомств:")
    await state.set_state(Form.goal)

@router.message(Form.goal)
async def process_goal(message: types.Message, state: FSMContext):
    data = await state.get_data()
    name = data.get("name")
    goal = message.text
    await message.answer(f"Анкета сохранена ✅\nИмя: {name}\nЦель: {goal}")
    await state.clear()

async def main() -> None:
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
