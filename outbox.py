"""Queue for messages that could not be sent yet.

When SMTP is unreachable — a VPN that forbids mail ports, a flaky connection,
a laptop that woke up without Wi-Fi — the message is written to disk instead of
being lost, and retried with exponential backoff until the server accepts it.

Each queued message is two files, so the queue stays inspectable and survives a
crash:

    <id>.eml     the built message, exactly as it will be handed to SMTP
    <id>.json    envelope + bookkeeping (recipients, attempts, last error)

Only *retryable* failures are queued: a wrong password or a rejected recipient
would fail identically forever, so those are reported to the user instead.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

from logging_setup import get_logger

logger = get_logger("send", "mail.outbox")

__all__ = ["QueuedMessage", "Outbox"]

#: Backoff schedule: 1, 2, 4 … minutes, capped.
_BASE_DELAY = timedelta(minutes=1)
_MAX_DELAY = timedelta(minutes=30)
#: Give up (and tell the user) after this many failures.
MAX_ATTEMPTS = 20


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return _now()
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass
class QueuedMessage:
    """One message waiting to go out."""

    identifier: str
    account: str                      # profile name that must send it
    sender: str
    recipients: list[str]
    subject: str = ""
    message_id: str = ""
    attempts: int = 0
    last_error: str = ""
    created: str = field(default_factory=lambda: _now().isoformat())
    next_attempt: str = field(default_factory=lambda: _now().isoformat())
    #: Filled in when the message is loaded from disk.
    raw: bytes = field(default=b"", repr=False, compare=False)

    @property
    def due(self) -> bool:
        return _parse_time(self.next_attempt) <= _now()

    @property
    def exhausted(self) -> bool:
        return self.attempts >= MAX_ATTEMPTS

    def metadata(self) -> dict[str, object]:
        data = asdict(self)
        data.pop("raw", None)
        return data


class Outbox:
    """Disk-backed queue of unsent messages."""

    def __init__(self, directory: Path | str) -> None:
        self.directory = Path(directory)
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.warning("Cannot create the outbox folder %s", self.directory,
                           exc_info=True)

    # ------------------------------------------------------------- contents
    def _paths(self, identifier: str) -> tuple[Path, Path]:
        return (self.directory / f"{identifier}.eml",
                self.directory / f"{identifier}.json")

    def add(self, raw: bytes, sender: str, recipients: Iterable[str], account: str,
            subject: str = "", message_id: str = "", error: str = "") -> QueuedMessage:
        """Queue a message that could not be sent."""
        item = QueuedMessage(
            identifier=uuid.uuid4().hex[:16],
            account=account,
            sender=sender,
            recipients=[r for r in recipients if r],
            subject=subject,
            message_id=message_id,
            attempts=1 if error else 0,
            last_error=error,
            next_attempt=(_now() + _BASE_DELAY).isoformat() if error else _now().isoformat(),
            raw=raw,
        )
        eml_path, meta_path = self._paths(item.identifier)
        try:
            eml_path.write_bytes(raw)
            meta_path.write_text(json.dumps(item.metadata(), indent=2), encoding="utf-8")
        except OSError:
            logger.exception("Could not write the queued message to %s", self.directory)
        logger.info("Message queued in the outbox",
                    extra={"event": "outbox_add", "id": item.identifier,
                           "account": account, "recipients": len(item.recipients)})
        return item

    def all(self) -> list[QueuedMessage]:
        """Every queued message, oldest first, with its raw bytes loaded."""
        items: list[QueuedMessage] = []
        if not self.directory.exists():
            return items
        for meta_path in sorted(self.directory.glob("*.json")):
            try:
                data = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                logger.warning("Ignoring unreadable queue entry %s", meta_path)
                continue
            eml_path = meta_path.with_suffix(".eml")
            try:
                raw = eml_path.read_bytes()
            except OSError:
                logger.warning("Queued message %s has no body, dropping it", meta_path.stem)
                self._unlink(meta_path, eml_path)
                continue
            data.pop("raw", None)
            try:
                item = QueuedMessage(raw=raw, **data)
            except TypeError:
                logger.warning("Queue entry %s has an unexpected shape", meta_path.stem)
                continue
            items.append(item)
        items.sort(key=lambda entry: entry.created)
        return items

    def due(self, account: Optional[str] = None) -> list[QueuedMessage]:
        """Queued messages whose backoff has elapsed."""
        return [item for item in self.all()
                if item.due and not item.exhausted
                and (account is None or item.account == account)]

    def count(self) -> int:
        return len(list(self.directory.glob("*.json"))) if self.directory.exists() else 0

    # -------------------------------------------------------------- updates
    def record_failure(self, item: QueuedMessage, error: str) -> QueuedMessage:
        """Note a failed attempt and schedule the next one."""
        item.attempts += 1
        item.last_error = error
        delay = min(_BASE_DELAY * (2 ** max(0, item.attempts - 1)), _MAX_DELAY)
        item.next_attempt = (_now() + delay).isoformat()
        _, meta_path = self._paths(item.identifier)
        try:
            meta_path.write_text(json.dumps(item.metadata(), indent=2), encoding="utf-8")
        except OSError:
            logger.debug("Could not update %s", meta_path, exc_info=True)
        logger.info("Outbox retry failed (attempt %d)", item.attempts,
                    extra={"event": "outbox_retry_failed", "id": item.identifier,
                           "attempts": item.attempts, "next": item.next_attempt})
        return item

    def remove(self, item: QueuedMessage | str) -> None:
        identifier = item if isinstance(item, str) else item.identifier
        self._unlink(*reversed(self._paths(identifier)))
        logger.info("Message removed from the outbox",
                    extra={"event": "outbox_remove", "id": identifier})

    def clear(self) -> int:
        removed = 0
        for meta_path in list(self.directory.glob("*.json")):
            self._unlink(meta_path, meta_path.with_suffix(".eml"))
            removed += 1
        return removed

    @staticmethod
    def _unlink(*paths: Path) -> None:
        for path in paths:
            try:
                path.unlink()
            except OSError:
                pass
