"""
Telegram Business Chat Tracker Bot
Отслеживает сообщения, редактирования и удаления в Telegram Business чатах.
"""

import asyncio
import logging
import os
import sys
from datetime import datetime
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    Message,
    Update,
    BusinessConnection,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    select,
    func,
)
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# ─── Конфигурация ──────────────────────────────────────────────────────────────

BOT_TOKEN: str = os.environ.get("BOT_TOKEN", "")
DATABASE_URL: str = os.environ.get(
    "DATABASE_URL",
    "postgresql+asyncpg://bothost_db_0f4e9694f89c:Xp9i3BN4-YQq_SyFVK0NxvImLPnP7BBF6gjNFDTW5VI@node1.pghost.ru:15756/bothost_db_0f4e9694f89c",
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

# ─── База данных ───────────────────────────────────────────────────────────────


class Base(DeclarativeBase):
    pass


class BusinessChat(Base):
    __tablename__ = "business_chats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    business_connection_id: Mapped[str] = mapped_column(
        String(255), unique=True, index=True
    )
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    chat_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow
    )
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    messages: Mapped[list["TrackedMessage"]] = relationship(
        "TrackedMessage", back_populates="chat", cascade="all, delete-orphan"
    )
    notifications: Mapped[list["UserNotification"]] = relationship(
        "UserNotification", back_populates="chat", cascade="all, delete-orphan"
    )


class TrackedMessage(Base):
    __tablename__ = "tracked_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("business_chats.id"), index=True
    )
    business_message_id: Mapped[int] = mapped_column(BigInteger)
    message_type: Mapped[str] = mapped_column(String(50))  # "incoming" / "outgoing"
    from_user_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    from_username: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    reply_to_message_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )
    is_edited: Mapped[bool] = mapped_column(Boolean, default=False)
    edit_count: Mapped[int] = mapped_column(Integer, default=0)
    edit_history: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    chat: Mapped["BusinessChat"] = relationship(
        "BusinessChat", back_populates="messages"
    )


class UserNotification(Base):
    __tablename__ = "user_notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    business_chat_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("business_chats.id")
    )
    notify_new_messages: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_edits: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_deletions: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    chat: Mapped["BusinessChat"] = relationship(
        "BusinessChat", back_populates="notifications"
    )


# ─── Движок SQLAlchemy ─────────────────────────────────────────────────────────

engine = create_async_engine(DATABASE_URL, echo=False)
async_session: async_sessionmaker[AsyncSession] = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables created / verified.")


# ─── Клавиатуры ────────────────────────────────────────────────────────────────


def kb_main_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="📊 Мои бизнес-чаты", callback_data="list_chats")
    b.button(text="ℹ️ Статус бота", callback_data="bot_status")
    b.button(text="❓ Помощь", callback_data="help")
    b.adjust(1)
    return b.as_markup()


def kb_chat_settings(chat_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✉️ Новые сообщения", callback_data=f"toggle_msgs:{chat_id}")
    b.button(text="✏️ Редактирования", callback_data=f"toggle_edits:{chat_id}")
    b.button(text="🗑 Удаления", callback_data=f"toggle_dels:{chat_id}")
    b.button(text="📋 История сообщений", callback_data=f"history:{chat_id}")
    b.button(text="❌ Отключить чат", callback_data=f"disable:{chat_id}")
    b.button(text="🔙 Назад", callback_data="list_chats")
    b.adjust(1)
    return b.as_markup()


def kb_history(chat_id: int, page: int, has_prev: bool, has_next: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if has_prev:
        b.button(text="⬅️ Назад", callback_data=f"hist_page:{chat_id}:{page - 1}")
    if has_next:
        b.button(text="➡️ Вперед", callback_data=f"hist_page:{chat_id}:{page + 1}")
    b.button(text="🔙 К настройкам", callback_data=f"settings:{chat_id}")
    b.adjust(2, 1)
    return b.as_markup()


# ─── Роутер и обработчики ──────────────────────────────────────────────────────

router = Router()

HELP_TEXT = (
    "🤖 <b>Business Chat Tracker</b>\n\n"
    "Этот бот отслеживает активность в ваших Telegram Business чатах.\n\n"
    "<b>Команды:</b>\n"
    "/start — главное меню\n"
    "/chats — список ваших бизнес-чатов\n"
    "/status — общая статистика\n"
    "/help — эта справка\n\n"
    "<b>Как начать:</b>\n"
    "1. Подключите бота в настройках Telegram Business.\n"
    "2. Бот автоматически начнёт отслеживать сообщения.\n"
    "3. Настройте уведомления через меню чатов."
)


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await message.answer(
        "👋 <b>Добро пожаловать в Business Chat Tracker!</b>\n\n"
        "Бот отслеживает сообщения, редактирования и удаления "
        "в ваших Telegram Business чатах.\n\n"
        "Выберите действие:",
        reply_markup=kb_main_menu(),
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT, reply_markup=kb_main_menu())


@router.message(Command("chats"))
async def cmd_chats(message: Message) -> None:
    async with async_session() as session:
        result = await session.execute(
            select(BusinessChat).where(
                BusinessChat.user_id == message.from_user.id,
                BusinessChat.is_active == True,
            )
        )
        chats = result.scalars().all()

    if not chats:
        await message.answer(
            "У вас нет активных бизнес-чатов.\n"
            "Подключите бота в настройках Telegram Business."
        )
        return

    lines = ["📊 <b>Ваши активные бизнес-чаты:</b>\n"]
    for chat in chats:
        name = chat.chat_name or f"Чат #{chat.id}"
        lines.append(f"• <b>{name}</b> (ID: {chat.id})")
    await message.answer("\n".join(lines), reply_markup=kb_main_menu())


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    async with async_session() as session:
        total_chats = (
            await session.execute(
                select(func.count(BusinessChat.id)).where(
                    BusinessChat.user_id == message.from_user.id
                )
            )
        ).scalar_one()
        active_chats = (
            await session.execute(
                select(func.count(BusinessChat.id)).where(
                    BusinessChat.user_id == message.from_user.id,
                    BusinessChat.is_active == True,
                )
            )
        ).scalar_one()

        # Подсчёт по сообщениям пользователя
        user_chat_ids_result = await session.execute(
            select(BusinessChat.id).where(BusinessChat.user_id == message.from_user.id)
        )
        user_chat_ids = [r[0] for r in user_chat_ids_result.all()]

        total_msgs = edited_msgs = deleted_msgs = 0
        if user_chat_ids:
            total_msgs = (
                await session.execute(
                    select(func.count(TrackedMessage.id)).where(
                        TrackedMessage.chat_id.in_(user_chat_ids)
                    )
                )
            ).scalar_one()
            edited_msgs = (
                await session.execute(
                    select(func.count(TrackedMessage.id)).where(
                        TrackedMessage.chat_id.in_(user_chat_ids),
                        TrackedMessage.is_edited == True,
                    )
                )
            ).scalar_one()
            deleted_msgs = (
                await session.execute(
                    select(func.count(TrackedMessage.id)).where(
                        TrackedMessage.chat_id.in_(user_chat_ids),
                        TrackedMessage.is_deleted == True,
                    )
                )
            ).scalar_one()

    await message.answer(
        f"📈 <b>Ваша статистика</b>\n\n"
        f"💼 Всего чатов: {total_chats}\n"
        f"✅ Активных: {active_chats}\n"
        f"💬 Сообщений: {total_msgs}\n"
        f"✏️ Редактирований: {edited_msgs}\n"
        f"🗑 Удалений: {deleted_msgs}",
        reply_markup=kb_main_menu(),
    )


# ─── Business Connection ───────────────────────────────────────────────────────


@router.business_connection()
async def handle_business_connection(event: BusinessConnection, bot: Bot) -> None:
    conn_id = event.id
    user_id = event.user.id
    is_enabled = event.is_enabled

    async with async_session() as session:
        result = await session.execute(
            select(BusinessChat).where(
                BusinessChat.business_connection_id == conn_id
            )
        )
        chat = result.scalar_one_or_none()

        if is_enabled:
            if chat is None:
                chat = BusinessChat(
                    business_connection_id=conn_id,
                    user_id=user_id,
                    chat_name=f"Business Chat {conn_id[:8]}",
                    is_active=True,
                )
                session.add(chat)
                await session.flush()

                notif = UserNotification(
                    user_id=user_id,
                    business_chat_id=chat.id,
                )
                session.add(notif)
                await session.commit()

                try:
                    await bot.send_message(
                        user_id,
                        f"✅ <b>Бизнес-чат подключён!</b>\n\n"
                        f"ID соединения: <code>{conn_id}</code>\n"
                        f"Внутренний ID чата: #{chat.id}\n\n"
                        f"Теперь бот отслеживает сообщения в этом чате.",
                        reply_markup=kb_main_menu(),
                    )
                except Exception as e:
                    logger.warning(f"Не удалось отправить уведомление {user_id}: {e}")
            else:
                chat.is_active = True
                chat.deleted_at = None
                await session.commit()

                try:
                    await bot.send_message(
                        user_id,
                        f"🔄 <b>Бизнес-чат повторно активирован</b>\n"
                        f"Чат #{chat.id} снова отслеживается.",
                    )
                except Exception as e:
                    logger.warning(f"Не удалось отправить уведомление {user_id}: {e}")
        else:
            if chat:
                chat.is_active = False
                chat.deleted_at = datetime.utcnow()
                await session.commit()

                try:
                    await bot.send_message(
                        user_id,
                        f"⛔ <b>Бизнес-чат отключён</b>\n"
                        f"Чат #{chat.id} больше не отслеживается.",
                    )
                except Exception as e:
                    logger.warning(f"Не удалось отправить уведомление {user_id}: {e}")


# ─── Business Messages ─────────────────────────────────────────────────────────


@router.business_message()
async def handle_business_message(message: Message, bot: Bot) -> None:
    conn_id = message.business_connection_id
    if not conn_id:
        return

    async with async_session() as session:
        result = await session.execute(
            select(BusinessChat).where(
                BusinessChat.business_connection_id == conn_id,
                BusinessChat.is_active == True,
            )
        )
        chat = result.scalar_one_or_none()
        if not chat:
            return

        from_user = message.from_user
        from_user_id = from_user.id if from_user else None
        from_username = from_user.username if from_user else None

        # incoming = от клиента, outgoing = от владельца бизнеса
        try:
            connection_info = await bot.get_business_connection(conn_id)
            owner_id = connection_info.user.id
        except Exception:
            owner_id = chat.user_id

        msg_type = "outgoing" if from_user_id == owner_id else "incoming"

        text = message.text or message.caption or "[Media/Non-text]"

        reply_to = None
        if message.reply_to_message:
            reply_to = message.reply_to_message.message_id

        tracked = TrackedMessage(
            chat_id=chat.id,
            business_message_id=message.message_id,
            message_type=msg_type,
            from_user_id=from_user_id,
            from_username=from_username,
            text=text,
            reply_to_message_id=reply_to,
        )
        session.add(tracked)
        await session.commit()
        await session.refresh(tracked)

        await notify_user(session, bot, chat, tracked, "new_message")


@router.edited_business_message()
async def handle_edited_business_message(message: Message, bot: Bot) -> None:
    conn_id = message.business_connection_id
    if not conn_id:
        return

    async with async_session() as session:
        result = await session.execute(
            select(BusinessChat).where(
                BusinessChat.business_connection_id == conn_id,
                BusinessChat.is_active == True,
            )
        )
        chat = result.scalar_one_or_none()
        if not chat:
            return

        result = await session.execute(
            select(TrackedMessage).where(
                TrackedMessage.chat_id == chat.id,
                TrackedMessage.business_message_id == message.message_id,
            )
        )
        tracked = result.scalar_one_or_none()
        if not tracked:
            return

        new_text = message.text or message.caption or "[Media/Non-text]"
        old_text = tracked.text

        history = tracked.edit_history or []
        history.append(
            {
                "old_text": old_text,
                "new_text": new_text,
                "edited_at": datetime.utcnow().isoformat(),
            }
        )

        tracked.edit_history = history
        tracked.text = new_text
        tracked.is_edited = True
        tracked.edit_count = (tracked.edit_count or 0) + 1
        tracked.updated_at = datetime.utcnow()

        await session.commit()
        await session.refresh(tracked)

        await notify_user(session, bot, chat, tracked, "edited")


# ─── Удаления через @router.update() ──────────────────────────────────────────


@router.update()
async def handle_any_update(update: Update, bot: Bot) -> None:
    deleted_event = getattr(update, "business_messages_deleted", None)
    if not deleted_event:
        return

    conn_id = deleted_event.business_connection_id
    message_ids = deleted_event.message_ids

    async with async_session() as session:
        result = await session.execute(
            select(BusinessChat).where(
                BusinessChat.business_connection_id == conn_id,
                BusinessChat.is_active == True,
            )
        )
        chat = result.scalar_one_or_none()
        if not chat:
            return

        for msg_id in message_ids:
            result = await session.execute(
                select(TrackedMessage).where(
                    TrackedMessage.chat_id == chat.id,
                    TrackedMessage.business_message_id == msg_id,
                )
            )
            tracked = result.scalar_one_or_none()
            if tracked and not tracked.is_deleted:
                tracked.is_deleted = True
                tracked.deleted_at = datetime.utcnow()
                tracked.updated_at = datetime.utcnow()
                await session.flush()
                await notify_user(session, bot, chat, tracked, "deleted")

        await session.commit()


# ─── Callback-обработчики ─────────────────────────────────────────────────────


@router.callback_query(F.data == "list_chats")
async def cb_list_chats(callback: CallbackQuery) -> None:
    async with async_session() as session:
        result = await session.execute(
            select(BusinessChat).where(
                BusinessChat.user_id == callback.from_user.id
            )
        )
        chats = result.scalars().all()

    if not chats:
        await callback.message.edit_text(
            "📭 У вас нет бизнес-чатов.\nПодключите бота в настройках Telegram Business.",
            reply_markup=kb_main_menu(),
        )
        await callback.answer()
        return

    b = InlineKeyboardBuilder()
    async with async_session() as session:
        lines = ["📊 <b>Ваши бизнес-чаты:</b>\n"]
        for chat in chats:
            msg_count = (
                await session.execute(
                    select(func.count(TrackedMessage.id)).where(
                        TrackedMessage.chat_id == chat.id
                    )
                )
            ).scalar_one()
            edit_count = (
                await session.execute(
                    select(func.count(TrackedMessage.id)).where(
                        TrackedMessage.chat_id == chat.id,
                        TrackedMessage.is_edited == True,
                    )
                )
            ).scalar_one()

            status = "✅" if chat.is_active else "⛔"
            name = chat.chat_name or f"Чат #{chat.id}"
            lines.append(
                f"{status} <b>{name}</b> (#{chat.id})\n"
                f"   💬 {msg_count} сообщ. | ✏️ {edit_count} ред."
            )
            b.button(text=f"{status} {name}", callback_data=f"settings:{chat.id}")

    b.button(text="🔙 Главное меню", callback_data="back_main")
    b.adjust(1)

    await callback.message.edit_text(
        "\n".join(lines), reply_markup=b.as_markup()
    )
    await callback.answer()


@router.callback_query(F.data == "bot_status")
async def cb_bot_status(callback: CallbackQuery) -> None:
    async with async_session() as session:
        total_chats = (await session.execute(select(func.count(BusinessChat.id)))).scalar_one()
        active_chats = (
            await session.execute(
                select(func.count(BusinessChat.id)).where(BusinessChat.is_active == True)
            )
        ).scalar_one()
        total_msgs = (await session.execute(select(func.count(TrackedMessage.id)))).scalar_one()
        edited_msgs = (
            await session.execute(
                select(func.count(TrackedMessage.id)).where(TrackedMessage.is_edited == True)
            )
        ).scalar_one()
        deleted_msgs = (
            await session.execute(
                select(func.count(TrackedMessage.id)).where(TrackedMessage.is_deleted == True)
            )
        ).scalar_one()

    await callback.message.edit_text(
        f"📈 <b>Статус бота (глобально)</b>\n\n"
        f"💼 Всего чатов: {total_chats}\n"
        f"✅ Активных: {active_chats}\n"
        f"💬 Сообщений: {total_msgs}\n"
        f"✏️ Редактирований: {edited_msgs}\n"
        f"🗑 Удалений: {deleted_msgs}",
        reply_markup=kb_main_menu(),
    )
    await callback.answer()


@router.callback_query(F.data == "help")
async def cb_help(callback: CallbackQuery) -> None:
    await callback.message.edit_text(HELP_TEXT, reply_markup=kb_main_menu())
    await callback.answer()


@router.callback_query(F.data == "back_main")
async def cb_back_main(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        "👋 <b>Главное меню</b>\nВыберите действие:",
        reply_markup=kb_main_menu(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("settings:"))
async def cb_settings(callback: CallbackQuery) -> None:
    chat_id = int(callback.data.split(":")[1])

    async with async_session() as session:
        chat = await session.get(BusinessChat, chat_id)
        if not chat or chat.user_id != callback.from_user.id:
            await callback.answer("Чат не найден.", show_alert=True)
            return

        notif_result = await session.execute(
            select(UserNotification).where(
                UserNotification.user_id == callback.from_user.id,
                UserNotification.business_chat_id == chat_id,
            )
        )
        notif = notif_result.scalar_one_or_none()

    name = chat.chat_name or f"Чат #{chat.id}"
    status = "✅ Активен" if chat.is_active else "⛔ Отключён"
    msgs_icon = "✅" if (notif and notif.notify_new_messages) else "❌"
    edits_icon = "✅" if (notif and notif.notify_edits) else "❌"
    dels_icon = "✅" if (notif and notif.notify_deletions) else "❌"

    await callback.message.edit_text(
        f"⚙️ <b>Настройки: {name}</b>\n\n"
        f"Статус: {status}\n\n"
        f"Уведомления:\n"
        f"{msgs_icon} Новые сообщения\n"
        f"{edits_icon} Редактирования\n"
        f"{dels_icon} Удаления",
        reply_markup=kb_chat_settings(chat_id),
    )
    await callback.answer()


async def _toggle_notification(
    callback: CallbackQuery, chat_id: int, field: str
) -> None:
    async with async_session() as session:
        chat = await session.get(BusinessChat, chat_id)
        if not chat or chat.user_id != callback.from_user.id:
            await callback.answer("Чат не найден.", show_alert=True)
            return

        notif_result = await session.execute(
            select(UserNotification).where(
                UserNotification.user_id == callback.from_user.id,
                UserNotification.business_chat_id == chat_id,
            )
        )
        notif = notif_result.scalar_one_or_none()
        if not notif:
            notif = UserNotification(
                user_id=callback.from_user.id, business_chat_id=chat_id
            )
            session.add(notif)

        current = getattr(notif, field)
        setattr(notif, field, not current)
        await session.commit()

    await callback.answer(
        f"{'✅ Включено' if not current else '❌ Выключено'}",
        show_alert=False,
    )
    # Обновить страницу настроек
    await cb_settings(callback)


@router.callback_query(F.data.startswith("toggle_msgs:"))
async def cb_toggle_msgs(callback: CallbackQuery) -> None:
    chat_id = int(callback.data.split(":")[1])
    await _toggle_notification(callback, chat_id, "notify_new_messages")


@router.callback_query(F.data.startswith("toggle_edits:"))
async def cb_toggle_edits(callback: CallbackQuery) -> None:
    chat_id = int(callback.data.split(":")[1])
    await _toggle_notification(callback, chat_id, "notify_edits")


@router.callback_query(F.data.startswith("toggle_dels:"))
async def cb_toggle_dels(callback: CallbackQuery) -> None:
    chat_id = int(callback.data.split(":")[1])
    await _toggle_notification(callback, chat_id, "notify_deletions")


@router.callback_query(F.data.startswith("disable:"))
async def cb_disable_chat(callback: CallbackQuery) -> None:
    chat_id = int(callback.data.split(":")[1])

    async with async_session() as session:
        chat = await session.get(BusinessChat, chat_id)
        if not chat or chat.user_id != callback.from_user.id:
            await callback.answer("Чат не найден.", show_alert=True)
            return

        chat.is_active = False
        chat.deleted_at = datetime.utcnow()
        await session.commit()

    await callback.message.edit_text(
        f"⛔ Чат #{chat_id} отключён.", reply_markup=kb_main_menu()
    )
    await callback.answer("Чат отключён.")


@router.callback_query(F.data.startswith("history:"))
async def cb_history(callback: CallbackQuery) -> None:
    chat_id = int(callback.data.split(":")[1])
    await _show_history(callback, chat_id, 0)


@router.callback_query(F.data.startswith("hist_page:"))
async def cb_hist_page(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    chat_id = int(parts[1])
    page = int(parts[2])
    await _show_history(callback, chat_id, page)


async def _show_history(callback: CallbackQuery, chat_id: int, page: int) -> None:
    per_page = 5

    async with async_session() as session:
        chat = await session.get(BusinessChat, chat_id)
        if not chat or chat.user_id != callback.from_user.id:
            await callback.answer("Чат не найден.", show_alert=True)
            return

        total_result = await session.execute(
            select(func.count(TrackedMessage.id)).where(
                TrackedMessage.chat_id == chat_id
            )
        )
        total = total_result.scalar_one()

        msgs_result = await session.execute(
            select(TrackedMessage)
            .where(TrackedMessage.chat_id == chat_id)
            .order_by(TrackedMessage.created_at.desc())
            .offset(page * per_page)
            .limit(per_page)
        )
        messages = msgs_result.scalars().all()

    if not messages:
        await callback.message.edit_text(
            "📭 История сообщений пуста.",
            reply_markup=kb_chat_settings(chat_id),
        )
        await callback.answer()
        return

    lines = [f"📋 <b>История чата #{chat_id}</b> (стр. {page + 1})\n"]
    for msg in messages:
        direction = "📨" if msg.message_type == "incoming" else "📤"
        user = f"@{msg.from_username}" if msg.from_username else f"ID:{msg.from_user_id}"
        preview = (msg.text or "")[:100]
        if len(msg.text or "") > 100:
            preview += "…"
        flags = []
        if msg.is_edited:
            flags.append(f"✏️{msg.edit_count}")
        if msg.is_deleted:
            flags.append("🗑")
        flag_str = " ".join(flags)
        lines.append(
            f"{direction} <b>{user}</b> {flag_str}\n"
            f"   {preview}\n"
            f"   <i>{msg.created_at.strftime('%d.%m.%Y %H:%M')}</i>\n"
        )

    has_prev = page > 0
    has_next = (page + 1) * per_page < total

    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=kb_history(chat_id, page, has_prev, has_next),
    )
    await callback.answer()


# ─── Уведомления ──────────────────────────────────────────────────────────────


async def notify_user(
    session: AsyncSession,
    bot: Bot,
    chat: BusinessChat,
    message: TrackedMessage,
    event_type: str,
) -> None:
    result = await session.execute(
        select(UserNotification).where(
            UserNotification.user_id == chat.user_id,
            UserNotification.business_chat_id == chat.id,
        )
    )
    notif = result.scalar_one_or_none()

    if notif:
        if event_type == "new_message" and not notif.notify_new_messages:
            return
        if event_type == "edited" and not notif.notify_edits:
            return
        if event_type == "deleted" and not notif.notify_deletions:
            return

    emoji_map = {
        "new_message": "💬",
        "edited": "✏️",
        "deleted": "🗑",
    }
    type_map = {
        "new_message": "Новое сообщение",
        "edited": "Сообщение отредактировано",
        "deleted": "Сообщение удалено",
    }

    emoji = emoji_map.get(event_type, "📩")
    type_label = type_map.get(event_type, event_type)
    from_name = f"@{message.from_username}" if message.from_username else "Неизвестный"

    lines = [
        f"{emoji} <b>{type_label}</b>",
        f"📋 Чат: #{chat.id}",
        f"👤 От: {from_name}",
    ]

    if event_type in ("new_message", "edited"):
        preview = (message.text or "")[:200]
        if len(message.text or "") > 200:
            preview += "…"
        lines.append(f"💬 Текст: {preview}")

    if event_type == "edited":
        lines.append(f"🔄 Редактирований: {message.edit_count}")

    text = "\n".join(lines)

    try:
        await bot.send_message(chat.user_id, text)
    except Exception as e:
        logger.warning(f"Не удалось отправить уведомление пользователю {chat.user_id}: {e}")


# ─── Запуск ────────────────────────────────────────────────────────────────────


async def main() -> None:
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN не задан! Установите переменную окружения BOT_TOKEN.")
        sys.exit(1)

    await init_db()

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(router)

    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Бот запущен. Ожидание обновлений...")

    try:
        await dp.start_polling(bot)
    except KeyboardInterrupt:
        logger.info("Бот остановлен.")
    finally:
        await bot.session.close()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
