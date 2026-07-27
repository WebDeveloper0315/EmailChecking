"""SQLite index of everything that has been synchronised.

Why a database
--------------
Before this, the message list lived only in memory: every start re-parsed every
cached ``.eml`` (1800 messages is slow), and every sync asked the server for the
flags of *all* messages to work out what changed.

The database keeps, per (account, folder):

* one row per message with what the list needs - sender, subject, date, size,
  flags, a short preview - so the window can be filled without parsing a single
  MIME body;
* the folder's sync state: ``UIDVALIDITY``, the highest UID seen and the
  ``HIGHESTMODSEQ`` at that moment.

That state is what makes an incremental sync possible: new mail is
``UID FETCH <highest+1>:*`` and flag changes are ``CHANGEDSINCE <modseq>`` -
instead of scanning the whole mailbox each time.

The full message (attachments, HTML, inline images) is still parsed on demand
from the cached raw bytes when a message is opened.

Thread safety: one connection with ``check_same_thread=False`` guarded by a
lock, because the sync worker writes while the UI reads.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional, Sequence

from logging_setup import get_logger

logger = get_logger("sync", "mail.database")

__all__ = ["MessageRecord", "FolderState", "MailDatabase"]

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS folders (
    account       TEXT NOT NULL,
    folder        TEXT NOT NULL,
    uid_validity  INTEGER NOT NULL DEFAULT 0,
    highest_uid   INTEGER NOT NULL DEFAULT 0,
    mod_sequence  INTEGER NOT NULL DEFAULT 0,
    total         INTEGER NOT NULL DEFAULT 0,
    unread        INTEGER NOT NULL DEFAULT 0,
    last_sync     TEXT,
    PRIMARY KEY (account, folder)
);

CREATE TABLE IF NOT EXISTS messages (
    account         TEXT NOT NULL,
    folder          TEXT NOT NULL,
    uid             INTEGER NOT NULL,
    message_id      TEXT,
    subject         TEXT,
    sender_name     TEXT,
    sender_email    TEXT,
    recipients      TEXT,
    date_iso        TEXT,
    date_raw        TEXT,
    size            INTEGER NOT NULL DEFAULT 0,
    flags           TEXT NOT NULL DEFAULT '',
    preview         TEXT,
    attachments     INTEGER NOT NULL DEFAULT 0,
    search_blob     TEXT,
    PRIMARY KEY (account, folder, uid)
);

CREATE INDEX IF NOT EXISTS messages_by_date
    ON messages (account, folder, date_iso DESC);
"""


@dataclass
class MessageRecord:
    """The row behind one line of the message list."""

    account: str
    folder: str
    uid: int
    message_id: str = ""
    subject: str = ""
    sender_name: str = ""
    sender_email: str = ""
    recipients: str = ""
    date_iso: str = ""
    date_raw: str = ""
    size: int = 0
    flags: frozenset[str] = frozenset()
    preview: str = ""
    attachments: int = 0
    search_blob: str = ""

    @property
    def date(self) -> Optional[datetime]:
        if not self.date_iso:
            return None
        try:
            parsed = datetime.fromisoformat(self.date_iso)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass
class FolderState:
    """Sync bookmarks for one mailbox."""

    account: str = ""
    folder: str = ""
    uid_validity: int = 0
    highest_uid: int = 0
    mod_sequence: int = 0
    total: int = 0
    unread: int = 0
    last_sync: Optional[str] = None


class MailDatabase:
    """SQLite index; safe to share between the UI and the sync threads."""

    def __init__(self, path: Optional[Path | str] = None) -> None:
        self._lock = threading.RLock()
        self._closed = False
        self.path = Path(path) if path else None
        if self.path is not None:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                logger.warning("Cannot create %s, using an in-memory index",
                               self.path.parent, exc_info=True)
                self.path = None

        target = str(self.path) if self.path is not None else ":memory:"
        self._connection = sqlite3.connect(target, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.executescript(_SCHEMA)
            self._connection.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema', ?)",
                (str(SCHEMA_VERSION),),
            )
            # WAL keeps the UI's reads from blocking the worker's writes.
            if self.path is not None:
                try:
                    self._connection.execute("PRAGMA journal_mode=WAL")
                    self._connection.execute("PRAGMA synchronous=NORMAL")
                except sqlite3.Error:
                    pass
            self._connection.commit()
        logger.info("Message index ready", extra={"event": "db_open", "path": target})

    def close(self) -> None:
        with self._lock:
            self._closed = True
            try:
                self._connection.commit()
                self._connection.close()
            except sqlite3.Error:
                pass

    @property
    def closed(self) -> bool:
        return self._closed

    def _guard(self) -> bool:
        """True when the index is usable.

        Qt can deliver a queued signal after the window has closed its stores;
        answering "nothing" is better than raising ProgrammingError out of a
        slot that only wanted to refresh a label.
        """
        return not self._closed

    # ---------------------------------------------------------------- folders
    def folder_state(self, account: str, folder: str) -> FolderState:
        if not self._guard():
            return FolderState(account=account, folder=folder)
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM folders WHERE account = ? AND folder = ?",
                (account, folder),
            ).fetchone()
        if row is None:
            return FolderState(account=account, folder=folder)
        return FolderState(
            account=account, folder=folder,
            uid_validity=row["uid_validity"], highest_uid=row["highest_uid"],
            mod_sequence=row["mod_sequence"], total=row["total"],
            unread=row["unread"], last_sync=row["last_sync"],
        )

    def save_folder_state(self, state: FolderState) -> None:
        if not self._guard():
            return
        with self._lock:
            self._connection.execute(
                """INSERT INTO folders
                   (account, folder, uid_validity, highest_uid, mod_sequence,
                    total, unread, last_sync)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(account, folder) DO UPDATE SET
                     uid_validity = excluded.uid_validity,
                     highest_uid  = MAX(folders.highest_uid, excluded.highest_uid),
                     mod_sequence = excluded.mod_sequence,
                     total        = excluded.total,
                     unread       = excluded.unread,
                     last_sync    = excluded.last_sync""",
                (state.account, state.folder, state.uid_validity, state.highest_uid,
                 state.mod_sequence, state.total, state.unread,
                 state.last_sync or datetime.now(timezone.utc).isoformat()),
            )
            self._connection.commit()

    def reset_folder(self, account: str, folder: str, uid_validity: int = 0) -> None:
        """UIDVALIDITY changed: every cached UID for the folder is meaningless."""
        if not self._guard():
            return
        with self._lock:
            self._connection.execute(
                "DELETE FROM messages WHERE account = ? AND folder = ?", (account, folder))
            self._connection.execute(
                """INSERT INTO folders (account, folder, uid_validity, highest_uid,
                                        mod_sequence, total, unread, last_sync)
                   VALUES (?, ?, ?, 0, 0, 0, 0, NULL)
                   ON CONFLICT(account, folder) DO UPDATE SET
                     uid_validity = excluded.uid_validity,
                     highest_uid = 0, mod_sequence = 0, total = 0, unread = 0""",
                (account, folder, uid_validity),
            )
            self._connection.commit()
        logger.info("Folder index reset", extra={"event": "db_reset", "account": account,
                                                 "folder": folder})

    def folders(self, account: str) -> list[str]:
        if not self._guard():
            return []
        with self._lock:
            rows = self._connection.execute(
                "SELECT folder FROM folders WHERE account = ?", (account,)).fetchall()
        return [row["folder"] for row in rows]

    # --------------------------------------------------------------- messages
    def upsert(self, record: MessageRecord) -> bool:
        """Insert or update one message.  True when it was not indexed before."""
        if not self._guard():
            return False
        with self._lock:
            existing = self._connection.execute(
                "SELECT 1 FROM messages WHERE account = ? AND folder = ? AND uid = ?",
                (record.account, record.folder, record.uid),
            ).fetchone()
            self._connection.execute(
                """INSERT OR REPLACE INTO messages
                   (account, folder, uid, message_id, subject, sender_name, sender_email,
                    recipients, date_iso, date_raw, size, flags, preview, attachments,
                    search_blob)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (record.account, record.folder, record.uid, record.message_id,
                 record.subject, record.sender_name, record.sender_email,
                 record.recipients, record.date_iso, record.date_raw, record.size,
                 json.dumps(sorted(record.flags)), record.preview, record.attachments,
                 record.search_blob),
            )
            # UPSERT, not UPDATE: on the very first message of a folder there is
            # no row yet, and a plain UPDATE would silently drop the high-water
            # mark - so an interrupted first sync would rescan the mailbox.
            self._connection.execute(
                """INSERT INTO folders (account, folder, highest_uid)
                   VALUES (?, ?, ?)
                   ON CONFLICT(account, folder) DO UPDATE SET
                     highest_uid = MAX(folders.highest_uid, excluded.highest_uid)""",
                (record.account, record.folder, record.uid),
            )
            self._connection.commit()
        return existing is None

    def records(self, account: str, folder: str, limit: int = 0) -> list[MessageRecord]:
        if not self._guard():
            return []
        query = ("SELECT * FROM messages WHERE account = ? AND folder = ? "
                 "ORDER BY date_iso DESC, uid DESC")
        parameters: tuple = (account, folder)
        if limit:
            query += " LIMIT ?"
            parameters = (account, folder, limit)
        with self._lock:
            rows = self._connection.execute(query, parameters).fetchall()
        return [_to_record(row) for row in rows]

    def record(self, account: str, folder: str, uid: int) -> Optional[MessageRecord]:
        if not self._guard():
            return None
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM messages WHERE account = ? AND folder = ? AND uid = ?",
                (account, folder, int(uid)),
            ).fetchone()
        return _to_record(row) if row is not None else None

    def uids(self, account: str, folder: str) -> set[int]:
        if not self._guard():
            return set()
        with self._lock:
            rows = self._connection.execute(
                "SELECT uid FROM messages WHERE account = ? AND folder = ?",
                (account, folder)).fetchall()
        return {row["uid"] for row in rows}

    def flags_by_uid(self, account: str, folder: str) -> dict[int, frozenset[str]]:
        if not self._guard():
            return {}
        with self._lock:
            rows = self._connection.execute(
                "SELECT uid, flags FROM messages WHERE account = ? AND folder = ?",
                (account, folder)).fetchall()
        return {row["uid"]: _load_flags(row["flags"]) for row in rows}

    def set_flags(self, account: str, folder: str, uid: int,
                  flags: Iterable[str]) -> bool:
        """Store new flags.  True when they actually differed."""
        new_flags = frozenset(flags)
        if not self._guard():
            return False
        with self._lock:
            row = self._connection.execute(
                "SELECT flags FROM messages WHERE account = ? AND folder = ? AND uid = ?",
                (account, folder, int(uid))).fetchone()
            if row is None or _load_flags(row["flags"]) == new_flags:
                return False
            self._connection.execute(
                "UPDATE messages SET flags = ? WHERE account = ? AND folder = ? AND uid = ?",
                (json.dumps(sorted(new_flags)), account, folder, int(uid)))
            self._connection.commit()
        return True

    def delete(self, account: str, folder: str, uids: Sequence[int]) -> list[int]:
        if not uids or not self._guard():
            return []
        with self._lock:
            placeholders = ",".join("?" for _ in uids)
            self._connection.execute(
                f"DELETE FROM messages WHERE account = ? AND folder = ? "
                f"AND uid IN ({placeholders})",
                (account, folder, *[int(uid) for uid in uids]),
            )
            self._connection.commit()
        return [int(uid) for uid in uids]

    def move(self, account: str, source: str, uid: int, destination: str) -> None:
        if not self._guard():
            return
        with self._lock:
            self._connection.execute(
                """UPDATE OR REPLACE messages SET folder = ?
                   WHERE account = ? AND folder = ? AND uid = ?""",
                (destination, account, source, int(uid)))
            self._connection.commit()

    def counts(self, account: str, folder: str) -> tuple[int, int]:
        if not self._guard():
            return 0, 0
        with self._lock:
            row = self._connection.execute(
                "SELECT COUNT(*) AS total FROM messages WHERE account = ? AND folder = ?",
                (account, folder)).fetchone()
            total = row["total"] if row else 0
            rows = self._connection.execute(
                "SELECT flags FROM messages WHERE account = ? AND folder = ?",
                (account, folder)).fetchall()
        unread = sum(1 for row in rows if "\\seen" not in
                     {flag.lower() for flag in _load_flags(row["flags"])})
        return total, unread

    def clear_account(self, account: str) -> None:
        if not self._guard():
            return
        with self._lock:
            self._connection.execute("DELETE FROM messages WHERE account = ?", (account,))
            self._connection.execute("DELETE FROM folders WHERE account = ?", (account,))
            self._connection.commit()

    def clear(self) -> None:
        if not self._guard():
            return
        with self._lock:
            self._connection.execute("DELETE FROM messages")
            self._connection.execute("DELETE FROM folders")
            self._connection.commit()

    def prune(self, account: str, folder: str, keep: int) -> list[int]:
        """Keep only the newest ``keep`` messages of a folder; returns the rest."""
        if not self._guard():
            return []
        with self._lock:
            rows = self._connection.execute(
                """SELECT uid FROM messages WHERE account = ? AND folder = ?
                   ORDER BY date_iso DESC, uid DESC LIMIT -1 OFFSET ?""",
                (account, folder, int(keep)),
            ).fetchall()
        stale = [row["uid"] for row in rows]
        if stale:
            self.delete(account, folder, stale)
        return stale


def _load_flags(value: Optional[str]) -> frozenset[str]:
    if not value:
        return frozenset()
    try:
        return frozenset(json.loads(value))
    except (TypeError, ValueError):
        return frozenset()


def _to_record(row: sqlite3.Row) -> MessageRecord:
    return MessageRecord(
        account=row["account"], folder=row["folder"], uid=row["uid"],
        message_id=row["message_id"] or "", subject=row["subject"] or "",
        sender_name=row["sender_name"] or "", sender_email=row["sender_email"] or "",
        recipients=row["recipients"] or "", date_iso=row["date_iso"] or "",
        date_raw=row["date_raw"] or "", size=row["size"] or 0,
        flags=_load_flags(row["flags"]), preview=row["preview"] or "",
        attachments=row["attachments"] or 0, search_blob=row["search_blob"] or "",
    )
