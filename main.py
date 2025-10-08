
from aiogram import Bot, Dispatcher, types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.utils import executor
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
import asyncio
import logging
import os

TOKEN = os.getenv("BOT_TOKEN")
bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())
logging.basicConfig(level=logging.INFO)

class Form(StatesGroup):
    name = State()
    goal = State()

@dp.message(commands="start")
@dp.message(commands="menu")
async def start(message: types.Message, state: FSMContext):
    kb = [
        [KeyboardButton(text="🔍 Найти рядом")],
        [KeyboardButton(text="📝 Моя анкета")],
        [KeyboardButton(text="⚙️ Настройки")],
        [KeyboardButton(text="💖 Помочь проекту")]
    ]
    await message.answer("Меню:", reply_markup=ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True))

@dp.message(lambda msg: msg.text == "📝 Моя анкета")
async def start_form(message: types.Message, state: FSMContext):
    await message.answer("Введите своё имя:")
    await state.set_state(Form.name)

@dp.message(Form.name)
async def process_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text)
    await message.answer("Выберите цель знакомств:")
    await state.set_state(Form.goal)

@dp.message(Form.goal)
async def process_goal(message: types.Message, state: FSMContext):
    data = await state.get_data()
    name = data.get("name")
    goal = message.text
    await message.answer(f"Анкета сохранена ✅\nИмя: {name}\nЦель: {goal}")
    await state.clear()

if __name__ == "__main__":
    dp.run_polling(bot)
