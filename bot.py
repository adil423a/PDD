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
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    FSInputFile,
    Message,
    PollAnswer,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

TOKEN = os.getenv("BOT_TOKEN", "ВСТАВЬТЕ_ТОКЕН_СЮДА")
QUESTIONS_FILE = "questions.json"
DB_FILE = "results.db"

# сколько ошибок показывать в итоговом сообщении
MAX_SHOWN_ERRORS = 10

logging.basicConfig(level=logging.INFO)

# ---------- Вопросы ----------
with open(QUESTIONS_FILE, encoding="utf-8") as f:
    QUESTIONS = json.load(f)
QINDEX = {q["question"]: i for i, q in enumerate(QUESTIONS)}
TOTAL = len(QUESTIONS)

MODE_NAMES = {"train": "Тренировка", "exam": "Экзамен", "review": "Ошибки"}

# ---------- База данных ----------
db = sqlite3.connect(DB_FILE)
db.execute(
    """CREATE TABLE IF NOT EXISTS results (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        username TEXT,
        score INTEGER NOT NULL,
        total INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        mode TEXT DEFAULT 'train'
    )"""
)
# миграция для старой базы (если results.db уже была без колонки mode)
if "mode" not in [r[1] for r in db.execute("PRAGMA table_info(results)")]:
    db.execute("ALTER TABLE results ADD COLUMN mode TEXT DEFAULT 'train'")
db.execute("CREATE TABLE IF NOT EXISTS sessions (user_id INTEGER PRIMARY KEY, data TEXT NOT NULL)")
db.execute(
    """CREATE TABLE IF NOT EXISTS mistakes (
        user_id INTEGER NOT NULL,
        question TEXT NOT NULL,
        PRIMARY KEY (user_id, question)
    )"""
)
db.commit()


def save_result(user_id, username, score, total, mode):
    db.execute(
        "INSERT INTO results (user_id, username, score, total, created_at, mode) VALUES (?,?,?,?,?,?)",
        (user_id, username, score, total, datetime.now().strftime("%d.%m.%Y %H:%M"), mode),
    )
    db.commit()


def get_results(user_id):
    return db.execute(
        "SELECT score, total, created_at, mode FROM results WHERE user_id=? ORDER BY id DESC",
        (user_id,),
    ).fetchall()


# --- сессии (хранятся в базе, переживают перезапуск бота) ---
def get_session(user_id):
    row = db.execute("SELECT data FROM sessions WHERE user_id=?", (user_id,)).fetchone()
    return json.loads(row[0]) if row else None


def put_session(user_id, s):
    db.execute(
        "INSERT OR REPLACE INTO sessions (user_id, data) VALUES (?,?)",
        (user_id, json.dumps(s)),
    )
    db.commit()


def del_session(user_id):
    db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
    db.commit()


# --- банк ошибок ---
def get_mistakes(user_id):
    rows = db.execute("SELECT question FROM mistakes WHERE user_id=?", (user_id,)).fetchall()
    return [QINDEX[r[0]] for r in rows if r[0] in QINDEX]


def add_mistake(user_id, q_idx):
    db.execute(
        "INSERT OR IGNORE INTO mistakes (user_id, question) VALUES (?,?)",
        (user_id, QUESTIONS[q_idx]["question"]),
    )
    db.commit()


def remove_mistake(user_id, q_idx):
    db.execute(
        "DELETE FROM mistakes WHERE user_id=? AND question=?",
        (user_id, QUESTIONS[q_idx]["question"]),
    )
    db.commit()


bot = Bot(TOKEN)
dp = Dispatcher()


# ---------- Клавиатуры ----------
def main_menu(user_id):
    kb = InlineKeyboardBuilder()
    s = get_session(user_id)
    if s:
        kb.button(
            text=f"▶️ Продолжить тест ({s['i'] + 1}/{len(s['order'])})",
            callback_data="resume",
        )
    kb.button(text="🚦 Новый тест", callback_data="start_quiz")
    n = len(get_mistakes(user_id))
    if n:
        kb.button(text=f"🔁 Работа над ошибками ({n})", callback_data="review")
    kb.button(text="📊 Мои результаты", callback_data="stats")
    kb.adjust(1)
    return kb.as_markup()


def poll_kb(i):
    kb = InlineKeyboardBuilder()
    kb.button(text="Дальше ➡️", callback_data=f"next:{i}")
    kb.button(text="⏹ Прервать", callback_data="stop")
    kb.adjust(2)
    return kb.as_markup()


# ---------- Логика теста ----------
def start_session(user_id, mode, indices):
    order = list(indices)
    random.shuffle(order)
    s = {
        "mode": mode,          # train / exam / review
        "order": order,
        "i": 0,
        "score": 0,
        "wrong": [],
        "answered": False,
        "poll_id": None,
        "message_id": None,
        "correct_pos": 0,
    }
    put_session(user_id, s)
    return s


async def send_question(chat_id, user_id):
    s = get_session(user_id)
    q = QUESTIONS[s["order"][s["i"]]]

    # перемешиваем варианты, запоминаем где правильный
    perm = list(range(len(q["options"])))
    random.shuffle(perm)
    s["correct_pos"] = perm.index(q["correct"])
    options = [q["options"][p][:100] for p in perm]

    if q.get("image") and os.path.exists(q["image"]):
        await bot.send_photo(chat_id, FSInputFile(q["image"]))

    title = f"{s['i'] + 1}/{len(s['order'])}. {q['question']}"[:300]
    kwargs = dict(
        chat_id=chat_id,
        question=title,
        options=options,
        is_anonymous=False,
        reply_markup=poll_kb(s["i"]),
    )
    if s["mode"] == "exam":
        kwargs["type"] = "regular"   # без подсказки правильного ответа
    else:
        kwargs["type"] = "quiz"
        kwargs["correct_option_id"] = s["correct_pos"]
        if q.get("explanation"):
            kwargs["explanation"] = q["explanation"][:200]

    msg = await bot.send_poll(**kwargs)
    s["poll_id"] = msg.poll.id
    s["message_id"] = msg.message_id
    s["answered"] = False
    put_session(user_id, s)


async def finish_quiz(chat_id, user_id, username):
    s = get_session(user_id)
    del_session(user_id)
    total = len(s["order"])
    score = s["score"]
    percent = round(score / total * 100)

    if s["mode"] == "review":
        left = len(get_mistakes(user_id))
        text = (
            f"🔁 <b>Работа над ошибками завершена</b>\n\n"
            f"Исправлено: <b>{score}/{total}</b>\n"
            f"Осталось в ошибках: <b>{left}</b>"
        )
    else:
        save_result(user_id, username, score, total, s["mode"])
        text = (
            f"🏁 <b>Тест завершён!</b> ({MODE_NAMES[s['mode']]})\n\n"
            f"Результат: <b>{score}/{total}</b> ({percent}%)\n"
        )
        if percent >= 90:
            text += "Отлично, так держать! 🎉"
        elif percent >= 70:
            text += "Неплохо, но есть что повторить 👍"
        else:
            text += "Стоит ещё потренироваться 📚"

        if s["wrong"]:
            text += "\n\n<b>Ошибки:</b>\n"
            for idx in s["wrong"][:MAX_SHOWN_ERRORS]:
                q = QUESTIONS[idx]
                text += (
                    f"• {html.escape(q['question'])}\n"
                    f"   ✔ {html.escape(q['options'][q['correct']])}\n\n"
                )
            if len(s["wrong"]) > MAX_SHOWN_ERRORS:
                text += f"…и ещё {len(s['wrong']) - MAX_SHOWN_ERRORS}"

    # защита от лимита Telegram (4096 символов)
    if len(text) > 4000:
        text = text[:3990] + "…"

    await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=main_menu(user_id))


async def advance(chat_id, user_id, username):
    s = get_session(user_id)
    if not s:
        return
    if s["answered"]:
        s["i"] += 1
        s["answered"] = False
        put_session(user_id, s)
    if s["i"] >= len(s["order"]):
        await finish_quiz(chat_id, user_id, username)
    else:
        await send_question(chat_id, user_id)


# ---------- Команды ----------
@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "Привет! 👋 Это тренажёр по ПДД.\n\n"
        "• <b>Тренировка</b> — сразу видно, правильно ли ответили.\n"
        "• <b>Экзамен</b> — результат только в конце.\n"
        "• Неправильные ответы копятся в «Работе над ошибками».",
        parse_mode="HTML",
        reply_markup=main_menu(message.from_user.id),
    )


@dp.message(Command("menu"))
async def cmd_menu(message: Message):
    await message.answer("Главное меню:", reply_markup=main_menu(message.from_user.id))


@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    await message.answer(
        format_stats(message.from_user.id), parse_mode="HTML",
        reply_markup=main_menu(message.from_user.id),
    )


@dp.message(Command("stop"))
async def cmd_stop(message: Message):
    del_session(message.from_user.id)
    await message.answer("Тест прерван.", reply_markup=main_menu(message.from_user.id))


def format_stats(user_id):
    rows = get_results(user_id)
    n_err = len(get_mistakes(user_id))
    if not rows:
        return "Вы ещё не проходили тест." + (f"\nВ работе над ошибками: {n_err}" if n_err else "")
    best = max(r[0] / r[1] for r in rows)
    avg = sum(r[0] / r[1] for r in rows) / len(rows)
    text = (
        f"📊 <b>Ваши результаты</b>\n\n"
        f"Попыток: {len(rows)}\n"
        f"Лучший результат: {round(best * 100)}%\n"
        f"Средний: {round(avg * 100)}%\n"
        f"Вопросов в работе над ошибками: {n_err}\n\n"
        f"<b>Последние попытки:</b>\n"
    )
    for score, total, created, mode in rows[:5]:
        text += f"• {created} — {score}/{total} ({round(score / total * 100)}%), {MODE_NAMES.get(mode, mode)}\n"
    return text


# ---------- Меню и выбор режима ----------
@dp.callback_query(F.data == "stats")
async def cb_stats(call: CallbackQuery):
    await call.message.answer(
        format_stats(call.from_user.id), parse_mode="HTML",
        reply_markup=main_menu(call.from_user.id),
    )
    await call.answer()


@dp.callback_query(F.data == "start_quiz")
async def cb_start_quiz(call: CallbackQuery):
    kb = InlineKeyboardBuilder()
    kb.button(text="📖 Тренировка (с подсказками)", callback_data="mode:train")
    kb.button(text="📝 Экзамен (результат в конце)", callback_data="mode:exam")
    kb.adjust(1)
    await call.message.answer("Выберите режим:", reply_markup=kb.as_markup())
    await call.answer()


@dp.callback_query(F.data.startswith("mode:"))
async def cb_mode(call: CallbackQuery):
    mode = call.data.split(":")[1]
    counts = [c for c in (10, 20) if c < TOTAL] + [TOTAL]
    kb = InlineKeyboardBuilder()
    for c in counts:
        label = f"Все {c}" if c == TOTAL else str(c)
        kb.button(text=label, callback_data=f"cnt:{mode}:{c}")
    kb.adjust(len(counts))
    await call.message.edit_text("Сколько вопросов?", reply_markup=kb.as_markup())
    await call.answer()


@dp.callback_query(F.data.startswith("cnt:"))
async def cb_count(call: CallbackQuery):
    _, mode, n = call.data.split(":")
    indices = random.sample(range(TOTAL), int(n))
    start_session(call.from_user.id, mode, indices)
    await call.message.edit_reply_markup(reply_markup=None)
    await call.answer()
    await send_question(call.message.chat.id, call.from_user.id)


@dp.callback_query(F.data == "review")
async def cb_review(call: CallbackQuery):
    indices = get_mistakes(call.from_user.id)
    if not indices:
        await call.answer("Ошибок пока нет 👏", show_alert=True)
        return
    start_session(call.from_user.id, "review", indices)
    await call.answer()
    await send_question(call.message.chat.id, call.from_user.id)


@dp.callback_query(F.data == "resume")
async def cb_resume(call: CallbackQuery):
    if not get_session(call.from_user.id):
        await call.answer("Нет незавершённого теста.", show_alert=True)
        return
    await call.answer()
    await advance(call.message.chat.id, call.from_user.id, call.from_user.username)


@dp.callback_query(F.data == "stop")
async def cb_stop(call: CallbackQuery):
    del_session(call.from_user.id)
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await call.message.answer("Тест прерван.", reply_markup=main_menu(call.from_user.id))
    await call.answer()


# ---------- Ответ на вопрос (нативный опрос Telegram) ----------
@dp.poll_answer()
async def on_poll_answer(pa: PollAnswer):
    user_id = pa.user.id
    s = get_session(user_id)
    if not s or s["poll_id"] != pa.poll_id or s["answered"] or not pa.option_ids:
        return
    s["answered"] = True

    q_idx = s["order"][s["i"]]
    if pa.option_ids[0] == s["correct_pos"]:
        s["score"] += 1
        remove_mistake(user_id, q_idx)
    else:
        s["wrong"].append(q_idx)
        add_mistake(user_id, q_idx)
    put_session(user_id, s)

    # в экзамене закрываем опрос, чтобы нельзя было переголосовать
    if s["mode"] == "exam":
        try:
            await bot.stop_poll(user_id, s["message_id"], reply_markup=poll_kb(s["i"]))
        except Exception:
            pass


@dp.callback_query(F.data.startswith("next:"))
async def cb_next(call: CallbackQuery):
    user_id = call.from_user.id
    s = get_session(user_id)
    i = int(call.data.split(":")[1])
    if not s or s["i"] != i:
        await call.answer("Этот вопрос уже неактуален.")
        try:
            await call.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return
    if not s["answered"]:
        await call.answer("Сначала выберите вариант ответа 👆", show_alert=True)
        return
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await call.answer()
    await advance(call.message.chat.id, user_id, call.from_user.username)


async def main():
    await bot.set_my_commands([
        BotCommand(command="menu", description="Главное меню"),
        BotCommand(command="stats", description="Мои результаты"),
        BotCommand(command="stop", description="Прервать тест"),
    ])
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
