# src/handlers/admin_reset.py
from __future__ import annotations

import os
import hmac
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    ConversationHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from src.db import DB

# Conversation states
(ADMIN_RESET_PW, ADMIN_RESET_CONFIRM) = range(90, 92)

RESET_ENABLE_ENV = "ENABLE_DB_RESET"
RESET_PASSWORD_ENV = "DB_RESET_PASSWORD"


def build_admin_reset_conv(*, db: DB, admin_tg_ids: set[int], common_fallbacks: list) -> ConversationHandler:
    """
    Returns ConversationHandler for "🧨 Сброс БД" flow:
    1) ask password
    2) confirm via inline buttons
    3) call db.reset_all_data()
    """

    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        tg = update.effective_user
        if not tg or not update.message:
            return ConversationHandler.END

        if tg.id not in admin_tg_ids:
            await update.message.reply_text("⛔ Недостаточно прав.")
            return ConversationHandler.END

        if os.environ.get(RESET_ENABLE_ENV, "0") != "1":
            await update.message.reply_text("⛔ Сброс БД отключён (ENABLE_DB_RESET=0).")
            return ConversationHandler.END

        context.user_data["reset_attempts"] = 0
        await update.message.reply_text(
            "<b>🧨 Сброс базы данных</b>\n\n"
            "Это удалит ВСЕ данные (users/teams/operations/audit_log), но оставит таблицы.\n"
            "Введите пароль для сброса:",
            parse_mode="HTML",
        )
        return ADMIN_RESET_PW

    async def password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        tg = update.effective_user
        if not tg or not update.message:
            return ConversationHandler.END

        if tg.id not in admin_tg_ids:
            await update.message.reply_text("⛔ Недостаточно прав.")
            return ConversationHandler.END

        if os.environ.get(RESET_ENABLE_ENV, "0") != "1":
            await update.message.reply_text("⛔ Сброс БД отключён (ENABLE_DB_RESET=0).")
            return ConversationHandler.END

        entered = (update.message.text or "").strip()
        expected = os.environ.get(RESET_PASSWORD_ENV, "")

        ok = bool(expected) and hmac.compare_digest(entered, expected)

        if not ok:
            context.user_data["reset_attempts"] = int(context.user_data.get("reset_attempts", 0)) + 1
            if context.user_data["reset_attempts"] >= 3:
                await update.message.reply_text("⛔ Неверный пароль. Слишком много попыток. Отмена.")
                return ConversationHandler.END
            await update.message.reply_text("❌ Неверный пароль. Попробуйте ещё раз:")
            return ADMIN_RESET_PW

        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("✅ ДА, СБРОСИТЬ", callback_data="resetdb:confirm")],
                [InlineKeyboardButton("↩️ Отмена", callback_data="resetdb:cancel")],
            ]
        )

        await update.message.reply_text(
            "Пароль верный.\n\n"
            "⚠️ Подтвердите: удалить ВСЕ данные из базы?",
            reply_markup=kb,
        )
        return ADMIN_RESET_CONFIRM

    async def confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        q = update.callback_query
        if not q:
            return ConversationHandler.END
        await q.answer()

        tg = update.effective_user
        if not tg:
            return ConversationHandler.END

        if tg.id not in admin_tg_ids:
            await q.edit_message_text("⛔ Недостаточно прав.")
            return ConversationHandler.END

        if os.environ.get(RESET_ENABLE_ENV, "0") != "1":
            await q.edit_message_text("⛔ Сброс БД отключён (ENABLE_DB_RESET=0).")
            return ConversationHandler.END

        if (q.data or "") == "resetdb:cancel":
            await q.edit_message_text("Ок, отменено.")
            return ConversationHandler.END

        # confirm
        try:
            # 1) wipe everything
            db.reset_all_data()

            # 2) ensure current super-admin exists immediately after wipe
            full_name = (tg.full_name or "").strip() or "Без имени"
            db.create_user(tg.id, full_name, "worker", None)

            # 3) log event into fresh audit_log
            actor = db.get_user_by_tg(tg.id)
            actor_id = actor["id"] if actor else None
            db.log_event(
                actor_user_id=actor_id,
                event="db_reset",
                entity="db",
                entity_id=None,
                meta={"by_tg_id": tg.id},
            )

            await q.edit_message_text("✅ База очищена. Начинаем с чистого листа.")
        except Exception as e:
            await q.edit_message_text(f"❌ Ошибка при сбросе: {e}")

        return ConversationHandler.END

    return ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r"^🧨 Сброс БД$"), start)],
        states={
            ADMIN_RESET_PW: [MessageHandler(filters.TEXT & ~filters.COMMAND, password)],
            ADMIN_RESET_CONFIRM: [
                CallbackQueryHandler(confirm, pattern=r"^resetdb:(confirm|cancel)$")
            ],
        },
        fallbacks=common_fallbacks,
        name="admin_reset_db",
        persistent=False,
        allow_reentry=True,
    )