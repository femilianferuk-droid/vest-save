import os
import asyncio
import logging
import asyncpg
from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

# --- Конфигурация ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_URL = "postgresql://bothost_db_0f4e9694f89c:Xp9i3BN4-YQq_SyFVK0NxvImLPnP7BBF6gjNFDTW5VI@node1.pghost.ru:15756/bothost_db_0f4e9694f89c"
WEB_APP_URL = "https://vest-save.vercel.app/" # Ссылка на твой Vercel
API_PORT = 8080

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# --- База Данных ---
async def init_db():
    conn = await asyncpg.connect(DB_URL)
    await conn.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            id SERIAL PRIMARY KEY,
            chat_id BIGINT,
            message_id BIGINT,
            text TEXT,
            status VARCHAR(50), -- 'active', 'edited', 'deleted'
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    ''')
    await conn.close()

async def get_db_pool():
    return await asyncpg.create_pool(DB_URL)

# --- Обработчики Бота ---
@dp.message(CommandStart())
async def cmd_start(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Открыть Vest Save", web_app=WebAppInfo(url=WEB_APP_URL))]
    ])
    await message.answer("Привет! Я бот <b>Vest Save</b>.\nНажми кнопку ниже, чтобы открыть Mini App.", reply_markup=kb)

@dp.business_message()
async def handle_new_business_message(message: Message, db_pool: asyncpg.Pool):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO messages (chat_id, message_id, text, status) VALUES ($1, $2, $3, $4)",
            message.chat.id, message.message_id, message.text or "[Вложение]", "active"
        )

@dp.edited_business_message()
async def handle_edited_business_message(message: Message, db_pool: asyncpg.Pool):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE messages SET text = $1, status = $2, updated_at = CURRENT_TIMESTAMP WHERE chat_id = $3 AND message_id = $4",
            message.text or "[Вложение]", "edited", message.chat.id, message.message_id
        )

@dp.deleted_business_messages()
async def handle_deleted_business_messages(message, db_pool: asyncpg.Pool):
    async with db_pool.acquire() as conn:
        for msg_id in message.message_ids:
            await conn.execute(
                "UPDATE messages SET status = $1, updated_at = CURRENT_TIMESTAMP WHERE chat_id = $2 AND message_id = $3",
                "deleted", message.chat.id, msg_id
            )

# --- API Эндпоинты для Vercel ---
async def api_get_chats(request):
    pool: asyncpg.Pool = request.app['db_pool']
    async with pool.acquire() as conn:
        records = await conn.fetch("SELECT chat_id, COUNT(*) as count FROM messages GROUP BY chat_id")
        data = [{"chat_id": r["chat_id"], "count": r["count"]} for r in records]
    return web.json_response(data)

async def api_get_messages(request):
    chat_id = request.query.get('chat_id')
    if not chat_id:
        return web.json_response({"error": "Missing chat_id"}, status=400)
    
    pool: asyncpg.Pool = request.app['db_pool']
    async with pool.acquire() as conn:
        records = await conn.fetch(
            "SELECT message_id, text, status FROM messages WHERE chat_id = $1 ORDER BY id DESC LIMIT 50",
            int(chat_id)
        )
        data = [{"message_id": r["message_id"], "text": r["text"], "status": r["status"]} for r in records]
    return web.json_response(data)

async def api_send_message(request):
    data = await request.json()
    chat_id = data.get('chat_id')
    text = data.get('text')
    
    if not chat_id or not text:
        return web.json_response({"error": "Bad request"}, status=400)
    
    try:
        await bot.send_message(chat_id=int(chat_id), text=text)
        return web.json_response({"success": True})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

# --- Настройка CORS Middleware (Важно для Vercel) ---
@web.middleware
async def cors_middleware(request, handler):
    if request.method == 'OPTIONS':
        return web.Response(headers={
            'Access-Control-Allow-Origin': WEB_APP_URL,
            'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
            'Access-Control-Allow-Headers': 'Content-Type',
        })
    try:
        response = await handler(request)
        response.headers['Access-Control-Allow-Origin'] = WEB_APP_URL
        return response
    except web.HTTPException as ex:
        ex.headers['Access-Control-Allow-Origin'] = WEB_APP_URL
        raise

# --- Запуск ---
async def main():
    await init_db()
    db_pool = await get_db_pool()
    dp.workflow_data.update({'db_pool': db_pool})

    app = web.Application(middlewares=[cors_middleware])
    app['db_pool'] = db_pool
    app.router.add_get('/api/chats', api_get_chats)
    app.router.add_get('/api/messages', api_get_messages)
    app.router.add_post('/api/send', api_send_message)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', API_PORT)
    await site.start()
    logging.info(f"API server started on port {API_PORT}")

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        await runner.cleanup()
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
