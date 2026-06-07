import os
import asyncio
import logging
import asyncpg
from aiogram import Bot, Dispatcher, F, html
from aiogram.filters import CommandStart
from aiogram.types import Message, BusinessConnection, BusinessMessagesDeleted
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

# ==========================================
# КОНФИГУРАЦИЯ
# ==========================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "ТВОЙ_ТОКЕН")
DB_URL = "postgresql://bothost_db_0f4e9694f89c:Xp9i3BN4-YQq_SyFVK0NxvImLPnP7BBF6gjNFDTW5VI@node1.pghost.ru:15756/bothost_db_0f4e9694f89c"

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# ==========================================
# БАЗА ДАННЫХ
# ==========================================
async def init_db():
    conn = await asyncpg.connect(DB_URL)
    
    # Таблица для хранения связей: ID подключения -> ID пользователя (владельца)
    await conn.execute('''
        CREATE TABLE IF NOT EXISTS connections (
            connection_id VARCHAR(255) PRIMARY KEY,
            user_id BIGINT
        );
    ''')
    
    # Таблица сообщений теперь привязана к connection_id
    await conn.execute('''
        CREATE TABLE IF NOT EXISTS business_messages (
            id SERIAL PRIMARY KEY,
            connection_id VARCHAR(255),
            chat_id BIGINT,
            message_id BIGINT,
            text TEXT,
            date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(chat_id, message_id)
        );
    ''')
    await conn.close()

async def get_db_pool():
    return await asyncpg.create_pool(DB_URL)

# ==========================================
# ОБРАБОТЧИКИ
# ==========================================

@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "👋 Привет! Я бот <b>Vest Save</b>.\n\n"
        "Я умею сохранять удаленные и измененные сообщения из ваших рабочих чатов.\n\n"
        "<b>Как мной пользоваться:</b>\n"
        "1. Зайдите в настройки Telegram -> Telegram Business.\n"
        "2. Подключите меня к своему аккаунту.\n"
        "3. Как только кто-то удалит или изменит сообщение в вашем чате, я мгновенно пришлю лог сюда, в этот диалог!"
    )

# 0. Отслеживаем подключение бота к бизнес-аккаунту
@dp.business_connection()
async def handle_business_connection(connection: BusinessConnection, db_pool: asyncpg.Pool):
    async with db_pool.acquire() as conn:
        if connection.is_enabled:
            # Пользователь подключил бота: сохраняем его user_id
            await conn.execute(
                "INSERT INTO connections (connection_id, user_id) VALUES ($1, $2) ON CONFLICT (connection_id) DO UPDATE SET user_id = $2",
                connection.id, connection.user.id
            )
            try:
                await bot.send_message(connection.user.id, "✅ Вы успешно подключили меня к бизнес-аккаунту! Теперь я слежу за чатами.")
            except:
                pass # Если пользователь заблокировал бота
        else:
            # Пользователь отключил бота: удаляем связь
            await conn.execute("DELETE FROM connections WHERE connection_id = $1", connection.id)

# 1. Логируем новые сообщения
@dp.business_message()
async def handle_new_message(message: Message, db_pool: asyncpg.Pool):
    # Если сообщение не привязано к бизнес-подключению, игнорируем
    if not message.business_connection_id:
        return

    text = message.text or message.caption or "[Медиа/Голосовое/Стикер]"
    
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO business_messages (connection_id, chat_id, message_id, text) 
            VALUES ($1, $2, $3, $4) 
            ON CONFLICT (chat_id, message_id) DO NOTHING
            """,
            message.business_connection_id, message.chat.id, message.message_id, text
        )

# 2. Отслеживаем измененные сообщения
@dp.edited_business_message()
async def handle_edited_message(message: Message, db_pool: asyncpg.Pool):
    if not message.business_connection_id:
        return

    new_text = message.text or message.caption or "[Медиа/Голосовое/Стикер]"
    
    async with db_pool.acquire() as conn:
        # Узнаем, кому отправлять уведомление (ищем владельца)
        owner = await conn.fetchrow("SELECT user_id FROM connections WHERE connection_id = $1", message.business_connection_id)
        if not owner:
            return

        user_id = owner['user_id']

        # Ищем старый текст
        old_record = await conn.fetchrow(
            "SELECT text FROM business_messages WHERE chat_id = $1 AND message_id = $2",
            message.chat.id, message.message_id
        )
        old_text = old_record['text'] if old_record else "[Текст не найден в базе]"
        
        # Обновляем текст в базе
        await conn.execute(
            "UPDATE business_messages SET text = $1 WHERE chat_id = $2 AND message_id = $3",
            new_text, message.chat.id, message.message_id
        )

    # Формируем имя собеседника или название чата для удобства
    chat_title = message.chat.title or message.chat.first_name or str(message.chat.id)

    alert_text = (
        f"✏️ <b>Изменено сообщение</b> в чате: <b>{chat_title}</b>\n\n"
        f"<b>❌ Было:</b>\n<blockquote>{html.quote(old_text)}</blockquote>\n\n"
        f"<b>✅ Стало:</b>\n<blockquote>{html.quote(new_text)}</blockquote>"
    )
    
    try:
        await bot.send_message(user_id, alert_text)
    except Exception as e:
        logging.error(f"Не удалось отправить лог пользователю {user_id}: {e}")

# 3. Отслеживаем удаленные сообщения
@dp.business_messages_deleted()
async def handle_deleted_messages(update: BusinessMessagesDeleted, db_pool: asyncpg.Pool):
    async with db_pool.acquire() as conn:
        # Ищем владельца подключения
        owner = await conn.fetchrow("SELECT user_id FROM connections WHERE connection_id = $1", update.business_connection_id)
        if not owner:
            return
            
        user_id = owner['user_id']
        chat_title = update.chat.title or update.chat.first_name or str(update.chat.id)

        # Проходимся по всем удаленным сообщениям
        for msg_id in update.message_ids:
            record = await conn.fetchrow(
                "SELECT text FROM business_messages WHERE chat_id = $1 AND message_id = $2",
                update.chat.id, msg_id
            )
            
            deleted_text = record['text'] if record else "[Текст не найден в базе]"
            
            alert_text = (
                f"🗑 <b>Удалено сообщение</b> в чате: <b>{chat_title}</b>\n\n"
                f"<b>Текст:</b>\n<blockquote>{html.quote(deleted_text)}</blockquote>"
            )
            
            try:
                await bot.send_message(user_id, alert_text)
            except Exception as e:
                logging.error(f"Не удалось отправить лог пользователю {user_id}: {e}")

# ==========================================
# ЗАПУСК БОТА
# ==========================================
async def main():
    await init_db()
    db_pool = await get_db_pool()
    
    dp.workflow_data.update({'db_pool': db_pool})

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logging.info("✅ Мультипользовательский бот-логгер успешно запущен!")
        await dp.start_polling(bot)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
