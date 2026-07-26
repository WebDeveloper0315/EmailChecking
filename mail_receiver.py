"""E-mail retrieval - a thin wrapper around the existing imbox based script.

The retrieval logic from ``email-check.ipynb`` is kept exactly as it was:
``Imbox(IMAP_SERVER, username=..., password=..., ssl=True, ssl_context=None,
starttls=False)`` followed by the same four ``messages()`` queries
(``today`` / ``unread`` / ``all`` / sender address).

What this module adds is a *raw* view on the result.  imbox already exposes the
untouched RFC 5322 bytes on every message it yields (``message.raw_email``), so
the viewer can run its own RFC-compliant parser instead of imbox's simplified
one, without changing a single line of how mail is fetched.  When ``raw_email``
is unexpectedly missing we re-fetch that one message with ``BODY.PEEK[]``
through imbox's own IMAP connection - still no new retrieval logic, just the
standard IMAP command that does not flip the ``\\Seen`` flag.

A second, offline source (``EmlFileSource``) reads ``.eml`` files from disk so
the viewer can be used - and tested - without credentials.
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional

from config import AccountSettings

logger = logging.getLogger(__name__)

__all__ = [
    "RawMessage",
    "ReceiveError",
    "ImboxReceiver",
    "EmlFileSource",
    "FILTERS",
]

#: Selectable queries, mirroring ``read_email_with_imbox(flag)``.
FILTERS: tuple[tuple[str, str], ...] = (
    ("unread", "Unread"),
    ("today", "Today"),
    ("all", "All messages"),
)


class ReceiveError(RuntimeError):
    """Raised for connection / authentication / protocol problems."""


@dataclass
class RawMessage:
    """Untouched message bytes plus the IMAP metadata we need."""

    uid: str
    raw: bytes
    folder: str = "INBOX"

    @property
    def size(self) -> int:
        return len(self.raw)


class ImboxReceiver:
    """Fetches raw messages over IMAP using imbox.

    Usable as a context manager::

        with ImboxReceiver(settings.account) as receiver:
            for message in receiver.fetch("unread", limit=25):
                ...
    """

    def __init__(self, account: AccountSettings) -> None:
        self.account = account
        self._imbox = None  # type: ignore[var-annotated]

    # ------------------------------------------------------------ connection
    def connect(self) -> None:
        if self._imbox is not None:
            return
        try:
            from imbox import Imbox  # pip install imbox
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise ReceiveError(
                "The 'imbox' package is required to download mail.\n"
                "Install it with:  pip install imbox"
            ) from exc

        if not self.account.username or not self.account.password:
            raise ReceiveError("No user name or password configured.")

        logger.info("Connecting to %s as %s", self.account.host, self.account.username)
        try:
            # Same call as the original script.
            self._imbox = Imbox(
                self.account.host,
                username=self.account.username,
                password=self.account.password,
                ssl=self.account.use_ssl,
                ssl_context=None,
                starttls=self.account.starttls,
            )
        except Exception as exc:
            raise ReceiveError(_friendly_error(exc, self.account)) from exc

    def logout(self) -> None:
        if self._imbox is None:
            return
        try:
            self._imbox.logout()
        except Exception:
            logger.debug("Ignoring error during logout", exc_info=True)
        finally:
            self._imbox = None

    def __enter__(self) -> "ImboxReceiver":
        self.connect()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.logout()

    # --------------------------------------------------------------- fetching
    def fetch(
        self,
        criteria: str = "unread",
        limit: Optional[int] = None,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> Iterator[RawMessage]:
        """Yield raw messages matching ``criteria``.

        ``criteria`` is ``"unread"``, ``"today"``, ``"all"`` or a sender address
        - exactly the flags the original ``read_email_with_imbox`` accepted.
        Messages are yielded one by one so the UI can show them while the rest
        is still downloading.
        """
        self.connect()
        assert self._imbox is not None

        try:
            messages = self._select(criteria)
        except Exception as exc:
            raise ReceiveError(_friendly_error(exc, self.account)) from exc

        folder = self.account.folder
        count = 0
        iterator = iter(messages)
        while True:
            if should_stop is not None and should_stop():
                logger.info("Fetch cancelled after %d message(s)", count)
                return
            try:
                uid, message = next(iterator)
            except StopIteration:
                return
            except Exception as exc:
                # One unreadable message must not abort the whole download.
                logger.warning("Skipping a message that could not be fetched: %s", exc)
                continue

            uid_text = uid.decode("ascii", "replace") if isinstance(uid, bytes) else str(uid)
            raw = self._raw_bytes(uid_text, message)
            if raw is None:
                logger.warning("No raw content for uid %s, skipped", uid_text)
                continue

            yield RawMessage(uid=uid_text, raw=raw, folder=folder)
            count += 1
            if limit is not None and count >= limit:
                logger.info("Reached the limit of %d message(s)", limit)
                return

    def _select(self, criteria: str):
        """The four queries of the original script, unchanged."""
        imbox = self._imbox
        assert imbox is not None
        folder = self.account.folder or "INBOX"

        if criteria == "today":
            today = datetime.date.today()
            return imbox.messages(
                folder=folder,
                date__gt=datetime.date(today.year, today.month, today.day),
            )
        if criteria == "unread":
            return imbox.messages(folder=folder, unread=True)
        if criteria == "all":
            return imbox.messages(folder=folder)
        return imbox.messages(folder=folder, sent_from=criteria)

    def _raw_bytes(self, uid: str, message: object) -> Optional[bytes]:
        """imbox hands us the original bytes; fall back to a BODY.PEEK fetch."""
        raw = getattr(message, "raw_email", None)
        if isinstance(raw, bytes) and raw:
            return raw
        if isinstance(raw, str) and raw:
            return raw.encode("utf-8", errors="surrogateescape")
        return self._peek(uid)

    def _peek(self, uid: str) -> Optional[bytes]:
        """Re-fetch one message without marking it read."""
        try:
            connection = self._imbox.connection  # type: ignore[union-attr]
            status, data = connection.uid("fetch", uid, "(BODY.PEEK[])")
            if status != "OK" or not data:
                return None
            for item in data:
                if isinstance(item, tuple) and len(item) > 1 and isinstance(item[1], bytes):
                    return item[1]
        except Exception:
            logger.debug("BODY.PEEK fetch failed for uid %s", uid, exc_info=True)
        return None

    # ----------------------------------------------------------------- flags
    def mark_seen(self, uid: str) -> bool:
        """Flag a message as read (only called when the user opts in)."""
        if self._imbox is None:
            return False
        try:
            self._imbox.mark_seen(uid)
            return True
        except Exception:
            logger.warning("Could not mark uid %s as seen", uid, exc_info=True)
            return False

    def folders(self) -> list[str]:
        """Mailbox names, for the folder picker."""
        self.connect()
        try:
            status, data = self._imbox.connection.list()  # type: ignore[union-attr]
        except Exception:
            logger.debug("LIST failed", exc_info=True)
            return ["INBOX"]
        if status != "OK" or not data:
            return ["INBOX"]

        names: list[str] = []
        for row in data:
            text = row.decode("utf-8", "replace") if isinstance(row, bytes) else str(row)
            # `(\HasNoChildren) "/" "INBOX/Sub folder"`
            parts = text.split(' "')
            name = parts[-1].strip().strip('"') if parts else ""
            if name and name not in names:
                names.append(name)
        return names or ["INBOX"]


class EmlFileSource:
    """Reads ``.eml`` / ``.msg``-as-MIME files from disk - no network needed."""

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


def _friendly_error(exc: Exception, account: AccountSettings) -> str:
    """Translate IMAP failures into something a user can act on."""
    text = str(exc)
    lowered = text.lower()
    if "authentication" in lowered or "invalid credentials" in lowered or "login" in lowered:
        hint = ""
        if "gmail" in account.host.lower():
            hint = ("\n\nGmail requires an app password (16 characters, no spaces) "
                    "with 2-step verification enabled - your normal password will "
                    "always be rejected.")
        elif "outlook" in account.host.lower() or "office365" in account.host.lower():
            hint = ("\n\nOutlook/Microsoft 365 accounts usually need an app password "
                    "or OAuth; basic authentication is often disabled.")
        return f"Login failed for {account.username}: {text}{hint}"
    if "getaddrinfo" in lowered or "name or service" in lowered or "no address" in lowered:
        return f"Cannot reach {account.host}: the server name could not be resolved."
    if "timed out" in lowered or "timeout" in lowered:
        return f"Connection to {account.host}:{account.port} timed out."
    if "certificate" in lowered or "ssl" in lowered:
        return f"TLS problem while connecting to {account.host}: {text}"
    return f"IMAP error: {text}"
