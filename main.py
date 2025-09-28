import os
import asyncio
from datetime import datetime, timezone
from typing import Dict, Optional, List, Set

from dotenv import load_dotenv
load_dotenv()

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ContentType
)
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext

from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

import gspread
from google.oauth2.service_account import Credentials
import re

DOCS_RE = re.compile(r"^https?://docs\.google\.com/document/d/([a-zA-Z0-9\-_]+)", re.IGNORECASE)

def is_google_docs_link(url: str) -> bool:
    return bool(DOCS_RE.match((url or "").strip()))

INSTRUCTION_NEED_GDOC = (
    "Нужна ссылка именно на <b>Google Документ</b>.\n\n"
    "Как сделать:\n"
    "1) Загрузи PDF/DOC/DOCX в Google Диск\n"
    "2) ПКМ → «Открыть с помощью» → «Google Документы»\n"
    "3) Файл → Поделиться → Доступ по ссылке: «Читатель»\n"
    "4) Пришли сюда ссылку 🙏"
)

# ==================== CONFIG (.env) ====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
HR_CHAT_ID = int(os.getenv("HR_CHAT_ID", "0"))
SPREADSHEET_NAME = os.getenv("GOOGLE_SHEETS_SPREADSHEET_NAME", "HR Bot Responses")
SHEET_TAB = os.getenv("GOOGLE_SHEETS_TAB", "Responses")
GOOGLE_SA_PATH = os.getenv("GOOGLE_SHEETS_KEYFILE")

ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "")  # Пример: 123,456
ADMIN_IDS: Set[int] = {int(x) for x in ADMIN_IDS_RAW.split(",") if x.strip().isdigit()}

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не задан в .env")
if not GOOGLE_SA_PATH or not os.path.exists(GOOGLE_SA_PATH):
    raise RuntimeError("GOOGLE_SHEETS_KEYFILE не найден. Проверь путь в .env")

# ==================== GOOGLE SHEETS ====================
def open_sheet():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    creds = Credentials.from_service_account_file(GOOGLE_SA_PATH, scopes=scopes)
    gc = gspread.authorize(creds)
    sh = gc.open(SPREADSHEET_NAME)
    return sh

def ensure_responses_ws():
    sh = open_sheet()
    try:
        ws = sh.worksheet(SHEET_TAB)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=SHEET_TAB, rows=2000, cols=40)
    return ws

def get_responses_headers() -> List[str]:
    ws = ensure_responses_ws()
    headers = ws.row_values(1)
    return headers

def append_row(data: Dict):
    ws = ensure_responses_ws()

    row = [
        datetime.now(timezone.utc).isoformat(),  # timestamp
        data.get("vacancy_title", ""),           # vacancy
        str(data.get("tg_id", "")),              # tg_id
        data.get("tg_username", ""),             # tg_username
        data.get("full_name", ""),               # full_name
        data.get("phone", ""),                   # phone
        data.get("email", ""),                   # email
        data.get("city", ""),                    # city
        str(data.get("experience_years", "")),   # experience_years
        data.get("expected_salary", ""),         # expected_salary
        data.get("additional_notes", ""),        # additional_notes
        data.get("resume_file_id", ""),          # resume_file_id
        data.get("resume_file_name", ""),        # resume_file_name
        data.get("resume_link_or_text", ""),     # resume_link_or_text
    ]
    ws.append_row(row, value_input_option="USER_ENTERED")

def load_vacancies_from_sheet() -> Dict[str, Dict]:
    """Собирает конфиг вакансий из листа 'Vacancies'."""
    sh = open_sheet()
    ws = sh.worksheet("Vacancies")
    rows = ws.get_all_records()  # список словарей по заголовкам: code|title|order|key|question
    vacs: Dict[str, Dict] = {}

    for r in rows:
        code = str(r.get("code", "")).strip()
        title = str(r.get("title", "")).strip()
        key = str(r.get("key", "")).strip()
        question = str(r.get("question", "")).strip()
        order_raw = str(r.get("order", "")).strip()

        if not (code and title and key and question):
            continue

        try:
            order = int(order_raw)
        except Exception:
            order = 0

        if code not in vacs:
            vacs[code] = {"title": title, "questions": []}

        vacs[code]["questions"].append({
            "key": key,
            "q": question,
            "_order": order
        })

    # сортируем вопросы внутри каждой вакансии
    for code in vacs:
        vacs[code]["questions"].sort(key=lambda x: x.get("_order", 0))
        for q in vacs[code]["questions"]:
            q.pop("_order", None)

    return vacs

def collect_required_keys(vacs: Dict[str, Dict]) -> Set[str]:
    req: Set[str] = set()
    for v in vacs.values():
        for q in v.get("questions", []):
            k = q.get("key")
            if k:
                req.add(k)
    return req

def find_missing_columns(vacs: Dict[str, Dict]) -> Set[str]:
    """Каких колонок нет в Responses для ключей вопросов."""
    headers = set(get_responses_headers())
    fixed = {
        "timestamp", "vacancy", "tg_id", "tg_username",
        "resume_file_id", "resume_file_name", "resume_link_or_text"
    }
    required = collect_required_keys(vacs)
    needed = required - fixed
    return needed - headers

def reload_vacancies_safe() -> Dict[str, Dict]:
    new_vacs = load_vacancies_from_sheet()
    if not new_vacs:
        raise RuntimeError("Лист 'Vacancies' пуст или оформлен неверно")
    return new_vacs

# ==================== ЗАГРУЗКА ВАКАНСИЙ ПРИ СТАРТЕ ====================
VACANCIES: Dict[str, Dict] = load_vacancies_from_sheet()

# ==================== BOT + FSM ====================
RESUME_PROMPT = (
    "Скинь, пожалуйста, ссылку на резюме в формате <b>Google Документа</b>.\n\n"
    "Как сделать:\n"
    "1) Загрузи свой PDF или DOC/DOCX в Google Диск\n"
    "2) ПКМ → «Открыть с помощью» → «Google Документы»\n"
    "3) Файл → Поделиться → Доступ по ссылке: «Читатель»\n"
    "4) Пришли мне эту ссылку 🙏"
)


bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

class ApplyStates(StatesGroup):
    choosing_vacancy = State()
    asking_questions = State()
    waiting_resume = State()

def vacancies_kb() -> InlineKeyboardMarkup:
    # Если вакансий нет — покажем заглушку
    if not VACANCIES:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Вакансии временно недоступны", callback_data="noop")]
        ])
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=vac["title"], callback_data=f"vac:{code}")]
            for code, vac in VACANCIES.items()
        ]
    )

def next_question(vacancy_code: str, idx: int) -> Optional[str]:
    qs = VACANCIES[vacancy_code]["questions"]
    return qs[idx]["q"] if idx < len(qs) else None

def question_key(vacancy_code: str, idx: int) -> str:
    return VACANCIES[vacancy_code]["questions"][idx]["key"]

# ==================== HANDLERS ====================
@dp.message(CommandStart())
async def start_cmd(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(
        "Привет! Я помогу найти работу мечты. Спойлер - нам понадобится ссылка на твоё резюме в Google Докс. PDF работает не так хорошо, как DOC, поэтому в разы удобнее будет, если ты скинешь ссылку именно на DOC. Обязательно настрой доступ, так чтобы любой человек мог открыть ссылку и изучить его.\nВыбери интересующую вакансию:",
        reply_markup=vacancies_kb()
    )
    await state.set_state(ApplyStates.choosing_vacancy)

@dp.callback_query(F.data.startswith("vac:"))
async def choose_vacancy(cb: CallbackQuery, state: FSMContext):
    vac_code = cb.data.split(":")[1]
    vac = VACANCIES.get(vac_code)
    if not vac:
        await cb.answer("Вакансия не найдена", show_alert=True)
        return
    await state.update_data(vacancy_code=vac_code, q_idx=0, answers={})
    await cb.message.edit_text(f"Ты выбрал: <b>{vac['title']}</b>\nОтветь на несколько вопросов.")
    q = next_question(vac_code, 0)
    if q:
        await cb.message.answer(q)
        await state.set_state(ApplyStates.asking_questions)
    else:
        await cb.message.answer(RESUME_PROMPT)
        await state.set_state(ApplyStates.waiting_resume)
    await cb.answer()

@dp.message(ApplyStates.asking_questions)
async def process_answer(m: Message, state: FSMContext):
    data = await state.get_data()
    vac_code: str = data["vacancy_code"]
    idx: int = data.get("q_idx", 0)
    answers: Dict = data.get("answers", {})

    key = question_key(vac_code, idx)
    answers[key] = (m.text or "").strip()

    idx += 1
    await state.update_data(q_idx=idx, answers=answers)

    q = next_question(vac_code, idx)
    if q:
        await m.answer(q)
    else:
        await m.answer(RESUME_PROMPT)
        await state.set_state(ApplyStates.waiting_resume)

@dp.message(ApplyStates.waiting_resume, F.content_type.in_({ContentType.DOCUMENT, ContentType.TEXT}))
async def process_resume(m: Message, state: FSMContext):
    data = await state.get_data()
    vac_code = data["vacancy_code"]
    vac = VACANCIES[vac_code]
    answers: Dict = data.get("answers", {})

    resume_file_id = ""
    resume_file_name = ""
    resume_link_or_text = ""

    if m.content_type == ContentType.DOCUMENT:
        # ❌ Файлы больше не принимаем напрямую
        await m.answer(INSTRUCTION_NEED_GDOC, parse_mode="HTML")
        return
    else:
        resume_link_or_text = (m.text or "").strip()
        if not is_google_docs_link(resume_link_or_text):
            await m.answer(INSTRUCTION_NEED_GDOC, parse_mode="HTML")
            return

    row = {
        "vacancy_title": vac["title"],
        "tg_id": m.from_user.id,
        "tg_username": m.from_user.username or "",
        **answers,
        "resume_file_id": resume_file_id,
        "resume_file_name": resume_file_name,
        "resume_link_or_text": resume_link_or_text,
    }

    # приведение опыта к числу (если возможно)
    if "experience_years" in row and row["experience_years"]:
        try:
            row["experience_years"] = float(str(row["experience_years"]).replace(",", "."))
        except Exception:
            pass

    try:
        append_row(row)
    except Exception as e:
        await m.answer("⚠️ Произошла ошибка при записи в таблицу. Я всё равно уведомлю HR.")
        await notify_hr(row, fail=str(e))
    else:
        await m.answer("Спасибо! Получили информацию о тебе. HR свяжется, если отклик подойдёт ✅")
        await notify_hr(row)

    await state.clear()


# ---------- HR уведомления ----------
async def notify_hr(row: Dict, fail: Optional[str] = None):
    if HR_CHAT_ID == 0:
        return
    parts = [
        f"📝 <b>Новый отклик</b>\nВакансия: <b>{row.get('vacancy_title','')}</b>",
        f"👤 Кандидат: {row.get('full_name','(не указано)')}",
        f"🆔 TG ID: <code>{row.get('tg_id')}</code>",
    ]
    if row.get("tg_username"):
        parts.append(f"🔗 @{row['tg_username']}")
    if row.get("city"):
        parts.append(f"📍 Город: {row['city']}")
    if row.get("experience_years"):
        parts.append(f"⏳ Опыт: {row['experience_years']}")
    if row.get("expected_salary"):
        parts.append(f"💰 Ожидания: {row['expected_salary']}")
    if row.get("email"):
        parts.append(f"✉️ Email: {row['email']}")
    if row.get("phone"):
        parts.append(f"📞 Телефон: {row['phone']}")
    if row.get("additional_notes"):
        parts.append(f"🧩 Примечание: {row['additional_notes'][:300]}")
    if row.get("resume_file_id"):
        parts.append("📎 Резюме: файл приложу ниже")
    elif row.get("resume_link_or_text"):
        parts.append(f"🔗 Резюме/ссылка: {row['resume_link_or_text']}")
    if fail:
        parts.append(f"\n⚠️ Ошибка записи в таблицу: <code>{fail}</code>")

    text = "\n".join(parts)
    try:
        await bot.send_message(HR_CHAT_ID, text)
        if row.get("resume_file_id"):
            await bot.send_document(HR_CHAT_ID, row["resume_file_id"], caption="Резюме кандидата")
    except Exception:
        pass

# ---------- Служебные команды ----------
@dp.message(Command("myid"))
async def myid_cmd(m: Message):
    await m.answer(f"Ваш TG ID: <code>{m.from_user.id}</code>")

@dp.message(Command("reload"))
async def reload_cmd(m: Message):
    if m.from_user.id not in ADMIN_IDS:
        await m.answer("⛔ Нет доступа. Попросите администратора добавить ваш TG ID в ADMIN_IDS в .env")
        return

    await m.answer("🔄 Обновляю список вакансий из таблицы…")
    try:
        new_vacs = reload_vacancies_safe()
    except Exception as e:
        await m.answer(f"❌ Не удалось обновить вакансии: <code>{e}</code>")
        return

    global VACANCIES
    VACANCIES = new_vacs

    missing = find_missing_columns(VACANCIES)
    total_vac = len(VACANCIES)
    total_q = sum(len(v.get("questions", [])) for v in VACANCIES.values())

    msg = [f"✅ Обновлено. Вакансий: <b>{total_vac}</b>, вопросов: <b>{total_q}</b>."]
    if missing:
        miss_list = ", ".join(sorted(missing))
        msg.append(
            f"⚠️ В листе <b>{SHEET_TAB}</b> отсутствуют колонки для ключей: <code>{miss_list}</code>.\n"
            f"Добавьте их в <b>конец</b> строки заголовков, чтобы ответы корректно записывались."
        )
    else:
        msg.append("🟢 Все ключи вопросов найдены в заголовках листа Responses.")

    await m.answer("\n".join(msg))

# ==================== RUN ====================
async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
