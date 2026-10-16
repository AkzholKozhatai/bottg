import asyncio
import logging
import os
import time
from datetime import datetime

import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from telethon import TelegramClient
from telethon.sessions import StringSession

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("telegram-scheduler")

BOT_TOKEN = os.environ["BOT_TOKEN"]
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
OWNER_ID = int(os.environ["OWNER_ID"])
TELETHON_SESSION = os.environ["TELETHON_SESSION"]
DB_PATH = os.getenv("DB_PATH", "tasks.sqlite3")
MIN_INTERVAL_SECONDS = 300  # минимум 5 минут

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
client = TelegramClient(StringSession(TELETHON_SESSION), API_ID, API_HASH)
db: aiosqlite.Connection | None = None


class CreateTask(StatesGroup):
    choosing_chat = State()
    entering_text = State()
    entering_interval = State()
    entering_count = State()
    confirming = State()


def owner_only(user_id: int | None) -> bool:
    return user_id == OWNER_ID


def main_menu():
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Создать задачу", callback_data="new_task")
    kb.button(text="📋 Мои задачи", callback_data="list_tasks")
    kb.adjust(1)
    return kb.as_markup()


async def init_db():
    global db
    db = await aiosqlite.connect(DB_PATH)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            chat_title TEXT NOT NULL,
            message_text TEXT NOT NULL,
            interval_seconds INTEGER NOT NULL,
            remaining INTEGER NOT NULL,
            next_run REAL NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            created_at REAL NOT NULL
        )
    """)
    await db.commit()


@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    if not owner_only(message.from_user.id if message.from_user else None):
        return
    await state.clear()
    await message.answer(
        "Пульт управления сообщениями готов.\n\n"
        "Создавай задачи только для чатов, где автоматические сообщения разрешены. "
        "Минимальный интервал — 5 минут; у каждой задачи ограниченное число отправок.",
        reply_markup=main_menu()
    )


@dp.message(Command("menu"))
async def menu(message: Message):
    if not owner_only(message.from_user.id if message.from_user else None):
        return
    await message.answer("Меню управления:", reply_markup=main_menu())


@dp.callback_query(F.data == "new_task")
async def new_task(callback: CallbackQuery, state: FSMContext):
    if not owner_only(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    dialogs = await client.get_dialogs(limit=100)
    kb = InlineKeyboardBuilder()
    added = 0
    for dialog in dialogs:
        entity = dialog.entity
        # Не показываем ботов и служебные сущности; доступны личные чаты и группы.
        if getattr(entity, "bot", False):
            continue
        title = (dialog.name or str(dialog.id)).replace("\n", " ")[:45]
        kb.button(text=title, callback_data=f"chat:{dialog.id}")
        added += 1
        if added >= 50:
            break
    if not added:
        await callback.message.answer("Не нашёл доступных чатов в аккаунте.")
        await callback.answer()
        return
    kb.adjust(1)
    await state.set_state(CreateTask.choosing_chat)
    await callback.message.answer("Выбери чат:", reply_markup=kb.as_markup())
    await callback.answer()


@dp.callback_query(CreateTask.choosing_chat, F.data.startswith("chat:"))
async def choose_chat(callback: CallbackQuery, state: FSMContext):
    if not owner_only(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    try:
        chat_id = int(callback.data.split(":", 1)[1])
        entity = await client.get_entity(chat_id)
        title = getattr(entity, "title", None) or getattr(entity, "first_name", None) or str(chat_id)
    except Exception as exc:
        log.exception("Cannot resolve chat")
        await callback.message.answer(f"Не удалось открыть этот чат: {exc}")
        await callback.answer()
        return
    await state.update_data(chat_id=chat_id, chat_title=title)
    await state.set_state(CreateTask.entering_text)
    await callback.message.answer("Отправь текст сообщения одним сообщением.")
    await callback.answer()


@dp.message(CreateTask.entering_text)
async def enter_text(message: Message, state: FSMContext):
    if not owner_only(message.from_user.id if message.from_user else None):
        return
    text = (message.text or "").strip()
    if not text:
        await message.answer("Нужен текстовый вариант сообщения. Попробуй ещё раз.")
        return
    if len(text) > 4000:
        await message.answer("Слишком длинный текст (максимум 4000 символов).")
        return
    await state.update_data(message_text=text)
    await state.set_state(CreateTask.entering_interval)
    await message.answer("Укажи интервал в минутах (минимум 5, например: 30).")


@dp.message(CreateTask.entering_interval)
async def enter_interval(message: Message, state: FSMContext):
    if not owner_only(message.from_user.id if message.from_user else None):
        return
    try:
        minutes = int((message.text or "").strip())
    except ValueError:
        await message.answer("Введи целое число минут, например 30.")
        return
    if minutes < MIN_INTERVAL_SECONDS // 60 or minutes > 10080:
        await message.answer("Интервал должен быть от 5 до 10080 минут (7 дней).")
        return
    await state.update_data(interval_seconds=minutes * 60)
    await state.set_state(CreateTask.entering_count)
    await message.answer("Сколько раз отправить сообщение? Введи число от 1 до 100.")


@dp.message(CreateTask.entering_count)
async def enter_count(message: Message, state: FSMContext):
    if not owner_only(message.from_user.id if message.from_user else None):
        return
    try:
        count = int((message.text or "").strip())
    except ValueError:
        await message.answer("Введи целое число от 1 до 100.")
        return
    if not 1 <= count <= 100:
        await message.answer("Количество должно быть от 1 до 100.")
        return
    await state.update_data(remaining=count)
    data = await state.get_data()
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Создать", callback_data="confirm_task")
    kb.button(text="❌ Отмена", callback_data="cancel_task")
    kb.adjust(2)
    await state.set_state(CreateTask.confirming)
    await message.answer(
        f"Проверь задачу:\n\n"
        f"Чат: {data['chat_title']}\n"
        f"Текст: {data['message_text']}\n"
        f"Интервал: {data['interval_seconds'] // 60} мин.\n"
        f"Количество: {count}\n\n"
        f"Первая отправка — через указанный интервал.",
        reply_markup=kb.as_markup()
    )


@dp.callback_query(F.data == "confirm_task")
async def confirm_task(callback: CallbackQuery, state: FSMContext):
    if not owner_only(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    if await state.get_state() != CreateTask.confirming.state:
        await callback.answer("Сначала создай задачу заново.", show_alert=True)
        return
    data = await state.get_data()
    now = time.time()
    await db.execute(
        """INSERT INTO tasks
        (chat_id, chat_title, message_text, interval_seconds, remaining, next_run, active, created_at)
        VALUES (?, ?, ?, ?, ?, ?, 1, ?)""",
        (data["chat_id"], data["chat_title"], data["message_text"],
         data["interval_seconds"], data["remaining"], now + data["interval_seconds"], now)
    )
    await db.commit()
    await state.clear()
    await callback.message.answer("Задача создана. Первая отправка будет после интервала.", reply_markup=main_menu())
    await callback.answer()


@dp.callback_query(F.data == "cancel_task")
async def cancel_task(callback: CallbackQuery, state: FSMContext):
    if not owner_only(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.clear()
    await callback.message.answer("Создание отменено.", reply_markup=main_menu())
    await callback.answer()


@dp.callback_query(F.data == "list_tasks")
async def list_tasks(callback: CallbackQuery):
    if not owner_only(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    cursor = await db.execute(
        "SELECT id, chat_title, message_text, interval_seconds, remaining, next_run, active FROM tasks ORDER BY id DESC LIMIT 30"
    )
    rows = await cursor.fetchall()
    await cursor.close()
    if not rows:
        await callback.message.answer("Задач пока нет.", reply_markup=main_menu())
        await callback.answer()
        return
    for task_id, title, text, interval, remaining, next_run, active in rows:
        status = "активна" if active else "остановлена/завершена"
        preview = text.replace("\n", " ")[:100]
        kb = InlineKeyboardBuilder()
        if active:
            kb.button(text="⏹ Остановить", callback_data=f"stop:{task_id}")
        kb.adjust(1)
        await callback.message.answer(
            f"Задача #{task_id} — {status}\n"
            f"Чат: {title}\n"
            f"Текст: {preview}\n"
            f"Интервал: {interval // 60} мин.\n"
            f"Осталось отправок: {remaining}\n"
            f"Следующая отправка: {datetime.fromtimestamp(next_run).strftime('%Y-%m-%d %H:%M:%S') if active else '—'}",
            reply_markup=kb.as_markup() if active else None
        )
    await callback.message.answer("Управление:", reply_markup=main_menu())
    await callback.answer()


@dp.callback_query(F.data.startswith("stop:"))
async def stop_task(callback: CallbackQuery):
    if not owner_only(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    try:
        task_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Неверный ID")
        return
    await db.execute("UPDATE tasks SET active=0 WHERE id=?", (task_id,))
    await db.commit()
    await callback.message.answer(f"Задача #{task_id} остановлена.")
    await callback.answer()


async def scheduler():
    while True:
        try:
            now = time.time()
            cursor = await db.execute(
                "SELECT id, chat_id, chat_title, message_text, interval_seconds, remaining "
                "FROM tasks WHERE active=1 AND next_run<=? ORDER BY next_run LIMIT 10",
                (now,)
            )
            rows = await cursor.fetchall()
            await cursor.close()
            for task_id, chat_id, title, message_text, interval, remaining in rows:
                # Re-check active status before sending, in case it was stopped.
                check = await db.execute("SELECT active, remaining FROM tasks WHERE id=?", (task_id,))
                current = await check.fetchone()
                await check.close()
                if not current or not current[0] or current[1] <= 0:
                    continue
                try:
                    await client.send_message(chat_id, message_text)
                    remaining -= 1
                    if remaining <= 0:
                        await db.execute("UPDATE tasks SET remaining=0, active=0 WHERE id=?", (task_id,))
                    else:
                        await db.execute(
                            "UPDATE tasks SET remaining=?, next_run=? WHERE id=?",
                            (remaining, time.time() + interval, task_id)
                        )
                    await db.commit()
                    await bot.send_message(OWNER_ID, f"Задача #{task_id}: сообщение отправлено в «{title}». Осталось: {remaining}.")
                except Exception as exc:
                    log.exception("Send failed for task %s", task_id)
                    # Don't loop rapidly on errors; stop this task for owner review.
                    await db.execute("UPDATE tasks SET active=0 WHERE id=?", (task_id,))
                    await db.commit()
                    await bot.send_message(OWNER_ID, f"Задача #{task_id} остановлена из-за ошибки отправки: {str(exc)[:700]}")
        except Exception:
            log.exception("Scheduler loop failed")
        await asyncio.sleep(5)


async def main():
    await init_db()
    await client.connect()
    if not await client.is_user_authorized():
        raise RuntimeError("Telethon-сессия не авторизована. Создай TELETHON_SESSION через setup_session.py.")
    scheduler_task = asyncio.create_task(scheduler())
    try:
        await dp.start_polling(bot)
    finally:
        scheduler_task.cancel()
        await client.disconnect()
        await bot.session.close()
        if db:
            await db.close()


if __name__ == "__main__":
    asyncio.run(main())
