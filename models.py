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
    in_reply_to: str = ""
    references: list[str] = field(default_factory=list)
    html_body: str = ""
    text_body: str = ""
    attachments: list[Attachment] = field(default_factory=list)
    inline_images: list[Attachment] = field(default_factory=list)
    headers: list[tuple[str, str]] = field(default_factory=list)
    #: Non fatal problems found while parsing (bad charset, broken MIME, ...).
    warnings: list[str] = field(default_factory=list)
    raw_size: int = 0
    source: str = ""              # IMAP folder or file path, for the status bar
    folder: str = ""              # IMAP mailbox this message lives in
    flags: frozenset[str] = frozenset()   # IMAP flags, e.g. {"\\Seen", "\\Flagged"}

    # ---- summary mode ------------------------------------------------------
    # A message restored from the index has only what the list needs; the MIME
    # body is parsed on demand when it is opened.  ``loaded`` says which it is.
    loaded: bool = True
    preview_text: str = ""        # snippet stored in the index
    search_text: str = ""         # pre-computed search blob from the index
    attachment_count: int = 0     # attachment count known without parsing

    # -------------------------------------------------------------- IMAP state
    @property
    def uid_number(self) -> int:
        """Numeric IMAP UID, or 0 for messages loaded from a file."""
        try:
            return int(self.uid)
        except (TypeError, ValueError):
            return 0

    @property
    def _lower_flags(self) -> set[str]:
        return {f.lower() for f in self.flags}

    @property
    def is_read(self) -> bool:
        return "\\seen" in self._lower_flags

    @property
    def is_starred(self) -> bool:
        return "\\flagged" in self._lower_flags

    @property
    def is_answered(self) -> bool:
        return "\\answered" in self._lower_flags

    @property
    def is_draft(self) -> bool:
        return "\\draft" in self._lower_flags

    @property
    def is_important(self) -> bool:
        return "$important" in self._lower_flags

    def with_flags(self, flags: frozenset[str]) -> "Email":
        """Return the same message with different flags (models are shared)."""
        self.flags = frozenset(flags)
        return self

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
        return bool(self.attachments) or self.attachment_count > 0

    def preview(self, limit: int = 120) -> str:
        """Short one-line snippet for the message list."""
        if not self.loaded and self.preview_text:
            return self.preview_text[:limit]
        text = " ".join(self.text_body.split())
        if not text and self.html_body:
            from html_processor import html_to_text  # local import: avoid cycle

            text = " ".join(html_to_text(self.html_body).split())
        if not text:
            return self.preview_text[:limit]
        return text[:limit]

    def body_text(self) -> str:
        """Plain-text body, rendered from HTML when there is no text part."""
        if self.text_body:
            return self.text_body
        if self.html_body:
            from html_processor import html_to_text

            return html_to_text(self.html_body)
        # Summary from the index: the stored snippet is all we have until the
        # message is opened and its MIME body parsed.
        return self.preview_text

    def field_text(self, field: str) -> str:
        """Text used by the search box for one searchable field."""
        if field == "subject":
            return self.subject
        if field == "from":
            return format_addresses(self.from_addrs)
        if field == "to":
            return format_addresses(self.to_addrs) + " " + format_addresses(self.cc_addrs)
        if field == "date":
            return f"{self.display_date} {self.date_raw}"
        if field == "body":
            return self.body_text()
        if field == "attachment":
            if self.attachments or self.loaded:
                return " ".join(a.filename for a in self.attachments)
            return self.search_text
        return self.search_blob()

    def matches(self, query: str, field: str = "all") -> bool:
        """Case-insensitive substring match, used for incremental search."""
        needle = (query or "").strip().lower()
        if not needle:
            return True
        return needle in self.field_text(field).lower()

    def search_blob(self) -> str:
        """Everything the quick-filter box should match against."""
        if not self.loaded and self.search_text:
            return self.search_text
        parts = [
            self.subject,
            format_addresses(self.from_addrs),
            format_addresses(self.to_addrs),
            format_addresses(self.cc_addrs),
            self.display_date,
            self.date_raw,
            self.body_text(),
        ]
        parts.extend(a.filename for a in self.attachments)
        return "\n".join(p for p in parts if p).lower()
