# Точка входа приложения. Запускается командой: python -m src
import os
import asyncio
import aiohttp
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart

# Читаем переменные окружения, которые придут из .env (на сервере)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OLLAMA = os.getenv("OLLAMA_ENDPOINT", "http://host.docker.internal:11434")
MODEL = os.getenv("MODEL_NAME", "qwen2.5:7b-instruct-q4_K_M")

bot = Bot(TELEGRAM_TOKEN)
dp = Dispatcher()

@dp.message(CommandStart())
async def start(msg: types.Message):
    await msg.answer("Привет! Я на месте. Спроси меня что-нибудь.")

@dp.message()
async def handle(msg: types.Message):
    text = msg.text or ""
    if text == "пошёл нахуй":
        await msg.answer("Сам пошёл нахуй!")
        return
    if len(text) > 25:
        await msg.answer("Ты еблан, пиши короче!")
        return
    
    prompt = (
        "Отвечай нейтрально, с подколкой. "
        "Если пользователь грубит — отвечай жёстче." \
        "Можешь использовать нецензурную лексику.\n"
        f"Вопрос: {text}"
    )
    async with aiohttp.ClientSession() as session:
        async with session.post(f"{OLLAMA}/api/generate", json={
            "model": MODEL,
            "prompt": prompt,
            "stream": False,
            "temperature": 0.7,
        }) as r:
            data = await r.json()
            reply = data.get("response", "…")
    await msg.answer(reply)

if __name__ == "__main__":
    asyncio.run(dp.start_polling(bot))
