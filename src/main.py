# src/main.py
from __future__ import annotations

import re
import json
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


# ---------- Conversation states ----------
(OP_DATE, OP_HOURS, OP_COMMENT) = range(3)
(ADMIN_ADD_TG, ADMIN_ADD_NAME, ADMIN_ADD_ROLE, ADMIN_ADD_TEAM) = range(10, 14)
(REJECT_REASON,) = (20,)
(TEAM_CREATE_NAME,) = (30,)
(FOREMAN_ADJ_PICK_TG, FOREMAN_ADJ_TYPE, FOREMAN_ADJ_DATE, FOREMAN_ADJ_HOURS, FOREMAN_ADJ_COMMENT) = range(40, 45)


# ---------- UI helpers ----------
ROLE_TITLES = {
    "worker": "Сотрудник",
    "foreman": "Бригадир",
    "director": "Руководитель",
    "admin": "Админ",
}

def main_menu(role: str) -> ReplyKeyboardMarkup:
    if role == "worker":
        rows = [
            [KeyboardButton("➕ Добавить часы"), KeyboardButton("➖ Списать часы")],
            [KeyboardButton("💼 Мой баланс"), KeyboardButton("📄 Моя выписка")],
            [KeyboardButton("⏳ Мои заявки"), KeyboardButton("ℹ️ Помощь")],
        ]
    elif role == "foreman":
        rows = [
            [KeyboardButton("⏳ Заявки на подтверждение"), KeyboardButton("👥 Команда (балансы)")],
            [KeyboardButton("➕➖ Корректировка сотруднику"), KeyboardButton("📄 Выписка сотрудника")],
            [KeyboardButton("ℹ️ Помощь")],
        ]
    elif role == "director":
        rows = [
            [KeyboardButton("🧾 Лента событий"), KeyboardButton("👥 Все сотрудники (балансы)")],
            [KeyboardButton("👤 Карточка сотрудника"), KeyboardButton("ℹ️ Помощь")],
        ]
    else:  # admin
        rows = [
            [KeyboardButton("➕ Добавить пользователя"), KeyboardButton("👥 Пользователи")],
            [KeyboardButton("🏗️ Команды"), KeyboardButton("👤 Назначить бригадира")],
            [KeyboardButton("ℹ️ Помощь")],
        ]
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
    # allow increments like 0.5
    return round(v, 2)


# ---------- App globals ----------
settings = load_settings()
db = DB(settings.database_url)


# ---------- Auth helpers ----------
def ensure_user_or_deny(update: Update, tg_id: int, full_name: str) -> dict | None:
    user = db.get_user_by_tg(tg_id)
    if user:
        return user

    # auto-create admin if tg_id in ADMIN_TG_IDS
    if tg_id in settings.admin_tg_ids:
        return db.upsert_user_minimal_admin(tg_id=tg_id, full_name=full_name, role="admin")

    return None


async def send_to_directors(text: str) -> None:
    director_ids = db.list_director_tg_ids()
    if not director_ids:
        return
    app = Application.get_current()
    if not app:
        return
    for tg_id in director_ids:
        try:
            await app.bot.send_message(chat_id=tg_id, text=text)
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

    role = user["role"]
    await update.message.reply_text(
        f"✅ Вы вошли как: {ROLE_TITLES.get(role, role)}\n"
        f"👤 {user['full_name']}\n\n"
        "Выберите действие кнопками ниже.",
        reply_markup=main_menu(role),
    )


async def help_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg = update.effective_user
    if not tg or not update.message:
        return

    user = db.get_user_by_tg(tg.id)
    if not user:
        await update.message.reply_text("Напишите /старт чтобы начать.")
        return

    role = user["role"]
    if role == "worker":
        text = (
            "ℹ️ Помощь (Сотрудник)\n\n"
            "➕ Добавить часы — отправить заявку на начисление.\n"
            "➖ Списать часы — отправить заявку на списание (баланс может стать отрицательным).\n"
            "💼 Мой баланс — текущий итог.\n"
            "📄 Моя выписка — последние операции.\n"
            "⏳ Мои заявки — ожидающие решения.\n"
        )
    elif role == "foreman":
        text = (
            "ℹ️ Помощь (Бригадир)\n\n"
            "⏳ Заявки на подтверждение — подтвердить или отклонить.\n"
            "👥 Команда (балансы) — сводка по сотрудникам.\n"
            "➕➖ Корректировка сотруднику — вручную добавить/списать (с причиной).\n"
        )
    elif role == "director":
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

    await update.message.reply_text(text, reply_markup=main_menu(role))


# ---------- Worker: create operation (credit/debit) ----------
async def op_start_credit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["op_type"] = "credit"
    await update.message.reply_text("Введите дату (например 21.02.2026 или 2026-02-21):")
    return OP_DATE

async def op_start_debit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
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
        await update.message.reply_text("⛔ Нет доступа. Напишите /старт")
        return ConversationHandler.END

    comment = (update.message.text or "").strip()
    op_type = context.user_data.get("op_type")
    op_date = context.user_data.get("op_date")
    hours = float(context.user_data.get("hours", 0))

    if op_type not in ("credit", "debit") or not op_date or hours <= 0:
        await update.message.reply_text("❌ Ошибка данных. Начните заново.")
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

    sign = "➕" if op_type == "credit" else "➖"
    await update.message.reply_text(
        f"✅ Заявка отправлена бригадиру.\n"
        f"{sign} {hours} ч • {op_date}\n"
        f"Комментарий: {comment}",
        reply_markup=main_menu(user["role"]),
    )

    # Foreman message with inline buttons
    if foreman_tg:
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Подтвердить", callback_data=f"approve:{op['id']}"),
                InlineKeyboardButton("❌ Отклонить", callback_data=f"reject:{op['id']}"),
            ]
        ])
        # balance preview (may go negative)
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
            f"Баланс после: {round(bal_after,2)} ч"
        )
        try:
            await context.bot.send_message(chat_id=foreman_tg, text=text, reply_markup=kb)
        except Exception:
            pass

    # Directors feed (optional)
    await send_to_directors(
        f"🧾 Событие: создана заявка\n"
        f"{user['full_name']} {sign}{hours}ч на {op_date}"
    )

    return ConversationHandler.END


async def my_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg = update.effective_user
    if not tg or not update.message:
        return
    user = db.get_user_by_tg(tg.id)
    if not user:
        await update.message.reply_text("⛔ Нет доступа. Напишите /старт")
        return
    bal = db.calc_balance_hours(user["id"])
    status = "долг" if bal < 0 else "доступно"
    await update.message.reply_text(f"💼 Баланс: {bal} ч ({status})", reply_markup=main_menu(user["role"]))

async def my_pending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg = update.effective_user
    if not tg or not update.message:
        return
    user = db.get_user_by_tg(tg.id)
    if not user:
        await update.message.reply_text("⛔ Нет доступа. Напишите /старт")
        return

    rows = db.execute("""
        SELECT id, op_type, op_date, hours, comment, status
        FROM operations
        WHERE target_user_id={p} AND status='pending'
        ORDER BY created_at DESC
        LIMIT 20
    """, (user["id"],), fetch="all")

    if not rows:
        await update.message.reply_text("⏳ У вас нет заявок в ожидании.", reply_markup=main_menu(user["role"]))
        return

    lines = ["⏳ Ваши заявки (ожидают):"]
    for r in rows:
        sign = "➕" if r["op_type"] == "credit" else "➖"
        lines.append(f"#{r['id']} {sign}{r['hours']}ч • {r['op_date']} • {r['comment'][:40]}")
    await update.message.reply_text("\n".join(lines), reply_markup=main_menu(user["role"]))

async def my_statement(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg = update.effective_user
    if not tg or not update.message:
        return
    user = db.get_user_by_tg(tg.id)
    if not user:
        await update.message.reply_text("⛔ Нет доступа. Напишите /старт")
        return

    bal = db.calc_balance_hours(user["id"])
    rows = db.list_statement(user["id"], limit=20)
    if not rows:
        await update.message.reply_text("Пока нет операций.", reply_markup=main_menu(user["role"]))
        return

    header = [
        "📄 Выписка (последние 20)",
        "Дата   Тип  Часы  Статус   Комментарий",
    ]
    body = []
    for r in rows:
        sign = "+" if r["op_type"] == "credit" else "-"
        dt = str(r["op_date"])[:10]
        st = r["status"]
        c = (r["comment"] or "").replace("\n", " ").strip()
        if len(c) > 28:
            c = c[:28] + "…"
        body.append(f"{dt[5:]}  {sign}   {r['hours']}   {st:8} {c}")

    footer = [f"\nИтоговый баланс: {bal} ч"]
    await update.message.reply_text("\n".join(header + body + footer), reply_markup=main_menu(user["role"]))


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
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Подтвердить", callback_data=f"approve:{op['id']}"),
            InlineKeyboardButton("❌ Отклонить", callback_data=f"reject:{op['id']}"),
        ]])
        # compute balances
        bal_now = db.calc_balance_hours(op["target_user_id"])
        bal_after = bal_now + (float(op["hours"]) if op["op_type"] == "credit" else -float(op["hours"]))
        text = (
            f"Заявка #{op['id']}\n"
            f"Сотрудник: {op['target_name']}\n"
            f"{sign} {op['hours']} ч • {op['op_date']}\n"
            f"Комментарий: {op['comment']}\n\n"
            f"Баланс сейчас: {bal_now} ч\n"
            f"Баланс после: {round(bal_after,2)} ч"
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
        # notify worker
        target = db.execute("SELECT tg_id, full_name FROM users WHERE id={p}", (op["target_user_id"],), fetch="one")
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
            f"🧾 Одобрено: {target['full_name'] if target else 'сотрудник'} "
            f"{sign}{op['hours']}ч ({op['op_date']})"
        )

        await q.edit_message_text(f"✅ Одобрено. Баланс сотрудника: {bal} ч")
        return ConversationHandler.END

    if data.startswith("reject:"):
        op_id = int(data.split(":", 1)[1])
        context.user_data["reject_op_id"] = op_id
        context.user_data["reject_msg_chat_id"] = q.message.chat_id if q.message else None
        context.user_data["reject_msg_id"] = q.message.message_id if q.message else None
        await q.edit_message_text("❌ Отклонение: напишите причину одним сообщением.")
        return REJECT_REASON

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
        await update.message.reply_text("Причина слишком короткая. Напишите подробнее:")
        return REJECT_REASON

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
        meta={"op_type": op["op_type"], "hours": float(op["hours"]), "target_user_id": op["target_user_id"], "reason": reason},
    )

    await send_to_directors(
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

    # foreman sees only their team (admin sees all)
    if user["role"] == "admin":
        rows = db.execute("SELECT id, full_name, role, team_id FROM users WHERE role='worker' ORDER BY full_name", fetch="all")
        title = "👥 Балансы сотрудников (все)"
    else:
        team_id = user.get("team_id")
        if not team_id:
            await update.message.reply_text("У вас не назначена команда. Обратитесь к админу.")
            return
        rows = db.execute("SELECT id, full_name FROM users WHERE role='worker' AND team_id={p} ORDER BY full_name", (team_id,), fetch="all")
        title = "👥 Балансы команды"

    if not rows:
        await update.message.reply_text("Сотрудников не найдено.")
        return

    lines = [title]
    for r in rows:
        bal = db.calc_balance_hours(r["id"])
        lines.append(f"• {r['full_name']}: {bal} ч")
    await update.message.reply_text("\n".join(lines), reply_markup=main_menu(user["role"]))


# ---------- Director views ----------
async def director_feed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg = update.effective_user
    if not tg or not update.message:
        return
    user = db.get_user_by_tg(tg.id)
    if not user or user["role"] not in ("director", "admin"):
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
    user = db.get_user_by_tg(tg.id)
    if not user or user["role"] not in ("director", "admin"):
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
    await update.message.reply_text("\n".join(lines), reply_markup=main_menu(user["role"]))


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

    # team only for worker/foreman
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
    admin = db.get_user_by_tg(tg.id)
    if not admin or admin["role"] != "admin":
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

async def admin_list_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg = update.effective_user
    if not tg or not update.message:
        return
    admin = db.get_user_by_tg(tg.id)
    if not admin or admin["role"] != "admin":
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
    admin = db.get_user_by_tg(tg.id)
    if not admin or admin["role"] != "admin":
        await update.message.reply_text("⛔ Недостаточно прав.")
        return

    teams = db.list_teams()
    if not teams:
        await update.message.reply_text("🏗️ Команд пока нет.\nНапишите название новой команды:", reply_markup=main_menu("admin"))
        context.user_data["team_create_mode"] = True
        return
    listing = "\n".join([f"{t['id']}: {t['name']} (foreman_user_id={t.get('foreman_user_id')})" for t in teams])
    await update.message.reply_text(
        "🏗️ Команды:\n" + listing + "\n\nЧтобы создать новую — напишите её название сообщением.",
        reply_markup=main_menu("admin"),
    )
    context.user_data["team_create_mode"] = True

async def admin_create_team_from_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # this reacts only when team_create_mode flag is set
    if not update.message:
        return
    tg = update.effective_user
    if not tg:
        return
    admin = db.get_user_by_tg(tg.id)
    if not admin or admin["role"] != "admin":
        return

    if not context.user_data.get("team_create_mode"):
        return

    name = (update.message.text or "").strip()
    if len(name) < 2:
        await update.message.reply_text("Название слишком короткое. Введите ещё раз:")
        return

    db.create_team(name)
    teams = db.list_teams()
    listing = "\n".join([f"{t['id']}: {t['name']}" for t in teams])
    await update.message.reply_text("✅ Команда создана.\n" + listing, reply_markup=main_menu("admin"))

async def admin_assign_foreman_hint(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg = update.effective_user
    if not tg or not update.message:
        return
    admin = db.get_user_by_tg(tg.id)
    if not admin or admin["role"] != "admin":
        await update.message.reply_text("⛔ Недостаточно прав.")
        return

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


# ---------- Router for button texts ----------
async def router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Optional[int]:
    if not update.message:
        return None

    text = (update.message.text or "").strip()
    tg = update.effective_user
    if not tg:
        return None

    user = db.get_user_by_tg(tg.id)
    if not user:
        await update.message.reply_text("Напишите /старт")
        return None

    role = user["role"]

    # universal
    if text == "ℹ️ Помощь":
        await help_msg(update, context)
        return None

    # worker
    if role == "worker":
        if text == "➕ Добавить часы":
            return await op_start_credit(update, context)
        if text == "➖ Списать часы":
            return await op_start_debit(update, context)
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
    if role in ("foreman", "admin"):
        if text == "⏳ Заявки на подтверждение":
            await foreman_pending(update, context)
            return None
        if text == "👥 Команда (балансы)":
            await foreman_team_balances(update, context)
            return None

    # director/admin
    if role in ("director", "admin"):
        if text == "🧾 Лента событий":
            await director_feed(update, context)
            return None
        if text == "👥 Все сотрудники (балансы)":
            await director_all_balances(update, context)
            return None
        if text == "👤 Карточка сотрудника":
            await update.message.reply_text("Пока MVP: используйте '👥 Все сотрудники (балансы)'. Карточку добавим следующим шагом.")
            return None

    # admin
    if role == "admin":
        if text == "➕ Добавить пользователя":
            return await admin_add_user_start(update, context)
        if text == "👥 Пользователи":
            await admin_list_users(update, context)
            return None
        if text == "🏗️ Команды":
            await admin_teams_menu(update, context)
            return None
        if text == "👤 Назначить бригадира":
            await admin_assign_foreman_hint(update, context)
            return None

    await update.message.reply_text("Не понял команду. Используйте кнопки меню.", reply_markup=main_menu(role))
    return None


def build_app() -> Application:
    app = Application.builder().token(settings.bot_token).build()

    # /start and /старт
    app.add_handler(CommandHandler(["start", "старт"], start))

    # Operation conversation (worker)
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
        fallbacks=[],
        name="op_conv",
        persistent=False,
    )
    app.add_handler(op_conv)

    # Reject conversation (foreman inline)
    reject_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(on_callback, pattern=r"^(approve|reject):")],
        states={
            REJECT_REASON: [MessageHandler(filters.TEXT & ~filters.COMMAND, reject_reason)],
        },
        fallbacks=[CallbackQueryHandler(on_callback, pattern=r"^(approve|reject):")],
        name="reject_conv",
        persistent=False,
    )
    app.add_handler(reject_conv)

    # Admin add user conversation
    admin_add_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r"^➕ Добавить пользователя$"), admin_add_user_start)],
        states={
            ADMIN_ADD_TG: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_user_tg)],
            ADMIN_ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_user_name)],
            ADMIN_ADD_ROLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_user_role)],
            ADMIN_ADD_TEAM: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_user_team)],
        },
        fallbacks=[],
        name="admin_add_user",
        persistent=False,
    )
    app.add_handler(admin_add_conv)

    # Router for all other buttons/text
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, router))

    return app


def main() -> None:
    app = build_app()
    print("OvertimeBot запущен. Нажмите Ctrl+C для остановки.")
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
