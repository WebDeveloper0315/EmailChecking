"""Data model shared by the parser, the storage helpers and the UI.

The model deliberately knows nothing about IMAP, MIME or Qt: it is a plain
description of "an e-mail as a human wants to see it".  Everything that is
expensive (attachment payloads) is loaded lazily so that a 20 MB message can be
listed without decoding a single byte of its attachments.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional, Sequence

__all__ = [
    "Address",
    "Attachment",
    "Email",
    "format_addresses",
]


@dataclass(frozen=True)
class Address:
    """A single RFC 5322 mailbox (already RFC 2047 decoded)."""

    name: str = ""
    email: str = ""

    def __str__(self) -> str:
        if self.name and self.email:
            return f"{self.name} <{self.email}>"
        return self.name or self.email

    @property
    def short(self) -> str:
        """Name if we have one, address otherwise - what a mail client shows."""
        return self.name or self.email


def format_addresses(addresses: Sequence[Address]) -> str:
    """Join addresses the way mail clients render a header line."""
    return ", ".join(str(a) for a in addresses if str(a))


@dataclass
class Attachment:
    """A file-like MIME part: real attachment or inline (CID) image."""

    filename: str
    content_type: str = "application/octet-stream"
    size: int = 0
    content_id: str = ""          # without the surrounding angle brackets
    is_inline: bool = False
    charset: str = ""
    #: Decodes the payload on demand; set by the parser.
    loader: Optional[Callable[[], bytes]] = field(default=None, repr=False, compare=False)
    _data: Optional[bytes] = field(default=None, repr=False, compare=False)

    @property
    def data(self) -> bytes:
        """Decoded payload.  Decoded once, then cached."""
        if self._data is None:
            if self.loader is None:
                return b""
            try:
                self._data = self.loader() or b""
            except Exception:  # pragma: no cover - defensive, see mail_parser
                self._data = b""
            # Now that the real payload is known, correct any size estimate.
            self.size = len(self._data)
        return self._data

    def release(self) -> None:
        """Drop the cached payload (it can always be decoded again)."""
        self._data = None

    @property
    def is_image(self) -> bool:
        return self.content_type.lower().startswith("image/")


@dataclass
class Email:
    """A parsed message, ready to be displayed."""

    uid: str = ""
    subject: str = ""
    from_addrs: list[Address] = field(default_factory=list)
    to_addrs: list[Address] = field(default_factory=list)
    cc_addrs: list[Address] = field(default_factory=list)
    bcc_addrs: list[Address] = field(default_factory=list)
    reply_to: list[Address] = field(default_factory=list)
    date: Optional[datetime] = None
    date_raw: str = ""
    message_id: str = ""
    html_body: str = ""
    text_body: str = ""
    attachments: list[Attachment] = field(default_factory=list)
    inline_images: list[Attachment] = field(default_factory=list)
    headers: list[tuple[str, str]] = field(default_factory=list)
    #: Non fatal problems found while parsing (bad charset, broken MIME, ...).
    warnings: list[str] = field(default_factory=list)
    raw_size: int = 0
    source: str = ""              # IMAP folder or file path, for the status bar

    # ------------------------------------------------------------------ helpers
    @property
    def sender(self) -> str:
        return format_addresses(self.from_addrs) or "(unknown sender)"

    @property
    def sender_short(self) -> str:
        return self.from_addrs[0].short if self.from_addrs else "(unknown sender)"

    @property
    def display_subject(self) -> str:
        return self.subject or "(no subject)"

    @property
    def display_date(self) -> str:
        if self.date is None:
            return self.date_raw
        return self.date.strftime("%Y-%m-%d %H:%M")

    @property
    def sort_key(self) -> float:
        """Timestamp used for sorting; undated mail sorts last."""
        return self.date.timestamp() if self.date else 0.0

    @property
    def has_html(self) -> bool:
        return bool(self.html_body.strip())

    @property
    def has_attachments(self) -> bool:
        return bool(self.attachments)

    def preview(self, limit: int = 120) -> str:
        """Short one-line snippet for the message list."""
        text = " ".join(self.text_body.split())
        if not text and self.html_body:
            from html_processor import html_to_text  # local import: avoid cycle

            text = " ".join(html_to_text(self.html_body).split())
        return text[:limit]

    def search_blob(self) -> str:
        """Everything the quick-filter box should match against."""
        parts = [
            self.subject,
            format_addresses(self.from_addrs),
            format_addresses(self.to_addrs),
            self.text_body,
        ]
        if not self.text_body and self.html_body:
            from html_processor import html_to_text

            parts.append(html_to_text(self.html_body))
        parts.extend(a.filename for a in self.attachments)
        return "\n".join(p for p in parts if p).lower()
