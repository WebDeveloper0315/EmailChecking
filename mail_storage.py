"""Message store: a SQLite index, a raw-body cache on disk, an in-memory LRU.

Three layers, each doing one job:

* :class:`~mail_database.MailDatabase` - one row per message with what the list
  needs (sender, subject, date, size, flags, preview) plus the folder's sync
  bookmarks.  Restoring 1800 messages is a single query.
* the ``.eml`` cache - the untouched bytes, so a message is downloaded once and
  can be re-parsed, forwarded or saved later.
* an in-memory cache of fully parsed messages, so re-opening one is instant.

The public API is the same one the sync engine and the UI already used
(:meth:`add`, :meth:`messages`, :meth:`known_uids`, :meth:`update_flags`,
:meth:`remove`), with the persistence added underneath.

Thread safety: the database serialises its own access; this class only adds a
lock around the in-memory LRU.
"""

from __future__ import annotations

import re
import threading
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional, Sequence

from logging_setup import get_logger
from mail_database import FolderState, MailDatabase, MessageRecord
from models import Address, Email, format_addresses

logger = get_logger("sync", "mail.storage")

__all__ = ["MailStore", "FolderState"]

_UNSAFE = re.compile(r"[^A-Za-z0-9._@-]+")
#: How many fully parsed messages stay in memory.
_LRU_SIZE = 40


def _safe_name(name: str) -> str:
    cleaned = _UNSAFE.sub("_", name).strip("_")
    return cleaned[:80] or "folder"


class MailStore:
    """Everything known about one account's mail."""

    def __init__(
        self,
        cache_dir: Optional[Path] = None,
        account_key: str = "default",
        cache_enabled: bool = True,
        max_messages_per_folder: int = 200,
        database: Optional[MailDatabase] = None,
        legacy_keys: Sequence[str] = (),
    ) -> None:
        self._lock = threading.RLock()
        self._account = _safe_name(account_key)
        self._cache_enabled = cache_enabled and cache_dir is not None
        self._cache_root = Path(cache_dir) / self._account if cache_dir else None
        self._max_per_folder = max(10, max_messages_per_folder)
        self._loaded: "OrderedDict[tuple[str, int], Email]" = OrderedDict()

        if self._cache_enabled and self._cache_root is not None:
            self._adopt_legacy_cache(Path(cache_dir), legacy_keys)
            try:
                self._cache_root.mkdir(parents=True, exist_ok=True)
            except OSError:
                logger.warning("Cache disabled: cannot create %s", self._cache_root)
                self._cache_enabled = False

        if database is not None:
            self.db = database
        else:
            index_path = (self._cache_root / "index.sqlite3"
                          if self._cache_root is not None and self._cache_enabled else None)
            self.db = MailDatabase(index_path)

    def _adopt_legacy_cache(self, root: Path, legacy_keys: Sequence[str]) -> None:
        """Reuse a cache directory an earlier version created.

        The directory name is derived from the account, and that derivation has
        changed; without this, a rename (or an upgrade) would silently download
        every message again.
        """
        if self._cache_root is None or self._cache_root.exists():
            return
        for key in legacy_keys:
            candidate = root / _safe_name(key)
            if candidate.is_dir() and candidate != self._cache_root:
                try:
                    candidate.rename(self._cache_root)
                    logger.info("Adopted the cache of a previous version",
                                extra={"event": "cache_migrate",
                                       "from": str(candidate), "to": str(self._cache_root)})
                    return
                except OSError:
                    logger.debug("Could not move %s", candidate, exc_info=True)

    # ------------------------------------------------------------- folder state
    def folder(self, name: str) -> FolderState:
        return self.db.folder_state(self._account, name)

    def folder_names(self) -> list[str]:
        return self.db.folders(self._account)

    def known_uids(self, folder: str) -> set[int]:
        """UIDs already indexed - the sync worker skips these."""
        return self.db.uids(self._account, folder)

    def highest_uid(self, folder: str) -> int:
        """Largest UID ever seen, the anchor for "only fetch what is new"."""
        return self.db.folder_state(self._account, folder).highest_uid

    def mod_sequence(self, folder: str) -> int:
        return self.db.folder_state(self._account, folder).mod_sequence

    def set_sync_state(self, folder: str, *, uid_validity: int = 0, highest_uid: int = 0,
                       mod_sequence: int = 0, total: int = 0, unread: int = 0) -> None:
        state = self.db.folder_state(self._account, folder)
        self.db.save_folder_state(FolderState(
            account=self._account, folder=folder,
            uid_validity=uid_validity or state.uid_validity,
            highest_uid=max(highest_uid, state.highest_uid),
            mod_sequence=mod_sequence or state.mod_sequence,
            total=total, unread=unread,
            last_sync=datetime.now(timezone.utc).isoformat(),
        ))

    def set_validity(self, folder: str, uid_validity: int) -> bool:
        """Drop the folder when UIDVALIDITY changed - every UID is stale then."""
        state = self.db.folder_state(self._account, folder)
        if state.uid_validity and uid_validity and state.uid_validity != uid_validity:
            logger.warning("UIDVALIDITY of %s changed %s -> %s, clearing the index",
                           folder, state.uid_validity, uid_validity,
                           extra={"event": "uidvalidity_change", "folder": folder})
            self.db.reset_folder(self._account, folder, uid_validity)
            self._clear_folder_cache(folder)
            with self._lock:
                for key in [k for k in self._loaded if k[0] == folder]:
                    del self._loaded[key]
            return True
        if uid_validity and not state.uid_validity:
            self.db.save_folder_state(FolderState(
                account=self._account, folder=folder, uid_validity=uid_validity,
                highest_uid=state.highest_uid, mod_sequence=state.mod_sequence,
                total=state.total, unread=state.unread))
        return False

    def mark_synced(self, folder: str, total: int, unread: int) -> None:
        self.set_sync_state(folder, total=total, unread=unread)

    # ---------------------------------------------------------------- messages
    def messages(self, folder: str) -> list[Email]:
        """The folder's messages, newest first, as list summaries."""
        records = self.db.records(self._account, folder)
        with self._lock:
            return [self._loaded.get((folder, record.uid)) or _to_email(record)
                    for record in records]

    def message(self, folder: str, uid: int) -> Optional[Email]:
        with self._lock:
            cached = self._loaded.get((folder, int(uid)))
        if cached is not None:
            return cached
        record = self.db.record(self._account, folder, int(uid))
        return _to_email(record) if record is not None else None

    def counts(self, folder: str) -> tuple[int, int]:
        """(total, unread) as currently indexed."""
        return self.db.counts(self._account, folder)

    def add(self, mail: Email) -> bool:
        """Store a parsed message.  Returns True when it was not known yet."""
        folder = mail.folder or mail.source or "INBOX"
        record = _to_record(self._account, folder, mail)
        is_new = self.db.upsert(record)
        self._remember(folder, mail)
        return is_new

    def update_flags(self, folder: str, uid: int, flags: Iterable[str]) -> bool:
        """Apply server flags.  Returns True when something actually changed."""
        new_flags = frozenset(flags)
        changed = self.db.set_flags(self._account, folder, int(uid), new_flags)
        with self._lock:
            cached = self._loaded.get((folder, int(uid)))
            if cached is not None:
                cached.flags = new_flags
        return changed

    def remove(self, folder: str, uids: Iterable[int]) -> list[int]:
        """Forget messages that disappeared from the server."""
        removed = self.db.delete(self._account, folder, [int(uid) for uid in uids])
        with self._lock:
            for uid in removed:
                self._loaded.pop((folder, uid), None)
        for uid in removed:
            self._delete_cached(folder, uid)
        if removed:
            logger.info("Removed %d message(s) from %s", len(removed), folder,
                        extra={"event": "store_remove", "folder": folder,
                               "uids": removed[:20]})
        return removed

    def move_message(self, source_folder: str, uid: int, target_folder: str) -> None:
        """Reflect a server-side move locally so the UI updates immediately."""
        self.db.move(self._account, source_folder, int(uid), target_folder)
        with self._lock:
            mail = self._loaded.pop((source_folder, int(uid)), None)
        if mail is not None:
            mail.folder = target_folder
            self._remember(target_folder, mail)

    def clear(self) -> None:
        self.db.clear_account(self._account)
        with self._lock:
            self._loaded.clear()

    # ------------------------------------------------------------ full message
    def ensure_loaded(self, mail: Email) -> Email:
        """Parse the full MIME body of a summary, on demand.

        The list only needs headers, so messages restored from the index arrive
        without a body; this fills one in when the user opens it.
        """
        if mail.loaded:
            return mail
        folder = mail.folder or "INBOX"
        uid = mail.uid_number
        with self._lock:
            cached = self._loaded.get((folder, uid))
        if cached is not None and cached.loaded:
            return cached

        raw = self.cached_raw(folder, uid)
        if raw is None:
            return mail
        from mail_parser import parse_email

        try:
            full = parse_email(raw, uid=str(uid), source=folder, folder=folder,
                               flags=mail.flags)
        except Exception:
            logger.exception("Could not parse cached message %s/%s", folder, uid)
            return mail
        full.preview_text = mail.preview_text
        full.search_text = mail.search_text
        self._remember(folder, full)
        return full

    def _remember(self, folder: str, mail: Email) -> None:
        if not mail.loaded:
            return
        key = (folder, mail.uid_number)
        with self._lock:
            self._loaded[key] = mail
            self._loaded.move_to_end(key)
            while len(self._loaded) > _LRU_SIZE:
                self._loaded.popitem(last=False)

    # ----------------------------------------------------------- disk cache
    @property
    def cache_enabled(self) -> bool:
        return self._cache_enabled

    def _cache_file(self, folder: str, uid: int) -> Optional[Path]:
        if not self._cache_enabled or self._cache_root is None:
            return None
        return self._cache_root / _safe_name(folder) / f"{int(uid)}.eml"

    def cached_raw(self, folder: str, uid: int) -> Optional[bytes]:
        path = self._cache_file(folder, uid)
        if path is None or not path.exists():
            return None
        try:
            return path.read_bytes()
        except OSError:
            return None

    def store_raw(self, folder: str, uid: int, raw: bytes) -> None:
        path = self._cache_file(folder, uid)
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        except OSError:
            logger.debug("Could not cache %s/%s", folder, uid, exc_info=True)

    def cached_uids(self, folder: str) -> list[int]:
        """UIDs available on disk, newest (highest UID) first."""
        path = self._cache_file(folder, 0)
        if path is None or not path.parent.exists():
            return []
        uids = [int(entry.stem) for entry in path.parent.glob("*.eml")
                if entry.stem.isdigit()]
        return sorted(uids, reverse=True)

    def _delete_cached(self, folder: str, uid: int) -> None:
        path = self._cache_file(folder, uid)
        if path is not None and path.exists():
            try:
                path.unlink()
            except OSError:
                pass

    def _clear_folder_cache(self, folder: str) -> None:
        path = self._cache_file(folder, 0)
        if path is None or not path.parent.exists():
            return
        for entry in path.parent.glob("*.eml"):
            try:
                entry.unlink()
            except OSError:
                pass

    def prune_cache(self, folder: str) -> int:
        """Keep the newest ``max_messages_per_folder`` messages of a folder."""
        stale = self.db.prune(self._account, folder, self._max_per_folder)
        for uid in stale:
            self._delete_cached(folder, uid)
            with self._lock:
                self._loaded.pop((folder, uid), None)
        # Cached bodies with no index row left (older runs) go too.
        indexed = self.known_uids(folder)
        for uid in self.cached_uids(folder):
            if uid not in indexed:
                self._delete_cached(folder, uid)
        if stale:
            logger.info("Pruned %d message(s) from %s", len(stale), folder,
                        extra={"event": "cache_prune", "folder": folder,
                               "removed": len(stale)})
        return len(stale)

    def close(self) -> None:
        self.db.close()


# ------------------------------------------------------------- conversions
def _to_record(account: str, folder: str, mail: Email) -> MessageRecord:
    sender = mail.from_addrs[0] if mail.from_addrs else Address()
    return MessageRecord(
        account=account,
        folder=folder,
        uid=mail.uid_number,
        message_id=mail.message_id,
        subject=mail.subject,
        sender_name=sender.name,
        sender_email=sender.email,
        recipients=format_addresses(mail.to_addrs),
        date_iso=mail.date.isoformat() if mail.date else "",
        date_raw=mail.date_raw,
        size=mail.raw_size,
        flags=frozenset(mail.flags),
        preview=mail.preview(240),
        attachments=len(mail.attachments),
        search_blob=mail.search_blob()[:4000],
    )


def _to_email(record: MessageRecord) -> Email:
    """Rebuild the list entry from the index, without touching any MIME."""
    return Email(
        uid=str(record.uid),
        subject=record.subject,
        from_addrs=[Address(name=record.sender_name, email=record.sender_email)]
        if (record.sender_name or record.sender_email) else [],
        to_addrs=[Address(name="", email=part.strip())
                  for part in record.recipients.split(",") if part.strip()],
        date=record.date,
        date_raw=record.date_raw,
        message_id=record.message_id,
        raw_size=record.size,
        folder=record.folder,
        source=record.folder,
        flags=record.flags,
        loaded=False,
        preview_text=record.preview,
        search_text=record.search_blob,
        attachment_count=record.attachments,
    )
