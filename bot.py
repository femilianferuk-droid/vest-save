import asyncio
import logging
import os
import sys
from datetime import datetime
from typing import Optional, List

from aiogram import Bot, Dispatcher, F, Router, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (
    BusinessConnection,
    BusinessMessagesDeleted,
    Message,
    CallbackQuery,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    select,
    func,
    JSON,
)
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# ==================== КОНФИГУРАЦИЯ ====================

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("❌ Не задан BOT_TOKEN в переменных окружения!")

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://bothost_db_0f4e9694f89c:Xp9i3BN4-YQq_SyFVK0NxvImLPnP7BBF6gjNFDTW5VI@node1.pghost.ru:15756/bothost_db_0f4e9694f89c"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

# ==================== БАЗА ДАННЫХ ====================

engine = create_async_engine(DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class BusinessChat(Base):
    __tablename__ = "business_chats"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    business_connection_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    chat_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    messages: Mapped[List["TrackedMessage"]] = relationship(back_populates="chat", cascade="all, delete-orphan")


class TrackedMessage(Base):
    __tablename__ = "tracked_messages"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("business_chats.id"), index=True)
    business_message_id: Mapped[int] = mapped_column(BigInteger)
    message_type: Mapped[str] = mapped_column(String(50))
    from_user_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    from_username: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    reply_to_message_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    is_edited: Mapped[bool] = mapped_column(Boolean, default=False)
    edit_count: Mapped[int] = mapped_column(Integer, default=0)
    edit_history: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    chat: Mapped["BusinessChat"] = relationship(back_populates="messages")


class UserNotification(Base):
    __tablename__ = "user_notifications"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    business_chat_id: Mapped[int] = mapped_column(ForeignKey("business_chats.id"))
    notify_new_messages: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_edits: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_deletions: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("✅ База данных инициализирована")


# ==================== КЛАВИАТУРЫ ====================

def get_main_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="📊 Мои бизнес-чаты", callback_data="list_chats")
    builder.button(text="ℹ️ Статус бота", callback_data="bot_status")
    builder.button(text="❓ Помощь", callback_data="help")
    builder.adjust(1)
    return builder.as_markup()


def get_chat_settings_keyboard(chat_id: int):
    builder = InlineKeyboardBuilder()
    builder.button(text="✉️ Новые сообщения", callback_data=f"toggle_msgs:{chat_id}")
    builder.button(text="✏️ Редактирования", callback_data=f"toggle_edits:{chat_id}")
    builder.button(text="🗑 Удаления", callback_data=f"toggle_dels:{chat_id}")
    builder.button(text="📋 История сообщений", callback_data=f"history:{chat_id}")
    builder.button(text="❌ Отключить чат", callback_data=f"disable:{chat_id}")
    builder.button(text="🔙 Назад", callback_data="list_chats")
    builder.adjust(2, 2, 1, 1)
    return builder.as_markup()


def get_history_keyboard(chat_id: int, page: int = 0):
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Назад", callback_data=f"hist_page:{chat_id}:{page-1}")
    builder.button(text="➡️ Вперед", callback_data=f"hist_page:{chat_id}:{page+1}")
    builder.button(text="🔙 К настройкам", callback_data=f"settings:{chat_id}")
    builder.adjust(2, 1)
    return builder.as_markup()


# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================

async def get_business_chat(session: AsyncSession, business_connection_id: str) -> Optional[BusinessChat]:
    result = await session.execute(
        select(BusinessChat).where(
            BusinessChat.business_connection_id == business_connection_id,
            BusinessChat.is_active == True,
        )
    )
    return result.scalar_one_or_none()


async def notify_user(session: AsyncSession, chat: BusinessChat, message: TrackedMessage, event_type: str):
    notification = await session.execute(
        select(UserNotification).where(
            UserNotification.business_chat_id == chat.id,
            UserNotification.user_id == chat.user_id,
        )
    )
    notification = notification.scalar_one_or_none()
    if not notification:
        return

    should_notify = False
    event_emoji = ""
    event_text = ""

    if event_type == "new_message" and notification.notify_new_messages:
        should_notify = True
        event_emoji = "📨"
        event_text = "Новое сообщение"
    elif event_type == "edited" and notification.notify_edits:
        should_notify = True
        event_emoji = "✏️"
        event_text = "Сообщение изменено"
    elif event_type == "deleted" and notification.notify_deletions:
        should_notify = True
        event_emoji = "🗑"
        event_text = "Сообщение удалено"

    if not should_notify:
        return

    notification_text = f"{event_emoji} *{event_text}*\n\n"
    notification_text += f"📋 Чат: #{chat.id}\n"
    notification_text += f"👤 От: {message.from_username or 'Неизвестный'}\n"

    if event_type != "deleted":
        text_preview = (message.text or "")[:200]
        if len(message.text or "") > 200:
            text_preview += "..."
        notification_text += f"💬 Текст: {text_preview}\n"

    if event_type == "edited":
        notification_text += f"🔄 Редактирований: {message.edit_count}\n"

    try:
        await bot.send_message(chat.user_id, notification_text, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Не удалось отправить уведомление: {e}")


async def list_user_chats(message: Message, user_id: int, edit: bool = False):
    async with async_session() as session:
        chats = await session.execute(
            select(BusinessChat).where(
                BusinessChat.user_id == user_id,
                BusinessChat.is_active == True,
            )
        )
        chats = chats.scalars().all()

        if not chats:
            text = "📭 *У вас нет активных бизнес-чатов*\n\nПодключите бота в Telegram Business."
            markup = get_main_keyboard()
        else:
            text = f"📊 *Ваши бизнес-чаты ({len(chats)})*\n\n"
            builder = InlineKeyboardBuilder()

            for chat in chats:
                msg_count = await session.execute(
                    select(func.count(TrackedMessage.id)).where(TrackedMessage.chat_id == chat.id)
                )
                msg_count = msg_count.scalar() or 0

                edited_count = await session.execute(
                    select(func.count(TrackedMessage.id)).where(
                        TrackedMessage.chat_id == chat.id,
                        TrackedMessage.is_edited == True,
                    )
                )
                edited_count = edited_count.scalar() or 0

                text += f"💼 *Чат #{chat.id}*\n"
                text += f"📝 Сообщений: {msg_count}\n"
                text += f"✏️ Редактирований: {edited_count}\n"
                text += f"🔗 ID: `{chat.business_connection_id[:12]}...`\n\n"

                builder.button(text=f"⚙️ Чат #{chat.id}", callback_data=f"settings:{chat.id}")

            builder.button(text="🔙 Назад", callback_data="back_main")
            builder.adjust(1)
            markup = builder.as_markup()

        if edit:
            await message.edit_text(text, reply_markup=markup, parse_mode=ParseMode.MARKDOWN)
        else:
            await message.answer(text, reply_markup=markup, parse_mode=ParseMode.MARKDOWN)


async def show_bot_status(message: Message, edit: bool = False):
    async with async_session() as session:
        total_chats = await session.execute(select(func.count(BusinessChat.id)))
        total_chats = total_chats.scalar() or 0

        active_chats = await session.execute(
            select(func.count(BusinessChat.id)).where(BusinessChat.is_active == True)
        )
        active_chats = active_chats.scalar() or 0

        total_messages = await session.execute(select(func.count(TrackedMessage.id)))
        total_messages = total_messages.scalar() or 0

        edited_messages = await session.execute(
            select(func.count(TrackedMessage.id)).where(TrackedMessage.is_edited == True)
        )
        edited_messages = edited_messages.scalar() or 0

        deleted_messages = await session.execute(
            select(func.count(TrackedMessage.id)).where(TrackedMessage.is_deleted == True)
        )
        deleted_messages = deleted_messages.scalar() or 0

        text = (
            "📊 *Статус бота*\n\n"
            f"💼 Всего бизнес-чатов: {total_chats}\n"
            f"🟢 Активных: {active_chats}\n"
            f"📝 Всего сообщений: {total_messages}\n"
            f"✏️ Редактирований: {edited_messages}\n"
            f"🗑 Удалений: {deleted_messages}\n"
        )

        if edit:
            await message.edit_text(text, reply_markup=get_main_keyboard(), parse_mode=ParseMode.MARKDOWN)
        else:
            await message.answer(text, reply_markup=get_main_keyboard(), parse_mode=ParseMode.MARKDOWN)


async def show_chat_history(message: Message, chat_id: int, page: int = 0, user_id: int = None, edit: bool = False):
    per_page = 5
    offset = page * per_page

    async with async_session() as session:
        chat = await session.get(BusinessChat, chat_id)
        if not chat or (user_id and chat.user_id != user_id):
            text = "❌ Чат не найден или доступ запрещён"
            if edit:
                await message.edit_text(text)
            else:
                await message.answer(text)
            return

        messages_query = await session.execute(
            select(TrackedMessage)
            .where(TrackedMessage.chat_id == chat_id)
            .order_by(TrackedMessage.created_at.desc())
            .offset(offset)
            .limit(per_page)
        )
        messages = messages_query.scalars().all()

        total_count = await session.execute(
            select(func.count(TrackedMessage.id)).where(TrackedMessage.chat_id == chat_id)
        )
        total_count = total_count.scalar() or 0

        total_pages = (total_count + per_page - 1) // per_page if total_count > 0 else 1
        text = f"📋 *История чата #{chat.id}*\n"
        text += f"Страница {page + 1}/{total_pages}\n\n"

        for msg in messages:
            status = "🗑 " if msg.is_deleted else "✏️ " if msg.is_edited else ""
            msg_type = "📥" if msg.message_type == "incoming" else "📤"
            text += f"{status}{msg_type} *{msg.from_username or 'Unknown'}*\n"
            text += f"{(msg.text or '')[:100]}\n"
            if msg.is_edited:
                text += f"_ред. {msg.edit_count} раз(а)_\n"
            text += "\n"

        if edit:
            await message.edit_text(
                text, reply_markup=get_history_keyboard(chat_id, page), parse_mode=ParseMode.MARKDOWN
            )
        else:
            await message.answer(
                text, reply_markup=get_history_keyboard(chat_id, page), parse_mode=ParseMode.MARKDOWN
            )


async def toggle_notification(callback: CallbackQuery, field: str, description: str):
    chat_id = int(callback.data.split(":")[1])
    async with async_session() as session:
        notification = await session.execute(
            select(UserNotification).where(
                UserNotification.user_id == callback.from_user.id,
                UserNotification.business_chat_id == chat_id,
            )
        )
        notification = notification.scalar_one_or_none()

        if notification:
            current_value = getattr(notification, field)
            setattr(notification, field, not current_value)
            await session.commit()
            status = "включены" if not current_value else "выключены"
            await callback.answer(f"Уведомления о {description} {status}")
            await cb_chat_settings(callback)
        else:
            await callback.answer("Настройки не найдены", show_alert=True)


# ==================== ОБРАБОТЧИК УДАЛЕНИЙ ====================

async def handle_deleted_business_messages(event: BusinessMessagesDeleted, bot_instance: Bot):
    """Отдельная функция-обработчик удаления бизнес-сообщений"""
    if not event.business_connection_id:
        return

    async with async_session() as session:
        chat = await get_business_chat(session, event.business_connection_id)
        if not chat:
            return

        for message_id in event.message_ids:
            existing = await session.execute(
                select(TrackedMessage).where(
                    TrackedMessage.chat_id == chat.id,
                    TrackedMessage.business_message_id == message_id,
                )
            )
            tracked_msg = existing.scalar_one_or_none()

            if tracked_msg and not tracked_msg.is_deleted:
                tracked_msg.is_deleted = True
                tracked_msg.deleted_at = datetime.utcnow()
                await session.commit()

                await notify_user(session, chat, tracked_msg, "deleted")


# ==================== РОУТЕР ====================

router = Router()


# Команды
@router.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "🤖 *Бот отслеживания бизнес-чатов*\n\n"
        "Я помогаю отслеживать сообщения, изменения и удаления в бизнес-чатах.\n\n"
        "📌 *Как использовать:*\n"
        "1. Подключите бота как бизнес-бота в Telegram Business\n"
        "2. Бот автоматически начнёт отслеживать чаты\n"
        "3. Настройте уведомления под себя\n\n"
        "Используйте кнопки ниже для управления.",
        reply_markup=get_main_keyboard(),
        parse_mode=ParseMode.MARKDOWN,
    )


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "📖 *Справка по боту*\n\n"
        "*Основные функции:*\n"
        "• Отслеживание входящих и исходящих сообщений\n"
        "• Отслеживание редактирования сообщений\n"
        "• Отслеживание удаления сообщений\n"
        "• История сообщений\n"
        "• Уведомления о всех изменениях\n\n"
        "Просто подключите бота в Telegram Business!",
        parse_mode=ParseMode.MARKDOWN,
    )


@router.message(Command("chats"))
async def cmd_chats(message: Message):
    await list_user_chats(message, user_id=message.from_user.id)


@router.message(Command("status"))
async def cmd_status(message: Message):
    await show_bot_status(message)


# Бизнес-подключение
@router.business_connection()
async def on_business_connection(business_connection: BusinessConnection):
    async with async_session() as session:
        if business_connection.is_enabled:
            existing_chat = await session.execute(
                select(BusinessChat).where(BusinessChat.business_connection_id == business_connection.id)
            )
            chat = existing_chat.scalar_one_or_none()

            if not chat:
                chat = BusinessChat(
                    business_connection_id=business_connection.id,
                    user_id=business_connection.user.id,
                    chat_name=f"Business Chat {business_connection.id[:8]}",
                    is_active=True,
                )
                session.add(chat)
                await session.commit()
                await session.refresh(chat)
                logger.info(f"✅ Новый бизнес-чат: {chat.id}")

                notification = UserNotification(
                    user_id=business_connection.user.id,
                    business_chat_id=chat.id,
                )
                session.add(notification)
                await session.commit()

                try:
                    await bot.send_message(
                        business_connection.user.id,
                        f"✅ *Бизнес-чат подключён!*\n\n"
                        f"Чат #{chat.id} теперь отслеживается.\n"
                        f"Используйте /chats для просмотра.",
                        parse_mode=ParseMode.MARKDOWN,
                    )
                except Exception as e:
                    logger.error(f"Не удалось отправить уведомление: {e}")
            else:
                chat.is_active = True
                chat.deleted_at = None
                await session.commit()
                logger.info(f"🔄 Бизнес-чат переподключён: {chat.id}")
        else:
            chat_result = await session.execute(
                select(BusinessChat).where(BusinessChat.business_connection_id == business_connection.id)
            )
            chat = chat_result.scalar_one_or_none()
            if chat:
                chat.is_active = False
                chat.deleted_at = datetime.utcnow()
                await session.commit()
                logger.info(f"❌ Бизнес-чат отключён: {chat.id}")


# Бизнес-сообщения
@router.business_message()
async def on_business_message(message: Message):
    if not message.business_connection_id:
        return

    async with async_session() as session:
        chat = await get_business_chat(session, message.business_connection_id)
        if not chat:
            return

        tracked_msg = TrackedMessage(
            chat_id=chat.id,
            business_message_id=message.message_id,
            message_type="incoming" if message.from_user.id != message.business_connection_id else "outgoing",
            from_user_id=message.from_user.id,
            from_username=message.from_user.username or message.from_user.full_name,
            text=message.text or message.caption or "[Media/Non-text]",
            reply_to_message_id=message.reply_to_message.message_id if message.reply_to_message else None,
        )
        session.add(tracked_msg)
        await session.commit()
        await session.refresh(tracked_msg)

        await notify_user(session, chat, tracked_msg, "new_message")


# Редактирование бизнес-сообщений
@router.edited_business_message()
async def on_edited_business_message(message: Message):
    if not message.business_connection_id:
        return

    async with async_session() as session:
        chat = await get_business_chat(session, message.business_connection_id)
        if not chat:
            return

        existing = await session.execute(
            select(TrackedMessage).where(
                TrackedMessage.chat_id == chat.id,
                TrackedMessage.business_message_id == message.message_id,
            )
        )
        tracked_msg = existing.scalar_one_or_none()

        if tracked_msg:
            edit_history = tracked_msg.edit_history or []
            edit_history.append({
                "old_text": tracked_msg.text,
                "new_text": message.text or message.caption or "[Media/Non-text]",
                "edited_at": datetime.utcnow().isoformat(),
            })

            tracked_msg.text = message.text or message.caption or "[Media/Non-text]"
            tracked_msg.is_edited = True
            tracked_msg.edit_count += 1
            tracked_msg.edit_history = edit_history
            await session.commit()

            await notify_user(session, chat, tracked_msg, "edited")


# Callback-хендлеры
@router.callback_query(F.data == "list_chats")
async def cb_list_chats(callback: CallbackQuery):
    await list_user_chats(callback.message, user_id=callback.from_user.id, edit=True)
    await callback.answer()


@router.callback_query(F.data == "bot_status")
async def cb_bot_status(callback: CallbackQuery):
    await show_bot_status(callback.message, edit=True)
    await callback.answer()


@router.callback_query(F.data == "help")
async def cb_help(callback: CallbackQuery):
    await callback.message.edit_text(
        "📖 *Справка по боту*\n\n"
        "*Основные функции:*\n"
        "• Отслеживание входящих и исходящих сообщений\n"
        "• Отслеживание редактирования сообщений\n"
        "• Отслеживание удаления сообщений\n"
        "• История сообщений\n"
        "• Уведомления о всех изменениях",
        reply_markup=get_main_keyboard(),
        parse_mode=ParseMode.MARKDOWN,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("settings:"))
async def cb_chat_settings(callback: CallbackQuery):
    chat_id = int(callback.data.split(":")[1])
    async with async_session() as session:
        chat = await session.get(BusinessChat, chat_id)
        if not chat:
            await callback.answer("Чат не найден", show_alert=True)
            return

        notification = await session.execute(
            select(UserNotification).where(
                UserNotification.user_id == callback.from_user.id,
                UserNotification.business_chat_id == chat_id,
            )
        )
        notification = notification.scalar_one_or_none()

        status_text = (
            f"⚙️ *Настройки чата #{chat.id}*\n\n"
            f"📊 *Статус уведомлений:*\n"
            f"✉️ Новые сообщения: {'✅ Вкл' if notification and notification.notify_new_messages else '❌ Выкл'}\n"
            f"✏️ Редактирования: {'✅ Вкл' if notification and notification.notify_edits else '❌ Выкл'}\n"
            f"🗑 Удаления: {'✅ Вкл' if notification and notification.notify_deletions else '❌ Выкл'}\n"
        )

        await callback.message.edit_text(
            status_text,
            reply_markup=get_chat_settings_keyboard(chat_id),
            parse_mode=ParseMode.MARKDOWN,
        )
        await callback.answer()


@router.callback_query(F.data.startswith("toggle_msgs:"))
async def cb_toggle_messages(callback: CallbackQuery):
    await toggle_notification(callback, "notify_new_messages", "новые сообщения")


@router.callback_query(F.data.startswith("toggle_edits:"))
async def cb_toggle_edits(callback: CallbackQuery):
    await toggle_notification(callback, "notify_edits", "редактирования")


@router.callback_query(F.data.startswith("toggle_dels:"))
async def cb_toggle_deletions(callback: CallbackQuery):
    await toggle_notification(callback, "notify_deletions", "удаления")


@router.callback_query(F.data.startswith("disable:"))
async def cb_disable_chat(callback: CallbackQuery):
    chat_id = int(callback.data.split(":")[1])
    async with async_session() as session:
        chat = await session.get(BusinessChat, chat_id)
        if chat:
            chat.is_active = False
            chat.deleted_at = datetime.utcnow()
            await session.commit()
            await callback.answer("❌ Чат отключён", show_alert=True)
            await callback.message.edit_text(
                "❌ *Чат отключён*\n\nИспользуйте /chats для просмотра списка.",
                reply_markup=get_main_keyboard(),
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            await callback.answer("Чат не найден", show_alert=True)


@router.callback_query(F.data.startswith("history:"))
async def cb_show_history(callback: CallbackQuery):
    chat_id = int(callback.data.split(":")[1])
    await show_chat_history(callback.message, chat_id, user_id=callback.from_user.id)
    await callback.answer()


@router.callback_query(F.data.startswith("hist_page:"))
async def cb_history_page(callback: CallbackQuery):
    parts = callback.data.split(":")
    chat_id = int(parts[1])
    page = int(parts[2])
    await show_chat_history(callback.message, chat_id, page, user_id=callback.from_user.id, edit=True)
    await callback.answer()


@router.callback_query(F.data == "back_main")
async def cb_back_main(callback: CallbackQuery):
    await callback.message.edit_text(
        "🤖 *Главное меню*\n\nВыберите действие:",
        reply_markup=get_main_keyboard(),
        parse_mode=ParseMode.MARKDOWN,
    )
    await callback.answer()


# ==================== ИНИЦИАЛИЗАЦИЯ И ЗАПУСК ====================

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)

dp = Dispatcher()
dp.include_router(router)

# Регистрируем обработчик удаления через business_messages_deleted
dp.business_messages_deleted.register(handle_deleted_business_messages)


async def main():
    logger.info("🤖 Запуск бота...")
    await init_db()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("🛑 Бот остановлен")
    except Exception as e:
        logger.error(f"❌ Критическая ошибка: {e}")
