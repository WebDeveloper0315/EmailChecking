"""Automatic mailbox synchronisation.

Two layers:

* :class:`FolderSynchronizer` - the algorithm, plain Python, no Qt.  It is what
  the unit tests drive against the fake IMAP server.
* :class:`SyncWorker` / :class:`SyncController` - the Qt wrapper.  The worker
  lives in its own ``QThread`` and owns the only :class:`ImapClient`, so no
  network call ever runs on the UI thread and IMAP commands are serialised on
  one connection (imaplib is not thread safe).

Sync algorithm (one folder, one pass)
-------------------------------------
1. read ``UIDVALIDITY``; if it changed, the cached UIDs are meaningless and the
   folder is dropped;
2. one ``UID FETCH 1:* (UID FLAGS RFC822.SIZE)`` - this gives which messages
   exist and their flags in a single round trip, without downloading bodies;
3. UIDs we know but the server no longer lists -> removed;
4. UIDs known on both sides whose flags differ -> flag update (read/unread,
   star ...), no download;
5. UIDs we have never seen -> body downloaded (newest first, capped), or read
   straight from the on-disk cache when a previous run already stored it.

Only steps 3-5 produce events, so a quiet mailbox costs exactly one FETCH per
interval and the UI is never rebuilt from scratch: the selection and the scroll
position survive because existing rows are left alone.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

from logging_setup import get_logger
from mail_parser import parse_email
from mail_receiver import (
    AuthenticationError,
    ConnectionLostError,
    FolderInfo,
    ImapClient,
    ReceiveError,
)
from mail_storage import MailStore
from models import Email

logger = get_logger("sync")

__all__ = [
    "SyncResult",
    "FolderSynchronizer",
    "SyncWorker",
    "SyncController",
    "QT_AVAILABLE",
]


@dataclass
class SyncResult:
    """What one synchronisation pass changed."""

    folder: str
    new_messages: list[Email] = field(default_factory=list)
    flag_updates: list[tuple[int, frozenset[str]]] = field(default_factory=list)
    removed_uids: list[int] = field(default_factory=list)
    total: int = 0
    unread: int = 0
    error: str = ""
    cancelled: bool = False
    from_cache: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.new_messages or self.flag_updates or self.removed_uids)


class FolderSynchronizer:
    """Incremental folder sync.  Pure Python so it can be tested headlessly."""

    def __init__(self, client: ImapClient, store: MailStore, max_messages: int = 200) -> None:
        self.client = client
        self.store = store
        self.max_messages = max(1, max_messages)

    def sync(
        self,
        folder: str,
        criteria: str = "all",
        should_stop: Optional[Callable[[], bool]] = None,
        on_message: Optional[Callable[[Email], None]] = None,
        on_progress: Optional[Callable[[int, int], None]] = None,
    ) -> SyncResult:
        """Bring ``folder`` up to date.  Never raises for expected failures."""
        result = SyncResult(folder=folder)
        stop = should_stop or (lambda: False)

        try:
            self.client.connect()
            validity = self.client.uid_validity(folder)
            self.store.set_validity(folder, validity)

            statuses = self.client.fetch_statuses(folder)
            server_flags = {status.uid: status.flags for status in statuses}
            server_uids = set(server_flags)

            # A quick filter (unread/today/sender) narrows what we download, but
            # must never be read as "everything else was deleted".
            filtered = criteria not in ("", "all", None)
            if filtered:
                wanted = set(self.client.search_uids(folder, criteria)) & server_uids
            else:
                wanted = server_uids

            known = self.store.known_uids(folder)

            if not filtered:
                result.removed_uids = sorted(known - server_uids)
                if result.removed_uids:
                    self.store.remove(folder, result.removed_uids)

            for uid in sorted(known & server_uids):
                if self.store.update_flags(folder, uid, server_flags[uid]):
                    result.flag_updates.append((uid, frozenset(server_flags[uid])))

            new_uids = sorted(wanted - known, reverse=True)[: self.max_messages]
            for index, uid in enumerate(new_uids, start=1):
                if stop():
                    result.cancelled = True
                    break
                mail = self._load(folder, uid, server_flags.get(uid, frozenset()), result)
                if mail is None:
                    continue
                self.store.add(mail)
                result.new_messages.append(mail)
                if on_message is not None:
                    on_message(mail)
                if on_progress is not None:
                    on_progress(index, len(new_uids))

            result.total = len(server_uids)
            result.unread = sum(
                1 for flags in server_flags.values()
                if "\\seen" not in {f.lower() for f in flags}
            )
            self.store.mark_synced(folder, result.total, result.unread)
            if self.store.cache_enabled:
                self.store.prune_cache(folder)

            logger.info(
                "Synced %s: %d new, %d flag change(s), %d removed",
                folder, len(result.new_messages), len(result.flag_updates),
                len(result.removed_uids),
                extra={"event": "sync_done", "folder": folder,
                       "new": len(result.new_messages), "cached": result.from_cache,
                       "flags": len(result.flag_updates), "removed": len(result.removed_uids),
                       "total": result.total, "unread": result.unread},
            )
        except (AuthenticationError, ConnectionLostError, ReceiveError) as exc:
            result.error = str(exc)
            logger.warning("Sync of %s failed: %s", folder, exc,
                           extra={"event": "sync_failed", "folder": folder,
                                  "error_type": type(exc).__name__})
        except Exception as exc:  # a bug here must not kill the worker thread
            result.error = f"Unexpected error: {exc}"
            logger.exception("Unexpected sync error for %s", folder,
                             extra={"event": "sync_crash", "folder": folder})
        return result

    def _load(self, folder: str, uid: int, flags: frozenset[str],
              result: SyncResult) -> Optional[Email]:
        """Cache first, network second - a message is downloaded only once."""
        raw = self.store.cached_raw(folder, uid)
        if raw is not None:
            result.from_cache += 1
        else:
            raw = self.client.fetch_raw(folder, uid)
            if raw is None:
                return None
            self.store.store_raw(folder, uid, raw)
        try:
            return parse_email(raw, uid=str(uid), source=folder, folder=folder, flags=flags)
        except Exception:
            logger.exception("Could not parse UID %s in %s", uid, folder)
            return None

    def load_from_cache(self, folder: str, limit: int = 0,
                        on_message: Optional[Callable[[Email], None]] = None) -> list[Email]:
        """Show what is on disk immediately, before the network is touched."""
        loaded: list[Email] = []
        known = self.store.known_uids(folder)
        for uid in self.store.cached_uids(folder)[: (limit or self.max_messages)]:
            if uid in known:
                continue
            raw = self.store.cached_raw(folder, uid)
            if raw is None:
                continue
            try:
                mail = parse_email(raw, uid=str(uid), source=folder, folder=folder)
            except Exception:
                continue
            self.store.add(mail)
            loaded.append(mail)
            if on_message is not None:
                on_message(mail)
        if loaded:
            logger.info("Loaded %d cached message(s) for %s", len(loaded), folder,
                        extra={"event": "cache_load", "folder": folder, "count": len(loaded)})
        return loaded


# --------------------------------------------------------------------- Qt part
try:  # the engine above must stay importable without a GUI stack
    import qt_bootstrap

    qt_bootstrap.prepare()
    from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal, Slot

    QT_AVAILABLE = True
except Exception:  # pragma: no cover - only on installs without PySide6
    QT_AVAILABLE = False


if QT_AVAILABLE:

    class SyncWorker(QObject):
        """Owns the IMAP connection and executes every network request."""

        folders_listed = Signal(object)          # list[FolderInfo]
        sync_started = Signal(str)               # folder
        sync_progress = Signal(int, int)         # done, total
        message_arrived = Signal(object)         # Email
        flags_changed = Signal(str, int, object)  # folder, uid, frozenset[str]
        messages_removed = Signal(str, object)   # folder, list[int]
        sync_finished = Signal(object)           # SyncResult
        operation_finished = Signal(str, bool, str)   # operation, ok, message
        connection_changed = Signal(bool, str)   # connected, message
        cache_loaded = Signal(str, int)          # folder, count

        def __init__(self, account, store: MailStore, max_messages: int = 200) -> None:
            super().__init__()
            self._account = account
            self._store = store
            self._max_messages = max_messages
            self._client: Optional[ImapClient] = None
            self._stop_event = threading.Event()
            self._busy = False
            self._trash_folder: Optional[str] = None

        # ------------------------------------------------------------ helpers
        @property
        def busy(self) -> bool:
            return self._busy

        def request_stop(self) -> None:
            """Callable from the UI thread: aborts the running download."""
            self._stop_event.set()

        def update_account(self, account) -> None:
            self._account = account
            self._disconnect()

        def _ensure_client(self) -> ImapClient:
            if self._client is None:
                self._client = ImapClient(self._account)
                self._client.connect()
                self.connection_changed.emit(True, f"Connected to {self._account.host}")
            elif not self._client.noop():
                logger.info("Connection lost, reconnecting",
                            extra={"event": "reconnect", "host": self._account.host})
                self._disconnect()
                self._client = ImapClient(self._account)
                self._client.connect()
                self.connection_changed.emit(True, "Reconnected")
            return self._client

        def _disconnect(self) -> None:
            if self._client is not None:
                self._client.logout()
                self._client = None

        def _fail(self, operation: str, exc: Exception) -> str:
            message = str(exc)
            if isinstance(exc, (ConnectionLostError, AuthenticationError)):
                self._disconnect()
                self.connection_changed.emit(False, message)
            logger.warning("%s failed: %s", operation, message,
                           extra={"event": "operation_failed", "operation": operation})
            self.operation_finished.emit(operation, False, message)
            return message

        # -------------------------------------------------------------- slots
        @Slot()
        def shutdown(self) -> None:
            self._stop_event.set()
            self._disconnect()

        @Slot()
        def list_folders(self) -> None:
            self._busy = True
            try:
                client = self._ensure_client()
                folders = client.list_folders(with_counts=True)
                for folder in folders:
                    if folder.kind == "trash":
                        self._trash_folder = folder.name
                self.folders_listed.emit(folders)
                self.operation_finished.emit("folders", True, f"{len(folders)} folders")
            except Exception as exc:
                self._fail("folders", exc)
            finally:
                self._busy = False

        @Slot(str, int)
        def load_cache(self, folder: str, limit: int) -> None:
            """Populate the list from disk before any network access."""
            try:
                synchronizer = FolderSynchronizer(
                    self._client or ImapClient(self._account), self._store, self._max_messages
                )
                loaded = synchronizer.load_from_cache(
                    folder, limit, on_message=self.message_arrived.emit
                )
                self.cache_loaded.emit(folder, len(loaded))
            except Exception:
                logger.debug("Cache load failed for %s", folder, exc_info=True)

        @Slot(str, str)
        def sync_folder(self, folder: str, criteria: str) -> None:
            if self._busy:
                logger.debug("Sync skipped, worker busy",
                             extra={"event": "sync_skipped", "folder": folder})
                return
            self._busy = True
            self._stop_event.clear()
            self.sync_started.emit(folder)
            try:
                client = self._ensure_client()
                synchronizer = FolderSynchronizer(client, self._store, self._max_messages)
                result = synchronizer.sync(
                    folder,
                    criteria,
                    should_stop=self._stop_event.is_set,
                    on_message=self.message_arrived.emit,
                    on_progress=lambda done, total: self.sync_progress.emit(done, total),
                )
                for uid, flags in result.flag_updates:
                    self.flags_changed.emit(folder, uid, flags)
                if result.removed_uids:
                    self.messages_removed.emit(folder, result.removed_uids)
                if result.error:
                    self.connection_changed.emit(False, result.error)
                self.sync_finished.emit(result)
            except Exception as exc:
                message = self._fail("sync", exc)
                self.sync_finished.emit(SyncResult(folder=folder, error=message))
            finally:
                self._busy = False

        @Slot(str, object, object, bool)
        def set_flags(self, folder: str, uids: object, flags: object, add: bool) -> None:
            self._busy = True
            try:
                client = self._ensure_client()
                uid_list = [int(u) for u in uids]  # type: ignore[union-attr]
                client.store_flags(folder, uid_list, list(flags), add)  # type: ignore[arg-type]
                for uid in uid_list:
                    mail = self._store.message(folder, uid)
                    if mail is not None:
                        current = set(mail.flags)
                        if add:
                            current |= set(flags)   # type: ignore[arg-type]
                        else:
                            current -= set(flags)   # type: ignore[arg-type]
                        self._store.update_flags(folder, uid, current)
                        self.flags_changed.emit(folder, uid, frozenset(current))
                self.operation_finished.emit("flags", True, "")
            except Exception as exc:
                self._fail("flags", exc)
            finally:
                self._busy = False

        @Slot(str, object, bool)
        def delete_messages(self, folder: str, uids: object, permanent: bool) -> None:
            self._busy = True
            try:
                client = self._ensure_client()
                uid_list = [int(u) for u in uids]  # type: ignore[union-attr]
                trash = None if permanent else self._trash_folder
                client.delete(folder, uid_list, permanent=permanent, trash_folder=trash)
                self._store.remove(folder, uid_list)
                self.messages_removed.emit(folder, uid_list)
                where = "permanently deleted" if permanent or not trash else f"moved to {trash}"
                self.operation_finished.emit("delete", True,
                                             f"{len(uid_list)} message(s) {where}")
            except Exception as exc:
                self._fail("delete", exc)
            finally:
                self._busy = False

        @Slot(str, object, str)
        def move_messages(self, folder: str, uids: object, destination: str) -> None:
            self._busy = True
            try:
                client = self._ensure_client()
                uid_list = [int(u) for u in uids]  # type: ignore[union-attr]
                client.move(folder, uid_list, destination)
                for uid in uid_list:
                    self._store.move_message(folder, uid, destination)
                self.messages_removed.emit(folder, uid_list)
                self.operation_finished.emit("move", True,
                                             f"{len(uid_list)} message(s) moved to {destination}")
            except Exception as exc:
                self._fail("move", exc)
            finally:
                self._busy = False

        @Slot(str, object, object)
        def append_message(self, folder: str, raw: object, flags: object) -> None:
            self._busy = True
            try:
                client = self._ensure_client()
                client.append(folder, bytes(raw), list(flags))  # type: ignore[arg-type]
                self.operation_finished.emit("append", True, f"Stored in {folder}")
            except Exception as exc:
                self._fail("append", exc)
            finally:
                self._busy = False

        @Slot(str, str)
        def search_server(self, folder: str, query: str) -> None:
            """IMAP SEARCH, so messages that were never downloaded are found."""
            self._busy = True
            try:
                client = self._ensure_client()
                escaped = query.replace('"', "'")
                uids = client.search_raw(folder, f'TEXT "{escaped}"')
                known = self._store.known_uids(folder)
                missing = sorted(set(uids) - known, reverse=True)[: self._max_messages]
                synchronizer = FolderSynchronizer(client, self._store, self._max_messages)
                for uid in missing:
                    mail = synchronizer._load(folder, uid, frozenset(), SyncResult(folder))
                    if mail is not None:
                        self._store.add(mail)
                        self.message_arrived.emit(mail)
                self.operation_finished.emit(
                    "search", True, f"{len(uids)} message(s) matched on the server"
                )
            except Exception as exc:
                self._fail("search", exc)
            finally:
                self._busy = False

    class SyncController(QObject):
        """UI-side facade: owns the thread and the automatic refresh timer.

        Requests are plain signals connected to the worker's slots.  Because the
        worker lives in another thread, Qt makes every one of those connections
        queued automatically: calling any method below returns immediately and
        the work happens on the sync thread.
        """

        # Request signals (UI thread -> worker thread, queued by Qt).
        _sync_requested = Signal(str, str)
        _folders_requested = Signal()
        _cache_requested = Signal(str, int)
        _flags_requested = Signal(str, object, object, bool)
        _delete_requested = Signal(str, object, bool)
        _move_requested = Signal(str, object, str)
        _append_requested = Signal(str, object, object)
        _search_requested = Signal(str, str)
        _account_changed = Signal(object)
        _shutdown_requested = Signal()

        def __init__(self, account, store: MailStore, settings, parent: Optional[QObject] = None):
            super().__init__(parent)
            self._settings = settings
            self._store = store
            self._thread = QThread()
            self._thread.setObjectName("imap-sync")
            self.worker = SyncWorker(account, store, settings.sync.max_messages_per_folder)
            self.worker.moveToThread(self._thread)

            self._sync_requested.connect(self.worker.sync_folder)
            self._folders_requested.connect(self.worker.list_folders)
            self._cache_requested.connect(self.worker.load_cache)
            self._flags_requested.connect(self.worker.set_flags)
            self._delete_requested.connect(self.worker.delete_messages)
            self._move_requested.connect(self.worker.move_messages)
            self._append_requested.connect(self.worker.append_message)
            self._search_requested.connect(self.worker.search_server)
            self._account_changed.connect(self.worker.update_account)
            self._shutdown_requested.connect(self.worker.shutdown)

            self._thread.start()

            self._timer = QTimer(self)
            self._timer.setSingleShot(False)
            self._timer.timeout.connect(self._on_timer)
            self._folder = account.folder or "INBOX"
            self._criteria = "all"
            self.apply_interval(settings.sync.interval_seconds)

        # ------------------------------------------------------------ control
        @property
        def folder(self) -> str:
            return self._folder

        def set_target(self, folder: str, criteria: str) -> None:
            self._folder, self._criteria = folder, criteria

        def apply_interval(self, seconds: int) -> None:
            if seconds and seconds > 0:
                self._timer.setInterval(int(seconds) * 1000)
                self._timer.start()
                logger.info("Automatic sync every %s s", seconds,
                            extra={"event": "interval", "seconds": seconds})
            else:
                self._timer.stop()
                logger.info("Automatic sync disabled (manual only)",
                            extra={"event": "interval", "seconds": 0})

        def update_account(self, account) -> None:
            self._account_changed.emit(account)

        def sync_now(self, folder: Optional[str] = None, criteria: Optional[str] = None) -> None:
            self._sync_requested.emit(folder or self._folder, criteria or self._criteria)

        def list_folders(self) -> None:
            self._folders_requested.emit()

        def load_cache(self, folder: str, limit: int = 0) -> None:
            self._cache_requested.emit(folder, int(limit))

        def set_flags(self, folder: str, uids: Sequence[int], flags: Sequence[str],
                      add: bool = True) -> None:
            self._flags_requested.emit(folder, list(uids), list(flags), bool(add))

        def delete(self, folder: str, uids: Sequence[int], permanent: bool = False) -> None:
            self._delete_requested.emit(folder, list(uids), bool(permanent))

        def move(self, folder: str, uids: Sequence[int], destination: str) -> None:
            self._move_requested.emit(folder, list(uids), destination)

        def append(self, folder: str, raw: bytes, flags: Sequence[str] = ()) -> None:
            self._append_requested.emit(folder, raw, list(flags))

        def search_server(self, folder: str, query: str) -> None:
            self._search_requested.emit(folder, query)

        def stop_current(self) -> None:
            self.worker.request_stop()

        def shutdown(self) -> None:
            self._timer.stop()
            self.worker.request_stop()
            self._shutdown_requested.emit()
            self._thread.quit()
            if not self._thread.wait(5000):
                logger.warning("Sync thread did not stop in time")

        # ----------------------------------------------------------- internal
        def _on_timer(self) -> None:
            if self.worker.busy:
                logger.debug("Timer tick skipped, a sync is still running")
                return
            self.sync_now()

else:  # pragma: no cover - documented fallback when PySide6 is missing
    SyncWorker = None  # type: ignore[assignment]
    SyncController = None  # type: ignore[assignment]
