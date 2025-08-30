# Точка входа приложения. Запускается командой: python -m src
import os
import asyncio
import aiohttp
import time
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart, Command
from aiogram.types import ChatMemberUpdated, ChatMember

# Читаем переменные окружения, которые придут из .env (на сервере)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OLLAMA = os.getenv("OLLAMA_ENDPOINT", "http://host.docker.internal:11434")
MODEL = os.getenv("MODEL_NAME", "qwen2.5:7b-instruct-q4_K_M")

# Системный промпт, задается один раз при запуске
SYSTEM_PROMPT = os.getenv("SYSTEM_PROMPT", 
    "Отвечай нейтрально, с подколкой. "
    "Если пользователь грубит — отвечай жёстче. "
    "Можешь использовать нецензурную лексику."
)

# Проверяем, поддерживает ли модель chat API
USE_CHAT_API = os.getenv("USE_CHAT_API", "true").lower() == "true"

bot = Bot(TELEGRAM_TOKEN)
dp = Dispatcher()

@dp.message(CommandStart())
async def start(msg: types.Message):
    await msg.answer("Привет! Я на месте. Спроси меня что-нибудь.")

@dp.message(Command("help"))
async def help_command(msg: types.Message):
    help_text = """
🤖 **Как использовать бота:**

1. **Добавьте меня в группу** - я автоматически поприветствую всех
2. **Отправьте сообщение** в ответ на любое сообщение в группе
3. **Используйте команды:**
   - `/start` - приветствие
   - `/help` - эта справка
   - `/ping` - проверить, что бот работает

💡 **Совет:** Просто ответьте (reply) на любое сообщение в группе, и я отвечу!
    """
    await msg.answer(help_text, parse_mode="Markdown")

@dp.message(Command("ping"))
async def ping_command(msg: types.Message):
    await msg.answer("🏓 Понг! Бот работает!")

@dp.chat_member()
async def on_chat_member_update(event: ChatMemberUpdated):
    """Обрабатываем добавление/удаление бота из группы"""
    if event.new_chat_member.user.id == bot.id:
        if event.new_chat_member.status == ChatMember.MEMBER:
            # Бота добавили в группу
            welcome_text = """
🎉 **Привет всем! Я новый участник группы!**

🤖 **Как со мной общаться:**
• Просто ответьте (reply) на любое сообщение в группе
• Используйте команду `/help` для подробной справки
• Команда `/ping` чтобы проверить, что я работаю

Готов отвечать на ваши вопросы! 🚀
            """
            await event.chat.send_message(welcome_text, parse_mode="Markdown")
        elif event.new_chat_member.status == ChatMember.LEFT:
            # Бота удалили из группы
            await event.chat.send_message("👋 Пока всем! Было приятно пообщаться!")

@dp.message()
async def handle(msg: types.Message):
    # Проверяем, что это reply-сообщение
    if not msg.reply_to_message:
        return  # Игнорируем сообщения без reply
    
    text = msg.text or ""
    if text == "пошёл нахуй":
        await msg.answer("Сам пошёл нахуй!")
        return
    if len(text) > 1000:
        await msg.answer("Ты еблан, пиши короче!")
        return
    
    start_time = time.time()
    async with aiohttp.ClientSession() as session:
        if USE_CHAT_API:
            # Используем chat API для более эффективной работы
            async with session.post(f"{OLLAMA}/api/chat", json={
                "model": MODEL,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": text}
                ],
                "stream": False,
                "temperature": 0.7,
            }) as r:
                data = await r.json()
                reply = data.get("message", {}).get("content", "…")
        else:
            # Fallback на generate API с системным промптом
            async with session.post(f"{OLLAMA}/api/generate", json={
                "model": MODEL,
                "prompt": text,  # Только вопрос пользователя
                "system": SYSTEM_PROMPT,  # Системный промпт
                "stream": False,
                "temperature": 0.7,
            }) as r:
                data = await r.json()
                reply = data.get("response", "…")
    
    response_time = time.time() - start_time
    print(f"Время ответа: {response_time:.2f} сек")
    
    await msg.answer(reply)

if __name__ == "__main__":
    asyncio.run(dp.start_polling(bot))
