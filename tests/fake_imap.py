"""An in-memory IMAP server double.

It implements enough of ``imaplib.IMAP4`` for both imbox and
:mod:`mail_receiver` to run against it, so the whole retrieval path - connect,
LIST, SEARCH, FETCH, STORE, COPY/MOVE, EXPUNGE, APPEND - can be tested without
credentials or a network.

Usage::

    with fake_imap_server(mailboxes={"INBOX": [...]}) as server:
        client = ImapClient(account)
        client.connect()
        ...
        server.commands          # every command the client issued
"""

from __future__ import annotations

import imaplib
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator, Optional

__all__ = ["FakeMessage", "FakeIMAP", "fake_imap_server", "sample_message"]


def sample_message(subject: str = "Test", body: str = "hello",
                   sender: str = "sender@example.com", charset: str = "utf-8") -> bytes:
    """A minimal but valid RFC 5322 message, encoded in ``charset``."""
    headers = (
        f"From: {sender}\r\n"
        f"To: receiver@example.com\r\n"
        f"Subject: {subject}\r\n"
        "Date: Fri, 24 Jul 2026 09:15:00 +0200\r\n"
        f"Message-ID: <{abs(hash(subject)):x}@example.com>\r\n"
        "MIME-Version: 1.0\r\n"
        f"Content-Type: text/plain; charset={charset}\r\n\r\n"
    )
    return headers.encode("ascii") + body.encode(charset, "replace") + b"\r\n"


@dataclass
class FakeMessage:
    uid: int
    raw: bytes
    flags: set[str] = field(default_factory=set)
    internaldate: str = '"24-Jul-2026 09:15:00 +0200"'

    @property
    def size(self) -> int:
        return len(self.raw)

    def flag_text(self) -> str:
        return " ".join(sorted(self.flags))


class FakeSocket:
    def __init__(self) -> None:
        self.timeout: Optional[float] = None

    def settimeout(self, value: float) -> None:
        self.timeout = value


class FakeIMAP:
    """Mimics the parts of ``imaplib.IMAP4_SSL`` that the client uses."""

    error = imaplib.IMAP4.error
    abort = imaplib.IMAP4.abort

    def __init__(
        self,
        mailboxes: Optional[dict[str, list[FakeMessage]]] = None,
        username: str = "user@example.com",
        password: str = "secret",
        capabilities: tuple[str, ...] = ("IMAP4REV1", "MOVE", "UIDPLUS", "IDLE"),
        folder_flags: Optional[dict[str, str]] = None,
        delimiter: str = "/",
        fetch_delay: float = 0.0,
    ) -> None:
        #: Artificial per-FETCH latency, to imitate a slow server in tests.
        self.fetch_delay = fetch_delay
        self.mailboxes: dict[str, list[FakeMessage]] = mailboxes or {"INBOX": []}
        self.username = username
        self.password = password
        self.capabilities = capabilities
        self.delimiter = delimiter
        self.folder_flags = folder_flags or {}
        self.selected: Optional[str] = None
        self.readonly = False
        self.logged_in = False
        self.logged_out = False
        self.commands: list[tuple] = []
        self.appended: list[tuple[str, bytes, Optional[str]]] = []
        self.uid_validity = 42
        self._next_uid = max(
            [m.uid for messages in self.mailboxes.values() for m in messages] or [0]
        ) + 1
        self._socket = FakeSocket()

    # ------------------------------------------------------------- plumbing
    def socket(self) -> FakeSocket:
        return self._socket

    def _record(self, *command: object) -> None:
        self.commands.append(tuple(command))

    def _folder(self, name: str) -> list[FakeMessage]:
        return self.mailboxes.setdefault(_unquote(name), [])

    def _current(self) -> list[FakeMessage]:
        if self.selected is None:
            raise self.error("No mailbox selected")
        return self.mailboxes.setdefault(self.selected, [])

    # -------------------------------------------------------------- session
    def login(self, username: str, password: str):
        self._record("login", username)
        if username != self.username or password != self.password:
            raise self.error(
                b"[AUTHENTICATIONFAILED] Invalid credentials (Failure)"
            )
        self.logged_in = True
        return "OK", [b"LOGIN completed"]

    def select(self, mailbox: str = "INBOX", readonly: bool = False):
        self._record("select", mailbox, readonly)
        name = _unquote(mailbox)
        if name not in self.mailboxes:
            return "NO", [b"Unknown Mailbox"]
        self.selected = name
        self.readonly = readonly
        return "OK", [str(len(self.mailboxes[name])).encode()]

    def close(self):
        self._record("close")
        self.selected = None
        return "OK", [b"CLOSE completed"]

    def logout(self):
        self._record("logout")
        self.logged_out = True
        return "BYE", [b"LOGOUT completed"]

    def noop(self):
        self._record("noop")
        return "OK", [b"NOOP completed"]

    def list(self, directory: str = '""', pattern: str = "*"):
        self._record("list", directory, pattern)
        rows: list[bytes] = []
        for name in self.mailboxes:
            flags = self.folder_flags.get(name, "\\HasNoChildren")
            rows.append(f'({flags}) "{self.delimiter}" "{name}"'.encode())
        return "OK", rows

    def status(self, mailbox: str, names: str):
        self._record("status", mailbox, names)
        name = _unquote(mailbox)
        if name not in self.mailboxes:
            return "NO", [b"Unknown Mailbox"]
        messages = self.mailboxes[name]
        unseen = sum(1 for m in messages if "\\Seen" not in m.flags)
        parts = []
        if "MESSAGES" in names:
            parts.append(f"MESSAGES {len(messages)}")
        if "UNSEEN" in names:
            parts.append(f"UNSEEN {unseen}")
        if "UIDVALIDITY" in names:
            parts.append(f"UIDVALIDITY {self.uid_validity}")
        return "OK", [f'"{name}" ({" ".join(parts)})'.encode()]

    def append(self, mailbox: str, flags, date_time, message):
        self._record("append", mailbox, flags)
        name = _unquote(mailbox)
        raw = message if isinstance(message, bytes) else str(message).encode()
        parsed_flags = set(re.findall(r"\\\w+", flags or ""))
        self.mailboxes.setdefault(name, []).append(
            FakeMessage(uid=self._next_uid, raw=raw, flags=parsed_flags)
        )
        self.appended.append((name, raw, flags))
        self._next_uid += 1
        return "OK", [b"[APPENDUID 42 1] APPEND completed"]

    def expunge(self):
        self._record("expunge")
        messages = self._current()
        remaining = [m for m in messages if "\\Deleted" not in m.flags]
        self.mailboxes[self.selected or "INBOX"] = remaining
        return "OK", [b"EXPUNGE completed"]

    # ------------------------------------------------------------------ uid
    def uid(self, command: str, *args):
        command = command.upper()
        self._record("uid", command, *args)
        handler = getattr(self, f"_uid_{command.lower()}", None)
        if handler is None:
            return "BAD", [f"Unsupported command {command}".encode()]
        return handler(*args)

    def _uid_search(self, charset, query):
        messages = self._current()
        matched = [m for m in messages if _matches(m, query)]
        return "OK", [" ".join(str(m.uid) for m in matched).encode()]

    def _uid_fetch(self, message_set: str, spec: str):
        messages = _select_set(self._current(), message_set)
        spec_upper = spec.upper()
        if self.fetch_delay and ("BODY.PEEK[]" in spec_upper or "BODY[]" in spec_upper):
            import time as _time

            _time.sleep(self.fetch_delay)
        response: list = []
        for index, message in enumerate(messages, start=1):
            if "BODY.PEEK[]" in spec_upper or "BODY[]" in spec_upper or "RFC822" == spec_upper:
                header = (f"{index} (UID {message.uid} "
                          f"FLAGS ({message.flag_text()}) "
                          f"BODY[] {{{message.size}}}").encode()
                response.append((header, message.raw))
                response.append(b")")
                if "BODY[]" in spec_upper and "PEEK" not in spec_upper:
                    message.flags.add("\\Seen")
            else:
                parts = [f"{index} (UID {message.uid}"]
                if "FLAGS" in spec_upper:
                    parts.append(f"FLAGS ({message.flag_text()})")
                if "RFC822.SIZE" in spec_upper:
                    parts.append(f"RFC822.SIZE {message.size}")
                response.append((" ".join(parts) + ")").encode())
        return "OK", response

    def _uid_store(self, message_set: str, command: str, flags: str):
        messages = _select_set(self._current(), message_set)
        parsed = set(re.findall(r"\\?\$?\w+", flags.strip("()")))
        parsed = {f if f.startswith(("\\", "$")) else "\\" + f for f in parsed}
        for message in messages:
            if command.startswith("+"):
                message.flags |= parsed
            elif command.startswith("-"):
                message.flags -= parsed
            else:
                message.flags = set(parsed)
        return "OK", [b"STORE completed"]

    def _uid_copy(self, message_set: str, mailbox: str):
        messages = _select_set(self._current(), message_set)
        target = self._folder(mailbox)
        for message in messages:
            target.append(FakeMessage(uid=self._next_uid, raw=message.raw,
                                      flags=set(message.flags)))
            self._next_uid += 1
        return "OK", [b"COPY completed"]

    def _uid_move(self, message_set: str, mailbox: str):
        status, _ = self._uid_copy(message_set, mailbox)
        if status != "OK":
            return status, [b"MOVE failed"]
        moved = {m.uid for m in _select_set(self._current(), message_set)}
        self.mailboxes[self.selected or "INBOX"] = [
            m for m in self._current() if m.uid not in moved
        ]
        return "OK", [b"MOVE completed"]

    def _uid_expunge(self, message_set: str):
        targets = {m.uid for m in _select_set(self._current(), message_set)}
        self.mailboxes[self.selected or "INBOX"] = [
            m for m in self._current()
            if not (m.uid in targets and "\\Deleted" in m.flags)
        ]
        return "OK", [b"EXPUNGE completed"]


def _unquote(name: str) -> str:
    if isinstance(name, bytes):
        name = name.decode()
    return str(name).strip().strip('"')


def _select_set(messages: list[FakeMessage], message_set: str) -> list[FakeMessage]:
    """Resolve ``1,2``, ``3:7`` and ``1:*`` against UIDs."""
    if not message_set:
        return []
    wanted: list[FakeMessage] = []
    by_uid = {m.uid: m for m in messages}
    for part in str(message_set).split(","):
        part = part.strip()
        if ":" in part:
            low, _, high = part.partition(":")
            low_value = int(low) if low.isdigit() else 1
            high_value = max(by_uid) if high.strip() == "*" and by_uid else (
                int(high) if high.strip().isdigit() else 0
            )
            wanted.extend(m for m in messages if low_value <= m.uid <= high_value)
        elif part.isdigit():
            message = by_uid.get(int(part))
            if message is not None:
                wanted.append(message)
    seen: set[int] = set()
    unique = []
    for message in wanted:
        if message.uid not in seen:
            seen.add(message.uid)
            unique.append(message)
    return unique


def _matches(message: FakeMessage, query: str) -> bool:
    """Support the subset of SEARCH keys that imbox and the client generate."""
    query = (query or "(ALL)").strip()
    if query.upper() in ("(ALL)", "ALL"):
        return True
    upper = query.upper()
    if "UNSEEN" in upper:
        return "\\Seen" not in message.flags
    if "UNFLAGGED" in upper:
        return "\\Flagged" not in message.flags
    if "FLAGGED" in upper:
        return "\\Flagged" in message.flags
    from_match = re.search(r'FROM\s+"([^"]*)"', query, re.IGNORECASE)
    if from_match:
        return from_match.group(1).lower() in message.raw.decode("latin-1").lower()
    text_match = re.search(r'TEXT\s+"([^"]*)"', query, re.IGNORECASE)
    if text_match:
        return text_match.group(1).lower() in message.raw.decode("latin-1").lower()
    subject_match = re.search(r'SUBJECT\s+"([^"]*)"', query, re.IGNORECASE)
    if subject_match:
        return subject_match.group(1).lower() in message.raw.decode("latin-1").lower()
    if "SINCE" in upper:
        return True          # the fake mailbox is "today"
    if "BEFORE" in upper:
        return False
    return True


class _FakeTransport:
    """Stands in for ``imbox.imap.ImapTransport``."""

    server_factory = None            # set by fake_imap_server()

    def __init__(self, hostname, port=None, ssl=True, ssl_context=None, starttls=False):
        self.hostname = hostname
        self.port = port or (993 if ssl else 143)
        self.server = type(self).server_factory()

    def list_folders(self):
        return self.server.list()

    def connect(self, username, password):
        self.server.login(username, password)
        self.server.select()
        return self.server


@contextmanager
def fake_imap_server(**kwargs) -> Iterator[FakeIMAP]:
    """Patch imbox so every connection lands on an in-memory server."""
    import imbox.imap
    import imbox.imbox

    server = FakeIMAP(**kwargs)

    class Transport(_FakeTransport):
        server_factory = staticmethod(lambda: server)

    original_imap = imbox.imap.ImapTransport
    original_imbox = imbox.imbox.ImapTransport
    imbox.imap.ImapTransport = Transport
    imbox.imbox.ImapTransport = Transport
    try:
        yield server
    finally:
        imbox.imap.ImapTransport = original_imap
        imbox.imbox.ImapTransport = original_imbox
