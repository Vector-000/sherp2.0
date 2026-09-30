import datetime
import logging
import os
import sqlite3
from dataclasses import dataclass
from typing import List, Optional

from helper import get_config

logger = logging.getLogger(__name__)

__DEFAULT_DB_PATH = "db/polls.db"

__cfg = get_config().get("polls", None)
POLL_DB_PATH = __cfg.get("db_path", __DEFAULT_DB_PATH) if __cfg else __DEFAULT_DB_PATH

_SCHEMA = """
CREATE TABLE IF NOT EXISTS polls (
    message_id INTEGER PRIMARY KEY,
    channel_id INTEGER NOT NULL,
    guild_id INTEGER NOT NULL,
    author_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    question TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    closed INTEGER NOT NULL DEFAULT 0
);
"""

_COLUMNS = (
    "message_id, channel_id, guild_id, author_id, kind, question, "
    "created_at, expires_at, closed"
)


@dataclass(frozen=True)
class PollRecord:
    message_id: int
    channel_id: int
    guild_id: int
    author_id: int
    kind: str  # "poll" or "vote"
    question: str
    created_at: datetime.datetime
    expires_at: datetime.datetime
    closed: bool = False


class PollStore:
    # Persists the polls and votes the bot posted, so their authors can close
    # them after a restart. Every method runs synchronously in a single
    # transaction, so calls made from the event loop never interleave.
    # Timestamps are stored as ISO strings in UTC.

    def __init__(self, path: str):
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._conn = sqlite3.connect(path)
        with self._conn:
            self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def add(self, record: PollRecord) -> None:
        with self._conn:
            self._conn.execute(
                f"INSERT INTO polls ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.message_id,
                    record.channel_id,
                    record.guild_id,
                    record.author_id,
                    record.kind,
                    record.question,
                    _to_text(record.created_at),
                    _to_text(record.expires_at),
                    int(record.closed),
                ),
            )

    def get(self, message_id: int) -> Optional[PollRecord]:
        row = self._conn.execute(
            f"SELECT {_COLUMNS} FROM polls WHERE message_id = ?", (message_id,)
        ).fetchone()
        return None if row is None else _to_record(row)

    def list_open(self, guild_id: int, author_id: int, kind: str) -> List[PollRecord]:
        # Returns the author's open polls or votes, newest first.
        rows = self._conn.execute(
            f"SELECT {_COLUMNS} FROM polls "
            "WHERE guild_id = ? AND author_id = ? AND kind = ? AND closed = 0 "
            "ORDER BY created_at DESC, message_id DESC",
            (guild_id, author_id, kind),
        ).fetchall()
        return [_to_record(row) for row in rows]

    def list_expired(self, now: datetime.datetime) -> List[PollRecord]:
        # Returns open polls and votes whose Discord poll has already ended.
        rows = self._conn.execute(
            f"SELECT {_COLUMNS} FROM polls "
            "WHERE closed = 0 AND expires_at <= ? ORDER BY expires_at",
            (_to_text(now),),
        ).fetchall()
        return [_to_record(row) for row in rows]

    def claim_close(self, message_id: int) -> bool:
        # Marks an open poll as closed. Returns ``False`` if it was already
        # closed (or unknown), so each poll is only closed and reported once.
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE polls SET closed = 1 WHERE message_id = ? AND closed = 0",
                (message_id,),
            )
            return cursor.rowcount == 1

    def reopen(self, message_id: int) -> None:
        # Undoes ``claim_close`` when the poll could not be ended after all.
        with self._conn:
            self._conn.execute(
                "UPDATE polls SET closed = 0 WHERE message_id = ?", (message_id,)
            )


def _to_text(moment: datetime.datetime) -> str:
    # Stored in UTC with a fixed format, so the strings sort chronologically.
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    return moment.astimezone(datetime.timezone.utc).isoformat(timespec="microseconds")


def _to_record(row: tuple) -> PollRecord:
    return PollRecord(
        message_id=row[0],
        channel_id=row[1],
        guild_id=row[2],
        author_id=row[3],
        kind=row[4],
        question=row[5],
        created_at=datetime.datetime.fromisoformat(row[6]),
        expires_at=datetime.datetime.fromisoformat(row[7]),
        closed=bool(row[8]),
    )


def open_poll_store(path: str = POLL_DB_PATH) -> Optional[PollStore]:
    try:
        return PollStore(path)
    except (OSError, sqlite3.Error):
        logger.exception("Failed to open poll database path=%s", path)
        return None
