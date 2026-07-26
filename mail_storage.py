"""In-memory message store with an optional on-disk raw cache.

Responsibilities
----------------
* remember which UIDs of which folder are already known, so a sync never
  downloads the same message twice (the "prevent duplicate downloads"
  requirement) - including across restarts, thanks to the ``.eml`` cache;
* keep flags and unread counts per folder;
* hand the UI a stable, sorted list of messages.

Thread safety: the sync worker (background thread) asks for
:meth:`known_uids` and writes raw messages into the cache, while the UI thread
reads :meth:`messages`.  Every mutation is guarded by one re-entrant lock; the
objects handed out are snapshots (new lists), so callers can iterate safely.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from logging_setup import get_logger
from models import Email

logger = get_logger("sync", "mail.storage")

__all__ = ["FolderState", "MailStore"]

_UNSAFE = re.compile(r"[^A-Za-z0-9._@-]+")


def _safe_name(name: str) -> str:
    cleaned = _UNSAFE.sub("_", name).strip("_")
    return cleaned[:80] or "folder"


@dataclass
class FolderState:
    """Everything known about one mailbox."""

    name: str
    uid_validity: int = 0
    messages: dict[int, Email] = field(default_factory=dict)
    total: int = 0
    unread: int = 0
    last_sync: Optional[datetime] = None
    fully_loaded: bool = False

    def sorted_messages(self) -> list[Email]:
        return sorted(self.messages.values(), key=lambda m: (m.sort_key, m.uid_number),
                      reverse=True)


class MailStore:
    """Message cache shared by the UI and the sync engine."""

    def __init__(
        self,
        cache_dir: Optional[Path] = None,
        account_key: str = "default",
        cache_enabled: bool = True,
        max_messages_per_folder: int = 200,
    ) -> None:
        self._lock = threading.RLock()
        self._folders: dict[str, FolderState] = {}
        self._account_key = _safe_name(account_key)
        self._cache_enabled = cache_enabled and cache_dir is not None
        self._cache_root = Path(cache_dir) / self._account_key if cache_dir else None
        self._max_per_folder = max(10, max_messages_per_folder)
        if self._cache_enabled and self._cache_root is not None:
            try:
                self._cache_root.mkdir(parents=True, exist_ok=True)
            except OSError:
                logger.warning("Cache disabled: cannot create %s", self._cache_root)
                self._cache_enabled = False

    # ----------------------------------------------------------------- state
    def folder(self, name: str) -> FolderState:
        with self._lock:
            state = self._folders.get(name)
            if state is None:
                state = FolderState(name=name)
                self._folders[name] = state
            return state

    def folder_names(self) -> list[str]:
        with self._lock:
            return list(self._folders)

    def known_uids(self, folder: str) -> set[int]:
        """UIDs already parsed - the sync worker skips these."""
        with self._lock:
            return set(self._folders.get(folder, FolderState(folder)).messages)

    def messages(self, folder: str) -> list[Email]:
        with self._lock:
            return self.folder(folder).sorted_messages()

    def message(self, folder: str, uid: int) -> Optional[Email]:
        with self._lock:
            return self.folder(folder).messages.get(int(uid))

    def counts(self, folder: str) -> tuple[int, int]:
        """(total, unread) as currently loaded."""
        with self._lock:
            state = self.folder(folder)
            return len(state.messages), sum(1 for m in state.messages.values() if not m.is_read)

    # ------------------------------------------------------------- mutations
    def add(self, mail: Email) -> bool:
        """Store a parsed message.  Returns True when it was not known yet."""
        uid = mail.uid_number
        folder = mail.folder or mail.source or "INBOX"
        with self._lock:
            state = self.folder(folder)
            is_new = uid not in state.messages
            state.messages[uid] = mail
            return is_new

    def update_flags(self, folder: str, uid: int, flags: Iterable[str]) -> bool:
        """Apply server flags.  Returns True when something actually changed."""
        new_flags = frozenset(flags)
        with self._lock:
            mail = self.folder(folder).messages.get(int(uid))
            if mail is None or mail.flags == new_flags:
                return False
            mail.flags = new_flags
            return True

    def remove(self, folder: str, uids: Iterable[int]) -> list[int]:
        """Forget messages that disappeared from the server."""
        removed: list[int] = []
        with self._lock:
            state = self.folder(folder)
            for uid in list(uids):
                if int(uid) in state.messages:
                    del state.messages[int(uid)]
                    removed.append(int(uid))
        for uid in removed:
            self._delete_cached(folder, uid)
        if removed:
            logger.info("Removed %d message(s) from %s", len(removed), folder,
                        extra={"event": "store_remove", "folder": folder, "uids": removed})
        return removed

    def move_message(self, source_folder: str, uid: int, target_folder: str) -> None:
        """Reflect a server-side move locally so the UI updates immediately."""
        with self._lock:
            mail = self.folder(source_folder).messages.pop(int(uid), None)
            if mail is None:
                return
            mail.folder = target_folder
            self.folder(target_folder).messages[int(uid)] = mail

    def set_validity(self, folder: str, uid_validity: int) -> bool:
        """Drop the folder when UIDVALIDITY changed - every UID is stale then."""
        with self._lock:
            state = self.folder(folder)
            if state.uid_validity and uid_validity and state.uid_validity != uid_validity:
                logger.warning("UIDVALIDITY of %s changed %s -> %s, clearing cache",
                               folder, state.uid_validity, uid_validity,
                               extra={"event": "uidvalidity_change", "folder": folder})
                state.messages.clear()
                self._clear_folder_cache(folder)
                state.uid_validity = uid_validity
                return True
            state.uid_validity = uid_validity or state.uid_validity
            return False

    def mark_synced(self, folder: str, total: int, unread: int) -> None:
        with self._lock:
            state = self.folder(folder)
            state.total = total
            state.unread = unread
            state.last_sync = datetime.now(timezone.utc)

    def clear(self) -> None:
        with self._lock:
            self._folders.clear()

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
        if path is None:
            return []
        directory = path.parent
        if not directory.exists():
            return []
        uids: list[int] = []
        for entry in directory.glob("*.eml"):
            if entry.stem.isdigit():
                uids.append(int(entry.stem))
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
        """Keep only the newest ``max_messages_per_folder`` cached messages."""
        uids = self.cached_uids(folder)
        stale = uids[self._max_per_folder:]
        for uid in stale:
            self._delete_cached(folder, uid)
        if stale:
            logger.info("Pruned %d cached message(s) from %s", len(stale), folder,
                        extra={"event": "cache_prune", "folder": folder, "removed": len(stale)})
        return len(stale)
