import asyncio
import html
import json
import logging
import os
import random
import sqlite3
from datetime import datetime

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, FSInputFile, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

TOKEN = os.getenv("BOT_TOKEN", "ВСТАВЬТЕ_ТОКЕН_СЮДА")
QUESTIONS_FILE = "questions.json"
DB_FILE = "results.db"
QUIZ_SIZE = 50  # сколько вопросов в одном тесте

logging.basicConfig(level=logging.INFO)

# ---------- Вопросы ----------
with open(QUESTIONS_FILE, encoding="utf-8") as f:
    QUESTIONS = json.load(f)

# ---------- База данных ----------
db = sqlite3.connect(DB_FILE)
db.execute(
    """CREATE TABLE IF NOT EXISTS results (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        username TEXT,
        score INTEGER NOT NULL,
        total INTEGER NOT NULL,
        created_at TEXT NOT NULL
    )"""
)
db.commit()


def save_result(user_id, username, score, total):
    db.execute(
        "INSERT INTO results (user_id, username, score, total, created_at) VALUES (?,?,?,?,?)",
        (user_id, username, score, total, datetime.now().strftime("%d.%m.%Y %H:%M")),
    )
    db.commit()


def get_stats(user_id):
    rows = db.execute(
        "SELECT score, total, created_at FROM results WHERE user_id=? ORDER BY id DESC",
        (user_id,),
    ).fetchall()
    return rows


# ---------- Сессии (текущие тесты в памяти) ----------
sessions = {}  # user_id -> dict

bot = Bot(TOKEN, parse_mode=None)
dp = Dispatcher()


def main_menu():
    kb = InlineKeyboardBuilder()
    kb.button(text="🚦 Начать тест", callback_data="start_quiz")
    kb.button(text="📊 Мои результаты", callback_data="stats")
    kb.adjust(1)
    return kb.as_markup()


def build_question_text(s):
    q = QUESTIONS[s["order"][s["i"]]]
    total = len(s["order"])
    text = f"<b>Вопрос {s['i'] + 1}/{total}</b>\n\n{html.escape(q['question'])}\n\n"
    for n, opt in enumerate(q["options"], 1):
        text += f"<b>{n}.</b> {html.escape(opt)}\n"
    kb = InlineKeyboardBuilder()
    for n in range(len(q["options"])):
        kb.button(text=str(n + 1), callback_data=f"ans:{s['i']}:{n}")
    kb.adjust(len(q["options"]))
    return q, text, kb.as_markup()


async def send_question(chat_id, user_id):
    s = sessions[user_id]
    q, text, markup = build_question_text(s)
    if q.get("image") and os.path.exists(q["image"]):
        await bot.send_photo(
            chat_id, FSInputFile(q["image"]), caption=text,
            reply_markup=markup, parse_mode="HTML",
        )
    else:
        await bot.send_message(chat_id, text, reply_markup=markup, parse_mode="HTML")


async def finish_quiz(chat_id, user_id, username):
    s = sessions.pop(user_id)
    total = len(s["order"])
    score = s["score"]
    save_result(user_id, username, score, total)
    percent = round(score / total * 100)

    text = f"🏁 <b>Тест завершён!</b>\n\nРезультат: <b>{score}/{total}</b> ({percent}%)\n"
    if percent >= 90:
        text += "Отлично, так держать! 🎉"
    elif percent >= 70:
        text += "Неплохо, но есть что повторить 👍"
    else:
        text += "Стоит ещё потренироваться 📚"

    if s["wrong"]:
        text += "\n\n<b>Ошибки:</b>\n"
        for idx in s["wrong"][:15]:
            text += f"• {html.escape(QUESTIONS[idx]['question'][:90])}\n"
        if len(s["wrong"]) > 15:
            text += f"…и ещё {len(s['wrong']) - 15}"

    await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=main_menu())


# ---------- Хендлеры ----------
@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "Привет! 👋 Это тренажёр по ПДД.\n"
        f"В тесте {min(QUIZ_SIZE, len(QUESTIONS))} вопросов, они идут по очереди. "
        "В конце я покажу результат и сохраню его.",
        reply_markup=main_menu(),
    )


@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    await message.answer(format_stats(message.from_user.id), parse_mode="HTML", reply_markup=main_menu())


def format_stats(user_id):
    rows = get_stats(user_id)
    if not rows:
        return "Вы ещё не проходили тест."
    best = max(r[0] / r[1] for r in rows)
    avg = sum(r[0] / r[1] for r in rows) / len(rows)
    text = (
        f"📊 <b>Ваши результаты</b>\n\n"
        f"Попыток: {len(rows)}\n"
        f"Лучший результат: {round(best * 100)}%\n"
        f"Средний: {round(avg * 100)}%\n\n"
        f"<b>Последние попытки:</b>\n"
    )
    for score, total, created in rows[:5]:
        text += f"• {created} — {score}/{total} ({round(score / total * 100)}%)\n"
    return text


@dp.callback_query(F.data == "stats")
async def cb_stats(call: CallbackQuery):
    await call.message.answer(format_stats(call.from_user.id), parse_mode="HTML", reply_markup=main_menu())
    await call.answer()


@dp.callback_query(F.data == "start_quiz")
async def cb_start_quiz(call: CallbackQuery):
    order = list(range(len(QUESTIONS)))
    random.shuffle(order)
    order = order[:QUIZ_SIZE]
    sessions[call.from_user.id] = {"order": order, "i": 0, "score": 0, "wrong": [], "answered": False}
    await call.answer()
    await send_question(call.message.chat.id, call.from_user.id)


@dp.callback_query(F.data.startswith("ans:"))
async def cb_answer(call: CallbackQuery):
    user_id = call.from_user.id
    s = sessions.get(user_id)
    if not s:
        await call.answer("Тест не запущен. Нажмите «Начать тест».", show_alert=True)
        return

    _, i, chosen = call.data.split(":")
    i, chosen = int(i), int(chosen)
    if i != s["i"] or s["answered"]:
        await call.answer("Вы уже ответили на этот вопрос.")
        return
    s["answered"] = True

    q_index = s["order"][i]
    q = QUESTIONS[q_index]
    correct = q["correct"]

    # убираем кнопки у вопроса
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    if chosen == correct:
        s["score"] += 1
        text = "✅ <b>Верно!</b>"
    else:
        s["wrong"].append(q_index)
        text = (
            f"❌ <b>Неверно.</b>\nПравильный ответ: <b>{correct + 1}.</b> "
            f"{html.escape(q['options'][correct])}"
        )
    if q.get("explanation"):
        text += f"\n\n💡 {html.escape(q['explanation'])}"

    kb = InlineKeyboardBuilder()
    last = s["i"] + 1 >= len(s["order"])
    kb.button(text="🏁 Завершить" if last else "Дальше ➡️", callback_data="next")
    await call.message.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())
    await call.answer()


@dp.callback_query(F.data == "next")
async def cb_next(call: CallbackQuery):
    user_id = call.from_user.id
    s = sessions.get(user_id)
    if not s:
        await call.answer("Тест не запущен.", show_alert=True)
        return
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await call.answer()

    s["i"] += 1
    s["answered"] = False
    if s["i"] >= len(s["order"]):
        s["i"] -= 1  # чтобы не выйти за границы
        await finish_quiz(call.message.chat.id, user_id, call.from_user.username)
    else:
        await send_question(call.message.chat.id, user_id)


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
