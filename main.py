# main.py — Telegram-бот планировщик с меню-кнопками, JobQueue, диалогом добавления,
# однодневным назначением гибких задач, просроченными, автоудалением экранов
# и чтением стоических цитат из файла quotes.json (UTF-8).

from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta, time
from typing import Optional, Dict, List, Tuple
import uuid
import copy
import json
from pathlib import Path

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, PicklePersistence, ConversationHandler,
    MessageHandler, filters
)

# ==================== Конфигурация ====================
BOT_TOKEN = "8382727090:AAEzR9dhvDcCgwFVXAEZBMJU60wEaChzfl4"  # замените
DAY_START = (6, 0)
DAY_END = (22, 0)

# Путь к файлу цитат стоиков (UTF-8), формат: [{"q": "цитата", "a": "автор"}, ...]
QUOTES_PATH = Path("quotes.json")
QUOTES: List[dict] = []

def load_quotes() -> None:
    """Загрузить список цитат из quotes.json (UTF-8)."""
    global QUOTES
    try:
        if QUOTES_PATH.exists():
            with QUOTES_PATH.open("r", encoding="utf-8") as f:
                QUOTES = json.load(f)
        else:
            QUOTES = []
    except Exception:
        QUOTES = []

def stoic_quote_ru() -> str:
    """Вернуть случайную цитату стоика на русском из QUOTES, либо фолбэк."""
    import random
    if QUOTES:
        item = random.choice(QUOTES)
        q = (item.get("q") or "").strip()
        a = (item.get("a") or "Стоик").strip()
        if q:
            return f"«{q}» — {a}"
    return "«Счастье вашей жизни зависит от качества ваших мыслей.» — Марк Аврелий"

# ==================== Домены ====================
@dataclass
class Task:
    id: str
    title: str
    duration_min: int
    deadline_at: datetime
    effort: str = "medium"  # quick|medium|heavy|extreme
    fixed_start: Optional[datetime] = None
    fixed_end: Optional[datetime] = None
    splittable: bool = False
    done: bool = False
    auto: bool = False              # для гибких: включать в автопланирование
    constant: bool = False          # постоянная (циклическая)
    dow: List[int] = None           # 0=Пн ... 6=Вс
    constant_start_hm: Optional[Tuple[int, int]] = None  # (HH,MM)
    constant_end_hm: Optional[Tuple[int, int]] = None    # (HH,MM)
    planned_for: Optional[str] = None  # 'YYYY-MM-DD' — назначенная дата для гибкой
    overdue: bool = False           # просроченная

@dataclass
class DoneEntry:
    task: Task
    completed_at: datetime

# ==================== Персистентность ====================
def get_store(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> Dict:
    chat = context.chat_data.setdefault(chat_id, {})
    chat.setdefault("tasks", {})     # активные задачи
    chat.setdefault("history", [])   # история выполненных
    chat.setdefault("overdue", {})   # просроченные
    return chat

def ser_task(t: Task) -> dict:
    return {
        "id": t.id,
        "title": t.title,
        "duration_min": t.duration_min,
        "deadline_at": t.deadline_at.isoformat(),
        "effort": t.effort,
        "fixed_start": t.fixed_start.isoformat() if t.fixed_start else None,
        "fixed_end": t.fixed_end.isoformat() if t.fixed_end else None,
        "splittable": t.splittable,
        "done": t.done,
        "auto": t.auto,
        "constant": t.constant,
        "dow": t.dow or [],
        "constant_start_hm": list(t.constant_start_hm) if t.constant_start_hm else None,
        "constant_end_hm": list(t.constant_end_hm) if t.constant_end_hm else None,
        "planned_for": t.planned_for,
        "overdue": t.overdue,
    }

def deser_task(d: dict) -> Task:
    def parse_dt(x):
        return datetime.fromisoformat(x) if x else None
    csh = tuple(d["constant_start_hm"]) if d.get("constant_start_hm") else None
    ceh = tuple(d["constant_end_hm"]) if d.get("constant_end_hm") else None
    return Task(
        id=d["id"],
        title=d["title"],
        duration_min=int(d["duration_min"]),
        deadline_at=datetime.fromisoformat(d["deadline_at"]),
        effort=d.get("effort", "medium"),
        fixed_start=parse_dt(d.get("fixed_start")),
        fixed_end=parse_dt(d.get("fixed_end")),
        splittable=bool(d.get("splittable", False)),
        done=bool(d.get("done", False)),
        auto=bool(d.get("auto", False)),
        constant=bool(d.get("constant", False)),
        dow=list(d.get("dow", [])),
        constant_start_hm=csh,
        constant_end_hm=ceh,
        planned_for=d.get("planned_for"),
        overdue=bool(d.get("overdue", False)),
    )

def ser_done(entry: DoneEntry) -> dict:
    return {"task": ser_task(entry.task), "completed_at": entry.completed_at.isoformat()}

def deser_done(d: dict) -> DoneEntry:
    return DoneEntry(task=deser_task(d["task"]), completed_at=datetime.fromisoformat(d["completed_at"]))

# ==================== Планирование ====================
@dataclass
class PlanItem:
    start: datetime
    end: datetime
    label: str
    task_id: Optional[str] = None

def effort_weight(e: str) -> float:
    return {"quick": 0.2, "medium": 0.5, "heavy": 0.8, "extreme": 1.0}.get(e, 0.5)

def compute_score(now: datetime, t: Task, alpha=1.0, beta=1.0) -> float:
    dt_min = max(1.0, (t.deadline_at - now).total_seconds() / 60.0)
    urgency = 1.0 / dt_min
    return alpha * urgency + beta * effort_weight(t.effort)

def day_window(day: datetime, now: datetime) -> Tuple[datetime, datetime]:
    start = day.replace(hour=DAY_START[0], minute=DAY_START[1], second=0, microsecond=0)
    end = day.replace(hour=DAY_END[0], minute=DAY_END[1], second=0, microsecond=0)
    if day.date() == now.date():
        start = max(start, now)
    return start, end

def meals_for_day(day: datetime) -> List[Tuple[datetime, datetime, str]]:
    def block(h, m, dur, label):
        s = day.replace(hour=h, minute=m, second=0, microsecond=0)
        return (s, s + timedelta(minutes=dur), label)
    return [
        block(8, 0, 30, "Завтрак"),
        block(13, 0, 45, "Обед"),
        block(19, 0, 45, "Ужин"),
    ]

def build_fixed_blocks(day: datetime, now: datetime, tasks: List[Task]) -> Tuple[List[PlanItem], List[Tuple[datetime, datetime]]]:
    day_start, day_end = day_window(day, now)
    if day_start >= day_end:
        return [], []
    free = [(day_start, day_end)]
    items: List[PlanItem] = []

    fixed_blocks = []
    # Приемы пищи
    for s, e, label in meals_for_day(day):
        if e > day_start and s < day_end:
            fixed_blocks.append((max(s, day_start), min(e, day_end), label, None))
    # Фиксированные задачи
    for t in tasks:
        if t.done:
            continue
        if t.fixed_start and t.fixed_end and t.fixed_end > day_start and t.fixed_start < day_end:
            s = max(t.fixed_start, day_start)
            e = min(t.fixed_end, day_end)
            fixed_blocks.append((s, e, t.title, t.id))
    # Постоянные задачи
    for t in tasks:
        if t.done or not t.constant or not t.dow or not t.constant_start_hm or not t.constant_end_hm:
            continue
        if day.weekday() in t.dow:
            sh, sm = t.constant_start_hm
            eh, em = t.constant_end_hm
            s = day.replace(hour=sh, minute=sm, second=0, microsecond=0)
            e = day.replace(hour=eh, minute=em, second=0, microsecond=0)
            if e > s and e > day_start and s < day_end:
                fixed_blocks.append((max(s, day_start), min(e, day_end), t.title, t.id))

    for s, e, label, tid in sorted(fixed_blocks, key=lambda x: x[0]):
        items.append(PlanItem(start=s, end=e, label=label, task_id=tid))
        new_free = []
        for fs, fe in free:
            if e <= fs or s >= fe:
                new_free.append((fs, fe))
            else:
                if fs < s:
                    new_free.append((fs, s))
                if e < fe:
                    new_free.append((e, fe))
        free = sorted(new_free, key=lambda p: p[0])
    return items, free

def eligible_flex_for_day(day: datetime, now: datetime, tasks: List[Task]) -> List[Task]:
    day_end = day.replace(hour=DAY_END[0], minute=DAY_END[1], second=0, microsecond=0)
    out = []
    for t in tasks:
        if t.done or t.constant or (t.fixed_start and t.fixed_end) or not t.auto or t.overdue:
            continue
        # не ставим после дедлайна (если дедлайн раньше конца дня)
        if t.deadline_at and t.deadline_at < day_end:
            continue
        # назначаем только один день (None/сегодня/перенос с прошлой плановой даты)
        if t.planned_for is None or t.planned_for == day.strftime("%Y-%m-%d") or t.planned_for < day.strftime("%Y-%m-%d"):
            out.append(t)
    return sorted(out, key=lambda x: (-compute_score(now, x), x.duration_min))

def plan_today_assign_once(day: datetime, now: datetime, tasks: List[Task], persist: bool) -> List[PlanItem]:
    items, free = build_fixed_blocks(day, now, tasks)
    flex = eligible_flex_for_day(day, now, tasks)

    def place(minutes: int, label: str, tid: str) -> bool:
        nonlocal free, items
        for i, (fs, fe) in enumerate(free):
            slot = int((fe - fs).total_seconds() // 60)
            if slot >= minutes:
                s = fs
                e = fs + timedelta(minutes=minutes)
                items.append(PlanItem(start=s, end=e, label=label, task_id=tid))
                new_free = []
                if e < fe:
                    new_free.append((e, fe))
                free = free[:i] + new_free + free[i+1:]
                return True
        return False

    for t in flex:
        need = t.duration_min
        chunk = 120 if (t.effort == "extreme" and t.splittable) else need
        placed_total = 0
        while need > 0 and free:
            part = min(chunk, need)
            if not place(part, t.title, t.id):
                break
            placed_total += part
            need -= part
        if placed_total > 0 and persist:
            t.planned_for = day.strftime("%Y-%m-%d")

    return sorted(items, key=lambda x: x.start)

def plan_week_without_dup(start_day: datetime, now: datetime, tasks: List[Task]) -> Dict[str, List[PlanItem]]:
    tasks_copy = [copy.deepcopy(t) for t in tasks]
    days = [(start_day + timedelta(days=i)).replace(hour=12, minute=0, second=0, microsecond=0) for i in range(7)]
    per_day: Dict[datetime.date, List[PlanItem]] = {}
    free_map: Dict[datetime.date, List[Tuple[datetime, datetime]]] = {}

    for day in days:
        items, free = build_fixed_blocks(day, now, tasks_copy)
        per_day[day.date()] = items
        free_map[day.date()] = free

    flex = [t for t in tasks_copy if not t.done and not t.constant and not (t.fixed_start and t.fixed_end) and t.auto and not t.overdue]
    flex = sorted(flex, key=lambda x: (-compute_score(now, x), x.duration_min))

    for t in flex:
        last_day = min(days[-1], t.deadline_at) if t.deadline_at else days[-1]
        for day in days:
            if day > last_day:
                break
            key = day.date()
            free = free_map[key]
            def place(minutes: int) -> bool:
                nonlocal free
                for i, (fs, fe) in enumerate(free):
                    slot = int((fe - fs).total_seconds() // 60)
                    if slot >= minutes:
                        s = fs
                        e = fs + timedelta(minutes=minutes)
                        per_day[key].append(PlanItem(s, e, t.title, t.id))
                        new_free = []
                        if e < fe:
                            new_free.append((e, fe))
                        free = free[:i] + new_free + free[i+1:]
                        free_map[key] = free
                        return True
                return False
            need = t.duration_min
            chunk = 120 if (t.effort == "extreme" and t.splittable) else need
            placed = 0
            while need > 0 and free:
                part = min(chunk, need)
                if not place(part):
                    break
                placed += part
                need -= part
            if placed > 0:
                t.planned_for = day.strftime("%Y-%m-%d")
                break

    result = {}
    for day in days:
        items = sorted(per_day[day.date()], key=lambda x: x.start)
        result[day.strftime("%a %d.%m")] = items
    return result

# ==================== Форматирование ====================
def hmm(dt: timedelta) -> str:
    total_min = int(dt.total_seconds() // 60)
    h, m = divmod(total_min, 60)
    return f"{h:02d}:{m:02d}"

def time_left_str(now: datetime, to: datetime) -> str:
    if to <= now:
        return "00:00"
    return hmm(to - now)

def fmt_plan(items: List[PlanItem]) -> str:
    if not items:
        return "Нет задач на выбранный день."
    lines = [f"{it.start:%H:%M}-{it.end:%H:%M} • {it.label}" for it in items]
    return "\n".join(lines)

def fmt_tasks(tasks: List[Task], now: datetime) -> str:
    if not tasks:
        return "Список пуст."
    out = []
    for t in tasks:
        status = "✅" if t.done else ("🟩" if t.auto else "⬜️")
        if t.constant:
            tl = "—"
        elif t.fixed_end:
            tl = time_left_str(now, t.fixed_end)
        else:
            tl = time_left_str(now, t.deadline_at)
        tag = "фикс" if (t.fixed_start and t.fixed_end) else ("пост." if t.constant else "гибк.")
        out.append(f"{status} [{t.id}] {t.title} — до дедлайна {tl}; {t.effort}; {t.duration_min} мин; {tag}")
    return "\n".join(out)

def fmt_history(hist: List[DoneEntry]) -> str:
    if not hist:
        return "История пуста."
    lines = []
    for e in hist[-50:][::-1]:
        lines.append(f"✅ [{e.task.id}] {e.task.title} — выполнено {e.completed_at:%Y-%m-%d %H:%M}")
    return "\n".join(lines)

# ==================== Клавиатуры ====================
def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Добавить задачу", callback_data="menu:add")],
        [InlineKeyboardButton("📅 Сегодня", callback_data="menu:today"),
         InlineKeyboardButton("🗓 Неделя", callback_data="menu:week")],
        [InlineKeyboardButton("📋 Список задач", callback_data="menu:list"),
         InlineKeyboardButton("📜 История", callback_data="menu:history")],
        [InlineKeyboardButton("⏰ Просроченные", callback_data="menu:overdue")],
        [InlineKeyboardButton("⚙️ Настройки (в разработке)", callback_data="menu:settings")]
    ])

def task_row_buttons(t: Task) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Готово" if not t.done else "↩️ Вернуть", callback_data=f"task:done:{t.id}"),
        InlineKeyboardButton("🟩 Авто" if t.auto else "⬜️ Авто", callback_data=f"task:auto:{t.id}")
    ],[
        InlineKeyboardButton("🗑 Удалить", callback_data=f"task:del:{t.id}"),
        InlineKeyboardButton("🆕 На основе", callback_data=f"task:dup:{t.id}")
    ]])

def overdue_row_kb(tid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🗓 Новый дедлайн", callback_data=f"od:setdl:{tid}"),
        InlineKeyboardButton("✅ Готово", callback_data=f"od:done:{tid}"),
        InlineKeyboardButton("🗑 Удалить", callback_data=f"od:del:{tid}"),
    ]])

# ==================== Экран-утилиты ====================
async def delete_bot_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg_ids = context.user_data.get("bot_messages", [])
    chat_id = update.effective_chat.id
    for mid in msg_ids:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=mid)
        except Exception:
            pass
    context.user_data["bot_messages"] = []

async def send_screen(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, reply_markup=None):
    await delete_bot_messages(update, context)
    if reply_markup is None:
        reply_markup = main_menu_kb()
    msg = await update.effective_chat.send_message(text, reply_markup=reply_markup)
    context.user_data.setdefault("bot_messages", []).append(msg.message_id)
    return msg

async def send_screen_plain(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    msg = await update.effective_chat.send_message(text)
    context.user_data.setdefault("bot_messages", []).append(msg.message_id)
    return msg

async def send_keyboard(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, kb: InlineKeyboardMarkup):
    msg = await update.effective_chat.send_message(text, reply_markup=kb)
    context.user_data.setdefault("bot_messages", []).append(msg.message_id)
    return msg

# ==================== Диалог добавления ====================
(
    A_TITLE, A_KIND,
    A_DEADLINE_FLEX, A_DURATION_FLEX,
    A_FIXED_START, A_FIXED_END,
    A_CONST_DAYS, A_CONST_TIME_START, A_CONST_TIME_END,
    A_EFFORT, A_SPLIT, A_AUTO, A_SAVE
) = range(13)

def start_add_conv(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["add"] = {}
    context.user_data["bot_messages"] = []

def parse_days_from_buttons(selected: List[str]) -> List[int]:
    map_ru = {"Пн":0,"Вт":1,"Ср":2,"Чт":3,"Пт":4,"Сб":5,"Вс":6}
    return [map_ru[x] for x in selected if x in map_ru]

def days_kb(selected: List[str]) -> InlineKeyboardMarkup:
    days = ["Пн","Вт","Ср","Чт","Пт","Сб","Вс"]
    row = []
    rows = []
    for d in days:
        mark = "✅" if d in selected else "⬜️"
        row.append(InlineKeyboardButton(f"{mark} {d}", callback_data=f"add:day:{d}"))
        if len(row)==4:
            rows.append(row); row=[]
    if row: rows.append(row)
    rows.append([InlineKeyboardButton("Дальше ▶", callback_data="add:days_next")])
    return InlineKeyboardMarkup(rows)

def parse_dt(s: str) -> datetime:
    return datetime.fromisoformat(s)

async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    start_add_conv(context)
    await send_screen_plain(update, context, "Название задачи?")
    return A_TITLE

async def add_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["add"]["title"] = update.message.text.strip()
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Фикс-время", callback_data="add:kind:fixed"),
         InlineKeyboardButton("Постоянная", callback_data="add:kind:const")],
        [InlineKeyboardButton("Гибкая (авто)", callback_data="add:kind:flex")]
    ])
    await send_screen_plain(update, context, "Тип задачи?")
    await send_keyboard(update, context, "Выберите тип:", kb)
    return A_KIND

async def add_kind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    kind = q.data.split(":")[-1]
    context.user_data["add"]["kind"] = kind
    if kind == "fixed":
        await send_screen_plain(update, context, "Начало (YYYY-MM-DD HH:MM)?")
        return A_FIXED_START
    if kind == "const":
        context.user_data["add"]["days_sel"] = []
        await send_screen_plain(update, context, "Выберите дни недели:")
        await send_keyboard(update, context, "Отметьте дни:", days_kb([]))
        return A_CONST_DAYS
    await send_screen_plain(update, context, "Дедлайн (YYYY-MM-DD HH:MM)?")
    return A_DEADLINE_FLEX

# Гибкая
async def add_deadline_flex(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data["add"]["deadline_at"] = parse_dt(update.message.text.strip())
    except Exception:
        await send_screen_plain(update, context, "Неверный формат, пример: 2025-10-30 18:00")
        return A_DEADLINE_FLEX
    await send_screen_plain(update, context, "Длительность в минутах?")
    return A_DURATION_FLEX

async def add_duration_flex(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data["add"]["duration_min"] = int(update.message.text.strip())
    except Exception:
        await send_screen_plain(update, context, "Введите целое число минут.")
        return A_DURATION_FLEX
    return await ask_effort(update, context)

# Фикс
async def add_fixed_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data["add"]["fixed_start"] = parse_dt(update.message.text.strip())
    except Exception:
        await send_screen_plain(update, context, "Неверный формат, пример: 2025-10-30 16:00")
        return A_FIXED_START
    await send_screen_plain(update, context, "Окончание (YYYY-MM-DD HH:MM)?")
    return A_FIXED_END

async def add_fixed_end(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data["add"]["fixed_end"] = parse_dt(update.message.text.strip())
    except Exception:
        await send_screen_plain(update, context, "Неверный формат, пример: 2025-10-30 17:30")
        return A_FIXED_END
    a = context.user_data["add"]
    duration_min = int((a["fixed_end"] - a["fixed_start"]).total_seconds() // 60)
    context.user_data["add"]["duration_min"] = duration_min
    return await ask_effort(update, context)

# Постоянная
async def add_days_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    _,_,d = q.data.split(":")
    sel = context.user_data["add"]["days_sel"]
    if d in sel: sel.remove(d)
    else: sel.append(d)
    await q.message.edit_reply_markup(reply_markup=days_kb(sel))
    return A_CONST_DAYS

async def add_days_next(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    sel = context.user_data["add"]["days_sel"]
    if not sel:
        await q.message.reply_text("Отметьте хотя бы один день недели.")
        return A_CONST_DAYS
    context.user_data["add"]["dow"] = parse_days_from_buttons(sel)
    await send_screen_plain(update, context, "Время начала (HH:MM)?")
    return A_CONST_TIME_START

async def add_const_time_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    try:
        h, m = map(int, txt.split(":"))
        context.user_data["add"]["constant_start_hm"] = (h, m)
    except Exception:
        await send_screen_plain(update, context, "Неверный формат, пример: 09:30")
        return A_CONST_TIME_START
    await send_screen_plain(update, context, "Время окончания (HH:MM)?")
    return A_CONST_TIME_END

async def add_const_time_end(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    try:
        h, m = map(int, txt.split(":"))
        context.user_data["add"]["constant_end_hm"] = (h, m)
    except Exception:
        await send_screen_plain(update, context, "Неверный формат, пример: 11:00")
        return A_CONST_TIME_END
    a = context.user_data["add"]
    sh, sm = a["constant_start_hm"]; eh, em = a["constant_end_hm"]
    start_dt = datetime.now().replace(hour=sh, minute=sm, second=0, microsecond=0)
    end_dt = datetime.now().replace(hour=eh, minute=em, second=0, microsecond=0)
    duration_min = max(0, int((end_dt - start_dt).total_seconds() // 60))
    context.user_data["add"]["duration_min"] = duration_min
    return await ask_effort(update, context)

# Общие шаги
async def ask_effort(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("quick", callback_data="add:eff:quick"),
         InlineKeyboardButton("medium", callback_data="add:eff:medium")],
        [InlineKeyboardButton("heavy", callback_data="add:eff:heavy"),
         InlineKeyboardButton("extreme", callback_data="add:eff:extreme")]
    ])
    await send_screen_plain(update, context, "Трудозатратность?")
    await send_keyboard(update, context, "Выберите трудозатратность:", kb)
    return A_EFFORT

async def add_effort(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    context.user_data["add"]["effort"] = q.data.split(":")[-1]
    kind = context.user_data["add"]["kind"]
    if kind == "flex":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Можно дробить? ✅", callback_data="add:split:yes"),
             InlineKeyboardButton("Нельзя дробить ❌", callback_data="add:split:no")]
        ])
        await send_screen_plain(update, context, "Дробить задачу на части?")
        await send_keyboard(update, context, "Выберите:", kb)
        return A_SPLIT
    else:
        context.user_data["add"]["splittable"] = False
        context.user_data["add"]["auto"] = False
        return await show_confirmation(update, context)

async def add_split(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    context.user_data["add"]["splittable"] = q.data.endswith("yes")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Автопланировать ✅", callback_data="add:auto:yes"),
         InlineKeyboardButton("Не автопланировать ❌", callback_data="add:auto:no")]
    ])
    await send_screen_plain(update, context, "Отмечать для автопланирования?")
    await send_keyboard(update, context, "Выберите:", kb)
    return A_AUTO

async def add_auto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    context.user_data["add"]["auto"] = q.data.endswith("yes")
    return await show_confirmation(update, context)

async def show_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    a = context.user_data["add"]
    lines = [
        f"Название: {a['title']}",
        f"Тип: {a['kind']}",
        f"Длительность: {a['duration_min']} мин",
        f"Трудозатратность: {a['effort']}",
        f"Дробить: {'да' if a.get('splittable') else 'нет'}",
        f"Автопланировать: {'да' if a.get('auto') else 'нет'}",
    ]
    if a["kind"]=="flex":
        lines.insert(2, f"Дедлайн: {a['deadline_at']:%Y-%m-%d %H:%M}")
    if a["kind"]=="fixed":
        lines.append(f"Фикс: {a['fixed_start']} — {a['fixed_end']}")
    if a["kind"]=="const":
        lines.append(f"Дни: {a['dow']}, Время: {a['constant_start_hm']}–{a['constant_end_hm']}")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Сохранить", callback_data="add:save"),
         InlineKeyboardButton("Отмена", callback_data="add:cancel")]
    ])
    await send_screen_plain(update, context, "Проверьте данные:\n" + "\n".join(lines))
    await send_keyboard(update, context, "Сохранить?", kb)
    return A_SAVE

async def add_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if q.data.endswith("cancel"):
        context.user_data.pop("add", None)
        await send_screen(update, context, "Отменено.")
        return ConversationHandler.END
    a = context.user_data["add"]
    kind = a["kind"]
    tid = uuid.uuid4().hex[:8]

    if kind == "fixed":
        deadline_for_store = a["fixed_end"]
    elif kind == "flex":
        deadline_for_store = a["deadline_at"]
    else:
        deadline_for_store = datetime.now()

    t = Task(
        id=tid,
        title=a["title"],
        duration_min=a["duration_min"],
        deadline_at=deadline_for_store,
        effort=a["effort"],
        splittable=a.get("splittable", False),
        auto=a.get("auto", False),
    )
    if kind=="fixed":
        t.fixed_start = a.get("fixed_start")
        t.fixed_end   = a.get("fixed_end")
    if kind=="const":
        t.constant = True
        t.dow = a["dow"]
        t.constant_start_hm = a["constant_start_hm"]
        t.constant_end_hm   = a["constant_end_hm"]
        t.auto = False
        t.splittable = False

    store = get_store(context, update.effective_chat.id)
    store["tasks"][tid] = ser_task(t)
    context.user_data.pop("add", None)
    await send_screen(update, context, f"Добавлено: [{tid}] {t.title}")
    return ConversationHandler.END

# ==================== Просроченные ====================
async def sweep_overdue(update_or_context, context: ContextTypes.DEFAULT_TYPE, now: datetime):
    chat_id = update_or_context.effective_chat.id if hasattr(update_or_context, "effective_chat") else context.job.chat_id
    store = get_store(context, chat_id)
    tasks = store["tasks"]
    moved = []
    for tid, d in list(tasks.items()):
        t = deser_task(d)
        if t.done or t.constant:
            continue
        anchor = t.fixed_end if t.fixed_end else t.deadline_at
        if anchor and anchor < now and not t.overdue:
            t.overdue = True
            store["overdue"][tid] = ser_task(t)
            tasks.pop(tid, None)
            moved.append((t.title, anchor))
    for title, anchor in moved:
        await context.bot.send_message(chat_id, f"Дедлайн «{title}» прошел {anchor:%Y-%m-%d %H:%M}. Задача перемещена в список просроченных.")

def overdue_row_kb(tid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🗓 Новый дедлайн", callback_data=f"od:setdl:{tid}"),
        InlineKeyboardButton("✅ Готово", callback_data=f"od:done:{tid}"),
        InlineKeyboardButton("🗑 Удалить", callback_data=f"od:del:{tid}"),
    ]])

async def show_overdue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    store = get_store(context, update.effective_chat.id)
    od = store.get("overdue", {})
    if not od:
        await send_screen(update, context, "Просроченных задач нет.")
        return
    await send_screen(update, context, "Просроченные задачи:")
    for tid, d in od.items():
        t = deser_task(d)
        msg = await update.effective_chat.send_message(f"[{tid}] {t.title}", reply_markup=overdue_row_kb(tid))
        context.user_data.setdefault("bot_messages", []).append(msg.message_id)

O_SET_DL = 1001

async def overdue_actions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    chat_id = update.effective_chat.id
    store = get_store(context, chat_id)
    od = store.setdefault("overdue", {})
    if not q.data.startswith("od:"):
        return
    _, action, tid = q.data.split(":")
    if tid not in od:
        return
    t = deser_task(od[tid])

    if action == "del":
        od.pop(tid, None)
        try: await q.message.delete()
        except Exception: pass
        return

    if action == "done":
        t.done = True
        store["history"].append(ser_done(DoneEntry(task=t, completed_at=datetime.now())))
        od.pop(tid, None)
        try: await q.message.delete()
        except Exception: pass
        return

    if action == "setdl":
        context.user_data["setdl_tid"] = tid
        await send_screen_plain(update, context, "Пришлите новый дедлайн (YYYY-MM-DD HH:MM)")
        return O_SET_DL

async def handle_setdl_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    try:
        new_dt = datetime.fromisoformat(txt)
    except Exception:
        await send_screen_plain(update, context, "Неверный формат, пример: 2025-10-30 18:00")
        return O_SET_DL
    tid = context.user_data.pop("setdl_tid", None)
    if not tid:
        await send_screen(update, context, "Не найден идентификатор задачи.")
        return ConversationHandler.END
    store = get_store(context, update.effective_chat.id)
    od = store.setdefault("overdue", {})
    if tid not in od:
        await send_screen(update, context, "Задача уже обновлена или удалена.")
        return ConversationHandler.END
    t = deser_task(od[tid])
    t.overdue = False
    t.deadline_at = new_dt
    t.planned_for = None
    store["tasks"][tid] = ser_task(t)
    od.pop(tid, None)
    await send_screen(update, context, f"Новый дедлайн установлен: {new_dt:%Y-%m-%d %H:%M}")
    return ConversationHandler.END

# ==================== Экраны ====================
async def show_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now()
    await sweep_overdue(update, context, now)
    store = get_store(context, update.effective_chat.id)
    tasks = [deser_task(d) for d in store["tasks"].values()]
    quote = stoic_quote_ru()
    plan = plan_today_assign_once(now, now, tasks, persist=True)
    # Сохраняем возможные обновления planned_for
    for t in tasks:
        store["tasks"][t.id] = ser_task(t)
    text = f"{quote}\n\nПлан на сегодня:\n{fmt_plan(plan)}"
    await send_screen(update, context, text)

async def show_week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now()
    await sweep_overdue(update, context, now)
    store = get_store(context, update.effective_chat.id)
    tasks = [deser_task(d) for d in store["tasks"].values()]
    start_day = now.replace(hour=12, minute=0, second=0, microsecond=0)
    week = plan_week_without_dup(start_day, now, tasks)
    parts = []
    for day_label, items in week.items():
        parts.append(f"— {day_label} —")
        parts.append(fmt_plan(items))
    await send_screen(update, context, "Недельный обзор:\n" + "\n".join(parts))

async def show_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    store = get_store(context, update.effective_chat.id)
    tasks_map = store["tasks"]
    tasks = [deser_task(d) for d in tasks_map.values()]
    now = datetime.now()
    await send_screen(update, context, "Список задач (🟩 — отмечена для автопланирования):\n" + fmt_tasks(tasks, now))
    for t in tasks:
        msg = await update.effective_chat.send_message(f"[{t.id}] {t.title}", reply_markup=task_row_buttons(t))
        context.user_data.setdefault("bot_messages", []).append(msg.message_id)

async def show_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    store = get_store(context, update.effective_chat.id)
    hist = [deser_done(d) for d in store["history"]]
    await send_screen(update, context, "История выполненных:\n" + fmt_history(hist))

# ==================== Операции над задачами ====================
async def task_actions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    chat_id = update.effective_chat.id
    store = get_store(context, chat_id)
    tasks = store["tasks"]
    parts = q.data.split(":")
    _, action, tid = parts
    if tid not in tasks:
        await q.message.reply_text("Задача не найдена.")
        return
    t = deser_task(tasks[tid])

    if action == "done":
        if not t.done:
            t.done = True
            store["history"].append(ser_done(DoneEntry(task=t, completed_at=datetime.now())))
        else:
            t.done = False
        tasks[tid] = ser_task(t)
        try: await q.message.delete()
        except Exception: pass
        return

    if action == "auto":
        t.auto = not t.auto
        tasks[tid] = ser_task(t)
        try: await q.message.edit_reply_markup(reply_markup=task_row_buttons(t))
        except Exception: pass
        return

    if action == "del":
        tasks.pop(tid, None)
        try: await q.message.delete()
        except Exception: pass
        return

    if action == "dup":
        context.user_data["add"] = {
            "title": t.title,
            "duration_min": t.duration_min,
            "deadline_at": t.deadline_at,
            "kind": "flex",
            "effort": t.effort,
            "splittable": t.splittable,
            "auto": True
        }
        try: await q.message.delete()
        except Exception: pass
        await update.effective_chat.send_message("База заполнена из истории, задайте недостающие поля.", reply_markup=main_menu_kb())
        return

# ==================== Меню и фоновые задачи ====================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["bot_messages"] = []
    if context.job_queue:
        chat_id = update.effective_chat.id
        for name in (f"daily-{chat_id}", f"watch-{chat_id}"):
            for job in context.job_queue.get_jobs_by_name(name):
                job.schedule_removal()
        context.job_queue.run_daily(morning_digest, time=time(7,30), chat_id=chat_id, name=f"daily-{chat_id}")
        context.job_queue.run_repeating(deadline_watchdog, interval=1800, first=10, chat_id=chat_id, name=f"watch-{chat_id}")
    await update.effective_chat.send_message("Главное меню:", reply_markup=main_menu_kb())

async def menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    key = q.data.split(":")[1]
    if key == "today":
        await show_today(update, context); return
    if key == "week":
        await show_week(update, context); return
    if key == "list":
        await show_list(update, context); return
    if key == "history":
        await show_history(update, context); return
    if key == "overdue":
        await show_overdue(update, context); return
    if key == "settings":
        await send_screen(update, context, "Настройки в разработке."); return

async def morning_digest(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now()
    await sweep_overdue(context, context, now)
    chat_id = context.job.chat_id
    store = get_store(context, chat_id)
    tasks = [deser_task(d) for d in store["tasks"].values()]
    quote = stoic_quote_ru()
    plan = plan_today_assign_once(now, now, tasks, persist=True)
    for t in tasks:
        store["tasks"][t.id] = ser_task(t)
    await context.bot.send_message(chat_id, f"{quote}\n\nПлан на сегодня:\n{fmt_plan(plan)}")

async def deadline_watchdog(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now()
    chat_id = context.job.chat_id
    store = get_store(context, chat_id)
    await sweep_overdue(context, context, now)
    for d in list(store["tasks"].values()):
        t = deser_task(d)
        if t.done or t.constant:
            continue
        anchor = t.fixed_end if t.fixed_end else t.deadline_at
        left = anchor - now
        if timedelta(hours=0) < left <= timedelta(hours=24):
            await context.bot.send_message(chat_id, f"Дедлайн «{t.title}» приближается! Нужно ускориться.")
        if timedelta(hours=0) < left <= timedelta(hours=4):
            await context.bot.send_message(chat_id, f"СРОЧНО: дедлайн «{t.title}» менее чем через 4 часа!")

# ==================== Точка входа ====================
def main():
    load_quotes()  # загрузить цитаты стоиков из файла quotes.json (UTF-8)
    persistence = PicklePersistence(filepath="state.pkl")
    app = Application.builder().token(BOT_TOKEN).persistence(persistence).build()

    app.add_handler(CommandHandler("start", start_cmd))

    # Диалог "Добавить задачу" — РАНЬШЕ общего меню
    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(add_start, pattern=r"^menu:add$")],
        states={
            A_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_title)],
            A_KIND: [CallbackQueryHandler(add_kind, pattern=r"^add:kind:")],
            A_DEADLINE_FLEX: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_deadline_flex)],
            A_DURATION_FLEX: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_duration_flex)],
            A_FIXED_START: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_fixed_start)],
            A_FIXED_END: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_fixed_end)],
            A_CONST_DAYS: [
                CallbackQueryHandler(add_days_select, pattern=r"^add:day:"),
                CallbackQueryHandler(add_days_next, pattern=r"^add:days_next$")
            ],
            A_CONST_TIME_START: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_const_time_start)],
            A_CONST_TIME_END: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_const_time_end)],
            A_EFFORT: [CallbackQueryHandler(add_effort, pattern=r"^add:eff:")],
            A_SPLIT: [CallbackQueryHandler(add_split, pattern=r"^add:split:")],
            A_AUTO: [CallbackQueryHandler(add_auto, pattern=r"^add:auto:")],
            A_SAVE: [CallbackQueryHandler(add_save, pattern=r"^add:(save|cancel)$")],
        },
        fallbacks=[],
        name="add_task_conv",
        persistent=True,
        per_chat=True,
        per_user=True,
        per_message=False,  # важно: ожидаем текст на «Название задачи?»
        allow_reentry=True  # разрешаем перезапуск мастера нажатием «Добавить»
    )
    app.add_handler(conv)

    # Короткий диалог «Новый дедлайн» для просроченных задач
    setdl_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(overdue_actions, pattern=r"^od:setdl:")],
        states={
            O_SET_DL: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_setdl_text)]
        },
        fallbacks=[],
        name="setdl_conv",
        persistent=True,
        per_chat=True,
        per_user=True,
        per_message=False
    )
    app.add_handler(setdl_conv)

    # Обработчики просроченных для «Готово» и «Удалить»
    app.add_handler(CallbackQueryHandler(overdue_actions, pattern=r"^od:(done|del):"))

    # Общее меню — после диалогов; не ловит «menu:add»
    app.add_handler(CallbackQueryHandler(menu_router, pattern=r"^menu:(?!add$)"))

    # Операции над задачами (готово/авто/удалить/на основе)
    app.add_handler(CallbackQueryHandler(task_actions, pattern=r"^task:(done|auto|del|dup):"))

    app.run_polling()

if __name__ == "__main__":
    load_quotes()  # загрузить цитаты стоиков из quotes.json (UTF-8)
    main()
