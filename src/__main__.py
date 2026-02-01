# Точка входа приложения. Запускается командой: python -m src
import os
import asyncio
import aiohttp
import time
from dataclasses import dataclass
from datetime import date, timedelta
from aiogram import Bot, Dispatcher, types
from aiogram.dispatcher.event.bases import SkipHandler
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
    last_joke_day: date | None = None
    jokes_today: int = 0
    last_maintenance_day: date | None = None
    last_vacuum_day: date | None = None


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


def _maybe_maintenance(chat_id: str, state: SessionState) -> None:
    today = date.today()
    if state.last_maintenance_day != today:
        state.last_maintenance_day = today
        keep_msgs_days = int(os.getenv("MEMORY_KEEP_DAYS", "14"))
        keep_sum_days = int(os.getenv("MEMORY_SUMMARY_KEEP_DAYS", "60"))
        store.prune_old_messages(keep_msgs_days)
        store.prune_old_summaries(keep_sum_days)

    vacuum_weekday = int(os.getenv("MEMORY_VACUUM_WEEKDAY", "6"))
    if today.weekday() != vacuum_weekday:
        return
    if state.last_vacuum_day == today:
        return
    state.last_vacuum_day = today
    store.vacuum()


def _is_question(text: str) -> bool:
    return "?" in text


bot = Bot(TELEGRAM_TOKEN)
dp = Dispatcher()
state_by_chat: dict[str, SessionState] = {}
BOT_ID: int | None = None

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

@dp.message()
async def store_any_message(msg: types.Message):
    if msg.from_user and BOT_ID and msg.from_user.id == BOT_ID:
        return
    if (
        BOT_ID
        and msg.reply_to_message
        and msg.reply_to_message.from_user
        and msg.reply_to_message.from_user.id == BOT_ID
    ):
        raise SkipHandler
    text = msg.text or msg.caption or ""
    if not text.strip():
        return
    if text.strip().startswith("/"):
        return
    chat_id = str(msg.chat.id)
    msg_id = str(msg.message_id)
    state = _get_state(chat_id, state_by_chat)
    _maybe_summarize(chat_id, state)
    _maybe_maintenance(chat_id, state)
    store.add_message(chat_id, msg_id, "user", text)

    ambient_enabled = os.getenv("AMBIENT_JOKE_ENABLED", "true").lower() == "true"
    if not ambient_enabled:
        return
    if not _is_question(text):
        return

    today = date.today()
    if state.last_joke_day != today:
        state.last_joke_day = today
        state.jokes_today = 0

    max_per_day = int(os.getenv("AMBIENT_JOKE_MAX_PER_DAY", "4"))
    if state.jokes_today >= max_per_day:
        return

    humor_cfg = HumorConfig(
        humor_rate=float(os.getenv("AMBIENT_JOKE_RATE", "0.04")),
        min_gap_seconds=int(os.getenv("AMBIENT_JOKE_MIN_GAP_SECONDS", "1800")),
        min_length=int(os.getenv("HUMOR_MIN_LENGTH", "6")),
        max_length=int(os.getenv("HUMOR_MAX_LENGTH", "600")),
        block_keywords=tuple(
            k.strip().lower()
            for k in os.getenv("HUMOR_BLOCK_KEYWORDS", "").split(",")
            if k.strip()
        ),
    )
    if not should_add_humor(text, state.last_humor_ts, humor_cfg):
        return

    ctx_cfg = ContextConfig(
        recent_limit=int(os.getenv("RECENT_LIMIT", "40")),
        summary_days=int(os.getenv("SUMMARY_DAYS", "7")),
        max_summary_chars=int(os.getenv("SUMMARY_MAX_CHARS", "2000")),
        system_prompt=SYSTEM_PROMPT,
    )
    ctx = build_context(chat_id, text, store, ctx_cfg)
    ctx["messages"].append(
        {
            "role": "system",
            "content": (
                "Если уместно, вкинь одну короткую шутку/подкол в чат. "
                "Если не к месту — ответь максимально кратко или промолчи."
            ),
        }
    )

    async with aiohttp.ClientSession() as session:
        if USE_CHAT_API:
            async with session.post(f"{OLLAMA}/api/chat", json={
                "model": MODEL,
                "messages": ctx["messages"],
                "stream": False,
                "temperature": 0.7,
            }) as r:
                data = await r.json()
                reply = data.get("message", {}).get("content", "").strip()
        else:
            async with session.post(f"{OLLAMA}/api/generate", json={
                "model": MODEL,
                "prompt": ctx["messages"][-1]["content"],
                "system": ctx["messages"][0]["content"],
                "stream": False,
                "temperature": 0.7,
            }) as r:
                data = await r.json()
                reply = data.get("response", "").strip()

    if not reply:
        return

    state.last_humor_ts = time.time()
    state.jokes_today += 1
    store.add_message(chat_id, msg_id + ":assistant", "assistant", reply)
    await msg.reply(reply)

@dp.chat_member()
async def on_chat_member_update(event: ChatMemberUpdated):
    """Обрабатываем добавление/удаление бота из группы"""
    if BOT_ID and event.new_chat_member.user.id == BOT_ID:
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
    if not BOT_ID or not msg.reply_to_message.from_user or msg.reply_to_message.from_user.id != BOT_ID:
        return  # Отвечаем только на реплаи боту
    
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
    _maybe_maintenance(chat_id, state)

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
    _maybe_maintenance(chat_id, state)
    await msg.answer(reply)

async def main() -> None:
    global BOT_ID
    me = await bot.get_me()
    BOT_ID = me.id
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
