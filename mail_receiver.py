"""IMAP access, built around the existing imbox based retrieval logic.

Why this module looks the way it does
-------------------------------------
The original notebook connects with ``Imbox(...)`` and selects messages with
four queries (``today`` / ``unread`` / ``all`` / sender).  That logic is kept:
:meth:`ImapClient.search_uids` still goes through ``imbox.messages(**query)``,
so the search semantics are imbox's, not a re-implementation.

Three things had to be handled around it:

1. **imbox changed its API in 0.10.**  ``Imbox.__init__`` used to take
   ``(hostname, username=..., password=..., ssl=..., ssl_context=...,
   starttls=...)``; since 0.10 it takes a single ``Config`` dataclass.  Calling
   the old signature against 0.10 raises ``TypeError: Imbox.__init__() got an
   unexpected keyword argument 'username'`` *before any socket is opened* -
   which is exactly why fetching failed.  :func:`_open_imbox` inspects the
   installed signature and calls whichever one is present, so the same code
   works with 0.9.x and 0.10.x.

2. **imbox 0.10 hands out a lossy body.**  ``parse_email`` stores
   ``raw_email`` as ``str_encode(raw, charset, errors="ignore")`` - bytes that
   do not fit the *top level* charset are dropped, which corrupts any part in a
   different charset.  We therefore never read imbox's parsed payload; the raw
   bytes are fetched once with ``BODY.PEEK[]`` (which also does not set
   ``\\Seen``) and handed to our own parser.

3. **A mail client needs more than a search.**  Folder discovery, flags,
   move/delete and APPEND are done directly on imbox's own ``imaplib``
   connection (``imbox.connection``), so there is still a single connection and
   a single code path for authentication.
"""

from __future__ import annotations

import datetime
import imaplib
import inspect
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional, Sequence

from config import AccountSettings
from logging_setup import get_logger, register_secret

logger = get_logger("imap")

__all__ = [
    "RawMessage",
    "FolderInfo",
    "MessageStatus",
    "ReceiveError",
    "AuthenticationError",
    "ConnectionLostError",
    "ImapClient",
    "ImboxReceiver",
    "EmlFileSource",
    "FILTERS",
    "SPECIAL_FOLDER_ORDER",
]

#: Selectable quick filters, mirroring ``read_email_with_imbox(flag)``.
FILTERS: tuple[tuple[str, str], ...] = (
    ("all", "All messages"),
    ("unread", "Unread"),
    ("today", "Today"),
    ("flagged", "Starred"),
)

#: Folder kinds, in the order a mail client lists them.
SPECIAL_FOLDER_ORDER = ("inbox", "drafts", "sent", "archive", "spam", "trash", "all", "other")


class ReceiveError(RuntimeError):
    """Any IMAP problem, already translated into something a user can read."""


class AuthenticationError(ReceiveError):
    """Wrong user name / password / app password."""


class ConnectionLostError(ReceiveError):
    """The connection dropped; the caller may reconnect and retry."""


@dataclass
class RawMessage:
    """Untouched message bytes plus the IMAP metadata we need."""

    uid: str
    raw: bytes
    folder: str = "INBOX"
    flags: frozenset[str] = frozenset()

    @property
    def size(self) -> int:
        return len(self.raw)


@dataclass
class FolderInfo:
    """One mailbox as reported by ``LIST``."""

    name: str                      # raw IMAP name, e.g. "[Gmail]/Sent Mail"
    display: str = ""              # decoded, last path segment
    kind: str = "other"            # inbox/sent/drafts/trash/spam/archive/all/other
    flags: tuple[str, ...] = ()
    delimiter: str = "/"
    selectable: bool = True
    total: int = 0
    unread: int = 0

    @property
    def path_parts(self) -> list[str]:
        return [p for p in self.name.split(self.delimiter or "/") if p]

    @property
    def sort_index(self) -> int:
        try:
            return SPECIAL_FOLDER_ORDER.index(self.kind)
        except ValueError:
            return len(SPECIAL_FOLDER_ORDER)


@dataclass
class MessageStatus:
    """Cheap per-message state: what a sync needs before downloading bodies."""

    uid: int
    flags: frozenset[str] = frozenset()
    size: int = 0

    @property
    def seen(self) -> bool:
        return "\\seen" in {f.lower() for f in self.flags}

    @property
    def flagged(self) -> bool:
        return "\\flagged" in {f.lower() for f in self.flags}


# --------------------------------------------------------------- IMAP UTF-7
def imap_utf7_decode(value: str | bytes) -> str:
    """Decode RFC 3501 modified UTF-7 mailbox names ("&APw-" -> "ü")."""
    if isinstance(value, bytes):
        value = value.decode("ascii", "replace")
    if "&" not in value:
        return value
    out: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char != "&":
            out.append(char)
            index += 1
            continue
        end = value.find("-", index)
        if end == -1:
            out.append(value[index:])
            break
        chunk = value[index + 1:end]
        if not chunk:
            out.append("&")
        else:
            try:
                padded = chunk.replace(",", "/")
                padded += "=" * (-len(padded) % 4)
                import base64

                out.append(base64.b64decode(padded).decode("utf-16-be"))
            except Exception:
                out.append(value[index:end + 1])
        index = end + 1
    return "".join(out)


def imap_utf7_encode(value: str) -> str:
    """Encode a mailbox name to modified UTF-7 (only when it needs it)."""
    if all(0x20 <= ord(c) <= 0x7E and c != "&" for c in value):
        return value
    import base64

    out: list[str] = []
    buffer: list[str] = []

    def flush() -> None:
        if buffer:
            encoded = base64.b64encode("".join(buffer).encode("utf-16-be")).decode("ascii")
            out.append("&" + encoded.rstrip("=").replace("/", ",") + "-")
            buffer.clear()

    for char in value:
        if char == "&":
            flush()
            out.append("&-")
        elif 0x20 <= ord(char) <= 0x7E:
            flush()
            out.append(char)
        else:
            buffer.append(char)
    flush()
    return "".join(out)


def quote_mailbox(name: str) -> str:
    """Quote a mailbox name for imaplib (spaces and brackets are common)."""
    encoded = imap_utf7_encode(name)
    if encoded.startswith('"') and encoded.endswith('"'):
        return encoded
    if re.search(r'[\s"\\(){}\[\]%*]', encoded) or not encoded:
        return '"' + encoded.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return encoded


# ------------------------------------------------------------ imbox adapter
def _open_imbox(account: AccountSettings):
    """Construct an ``Imbox`` regardless of the installed imbox version.

    imbox <= 0.9.x:  ``Imbox(hostname, username=, password=, ssl=, ssl_context=, starttls=)``
    imbox >= 0.10:   ``Imbox(Config(username=, password=, imap_url=, ssl=, ssl_context=, starttls=, port=))``
    """
    try:
        from imbox import Imbox
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise ReceiveError(
            "The 'imbox' package is required to download mail.\n"
            "Install it with:  pip install imbox"
        ) from exc

    try:
        parameters = inspect.signature(Imbox.__init__).parameters
    except (TypeError, ValueError):  # pragma: no cover - exotic builds
        parameters = {}

    if "config" in parameters:
        from imbox.settings import Config

        config = Config(
            username=account.username,
            password=account.password,
            imap_url=account.host,
            ssl=account.use_ssl,
            ssl_context=None,
            starttls=account.starttls,
            port=account.port or (993 if account.use_ssl else 143),
        )
        logger.debug("Connecting with the imbox >= 0.10 Config API",
                     extra={"event": "imbox_api", "api": "config"})
        return Imbox(config)

    logger.debug("Connecting with the imbox 0.9 keyword API",
                 extra={"event": "imbox_api", "api": "legacy"})
    return Imbox(
        account.host,
        username=account.username,
        password=account.password,
        ssl=account.use_ssl,
        ssl_context=None,
        starttls=account.starttls,
    )


# ------------------------------------------------------------ response parsing
_UID_RE = re.compile(rb"\bUID\s+(\d+)")
_FLAGS_RE = re.compile(rb"\bFLAGS\s+\(([^)]*)\)")
_SIZE_RE = re.compile(rb"\bRFC822\.SIZE\s+(\d+)")
_LIST_RE = re.compile(
    r'^\((?P<flags>[^)]*)\)\s+(?P<delim>"[^"]*"|NIL)\s+(?P<name>.+)$', re.IGNORECASE
)


def _as_bytes(item: object) -> bytes:
    if isinstance(item, bytes):
        return item
    if isinstance(item, tuple):
        return b" ".join(part for part in item if isinstance(part, bytes))
    if isinstance(item, str):
        return item.encode("utf-8", "replace")
    return b""


def _parse_status_line(line: bytes) -> Optional[MessageStatus]:
    uid_match = _UID_RE.search(line)
    if not uid_match:
        return None
    flags_match = _FLAGS_RE.search(line)
    size_match = _SIZE_RE.search(line)
    flags = frozenset(
        f.decode("ascii", "replace") for f in (flags_match.group(1).split() if flags_match else [])
    )
    return MessageStatus(
        uid=int(uid_match.group(1)),
        flags=flags,
        size=int(size_match.group(1)) if size_match else 0,
    )


def _classify_folder(name: str, flags: Sequence[str]) -> str:
    """Special-use flags first (RFC 6154), then the usual naming conventions."""
    lowered_flags = {f.lower().lstrip("\\") for f in flags}
    for flag, kind in (("sent", "sent"), ("drafts", "drafts"), ("trash", "trash"),
                       ("junk", "spam"), ("archive", "archive"), ("all", "all")):
        if flag in lowered_flags:
            return kind
    leaf = name.split("/")[-1].split(".")[-1].strip().lower()
    full = name.strip().lower()
    if full == "inbox":
        return "inbox"
    aliases = {
        "sent": ("sent", "sent mail", "sent items", "sent messages", "gesendet", "已发送"),
        "drafts": ("draft", "drafts", "entwürfe", "草稿"),
        "trash": ("trash", "deleted", "deleted items", "bin", "papierkorb", "已删除"),
        "spam": ("spam", "junk", "junk e-mail", "bulk mail"),
        "archive": ("archive", "archives", "all mail", "archiv"),
    }
    for kind, names in aliases.items():
        if leaf in names:
            return kind
    return "other"


# ------------------------------------------------------------------- client
class ImapClient:
    """A single IMAP session.  Not thread safe - use one per worker thread."""

    def __init__(self, account: AccountSettings) -> None:
        self.account = account
        self._imbox = None
        self._selected: Optional[str] = None
        self._selected_readonly = False
        self._capabilities: frozenset[str] = frozenset()
        register_secret(account.password)

    # ------------------------------------------------------------ connection
    @property
    def is_connected(self) -> bool:
        return self._imbox is not None

    @property
    def connection(self):
        if self._imbox is None:
            raise ConnectionLostError("Not connected.")
        return self._imbox.connection

    def connect(self) -> None:
        if self._imbox is not None:
            return
        if not self.account.username or not self.account.password:
            raise AuthenticationError("No user name or password configured.")

        logger.info("Connecting to IMAP server",
                    extra={"event": "connect", **self.account.redacted()})
        try:
            self._imbox = _open_imbox(self.account)
        except Exception as exc:
            raise _translate(exc, self.account) from exc

        # imbox does not expose a timeout, so apply one to the live socket:
        # without it a dead connection blocks the worker thread forever.
        try:
            self._imbox.connection.socket().settimeout(self.account.timeout)
        except Exception:
            logger.debug("Could not set a socket timeout", exc_info=True)

        try:
            raw = self._imbox.connection.capabilities
            self._capabilities = frozenset(c.upper() for c in raw)
        except Exception:
            self._capabilities = frozenset()
        self._selected = "INBOX"      # imbox selects INBOX while logging in
        self._selected_readonly = False
        logger.info("Connected", extra={"event": "connected",
                                        "capabilities": sorted(self._capabilities)[:12]})

    def logout(self) -> None:
        if self._imbox is None:
            return
        try:
            self._imbox.logout()
            logger.info("Disconnected", extra={"event": "logout"})
        except Exception:
            logger.debug("Ignoring error during logout", exc_info=True)
        finally:
            self._imbox = None
            self._selected = None

    def __enter__(self) -> "ImapClient":
        self.connect()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.logout()

    def has_capability(self, name: str) -> bool:
        return name.upper() in self._capabilities

    def noop(self) -> bool:
        """Cheap liveness check; False means the connection is gone."""
        try:
            status, _ = self.connection.noop()
            return status == "OK"
        except Exception:
            return False

    # --------------------------------------------------------------- folders
    def list_folders(self, with_counts: bool = False) -> list[FolderInfo]:
        """Discover mailboxes, including their special-use kind."""
        self.connect()
        try:
            status, data = self.connection.list()
        except Exception as exc:
            raise _translate(exc, self.account) from exc
        if status != "OK" or not data:
            return [FolderInfo(name="INBOX", display="Inbox", kind="inbox")]

        folders: list[FolderInfo] = []
        seen: set[str] = set()
        for row in data:
            info = self._parse_list_row(row)
            if info is None or info.name in seen:
                continue
            seen.add(info.name)
            folders.append(info)

        if not any(f.kind == "inbox" for f in folders):
            folders.insert(0, FolderInfo(name="INBOX", display="Inbox", kind="inbox"))

        if with_counts:
            for folder in folders:
                if folder.selectable:
                    self.refresh_counts(folder)

        folders.sort(key=lambda f: (f.sort_index, f.display.lower()))
        logger.info("Listed %d folders", len(folders),
                    extra={"event": "list_folders", "count": len(folders)})
        return folders

    def _parse_list_row(self, row: object) -> Optional[FolderInfo]:
        if isinstance(row, tuple):           # literal form: (b'(...) "/" {12}', b'name')
            prefix = _as_bytes(row[0]).decode("utf-8", "replace")
            name = _as_bytes(row[1]).decode("utf-8", "replace")
            text = f'{prefix.rsplit("{", 1)[0]}"{name}"'
        else:
            text = _as_bytes(row).decode("utf-8", "replace")
        match = _LIST_RE.match(text.strip())
        if not match:
            return None
        flags = tuple(match.group("flags").split())
        if any(f.lower() == "\\noselect" for f in flags):
            selectable = False
        else:
            selectable = True
        delimiter = match.group("delim").strip('"') or "/"
        name = match.group("name").strip()
        if name.startswith('"') and name.endswith('"') and len(name) > 1:
            name = name[1:-1]
        name = name.replace('\\"', '"')
        decoded = imap_utf7_decode(name)
        display = decoded.split(delimiter)[-1] if delimiter else decoded
        if decoded.upper() == "INBOX":
            display = "Inbox"
        return FolderInfo(
            name=name,
            display=display or decoded,
            kind=_classify_folder(decoded, flags),
            flags=flags,
            delimiter=delimiter,
            selectable=selectable,
        )

    def refresh_counts(self, folder: FolderInfo) -> FolderInfo:
        """Fill ``total`` / ``unread`` with one STATUS command."""
        try:
            status, data = self.connection.status(
                quote_mailbox(folder.name), "(MESSAGES UNSEEN)"
            )
            if status == "OK" and data:
                text = _as_bytes(data[0]).decode("utf-8", "replace")
                messages = re.search(r"MESSAGES\s+(\d+)", text, re.IGNORECASE)
                unseen = re.search(r"UNSEEN\s+(\d+)", text, re.IGNORECASE)
                folder.total = int(messages.group(1)) if messages else 0
                folder.unread = int(unseen.group(1)) if unseen else 0
        except Exception:
            logger.debug("STATUS failed for %s", folder.name, exc_info=True)
        return folder

    def select(self, folder: str, readonly: bool = False) -> int:
        """Select a mailbox; returns the message count.  Cached per folder."""
        self.connect()
        if self._selected == folder and self._selected_readonly == readonly:
            return -1
        try:
            status, data = self.connection.select(quote_mailbox(folder), readonly)
        except Exception as exc:
            raise _translate(exc, self.account) from exc
        if status != "OK":
            detail = _as_bytes(data[0] if data else b"").decode("utf-8", "replace")
            raise ReceiveError(f"Cannot open folder {folder!r}: {detail}")
        self._selected = folder
        self._selected_readonly = readonly
        try:
            return int(_as_bytes(data[0]).decode() or 0)
        except (ValueError, IndexError):
            return 0

    def uid_validity(self, folder: str) -> int:
        """UIDVALIDITY changes mean every cached UID for the folder is stale."""
        self.select(folder)
        try:
            status, data = self.connection.status(quote_mailbox(folder), "(UIDVALIDITY)")
            if status == "OK" and data:
                match = re.search(r"UIDVALIDITY\s+(\d+)",
                                  _as_bytes(data[0]).decode("utf-8", "replace"), re.IGNORECASE)
                if match:
                    return int(match.group(1))
        except Exception:
            logger.debug("UIDVALIDITY unavailable for %s", folder, exc_info=True)
        return 0

    # -------------------------------------------------------------- searching
    def search_uids(self, folder: str, criteria: str = "all") -> list[int]:
        """UIDs matching a quick filter - through imbox, as the notebook did.

        ``criteria`` is ``all`` / ``unread`` / ``today`` / ``flagged`` or a
        sender address, exactly like ``read_email_with_imbox(flag)``.
        """
        self.connect()
        assert self._imbox is not None
        try:
            messages = self._imbox.messages(folder=folder, **_imbox_query(criteria))
            self._selected = folder      # imbox.messages() selects the folder
            self._selected_readonly = False
            uids = getattr(messages, "_uid_list", None)
            if uids is None:             # very old imbox: fall back to iteration
                uids = [uid for uid, _ in messages]
            result = sorted(int(_as_bytes(uid) or 0) for uid in uids)
        except Exception as exc:
            raise _translate(exc, self.account) from exc
        logger.info("Search %s in %s matched %d message(s)", criteria, folder, len(result),
                    extra={"event": "search", "folder": folder,
                           "criteria": criteria, "matched": len(result)})
        return result

    def search_raw(self, folder: str, imap_query: str) -> list[int]:
        """Server side search with a raw IMAP query, e.g. ``TEXT "invoice"``."""
        self.select(folder)
        try:
            status, data = self.connection.uid("SEARCH", None, imap_query)
        except Exception as exc:
            raise _translate(exc, self.account) from exc
        if status != "OK" or not data or data[0] is None:
            return []
        return sorted(int(part) for part in _as_bytes(data[0]).split() if part.isdigit())

    def message_count(self, folder: str) -> int:
        """How many messages the server holds - one cheap STATUS command."""
        try:
            status, data = self.connection.status(quote_mailbox(folder), "(MESSAGES)")
            if status == "OK" and data:
                match = re.search(r"MESSAGES\s+(\d+)",
                                  _as_bytes(data[0]).decode("utf-8", "replace"),
                                  re.IGNORECASE)
                if match:
                    return int(match.group(1))
        except Exception:
            logger.debug("STATUS (MESSAGES) failed for %s", folder, exc_info=True)
        return -1

    def mod_sequence(self, folder: str) -> int:
        """``HIGHESTMODSEQ`` (RFC 7162), or 0 when the server has no CONDSTORE.

        With it, "what changed since last time" is one command instead of a
        scan of the whole mailbox.
        """
        if not self.has_capability("CONDSTORE"):
            return 0
        try:
            status, data = self.connection.status(quote_mailbox(folder), "(HIGHESTMODSEQ)")
            if status == "OK" and data:
                match = re.search(r"HIGHESTMODSEQ\s+(\d+)",
                                  _as_bytes(data[0]).decode("utf-8", "replace"),
                                  re.IGNORECASE)
                if match:
                    return int(match.group(1))
        except Exception:
            logger.debug("HIGHESTMODSEQ unavailable for %s", folder, exc_info=True)
        return 0

    def fetch_changed_flags(self, folder: str, since: int) -> list[MessageStatus]:
        """Only the messages whose flags changed since ``since`` (CONDSTORE)."""
        if since <= 0 or not self.has_capability("CONDSTORE"):
            return []
        self.select(folder)
        try:
            status, data = self.connection.uid(
                "FETCH", "1:*", f"(UID FLAGS) (CHANGEDSINCE {int(since)})"
            )
        except Exception as exc:
            raise _translate(exc, self.account) from exc
        if status != "OK" or not data:
            return []
        changed: list[MessageStatus] = []
        for item in data:
            parsed = _parse_status_line(_as_bytes(item))
            if parsed is not None:
                changed.append(parsed)
        logger.info("CONDSTORE reported %d changed message(s) in %s", len(changed), folder,
                    extra={"event": "condstore", "folder": folder,
                           "since": since, "changed": len(changed)})
        return changed

    def all_uids(self, folder: str) -> list[int]:
        """Every UID in the folder - used to spot messages deleted elsewhere."""
        return self.search_raw(folder, "ALL")

    def present_uids(self, folder: str, uids: Sequence[int]) -> set[int]:
        """Which of these UIDs the server still has (used to verify a delete)."""
        if not uids:
            return set()
        return {status.uid for status in self.fetch_statuses(folder, uids)}

    def fetch_statuses(self, folder: str, uids: Optional[Iterable[int]] = None,
                       uid_range: str = "") -> list[MessageStatus]:
        """UID + FLAGS + size in one round trip.

        ``uid_range`` accepts an IMAP range such as ``"1201:*"``, which is how
        an incremental sync asks only for messages newer than everything it has
        already seen.
        """
        self.select(folder)
        if uid_range:
            message_set = uid_range
        else:
            message_set = _uid_set(uids) if uids is not None else "1:*"
        if not message_set:
            return []
        try:
            status, data = self.connection.uid("FETCH", message_set, "(UID FLAGS RFC822.SIZE)")
        except Exception as exc:
            raise _translate(exc, self.account) from exc
        if status != "OK" or not data:
            return []
        statuses: list[MessageStatus] = []
        for item in data:
            parsed = _parse_status_line(_as_bytes(item))
            if parsed is not None:
                statuses.append(parsed)
        return statuses

    # --------------------------------------------------------------- fetching
    def fetch_raw(self, folder: str, uid: int | str) -> Optional[bytes]:
        """Download one message without setting ``\\Seen``."""
        self.select(folder)
        try:
            status, data = self.connection.uid("FETCH", str(uid), "(BODY.PEEK[])")
        except Exception as exc:
            raise _translate(exc, self.account) from exc
        if status != "OK" or not data:
            return None
        for item in data:
            if isinstance(item, tuple) and len(item) > 1 and isinstance(item[1], (bytes, bytearray)):
                return bytes(item[1])
        return None

    def fetch_messages(
        self,
        folder: str,
        uids: Sequence[int],
        should_stop: Optional[Callable[[], bool]] = None,
        on_progress: Optional[Callable[[int, int], None]] = None,
    ) -> Iterator[RawMessage]:
        """Download several messages, newest first, yielding as they arrive."""
        total = len(uids)
        flags_by_uid = {s.uid: s.flags for s in self.fetch_statuses(folder, uids)} if uids else {}
        for index, uid in enumerate(uids, start=1):
            if should_stop is not None and should_stop():
                logger.info("Fetch cancelled", extra={"event": "fetch_cancelled",
                                                      "folder": folder, "done": index - 1})
                return
            try:
                raw = self.fetch_raw(folder, uid)
            except ReceiveError:
                raise
            except Exception:
                logger.warning("Could not fetch UID %s from %s", uid, folder, exc_info=True)
                continue
            if raw is None:
                logger.warning("Empty body for UID %s in %s", uid, folder)
                continue
            if on_progress is not None:
                on_progress(index, total)
            yield RawMessage(uid=str(uid), raw=raw, folder=folder,
                             flags=flags_by_uid.get(uid, frozenset()))

    # ------------------------------------------------------------------ flags
    def store_flags(self, folder: str, uids: Sequence[int], flags: Sequence[str],
                    add: bool = True) -> bool:
        """Add or remove IMAP flags on a set of messages."""
        if not uids or not flags:
            return True
        self.select(folder)
        command = "+FLAGS.SILENT" if add else "-FLAGS.SILENT"
        payload = "(" + " ".join(flags) + ")"
        try:
            status, _ = self.connection.uid("STORE", _uid_set(uids), command, payload)
        except Exception as exc:
            raise _translate(exc, self.account) from exc
        ok = status == "OK"
        logger.info("%s %s on %d message(s) in %s", "Added" if add else "Removed",
                    payload, len(uids), folder,
                    extra={"event": "store_flags", "folder": folder, "flags": list(flags),
                           "add": add, "count": len(uids), "ok": ok})
        return ok

    def mark_seen(self, uid: int | str, folder: Optional[str] = None) -> bool:
        """Kept for the original script's API (``read_email_with_imbox``)."""
        return self.store_flags(folder or self._selected or "INBOX", [int(uid)], ["\\Seen"], True)

    # ------------------------------------------------------- delete and move
    def move(self, folder: str, uids: Sequence[int],
             destination: str) -> tuple[bool, list[int]]:
        """Move messages, preferring the atomic MOVE extension (RFC 6851).

        Returns ``(ok, still_there)``; the source folder is re-checked so a
        server that accepts the command without acting on it is caught.
        """
        if not uids:
            return True, []
        self.select(folder)
        message_set = _uid_set(uids)
        try:
            moved = False
            if self.has_capability("MOVE"):
                status, _ = self.connection.uid("MOVE", message_set, quote_mailbox(destination))
                moved = status == "OK"
                if not moved:
                    logger.warning("MOVE failed, falling back to COPY+DELETE")

            if not moved:
                status, data = self.connection.uid(
                    "COPY", message_set, quote_mailbox(destination))
                if status != "OK":
                    detail = _as_bytes(data[0] if data else b"").decode("utf-8", "replace")
                    logger.warning("COPY to %s failed: %s", destination, detail)
                    return False, list(uids)
                self.connection.uid("STORE", message_set, "+FLAGS.SILENT", "(\\Deleted)")
                self._expunge(uids)

            remaining = sorted(self.present_uids(folder, uids))
            logger.info("Moved %d message(s) %s -> %s (%d left behind)",
                        len(uids) - len(remaining), folder, destination, len(remaining),
                        extra={"event": "move", "folder": folder,
                               "destination": destination, "count": len(uids),
                               "remaining": remaining[:20]})
            return not remaining, remaining
        except Exception as exc:
            raise _translate(exc, self.account) from exc

    def delete(self, folder: str, uids: Sequence[int], permanent: bool = False,
               trash_folder: Optional[str] = None) -> tuple[bool, list[int]]:
        """Move to Trash, or expunge for good when ``permanent`` is set.

        Returns ``(ok, still_there)``.  The server is asked afterwards whether
        the messages really went away: several servers (Gmail in particular)
        answer OK to a STORE/EXPUNGE that does not remove anything, and the old
        code reported success regardless, so the row vanished from the list
        while the mail was still in the mailbox.
        """
        if not uids:
            return True, []
        if not permanent and trash_folder and trash_folder != folder:
            return self.move(folder, uids, trash_folder)

        self.select(folder)
        try:
            status, _ = self.connection.uid(
                "STORE", _uid_set(uids), "+FLAGS.SILENT", "(\\Deleted)")
            if status != "OK":
                return False, list(uids)
            self._expunge(uids)
            remaining = sorted(self.present_uids(folder, uids))
        except Exception as exc:
            raise _translate(exc, self.account) from exc
        logger.info("Deleted %d message(s) from %s (%d left behind)",
                    len(uids) - len(remaining), folder, len(remaining),
                    extra={"event": "delete", "folder": folder, "count": len(uids),
                           "permanent": True, "remaining": remaining[:20]})
        return not remaining, remaining

    def _expunge(self, uids: Sequence[int]) -> None:
        """UID EXPUNGE when available - a plain EXPUNGE would also remove
        messages *other* clients had flagged ``\\Deleted``."""
        try:
            if self.has_capability("UIDPLUS"):
                self.connection.uid("EXPUNGE", _uid_set(uids))
            else:
                self.connection.expunge()
        except Exception:
            logger.warning("EXPUNGE failed", exc_info=True)

    # ----------------------------------------------------------------- append
    def append(self, folder: str, raw: bytes, flags: Sequence[str] = (),
               when: Optional[float] = None) -> bool:
        """Upload a message (used for Sent copies and drafts)."""
        self.connect()
        flag_text = "(" + " ".join(flags) + ")" if flags else None
        stamp = imaplib.Time2Internaldate(when or time.time())
        try:
            status, data = self.connection.append(
                quote_mailbox(folder), flag_text, stamp, raw
            )
        except Exception as exc:
            raise _translate(exc, self.account) from exc
        ok = status == "OK"
        logger.info("APPEND to %s: %s", folder, status,
                    extra={"event": "append", "folder": folder, "bytes": len(raw), "ok": ok})
        # APPEND invalidates the cached selection state on some servers.
        self._selected = None
        return ok

    # ---------------------------------------------- original notebook helper
    def fetch(
        self,
        criteria: str = "unread",
        limit: Optional[int] = None,
        should_stop: Optional[Callable[[], bool]] = None,
        folder: Optional[str] = None,
    ) -> Iterator[RawMessage]:
        """The original ``read_email_with_imbox`` flow, yielding raw messages."""
        target = folder or self.account.folder or "INBOX"
        uids = self.search_uids(target, criteria)
        uids.sort(reverse=True)          # newest first
        if limit is not None:
            uids = uids[:limit]
        yield from self.fetch_messages(target, uids, should_stop)

    def folders(self) -> list[str]:
        """Mailbox names (kept from the previous version of this module)."""
        return [folder.name for folder in self.list_folders()]


#: The class was called ``ImboxReceiver`` before it grew folder/flag support.
ImboxReceiver = ImapClient


def _imbox_query(criteria: str) -> dict[str, object]:
    """Translate a quick filter into imbox's ``messages()`` keywords."""
    if criteria == "today":
        today = datetime.date.today()
        return {"date__gt": datetime.date(today.year, today.month, today.day)}
    if criteria == "unread":
        return {"unread": True}
    if criteria == "flagged":
        return {"flagged": True}
    if criteria in ("all", "", None):
        return {}
    return {"sent_from": criteria}


def _uid_set(uids: Iterable[int]) -> str:
    return ",".join(str(int(uid)) for uid in uids)


def _translate(exc: Exception, account: AccountSettings) -> ReceiveError:
    """Turn protocol/socket errors into messages a user can act on."""
    import socket
    import ssl

    text = str(exc)
    lowered = text.lower()

    if isinstance(exc, TypeError) and "imbox" in text.lower():
        return ReceiveError(
            "The installed 'imbox' version has a different API than expected.\n"
            f"({text})\nTry:  pip install --upgrade imbox"
        )
    if isinstance(exc, socket.timeout) or "timed out" in lowered:
        return ConnectionLostError(
            f"{account.host} did not answer within {account.timeout} seconds. "
            "Check your connection and try again."
        )
    if isinstance(exc, ssl.SSLError) or "ssl" in lowered or "certificate" in lowered:
        return ReceiveError(
            f"Secure connection to {account.host}:{account.port} failed: {text}\n"
            "Check the port (993 for SSL, 143 for STARTTLS) and the SSL setting."
        )
    if isinstance(exc, socket.gaierror) or "getaddrinfo" in lowered or "name or service" in lowered:
        return ReceiveError(f"Cannot find the server {account.host}. Check the server name.")
    if isinstance(exc, (ConnectionResetError, BrokenPipeError, imaplib.IMAP4.abort)):
        return ConnectionLostError(f"The connection to {account.host} was lost: {text}")
    if isinstance(exc, ConnectionRefusedError) or "refused" in lowered:
        return ReceiveError(f"{account.host}:{account.port} refused the connection.")
    if isinstance(exc, imaplib.IMAP4.error):
        if any(word in lowered for word in
               ("auth", "login", "credential", "password", "invalid", "denied")):
            hint = ""
            host = account.host.lower()
            if "gmail" in host:
                hint = ("\n\nGmail needs a 16-character app password with 2-step "
                        "verification enabled; the normal password is always rejected.\n"
                        "https://myaccount.google.com/apppasswords")
            elif "outlook" in host or "office365" in host:
                hint = ("\n\nOutlook/Microsoft 365 usually needs an app password, and "
                        "IMAP must be enabled for the mailbox.")
            elif "yahoo" in host:
                hint = "\n\nYahoo requires an app password generated in account security."
            return AuthenticationError(f"Login failed for {account.username}.\n{text}{hint}")
        return ReceiveError(f"The mail server rejected the request: {text}")
    if isinstance(exc, OSError):
        return ConnectionLostError(f"Network error talking to {account.host}: {text}")
    return ReceiveError(f"IMAP error: {text}")


# --------------------------------------------------------------- local files
class EmlFileSource:
    """Reads ``.eml`` files from disk - no network needed."""

    @staticmethod
    def read(paths: Iterable[str | Path]) -> Iterator[RawMessage]:
        for path in paths:
            file_path = Path(path)
            try:
                raw = file_path.read_bytes()
            except OSError as exc:
                logger.warning("Could not read %s: %s", file_path, exc)
                continue
            yield RawMessage(uid=str(file_path), raw=raw, folder=str(file_path.parent))

    @staticmethod
    def read_folder(folder: str | Path, pattern: str = "*.eml") -> Iterator[RawMessage]:
        yield from EmlFileSource.read(sorted(Path(folder).glob(pattern)))
