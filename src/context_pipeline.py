from dataclasses import dataclass
from typing import List, Dict

from .memory_store import MemoryStore


@dataclass
class ContextConfig:
    recent_limit: int = 40
    summary_days: int = 7
    max_summary_chars: int = 2000
    system_prompt: str = (
        "You are a helpful, direct, slightly sarcastic assistant. "
        "Do not spam jokes. Answer concisely when possible."
    )


def _trim_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3] + "..."


def build_context(
    chat_id: str,
    user_text: str,
    store: MemoryStore,
    cfg: ContextConfig,
) -> Dict[str, List[Dict[str, str]]]:
    recent = store.get_recent_messages(chat_id, cfg.recent_limit)
    summaries = store.get_summaries(chat_id, cfg.summary_days)

    summary_text = ""
    if summaries:
        joined = "\n".join([f"{d}: {s}" for d, s in summaries])
        summary_text = _trim_text(joined, cfg.max_summary_chars)

    messages: List[Dict[str, str]] = []
    system_parts = [cfg.system_prompt]
    if summary_text:
        system_parts.append("Memory summary:\n" + summary_text)
    system_msg = "\n\n".join(system_parts)
    messages.append({"role": "system", "content": system_msg})

    for m in recent:
        if not m.text.strip():
            continue
        messages.append({"role": m.role, "content": m.text})

    messages.append({"role": "user", "content": user_text})
    return {"messages": messages}

