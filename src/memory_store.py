import sqlite3
from dataclasses import dataclass
from datetime import datetime, date, timedelta
from typing import Iterable, Optional, List, Tuple


@dataclass
class MessageRow:
    chat_id: str
    msg_id: str
    role: str  # "user" or "assistant" or "system"
    text: str
    ts: float


class MemoryStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    chat_id TEXT NOT NULL,
                    msg_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    text TEXT NOT NULL,
                    ts REAL NOT NULL,
                    PRIMARY KEY (chat_id, msg_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS summaries (
                    chat_id TEXT NOT NULL,
                    day TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    ts REAL NOT NULL,
                    PRIMARY KEY (chat_id, day)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_messages_chat_ts
                ON messages(chat_id, ts)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_summaries_chat_day
                ON summaries(chat_id, day)
                """
            )

    def add_message(
        self,
        chat_id: str,
        msg_id: str,
        role: str,
        text: str,
        ts: Optional[float] = None,
    ) -> None:
        if ts is None:
            ts = datetime.utcnow().timestamp()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO messages(chat_id, msg_id, role, text, ts)
                VALUES(?, ?, ?, ?, ?)
                """,
                (chat_id, msg_id, role, text, ts),
            )

    def get_recent_messages(self, chat_id: str, limit: int) -> List[MessageRow]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT chat_id, msg_id, role, text, ts
                FROM messages
                WHERE chat_id = ?
                ORDER BY ts DESC
                LIMIT ?
                """,
                (chat_id, limit),
            ).fetchall()
        rows = list(reversed(rows))
        return [MessageRow(**dict(r)) for r in rows]

    def get_messages_for_day(self, chat_id: str, day: date) -> List[MessageRow]:
        start = datetime.combine(day, datetime.min.time()).timestamp()
        end = datetime.combine(day + timedelta(days=1), datetime.min.time()).timestamp()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT chat_id, msg_id, role, text, ts
                FROM messages
                WHERE chat_id = ? AND ts >= ? AND ts < ?
                ORDER BY ts ASC
                """,
                (chat_id, start, end),
            ).fetchall()
        return [MessageRow(**dict(r)) for r in rows]

    def upsert_summary(self, chat_id: str, day: date, summary: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO summaries(chat_id, day, summary, ts)
                VALUES(?, ?, ?, ?)
                """,
                (chat_id, day.isoformat(), summary, datetime.utcnow().timestamp()),
            )

    def get_summaries(self, chat_id: str, days: int = 7) -> List[Tuple[str, str]]:
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT day, summary
                FROM summaries
                WHERE chat_id = ? AND day >= ?
                ORDER BY day ASC
                """,
                (chat_id, cutoff),
            ).fetchall()
        return [(r["day"], r["summary"]) for r in rows]

    def prune_old_messages(self, days_to_keep: int) -> int:
        if days_to_keep <= 0:
            return 0
        cutoff = (datetime.utcnow() - timedelta(days=days_to_keep)).timestamp()
        with self._connect() as conn:
            cur = conn.execute(
                """
                DELETE FROM messages
                WHERE ts < ?
                """,
                (cutoff,),
            )
            return cur.rowcount

    def prune_old_summaries(self, days_to_keep: int) -> int:
        if days_to_keep <= 0:
            return 0
        cutoff = (date.today() - timedelta(days=days_to_keep)).isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                """
                DELETE FROM summaries
                WHERE day < ?
                """,
                (cutoff,),
            )
            return cur.rowcount

    def vacuum(self) -> None:
        with self._connect() as conn:
            conn.execute("VACUUM")

    def summarize_day(
        self,
        chat_id: str,
        day: date,
        summarizer_fn,
        min_messages: int = 12,
    ) -> Optional[str]:
        """
        summarizer_fn: callable that takes List[MessageRow] and returns a string summary.
        """
        msgs = self.get_messages_for_day(chat_id, day)
        if len(msgs) < min_messages:
            return None
        summary = summarizer_fn(msgs)
        if summary:
            self.upsert_summary(chat_id, day, summary)
        return summary


def naive_summarizer(msgs: Iterable[MessageRow]) -> str:
    """
    Fast fallback summarizer (no LLM).
    Keeps the last few user/assistant messages and a simple topic line.
    """
    recent = list(msgs)[-8:]
    topic_terms = []
    for m in recent:
        for t in m.text.split():
            t = t.strip(".,:;!?()[]{}\"'").lower()
            if 3 <= len(t) <= 16:
                topic_terms.append(t)
    top_terms = sorted(set(topic_terms))[:12]
    lines = []
    if top_terms:
        lines.append("topics: " + ", ".join(top_terms))
    for m in recent:
        prefix = "U" if m.role == "user" else "A"
        lines.append(f"{prefix}: {m.text[:120]}")
    return "\n".join(lines)

