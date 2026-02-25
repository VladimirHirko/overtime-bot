# src/main.py
from __future__ import annotations

import re
import json
import csv
import io
from datetime import datetime
from typing import Optional

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

from src.config import load_settings
from src.db import DB
from src.handlers.admin_reset import build_admin_reset_conv


# ---------- Conversation states ----------
(OP_DATE, OP_HOURS, OP_COMMENT) = range(3)
(ADMIN_ADD_TG, ADMIN_ADD_NAME, ADMIN_ADD_ROLE, ADMIN_ADD_TEAM) = range(10, 14)
(ADMIN_EDIT_TG, ADMIN_EDIT_ROLE, ADMIN_EDIT_TEAM) = range(60, 63)
(ADMIN_DEL_TG, ADMIN_DEL_CONFIRM) = range(70, 72)
(ADMIN_ASSIGN_FOREMAN_TEAM, ADMIN_ASSIGN_FOREMAN_TG) = range(80, 82)

# (оставляем на будущее, если вернёшь reject в ConversationHandler)
(REJECT_REASON,) = (20,)

# reserved for future flows
(TEAM_CREATE_NAME,) = (30,)
(FOREMAN_ADJ_PICK_TG, FOREMAN_ADJ_TYPE, FOREMAN_ADJ_DATE, FOREMAN_ADJ_HOURS, FOREMAN_ADJ_COMMENT) = range(40, 45)


# ---------- UI helpers ----------
ROLE_TITLES = {
    "worker": "Сотрудник",
    "foreman": "Бригадир",
    "director": "Руководитель",
    "admin": "Админ",
}

WELCOME_TEXT = (
    "👋 Добро пожаловать в OvertimeBot!\n\n"
    "Здесь ты отправляешь заявки на переработку и списания часов.\n\n"
    "Как пользоваться:\n"
    "1) Нажми ➕ «Добавить часы» → введи дату → часы → комментарий.\n"
    "2) Нажми ➖ «Списать часы» → введи дату → часы → причину.\n"
    "3) «💼 Мой баланс» — текущий итог.\n"
    "4) «📄 Моя выписка» — последние операции.\n"
    "5) «⏳ Мои заявки» — что ещё не подтверждено.\n\n"
    "Важно:\n"
    "• Заявки подтверждает бригадир.\n"
    "• Комментарий пиши коротко, но понятно (объект/задача).\n\n"
    "ℹ️ В любой момент нажми «Помощь» — инструкция будет там."
)

BTN_ADMIN_MODE = "🛠 Админ-режим"
BTN_WORKER_MODE = "👷 Рабочий-режим"

# --- Foreman mode switch (ONLY for role=foreman) ---
BTN_FOREMAN_TO_WORKER = "👷 Режим работника"
BTN_FOREMAN_TO_FOREMAN = "👷‍♂️ Режим бригадира"

def main_menu(role: str, is_super_admin: bool = False, is_foreman: bool = False) -> ReplyKeyboardMarkup:
    if role == "worker":
        rows = [
            [KeyboardButton("➕ Добавить часы"), KeyboardButton("➖ Списать часы")],
            [KeyboardButton("💼 Мой баланс"), KeyboardButton("📄 Моя выписка")],
            [KeyboardButton("⏳ Мои заявки"), KeyboardButton("ℹ️ Помощь")],
        ]
        if is_super_admin:
            rows.append([KeyboardButton(BTN_ADMIN_MODE)])

        # ✅ Если реальная роль foreman и он сейчас в worker-режиме — даём кнопку вернуться
        if is_foreman:
            rows.append([KeyboardButton(BTN_FOREMAN_TO_FOREMAN)])

    elif role == "foreman":
        rows = [
            [KeyboardButton("⏳ Заявки на подтверждение"), KeyboardButton("👥 Команда (балансы)")],
            [KeyboardButton("➕➖ Корректировка сотруднику"), KeyboardButton("📄 Выписка сотрудника")],
            [KeyboardButton("ℹ️ Помощь")],
        ]
        if is_super_admin:
            rows.append([KeyboardButton(BTN_ADMIN_MODE)])

        # ✅ Бригадир может уйти в режим работника
        if is_foreman:
            rows.append([KeyboardButton(BTN_FOREMAN_TO_WORKER)])

    elif role == "director":
        rows = [
            [KeyboardButton("🧾 Лента событий"), KeyboardButton("👥 Все сотрудники (балансы)")],
            [KeyboardButton("👤 Карточка сотрудника"), KeyboardButton("ℹ️ Помощь")],
        ]
        if is_super_admin:
            rows.append([KeyboardButton(BTN_ADMIN_MODE)])

    else:  # admin (UI-mode)
        rows = [
            [KeyboardButton("➕ Добавить пользователя"), KeyboardButton("👥 Пользователи")],
            [KeyboardButton("✏️ Изменить пользователя"), KeyboardButton("🗑️ Удалить пользователя")],
            [KeyboardButton("🏗️ Команды"), KeyboardButton("👤 Назначить бригадира")],
            [KeyboardButton("🧨 Сброс БД")],
            [KeyboardButton("ℹ️ Помощь")],
        ]
        if is_super_admin:
            rows.append([KeyboardButton(BTN_WORKER_MODE)])

    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

def parse_date(text: str) -> Optional[str]:
    """
    Accept:
      - YYYY-MM-DD
      - DD.MM.YYYY
      - DD/MM/YYYY
    Return ISO YYYY-MM-DD
    """
    text = text.strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", text)
    if m:
        try:
            dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            return None

    m = re.match(r"^(\d{2})[./](\d{2})[./](\d{4})$", text)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            dt = datetime(y, mo, d)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            return None

    return None


def parse_hours(text: str) -> Optional[float]:
    text = text.strip().replace(",", ".")
    try:
        v = float(text)
    except ValueError:
        return None
    if v <= 0:
        return None
    return round(v, 2)


# ---------- App globals ----------
settings = load_settings()
db = DB(settings.database_url)


# ---------- Auth helpers ----------
def ensure_user_or_deny(update: Update, tg_id: int, full_name: str) -> dict | None:
    user = db.get_user_by_tg(tg_id)

    # 🔒 Супер-админ: НЕ меняем роль в БД.
    # Просто гарантируем, что запись пользователя есть.
    if tg_id in settings.admin_tg_ids:
        if user:
            return user
        # если записи нет — создаём как worker (реальная роль)
        db.create_user(tg_id, full_name, "worker", None)
        return db.get_user_by_tg(tg_id)

    return user

def resolve_role_to_show(user: dict, tg_id: int, context: ContextTypes.DEFAULT_TYPE) -> str:
    """
    1) Super-admin может переключать UI через context.user_data['ui_role'] (как сейчас).
    2) Foreman переключает режим через users.view_mode в БД.
    3) Worker/Director по умолчанию показываются как их роль.
    """
    db_role = user["role"]
    is_super_admin = tg_id in settings.admin_tg_ids

    # 1) super-admin ui override (только для tg_id из списка)
    ui_role = context.user_data.get("ui_role")
    if ui_role not in ("worker", "foreman", "director", "admin"):
        ui_role = None

    if is_super_admin and ui_role:
        return ui_role

    # 2) foreman mode from DB
    if db_role == "foreman":
        vm = user.get("view_mode")
        if vm in ("worker", "foreman"):
            return vm
        return "foreman"

    # 3) default
    return db_role

async def send_to_directors(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    director_ids = db.list_director_tg_ids()
    if not director_ids:
        return

    for tg_id in director_ids:
        try:
            await context.bot.send_message(chat_id=int(tg_id), text=text)
        except Exception:
            pass


# ---------- Handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg = update.effective_user
    if not tg or not update.message:
        return

    tg_id = tg.id
    full_name = (tg.full_name or "").strip() or "Без имени"

    user = ensure_user_or_deny(update, tg_id, full_name)
    if not user:
        await update.message.reply_text(
            "⛔ Доступ запрещён.\n\n"
            "Попросите администратора добавить вас в систему.\n"
            f"Ваш Telegram ID: {tg_id}"
        )
        return

    is_super_admin = tg_id in settings.admin_tg_ids
    db_role = user["role"]

    role_to_show = resolve_role_to_show(user, tg_id, context)

    # защита: если кто-то НЕ супер-админ, но в ui_role оказался admin — откатываем
    if role_to_show == "admin" and not is_super_admin:
        role_to_show = db_role
        context.user_data["ui_role"] = db_role

        # приветствие один раз (для всех ролей или только worker — решай)
    if not db.has_seen_welcome(tg_id):
        await update.message.reply_text(WELCOME_TEXT)
        db.mark_welcome_seen(tg_id)

    await update.message.reply_text(
        f"✅ Роль (в базе): {ROLE_TITLES.get(db_role, db_role)}\n"
        f"🧩 Режим: {ROLE_TITLES.get(role_to_show, role_to_show)}\n"
        f"👤 {user['full_name']}\n\n"
        "Выберите действие кнопками ниже.",
        reply_markup=main_menu(
            role_to_show,
            is_super_admin=is_super_admin,
            is_foreman=(db_role == "foreman"),
        ),
    )

async def start_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # /start как fallback должен ЗАКРЫТЬ любой ConversationHandler
    await start(update, context)
    return ConversationHandler.END

async def mode_switch_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # Нажали кнопку режима во время диалога — сначала обработаем переключение,
    # затем принудительно завершим ConversationHandler
    await router(update, context)
    return ConversationHandler.END

async def help_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg = update.effective_user
    if not tg or not update.message:
        return

    user = db.get_user_by_tg(tg.id)
    if not user:
        await update.message.reply_text("Напишите /start или нажмите «Старт».")
        return

    is_super_admin = tg.id in settings.admin_tg_ids
    db_role = user["role"]
    role_to_show = resolve_role_to_show(user, tg.id, context)

    if role_to_show == "admin" and not is_super_admin:
        role_to_show = db_role
        context.user_data["ui_role"] = db_role

    if role_to_show == "worker":
        text = (
            "ℹ️ Помощь (Сотрудник)\n\n"
            "➕ Добавить часы — отправить заявку на начисление.\n"
            "➖ Списать часы — отправить заявку на списание (баланс может стать отрицательным).\n"
            "💼 Мой баланс — текущий итог.\n"
            "📄 Моя выписка — последние операции.\n"
            "⏳ Мои заявки — ожидающие решения.\n"
        )
    elif role_to_show == "foreman":
        text = (
            "ℹ️ Помощь (Бригадир)\n\n"
            "⏳ Заявки на подтверждение — подтвердить или отклонить.\n"
            "👥 Команда (балансы) — сводка по сотрудникам.\n"
            "➕➖ Корректировка сотруднику — вручную добавить/списать (с причиной).\n"
        )
    elif role_to_show == "director":
        text = (
            "ℹ️ Помощь (Руководитель)\n\n"
            "🧾 Лента событий — кто что запросил/кто подтвердил.\n"
            "👥 Все сотрудники (балансы) — сводка.\n"
            "👤 Карточка сотрудника — баланс + выписка.\n"
            "Роль только для просмотра.\n"
        )
    else:
        text = (
            "ℹ️ Помощь (Админ)\n\n"
            "➕ Добавить пользователя — добавить tg_id, имя, роль, команда.\n"
            "🏗️ Команды — создать/посмотреть команды.\n"
            "👤 Назначить бригадира — привязать бригадира к команде.\n"
        )

    await update.message.reply_text(
        text,
        reply_markup=main_menu(role_to_show, is_super_admin=is_super_admin, is_foreman=(db_role == "foreman")),
    )

# ---------- Worker: create operation (credit/debit) ----------
async def op_start_credit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("op_date", None)
    context.user_data.pop("hours", None)
    context.user_data["op_type"] = "credit"
    await update.message.reply_text("Введите дату (например 21.02.2026 или 2026-02-21):")
    return OP_DATE


async def op_start_debit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("op_date", None)
    context.user_data.pop("hours", None)
    context.user_data["op_type"] = "debit"
    await update.message.reply_text("Введите дату (например 21.02.2026 или 2026-02-21):")
    return OP_DATE


async def op_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    iso = parse_date(text)
    if not iso:
        await update.message.reply_text("❌ Не понял дату. Пример: 21.02.2026 или 2026-02-21. Повторите:")
        return OP_DATE
    context.user_data["op_date"] = iso
    await update.message.reply_text("Введите количество часов (например 1, 2.5):")
    return OP_HOURS


async def op_hours(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    hours = parse_hours(text)
    if hours is None:
        await update.message.reply_text("❌ Не понял часы. Пример: 1 или 2.5. Повторите:")
        return OP_HOURS
    context.user_data["hours"] = hours
    await update.message.reply_text("Введите комментарий (что именно и где):")
    return OP_COMMENT


async def op_comment_finish(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    tg = update.effective_user
    if not tg or not update.message:
        return ConversationHandler.END

    user = db.get_user_by_tg(tg.id)
    if not user:
        await update.message.reply_text("⛔ Нет доступа. Напишите /start или нажмите «Старт».")
        return ConversationHandler.END

    comment = (update.message.text or "").strip()
    op_type = context.user_data.get("op_type")
    op_date = context.user_data.get("op_date")
    hours = float(context.user_data.get("hours", 0))

    # защитная проверка типа операции
    if op_type not in ("credit", "debit"):
        await update.message.reply_text("❌ Не выбран тип операции (➕/➖). Начните заново кнопками меню.")
        return ConversationHandler.END

    op = db.create_operation(
        target_user_id=user["id"],
        created_by_user_id=user["id"],
        op_type=op_type,
        op_date=op_date,
        hours=hours,
        comment=comment,
        status="pending",
    )

    db.log_event(
        actor_user_id=user["id"],
        event="request_created",
        entity="operation",
        entity_id=op["id"],
        meta={"tg_id": tg.id, "op_type": op_type, "hours": hours, "date": op_date},
    )

    # Notify foreman (team-based)
    if user.get("team_id"):
        foreman_tg = db.get_team_foreman_tg(int(user["team_id"]))
    else:
        foreman_tg = None

    # ✅ правильный режим меню (worker/foreman) + супер-админ переключатель остаётся как раньше
    db_role = user["role"]
    is_super_admin = tg.id in settings.admin_tg_ids
    role_to_show = resolve_role_to_show(user, tg.id, context)

    sign = "➕" if op_type == "credit" else "➖"
    await update.message.reply_text(
        f"✅ Заявка отправлена бригадиру.\n"
        f"{sign} {hours} ч • {op_date}\n"
        f"Комментарий: {comment}",
        reply_markup=main_menu(
            role_to_show,
            is_super_admin=is_super_admin,
            is_foreman=(db_role == "foreman"),
        ),
    )

    # Foreman message with inline buttons
    if foreman_tg:
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Подтвердить", callback_data=f"approve:{op['id']}"),
                    InlineKeyboardButton("❌ Отклонить", callback_data=f"reject:{op['id']}"),
                ]
            ]
        )
        bal_now = db.calc_balance_hours(user["id"])
        bal_after = bal_now + (hours if op_type == "credit" else -hours)

        text = (
            "🆕 Новая заявка\n"
            f"Сотрудник: {user['full_name']}\n"
            f"Тип: {sign}\n"
            f"Дата: {op_date}\n"
            f"Часы: {hours}\n"
            f"Комментарий: {comment}\n\n"
            f"Баланс сейчас: {bal_now} ч\n"
            f"Баланс после: {round(bal_after, 2)} ч"
        )
        try:
            await context.bot.send_message(chat_id=foreman_tg, text=text, reply_markup=kb)
        except Exception:
            pass

    await send_to_directors(
        context,
        "🧾 Событие: создана заявка\n"
        f"{user['full_name']} {sign}{hours}ч на {op_date}"
    )

    return ConversationHandler.END


async def my_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg = update.effective_user
    if not tg or not update.message:
        return

    user = db.get_user_by_tg(tg.id)
    if not user:
        await update.message.reply_text("⛔ Нет доступа. Напишите /start или нажмите «Старт».")
        return

    # ✅ правильный режим меню (worker/foreman) + супер-админ переключатель остаётся как раньше
    db_role = user["role"]
    is_super_admin = tg.id in settings.admin_tg_ids
    role_to_show = resolve_role_to_show(user, tg.id, context)

    bal = db.calc_balance_hours(user["id"])
    status = "долг" if bal < 0 else "доступно"

    await update.message.reply_text(
        f"💼 Баланс: {bal} ч ({status})",
        reply_markup=main_menu(
            role_to_show,
            is_super_admin=is_super_admin,
            is_foreman=(db_role == "foreman"),
        ),
    )


async def my_pending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg = update.effective_user
    if not tg or not update.message:
        return

    user = db.get_user_by_tg(tg.id)
    if not user:
        await update.message.reply_text("⛔ Нет доступа. Напишите /start или нажмите «Старт».")
        return

    # ✅ правильный режим меню (worker/foreman) + супер-админ переключатель
    db_role = user["role"]
    is_super_admin = tg.id in settings.admin_tg_ids
    role_to_show = resolve_role_to_show(user, tg.id, context)

    rows = db.execute(
        """
        SELECT id, op_type, op_date, hours, comment, status
        FROM operations
        WHERE target_user_id={p} AND status='pending'
        ORDER BY created_at DESC
        LIMIT 20
        """,
        (user["id"],),
        fetch="all",
    )

    if not rows:
        await update.message.reply_text(
            "⏳ У вас нет заявок в ожидании.",
            reply_markup=main_menu(
                role_to_show,
                is_super_admin=is_super_admin,
                is_foreman=(db_role == "foreman"),
            ),
        )
        return

    lines = ["⏳ Ваши заявки (ожидают):"]
    for r in rows:
        sign = "➕" if r["op_type"] == "credit" else "➖"
        comment = (r.get("comment") or "").strip()
        if len(comment) > 40:
            comment = comment[:40] + "…"
        lines.append(f"#{r['id']} {sign}{r['hours']}ч • {r['op_date']} • {comment}")

    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=main_menu(
            role_to_show,
            is_super_admin=is_super_admin,
            is_foreman=(db_role == "foreman"),
        ),
    )


async def my_statement(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg = update.effective_user
    if not tg or not update.message:
        return

    user = db.get_user_by_tg(tg.id)
    if not user:
        await update.message.reply_text("⛔ Нет доступа. Напишите /start или нажмите «Старт».")
        return

    # ✅ правильный режим меню (worker/foreman) + супер-админ переключатель
    db_role = user["role"]
    is_super_admin = tg.id in settings.admin_tg_ids
    role_to_show = resolve_role_to_show(user, tg.id, context)

    bal = db.calc_balance_hours(user["id"])
    rows = db.list_statement(user["id"], limit=20)

    if not rows:
        await update.message.reply_text(
            "Пока нет операций.",
            reply_markup=main_menu(
                role_to_show,
                is_super_admin=is_super_admin,
                is_foreman=(db_role == "foreman"),
            ),
        )
        return

    header = [
        "📄 Выписка (последние 20)",
        "Дата   Тип  Часы  Статус   Комментарий",
    ]

    body: list[str] = []
    for r in rows:
        sign = "+" if r["op_type"] == "credit" else "-"
        dt = str(r["op_date"])[:10]
        st = str(r["status"])
        c = (r.get("comment") or "").replace("\n", " ").strip()
        if len(c) > 28:
            c = c[:28] + "…"
        body.append(f"{dt[5:]}  {sign}   {r['hours']}   {st:8} {c}")

    footer = [f"\nИтоговый баланс: {bal} ч"]

    await update.message.reply_text(
        "\n".join(header + body + footer),
        reply_markup=main_menu(
            role_to_show,
            is_super_admin=is_super_admin,
            is_foreman=(db_role == "foreman"),
        ),
    )

# ---------- Foreman: pending approvals ----------
async def foreman_pending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg = update.effective_user
    if not tg or not update.message:
        return
    user = db.get_user_by_tg(tg.id)
    if not user or user["role"] not in ("foreman", "admin"):
        await update.message.reply_text("⛔ Недостаточно прав.")
        return

    pending = db.list_pending_for_foreman(user["id"])
    if not pending:
        await update.message.reply_text("⏳ Нет заявок в ожидании.", reply_markup=main_menu(user["role"]))
        return

    await update.message.reply_text(f"⏳ Заявки в ожидании: {len(pending)}\nОткройте по одной ниже:")

    for op in pending[:10]:  # avoid spam
        sign = "➕" if op["op_type"] == "credit" else "➖"
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Подтвердить", callback_data=f"approve:{op['id']}"),
                    InlineKeyboardButton("❌ Отклонить", callback_data=f"reject:{op['id']}"),
                ]
            ]
        )
        bal_now = db.calc_balance_hours(op["target_user_id"])
        bal_after = bal_now + (float(op["hours"]) if op["op_type"] == "credit" else -float(op["hours"]))
        text = (
            f"Заявка #{op['id']}\n"
            f"Сотрудник: {op['target_name']}\n"
            f"{sign} {op['hours']} ч • {op['op_date']}\n"
            f"Комментарий: {op['comment']}\n\n"
            f"Баланс сейчас: {bal_now} ч\n"
            f"Баланс после: {round(bal_after, 2)} ч"
        )
        await update.message.reply_text(text, reply_markup=kb)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    if not q:
        return ConversationHandler.END
    await q.answer()

    tg = update.effective_user
    if not tg:
        return ConversationHandler.END

    user = db.get_user_by_tg(tg.id)
    if not user or user["role"] not in ("foreman", "admin"):
        await q.edit_message_text("⛔ Недостаточно прав.")
        return ConversationHandler.END

    data = q.data or ""
    if data.startswith("approve:"):
        op_id = int(data.split(":", 1)[1])
        op = db.get_operation(op_id)
        if not op or op["status"] != "pending":
            await q.edit_message_text("Эта заявка уже обработана.")
            return ConversationHandler.END

        db.approve_operation(op_id, user["id"])

        target = db.execute(
            "SELECT tg_id, full_name FROM users WHERE id={p}",
            (op["target_user_id"],),
            fetch="one",
        )
        bal = db.calc_balance_hours(op["target_user_id"])
        sign = "➕" if op["op_type"] == "credit" else "➖"
        msg_worker = (
            f"✅ Ваша заявка #{op_id} одобрена.\n"
            f"{sign}{op['hours']}ч • {op['op_date']}\n"
            f"Баланс теперь: {bal} ч"
        )
        if target:
            try:
                await context.bot.send_message(chat_id=int(target["tg_id"]), text=msg_worker)
            except Exception:
                pass

        db.log_event(
            actor_user_id=user["id"],
            event="request_approved",
            entity="operation",
            entity_id=op_id,
            meta={"op_type": op["op_type"], "hours": float(op["hours"]), "target_user_id": op["target_user_id"]},
        )
        await send_to_directors(
            context,
            f"🧾 Одобрено: {target['full_name'] if target else 'сотрудник'} "
            f"{sign}{op['hours']}ч ({op['op_date']})"
        )

        await q.edit_message_text(f"✅ Одобрено. Баланс сотрудника: {bal} ч")
        return ConversationHandler.END

    if data.startswith("reject:"):
        op_id = int(data.split(":", 1)[1])
        context.user_data["await_reject_reason_op_id"] = op_id
        await q.edit_message_text("❌ Отклонение: напишите причину одним сообщением.")
        return ConversationHandler.END

    return ConversationHandler.END


async def reject_reason(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    tg = update.effective_user
    if not tg or not update.message:
        return ConversationHandler.END

    user = db.get_user_by_tg(tg.id)
    if not user or user["role"] not in ("foreman", "admin"):
        await update.message.reply_text("⛔ Недостаточно прав.")
        return ConversationHandler.END

    reason = (update.message.text or "").strip()
    if len(reason) < 3:
        # НЕ ConversationHandler: просто снова попросим причину
        op_id = context.user_data.get("reject_op_id")
        if op_id:
            context.user_data["await_reject_reason_op_id"] = op_id
        await update.message.reply_text("Причина слишком короткая. Напишите подробнее:")
        return ConversationHandler.END

    op_id = int(context.user_data.get("reject_op_id", 0))
    op = db.get_operation(op_id)
    if not op or op["status"] != "pending":
        await update.message.reply_text("Эта заявка уже обработана.")
        return ConversationHandler.END

    db.reject_operation(op_id, user["id"], reason)
    target = db.execute("SELECT tg_id, full_name FROM users WHERE id={p}", (op["target_user_id"],), fetch="one")
    bal = db.calc_balance_hours(op["target_user_id"])
    sign = "➕" if op["op_type"] == "credit" else "➖"

    if target:
        try:
            await context.bot.send_message(
                chat_id=int(target["tg_id"]),
                text=(
                    f"❌ Ваша заявка #{op_id} отклонена.\n"
                    f"{sign}{op['hours']}ч • {op['op_date']}\n"
                    f"Причина: {reason}\n"
                    f"Баланс: {bal} ч"
                ),
            )
        except Exception:
            pass

    db.log_event(
        actor_user_id=user["id"],
        event="request_rejected",
        entity="operation",
        entity_id=op_id,
        meta={
            "op_type": op["op_type"],
            "hours": float(op["hours"]),
            "target_user_id": op["target_user_id"],
            "reason": reason,
        },
    )

    await send_to_directors(
        context,
        f"🧾 Отклонено: {target['full_name'] if target else 'сотрудник'} "
        f"{sign}{op['hours']}ч ({op['op_date']})\nПричина: {reason}"
    )

    await update.message.reply_text("✅ Готово. Заявка отклонена и сотрудник уведомлён.", reply_markup=main_menu(user["role"]))
    return ConversationHandler.END


# ---------- Foreman: team balances ----------
async def foreman_team_balances(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg = update.effective_user
    if not tg or not update.message:
        return
    user = db.get_user_by_tg(tg.id)
    if not user or user["role"] not in ("foreman", "admin"):
        await update.message.reply_text("⛔ Недостаточно прав.")
        return

    if user["role"] == "admin":
        rows = db.execute(
            "SELECT id, full_name, role, team_id FROM users WHERE role='worker' ORDER BY full_name",
            fetch="all",
        )
        title = "👥 Балансы сотрудников (все)"
    else:
        team_id = user.get("team_id")
        if not team_id:
            await update.message.reply_text("У вас не назначена команда. Обратитесь к админу.")
            return
        rows = db.execute(
            "SELECT id, full_name FROM users WHERE role='worker' AND team_id={p} ORDER BY full_name",
            (team_id,),
            fetch="all",
        )
        title = "👥 Балансы команды"

    if not rows:
        await update.message.reply_text("Сотрудников не найдено.")
        return

    lines = [title]
    for r in rows:
        bal = db.calc_balance_hours(r["id"])
        lines.append(f"• {r['full_name']}: {bal} ч")
    await update.message.reply_text("\n".join(lines), reply_markup=main_menu(user["role"]))

# ---------- Foreman: manual adjustment ----------
async def foreman_adj_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    tg = update.effective_user
    if not tg or not update.message:
        return ConversationHandler.END

    user = db.get_user_by_tg(tg.id)
    if not user or user["role"] not in ("foreman", "admin"):
        await update.message.reply_text("⛔ Недостаточно прав.")
        return ConversationHandler.END

    context.user_data.pop("adj_target_user_id", None)
    context.user_data.pop("adj_op_type", None)
    context.user_data.pop("adj_op_date", None)
    context.user_data.pop("adj_hours", None)

    if user["role"] == "admin":
        workers = db.execute(
            "SELECT id, tg_id, full_name FROM users WHERE role='worker' ORDER BY full_name",
            fetch="all",
        )
        title = "Выберите сотрудника (все работники):"
    else:
        workers = db.list_workers_for_foreman(user["id"])
        title = "Выберите сотрудника вашей команды:"

    if not workers:
        await update.message.reply_text("Сотрудников не найдено или у вас не назначена команда.")
        return ConversationHandler.END

    buttons = []
    for w in workers[:60]:
        buttons.append([InlineKeyboardButton(w["full_name"], callback_data=f"adj_pick:{w['id']}")])

    await update.message.reply_text(title, reply_markup=InlineKeyboardMarkup(buttons))
    return FOREMAN_ADJ_PICK_TG


async def foreman_adj_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    if not q:
        return ConversationHandler.END
    await q.answer()

    tg = update.effective_user
    if not tg:
        return ConversationHandler.END

    user = db.get_user_by_tg(tg.id)
    if not user or user["role"] not in ("foreman", "admin"):
        await q.edit_message_text("⛔ Недостаточно прав.")
        return ConversationHandler.END

    data = q.data or ""
    try:
        target_user_id = int(data.split(":", 1)[1])
    except Exception:
        await q.edit_message_text("Ошибка выбора сотрудника.")
        return ConversationHandler.END

    # security: foreman может править только свою команду
    if user["role"] == "foreman":
        allowed_user = db.execute(
            "SELECT team_id FROM users WHERE id={p} AND role='worker'",
            (target_user_id,),
            fetch="one",
        )
        if not allowed_user or int(allowed_user.get("team_id") or 0) != int(user.get("team_id") or 0):
            await q.edit_message_text("⛔ Нельзя корректировать сотрудника вне вашей команды.")
            return ConversationHandler.END

    context.user_data["adj_target_user_id"] = target_user_id

    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➕ Добавить часы", callback_data="adj_type:credit")],
            [InlineKeyboardButton("➖ Списать часы", callback_data="adj_type:debit")],
        ]
    )
    await q.edit_message_text("Выберите тип корректировки:", reply_markup=kb)
    return FOREMAN_ADJ_TYPE


async def foreman_adj_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    if not q:
        return ConversationHandler.END
    await q.answer()

    data = q.data or ""
    if not data.startswith("adj_type:"):
        await q.edit_message_text("Ошибка выбора типа.")
        return ConversationHandler.END

    op_type = data.split(":", 1)[1]
    if op_type not in ("credit", "debit"):
        await q.edit_message_text("Ошибка выбора типа.")
        return ConversationHandler.END

    context.user_data["adj_op_type"] = op_type
    await q.edit_message_text("Введите дату (например 21.02.2026 или 2026-02-21):")
    return FOREMAN_ADJ_DATE


async def foreman_adj_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message:
        return ConversationHandler.END

    iso = parse_date((update.message.text or "").strip())
    if not iso:
        await update.message.reply_text("❌ Не понял дату. Пример: 21.02.2026 или 2026-02-21. Повторите:")
        return FOREMAN_ADJ_DATE

    context.user_data["adj_op_date"] = iso
    await update.message.reply_text("Введите количество часов (например 1 или 2.5):")
    return FOREMAN_ADJ_HOURS


async def foreman_adj_hours(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message:
        return ConversationHandler.END

    hours = parse_hours((update.message.text or "").strip())
    if hours is None:
        await update.message.reply_text("❌ Не понял часы. Пример: 1 или 2.5. Повторите:")
        return FOREMAN_ADJ_HOURS

    context.user_data["adj_hours"] = float(hours)
    await update.message.reply_text("Введите причину/комментарий (обязательно):")
    return FOREMAN_ADJ_COMMENT


async def foreman_adj_comment_finish(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    tg = update.effective_user
    if not tg or not update.message:
        return ConversationHandler.END

    actor = db.get_user_by_tg(tg.id)
    if not actor or actor["role"] not in ("foreman", "admin"):
        await update.message.reply_text("⛔ Недостаточно прав.")
        return ConversationHandler.END

    target_user_id = int(context.user_data.get("adj_target_user_id", 0))
    op_type = context.user_data.get("adj_op_type")
    op_date = context.user_data.get("adj_op_date")
    hours = float(context.user_data.get("adj_hours", 0))
    comment = (update.message.text or "").strip()

    if not target_user_id or op_type not in ("credit", "debit") or not op_date or hours <= 0:
        await update.message.reply_text("Ошибка данных корректировки. Начните заново.")
        return ConversationHandler.END

    if len(comment) < 3:
        await update.message.reply_text("Причина слишком короткая. Напишите подробнее:")
        return FOREMAN_ADJ_COMMENT

    # security: foreman only own team
    if actor["role"] == "foreman":
        allowed_user = db.execute(
            "SELECT team_id FROM users WHERE id={p} AND role='worker'",
            (target_user_id,),
            fetch="one",
        )
        if not allowed_user or int(allowed_user.get("team_id") or 0) != int(actor.get("team_id") or 0):
            await update.message.reply_text("⛔ Нельзя корректировать сотрудника вне вашей команды.")
            return ConversationHandler.END

    op = db.create_adjustment_operation(
        target_user_id=target_user_id,
        created_by_user_id=actor["id"],
        op_type=op_type,
        op_date=op_date,
        hours=hours,
        comment=comment,
    )

    db.log_event(
        actor_user_id=actor["id"],
        event="manual_adjustment",
        entity="operation",
        entity_id=op["id"],
        meta={
            "target_user_id": target_user_id,
            "op_type": op_type,
            "hours": hours,
            "date": op_date,
            "comment": comment,
        },
    )

    target = db.execute(
        "SELECT tg_id, full_name FROM users WHERE id={p}",
        (target_user_id,),
        fetch="one",
    )

    sign = "➕" if op_type == "credit" else "➖"
    bal = db.calc_balance_hours(target_user_id)

    await update.message.reply_text(
        "✅ Корректировка выполнена.\n"
        f"Сотрудник: {target['full_name'] if target else target_user_id}\n"
        f"{sign} {hours} ч • {op_date}\n"
        f"Причина: {comment}\n"
        f"Баланс сотрудника: {bal} ч"
    )

    if target:
        try:
            await context.bot.send_message(
                chat_id=int(target["tg_id"]),
                text=(
                    "📌 Корректировка от бригадира/админа\n"
                    f"{sign}{hours}ч • {op_date}\n"
                    f"Причина: {comment}\n"
                    f"Баланс: {bal} ч"
                ),
            )
        except Exception:
            pass

    await send_to_directors(
        context,
        f"🧾 Корректировка: {target['full_name'] if target else 'сотрудник'} "
        f"{sign}{hours}ч ({op_date})"
    )

    return ConversationHandler.END

# ---------- Foreman: employee statement ----------
async def foreman_stmt_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    tg = update.effective_user
    if not tg or not update.message:
        return ConversationHandler.END

    actor = db.get_user_by_tg(tg.id)
    if not actor or actor["role"] not in ("foreman", "admin"):
        await update.message.reply_text("⛔ Недостаточно прав.")
        return ConversationHandler.END

    context.user_data.pop("stmt_target_user_id", None)

    if actor["role"] == "admin":
        workers = db.execute(
            "SELECT id, full_name FROM users WHERE role='worker' ORDER BY full_name",
            fetch="all",
        )
        title = "Выберите сотрудника (все работники):"
    else:
        workers = db.list_workers_for_foreman(actor["id"])
        title = "Выберите сотрудника вашей команды:"

    if not workers:
        await update.message.reply_text("Сотрудников не найдено или у вас не назначена команда.")
        return ConversationHandler.END

    buttons = []
    for w in workers[:60]:
        buttons.append([InlineKeyboardButton(w["full_name"], callback_data=f"stmt_pick:{w['id']}")])

    await update.message.reply_text(title, reply_markup=InlineKeyboardMarkup(buttons))
    return ConversationHandler.END


async def foreman_stmt_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    if not q:
        return ConversationHandler.END
    await q.answer()

    tg = update.effective_user
    if not tg:
        return ConversationHandler.END

    actor = db.get_user_by_tg(tg.id)
    if not actor or actor["role"] not in ("foreman", "admin"):
        await q.edit_message_text("⛔ Недостаточно прав.")
        return ConversationHandler.END

    data = q.data or ""
    try:
        target_user_id = int(data.split(":", 1)[1])
    except Exception:
        await q.edit_message_text("Ошибка выбора сотрудника.")
        return ConversationHandler.END

    # security: foreman only own team
    if actor["role"] == "foreman":
        allowed_user = db.execute(
            "SELECT team_id FROM users WHERE id={p} AND role='worker'",
            (target_user_id,),
            fetch="one",
        )
        if not allowed_user or int(allowed_user.get("team_id") or 0) != int(actor.get("team_id") or 0):
            await q.edit_message_text("⛔ Нельзя смотреть сотрудника вне вашей команды.")
            return ConversationHandler.END

    target = db.execute(
        "SELECT full_name FROM users WHERE id={p}",
        (target_user_id,),
        fetch="one",
    )
    name = target["full_name"] if target else f"#{target_user_id}"

    bal = db.calc_balance_hours(target_user_id)
    rows = db.list_statement(target_user_id, limit=20)

    lines = [f"📄 Выписка сотрудника: {name}", f"💼 Баланс: {bal} ч", ""]
    if not rows:
        lines.append("Операций пока нет.")
        await q.edit_message_text("\n".join(lines))
        return ConversationHandler.END

    lines.append("Дата   Тип  Часы  Статус   Комментарий")
    for r in rows:
        sign = "+" if r["op_type"] == "credit" else "-"
        dt = str(r["op_date"])[:10]
        st = r["status"]
        c = (r["comment"] or "").replace("\n", " ").strip()
        if len(c) > 28:
            c = c[:28] + "…"
        lines.append(f"{dt[5:]}  {sign}   {r['hours']}   {st:8} {c}")

    await q.edit_message_text("\n".join(lines))
    return ConversationHandler.END

# ---------- Director views ----------
async def director_feed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg = update.effective_user
    if not tg or not update.message:
        return

    user = db.get_user_by_tg(tg.id)
    is_super_admin = tg.id in settings.admin_tg_ids
    if not user or (user["role"] not in ("director", "admin") and not is_super_admin):
        await update.message.reply_text("⛔ Недостаточно прав.")
        return

    rows = db.list_audit(limit=25)
    if not rows:
        await update.message.reply_text("Лента пуста.")
        return

    lines = ["🧾 Лента событий (последние 25):"]
    for r in rows:
        actor = r.get("actor_name") or "Система"
        event = r["event"]
        ts = str(r["created_at"])[:19]
        meta = {}
        try:
            meta = json.loads(r.get("meta_json") or "{}")
        except Exception:
            pass

        if event == "request_created":
            sign = "➕" if meta.get("op_type") == "credit" else "➖"
            lines.append(f"{ts} — {actor}: создана заявка {sign}{meta.get('hours')}ч {meta.get('date')}")
        elif event == "request_approved":
            sign = "➕" if meta.get("op_type") == "credit" else "➖"
            lines.append(f"{ts} — {actor}: одобрено {sign}{meta.get('hours')}ч")
        elif event == "request_rejected":
            sign = "➕" if meta.get("op_type") == "credit" else "➖"
            reason = meta.get("reason", "")
            if len(reason) > 40:
                reason = reason[:40] + "…"
            lines.append(f"{ts} — {actor}: отклонено {sign}{meta.get('hours')}ч ({reason})")
        else:
            lines.append(f"{ts} — {actor}: {event}")

    await update.message.reply_text("\n".join(lines), reply_markup=main_menu(user["role"]))


async def director_all_balances(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg = update.effective_user
    if not tg or not update.message:
        return

    actor = db.get_user_by_tg(tg.id)
    is_super_admin = tg.id in settings.admin_tg_ids
    if not actor or (actor["role"] not in ("director", "admin") and not is_super_admin):
        await update.message.reply_text("⛔ Недостаточно прав.")
        return

    rows = db.execute("SELECT id, full_name FROM users WHERE role='worker' ORDER BY full_name", fetch="all")
    if not rows:
        await update.message.reply_text("Сотрудников не найдено.")
        return

    lines = ["👥 Балансы сотрудников:"]
    for r in rows:
        bal = db.calc_balance_hours(r["id"])
        lines.append(f"• {r['full_name']}: {bal} ч")

    await update.message.reply_text("\n".join(lines), reply_markup=main_menu(actor["role"]))

# ---------- Director/Admin: employee card ----------
async def director_card_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    tg = update.effective_user
    if not tg or not update.message:
        return ConversationHandler.END

    actor = db.get_user_by_tg(tg.id)
    is_super_admin = tg.id in settings.admin_tg_ids
    if not actor or (actor["role"] not in ("director","admin") and not is_super_admin):
        await update.message.reply_text("⛔ Недостаточно прав.")
        return ConversationHandler.END

    users = db.execute(
        "SELECT id, full_name, role, team_id FROM users ORDER BY full_name",
        fetch="all",
    )
    if not users:
        await update.message.reply_text("Пользователей нет.")
        return ConversationHandler.END

    buttons = []
    for u in users[:80]:
        label = f"{u['full_name']} ({u['role']})"
        buttons.append([InlineKeyboardButton(label, callback_data=f"card_pick:{u['id']}")])

    await update.message.reply_text(
        "Выберите сотрудника для карточки:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return ConversationHandler.END


async def director_card_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    if not q:
        return ConversationHandler.END
    await q.answer()

    tg = update.effective_user
    if not tg:
        return ConversationHandler.END

    actor = db.get_user_by_tg(tg.id)
    is_super_admin = tg.id in settings.admin_tg_ids
    if not actor or (actor["role"] not in ("director","admin") and not is_super_admin):
        await q.edit_message_text("⛔ Недостаточно прав.")
        return ConversationHandler.END

    data = q.data or ""
    try:
        user_id = int(data.split(":", 1)[1])
    except Exception:
        await q.edit_message_text("Ошибка выбора пользователя.")
        return ConversationHandler.END

    u = db.execute(
        "SELECT id, tg_id, full_name, role, team_id, active FROM users WHERE id={p}",
        (user_id,),
        fetch="one",
    )
    if not u:
        await q.edit_message_text("Пользователь не найден.")
        return ConversationHandler.END

    team_name = None
    if u.get("team_id"):
        t = db.execute("SELECT name FROM teams WHERE id={p}", (u["team_id"],), fetch="one")
        team_name = t["name"] if t else None

    bal = db.calc_balance_hours(user_id)
    ops = db.list_statement(user_id, limit=20)

    lines = [
        f"👤 Карточка сотрудника",
        f"ФИО: {u['full_name']}",
        f"TG: {u['tg_id']}",
        f"Роль: {u['role']}",
        f"Команда: {team_name or u.get('team_id') or '-'}",
        f"Активен: {'да' if bool(u.get('active')) else 'нет'}",
        "",
        f"💼 Баланс: {bal} ч",
        "",
    ]

    if not ops:
        lines.append("Операций пока нет.")
        await q.edit_message_text("\n".join(lines))
        return ConversationHandler.END

    lines.append("📄 Последние операции (20):")
    lines.append("Дата   Тип  Часы  Статус   Комментарий")
    for r in ops:
        sign = "+" if r["op_type"] == "credit" else "-"
        dt = str(r["op_date"])[:10]
        st = r["status"]
        c = (r["comment"] or "").replace("\n", " ").strip()
        if len(c) > 28:
            c = c[:28] + "…"
        lines.append(f"{dt[5:]}  {sign}   {r['hours']}   {st:8} {c}")

    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("📤 Экспорт CSV", callback_data=f"card_csv:{user_id}")]]
    )
    await q.edit_message_text("\n".join(lines), reply_markup=kb)
    return ConversationHandler.END

async def director_card_csv(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    if not q:
        return ConversationHandler.END
    await q.answer()

    tg = update.effective_user
    if not tg:
        return ConversationHandler.END

    actor = db.get_user_by_tg(tg.id)
    is_super_admin = tg.id in settings.admin_tg_ids
    if not actor or (actor["role"] not in ("director","admin") and not is_super_admin):
        await q.edit_message_text("⛔ Недостаточно прав.")
        return ConversationHandler.END

    data = q.data or ""
    try:
        user_id = int(data.split(":", 1)[1])
    except Exception:
        await q.message.reply_text("Ошибка: не смог прочитать user_id.")
        return ConversationHandler.END

    u = db.execute(
        "SELECT id, full_name, tg_id, role, team_id FROM users WHERE id={p}",
        (user_id,),
        fetch="one",
    )
    if not u:
        await q.message.reply_text("Пользователь не найден.")
        return ConversationHandler.END

    # Берём побольше строк для экспорта (можно увеличить)
    ops = db.list_statement(user_id, limit=1000)

    output = io.StringIO()
    writer = csv.writer(output, delimiter=";", quoting=csv.QUOTE_MINIMAL)

    # header
    writer.writerow(["id", "op_date", "op_type", "hours", "status", "comment", "created_at", "decided_at", "decided_by_name"])

    for r in ops:
        writer.writerow([
            r.get("id"),
            str(r.get("op_date") or ""),
            r.get("op_type"),
            r.get("hours"),
            r.get("status"),
            (r.get("comment") or "").replace("\n", " ").strip(),
            str(r.get("created_at") or ""),
            str(r.get("decided_at") or ""),
            r.get("decided_by_name") or "",
        ])

    csv_bytes = output.getvalue().encode("utf-8-sig")
    output.close()

    safe_name = (u["full_name"] or "user").strip().replace(" ", "_")
    ts = datetime.now().strftime("%Y-%m-%d")
    filename = f"statement_{safe_name}_{ts}.csv"

    bio = io.BytesIO(csv_bytes)
    bio.name = filename
    bio.seek(0)

    await context.bot.send_document(
        chat_id=q.message.chat_id,
        document=bio,
        caption=f"📄 CSV выписка: {u['full_name']} (до 1000 строк)",
    )

    return ConversationHandler.END

# ---------- Admin flows ----------
async def admin_add_user_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Введите Telegram ID пользователя (число):")
    return ADMIN_ADD_TG


async def admin_add_user_tg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    if not text.isdigit():
        await update.message.reply_text("❌ Нужно число. Введите Telegram ID:")
        return ADMIN_ADD_TG
    context.user_data["new_tg_id"] = int(text)
    await update.message.reply_text("Введите имя и фамилию (как будет отображаться):")
    return ADMIN_ADD_NAME


async def admin_add_user_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = (update.message.text or "").strip()
    if len(name) < 2:
        await update.message.reply_text("Имя слишком короткое. Повторите:")
        return ADMIN_ADD_NAME
    context.user_data["new_name"] = name
    await update.message.reply_text(
        "Выберите роль: worker / foreman / director / admin\n"
        "Напишите одним словом:"
    )
    return ADMIN_ADD_ROLE


async def admin_add_user_role(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    role = (update.message.text or "").strip().lower()
    if role not in ("worker", "foreman", "director", "admin"):
        await update.message.reply_text("❌ Роль должна быть: worker / foreman / director / admin. Повторите:")
        return ADMIN_ADD_ROLE
    context.user_data["new_role"] = role

    if role in ("worker", "foreman"):
        teams = db.list_teams()
        if teams:
            listing = "\n".join([f"{t['id']}: {t['name']}" for t in teams])
            await update.message.reply_text(f"Выберите команду (ID) или 0 без команды:\n{listing}")
        else:
            await update.message.reply_text("Команд пока нет. Введите 0 (без команды) или создайте команду в меню 🏗️ Команды.")
        return ADMIN_ADD_TEAM

    context.user_data["new_team_id"] = None
    return await admin_add_user_finish(update, context)

async def admin_add_user_team(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    if not text.isdigit():
        await update.message.reply_text("Введите число (ID команды) или 0:")
        return ADMIN_ADD_TEAM
    team_id = int(text)
    if team_id == 0:
        context.user_data["new_team_id"] = None
        return await admin_add_user_finish(update, context)

    exists = db.execute("SELECT id FROM teams WHERE id={p}", (team_id,), fetch="one")
    if not exists:
        await update.message.reply_text("❌ Такой команды нет. Введите существующий ID или 0:")
        return ADMIN_ADD_TEAM

    context.user_data["new_team_id"] = team_id
    return await admin_add_user_finish(update, context)


async def admin_add_user_finish(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    tg = update.effective_user
    if not tg or not update.message:
        return ConversationHandler.END

    if tg.id not in settings.admin_tg_ids:
        await update.message.reply_text("⛔ Недостаточно прав.")
        return ConversationHandler.END

    new_tg = int(context.user_data.get("new_tg_id"))
    new_name = str(context.user_data.get("new_name"))
    new_role = str(context.user_data.get("new_role"))
    team_id = context.user_data.get("new_team_id")

    existing = db.get_user_by_tg(new_tg)
    if existing:
        db.set_user_role_team(new_tg, new_role, team_id)
        await update.message.reply_text("✅ Пользователь обновлён (роль/команда).", reply_markup=main_menu("admin"))
        return ConversationHandler.END

    db.create_user(new_tg, new_name, new_role, team_id)
    await update.message.reply_text("✅ Пользователь добавлен.", reply_markup=main_menu("admin"))
    return ConversationHandler.END


# ---------- Admin: edit user ----------
async def admin_edit_user_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    tg = update.effective_user
    if not tg or not update.message:
        return ConversationHandler.END
    if tg.id not in settings.admin_tg_ids:
        await update.message.reply_text("⛔ Недостаточно прав.")
        return ConversationHandler.END

    await update.message.reply_text("Введите Telegram ID пользователя, которого нужно изменить:")
    return ADMIN_EDIT_TG


async def admin_edit_user_tg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    if not text.isdigit():
        await update.message.reply_text("❌ Нужно число. Введите Telegram ID:")
        return ADMIN_EDIT_TG

    tg_id = int(text)
    u = db.get_user_by_tg(tg_id)
    if not u:
        await update.message.reply_text("❌ Пользователь не найден. Проверьте tg_id и попробуйте снова:")
        return ADMIN_EDIT_TG

    context.user_data["edit_tg_id"] = tg_id

    await update.message.reply_text(
        "Текущие данные:\n"
        f"👤 {u['full_name']}\n"
        f"Роль: {u['role']}\n"
        f"Команда: {u.get('team_id')}\n\n"
        "Введите новую роль (worker / foreman / director / admin) или '-' чтобы оставить как есть:"
    )
    return ADMIN_EDIT_ROLE


async def admin_edit_user_role(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    tg = update.effective_user
    if not tg or not update.message:
        return ConversationHandler.END

    if tg.id not in settings.admin_tg_ids:
        await update.message.reply_text("⛔ Недостаточно прав.")
        return ConversationHandler.END

    role_raw = (update.message.text or "").strip().lower()
    if role_raw == "-":
        role = None
    else:
        if role_raw not in ("worker", "foreman", "director", "admin"):
            await update.message.reply_text("❌ Роль должна быть: worker / foreman / director / admin или '-'. Повторите:")
            return ADMIN_EDIT_ROLE
        role = role_raw

    context.user_data["edit_new_role"] = role

    await update.message.reply_text(
        "Введите команду (ID) или 0 чтобы убрать команду, или '-' чтобы оставить без изменений:"
    )
    return ADMIN_EDIT_TEAM


async def admin_edit_user_team(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    tg = update.effective_user
    if not tg or not update.message:
        return ConversationHandler.END

    if tg.id not in settings.admin_tg_ids:
        await update.message.reply_text("⛔ Недостаточно прав.")
        return ConversationHandler.END

    tg_id = int(context.user_data.get("edit_tg_id", 0))
    u = db.get_user_by_tg(tg_id)
    if not u:
        await update.message.reply_text("❌ Пользователь не найден.")
        return ConversationHandler.END

    text = (update.message.text or "").strip()

    # determine new team
    if text == "-":
        new_team = "KEEP"
    else:
        if not text.isdigit():
            await update.message.reply_text("❌ Нужно число (ID команды), 0 или '-'. Повторите:")
            return ADMIN_EDIT_TEAM
        team_id = int(text)
        if team_id == 0:
            new_team = None
        else:
            exists = db.execute("SELECT id FROM teams WHERE id={p}", (team_id,), fetch="one")
            if not exists:
                await update.message.reply_text("❌ Такой команды нет. Введите существующий ID, 0 или '-':")
                return ADMIN_EDIT_TEAM
            new_team = team_id

    new_role = context.user_data.get("edit_new_role", None)

    # apply updates
    final_role = u["role"] if new_role is None else new_role
    final_team = u.get("team_id") if new_team == "KEEP" else new_team

    # safety: forbid removing last admin
    if u["role"] == "admin" and final_role != "admin":
        admins = db.execute("SELECT COUNT(*) AS c FROM users WHERE role='admin'", fetch="one")
        if admins and int(admins["c"]) <= 1:
            await update.message.reply_text("⛔ Нельзя снять роль с последнего админа.")
            return ConversationHandler.END

    db.set_user_role_team(tg_id, final_role, final_team)

    await update.message.reply_text(
        "✅ Пользователь обновлён:\n"
        f"👤 {u['full_name']}\n"
        f"Роль: {final_role}\n"
        f"Команда: {final_team}",
        reply_markup=main_menu("admin"),
    )
    return ConversationHandler.END


# ---------- Admin: delete user ----------
async def admin_delete_user_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    tg = update.effective_user
    if not tg or not update.message:
        return ConversationHandler.END

    if tg.id not in settings.admin_tg_ids:
        await update.message.reply_text("⛔ Недостаточно прав.")
        return ConversationHandler.END

    await update.message.reply_text("Введите Telegram ID пользователя, которого нужно удалить:")
    return ADMIN_DEL_TG


async def admin_delete_user_tg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    tg = update.effective_user
    if not tg or not update.message:
        return ConversationHandler.END

    if tg.id not in settings.admin_tg_ids:
        await update.message.reply_text("⛔ Недостаточно прав.")
        return ConversationHandler.END

    text = (update.message.text or "").strip()
    if not text.isdigit():
        await update.message.reply_text("❌ Нужно число. Введите Telegram ID:")
        return ADMIN_DEL_TG

    victim_tg = int(text)

    if victim_tg == tg.id:
        await update.message.reply_text("⛔ Нельзя удалить самого себя.")
        return ConversationHandler.END

    u = db.get_user_by_tg(victim_tg)
    if not u:
        await update.message.reply_text("❌ Пользователь не найден. Проверьте tg_id:")
        return ADMIN_DEL_TG

    # forbid deleting last admin
    if u["role"] == "admin":
        admins = db.execute("SELECT COUNT(*) AS c FROM users WHERE role='admin'", fetch="one")
        if admins and int(admins["c"]) <= 1:
            await update.message.reply_text("⛔ Нельзя удалить последнего админа.")
            return ConversationHandler.END

    context.user_data["del_tg_id"] = victim_tg

    await update.message.reply_text(
        "Подтвердите удаление:\n"
        f"👤 {u['full_name']} | role={u['role']} | team={u.get('team_id')}\n\n"
        "Напишите: УДАЛИТЬ (заглавными), чтобы подтвердить.\n"
        "Или напишите: ОТМЕНА"
    )
    return ADMIN_DEL_CONFIRM


async def admin_delete_user_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    tg = update.effective_user
    if not tg or not update.message:
        return ConversationHandler.END

    if tg.id not in settings.admin_tg_ids:
        await update.message.reply_text("⛔ Недостаточно прав.")
        return ConversationHandler.END

    text = (update.message.text or "").strip()

    if text.upper() == "ОТМЕНА":
        await update.message.reply_text("Ок, отменено.", reply_markup=main_menu("admin"))
        return ConversationHandler.END

    if text.upper() != "УДАЛИТЬ":
        await update.message.reply_text("Не понял. Напишите УДАЛИТЬ или ОТМЕНА:")
        return ADMIN_DEL_CONFIRM

    victim_tg = int(context.user_data.get("del_tg_id", 0))
    u = db.get_user_by_tg(victim_tg)
    if not u:
        await update.message.reply_text("Пользователь уже отсутствует.", reply_markup=main_menu("admin"))
        return ConversationHandler.END

    # safety again
    if u["role"] == "admin":
        admins = db.execute("SELECT COUNT(*) AS c FROM users WHERE role='admin'", fetch="one")
        if admins and int(admins["c"]) <= 1:
            await update.message.reply_text("⛔ Нельзя удалить последнего админа.")
            return ConversationHandler.END

    db.delete_user_by_tg(victim_tg)

    await update.message.reply_text("✅ Пользователь удалён.", reply_markup=main_menu("admin"))
    return ConversationHandler.END


async def admin_list_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg = update.effective_user
    if not tg or not update.message:
        return
    if tg.id not in settings.admin_tg_ids:
        await update.message.reply_text("⛔ Недостаточно прав.")
        return

    users = db.list_users()
    if not users:
        await update.message.reply_text("Пользователей нет.")
        return

    lines = ["👥 Пользователи:"]
    for u in users[:50]:
        lines.append(f"{u['full_name']} | tg:{u['tg_id']} | {u['role']} | team:{u.get('team_id')}")
    await update.message.reply_text("\n".join(lines), reply_markup=main_menu("admin"))


async def admin_teams_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg = update.effective_user
    if not tg or not update.message:
        return
    if tg.id not in settings.admin_tg_ids:
        await update.message.reply_text("⛔ Недостаточно прав.")
        return ConversationHandler.END

    teams = db.list_teams()
    if not teams:
        await update.message.reply_text("🏗️ Команд пока нет.\nНапишите название новой команды:", reply_markup=main_menu("admin"))
        context.user_data["team_create_mode"] = True
        return

    listing = "\n".join([f"{t['id']}: {t['name']} (foreman_user_id={t.get('foreman_user_id')})" for t in teams])
    await update.message.reply_text(
        "🏗️ Команды:\n" + listing + "\n\n"
        "Чтобы создать новую — напишите её название.\n"
        "Чтобы выйти — нажмите любую кнопку меню.",
        reply_markup=main_menu("admin"),
    )
    context.user_data["team_create_mode"] = True


async def admin_assign_foreman_hint(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg = update.effective_user
    if not tg or not update.message:
        return
    if tg.id not in settings.admin_tg_ids:
        await update.message.reply_text("⛔ Недостаточно прав.")
        return ConversationHandler.END

    teams = db.list_teams()
    if not teams:
        await update.message.reply_text("Сначала создайте команду (🏗️ Команды).")
        return
    listing = "\n".join([f"{t['id']}: {t['name']} (foreman_user_id={t.get('foreman_user_id')})" for t in teams])
    await update.message.reply_text(
        "Чтобы назначить бригадира:\n"
        "1) Бригадир должен быть добавлен как пользователь с role=foreman и team_id.\n"
        "2) Я привяжу его к команде автоматически по team_id.\n\n"
        "Текущие команды:\n" + listing
    )


# ---------- Admin: assign foreman flow (manual) ----------
async def admin_assign_foreman_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    tg = update.effective_user
    if not tg or not update.message:
        return ConversationHandler.END
    if tg.id not in settings.admin_tg_ids:
        await update.message.reply_text("⛔ Недостаточно прав.")
        return ConversationHandler.END

    teams = db.list_teams()
    if not teams:
        await update.message.reply_text("Сначала создайте команду (🏗️ Команды).")
        return ConversationHandler.END

    listing = "\n".join([f"{t['id']}: {t['name']} (foreman_user_id={t.get('foreman_user_id')})" for t in teams])
    await update.message.reply_text(
        "Выберите команду (ID), куда назначаем бригадира:\n" + listing
    )
    return ADMIN_ASSIGN_FOREMAN_TEAM


async def admin_assign_foreman_team(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    tg = update.effective_user
    if not tg or not update.message:
        return ConversationHandler.END
    if tg.id not in settings.admin_tg_ids:
        await update.message.reply_text("⛔ Недостаточно прав.")
        return ConversationHandler.END

    text = (update.message.text or "").strip()
    if not text.isdigit():
        await update.message.reply_text("❌ Нужно число. Введите ID команды:")
        return ADMIN_ASSIGN_FOREMAN_TEAM

    team_id = int(text)
    exists = db.execute("SELECT id,name,foreman_user_id FROM teams WHERE id={p}", (team_id,), fetch="one")
    if not exists:
        await update.message.reply_text("❌ Такой команды нет. Введите существующий ID команды:")
        return ADMIN_ASSIGN_FOREMAN_TEAM

    context.user_data["assign_team_id"] = team_id
    await update.message.reply_text(
        "Введите Telegram ID бригадира (пользователь должен существовать и иметь роль foreman):"
    )
    return ADMIN_ASSIGN_FOREMAN_TG


async def admin_assign_foreman_tg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    tg = update.effective_user
    if not tg or not update.message:
        return ConversationHandler.END
    if tg.id not in settings.admin_tg_ids:
        await update.message.reply_text("⛔ Недостаточно прав.")
        return ConversationHandler.END

    text = (update.message.text or "").strip()
    if not text.isdigit():
        await update.message.reply_text("❌ Нужно число. Введите Telegram ID бригадира:")
        return ADMIN_ASSIGN_FOREMAN_TG

    foreman_tg = int(text)
    team_id = int(context.user_data.get("assign_team_id", 0))
    if not team_id:
        await update.message.reply_text("❌ Команда не выбрана. Начните заново.")
        return ConversationHandler.END

    u = db.get_user_by_tg(foreman_tg)
    if not u:
        await update.message.reply_text("❌ Пользователь не найден. Добавьте его сначала (➕ Добавить пользователя).")
        return ConversationHandler.END

    if u["role"] != "foreman":
        await update.message.reply_text("❌ У пользователя должна быть роль foreman. Измените роль и повторите.")
        return ConversationHandler.END

    # team_id у foreman должен совпадать с выбранной командой
    if (u.get("team_id") or None) != team_id:
        await update.message.reply_text(
            f"❌ У бригадира team_id={u.get('team_id')}, а выбрана команда {team_id}.\n"
            "Сначала назначьте бригадиру правильную команду (✏️ Изменить пользователя), затем повторите."
        )
        return ConversationHandler.END

    db.set_team_foreman(team_id, int(u["id"]))
    team = db.execute("SELECT id,name FROM teams WHERE id={p}", (team_id,), fetch="one")
    await update.message.reply_text(
        f"✅ Бригадир назначен.\nКоманда: {team['name'] if team else team_id}\nБригадир: {u['full_name']} (tg:{foreman_tg})",
        reply_markup=main_menu("admin", is_super_admin=True),
    )
    return ConversationHandler.END

# ---------- Router for button texts ----------
async def router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Optional[int]:
    if not update.message:
        return None

    text = (update.message.text or "").strip()

    tg = update.effective_user
    if not tg:
        return None

    # ✅ Если ждём причину отклонения — это приоритет №1 (иначе кнопки начнут «путаться»)
    user = db.get_user_by_tg(tg.id)
    if not user:
        await update.message.reply_text(
            "⛔ Нет доступа. Нажмите /start.\n"
            f"Ваш Telegram ID: {tg.id}"
        )
        return None

    op_id_wait = context.user_data.get("await_reject_reason_op_id")
    if op_id_wait and user and user["role"] in ("foreman", "admin"):
        reason = text
        if len(reason) < 3:
            await update.message.reply_text("Причина слишком короткая. Напишите подробнее:")
            return None

        context.user_data["await_reject_reason_op_id"] = None
        context.user_data["reject_op_id"] = int(op_id_wait)
        await reject_reason(update, context)
        return None

    # кнопка/текст “Старт”
    if text in ("Старт", "старт"):
        await start(update, context)
        return None

    # --- Переключение режимов (только супер-админ) ---
    if tg.id in settings.admin_tg_ids:
        if text == BTN_ADMIN_MODE:
            context.user_data["ui_role"] = "admin"
            await start(update, context)
            return None
        if text == BTN_WORKER_MODE:
            context.user_data["ui_role"] = "worker"
            await start(update, context)
            return None

    # --- Переключение режима для БРИГАДИРА (только role=foreman) ---
    if user and user.get("role") == "foreman":
        if text == BTN_FOREMAN_TO_WORKER:
            db.set_user_view_mode(tg.id, "worker")
            await start(update, context)
            return None

        if text == BTN_FOREMAN_TO_FOREMAN:
            db.set_user_view_mode(tg.id, "foreman")
            await start(update, context)
            return None

    db_role = user["role"]
    is_super_admin = tg.id in settings.admin_tg_ids

    role_to_show = resolve_role_to_show(user, tg.id, context)

    # защита на всякий: если не супер-админ, то режим admin запрещён
    if role_to_show == "admin" and not is_super_admin:
        role_to_show = db_role
        context.user_data["ui_role"] = db_role

    # 🏗️ Режим создания команды: админ пишет название сообщением
    if role_to_show == "admin" and context.user_data.get("team_create_mode"):
        admin_buttons = {
            "➕ Добавить пользователя",
            "👥 Пользователи",
            "✏️ Изменить пользователя",
            "🗑️ Удалить пользователя",
            "🏗️ Команды",
            "👤 Назначить бригадира",
            "ℹ️ Помощь",
        }

        # Если нажали кнопку меню — НЕ создаём команду, просто выходим из режима
        if text in admin_buttons:
            context.user_data["team_create_mode"] = False
            # выходим из режима и продолжаем обычную обработку кнопки ниже
        else:
            name = text
            if len(name) < 2:
                await update.message.reply_text("Название слишком короткое. Введите ещё раз:")
                return None

            db.create_team(name)
            context.user_data["team_create_mode"] = False  # выключаем режим после создания

            teams = db.list_teams()
            listing = "\n".join([f"{t['id']}: {t['name']}" for t in teams])
            await update.message.reply_text("✅ Команда создана.\n" + listing, reply_markup=main_menu("admin", is_super_admin=True))
            return None

    # universal
    if text == "ℹ️ Помощь":
        await help_msg(update, context)
        return None

    # worker
    if role_to_show == "worker":
        # if text == "➕ Добавить часы":
        #     return await op_start_credit(update, context)
        # if text == "➖ Списать часы":
        #     return await op_start_debit(update, context)
        if text == "💼 Мой баланс":
            await my_balance(update, context)
            return None
        if text == "📄 Моя выписка":
            await my_statement(update, context)
            return None
        if text == "⏳ Мои заявки":
            await my_pending(update, context)
            return None

    # foreman
    if role_to_show in ("foreman", "admin"):
        if text == "⏳ Заявки на подтверждение":
            await foreman_pending(update, context)
            return None
        if text == "👥 Команда (балансы)":
            await foreman_team_balances(update, context)
            return None
        if text == "➕➖ Корректировка сотруднику":
            return await foreman_adj_start(update, context)

        if text == "📄 Выписка сотрудника":
            return await foreman_stmt_start(update, context)

    # director/admin
    if role_to_show in ("director", "admin"):
        if text == "🧾 Лента событий":
            await director_feed(update, context)
            return None
        if text == "👥 Все сотрудники (балансы)":
            await director_all_balances(update, context)
            return None
        if text == "👤 Карточка сотрудника":
            return await director_card_start(update, context)

    # admin
    if role_to_show == "admin":
        if text == "➕ Добавить пользователя":
            return await admin_add_user_start(update, context)
        if text == "👥 Пользователи":
            await admin_list_users(update, context)
            return None
        if text == "🏗️ Команды":
            await admin_teams_menu(update, context)
            return None
        if text == "👤 Назначить бригадира":
            return await admin_assign_foreman_start(update, context)
        if text == "✏️ Изменить пользователя":
            return await admin_edit_user_start(update, context)
        if text == "🗑️ Удалить пользователя":
            return await admin_delete_user_start(update, context)

    await update.message.reply_text("Не понял команду. Используйте кнопки меню.", reply_markup=main_menu(role_to_show, is_super_admin=is_super_admin))
    return None


async def conv_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # мягко чистим режимы/хвосты
    context.user_data.pop("team_create_mode", None)
    context.user_data.pop("await_reject_reason_op_id", None)
    context.user_data.pop("reject_op_id", None)

    # можно и полностью чистить, но лучше не трогать служебные ключи
    for k in list(context.user_data.keys()):
        if k.startswith("new_") or k.startswith("edit_") or k.startswith("del_") or k.startswith("op_") or k in ("hours",):
            context.user_data.pop(k, None)

    tg = update.effective_user
    if tg and update.message:
        user = db.get_user_by_tg(tg.id)
        db_role = user["role"] if user else "worker"

        is_super_admin = tg.id in settings.admin_tg_ids
        ui_role = context.user_data.get("ui_role")
        if ui_role not in ("worker", "foreman", "director", "admin"):
            ui_role = None

        role_to_show = resolve_role_to_show(user, tg.id, context) if user else db_role
        if role_to_show == "admin" and not is_super_admin:
            role_to_show = db_role
            context.user_data["ui_role"] = db_role

        await update.message.reply_text(
            "Ок, отменено.",
            reply_markup=main_menu(role_to_show, is_super_admin=is_super_admin),
        )
    return ConversationHandler.END

def build_app() -> Application:
    app = Application.builder().token(settings.bot_token).build()

    # /start всегда доступен
    app.add_handler(CommandHandler(["start"], start))

    # общий fallback для любых диалогов
    common_fallbacks = [
        CommandHandler("start", start_fallback),
        MessageHandler(filters.Regex(r"^(Отмена|ОТМЕНА|Назад)$"), conv_cancel),
        MessageHandler(
            filters.Regex(
                rf"^({re.escape(BTN_ADMIN_MODE)}|{re.escape(BTN_WORKER_MODE)}|{re.escape(BTN_FOREMAN_TO_WORKER)}|{re.escape(BTN_FOREMAN_TO_FOREMAN)})$"
            ),
            mode_switch_fallback,
        ),
    ]

    op_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex(r"^➕ Добавить часы$"), op_start_credit),
            MessageHandler(filters.Regex(r"^➖ Списать часы$"), op_start_debit),
        ],
        states={
            OP_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, op_date)],
            OP_HOURS: [MessageHandler(filters.TEXT & ~filters.COMMAND, op_hours)],
            OP_COMMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, op_comment_finish)],
        },
        fallbacks=common_fallbacks,
        name="op_conv",
        persistent=False,
        allow_reentry=True,
    )
    app.add_handler(op_conv)

    # inline approve/reject
    app.add_handler(CallbackQueryHandler(on_callback, pattern=r"^(approve|reject):"))

    admin_add_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r"^➕ Добавить пользователя$"), admin_add_user_start)],
        states={
            ADMIN_ADD_TG: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_user_tg)],
            ADMIN_ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_user_name)],
            ADMIN_ADD_ROLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_user_role)],
            ADMIN_ADD_TEAM: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_user_team)],
        },
        fallbacks=common_fallbacks,
        name="admin_add_user",
        persistent=False,
        allow_reentry=True,
    )
    app.add_handler(admin_add_conv)

    admin_edit_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r"^✏️ Изменить пользователя$"), admin_edit_user_start)],
        states={
            ADMIN_EDIT_TG: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_edit_user_tg)],
            ADMIN_EDIT_ROLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_edit_user_role)],
            ADMIN_EDIT_TEAM: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_edit_user_team)],
        },
        fallbacks=common_fallbacks,
        name="admin_edit_user",
        persistent=False,
        allow_reentry=True,
    )
    app.add_handler(admin_edit_conv)

    admin_del_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r"^🗑️ Удалить пользователя$"), admin_delete_user_start)],
        states={
            ADMIN_DEL_TG: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_delete_user_tg)],
            ADMIN_DEL_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_delete_user_confirm)],
        },
        fallbacks=common_fallbacks,
        name="admin_delete_user",
        persistent=False,
        allow_reentry=True,
    )
    app.add_handler(admin_del_conv)

    admin_assign_foreman_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r"^👤 Назначить бригадира$"), admin_assign_foreman_start)],
        states={
            ADMIN_ASSIGN_FOREMAN_TEAM: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_assign_foreman_team)],
            ADMIN_ASSIGN_FOREMAN_TG: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_assign_foreman_tg)],
        },
        fallbacks=common_fallbacks,
        name="admin_assign_foreman",
        persistent=False,
        allow_reentry=True,
    )
    app.add_handler(admin_assign_foreman_conv)

    foreman_adj_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r"^➕➖ Корректировка сотруднику$"), foreman_adj_start)],
        states={
            FOREMAN_ADJ_PICK_TG: [CallbackQueryHandler(foreman_adj_pick, pattern=r"^adj_pick:")],
            FOREMAN_ADJ_TYPE: [CallbackQueryHandler(foreman_adj_type, pattern=r"^adj_type:")],
            FOREMAN_ADJ_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, foreman_adj_date)],
            FOREMAN_ADJ_HOURS: [MessageHandler(filters.TEXT & ~filters.COMMAND, foreman_adj_hours)],
            FOREMAN_ADJ_COMMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, foreman_adj_comment_finish)],
        },
        fallbacks=common_fallbacks,
        name="foreman_adj_conv",
        persistent=False,
        allow_reentry=True,
    )

    admin_reset_conv = build_admin_reset_conv(
        db=db,
        admin_tg_ids=set(settings.admin_tg_ids),
        common_fallbacks=common_fallbacks,
    )
    app.add_handler(admin_reset_conv)

    import logging
    logger = logging.getLogger(__name__)

    async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.exception("Unhandled exception", exc_info=context.error)

    app.add_error_handler(on_error)

    app.add_handler(foreman_adj_conv)

    app.add_handler(CallbackQueryHandler(foreman_stmt_pick, pattern=r"^stmt_pick:"))

    app.add_handler(CallbackQueryHandler(director_card_pick, pattern=r"^card_pick:"))

    app.add_handler(CallbackQueryHandler(director_card_csv, pattern=r"^card_csv:"))

    # router LAST
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, router))

    return app


def main() -> None:
    app = build_app()
    print("OvertimeBot запущен. Нажмите Ctrl+C для остановки.")
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
