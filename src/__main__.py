# Точка входа приложения. Запускается командой: python -m src
import os
import asyncio
import aiohttp
import time
from dataclasses import dataclass
from datetime import date, timedelta
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart, Command
from aiogram.types import ChatMemberUpdated, ChatMember

from .context_pipeline import build_context, ContextConfig
from .humor_gate import should_add_humor, HumorConfig
from .memory_store import MemoryStore, naive_summarizer

# Читаем переменные окружения, которые придут из .env (на сервере)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OLLAMA = os.getenv("OLLAMA_ENDPOINT", "http://host.docker.internal:11434")
MODEL = os.getenv("MODEL_NAME", "qwen2.5:7b-instruct-q4_K_M")

# Системный промпт, задается один раз при запуске
SYSTEM_PROMPT = os.getenv(
    "SYSTEM_PROMPT",
    "Ты — полноценный участник чата. Пиши живо, по‑человечески, кратко и по делу. "
    "Стиль: жёстко‑саркастичный, угарный, допускаешь грубость и мат, если это уместно в контексте. "
    "Не спамь шутками: максимум одна короткая шутка в ответе и только если реально к месту. "
    "Держи мысль, не уводи разговор в сторону, не выдумывай факты. "
    "Если вопрос серьёзный — отвечай серьёзно, без глума. "
    "Если ответить нечего — лучше коротко признай это, чем нести чушь."
)

# Проверяем, поддерживает ли модель chat API
USE_CHAT_API = os.getenv("USE_CHAT_API", "true").lower() == "true"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.getenv("DATA_DIR", os.path.join(BASE_DIR, "..", "data"))
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.getenv("MEMORY_DB_PATH", os.path.join(DATA_DIR, "bot_memory.sqlite"))

store = MemoryStore(DB_PATH)


@dataclass
class SessionState:
    last_humor_ts: float | None = None
    last_summary_day: date | None = None


def _get_state(chat_id: str, state_by_chat: dict) -> SessionState:
    state = state_by_chat.get(chat_id)
    if state is None:
        state = SessionState()
        state_by_chat[chat_id] = state
    return state


def _maybe_summarize(chat_id: str, state: SessionState) -> None:
    day = date.today() - timedelta(days=1)
    if state.last_summary_day == day:
        return
    store.summarize_day(chat_id, day, naive_summarizer)
    state.last_summary_day = day


bot = Bot(TELEGRAM_TOKEN)
dp = Dispatcher()
state_by_chat: dict[str, SessionState] = {}

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
    
    text = msg.text or msg.caption or ""
    if not text.strip():
        return
    if text == "пошёл нахуй":
        await msg.answer("Сам пошёл нахуй!")
        return
    if len(text) > 1000:
        await msg.answer("Ты еблан, пиши короче!")
        return
    
    chat_id = str(msg.chat.id)
    msg_id = str(msg.message_id)
    state = _get_state(chat_id, state_by_chat)
    _maybe_summarize(chat_id, state)

    ctx_cfg = ContextConfig(
        recent_limit=int(os.getenv("RECENT_LIMIT", "40")),
        summary_days=int(os.getenv("SUMMARY_DAYS", "7")),
        max_summary_chars=int(os.getenv("SUMMARY_MAX_CHARS", "2000")),
        system_prompt=SYSTEM_PROMPT,
    )
    ctx = build_context(chat_id, text, store, ctx_cfg)

    raw_block = os.getenv("HUMOR_BLOCK_KEYWORDS", "").strip()
    block_keywords = tuple(
        k.strip().lower() for k in raw_block.split(",") if k.strip()
    )
    humor_cfg = HumorConfig(
        humor_rate=float(os.getenv("HUMOR_RATE", "0.2")),
        min_gap_seconds=int(os.getenv("HUMOR_MIN_GAP_SECONDS", "180")),
        min_length=int(os.getenv("HUMOR_MIN_LENGTH", "6")),
        max_length=int(os.getenv("HUMOR_MAX_LENGTH", "600")),
        block_keywords=block_keywords,
    )
    if should_add_humor(text, state.last_humor_ts, humor_cfg):
        ctx["messages"].append(
            {
                "role": "system",
                "content": "Если уместно, добавь короткую шутку или лёгкий подкол в конце ответа.",
            }
        )
        state.last_humor_ts = time.time()

    store.add_message(chat_id, msg_id, "user", text)

    start_time = time.time()
    async with aiohttp.ClientSession() as session:
        if USE_CHAT_API:
            # Используем chat API для более эффективной работы
            async with session.post(f"{OLLAMA}/api/chat", json={
                "model": MODEL,
                "messages": ctx["messages"],
                "stream": False,
                "temperature": 0.7,
            }) as r:
                data = await r.json()
                reply = data.get("message", {}).get("content", "…")
        else:
            # Fallback на generate API с системным промптом
            async with session.post(f"{OLLAMA}/api/generate", json={
                "model": MODEL,
                "prompt": ctx["messages"][-1]["content"],
                "system": ctx["messages"][0]["content"],
                "stream": False,
                "temperature": 0.7,
            }) as r:
                data = await r.json()
                reply = data.get("response", "…")
    
    response_time = time.time() - start_time
    print(f"Время ответа: {response_time:.2f} сек")
    
    store.add_message(chat_id, msg_id + ":assistant", "assistant", reply)
    await msg.answer(reply)

if __name__ == "__main__":
    asyncio.run(dp.start_polling(bot))
